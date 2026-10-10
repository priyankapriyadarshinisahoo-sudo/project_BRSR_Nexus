"""Risk/control policy resolution and the blocking-reason engine.

A metric's control path is decided here, server-side, from the
`metric_control_policy` table. Absent rows resolve to a STANDARD default
policy; BRSR Core metrics without an explicit row get a HIGH default (the
seed script pins CRITICAL for the emissions KPIs explicitly).

This module must never import business modules — it is a shared dependency
like app.audit.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    Assignment,
    Evidence,
    MetricControlPolicy,
    MetricDefinition,
    MetricValue,
    ValidationException,
)
from app.models.enums import (
    AssignmentStatus,
    MetricRiskTier,
    ValidationExceptionStatus,
    ValidationSeverity,
)

DEFAULT_POLICY = {
    "risk_tier": MetricRiskTier.STANDARD,
    "risk_basis": None,
    "required_evidence": [],
    "methodology_required": False,
    "requires_independent_review": True,
    "requires_second_approval": False,
    "sla_days": None,
    "escalation_policy": ["OWNER"],
    "impact_reverification_required": True,
}

HIGH_DEFAULT_EVIDENCE = ["source_data"]


def get_policy(db: Session, framework_version_id: uuid.UUID, metric_code: str) -> MetricControlPolicy:
    """Return the stored policy row, or an unsaved default (not persisted)."""
    policy = db.scalar(
        select(MetricControlPolicy).where(
            MetricControlPolicy.framework_version_id == framework_version_id,
            MetricControlPolicy.metric_code == metric_code,
        )
    )
    if policy is not None:
        return policy
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == framework_version_id,
            MetricDefinition.metric_code == metric_code,
        )
    )
    defaults = dict(DEFAULT_POLICY)
    if metric is not None and metric.brsr_core:
        defaults["risk_tier"] = MetricRiskTier.HIGH
        defaults["required_evidence"] = list(HIGH_DEFAULT_EVIDENCE)
        defaults["escalation_policy"] = ["OWNER", "ESG_MANAGER"]
    return MetricControlPolicy(
        framework_version_id=framework_version_id,
        metric_code=metric_code,
        **defaults,
    )


def upsert_policy(
    db: Session,
    framework_version_id: uuid.UUID,
    metric_code: str,
    actor: AppUser,
    **fields,
) -> MetricControlPolicy:
    from app.audit.service import record
    from app.models.enums import AuditAction

    policy = db.scalar(
        select(MetricControlPolicy).where(
            MetricControlPolicy.framework_version_id == framework_version_id,
            MetricControlPolicy.metric_code == metric_code,
        )
    )
    if policy is None:
        payload = dict(DEFAULT_POLICY)
        payload.update({k: v for k, v in fields.items() if v is not None})
        policy = MetricControlPolicy(
            framework_version_id=framework_version_id,
            metric_code=metric_code,
            updated_by=actor.id,
            **payload,
        )
        db.add(policy)
        db.flush()
        record(
            db, action=AuditAction.CREATED, object_type="metric_control_policy",
            object_id=policy.id, actor_id=actor.id, actor_label=actor.email,
            metric_code=metric_code, new_value={"risk_tier": policy.risk_tier.value},
        )
        return policy
    changed = {}
    for key, value in fields.items():
        if value is None:
            continue
        current = getattr(policy, key)
        current = current.value if hasattr(current, "value") else current
        if current != value:
            changed[key] = {"old": current, "new": value}
            setattr(policy, key, value)
    if changed:
        policy.updated_by = actor.id
        db.flush()
        record(
            db, action=AuditAction.UPDATED, object_type="metric_control_policy",
            object_id=policy.id, actor_id=actor.id, actor_label=actor.email,
            metric_code=metric_code, new_value=changed,
        )
    return policy


def evidence_status(db: Session, value: MetricValue | None, policy: MetricControlPolicy) -> dict:
    """Which required evidence categories are satisfied on the value."""
    if value is None:
        return {"required": list(policy.required_evidence), "present": [], "missing": list(policy.required_evidence)}
    rows = list(
        db.scalars(
            select(Evidence.category).where(
                Evidence.metric_value_id == value.id, Evidence.is_deleted.is_(False)
            )
        ).all()
    )
    present = {c for c in rows if c}
    required = list(policy.required_evidence)
    # legacy uploads without a category count towards a single-item requirement
    if None in rows and len(required) == 1:
        present.add(required[0])
    missing = [c for c in required if c not in present]
    return {"required": required, "present": sorted(present), "missing": missing}


def blocking_reasons(
    db: Session,
    assignment: Assignment,
    value: MetricValue | None,
    stage: str,
) -> list[dict]:
    """Why this work item cannot advance from `stage`, and what must happen
    next. Pure read; used by the Control Passport and the review endpoints."""
    policy = get_policy(db, assignment.framework_version_id, assignment.metric_code)
    reasons: list[dict] = []

    unresolved_blocking = list(
        db.scalars(
            select(ValidationException).where(
                ValidationException.assignment_id == assignment.id,
                ValidationException.severity == ValidationSeverity.BLOCKING,
                ValidationException.status != ValidationExceptionStatus.RESOLVED,
            )
        ).all()
    )
    for exc in unresolved_blocking:
        reasons.append({
            "code": "BLOCKING_EXCEPTION",
            "detail": exc.message,
            "next_action": "Owner must resolve or explain the exception; reviewer must accept it",
        })

    if stage in ("submit", "approve"):
        ev = evidence_status(db, value, policy)
        if ev["missing"]:
            reasons.append({
                "code": "MISSING_EVIDENCE",
                "detail": f"Missing required evidence: {', '.join(ev['missing'])}",
                "next_action": "Data owner must upload the missing evidence categories",
            })
        if policy.methodology_required and (
            value is None or not (value.methodology_notes or value.formula_id)
        ):
            reasons.append({
                "code": "MISSING_METHODOLOGY",
                "detail": "Policy requires a documented methodology for this metric",
                "next_action": "Data owner must record the methodology before submission",
            })

    if stage == "approve" and policy.requires_second_approval:
        if assignment.reviewed_by_user_id is None:
            reasons.append({
                "code": "INDEPENDENT_REVIEW_PENDING",
                "detail": "Independent review has not been completed",
                "next_action": "Reviewer must complete the control check",
            })

    return reasons
