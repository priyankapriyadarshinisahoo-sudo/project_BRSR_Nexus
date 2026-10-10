"""Deterministic seed: entity hierarchy, users with scopes, framework versions,
reporting periods, metrics, assignments and demo data.

Run:  python -m app.seed

Seeding is idempotent: existing rows are matched by natural keys and skipped.
Demo anomalies are added in later-phase sections of this module as those
subsystems come online.
"""
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.db.session import SessionLocal
from app.framework import seeddata
from app.framework.service import FrameworkMetadataError, create_metric
from app.models import (
    AppUser,
    Entity,
    FormulaDefinition,
    FormulaVersion,
    FrameworkVersion,
    MetricDefinition,
    MetricLineage,
    ReportingPeriod,
    UserEntityScope,
    ValidationRule,
)
from app.models.enums import EntityType, MetricDataType, UserRole

logger = logging.getLogger(__name__)

# Demo credentials — development/demo only, change before any real deployment.
DEMO_PASSWORD = "Demo@12345"

USERS: list[dict] = [
    {"email": "admin@example.local", "full_name": "System Admin", "role": UserRole.ADMIN,
     "scope_names": ["MEIL Group"]},
    {"email": "manager@example.local", "full_name": "ESG Manager", "role": UserRole.ESG_MANAGER,
     "scope_names": ["MEIL Group"]},
    {"email": "reviewer@example.local", "full_name": "Lead Reviewer", "role": UserRole.REVIEWER,
     "scope_names": ["Subsidiary A", "Subsidiary B"]},
    {"email": "management@example.local", "full_name": "Management User", "role": UserRole.MANAGEMENT,
     "scope_names": ["MEIL Group"]},
    {"email": "assessor@example.local", "full_name": "External Assessor", "role": UserRole.ASSESSOR,
     "scope_names": ["MEIL Group"]},
]

ENTITIES: list[dict] = [
    # name, type, parent
    {"name": "MEIL Group", "type": EntityType.GROUP, "parent": None},
    {"name": "Subsidiary A", "type": EntityType.SUBSIDIARY, "parent": "MEIL Group"},
    {"name": "Subsidiary B", "type": EntityType.SUBSIDIARY, "parent": "MEIL Group"},
    {"name": "Business Unit A1", "type": EntityType.BUSINESS_UNIT, "parent": "Subsidiary A"},
    {"name": "Business Unit A2", "type": EntityType.BUSINESS_UNIT, "parent": "Subsidiary A"},
    {"name": "Business Unit B1", "type": EntityType.BUSINESS_UNIT, "parent": "Subsidiary B"},
    {"name": "Business Unit B2", "type": EntityType.BUSINESS_UNIT, "parent": "Subsidiary B"},
    {"name": "Plant Alpha", "type": EntityType.PLANT, "parent": "Business Unit A1"},
    {"name": "Plant Beta", "type": EntityType.PLANT, "parent": "Business Unit A1"},
    {"name": "Plant Gamma", "type": EntityType.PLANT, "parent": "Business Unit A2"},
    {"name": "Plant Delta", "type": EntityType.PLANT, "parent": "Business Unit A2"},
    {"name": "Project C", "type": EntityType.PROJECT, "parent": "Business Unit B1"},
    {"name": "Project D", "type": EntityType.PROJECT, "parent": "Business Unit B1"},
    {"name": "Plant Epsilon", "type": EntityType.PLANT, "parent": "Business Unit B2"},
    {"name": "Plant Zeta", "type": EntityType.PLANT, "parent": "Business Unit B2"},
]

DATA_OWNERS: list[dict] = [
    {"email": "owner-alpha@example.local", "full_name": "Owner — Plant Alpha", "plant": "Plant Alpha"},
    {"email": "owner-beta@example.local", "full_name": "Owner — Plant Beta", "plant": "Plant Beta"},
    {"email": "owner-gamma@example.local", "full_name": "Owner — Plant Gamma", "plant": "Plant Gamma"},
    {"email": "owner-delta@example.local", "full_name": "Owner — Plant Delta", "plant": "Plant Delta"},
    {"email": "owner-projc@example.local", "full_name": "Owner — Project C", "plant": "Project C"},
    {"email": "owner-eps@example.local", "full_name": "Owner — Plant Epsilon", "plant": "Plant Epsilon"},
]


def _get_or_create_entity(db: Session, name: str, entity_type: EntityType, parent: Entity | None) -> Entity:
    entity = db.scalar(select(Entity).where(Entity.name == name))
    if entity is None:
        entity = Entity(
            name=name, entity_type=entity_type,
            parent_id=parent.id if parent else None, effective_from="2020-04-01",
        )
        db.add(entity)
        db.flush()
        logger.info("seeded entity %s", name)
    return entity


CURRENT_FV = "BRSR-FY2025-26"
PREVIOUS_FV = "BRSR-FY2024-25"


def _seed_framework(db: Session) -> tuple[FrameworkVersion, FrameworkVersion]:
    """Two framework versions: previous (FY2024-25, subset + renames) and
    current (FY2025-26, full catalogue). Plus formulas, validation rules and
    cross-version lineage mappings."""
    current = db.scalar(select(FrameworkVersion).where(FrameworkVersion.version_code == CURRENT_FV))
    if current is None:
        current = FrameworkVersion(
            version_code=CURRENT_FV,
            name="BRSR Framework FY2025-26",
            description="Representative BRSR catalogue for FY2025-26 (synthetic; not a legal reproduction)",
            effective_from=date(2025, 4, 1),
            effective_to=date(2026, 3, 31),
            is_active=True,
        )
        db.add(current)
        db.flush()
    previous = db.scalar(select(FrameworkVersion).where(FrameworkVersion.version_code == PREVIOUS_FV))
    if previous is None:
        previous = FrameworkVersion(
            version_code=PREVIOUS_FV,
            name="BRSR Framework FY2024-25",
            description="Previous-year framework version (subset with renames for lineage demo)",
            effective_from=date(2024, 4, 1),
            effective_to=date(2025, 3, 31),
            is_active=False,
        )
        db.add(previous)
        db.flush()

    # ---- formulas (must exist before metrics reference them) ----
    for f in seeddata.FORMULAS:
        from sqlalchemy import exists as sa_exists

        for fv in (current, previous):
            definition = db.scalar(
                select(FormulaDefinition).where(
                    FormulaDefinition.framework_version_id == fv.id,
                    FormulaDefinition.code == f["code"],
                )
            )
            if definition is None:
                definition = FormulaDefinition(
                    framework_version_id=fv.id, code=f["code"], name=f["name"],
                    description=f["description"],
                )
                db.add(definition)
                db.flush()
            has_version = db.scalar(
                select(sa_exists().where(
                    FormulaVersion.formula_definition_id == definition.id,
                    FormulaVersion.version == 1,
                ))
            )
            if not has_version:
                db.add(FormulaVersion(
                    formula_definition_id=definition.id, version=1,
                    expression=f["expression"], input_metric_codes=f["inputs"],
                    constants=f["constants"],
                ))
    db.flush()

    def _metric_payload(m: dict) -> dict:
        payload = {
            "metric_code": m["code"],
            "section": m["section"],
            "principle": m.get("principle"),
            "label": m["label"],
            "data_type": MetricDataType(m["data_type"]),
            "unit_family": m.get("unit_family"),
            "canonical_unit": m.get("canonical_unit"),
            "allowed_units": m.get("allowed_units") or (
                [m["canonical_unit"]] if m.get("canonical_unit") else None
            ),
            "scope": m.get("scope"),
            "required": m.get("required", True),
            "aggregation_semantics": m["agg"],
            "ratio_numerator_code": m.get("num"),
            "ratio_denominator_code": m.get("den"),
            "evidence_required": m.get("evidence_required", False),
            "brsr_core": m.get("brsr_core", False),
            "display_order": m.get("order"),
        }
        return payload

    def _formula_id(code: str) -> int | None:
        if code is None:
            return None
        definition = db.scalar(
            select(FormulaDefinition).where(
                FormulaDefinition.framework_version_id == current.id,
                FormulaDefinition.code == code,
            )
        )
        return definition.id if definition else None

    existing_current = set(
        db.scalars(
            select(MetricDefinition.metric_code).where(
                MetricDefinition.framework_version_id == current.id
            )
        ).all()
    )

    # pass 1: non-ratio metrics (ratio metrics reference these codes)
    for m in seeddata.METRICS:
        if m.get("agg") == "RATIO_RECALCULATION" or m["code"] in existing_current:
            continue
        payload = _metric_payload(m)
        payload["calculation_rule_id"] = _formula_id(m.get("formula"))
        try:
            create_metric(db, current.id, payload)
        except FrameworkMetadataError as exc:
            logger.error("seed metric %s rejected: %s", m["code"], exc)
            raise
    db.flush()

    # pass 2: ratio metrics (numerator/denominator codes now exist)
    for m in seeddata.METRICS:
        if m.get("agg") != "RATIO_RECALCULATION" or m["code"] in existing_current:
            continue
        payload = _metric_payload(m)
        try:
            create_metric(db, current.id, payload)
        except FrameworkMetadataError as exc:
            logger.error("seed metric %s rejected: %s", m["code"], exc)
            raise
    db.flush()

    # previous version: subset of metrics (with one renamed water metric)
    existing_previous = set(
        db.scalars(
            select(MetricDefinition.metric_code).where(
                MetricDefinition.framework_version_id == previous.id
            )
        ).all()
    )
    by_code = {m["code"]: m for m in seeddata.METRICS}
    previous_specs: list[dict] = []
    for code in seeddata.PREVIOUS_VERSION_CODES:
        m = dict(by_code[code])
        previous_specs.append(m)
    for old_code, (new_code, _mapping) in seeddata.RENAMES.items():
        m = dict(by_code[new_code])
        m["code"] = old_code
        previous_specs.append(m)

    for m in previous_specs:
        if m["code"] in existing_previous or m.get("agg") == "RATIO_RECALCULATION":
            continue
        payload = _metric_payload(m)
        try:
            create_metric(db, previous.id, payload)
        except FrameworkMetadataError as exc:
            logger.error("seed previous metric %s rejected: %s", m["code"], exc)
            raise
    db.flush()
    for m in previous_specs:
        if m.get("agg") != "RATIO_RECALCULATION" or m["code"] in existing_previous:
            continue
        payload = _metric_payload(m)
        try:
            create_metric(db, previous.id, payload)
        except FrameworkMetadataError as exc:
            logger.error("seed previous metric %s rejected: %s", m["code"], exc)
            raise
    db.flush()

    # ---- cross-version lineage mappings ----
    for code in seeddata.PREVIOUS_VERSION_CODES:
        mapping_type = "REDEFINED" if code == "C-P6-WATER-CONSUMPTION" else "DIRECT"
        exists = db.scalar(
            select(MetricLineage).where(
                MetricLineage.from_framework_version_id == previous.id,
                MetricLineage.from_metric_code == code,
                MetricLineage.to_framework_version_id == current.id,
                MetricLineage.to_metric_code == code,
            )
        )
        if exists is None:
            db.add(MetricLineage(
                from_framework_version_id=previous.id,
                from_metric_code=code,
                to_framework_version_id=current.id,
                to_metric_code=code,
                mapping_type=mapping_type,
                notes=None if mapping_type == "DIRECT"
                else "Definition scope adjusted between versions; compare with care",
            ))
    for old_code, (new_code, mapping_type) in seeddata.RENAMES.items():
        exists = db.scalar(
            select(MetricLineage).where(
                MetricLineage.from_framework_version_id == previous.id,
                MetricLineage.from_metric_code == old_code,
                MetricLineage.to_framework_version_id == current.id,
                MetricLineage.to_metric_code == new_code,
            )
        )
        if exists is None:
            db.add(MetricLineage(
                from_framework_version_id=previous.id,
                from_metric_code=old_code,
                to_framework_version_id=current.id,
                to_metric_code=new_code,
                mapping_type=mapping_type,
                notes=f"Renamed from {old_code} in the previous framework version",
            ))
    db.flush()

    # ---- validation rules (deterministic rules for the current version;
    #      statistical peer rules apply to every version — they are
    #      version-agnostic) ----
    from app.models.enums import ValidationRuleClass, ValidationSeverity

    for r in seeddata.VALIDATION_RULES:
        target_versions = (
            (current, previous) if r["class"] == "ANOMALY_DETECTION" else (current,)
        )
        for fv in target_versions:
            exists = db.scalar(
                select(ValidationRule).where(
                    ValidationRule.framework_version_id == fv.id,
                    ValidationRule.rule_code == r["code"],
                    ValidationRule.version == 1,
                )
            )
            if exists is None:
                db.add(ValidationRule(
                    framework_version_id=fv.id,
                    rule_code=r["code"],
                    rule_class=ValidationRuleClass(r["class"]),
                    target_metric_code=r["target"],
                    severity=ValidationSeverity(r["severity"]),
                    config=r["config"],
                    applies_on=r["applies_on"],
                    version=1,
                    message_template=r["message"],
                ))
    db.flush()
    return current, previous


def _seed_periods(db: Session, current: FrameworkVersion, previous: FrameworkVersion) -> None:
    specs = [
        {"label": "FY2024-25", "start": date(2024, 4, 1), "end": date(2025, 3, 31),
         "fv": previous, "locked": True},
        {"label": "FY2025-26", "start": date(2025, 4, 1), "end": date(2026, 3, 31),
         "fv": current, "locked": False},
    ]
    for s in specs:
        period = db.scalar(select(ReportingPeriod).where(ReportingPeriod.label == s["label"]))
        if period is None:
            db.add(ReportingPeriod(
                label=s["label"], start_date=s["start"], end_date=s["end"],
                framework_version_id=s["fv"].id, locked=s["locked"],
            ))
            logger.info("seeded reporting period %s", s["label"])
    db.flush()


def _seed_policies(db: Session, current_fv: FrameworkVersion, manager: AppUser) -> None:
    """Deterministic control policies for the current framework version
    (feature 1/10). Rows only for metrics whose policy differs from the
    STANDARD default; absent rows resolve to defaults at runtime."""
    from app.models import MetricControlPolicy
    from app.models.enums import MetricRiskTier

    specs = [
        {
            "metric_code": "C-P6-SCOPE1-TCO2E",
            "risk_tier": MetricRiskTier.CRITICAL,
            "risk_basis": "BRSR Core GHG disclosure; externally assured sample metric",
            "required_evidence": ["source_data", "methodology", "emission_factor", "supporting_doc"],
            "methodology_required": True,
            "requires_second_approval": True,
            "sla_days": 10,
            "escalation_policy": ["OWNER", "REVIEWER", "ESG_MANAGER"],
        },
        {
            "metric_code": "C-P6-TOTAL-GHG",
            "risk_tier": MetricRiskTier.CRITICAL,
            "risk_basis": "BRSR Core headline GHG figure; aggregates Scope 1 + Scope 2",
            "required_evidence": ["methodology", "supporting_doc"],
            "methodology_required": True,
            "requires_second_approval": True,
            "sla_days": 10,
            "escalation_policy": ["OWNER", "REVIEWER", "ESG_MANAGER"],
        },
        {
            "metric_code": "C-P6-GHG-INTENSITY",
            "risk_tier": MetricRiskTier.CRITICAL,
            "risk_basis": "BRSR Core intensity KPI derived from headline GHG",
            "required_evidence": ["methodology"],
            "methodology_required": True,
            "requires_second_approval": True,
            "sla_days": 10,
            "escalation_policy": ["OWNER", "ESG_MANAGER"],
        },
        {
            "metric_code": "C-P6-TOTAL-ENERGY",
            "risk_tier": MetricRiskTier.HIGH,
            "risk_basis": "BRSR Core energy disclosure feeding intensity ratios",
            "required_evidence": ["source_data"],
            "sla_days": 14,
            "escalation_policy": ["OWNER", "ESG_MANAGER"],
        },
        {
            "metric_code": "C-P6-SCOPE2-TCO2E",
            "risk_tier": MetricRiskTier.HIGH,
            "risk_basis": "BRSR Core Scope 2 disclosure",
            "required_evidence": ["source_data", "methodology"],
            "sla_days": 14,
            "escalation_policy": ["OWNER", "ESG_MANAGER"],
        },
    ]
    for spec in specs:
        exists = db.scalar(
            select(MetricControlPolicy).where(
                MetricControlPolicy.framework_version_id == current_fv.id,
                MetricControlPolicy.metric_code == spec["metric_code"],
            )
        )
        if exists is None:
            db.add(MetricControlPolicy(
                framework_version_id=current_fv.id, updated_by=manager.id, **spec
            ))
    db.flush()
    logger.info("seeded %d metric control policies", len(specs))


def seed(db: Session) -> None:
    entities: dict[str, Entity] = {}
    for spec in ENTITIES:
        parent = entities.get(spec["parent"]) if spec["parent"] else None
        entities[spec["name"]] = _get_or_create_entity(db, spec["name"], spec["type"], parent)

    for spec in USERS:
        user = db.scalar(select(AppUser).where(AppUser.email == spec["email"]))
        if user is None:
            user = AppUser(
                email=spec["email"], full_name=spec["full_name"],
                hashed_password=hash_password(DEMO_PASSWORD), role=spec["role"],
            )
            db.add(user)
            db.flush()
            logger.info("seeded user %s", spec["email"])
        for scope_name in spec["scope_names"]:
            entity = entities[scope_name]
            exists = db.scalar(
                select(UserEntityScope).where(
                    UserEntityScope.user_id == user.id, UserEntityScope.entity_id == entity.id
                )
            )
            if exists is None:
                db.add(UserEntityScope(user_id=user.id, entity_id=entity.id))

    for spec in DATA_OWNERS:
        user = db.scalar(select(AppUser).where(AppUser.email == spec["email"]))
        if user is None:
            user = AppUser(
                email=spec["email"], full_name=spec["full_name"],
                hashed_password=hash_password(DEMO_PASSWORD), role=UserRole.DATA_OWNER,
            )
            db.add(user)
            db.flush()
            logger.info("seeded data owner %s", spec["email"])
        plant = entities[spec["plant"]]
        exists = db.scalar(
            select(UserEntityScope).where(
                UserEntityScope.user_id == user.id, UserEntityScope.entity_id == plant.id
            )
        )
        if exists is None:
            db.add(UserEntityScope(user_id=user.id, entity_id=plant.id))

    db.flush()

    current_fv, previous_fv = _seed_framework(db)
    _seed_periods(db, current_fv, previous_fv)

    from app.seed_demo import seed_collection

    manager = db.scalar(select(AppUser).where(AppUser.email == "manager@example.local"))
    reviewer = db.scalar(select(AppUser).where(AppUser.email == "reviewer@example.local"))

    _seed_policies(db, current_fv, manager)

    from app.seed_demo import seed_collection

    seed_collection(db, entities, manager, reviewer)

    db.commit()
    logger.info("seed complete: %d entities, %d users", len(entities), len(db.scalars(select(AppUser)).all()))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as db:
        seed(db)


if __name__ == "__main__":
    main()
