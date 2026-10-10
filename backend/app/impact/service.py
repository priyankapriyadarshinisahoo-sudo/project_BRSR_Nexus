"""Change Impact / Ripple Engine (master prompt feature 4).

Extends the existing dependency mechanisms — formula dependents
(`calculation.recompute_dependents`) and consolidation staleness
(`consolidation.mark_stale_upstream`) — into a recorded, notifiable
impact-propagation system:

    SOURCE CHANGE  →  dependency graph  →  affected derived metrics
                   →  affected consolidations  →  affected BRSR Core metrics

Only REAL relationships are used (formula input_metric_codes, ratio
numerator/denominator, entity hierarchy). Unrelated metrics are never
marked. Every affected item gets an ImpactEvent row; affected assignment
owners/reviewers are notified; policies may require re-verification.
"""
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.governance.policies import get_policy
from app.models import (
    AppUser,
    Assignment,
    ConsolidationTrace,
    Entity,
    FormulaDefinition,
    FormulaVersion,
    MetricDefinition,
    ReportingPeriod,
)
from app.models.enums import AssignmentStatus, AuditAction, NotificationType

logger = logging.getLogger(__name__)


class ImpactError(Exception):
    pass


def _metric(db: Session, framework_version_id: uuid.UUID, code: str) -> MetricDefinition | None:
    return db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == framework_version_id,
            MetricDefinition.metric_code == code,
        )
    )


def direct_dependents(db: Session, framework_version_id: uuid.UUID, code: str) -> set[str]:
    """Metric codes that consume `code` via a formula or as a ratio component."""
    definitions = list(
        db.scalars(
            select(FormulaDefinition).where(
                FormulaDefinition.framework_version_id == framework_version_id
            )
        ).all()
    )
    formula_by_id = {d.id: d for d in definitions}
    consuming: set[str] = set()
    versions = (
        db.scalars(
            select(FormulaVersion).where(
                FormulaVersion.formula_definition_id.in_([d.id for d in definitions] or [uuid.uuid4()])
            )
        ).all()
        if definitions
        else []
    )
    for fv in versions:
        if code in (fv.input_metric_codes or []):
            definition = formula_by_id.get(fv.formula_definition_id)
            if definition is None:
                continue
            consuming.update(
                db.scalars(
                    select(MetricDefinition.metric_code).where(
                        MetricDefinition.framework_version_id == framework_version_id,
                        MetricDefinition.calculation_rule_id == definition.id,
                    )
                ).all()
            )
    for metric in db.scalars(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == framework_version_id
        )
    ).all():
        if code in (metric.ratio_numerator_code, metric.ratio_denominator_code):
            consuming.add(metric.metric_code)
    consuming.discard(code)
    return consuming


def downstream_codes(
    db: Session, framework_version_id: uuid.UUID, code: str
) -> dict[str, list[str]]:
    """Transitive closure of `code` over dependency edges, per level.
    {level: [metric codes]} — level 1 is direct dependents."""
    levels: dict[str, list[str]] = {}
    frontier = {code}
    seen = {code}
    level = 0
    while frontier and level < 10:
        level += 1
        nxt: set[str] = set()
        for c in frontier:
            nxt |= direct_dependents(db, framework_version_id, c)
        nxt -= seen
        if not nxt:
            break
        levels[str(level)] = sorted(nxt)
        seen |= nxt
        frontier = nxt
    return levels


def upstream_codes(
    db: Session, framework_version_id: uuid.UUID, code: str
) -> dict[str, list[str]]:
    """Reverse closure: inputs the metric depends on (formula inputs, ratio
    num/den, transitively), plus consolidation children via the hierarchy."""
    levels: dict[str, list[str]] = {}
    seen = {code}
    frontier = {code}
    level = 0
    while frontier and level < 10:
        level += 1
        nxt: set[str] = set()
        for c in frontier | {code}:
            metric = _metric(db, framework_version_id, c)
            if metric is None:
                continue
            if metric.calculation_rule_id is not None:
                fv = db.scalar(
                    select(FormulaVersion)
                    .where(FormulaVersion.formula_definition_id == metric.calculation_rule_id)
                    .order_by(FormulaVersion.version.desc())
                    .limit(1)
                )
                if fv is not None:
                    nxt.update(fv.input_metric_codes or [])
            if metric.ratio_numerator_code:
                nxt.add(metric.ratio_numerator_code)
            if metric.ratio_denominator_code:
                nxt.add(metric.ratio_denominator_code)
        nxt -= seen
        if not nxt:
            break
        levels[str(level)] = sorted(nxt)
        seen |= nxt
        frontier = nxt
    return levels


def _ancestors(db: Session, entity_id: uuid.UUID) -> list[Entity]:
    ancestors: list[Entity] = []
    current = db.get(Entity, entity_id)
    seen: set[uuid.UUID] = set()
    while current is not None and current.parent_id and current.parent_id not in seen:
        seen.add(current.parent_id)
        parent = db.get(Entity, current.parent_id)
        if parent is None:
            break
        ancestors.append(parent)
        current = parent
    return ancestors


def propagate(
    db: Session,
    assignment: Assignment,
    *,
    source_version: int,
    old_value: str | None,
    new_value: str | None,
    actor: AppUser | None = None,
    request_id: str | None = None,
) -> dict:
    """Record the downstream impact of a submitted (changed) source value.

    Marks consolidation traces stale (real hierarchy paths), records
    ImpactEvents for every genuinely affected item, notifies responsible
    users, and raises a remediation exception when the source metric's
    policy requires re-verification. Returns a summary for the UI banner.
    """
    from app.models import ImpactEvent

    db_session = db
    period = db.get(ReportingPeriod, assignment.period_id)
    policy = get_policy(db_session, assignment.framework_version_id, assignment.metric_code)
    metric = _metric(db_session, assignment.framework_version_id, assignment.metric_code)
    if metric is None:
        return {"affected": 0, "derived": 0, "consolidations": 0, "brsr": 0, "levels": {}}

    closure = downstream_codes(db_session, assignment.framework_version_id, assignment.metric_code)
    affected_codes = {c for codes in closure.values() for c in codes}

    # ---- affected derived-metric assignments at the same entity ----
    derived_assignments: list[Assignment] = []
    if affected_codes:
        derived_assignments = list(
            db.scalars(
                select(Assignment).where(
                    Assignment.framework_version_id == assignment.framework_version_id,
                    Assignment.entity_id == assignment.entity_id,
                    Assignment.period_id == assignment.period_id,
                    Assignment.metric_code.in_(affected_codes),
                )
            ).all()
        )

    # ---- consolidation traces up the hierarchy (existing staleness mechanism) ----
    from app.consolidation.service import mark_stale_upstream

    stale_count = mark_stale_upstream(
        db, assignment.entity_id, assignment.metric_code, assignment.period_id
    )

    events: list[ImpactEvent] = []
    brsr_codes = set()

    def add_event(kind: str, *, entity_id, metric_code=None, affected_assignment=None, trace=None):
        events.append(
            ImpactEvent(
                source_assignment_id=assignment.id,
                source_entity_id=assignment.entity_id,
                source_metric_code=assignment.metric_code,
                period_id=assignment.period_id,
                source_version=source_version,
                old_value=old_value,
                new_value=new_value,
                affected_kind=kind,
                affected_entity_id=entity_id,
                affected_metric_code=metric_code,
                affected_assignment_id=affected_assignment.id if affected_assignment else None,
                affected_trace_id=trace.id if trace else None,
                reason=_reason(kind, metric, assignment),
                created_by=actor.id if actor else None,
            )
        )

    for dep in derived_assignments:
        add_event("derived", entity_id=dep.entity_id, metric_code=dep.metric_code,
                  affected_assignment=dep)
        dep_metric = _metric(db_session, dep.framework_version_id, dep.metric_code)
        if dep_metric is not None and dep_metric.brsr_core:
            brsr_codes.add(dep.metric_code)

    if stale_count:
        ancestors = _ancestors(db, assignment.entity_id)
        for ancestor in ancestors:
            trace = db.scalar(
                select(ConsolidationTrace).where(
                    ConsolidationTrace.entity_id == ancestor.id,
                    ConsolidationTrace.metric_code == assignment.metric_code,
                    ConsolidationTrace.period_id == assignment.period_id,
                )
            )
            if trace is not None:
                add_event("consolidation", entity_id=ancestor.id,
                          metric_code=assignment.metric_code, trace=trace)
        # consolidated BRSR Core disclosures: group-level traces of affected codes
        for code in brsr_codes | ({assignment.metric_code} if metric.brsr_core else set()):
            for ancestor in ancestors:
                trace = db.scalar(
                    select(ConsolidationTrace).where(
                        ConsolidationTrace.entity_id == ancestor.id,
                        ConsolidationTrace.metric_code == code,
                        ConsolidationTrace.period_id == assignment.period_id,
                    )
                )
                if trace is not None:
                    add_event("brsr_disclosure", entity_id=ancestor.id, metric_code=code,
                              trace=trace)

    for event in events:
        db.add(event)
    db.flush()

    # ---- notify responsible users (assignment owner of each derived item) ----
    from app.notifications.router import notify

    notified = 0
    summary = {
        "derived": sum(1 for e in events if e.affected_kind == "derived"),
        "consolidations": sum(1 for e in events if e.affected_kind == "consolidation"),
        "brsr": sum(1 for e in events if e.affected_kind == "brsr_disclosure"),
    }
    if events:
        message = (
            f"{assignment.metric_code} changed ({old_value} → {new_value}); "
            f"{summary['derived']} derived metric(s), {summary['consolidations']} consolidation(s) "
            f"and {summary['brsr']} BRSR disclosure(s) require re-verification"
        )
        recipients: set[uuid.UUID] = {assignment.owner_user_id}
        if assignment.reviewer_user_id:
            recipients.add(assignment.reviewer_user_id)
        for dep in derived_assignments:
            recipients.add(dep.owner_user_id)
        for recipient in recipients:
            notify(db, recipient, NotificationType.IMPACT_DETECTED, {
                "message": message,
                "assignment_id": str(assignment.id),
                "metric_code": assignment.metric_code,
                "derived": summary["derived"],
                "consolidations": summary["consolidations"],
                "brsr": summary["brsr"],
            })
            notified += 1
        for event in events:
            event.notified = True

    record(
        db, action=AuditAction.UPDATED, object_type="impact_scan",
        object_id=assignment.id, actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "impact-engine",
        entity_id=assignment.entity_id, metric_code=assignment.metric_code,
        old_value={"value": old_value} if old_value else None,
        new_value={"value": new_value, **summary, "stale_traces": stale_count},
        reason="source change impact scan",
        request_id=request_id,
    )

    if events and policy.impact_reverification_required:
        _raise_reverification_exception(
            db, assignment, summary, actor=actor, request_id=request_id
        )

    return {
        "affected": len(events),
        "stale_traces": stale_count,
        "notified": notified,
        "levels": closure,
        **summary,
    }


def _reason(kind: str, metric, assignment: Assignment) -> str:
    label = metric.label if metric else assignment.metric_code
    if kind == "derived":
        return f"Depends on {assignment.metric_code}, which changed; recalculation required"
    if kind == "consolidation":
        return f"Consolidated figure includes {assignment.metric_code}; recomputation required"
    return f"BRSR Core disclosure depends on {assignment.metric_code} ({label}); re-verification required"


def _raise_reverification_exception(db, assignment, summary, *, actor=None, request_id=None) -> None:
    """Policy-driven re-verification work item for a post-approval source change."""
    from app.models import ValidationRule, ValidationException
    from app.models.enums import ValidationRuleClass, ValidationSeverity, ValidationExceptionStatus

    sentinel = db.scalar(
        select(ValidationRule).where(
            ValidationRule.framework_version_id == assignment.framework_version_id,
            ValidationRule.rule_code == "GOV-REVERIFICATION",
        )
    )
    if sentinel is None:
        sentinel = ValidationRule(
            framework_version_id=assignment.framework_version_id,
            rule_code="GOV-REVERIFICATION",
            rule_class=ValidationRuleClass.LOGICAL_CONSISTENCY,
            target_metric_code=None,
            severity=ValidationSeverity.BLOCKING,
            config={},
            applies_on="review",
            version=1,
            message_template="Source data changed after downstream verification",
        )
        db.add(sentinel)
        db.flush()

    existing = db.scalar(
        select(ValidationException).where(
            ValidationException.assignment_id == assignment.id,
            ValidationException.rule_code == "GOV-REVERIFICATION",
            ValidationException.status != ValidationExceptionStatus.RESOLVED,
        )
    )
    if existing is not None:
        return
    exception = ValidationException(
        rule_id=sentinel.id,
        rule_code="GOV-REVERIFICATION",
        rule_version=1,
        severity=ValidationSeverity.BLOCKING,
        message=(
            f"Source data changed ({assignment.metric_code}); downstream impact: "
            f"{summary['derived']} derived, {summary['consolidations']} consolidations, "
            f"{summary['brsr']} BRSR disclosures. Re-verification required."
        ),
        entity_id=assignment.entity_id,
        metric_code=assignment.metric_code,
        period_id=assignment.period_id,
        assignment_id=assignment.id,
        category="CHANGE_IMPACT",
        action_required="Re-verify downstream impact and obtain re-approval before reporting",
        status=ValidationExceptionStatus.OPEN,
    )
    db.add(exception)
    db.flush()
    record(
        db, action=AuditAction.EXCEPTION_RAISED, object_type="validation_exception",
        object_id=exception.id, actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "impact-engine",
        entity_id=assignment.entity_id, metric_code=assignment.metric_code,
        new_value={"rule": "GOV-REVERIFICATION", "category": "CHANGE_IMPACT"},
        reason="policy requires re-verification after source change",
        request_id=request_id,
    )
