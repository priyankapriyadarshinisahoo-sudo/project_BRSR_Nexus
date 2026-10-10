"""Dashboard aggregation API (spec §19 — read-only, everything from real data).

Aggregates the read models (assignments, exceptions, consolidation traces,
BRSR Core metadata) into one role-scoped summary for the management dashboard.
Never fabricates numbers: absent traces/periods are returned as null with an
explicit note.
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_scoped_entity_ids
from app.db.session import get_db
from app.models import (
    AppUser,
    Assignment,
    ConsolidationTrace,
    Entity,
    Evidence,
    MetricDefinition,
    MetricValue,
    ReportingPeriod,
    ValidationException,
)
from app.models.enums import (
    AssignmentStatus,
    MetricValueStatus,
    UserRole,
    ValidationExceptionStatus,
    ValidationSeverity,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

KPI_CODES = [
    "C-P6-TOTAL-ENERGY",
    "C-P6-TOTAL-GHG",
    "C-P6-GHG-INTENSITY",
    "C-P6-ENERGY-INTENSITY",
    "C-P6-WATER-WITHDRAWAL",
]
SCOPE_CODES = {"C-P6-SCOPE1-TCO2E": "Scope 1", "C-P6-SCOPE2-TCO2E": "Scope 2"}


class KpiOut(BaseModel):
    metric_code: str
    label: str
    unit: str | None
    fy24: float | None = None
    fy25: float | None = None
    yoy_pct: float | None = None
    fy25_complete: bool | None = None


class DashboardSummary(BaseModel):
    ratio_demo: dict | None = None
    period_id: uuid.UUID
    assignments_total: int
    assignments_by_status: dict[str, int]
    approved: int
    overdue: list[dict]
    exceptions_open: dict[str, int]
    top_exception_rules: list[dict]
    core_readiness: dict
    kpis: list[KpiOut]
    scope_split: dict[str, float | None]
    entity_comparison: list[dict]
    notes: list[str]


def _group_entity(db: Session) -> Entity | None:
    return db.scalar(select(Entity).where(Entity.name == "MEIL Group"))


def _trace_value(db: Session, entity_id: uuid.UUID, metric_code: str, period_id: uuid.UUID):
    trace = db.scalar(
        select(ConsolidationTrace).where(
            ConsolidationTrace.entity_id == entity_id,
            ConsolidationTrace.metric_code == metric_code,
            ConsolidationTrace.period_id == period_id,
        )
    )
    if trace is None or trace.computed_value is None:
        return None, None, trace
    return float(trace.computed_value), trace.unit, trace


def _period_by_label(db: Session, label: str) -> ReportingPeriod | None:
    return db.scalar(select(ReportingPeriod).where(ReportingPeriod.label == label))


@router.get("/summary", response_model=DashboardSummary)
def summary(
    period_id: uuid.UUID = Query(default=None),
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> DashboardSummary:
    period = db.get(ReportingPeriod, period_id) if period_id else _period_by_label(db, "FY2025-26")
    if period is None:
        period = db.scalar(select(ReportingPeriod).order_by(ReportingPeriod.start_date.desc()))
    notes: list[str] = []
    all_visible = user.role in (UserRole.ADMIN, UserRole.ESG_MANAGER)

    # ---- assignments ----
    stmt = select(Assignment).where(Assignment.period_id == period.id)
    if not all_visible:
        stmt = stmt.where(Assignment.entity_id.in_(scoped_ids))
    assignments = list(db.scalars(stmt).all())
    by_status: dict[str, int] = {}
    for a in assignments:
        by_status[a.status.value] = by_status.get(a.status.value, 0) + 1
    approved = sum(
        by_status.get(s, 0) for s in ("APPROVED", "LOCKED")
    )
    today = date.today()
    overdue = [
        {
            "assignment_id": str(a.id),
            "metric_code": a.metric_code,
            "entity_id": str(a.entity_id),
            "due_date": str(a.due_date),
            "status": a.status.value,
        }
        for a in assignments
        if a.due_date and a.due_date < today
        and a.status not in (AssignmentStatus.APPROVED, AssignmentStatus.LOCKED)
    ][:6]
    entity_names = {
        str(e.id): e.name for e in db.scalars(select(Entity)).all()
    }
    for o in overdue:
        o["entity_name"] = entity_names.get(o["entity_id"], "?")

    # ---- exceptions ----
    ex_stmt = select(ValidationException).where(
        ValidationException.period_id == period.id,
        ValidationException.status != ValidationExceptionStatus.RESOLVED,
    )
    if not all_visible:
        ex_stmt = ex_stmt.where(ValidationException.entity_id.in_(scoped_ids))
    exceptions = list(db.scalars(ex_stmt).all())
    ex_by_sev = {"BLOCKING": 0, "WARNING": 0, "INFO": 0}
    rule_counts: dict[str, int] = {}
    for e in exceptions:
        ex_by_sev[e.severity.value] = ex_by_sev.get(e.severity.value, 0) + 1
        rule_counts[e.rule_code] = rule_counts.get(e.rule_code, 0) + 1
    top_rules = sorted(rule_counts.items(), key=lambda kv: -kv[1])[:5]

    # ---- BRSR Core readiness (for the period's framework) ----
    core_metrics = list(
        db.scalars(
            select(MetricDefinition).where(
                MetricDefinition.framework_version_id == period.framework_version_id,
                MetricDefinition.brsr_core.is_(True),
            )
        ).all()
    )
    core_total = len(core_metrics)
    approved_assignments = {a.metric_code: a for a in assignments
                            if a.status in (AssignmentStatus.APPROVED, AssignmentStatus.LOCKED)}
    core_approved = 0
    core_evidence = 0
    for m in core_metrics:
        a = approved_assignments.get(m.metric_code)
        if a is None:
            continue
        core_approved += 1
        value = db.scalar(
            select(MetricValue)
            .where(MetricValue.assignment_id == a.id)
            .order_by(MetricValue.version.desc())
            .limit(1)
        )
        if value is not None and db.scalar(
            select(Evidence.id).where(
                Evidence.metric_value_id == value.id, Evidence.is_deleted.is_(False)
            )
        ):
            core_evidence += 1
    blocking = ex_by_sev["BLOCKING"]
    ready = (
        core_total > 0
        and core_approved == core_total
        and core_evidence == core_total
        and blocking == 0
    )
    core_readiness = {
        "total": core_total,
        "approved": core_approved,
        "evidence": core_evidence,
        "blocking": blocking,
        "ready": ready,
        "label": "READY" if ready and period.locked else (
            "PROVISIONAL / IN PROGRESS" if not period.locked else "NOT READY"
        ),
    }

    # ---- KPIs with YoY ----
    group = _group_entity(db)
    fy24 = _period_by_label(db, "FY2024-25")
    kpis: list[KpiOut] = []
    for code in KPI_CODES:
        metric = db.scalar(
            select(MetricDefinition).where(
                MetricDefinition.framework_version_id == period.framework_version_id,
                MetricDefinition.metric_code == code,
            )
        )
        if metric is None or group is None:
            continue
        v24, unit, _ = (
            _trace_value(db, group.id, code, fy24.id) if fy24 else (None, None, None)
        )
        v25, _, t25 = _trace_value(db, group.id, code, period.id)
        yoy = None
        if v24 not in (None, 0) and v25 is not None:
            yoy = round((v25 - v24) / v24 * 100, 1)
        kpis.append(KpiOut(
            metric_code=code, label=metric.label, unit=unit,
            fy24=v24, fy25=v25, yoy_pct=yoy,
            fy25_complete=(t25 is not None and not t25.is_stale) if t25 else None,
        ))

    # ---- Scope split (FY24) ----
    scope_split: dict[str, float | None] = {}
    if group is not None and fy24 is not None:
        for code, label in SCOPE_CODES.items():
            v, _, _ = _trace_value(db, group.id, code, fy24.id)
            scope_split[label] = v

    # ---- entity comparison (FY24 group energy contributions) ----
    entity_comparison: list[dict] = []
    if group is not None and fy24 is not None:
        _, _, trace = _trace_value(db, group.id, "C-P6-TOTAL-ENERGY", fy24.id)
        if trace is not None:
            value_ids = trace.contributing_value_ids or []
            values = list(
                db.scalars(select(MetricValue).where(MetricValue.id.in_(value_ids))).all()
            )
            for v in values:
                a = db.get(Assignment, v.assignment_id)
                if a is None or v.normalized_value is None:
                    continue
                entity_comparison.append({
                    "entity": entity_names.get(str(a.entity_id), "?"),
                    "value": float(v.normalized_value),
                    "unit": v.normalized_unit or "",
                })
            entity_comparison.sort(key=lambda x: -x["value"])

    # ---- §5.6 worked example with REAL data: why ratios are recomputed ----
    ratio_demo = None
    intensity_metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == period.framework_version_id,
            MetricDefinition.metric_code == "C-P6-GHG-INTENSITY",
        )
    )
    if intensity_metric is not None and intensity_metric.ratio_numerator_code and fy24 is not None:
        num_code = intensity_metric.ratio_numerator_code
        den_code = intensity_metric.ratio_denominator_code
        plants = [
            e for e in db.scalars(
                select(Entity).where(Entity.is_active.is_(True))
            ).all()
            if e.entity_type.value in ("PLANT", "PROJECT")
        ]
        rows = []
        for plant in plants:
            def approved_norm(code: str, eid: uuid.UUID) -> float | None:
                v = db.execute(
                    select(MetricValue.normalized_value)
                    .join(Assignment, MetricValue.assignment_id == Assignment.id)
                    .where(
                        Assignment.entity_id == eid,
                        Assignment.period_id == fy24.id,
                        Assignment.metric_code == code,
                        MetricValue.status.in_([MetricValueStatus.APPROVED, MetricValueStatus.LOCKED]),
                    )
                    .order_by(MetricValue.version.desc())
                    .limit(1)
                ).scalar()
                return float(v) if v is not None else None
            num = approved_norm(num_code, plant.id)
            den = approved_norm(den_code, plant.id)
            if num is not None and den not in (None, 0):
                rows.append({"entity": plant.name, "num": num, "den": den,
                             "intensity": round(num / den, 4)})
        if len(rows) >= 2:
            correct = sum(r["num"] for r in rows) / sum(r["den"] for r in rows)
            naive = sum(r["intensity"] for r in rows) / len(rows)
            ratio_demo = {
                "metric": "C-P6-GHG-INTENSITY",
                "unit": intensity_metric.canonical_unit,
                "plants": sorted(rows, key=lambda r: -r["intensity"]),
                "correct": round(correct, 4),
                "naive_average": round(naive, 4),
                "num_total": round(sum(r["num"] for r in rows), 3),
                "den_total": round(sum(r["den"] for r in rows), 3),
            }

    if not kpis:
        notes.append("No consolidated KPIs yet — run consolidation from the Consolidation screen.")
    if fy24 is None:
        notes.append("FY2024-25 period missing; YoY comparisons unavailable.")

    return DashboardSummary(
        ratio_demo=ratio_demo,
        period_id=period.id,
        assignments_total=len(assignments),
        assignments_by_status=by_status,
        approved=approved,
        overdue=overdue,
        exceptions_open=ex_by_sev,
        top_exception_rules=[{"rule": r, "count": c} for r, c in top_rules],
        core_readiness=core_readiness,
        kpis=kpis,
        scope_split=scope_split,
        entity_comparison=entity_comparison,
        notes=notes,
    )


# --------------------------------------------------------------------------
# ESG Reporting Control Tower (feature 8) — governance indicators computed
# from real system data only. The existing dashboard summary is untouched.
# --------------------------------------------------------------------------

class ControlTower(BaseModel):
    period_id: uuid.UUID
    period_label: str
    period_locked: bool
    critical_metrics: dict          # total / ready / blocked / pending_review
    high_risk: dict                 # open blocking exceptions, missing evidence,
                                    # stale metrics, overdue, reviewer backlog,
                                    # open assurance requests
    funnel: dict[str, int]          # workflow stage counts
    bottleneck: dict                # current stage blocking the cycle
    deadlines: dict                 # next critical deadline, overdue count
    core_readiness: dict


@router.get("/control-tower", response_model=ControlTower)
def control_tower(
    period_id: uuid.UUID = Query(default=None),
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> ControlTower:
    from datetime import date as date_cls

    from app.models import AssuranceRequest, ConsolidationTrace
    from app.models.enums import AssuranceRequestStatus, ValidationExceptionStatus

    period = db.get(ReportingPeriod, period_id) if period_id else _period_by_label(db, "FY2025-26")
    if period is None:
        period = db.scalar(select(ReportingPeriod).order_by(ReportingPeriod.start_date.desc()))
    if period is None:
        raise HTTPException(status_code=404, detail="No reporting periods exist")
    all_visible = user.role in (UserRole.ADMIN, UserRole.ESG_MANAGER)

    stmt = select(Assignment).where(Assignment.period_id == period.id)
    if not all_visible:
        stmt = stmt.where(Assignment.entity_id.in_(scoped_ids))
    assignments = list(db.scalars(stmt).all())
    assignment_by_id = {a.id: a for a in assignments}

    funnel: dict[str, int] = {}
    for a in assignments:
        funnel[a.status.value] = funnel.get(a.status.value, 0) + 1

    # ---- risk tiers per metric (control policies) ----
    from app.governance.policies import get_policy

    metric_codes = {a.metric_code: db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == a.framework_version_id,
            MetricDefinition.metric_code == a.metric_code,
        )) for a in assignments}
    critical_assignments = []
    high_assignments = []
    for a in assignments:
        policy = get_policy(db, a.framework_version_id, a.metric_code)
        if policy.risk_tier.value == "CRITICAL":
            critical_assignments.append(a)
        elif policy.risk_tier.value == "HIGH":
            high_assignments.append(a)

    def _tier_counts(rows: list[Assignment]) -> dict:
        ready = blocked = pending = 0
        for a in rows:
            if a.status in (AssignmentStatus.APPROVED, AssignmentStatus.LOCKED):
                ready += 1
            elif a.status in (AssignmentStatus.SUBMITTED, AssignmentStatus.UNDER_REVIEW,
                              AssignmentStatus.PENDING_SECOND_APPROVAL):
                pending += 1
            else:
                blocked += 1
        return {"total": len(rows), "ready": ready, "blocked": blocked, "pending_review": pending}

    critical_metrics = _tier_counts(critical_assignments)

    # ---- open high-risk issues ----
    ex_stmt = select(ValidationException).where(
        ValidationException.period_id == period.id,
        ValidationException.status != ValidationExceptionStatus.RESOLVED,
    )
    if not all_visible:
        ex_stmt = ex_stmt.where(ValidationException.entity_id.in_(scoped_ids))
    open_exceptions = list(db.scalars(ex_stmt).all())
    open_blocking = sum(1 for e in open_exceptions if e.severity.value == "BLOCKING")

    missing_evidence = 0
    from app.models import Evidence

    for a in critical_assignments + high_assignments:
        if a.status in (AssignmentStatus.APPROVED, AssignmentStatus.LOCKED):
            continue
        policy = get_policy(db, a.framework_version_id, a.metric_code)
        if not policy.required_evidence:
            continue
        value = db.scalar(
            select(MetricValue).where(MetricValue.assignment_id == a.id)
            .order_by(MetricValue.version.desc()).limit(1)
        )
        if value is None:
            missing_evidence += len(policy.required_evidence)
            continue
        cats = {
            c for c in db.scalars(
                select(Evidence.category).where(
                    Evidence.metric_value_id == value.id, Evidence.is_deleted.is_(False)
                )
            ).all()
            if c
        }
        missing_evidence += sum(1 for c in policy.required_evidence if c not in cats)

    stale_traces = len(list(db.scalars(
        select(ConsolidationTrace.id).where(
            ConsolidationTrace.period_id == period.id,
            ConsolidationTrace.is_stale.is_(True),
        )
    ).all()))

    today = date_cls.today()
    overdue_assignments = [
        a for a in assignments
        if a.due_date and a.due_date < today
        and a.status not in (AssignmentStatus.APPROVED, AssignmentStatus.LOCKED)
    ]

    reviewer_backlog = sum(
        funnel.get(s, 0) for s in ("SUBMITTED", "UNDER_REVIEW", "PENDING_SECOND_APPROVAL")
    )

    ar_stmt = select(AssuranceRequest).where(
        AssuranceRequest.period_id == period.id,
        AssuranceRequest.status.notin_(
            (AssuranceRequestStatus.RESOLVED, AssuranceRequestStatus.REJECTED)
        ),
    )
    if not all_visible:
        ar_stmt = ar_stmt.where(AssuranceRequest.entity_id.in_(scoped_ids))
    open_assurance = len(list(db.scalars(ar_stmt).all()))

    high_risk = {
        "open_blocking_exceptions": open_blocking,
        "missing_evidence": missing_evidence,
        "stale_metrics": stale_traces,
        "overdue_owners": len(overdue_assignments),
        "reviewer_backlog": reviewer_backlog,
        "open_assurance_requests": open_assurance,
    }

    # ---- bottleneck detection: the stage with the oldest waiting work ----
    bottleneck = {"stage": None, "count": 0, "detail": None}
    stage_hints = [
        ("NOT_STARTED", "Assignment queue", "Owners have not started data entry"),
        ("IN_PROGRESS", "Data entry", "Owners are still drafting values"),
        ("SUBMITTED", "Reviewer queue", "Reviewers have not picked up submissions"),
        ("UNDER_REVIEW", "Review in progress", "Reviews are ongoing"),
        ("PENDING_SECOND_APPROVAL", "Second-level approval", "Awaiting second approvers"),
    ]
    for stage_key, label, detail in stage_hints:
        count = funnel.get(stage_key, 0)
        if count > bottleneck["count"]:
            bottleneck = {"stage": label, "count": count, "detail": detail}

    next_deadline = None
    upcoming = sorted(
        (a.due_date for a in assignments
         if a.due_date and a.due_date >= today
         and a.status not in (AssignmentStatus.APPROVED, AssignmentStatus.LOCKED)),
    )
    if upcoming:
        next_deadline = str(upcoming[0])

    # BRSR Core readiness (same definition as the summary endpoint)
    core_metrics = list(
        db.scalars(
            select(MetricDefinition).where(
                MetricDefinition.framework_version_id == period.framework_version_id,
                MetricDefinition.brsr_core.is_(True),
            )
        ).all()
    )
    approved_map = {a.metric_code: a for a in assignments
                    if a.status in (AssignmentStatus.APPROVED, AssignmentStatus.LOCKED)}
    core_approved = sum(1 for m in core_metrics if m.metric_code in approved_map)
    core_ready = (
        len(core_metrics) > 0
        and core_approved == len(core_metrics)
        and open_blocking == 0
        and stale_traces == 0
    )
    core_readiness = {
        "total": len(core_metrics),
        "approved": core_approved,
        "ready": core_ready,
        "label": "READY" if core_ready and period.locked else (
            "IN PROGRESS" if core_ready or not period.locked else "NOT READY"
        ),
        "blockers": {
            "missing_approval": len(core_metrics) - core_approved,
            "blocking_exceptions": open_blocking,
            "stale_traces": stale_traces,
        },
    }

    deadlines = {
        "next_critical_deadline": next_deadline,
        "overdue_count": len(overdue_assignments),
        "overdue": [
            {"assignment_id": str(a.id), "metric_code": a.metric_code,
             "entity_id": str(a.entity_id), "due_date": str(a.due_date),
             "status": a.status.value}
            for a in overdue_assignments[:6]
        ],
    }
    for d in deadlines["overdue"]:
        entity = db.get(Entity, uuid.UUID(d["entity_id"]))
        d["entity_name"] = entity.name if entity else "?"

    return ControlTower(
        period_id=period.id, period_label=period.label, period_locked=period.locked,
        critical_metrics=critical_metrics,
        high_risk=high_risk,
        funnel=funnel,
        bottleneck=bottleneck,
        deadlines=deadlines,
        core_readiness=core_readiness,
    )
