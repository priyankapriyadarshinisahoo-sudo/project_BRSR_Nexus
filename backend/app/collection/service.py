"""Collection domain service: assignment creation, value versioning, the
assignment state machine and review transitions.

Guards (spec §9):
- NOT_STARTED → IN_PROGRESS: owner opens/saves
- IN_PROGRESS → SUBMITTED: required structural validation passes
- SUBMITTED → UNDER_REVIEW: reviewer opens
- UNDER_REVIEW → APPROVED: no unresolved blocking exceptions
- UNDER_REVIEW → NEEDS_CORRECTION / REJECTED: review comment required
- NEEDS_CORRECTION / REJECTED → IN_PROGRESS: owner saves
- APPROVED → LOCKED: reporting period locked (system)
Every value edit creates a NEW MetricValue version; history is never mutated.
"""
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.entities.service import get_descendant_ids
from app.models import (
    AppUser,
    Assignment,
    Entity,
    FrameworkVersion,
    MetricDefinition,
    MetricValue,
    ReportingPeriod,
    ValidationException,
)
from app.models.enums import (
    AssignmentStatus,
    AuditAction,
    MetricValueStatus,
    NotificationType,
    UserRole,
    ValidationExceptionStatus,
    ValidationSeverity,
)


class CollectionError(Exception):
    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


# assignment-level transition map
TRANSITIONS: dict[AssignmentStatus, set[AssignmentStatus]] = {
    AssignmentStatus.NOT_STARTED: {AssignmentStatus.IN_PROGRESS},
    AssignmentStatus.IN_PROGRESS: {AssignmentStatus.SUBMITTED},
    AssignmentStatus.SUBMITTED: {AssignmentStatus.UNDER_REVIEW},
    AssignmentStatus.UNDER_REVIEW: {
        AssignmentStatus.PENDING_SECOND_APPROVAL,
        AssignmentStatus.APPROVED,
        AssignmentStatus.NEEDS_CORRECTION,
        AssignmentStatus.REJECTED,
    },
    # risk-adaptive workflow: CRITICAL/BRSR Core metrics need a second-level
    # approval before APPROVED (policy-driven, enforced server-side)
    AssignmentStatus.PENDING_SECOND_APPROVAL: {AssignmentStatus.APPROVED},
    AssignmentStatus.NEEDS_CORRECTION: {AssignmentStatus.IN_PROGRESS},
    AssignmentStatus.REJECTED: {AssignmentStatus.IN_PROGRESS},
    AssignmentStatus.APPROVED: {AssignmentStatus.LOCKED},
    AssignmentStatus.LOCKED: set(),
}

VALUE_STATUS_FOR_ASSIGNMENT = {
    AssignmentStatus.IN_PROGRESS: MetricValueStatus.IN_PROGRESS,
    AssignmentStatus.SUBMITTED: MetricValueStatus.SUBMITTED,
    AssignmentStatus.UNDER_REVIEW: MetricValueStatus.UNDER_REVIEW,
    AssignmentStatus.PENDING_SECOND_APPROVAL: MetricValueStatus.UNDER_REVIEW,
    AssignmentStatus.APPROVED: MetricValueStatus.APPROVED,
    AssignmentStatus.NEEDS_CORRECTION: MetricValueStatus.NEEDS_CORRECTION,
    AssignmentStatus.REJECTED: MetricValueStatus.REJECTED,
    AssignmentStatus.LOCKED: MetricValueStatus.LOCKED,
}


def create_assignment(
    db: Session,
    *,
    metric_code: str,
    entity_id: uuid.UUID,
    period_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    created_by: AppUser,
    due_date=None,
    request_id: str | None = None,
) -> Assignment:
    period = db.get(ReportingPeriod, period_id)
    if period is None:
        raise CollectionError("Reporting period not found", 404)
    if period.locked:
        raise CollectionError("Cannot assign metrics for a locked period")
    entity = db.get(Entity, entity_id)
    if entity is None:
        raise CollectionError("Entity not found", 404)
    owner = db.get(AppUser, owner_user_id)
    if owner is None:
        raise CollectionError("Owner user not found", 404)
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == period.framework_version_id,
            MetricDefinition.metric_code == metric_code,
        )
    )
    if metric is None:
        raise CollectionError(
            f"Metric {metric_code!r} does not exist in the period's framework version"
        )
    duplicate = db.scalar(
        select(Assignment).where(
            Assignment.metric_code == metric_code,
            Assignment.entity_id == entity_id,
            Assignment.period_id == period_id,
        )
    )
    if duplicate:
        raise CollectionError("An assignment for this metric/entity/period already exists")

    assignment = Assignment(
        metric_code=metric_code,
        framework_version_id=period.framework_version_id,
        entity_id=entity_id,
        period_id=period_id,
        owner_user_id=owner_user_id,
        due_date=due_date,
        status=AssignmentStatus.NOT_STARTED,
        created_by=created_by.id,
    )
    db.add(assignment)
    db.flush()
    record(
        db, action=AuditAction.CREATED, object_type="assignment", object_id=assignment.id,
        actor_id=created_by.id, actor_label=created_by.email,
        entity_id=entity_id, metric_code=metric_code,
        new_value={"period": period.label, "owner": owner.email},
        request_id=request_id,
    )
    return assignment


def visible_assignment_ids(db: Session, user: AppUser) -> set[uuid.UUID] | None:
    """None means unrestricted (ADMIN/ESG_MANAGER)."""
    if user.role.name in ("ADMIN", "ESG_MANAGER"):
        return None
    if user.role.name == "DATA_OWNER":
        return set(
            db.scalars(select(Assignment.id).where(Assignment.owner_user_id == user.id)).all()
        )
    # REVIEWER / MANAGEMENT / ASSESSOR: assignments within scoped entity subtrees
    scoped_entities: set[uuid.UUID] = set()
    for scope in user.entity_scopes:
        scoped_entities |= get_descendant_ids(db, scope.entity_id)
    return set(
        db.scalars(select(Assignment.id).where(Assignment.entity_id.in_(scoped_entities))).all()
    )


def latest_value(db: Session, assignment_id: uuid.UUID) -> MetricValue | None:
    return db.scalar(
        select(MetricValue)
        .where(MetricValue.assignment_id == assignment_id)
        .order_by(MetricValue.version.desc())
        .limit(1)
    )


def _metric_for_assignment(db: Session, assignment: Assignment) -> MetricDefinition:
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == assignment.framework_version_id,
            MetricDefinition.metric_code == assignment.metric_code,
        )
    )
    if metric is None:
        raise CollectionError("Metric definition missing", 500)
    return metric


def _structural_validate(metric: MetricDefinition, payload: dict) -> None:
    """Basic structural checks required before SUBMIT. The full deterministic
    validation engine (cross-field, YoY, evidence) runs in the validation phase."""
    if metric.data_type.value == "numeric":
        if payload.get("raw_value") is None:
            raise CollectionError("Numeric value is required", 422)
        try:
            Decimal(str(payload["raw_value"]))
        except InvalidOperation:
            raise CollectionError("Value is not a valid number", 422)
        if metric.allowed_units and payload.get("raw_unit") not in metric.allowed_units:
            raise CollectionError(
                f"Unit {payload.get('raw_unit')!r} is not allowed for this metric "
                f"(allowed: {metric.allowed_units})",
                422,
            )
    else:
        if not payload.get("qualitative_value"):
            raise CollectionError("A response is required for this metric", 422)


def save_value(
    db: Session,
    assignment: Assignment,
    *,
    action: str,
    payload: dict,
    actor: AppUser,
    expected_last_version: int | None = None,
    request_id: str | None = None,
) -> MetricValue:
    if assignment.owner_user_id != actor.id and actor.role.name != "ADMIN":
        raise CollectionError("Only the assignment owner may edit this metric", 403)
    period = db.get(ReportingPeriod, assignment.period_id)
    if period is None:
        raise CollectionError("Reporting period not found", 500)
    if period.locked:
        raise CollectionError("Reporting period is locked; values cannot be modified")

    last = latest_value(db, assignment.id)
    if expected_last_version is not None and last is not None and last.version != expected_last_version:
        raise CollectionError(
            f"Concurrent edit detected: value is at version {last.version}, "
            f"you were editing version {expected_last_version}"
        )

    metric = _metric_for_assignment(db, assignment)

    # unit normalization (spec §10): every numeric value stores its canonical form
    normalized_value = normalized_unit = None
    if payload.get("raw_value") is not None:
        try:
            from app.normalization.units import UnitConversionError, normalize_value

            normalized_value, normalized_unit = normalize_value(
                payload["raw_value"], payload.get("raw_unit"),
                metric.unit_family, metric.canonical_unit,
            )
        except UnitConversionError as exc:
            raise CollectionError(str(exc), 422)

    if action == "SUBMIT":
        _structural_validate(metric, payload)
        # submit-stage gate: BLOCKING rules are evaluated against the incoming
        # payload (not the previously persisted version, which may not exist on
        # a first submission) and refuse the submission with 422. The full
        # deterministic sweep (cross-field, YoY, evidence, ...) runs at review.
        from types import SimpleNamespace

        from app.validation import engine as validation_engine

        candidate = SimpleNamespace(
            raw_value=Decimal(str(payload["raw_value"]))
            if payload.get("raw_value") is not None else None,
            raw_unit=payload.get("raw_unit"),
            normalized_value=normalized_value,
            normalized_unit=normalized_unit,
            qualitative_value=payload.get("qualitative_value"),
            version=(last.version if last else 0) + 1,
        )
        blocking = [
            f
            for f in validation_engine.evaluate_assignment(db, assignment, "submit", value=candidate)
            if f.rule.severity == ValidationSeverity.BLOCKING
        ]
        if blocking:
            raise CollectionError(
                "Submission blocked by validation: " + "; ".join(f.message for f in blocking),
                422,
            )
        new_status = MetricValueStatus.SUBMITTED
        new_assignment_status = AssignmentStatus.SUBMITTED
    elif action == "SAVE_DRAFT":
        new_status = MetricValueStatus.IN_PROGRESS
        new_assignment_status = AssignmentStatus.IN_PROGRESS
    else:
        raise CollectionError("action must be SAVE_DRAFT or SUBMIT", 422)

    current_status = assignment.status
    if current_status == AssignmentStatus.LOCKED:
        raise CollectionError("Assignment is locked")
    if current_status not in (AssignmentStatus.NOT_STARTED, AssignmentStatus.IN_PROGRESS,
                              AssignmentStatus.NEEDS_CORRECTION, AssignmentStatus.REJECTED):
        raise CollectionError(
            f"Cannot edit a value in status {current_status.value}; "
            f"it is already {current_status.value}"
        )

    version = (last.version if last else 0) + 1
    value = MetricValue(
        assignment_id=assignment.id,
        version=version,
        raw_value=Decimal(str(payload["raw_value"])) if payload.get("raw_value") is not None else None,
        raw_unit=payload.get("raw_unit"),
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
        qualitative_value=payload.get("qualitative_value"),
        methodology_version=payload.get("methodology_version"),
        methodology_notes=payload.get("methodology_notes"),
        assumptions=payload.get("assumptions"),
        status=new_status,
        created_by=actor.id,
        submitted_by=actor.id if action == "SUBMIT" else None,
        submitted_at=datetime.now(UTC) if action == "SUBMIT" else None,
    )
    db.add(value)
    db.flush()

    assignment.status = new_assignment_status
    db.flush()

    record(
        db,
        action=AuditAction.SUBMITTED if action == "SUBMIT" else AuditAction.UPDATED,
        object_type="metric_value",
        object_id=value.id,
        actor_id=actor.id,
        actor_label=actor.email,
        entity_id=assignment.entity_id,
        metric_code=assignment.metric_code,
        old_value={"version": last.version, "raw_value": str(last.raw_value) if last else None}
        if last else None,
        new_value={"version": version, "raw_value": str(value.raw_value) if value.raw_value else None,
                   "raw_unit": value.raw_unit, "action": action},
        request_id=request_id,
    )

    # dependent derived metrics recompute when an input changes; failures are
    # logged, never silent, and never block the owner's save
    if value.raw_value is not None:
        import logging

        from app.calculation import service as calc_service

        logger = logging.getLogger(__name__)
        try:
            calc_service.recompute_dependents(
                db, assignment.framework_version_id, assignment.entity_id,
                assignment.period_id, assignment.metric_code, actor=actor,
                request_id=request_id,
            )
        except Exception as exc:
            logger.warning(
                "dependent recompute failed for %s/%s: %s",
                assignment.entity_id, assignment.metric_code, exc,
            )

    # change-impact / ripple engine (feature 4): a submitted NEW value that
    # differs from a prior version propagates through real dependencies only.
    if action == "SUBMIT" and last is not None and last.raw_value is not None \
            and value.raw_value is not None and last.raw_value != value.raw_value:
        import logging

        from app.impact import service as impact_service

        logger = logging.getLogger(__name__)
        try:
            impact_service.propagate(
                db, assignment,
                source_version=value.version,
                old_value=str(last.raw_value),
                new_value=str(value.raw_value),
                actor=actor, request_id=request_id,
            )
        except Exception as exc:
            logger.warning(
                "impact propagation failed for %s/%s: %s",
                assignment.entity_id, assignment.metric_code, exc,
            )
    return value


def review(
    db: Session,
    assignment: Assignment,
    *,
    action: str,
    actor: AppUser,
    comment: str | None = None,
    request_id: str | None = None,
) -> Assignment:
    if actor.role.name not in ("REVIEWER", "ESG_MANAGER", "ADMIN"):
        raise CollectionError("Only reviewers may perform review actions", 403)

    # --- separation of duties (feature 16, server-side enforced) ---
    if actor.id == assignment.owner_user_id:
        raise CollectionError(
            "Separation of duties: the data owner cannot review or approve their own submission",
            403,
        )
    value = latest_value(db, assignment.id)
    if value is not None and value.submitted_by == actor.id:
        raise CollectionError(
            "Separation of duties: the user who submitted this value cannot approve it", 403
        )
    # designated reviewer (manager orchestration): others may not act unless
    # they are ADMIN/ESG_MANAGER
    if (
        assignment.reviewer_user_id is not None
        and actor.id != assignment.reviewer_user_id
        and actor.role.name not in ("ADMIN", "ESG_MANAGER")
    ):
        raise CollectionError(
            "This assignment is designated to a different reviewer", 403
        )

    from app.governance.policies import blocking_reasons, get_policy

    policy = get_policy(db, assignment.framework_version_id, assignment.metric_code)
    current = assignment.status
    if action == "START_REVIEW":
        if current != AssignmentStatus.SUBMITTED:
            raise CollectionError(f"Cannot start review from status {current.value}")
        # review-stage validation sweep: cross-field, cross-section, YoY,
        # duplicate, evidence completeness, logical consistency
        from app.validation import engine as validation_engine

        validation_engine.run_for_assignments(
            db, [assignment], "review", actor=actor, request_id=request_id
        )
        new_status = AssignmentStatus.UNDER_REVIEW
        audit_action = AuditAction.REVIEWED
    elif action == "APPROVE":
        if current not in (AssignmentStatus.UNDER_REVIEW, AssignmentStatus.PENDING_SECOND_APPROVAL):
            raise CollectionError(f"Cannot approve from status {current.value}")
        if current == AssignmentStatus.UNDER_REVIEW:
            unresolved_blocking = db.scalar(
                select(ValidationException).where(
                    ValidationException.assignment_id == assignment.id,
                    ValidationException.severity == ValidationSeverity.BLOCKING,
                    ValidationException.status != ValidationExceptionStatus.RESOLVED,
                )
            )
            if unresolved_blocking is not None:
                raise CollectionError(
                    "Cannot approve: unresolved BLOCKING validation exceptions exist for this assignment"
                )
            reasons = blocking_reasons(db, assignment, value, "approve")
            if reasons:
                raise CollectionError(
                    "Cannot approve — policy controls outstanding: "
                    + "; ".join(f"{r['code']}: {r['detail']}" for r in reasons),
                    422,
                )
            if policy.requires_second_approval:
                new_status = AssignmentStatus.PENDING_SECOND_APPROVAL
                assignment.reviewed_by_user_id = actor.id
                from datetime import UTC, datetime

                assignment.reviewed_at = datetime.now(UTC)
                audit_action = AuditAction.REVIEWED
                assignment.status = new_status
                if value is not None:
                    value.status = VALUE_STATUS_FOR_ASSIGNMENT[new_status]
                db.flush()
                record(
                    db, action=audit_action, object_type="assignment", object_id=assignment.id,
                    actor_id=actor.id, actor_label=actor.email,
                    entity_id=assignment.entity_id, metric_code=assignment.metric_code,
                    old_value={"status": current.value},
                    new_value={"status": new_status.value},
                    reason=comment, request_id=request_id,
                )
                return assignment
            new_status = AssignmentStatus.APPROVED
            audit_action = AuditAction.APPROVED
        else:
            # second-level approval stage
            if action == "APPROVE" and current == AssignmentStatus.PENDING_SECOND_APPROVAL:
                new_status = AssignmentStatus.APPROVED
                audit_action = AuditAction.SECOND_APPROVAL_GRANTED
                assignment.second_approved_by_user_id = actor.id
                from datetime import UTC, datetime

                assignment.second_approved_at = datetime.now(UTC)
    elif action == "SECOND_APPROVAL":
        if current != AssignmentStatus.PENDING_SECOND_APPROVAL:
            raise CollectionError(f"Cannot grant second approval from status {current.value}")
        if actor.role.name not in ("ESG_MANAGER", "ADMIN", "REVIEWER"):
            raise CollectionError("Second-level approval requires an approver role", 403)
        if assignment.reviewed_by_user_id == actor.id:
            raise CollectionError(
                "Separation of duties: the first reviewer cannot also grant the second approval",
                403,
            )
        new_status = AssignmentStatus.APPROVED
        audit_action = AuditAction.SECOND_APPROVAL_GRANTED
        assignment.second_approved_by_user_id = actor.id
        from datetime import UTC, datetime

        assignment.second_approved_at = datetime.now(UTC)
    elif action in ("NEEDS_CORRECTION", "REJECT"):
        if current not in (AssignmentStatus.UNDER_REVIEW, AssignmentStatus.PENDING_SECOND_APPROVAL):
            raise CollectionError(f"Cannot {action.lower()} from status {current.value}")
        if not comment or not comment.strip():
            raise CollectionError("A review comment is required", 422)
        new_status = (
            AssignmentStatus.NEEDS_CORRECTION
            if action == "NEEDS_CORRECTION"
            else AssignmentStatus.REJECTED
        )
        audit_action = AuditAction.REJECTED
    else:
        raise CollectionError(
            "action must be START_REVIEW, APPROVE, SECOND_APPROVAL, NEEDS_CORRECTION or REJECT",
            422,
        )

    assignment.status = new_status
    value = latest_value(db, assignment.id)
    if value is not None:
        value.status = VALUE_STATUS_FOR_ASSIGNMENT[new_status]
    db.flush()

    if new_status == AssignmentStatus.APPROVED:
        # consolidated ancestors of this entity are now potentially stale
        from app.consolidation.service import mark_stale_upstream

        mark_stale_upstream(db, assignment.entity_id, assignment.metric_code,
                            assignment.period_id)
    record(
        db, action=audit_action, object_type="assignment", object_id=assignment.id,
        actor_id=actor.id, actor_label=actor.email,
        entity_id=assignment.entity_id, metric_code=assignment.metric_code,
        old_value={"status": current.value}, new_value={"status": new_status.value},
        reason=comment, request_id=request_id,
    )

    from app.notifications.router import notify_review

    if new_status == AssignmentStatus.APPROVED:
        notify_review(db, assignment, NotificationType.APPROVED,
                      f"{assignment.metric_code} was approved by {actor.email}")
    elif new_status in (AssignmentStatus.NEEDS_CORRECTION, AssignmentStatus.REJECTED):
        notify_review(db, assignment, NotificationType.RETURNED_FOR_CORRECTION,
                      f"{assignment.metric_code} returned by {actor.email}: {comment or ''}")
    return assignment


def reopen(
    db: Session,
    assignment: Assignment,
    *,
    actor: AppUser,
    reason: str,
    request_id: str | None = None,
) -> Assignment:
    """Post-approval change entry point (feature 4 / §4 rules): reopens an
    APPROVED assignment for correction; the subsequent submission enters the
    change-impact process. LOCKED assignments cannot be reopened."""
    if actor.role.name not in ("ESG_MANAGER", "ADMIN"):
        raise CollectionError("Only the ESG Manager may reopen an approved assignment", 403)
    if not reason or not reason.strip():
        raise CollectionError("A reason is required to reopen an approved assignment", 422)
    period = db.get(ReportingPeriod, assignment.period_id)
    if period is not None and period.locked:
        raise CollectionError("Reporting period is locked; assignment cannot be reopened")
    if assignment.status != AssignmentStatus.APPROVED:
        raise CollectionError(f"Cannot reopen an assignment in status {assignment.status.value}")
    assignment.status = AssignmentStatus.IN_PROGRESS
    value = latest_value(db, assignment.id)
    if value is not None:
        value.status = VALUE_STATUS_FOR_ASSIGNMENT[AssignmentStatus.IN_PROGRESS]
    db.flush()
    record(
        db, action=AuditAction.REOPENED, object_type="assignment", object_id=assignment.id,
        actor_id=actor.id, actor_label=actor.email,
        entity_id=assignment.entity_id, metric_code=assignment.metric_code,
        old_value={"status": AssignmentStatus.APPROVED.value},
        new_value={"status": AssignmentStatus.IN_PROGRESS.value},
        reason=reason, request_id=request_id,
    )
    from app.notifications.router import notify_review

    notify_review(db, assignment, NotificationType.RETURNED_FOR_CORRECTION,
                  f"{assignment.metric_code} reopened by {actor.email}: {reason}")
    return assignment


def reassign(
    db: Session,
    assignment: Assignment,
    *,
    new_owner_id: uuid.UUID,
    actor: AppUser,
    reason: str,
    request_id: str | None = None,
) -> Assignment:
    """Manager orchestration (feature 12): reassign the data owner. Audited
    with old/new owner + reason; the audit trail is the history mechanism."""
    if actor.role.name not in ("ESG_MANAGER", "ADMIN"):
        raise CollectionError("Only the ESG Manager may reassign work items", 403)
    if not reason or not reason.strip():
        raise CollectionError("A reason is required for reassignment", 422)
    new_owner = db.get(AppUser, new_owner_id)
    if new_owner is None or new_owner.role != UserRole.DATA_OWNER:
        raise CollectionError("New owner must be an active DATA_OWNER user", 422)
    old_owner_id = assignment.owner_user_id
    if old_owner_id == new_owner_id:
        raise CollectionError("The assignment already belongs to this owner", 422)
    old_owner = db.get(AppUser, old_owner_id)
    assignment.owner_user_id = new_owner_id
    db.flush()
    record(
        db, action=AuditAction.REASSIGNED, object_type="assignment", object_id=assignment.id,
        actor_id=actor.id, actor_label=actor.email,
        entity_id=assignment.entity_id, metric_code=assignment.metric_code,
        old_value={"owner": old_owner.email if old_owner else str(old_owner_id)},
        new_value={"owner": new_owner.email},
        reason=reason, request_id=request_id,
    )
    from app.notifications.router import notify

    notify(db, new_owner_id, NotificationType.ASSURANCE_REQUEST, {
        "message": f"{assignment.metric_code} was reassigned to you by {actor.email}: {reason}",
        "assignment_id": str(assignment.id),
        "metric_code": assignment.metric_code,
    })
    return assignment


def set_reviewers(
    db: Session,
    assignment: Assignment,
    *,
    actor: AppUser,
    reviewer_user_id: uuid.UUID | None = None,
    second_approver_user_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Assignment:
    if actor.role.name not in ("ESG_MANAGER", "ADMIN"):
        raise CollectionError("Only the ESG Manager may designate reviewers", 403)
    changed: dict = {}
    if reviewer_user_id is not None:
        user = db.get(AppUser, reviewer_user_id)
        if user is None or user.role.name not in ("REVIEWER", "ESG_MANAGER", "ADMIN"):
            raise CollectionError("Reviewer must be a REVIEWER/ESG_MANAGER/ADMIN user", 422)
        if reviewer_user_id == assignment.owner_user_id:
            raise CollectionError(
                "Separation of duties: the data owner cannot be designated as reviewer", 422
            )
        assignment.reviewer_user_id = reviewer_user_id
        changed["reviewer"] = user.email
    if second_approver_user_id is not None:
        user = db.get(AppUser, second_approver_user_id)
        if user is None or user.role.name not in ("ESG_MANAGER", "ADMIN", "REVIEWER"):
            raise CollectionError("Second approver must be an approver-role user", 422)
        if second_approver_user_id == assignment.owner_user_id:
            raise CollectionError(
                "Separation of duties: the data owner cannot be the second approver", 422
            )
        assignment.second_approver_user_id = second_approver_user_id
        changed["second_approver"] = user.email
    db.flush()
    if changed:
        record(
            db, action=AuditAction.UPDATED, object_type="assignment", object_id=assignment.id,
            actor_id=actor.id, actor_label=actor.email,
            entity_id=assignment.entity_id, metric_code=assignment.metric_code,
            new_value=changed, reason="reviewer designation", request_id=request_id,
        )
    return assignment
