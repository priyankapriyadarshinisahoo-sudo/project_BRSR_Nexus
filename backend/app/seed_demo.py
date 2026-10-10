"""Collection demo data: assignments and MetricValues for two reporting
periods, with the demo anomalies required by the specification:

1. Section A employee count != Section C workforce count (Plant Beta, FY2025-26)
2. Energy YoY +70% (Plant Alpha: 100 MWh FY24 -> 170,000 kWh draft FY25)
3. A BRSR Core metric with no evidence attached (Plant Delta CSR spend)
4. Ratio/intensity metrics ready for correct-consolidation demos (Phase 10)

All numbers are synthetic. FY2024-25 values are LOCKED (period locked);
FY2025-26 is deliberately mid-flight across plants:
  Alpha IN_PROGRESS drafts (owner demo), Beta APPROVED (with anomaly),
  Gamma/Delta SUBMITTED (review queue), Epsilon IN_PROGRESS,
  Project C APPROVED, Project D + Zeta NOT_STARTED ("6 of 8" dashboards).
"""
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.models import (
    AppUser,
    Assignment,
    Entity,
    FormulaDefinition,
    FrameworkVersion,
    MetricDefinition,
    MetricValue,
    ReportingPeriod,
)
from app.models.enums import (
    AssignmentStatus,
    AuditAction,
    MetricValueStatus,
    UserRole,
)

logger = logging.getLogger(__name__)

# metric_code -> (unit, fy24 value, fy25 value)
DEMO_METRICS: dict[str, tuple[str | None, float, float]] = {
    "C-P6-GRID-NONRENEWABLE-MWH": ("MWh", 100.0, 170.0),
    "C-P6-GRID-RENEWABLE-MWH": ("MWh", 20.0, 30.0),
    "C-P6-DIESEL-LITRES": ("litre", 12000.0, 11000.0),
    "C-P6-WATER-WITHDRAWAL": ("kL", 5000.0, 5400.0),
    "C-P6-WATER-DISCHARGE": ("kL", 3000.0, 3200.0),
    "C-P6-WATER-CONSUMPTION": ("kL", 2000.0, 2200.0),
    "C-P6-WASTE-GENERATED": ("tonne", 120.0, 135.0),
    "C-P6-WASTE-RECYCLED": ("tonne", 30.0, 40.0),
    "A-EMPLOYEES-PAYROLL": (None, 210.0, 215.0),
    "A-EMPLOYEES-LEFT": (None, 18.0, 20.0),
    "C-P3-WORKFORCE-TOTAL": (None, 210.0, 215.0),
    "A-REVENUE": ("INR crore", 500.0, 540.0),
    "C-P6-SCOPE2-TCO2E": ("tCO2e", 82.0, 139.0),
    "C-P3-LTI-COUNT": (None, 1.0, 0.0),
    "C-P3-MANHOURS": (None, 1_100_000.0, 1_150_000.0),
    "C-P8-CSR-SPEND": ("INR crore", 1.2, 1.5),
    "C-P9-DATA-BREACHES": (None, 0.0, 0.0),
}

# FY2024-25 framework version does not contain these metrics
FY24_EXCLUDED = {"C-P6-WATER-DISCHARGE", "C-P6-WASTE-RECYCLED"}

PLANT_FACTOR: dict[str, float] = {
    "Plant Alpha": 1.0,
    "Plant Beta": 1.2,
    "Plant Gamma": 0.8,
    "Plant Delta": 1.5,
    "Project C": 0.6,
    "Project D": 0.9,
    "Plant Epsilon": 1.1,
    "Plant Zeta": 0.7,
}

# FY2025-26 target assignment status per plant (demo mid-flight state)
FY25_STATUS: dict[str, AssignmentStatus] = {
    "Plant Alpha": AssignmentStatus.IN_PROGRESS,
    "Plant Beta": AssignmentStatus.APPROVED,
    "Plant Gamma": AssignmentStatus.SUBMITTED,
    "Plant Delta": AssignmentStatus.SUBMITTED,
    "Project C": AssignmentStatus.APPROVED,
    "Project D": AssignmentStatus.NOT_STARTED,
    "Plant Epsilon": AssignmentStatus.IN_PROGRESS,
    "Plant Zeta": AssignmentStatus.NOT_STARTED,
}

OWNER_FOR_PLANT: dict[str, str] = {
    "Plant Alpha": "owner-alpha@example.local",
    "Plant Beta": "owner-beta@example.local",
    "Plant Gamma": "owner-gamma@example.local",
    "Plant Delta": "owner-delta@example.local",
    "Project C": "owner-projc@example.local",
    "Project D": "owner-projc@example.local",
    "Plant Epsilon": "owner-eps@example.local",
    "Plant Zeta": "owner-eps@example.local",
}


# Revenue deliberately decoupled from activity scale for two plants so the
# §5.6 consolidation demo shows Σnum/Σden ≠ naive average of intensities:
# Delta is emissions-heavy (small revenue, big GHG), Zeta is efficient.
REVENUE_FACTOR: dict[str, float] = {
    "Plant Delta": 0.4,
    "Plant Zeta": 2.2,
}

# Governance demo scenario (master prompt §23): Plant Beta Scope 1 is the
# CRITICAL, fully-controlled work item. Diesel consumption is scaled so the
# calculated Scope 1 is 42,381 tCO2e (FY2025-26); submitting a corrected
# diesel figure of 16,089,552.24 L during the demo recomputes Scope 1 to
# 43,120 tCO2e and triggers the ripple engine. FY2024-25 keeps the YoY
# delta believable (~+4%).
BETA_DIESEL_LITRES = {
    "FY2024-25": 15192163.0,
    "FY2025-26": 15813805.97,
}


def _plant_value(code: str, plant: str, fy25: bool) -> float:
    base = DEMO_METRICS[code]
    value = base[2] if fy25 else base[1]
    if code == "C-P6-DIESEL-LITRES" and plant == "Plant Beta":
        return BETA_DIESEL_LITRES["FY2025-26" if fy25 else "FY2024-25"]
    factor = PLANT_FACTOR.get(plant, 1.0)
    if code == "A-REVENUE":
        factor = REVENUE_FACTOR.get(plant, factor)
    # Energy YoY anomaly is exact for Plant Alpha; others scale both years equally
    if code == "C-P6-GRID-NONRENEWABLE-MWH" and plant != "Plant Alpha":
        return round(value * factor)
    return round(value * factor, 2) if factor != 1.0 else value


def _seed_assignment_value(
    db: Session,
    *,
    metric_code: str,
    entity: Entity,
    period: ReportingPeriod,
    owner: AppUser,
    status_a: AssignmentStatus,
    value: float | None,
    raw_unit: str | None,
    created_by: AppUser,
) -> None:
    exists = db.scalar(
        select(Assignment).where(
            Assignment.metric_code == metric_code,
            Assignment.entity_id == entity.id,
            Assignment.period_id == period.id,
        )
    )
    if exists is not None:
        return
    assignment = Assignment(
        metric_code=metric_code,
        framework_version_id=period.framework_version_id,
        entity_id=entity.id,
        period_id=period.id,
        owner_user_id=owner.id,
        due_date=period.end_date,
        status=status_a,
        created_by=created_by.id,
    )
    db.add(assignment)
    db.flush()

    if value is None:
        return

    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == period.framework_version_id,
            MetricDefinition.metric_code == metric_code,
        )
    )
    is_numeric = metric is not None and metric.data_type.value == "numeric"
    normalized_value = normalized_unit = None
    if is_numeric and value is not None:
        from app.normalization.units import UnitConversionError, normalize_value

        try:
            normalized_value, normalized_unit = normalize_value(
                value, raw_unit, metric.unit_family, metric.canonical_unit
            )
        except UnitConversionError as exc:
            logger.error("seed value not normalizable for %s: %s", metric_code, exc)
            raise
    value_status = MetricValueStatus.LOCKED if status_a == AssignmentStatus.LOCKED \
        else MetricValueStatus(status_a.value)
    mv = MetricValue(
        assignment_id=assignment.id,
        version=1,
        raw_value=Decimal(str(value)),
        raw_unit=raw_unit if is_numeric else None,
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
        qualitative_value=None,
        status=value_status,
        created_by=owner.id,
        submitted_by=owner.id,
        submitted_at=period.end_date,
    )
    db.add(mv)
    db.flush()
    record(
        db, action=AuditAction.SUBMITTED, object_type="metric_value", object_id=mv.id,
        actor_id=owner.id, actor_label=owner.email,
        entity_id=entity.id, metric_code=metric_code,
        new_value={"version": 1, "raw_value": str(value), "raw_unit": raw_unit},
    )


def seed_collection(db: Session, entities: dict[str, Entity], esg_manager: AppUser, reviewer: AppUser) -> None:
    periods = {
        p.label: p
        for p in db.scalars(select(ReportingPeriod)).all()
    }
    owners = {
        u.email: u for u in db.scalars(select(AppUser).where(AppUser.role == UserRole.DATA_OWNER)).all()
    }
    # metric availability per framework version
    available: dict[str, set[str]] = {}
    for fv in db.scalars(select(FrameworkVersion)).all():
        available[fv.id] = set(
            db.scalars(
                select(MetricDefinition.metric_code).where(
                    MetricDefinition.framework_version_id == fv.id
                )
            ).all()
        )

    for plant_name, status_fy25 in FY25_STATUS.items():
        plant = entities.get(plant_name)
        if plant is None:
            continue
        owner = owners[OWNER_FOR_PLANT[plant_name]]
        for fy_label, locked in (("FY2024-25", True), ("FY2025-26", False)):
            period = periods[fy_label]
            fy25 = not locked
            for code, (unit, _fy24, _fy25) in DEMO_METRICS.items():
                if locked and code in FY24_EXCLUDED:
                    continue
                if code not in available.get(period.framework_version_id, set()):
                    continue
                if fy25 and status_fy25 == AssignmentStatus.NOT_STARTED:
                    # create the assignment shell only (no values)
                    _seed_assignment_value(
                        db, metric_code=code, entity=plant, period=period, owner=owner,
                        status_a=AssignmentStatus.NOT_STARTED, value=None, raw_unit=unit,
                        created_by=esg_manager,
                    )
                    continue
                if fy25 and code == "C-P6-GRID-NONRENEWABLE-MWH" and plant_name == "Plant Alpha":
                    # demo anomaly #2: owner's un-submitted draft in kWh (+70% YoY)
                    _seed_assignment_value(
                        db, metric_code=code, entity=plant, period=period, owner=owner,
                        status_a=AssignmentStatus.IN_PROGRESS, value=170000.0,
                        raw_unit="kWh", created_by=esg_manager,
                    )
                    continue
                _seed_assignment_value(
                    db, metric_code=code, entity=plant, period=period, owner=owner,
                    status_a=AssignmentStatus.LOCKED if locked else status_fy25,
                    value=_plant_value(code, plant_name, fy25),
                    raw_unit=unit, created_by=esg_manager,
                )

    # demo anomaly #1: Beta FY25 Section A employees != Section C workforce
    beta = entities["Plant Beta"]
    beta_period = periods["FY2025-26"]
    employees = db.scalar(
        select(Assignment).where(
            Assignment.metric_code == "A-EMPLOYEES-PAYROLL",
            Assignment.entity_id == beta.id,
            Assignment.period_id == beta_period.id,
        )
    )
    workforce = db.scalar(
        select(Assignment).where(
            Assignment.metric_code == "C-P3-WORKFORCE-TOTAL",
            Assignment.entity_id == beta.id,
            Assignment.period_id == beta_period.id,
        )
    )
    if employees is not None and workforce is not None:
        emp_value = db.scalar(
            select(MetricValue)
            .where(MetricValue.assignment_id == employees.id)
            .order_by(MetricValue.version.desc())
            .limit(1)
        )
        wf_value = db.scalar(
            select(MetricValue)
            .where(MetricValue.assignment_id == workforce.id)
            .order_by(MetricValue.version.desc())
            .limit(1)
        )
        if emp_value is not None:
            emp_value.raw_value = Decimal("252")
            emp_value.normalized_value = Decimal("252")
            emp_value.normalized_unit = "count"
        if wf_value is not None:
            wf_value.raw_value = Decimal("240")
            wf_value.normalized_value = Decimal("240")
            wf_value.normalized_unit = "count"
        db.flush()
        logger.info("demo anomaly: Beta FY25 employees=252 vs workforce=240")

    _seed_derived_metrics(db, entities, periods, owners, esg_manager)

    _seed_assurance_request(db, entities, periods)

    db.flush()
    logger.info("collection demo data seeded")


def _seed_assurance_request(db: Session, entities, periods) -> None:
    """Governance demo (master prompt §22): one OPEN assurance request from
    the assessor on the CRITICAL Scope 1 metric, waiting for the owner."""
    from datetime import date, timedelta

    from sqlalchemy import select as _select

    from app.audit.service import record as audit_record
    from app.models import AppUser, AssuranceRequest
    from app.models.enums import AssuranceRequestStatus, AssuranceRequestType, AuditAction

    exists = db.scalar(_select(AssuranceRequest).limit(1))
    if exists is not None:
        return
    assessor = db.scalar(_select(AppUser).where(AppUser.email == "assessor@example.local"))
    owner_beta = db.scalar(_select(AppUser).where(AppUser.email == "owner-beta@example.local"))
    beta = entities.get("Plant Beta")
    period = periods.get("FY2025-26")
    if assessor is None or owner_beta is None or beta is None or period is None:
        return
    req = AssuranceRequest(
        request_type=AssuranceRequestType.EVIDENCE,
        status=AssuranceRequestStatus.ASSIGNED,
        period_id=period.id, entity_id=beta.id,
        metric_code="C-P6-SCOPE1-TCO2E",
        requested_by=assessor.id, assignee_id=owner_beta.id,
        reason=(
            "Provide the supporting evidence package for the FY2025-26 Scope 1 "
            "recalculation: fuel purchase records, calculation methodology and "
            "the emission factor source applied."
        ),
        due_date=date.today() + timedelta(days=14),
        comments=[],
    )
    db.add(req)
    db.flush()
    audit_record(
        db, action=AuditAction.CREATED, object_type="assurance_request",
        object_id=req.id, actor_id=assessor.id, actor_label=assessor.email,
        entity_id=beta.id, metric_code="C-P6-SCOPE1-TCO2E",
        new_value={"type": "EVIDENCE", "reason": req.reason[:160]},
    )
    logger.info("seeded demo assurance request for Plant Beta Scope 1")


DERIVED_METRICS = ("C-P6-TOTAL-ENERGY", "C-P6-SCOPE1-TCO2E", "C-P6-TOTAL-GHG")


def _formula_id(db: Session, framework_version_id, code: str):
    definition = db.scalar(
        select(FormulaDefinition).where(
            FormulaDefinition.framework_version_id == framework_version_id,
            FormulaDefinition.code == code,
        )
    )
    return definition.id if definition else None


def _seed_derived_metrics(db: Session, entities, periods, owners, esg_manager) -> None:
    """FY2024-25: computed historical values (locked, with full formula
    lineage). FY2025-26: Plant Beta carries the governed demo scenario —
    its derived metrics are APPROVED with full formula lineage (Scope 1 =
    42,381 tCO2e, CRITICAL control path); other plants keep assignment
    shells empty for the calculation engine to fill during the demo."""
    from app.models import FormulaDefinition

    for plant_name in FY25_STATUS:
        plant = entities.get(plant_name)
        if plant is None:
            continue
        owner = owners[OWNER_FOR_PLANT[plant_name]]
        for fy_label, locked in (("FY2024-25", True), ("FY2025-26", False)):
            period = periods[fy_label]
            fv_id = period.framework_version_id
            if not locked and plant_name != "Plant Beta":
                for code in DERIVED_METRICS:
                    _seed_assignment_value(
                        db, metric_code=code, entity=plant, period=period, owner=owner,
                        status_a=AssignmentStatus.IN_PROGRESS, value=None, raw_unit=None,
                        created_by=esg_manager,
                    )
                continue

            fy25 = not locked
            renewable = _plant_value("C-P6-GRID-RENEWABLE-MWH", plant_name, fy25=fy25)
            nonrenewable = _plant_value("C-P6-GRID-NONRENEWABLE-MWH", plant_name, fy25=fy25)
            diesel = _plant_value("C-P6-DIESEL-LITRES", plant_name, fy25=fy25)
            scope2 = _plant_value("C-P6-SCOPE2-TCO2E", plant_name, fy25=fy25)
            total_energy = renewable + nonrenewable
            scope1 = round(diesel * 2.68 / 1000, 8)
            total_ghg = round(scope1 + scope2, 8)

            computed = {
                "C-P6-TOTAL-ENERGY": (
                    total_energy, "MWh", "F-TOTAL-ENERGY",
                    {"C-P6-GRID-RENEWABLE-MWH": str(renewable),
                     "C-P6-GRID-NONRENEWABLE-MWH": str(nonrenewable)},
                ),
                "C-P6-SCOPE1-TCO2E": (
                    scope1, "tCO2e", "F-SCOPE1",
                    {"C-P6-DIESEL-LITRES": str(diesel), "DIESEL_EF_KG_PER_LITRE": "2.68"},
                ),
                "C-P6-TOTAL-GHG": (
                    total_ghg, "tCO2e", "F-TOTAL-GHG",
                    {"C-P6-SCOPE1-TCO2E": str(scope1), "C-P6-SCOPE2-TCO2E": str(scope2)},
                ),
            }
            for code, (val, unit, formula_code, inputs) in computed.items():
                exists = db.scalar(
                    select(Assignment).where(
                        Assignment.metric_code == code,
                        Assignment.entity_id == plant.id,
                        Assignment.period_id == period.id,
                    )
                )
                if exists is not None:
                    continue
                status_a = AssignmentStatus.LOCKED if locked else AssignmentStatus.APPROVED
                assignment = Assignment(
                    metric_code=code, framework_version_id=fv_id, entity_id=plant.id,
                    period_id=period.id, owner_user_id=owner.id, due_date=period.end_date,
                    status=status_a, created_by=esg_manager.id,
                )
                db.add(assignment)
                db.flush()
                value_status = MetricValueStatus.LOCKED if locked else MetricValueStatus.APPROVED
                mv = MetricValue(
                    assignment_id=assignment.id, version=1,
                    raw_value=Decimal(str(val)), raw_unit=unit,
                    normalized_value=Decimal(str(val)), normalized_unit=unit,
                    is_calculated=True, formula_id=_formula_id(db, fv_id, formula_code),
                    formula_version=1, formula_inputs=inputs,
                    status=value_status, created_by=owner.id,
                    submitted_by=owner.id, submitted_at=period.end_date,
                )
                db.add(mv)
                db.flush()
                record(
                    db, action=AuditAction.SUBMITTED, object_type="metric_value",
                    object_id=mv.id, actor_id=owner.id, actor_label=owner.email,
                    entity_id=plant.id, metric_code=code,
                    new_value={"version": 1, "result": str(val), "formula": formula_code},
                )
