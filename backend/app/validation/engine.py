"""Deterministic, versioned validation engine (spec §11).

Ten rule classes execute at defined stages:
- draft:  type checks
- submit: REQUIRED_FIELD, RANGE_VALIDATION, UNIT_VALIDATION, TYPE_VALIDATION
- review: CROSS_FIELD / CROSS_SECTION reconciliation, YOY_VARIANCE,
          DUPLICATE_DETECTION, EVIDENCE_COMPLETENESS, LOGICAL_CONSISTENCY

Findings become ValidationException rows (OPEN). Re-runs auto-resolve OPEN
exceptions whose condition cleared; EXPLAINED exceptions are kept for the
reviewer to resolve. BLOCKING findings on SUBMIT refuse the submission.
"""
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Assignment,
    Evidence,
    MetricDefinition,
    MetricValue,
    ReportingPeriod,
    ValidationException,
    ValidationRule,
)
from app.models.enums import (
    AssignmentStatus,
    AuditAction,
    MetricValueStatus,
    ValidationExceptionStatus,
    ValidationSeverity,
)


@dataclass
class Finding:
    rule: ValidationRule
    message: str
    observed: Decimal | None = field(default=None)


def _rules_for(db: Session, framework_version_id: uuid.UUID, stage: str) -> list[ValidationRule]:
    return list(
        db.scalars(
            select(ValidationRule).where(
                ValidationRule.framework_version_id == framework_version_id,
                ValidationRule.is_active.is_(True),
                ValidationRule.applies_on == stage,
            )
        ).all()
    )


def _applies_to(rule: ValidationRule, metric: MetricDefinition | None) -> bool:
    if rule.target_metric_code is None:
        return True
    return metric is not None and rule.target_metric_code == metric.metric_code


def _latest_value(db: Session, assignment_id: uuid.UUID) -> MetricValue | None:
    return db.scalar(
        select(MetricValue)
        .where(MetricValue.assignment_id == assignment_id)
        .order_by(MetricValue.version.desc())
        .limit(1)
    )


def _metric(db: Session, assignment: Assignment) -> MetricDefinition | None:
    return db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == assignment.framework_version_id,
            MetricDefinition.metric_code == assignment.metric_code,
        )
    )


def _previous_period_value(
    db: Session, assignment: Assignment
) -> tuple[Decimal, str, str] | None:
    """Latest normalized value for the same entity+metric in an earlier period."""
    current_period = db.get(ReportingPeriod, assignment.period_id)
    rows = db.execute(
        select(MetricValue, Assignment, ReportingPeriod)
        .join(Assignment, MetricValue.assignment_id == Assignment.id)
        .join(ReportingPeriod, Assignment.period_id == ReportingPeriod.id)
        .where(
            Assignment.entity_id == assignment.entity_id,
            Assignment.metric_code == assignment.metric_code,
            ReportingPeriod.start_date < current_period.start_date,
            Assignment.status.in_([AssignmentStatus.APPROVED, AssignmentStatus.LOCKED]),
        )
        .order_by(ReportingPeriod.start_date.desc(), MetricValue.version.desc())
        .limit(1)
    ).first()
    if rows is None:
        return None
    value, _assignment, period = rows
    if value.normalized_value is None:
        return None
    return value.normalized_value, value.normalized_unit or "", period.label


def _cross_value(
    db: Session, assignment: Assignment, cross_metric_code: str
) -> Decimal | None:
    row = db.execute(
        select(MetricValue)
        .join(Assignment, MetricValue.assignment_id == Assignment.id)
        .where(
            Assignment.entity_id == assignment.entity_id,
            Assignment.period_id == assignment.period_id,
            Assignment.metric_code == cross_metric_code,
        )
        .order_by(MetricValue.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None or row.normalized_value is None:
        return None
    return row.normalized_value


# ---------------------------------------------------------------- rule classes


def _rule_required(db, assignment, metric, value, rule) -> list[Finding]:
    scope = rule.config.get("scope")
    if scope == "brsr_core" and (metric is None or not metric.brsr_core):
        return []
    if value is None or (value.raw_value is None and not value.qualitative_value):
        return [Finding(rule, f"{metric.label if metric else assignment.metric_code} is required but has no value")]
    return []


def _rule_range(db, assignment, metric, value, rule) -> list[Finding]:
    if value is None or value.raw_value is None or metric is None:
        return []
    scope = rule.config.get("scope")
    if scope and scope.startswith("unit_family:"):
        if metric.unit_family != scope.split(":", 1)[1]:
            return []
    elif not _applies_to(rule, metric):
        return []
    minimum = rule.config.get("min")
    maximum = rule.config.get("max")
    observed = value.normalized_value if value.normalized_value is not None else value.raw_value
    if minimum is not None and observed < Decimal(str(minimum)):
        return [Finding(rule, f"{rule.message_template}: observed {observed}", observed)]
    if maximum is not None and observed > Decimal(str(maximum)):
        return [Finding(rule, f"{rule.message_template}: observed {observed}", observed)]
    return []


def _rule_unit(db, assignment, metric, value, rule) -> list[Finding]:
    if value is None or metric is None or metric.data_type.value != "numeric":
        return []
    if metric.allowed_units and value.raw_unit is not None and value.raw_unit not in metric.allowed_units:
        return [Finding(rule, f"{rule.message_template}: {value.raw_unit!r}")]
    return []


def _rule_type(db, assignment, metric, value, rule) -> list[Finding]:
    if value is None or metric is None:
        return []
    if metric.data_type.value == "numeric":
        if value.raw_value is None and not value.qualitative_value:
            return []
        if value.raw_value is None:
            return [Finding(rule, f"{rule.message_template}: numeric value expected")]
    return []


def _rule_cross_field(db, assignment, metric, value, rule) -> list[Finding]:
    if not _applies_to(rule, metric):
        return []
    cross_code = rule.config.get("cross_metric_code")
    if not cross_code:
        return []
    tolerance = Decimal(str(rule.config.get("tolerance", 0)))
    cross = _cross_value(db, assignment, cross_code)
    if value is None or value.normalized_value is None or cross is None:
        return []
    if abs(value.normalized_value - cross) > tolerance:
        return [
            Finding(
                rule,
                f"{rule.message_template} ({assignment.metric_code}={value.normalized_value} "
                f"vs {cross_code}={cross})",
                value.normalized_value,
            )
        ]
    return []


def _rule_cross_section(db, assignment, metric, value, rule) -> list[Finding]:
    return _rule_cross_field(db, assignment, metric, value, rule)


def _rule_yoy(db, assignment, metric, value, rule) -> list[Finding]:
    if value is None or value.normalized_value in (None, 0):
        return []
    if not _applies_to(rule, metric):
        return []
    previous = _previous_period_value(db, assignment)
    if previous is None:
        return []
    old_value, _old_unit, old_period = previous
    if old_value == 0:
        return []
    change_pct = (value.normalized_value - old_value) / old_value * 100
    threshold = rule.config.get("threshold_percent")
    if threshold is None and rule.config.get("threshold_percent_from_env"):
        threshold = get_settings().default_yoy_variance_threshold_percent
    threshold = Decimal(str(threshold))
    if abs(change_pct) > threshold:
        return [
            Finding(
                rule,
                f"{metric.label if metric else assignment.metric_code} changed by "
                f"{change_pct:.0f}% year-on-year (vs {old_period}).",
                change_pct.quantize(Decimal("0.01")),
            )
        ]
    return []


def _rule_duplicate(db, assignment, metric, value, rule) -> list[Finding]:
    if value is None or value.version <= 1:
        return []
    prior = db.scalar(
        select(MetricValue)
        .where(
            MetricValue.assignment_id == assignment.id,
            MetricValue.version == value.version - 1,
        )
    )
    if prior is None:
        return []
    if prior.raw_value is not None and value.raw_value is not None \
            and prior.raw_value == value.raw_value and prior.raw_unit == value.raw_unit:
        return [Finding(rule, rule.message_template)]
    if prior.qualitative_value and prior.qualitative_value == value.qualitative_value:
        return [Finding(rule, rule.message_template)]
    return []


def _rule_evidence(db, assignment, metric, value, rule) -> list[Finding]:
    if metric is None or not metric.evidence_required:
        return []
    scope = rule.config.get("scope")
    if scope == "brsr_core" and not metric.brsr_core:
        return []
    if value is None:
        return []
    has_evidence = db.scalar(
        select(Evidence.id).where(
            Evidence.metric_value_id == value.id, Evidence.is_deleted.is_(False)
        )
    )
    if has_evidence is None:
        return [
            Finding(
                rule,
                f"{metric.label} ({assignment.metric_code}) is BRSR Core and requires "
                f"evidence, but none is attached",
            )
        ]
    return []


def _rule_logical(db, assignment, metric, value, rule) -> list[Finding]:
    condition = rule.config.get("condition")
    if condition == "nonzero_discharge_requires_nonzero_withdrawal":
        withdrawal = _cross_value(db, assignment, "C-P6-WATER-WITHDRAWAL")
        discharge = _cross_value(db, assignment, "C-P6-WATER-DISCHARGE")
        if withdrawal is not None and discharge is not None and discharge > 0 and withdrawal == 0:
            return [Finding(rule, rule.message_template)]
    return []


RULE_EVALUATORS = {
    "REQUIRED_FIELD": _rule_required,
    "RANGE_VALIDATION": _rule_range,
    "UNIT_VALIDATION": _rule_unit,
    "TYPE_VALIDATION": _rule_type,
    "CROSS_FIELD_RECONCILIATION": _rule_cross_field,
    "CROSS_SECTION_RECONCILIATION": _rule_cross_section,
    "YOY_VARIANCE": _rule_yoy,
    "DUPLICATE_DETECTION": _rule_duplicate,
    "EVIDENCE_COMPLETENESS": _rule_evidence,
    "LOGICAL_CONSISTENCY": _rule_logical,
}


def evaluate_assignment(
    db: Session, assignment: Assignment, stage: str, value: MetricValue | None = None
) -> list[Finding]:
    """Run every active rule for the stage against the assignment's latest value.

    `value` may be a not-yet-persisted candidate (duck-typed) so submit-stage
    rules judge the payload being submitted rather than the previous version.
    """
    metric = _metric(db, assignment)
    if value is None:
        value = _latest_value(db, assignment.id)
    findings: list[Finding] = []
    for rule in _rules_for(db, assignment.framework_version_id, stage):
        evaluator = RULE_EVALUATORS.get(rule.rule_class.value)
        if evaluator is None:
            # statistical rules run in the cross-site sweep, not per assignment
            continue
        try:
            findings.extend(evaluator(db, assignment, metric, value, rule))
        except Exception:
            # a broken rule must never crash the pipeline; surface it loudly
            findings.append(
                Finding(rule, f"Validation rule {rule.rule_code} failed to execute; treat as unverified")
            )
    return findings


def persist_findings(
    db: Session,
    assignment: Assignment,
    findings: list[Finding],
    actor_id: uuid.UUID | None = None,
    actor_label: str = "system",
    request_id: str | None = None,
) -> dict:
    """Upsert findings as exceptions; auto-resolve cleared OPEN exceptions."""
    value = _latest_value(db, assignment.id)
    existing = list(
        db.scalars(
            select(ValidationException).where(
                ValidationException.assignment_id == assignment.id,
                ValidationException.status != ValidationExceptionStatus.RESOLVED,
            )
        ).all()
    )
    raised = 0
    matched_rule_ids: set[uuid.UUID] = set()
    for finding in findings:
        matched_rule_ids.add(finding.rule.id)
        duplicate = next(
            (
                e
                for e in existing
                if e.rule_id == finding.rule.id and e.metric_value_id == (value.id if value else None)
            ),
            None,
        )
        if duplicate is not None:
            continue
        exception = ValidationException(
            rule_id=finding.rule.id,
            rule_code=finding.rule.rule_code,
            rule_version=finding.rule.version,
            severity=finding.rule.severity,
            message=finding.message,
            entity_id=assignment.entity_id,
            metric_code=assignment.metric_code,
            period_id=assignment.period_id,
            assignment_id=assignment.id,
            metric_value_id=value.id if value else None,
            observed_value=finding.observed,
            status=ValidationExceptionStatus.OPEN,
        )
        db.add(exception)
        db.flush()
        from app.audit.service import record

        record(
            db, action=AuditAction.EXCEPTION_RAISED, object_type="validation_exception",
            object_id=exception.id, actor_id=actor_id, actor_label=actor_label,
            entity_id=assignment.entity_id, metric_code=assignment.metric_code,
            new_value={"rule": finding.rule.rule_code, "severity": finding.rule.severity.value,
                       "message": finding.message},
            request_id=request_id,
        )
        raised += 1

    auto_resolved = 0
    for e in existing:
        if e.rule_id in matched_rule_ids or e.status == ValidationExceptionStatus.EXPLAINED:
            continue
        # governance sentinel: re-verification exceptions are only resolvable
        # through the remediation workflow, never by a re-run that simply
        # does not re-evaluate the impact policy
        if e.rule_code == "GOV-REVERIFICATION":
            continue
        e.status = ValidationExceptionStatus.RESOLVED
        e.resolved_by = actor_id
        from datetime import UTC, datetime

        e.resolved_at = datetime.now(UTC)
        auto_resolved += 1
    db.flush()
    return {"raised": raised, "auto_resolved": auto_resolved}


def run_for_assignments(
    db: Session, assignments: list[Assignment], stage: str, actor=None, request_id: str | None = None
) -> dict:
    totals = {"assignments": 0, "raised": 0, "auto_resolved": 0, "blocking": 0}
    for assignment in assignments:
        findings = evaluate_assignment(db, assignment, stage)
        result = persist_findings(
            db, assignment, findings,
            actor_id=actor.id if actor else None,
            actor_label=actor.email if actor else "system",
            request_id=request_id,
        )
        totals["assignments"] += 1
        totals["raised"] += result["raised"]
        totals["auto_resolved"] += result["auto_resolved"]
        totals["blocking"] += sum(1 for f in findings if f.rule.severity == ValidationSeverity.BLOCKING)
    return totals
