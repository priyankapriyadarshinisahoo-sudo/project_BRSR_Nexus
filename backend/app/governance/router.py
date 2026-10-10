"""Risk/control policy API (features 1 & 10). Policy changes are audited;
only ESG Manager / Admin may modify (permission policy.manage)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_scoped_entity_ids, require_roles
from app.db.session import get_db
from app.governance.policies import get_policy, upsert_policy
from app.models import AppUser, MetricControlPolicy, MetricDefinition
from app.models.enums import MetricRiskTier, UserRole

router = APIRouter(prefix="/api/v1/control-policies", tags=["control-policies"])
policy_manager = require_roles(UserRole.ADMIN, UserRole.ESG_MANAGER)


class PolicyOut(BaseModel):
    id: uuid.UUID | None
    framework_version_id: uuid.UUID
    metric_code: str
    risk_tier: str
    risk_basis: str | None
    required_evidence: list
    methodology_required: bool
    requires_independent_review: bool
    requires_second_approval: bool
    sla_days: int | None
    escalation_policy: list
    impact_reverification_required: bool
    brsr_core: bool = False
    metric_label: str | None = None


class PolicyPut(BaseModel):
    risk_tier: MetricRiskTier | None = None
    risk_basis: str | None = None
    required_evidence: list[str] | None = None
    methodology_required: bool | None = None
    requires_independent_review: bool | None = None
    requires_second_approval: bool | None = None
    sla_days: int | None = None
    escalation_policy: list[str] | None = None
    impact_reverification_required: bool | None = None


@router.get("", response_model=list[PolicyOut])
def list_policies(
    framework_version_id: uuid.UUID | None = Query(default=None),
    risk_tier: MetricRiskTier | None = Query(default=None),
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PolicyOut]:
    stmt = select(MetricControlPolicy)
    if framework_version_id:
        stmt = stmt.where(MetricControlPolicy.framework_version_id == framework_version_id)
    if risk_tier:
        stmt = stmt.where(MetricControlPolicy.risk_tier == risk_tier)
    rows = list(db.scalars(stmt).all())
    out: list[PolicyOut] = []
    for p in rows:
        metric = db.scalar(
            select(MetricDefinition).where(
                MetricDefinition.framework_version_id == p.framework_version_id,
                MetricDefinition.metric_code == p.metric_code,
            )
        )
        out.append(PolicyOut(
            id=p.id, framework_version_id=p.framework_version_id,
            metric_code=p.metric_code, risk_tier=p.risk_tier.value,
            risk_basis=p.risk_basis, required_evidence=p.required_evidence,
            methodology_required=p.methodology_required,
            requires_independent_review=p.requires_independent_review,
            requires_second_approval=p.requires_second_approval,
            sla_days=p.sla_days, escalation_policy=p.escalation_policy,
            impact_reverification_required=p.impact_reverification_required,
            brsr_core=bool(metric.brsr_core) if metric else False,
            metric_label=metric.label if metric else None,
        ))
    return out


@router.get("/resolve", response_model=PolicyOut)
def resolve_policy(
    framework_version_id: uuid.UUID,
    metric_code: str,
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PolicyOut:
    """Effective policy including defaults for metrics without a stored row."""
    policy = get_policy(db, framework_version_id, metric_code)
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == framework_version_id,
            MetricDefinition.metric_code == metric_code,
        )
    )
    return PolicyOut(
        id=policy.id if policy.id else None,
        framework_version_id=framework_version_id,
        metric_code=metric_code, risk_tier=policy.risk_tier.value,
        risk_basis=policy.risk_basis, required_evidence=policy.required_evidence,
        methodology_required=policy.methodology_required,
        requires_independent_review=policy.requires_independent_review,
        requires_second_approval=policy.requires_second_approval,
        sla_days=policy.sla_days, escalation_policy=policy.escalation_policy,
        impact_reverification_required=policy.impact_reverification_required,
        brsr_core=bool(metric.brsr_core) if metric else False,
        metric_label=metric.label if metric else None,
    )


@router.put("/{framework_version_id}/{metric_code}", response_model=PolicyOut)
def put_policy(
    framework_version_id: uuid.UUID,
    metric_code: str,
    body: PolicyPut,
    user: AppUser = Depends(policy_manager),
    db: Session = Depends(get_db),
) -> PolicyOut:
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == framework_version_id,
            MetricDefinition.metric_code == metric_code,
        )
    )
    if metric is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Metric not found in this framework version")
    policy = upsert_policy(
        db, framework_version_id, metric_code, user,
        risk_tier=body.risk_tier, risk_basis=body.risk_basis,
        required_evidence=body.required_evidence,
        methodology_required=body.methodology_required,
        requires_independent_review=body.requires_independent_review,
        requires_second_approval=body.requires_second_approval,
        sla_days=body.sla_days, escalation_policy=body.escalation_policy,
        impact_reverification_required=body.impact_reverification_required,
    )
    db.commit()
    db.refresh(policy)
    return PolicyOut(
        id=policy.id, framework_version_id=policy.framework_version_id,
        metric_code=policy.metric_code, risk_tier=policy.risk_tier.value,
        risk_basis=policy.risk_basis, required_evidence=policy.required_evidence,
        methodology_required=policy.methodology_required,
        requires_independent_review=policy.requires_independent_review,
        requires_second_approval=policy.requires_second_approval,
        sla_days=policy.sla_days, escalation_policy=policy.escalation_policy,
        impact_reverification_required=policy.impact_reverification_required,
        brsr_core=metric.brsr_core, metric_label=metric.label,
    )
