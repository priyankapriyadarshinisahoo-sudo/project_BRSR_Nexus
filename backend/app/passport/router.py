"""Metric Control Passport (feature 3) + impact/dependency endpoints
(features 4-5). Aggregates EXISTING data sources only — assignment, metric
definition, control policy, values, formula, evidence, exceptions, review
state, lineage, audit and impact events. Nothing is duplicated or invented.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_scoped_entity_ids
from app.collection.router import _load_scoped
from app.db.session import get_db
from app.governance.policies import evidence_status, get_policy
from app.impact import service as impact_service
from app.models import (
    AppUser,
    Assignment,
    AuditEvent,
    ConsolidationTrace,
    Entity,
    Evidence,
    FormulaDefinition,
    FormulaVersion,
    ImpactEvent,
    MetricDefinition,
    MetricValue,
    ReportingPeriod,
    ValidationException,
)
from app.models.enums import (
    AssignmentStatus,
    UserRole,
    ValidationExceptionStatus,
)

router = APIRouter(prefix="/api/v1/assignments", tags=["control-passport"])


class PassportOut(BaseModel):
    assignment_id: uuid.UUID
    metric_code: str
    metric_label: str
    entity_id: uuid.UUID
    entity_name: str
    period_id: uuid.UUID
    period_label: str
    status: str
    risk: dict            # tier, basis, brsr_core
    controls: dict        # required evidence, methodology, reviews, sla
    owner: str
    reviewer: str | None
    second_approver: str | None
    value: dict | None    # latest value snapshot
    formula: dict | None
    evidence: dict        # required/present/missing + count
    validation: dict      # exceptions summary
    review: dict          # reviewed_by, second approval state
    lineage: dict         # trace summary: kind, contributors, staleness
    audit_count: int
    impact: dict          # downstream impact summary
    blocking: list[dict]  # why blocked + next action


class ImpactEventOut(BaseModel):
    id: uuid.UUID
    affected_kind: str
    affected_entity_id: uuid.UUID | None
    affected_metric_code: str | None
    reason: str
    status: str
    created_at: object


class ImpactSummary(BaseModel):
    source_metric_code: str
    levels: dict
    events: list[ImpactEventOut]
    summary: dict


def _name(db: Session, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    user = db.get(AppUser, user_id)
    return user.full_name if user else None


@router.get("/{assignment_id}/control-passport", response_model=PassportOut)
def control_passport(
    assignment_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> PassportOut:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == assignment.framework_version_id,
            MetricDefinition.metric_code == assignment.metric_code,
        )
    )
    entity = db.get(Entity, assignment.entity_id)
    period = db.get(ReportingPeriod, assignment.period_id)
    policy = get_policy(db, assignment.framework_version_id, assignment.metric_code)

    values = list(
        db.scalars(
            select(MetricValue)
            .where(MetricValue.assignment_id == assignment.id)
            .order_by(MetricValue.version.desc())
        ).all()
    )
    latest = values[0] if values else None

    value_out = None
    if latest is not None:
        value_out = {
            "version": latest.version,
            "raw_value": float(latest.raw_value) if latest.raw_value is not None else None,
            "raw_unit": latest.raw_unit,
            "normalized_value": float(latest.normalized_value) if latest.normalized_value is not None else None,
            "normalized_unit": latest.normalized_unit,
            "status": latest.status.value,
            "is_calculated": latest.is_calculated,
            "methodology_version": latest.methodology_version,
            "methodology_notes": latest.methodology_notes,
            "assumptions": latest.assumptions,
            "history": [
                {"version": v.version, "raw_value": float(v.raw_value) if v.raw_value is not None else None,
                 "status": v.status.value, "is_calculated": v.is_calculated}
                for v in values
            ],
        }

    formula = None
    if latest is not None and latest.formula_id is not None:
        definition = db.get(FormulaDefinition, latest.formula_id)
        fv = db.scalar(
            select(FormulaVersion).where(
                FormulaVersion.formula_definition_id == latest.formula_id,
                FormulaVersion.version == (latest.formula_version or 1),
            )
        )
        if definition is not None and fv is not None:
            formula = {
                "code": definition.code, "name": definition.name,
                "version": fv.version, "expression": fv.expression,
                "input_metric_codes": fv.input_metric_codes,
                "constants": fv.constants,
                "resolved_inputs": latest.formula_inputs or {},
            }

    evidence = evidence_status(db, latest, policy)
    evidence_count = db.scalar(
        select(Evidence.id)
        .where(Evidence.metric_value_id == latest.id, Evidence.is_deleted.is_(False))
        .limit(1)
    ) if latest else None
    evidence["count"] = 1 if evidence_count else 0

    exceptions = list(
        db.scalars(
            select(ValidationException).where(
                ValidationException.assignment_id == assignment.id,
            )
        ).all()
    )
    validation = {
        "total": len(exceptions),
        "blocking": sum(1 for e in exceptions if e.severity.value == "BLOCKING"
                        and e.status != ValidationExceptionStatus.RESOLVED),
        "open": sum(1 for e in exceptions if e.status == ValidationExceptionStatus.OPEN),
        "investigating": sum(1 for e in exceptions if e.status == ValidationExceptionStatus.INVESTIGATING),
        "explained": sum(1 for e in exceptions if e.status == ValidationExceptionStatus.EXPLAINED),
    }

    review = {
        "reviewed_by": _name(db, assignment.reviewed_by_user_id),
        "reviewed_at": assignment.reviewed_at,
        "second_approved_by": _name(db, assignment.second_approved_by_user_id),
        "second_approved_at": assignment.second_approved_at,
        "second_approval_required": policy.requires_second_approval,
    }

    trace = db.scalar(
        select(ConsolidationTrace).where(
            ConsolidationTrace.entity_id == assignment.entity_id,
            ConsolidationTrace.metric_code == assignment.metric_code,
            ConsolidationTrace.period_id == assignment.period_id,
        )
    )
    lineage = {
        "kind": "leaf",
        "is_stale": False,
        "stale_reason": None,
        "contributors": 0,
        "upstream": impact_service.upstream_codes(
            db, assignment.framework_version_id, assignment.metric_code
        ),
        "downstream": impact_service.downstream_codes(
            db, assignment.framework_version_id, assignment.metric_code
        ),
    }
    if trace is not None:
        lineage.update({
            "kind": "consolidated" if trace.contributing_value_ids else "leaf",
            "is_stale": trace.is_stale,
            "stale_reason": trace.stale_reason,
            "contributors": len(trace.contributing_value_ids or []),
        })

    audit_count = db.scalar(
        select(AuditEvent.id).where(
            AuditEvent.metric_code == assignment.metric_code,
            AuditEvent.entity_id == str(assignment.entity_id),
        ).order_by(AuditEvent.created_at.desc()).limit(1)
    )
    audit_total = len(list(
        db.scalars(
            select(AuditEvent.id).where(
                AuditEvent.metric_code == assignment.metric_code,
                AuditEvent.entity_id == str(assignment.entity_id),
            )
        ).all()
    )) if audit_count else 0

    impact_events = list(
        db.scalars(
            select(ImpactEvent).where(
                ImpactEvent.source_assignment_id == assignment.id
            ).order_by(ImpactEvent.created_at.desc())
        ).all()
    )
    impact = {
        "events": len(impact_events),
        "derived": sum(1 for e in impact_events if e.affected_kind == "derived"),
        "consolidations": sum(1 for e in impact_events if e.affected_kind == "consolidation"),
        "brsr": sum(1 for e in impact_events if e.affected_kind == "brsr_disclosure"),
    }

    stage = "approve" if assignment.status in (
        AssignmentStatus.UNDER_REVIEW, AssignmentStatus.PENDING_SECOND_APPROVAL
    ) else "submit"
    from app.governance.policies import blocking_reasons

    blocking = blocking_reasons(db, assignment, latest, stage)

    return PassportOut(
        assignment_id=assignment.id,
        metric_code=assignment.metric_code,
        metric_label=metric.label if metric else assignment.metric_code,
        entity_id=assignment.entity_id,
        entity_name=entity.name if entity else "?",
        period_id=assignment.period_id,
        period_label=period.label if period else "?",
        status=assignment.status.value,
        risk={
            "tier": policy.risk_tier.value,
            "basis": policy.risk_basis,
            "brsr_core": bool(metric.brsr_core) if metric else False,
        },
        controls={
            "required_evidence": policy.required_evidence,
            "methodology_required": policy.methodology_required,
            "requires_independent_review": policy.requires_independent_review,
            "requires_second_approval": policy.requires_second_approval,
            "sla_days": policy.sla_days,
            "escalation_policy": policy.escalation_policy,
        },
        owner=_name(db, assignment.owner_user_id) or "?",
        reviewer=_name(db, assignment.reviewer_user_id),
        second_approver=_name(db, assignment.second_approver_user_id),
        value=value_out,
        formula=formula,
        evidence=evidence,
        validation=validation,
        review=review,
        lineage=lineage,
        audit_count=audit_total,
        impact=impact,
        blocking=blocking,
    )


@router.get("/{assignment_id}/impact", response_model=ImpactSummary)
def assignment_impact(
    assignment_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> ImpactSummary:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    events = list(
        db.scalars(
            select(ImpactEvent).where(
                ImpactEvent.source_assignment_id == assignment.id
            ).order_by(ImpactEvent.created_at.desc())
        ).all()
    )
    levels = impact_service.downstream_codes(
        db, assignment.framework_version_id, assignment.metric_code
    )
    return ImpactSummary(
        source_metric_code=assignment.metric_code,
        levels=levels,
        events=[
            ImpactEventOut(
                id=e.id, affected_kind=e.affected_kind,
                affected_entity_id=e.affected_entity_id,
                affected_metric_code=e.affected_metric_code,
                reason=e.reason, status=e.status.value, created_at=e.created_at,
            )
            for e in events
        ],
        summary={
            "derived": sum(1 for e in events if e.affected_kind == "derived"),
            "consolidations": sum(1 for e in events if e.affected_kind == "consolidation"),
            "brsr": sum(1 for e in events if e.affected_kind == "brsr_disclosure"),
        },
    )


@router.get("/{assignment_id}/dependencies/upstream")
def dependencies_upstream(
    assignment_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> dict:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    return {
        "metric_code": assignment.metric_code,
        "levels": impact_service.upstream_codes(
            db, assignment.framework_version_id, assignment.metric_code
        ),
    }


@router.get("/{assignment_id}/dependencies/downstream")
def dependencies_downstream(
    assignment_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> dict:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    return {
        "metric_code": assignment.metric_code,
        "levels": impact_service.downstream_codes(
            db, assignment.framework_version_id, assignment.metric_code
        ),
    }
