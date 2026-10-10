"""Assurance Room service (feature 7). The ASSESSOR role is reused; requests
are tracked work items. The assessor NEVER gains a write path on official
metric values — this module only reads and creates/updates its own rows."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.models import (
    AppUser,
    Assignment,
    AssuranceRequest,
    Entity,
    ReportingPeriod,
)
from app.models.enums import (
    AssuranceRequestStatus,
    AssuranceRequestType,
    AuditAction,
    NotificationType,
    UserRole,
)


class AssuranceError(Exception):
    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def create_request(
    db: Session,
    *,
    requester: AppUser,
    request_type: AssuranceRequestType,
    entity_id: uuid.UUID,
    metric_code: str,
    period_id: uuid.UUID,
    reason: str,
    assignment_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    due_date=None,
    request_id: str | None = None,
) -> AssuranceRequest:
    if requester.role != UserRole.ASSESSOR and requester.role != UserRole.ADMIN:
        raise AssuranceError("Only the assessor may raise assurance requests", 403)
    if not reason or not reason.strip():
        raise AssuranceError("A request reason is required", 422)
    if db.get(Entity, entity_id) is None or db.get(ReportingPeriod, period_id) is None:
        raise AssuranceError("Entity or period not found", 404)
    assignment = None
    if assignment_id is not None:
        assignment = db.get(Assignment, assignment_id)
        if assignment is None:
            raise AssuranceError("Assignment not found", 404)
    if assignee_id is not None:
        assignee = db.get(AppUser, assignee_id)
        if assignee is None:
            raise AssuranceError("Assignee not found", 404)
    req = AssuranceRequest(
        request_type=request_type,
        status=AssuranceRequestStatus.ASSIGNED if assignee_id else AssuranceRequestStatus.OPEN,
        period_id=period_id,
        entity_id=entity_id,
        metric_code=metric_code,
        assignment_id=assignment_id,
        requested_by=requester.id,
        assignee_id=assignee_id,
        reason=reason.strip(),
        due_date=due_date,
    )
    db.add(req)
    db.flush()
    record(
        db, action=AuditAction.CREATED, object_type="assurance_request",
        object_id=req.id, actor_id=requester.id, actor_label=requester.email,
        entity_id=entity_id, metric_code=metric_code,
        new_value={"type": request_type.value, "reason": reason[:200]},
        request_id=request_id,
    )
    if assignee_id:
        from app.notifications.router import notify

        notify(db, assignee_id, NotificationType.ASSURANCE_REQUEST, {
            "message": f"Assurance {request_type.value} request for {metric_code}: {reason[:160]}",
            "assurance_request_id": str(req.id),
        })
    return req


def transition(
    db: Session,
    req: AssuranceRequest,
    *,
    action: str,
    actor: AppUser,
    comment: str | None = None,
    response_text: str | None = None,
    response_evidence_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> AssuranceRequest:
    """Server-side state machine:

    OPEN → ASSIGNED → IN_PROGRESS → RESPONDED → UNDER_REVIEW → RESOLVED/REJECTED
    """
    is_admin = actor.role == UserRole.ADMIN
    is_assignee = actor.id == req.assignee_id
    is_requester = actor.id == req.requested_by
    is_reviewer = actor.role in (UserRole.REVIEWER, UserRole.ESG_MANAGER)

    if action == "ASSIGN":
        if not (is_requester or is_admin):
            raise AssuranceError("Only the requester may assign the request", 403)
        if req.status not in (AssuranceRequestStatus.OPEN, AssuranceRequestStatus.ASSIGNED):
            raise AssuranceError(f"Cannot assign from status {req.status.value}")
        if assignee_id is None:
            raise AssuranceError("assignee_id is required", 422)
        req.assignee_id = assignee_id
        req.status = AssuranceRequestStatus.ASSIGNED
    elif action == "START":
        if not (is_assignee or is_admin):
            raise AssuranceError("Only the assigned data owner may start work", 403)
        if req.status not in (AssuranceRequestStatus.ASSIGNED, AssuranceRequestStatus.OPEN):
            raise AssuranceError(f"Cannot start from status {req.status.value}")
        req.status = AssuranceRequestStatus.IN_PROGRESS
    elif action == "RESPOND":
        if not (is_assignee or is_admin):
            raise AssuranceError("Only the assigned data owner may respond", 403)
        if req.status not in (AssuranceRequestStatus.ASSIGNED, AssuranceRequestStatus.IN_PROGRESS):
            raise AssuranceError(f"Cannot respond from status {req.status.value}")
        if not response_text or not response_text.strip():
            raise AssuranceError("A response is required", 422)
        req.response_text = response_text.strip()
        req.response_evidence_id = response_evidence_id
        req.status = AssuranceRequestStatus.RESPONDED
    elif action == "UNDER_REVIEW":
        if not (is_requester or is_reviewer or is_admin):
            raise AssuranceError("Only the assessor or a reviewer may move to review", 403)
        if req.status != AssuranceRequestStatus.RESPONDED:
            raise AssuranceError(f"Cannot review from status {req.status.value}")
        req.status = AssuranceRequestStatus.UNDER_REVIEW
    elif action == "RESOLVE":
        if not (is_requester or is_reviewer or is_admin):
            raise AssuranceError("Only the assessor or a reviewer may resolve", 403)
        if req.status not in (AssuranceRequestStatus.RESPONDED, AssuranceRequestStatus.UNDER_REVIEW):
            raise AssuranceError(f"Cannot resolve from status {req.status.value}")
        req.status = AssuranceRequestStatus.RESOLVED
        req.reviewed_by = actor.id
        req.resolved_at = datetime.now(UTC)
    elif action == "REJECT":
        if not (is_requester or is_reviewer or is_admin):
            raise AssuranceError("Only the assessor or a reviewer may reject", 403)
        if req.status not in (AssuranceRequestStatus.RESPONDED, AssuranceRequestStatus.UNDER_REVIEW):
            raise AssuranceError(f"Cannot reject from status {req.status.value}")
        if not comment or not comment.strip():
            raise AssuranceError("A rejection comment is required", 422)
        req.status = AssuranceRequestStatus.REJECTED
        req.reviewed_by = actor.id
        req.resolved_at = datetime.now(UTC)
    elif action == "COMMENT":
        pass
    else:
        raise AssuranceError("Unknown assurance action", 422)

    if comment and comment.strip():
        req.comments = list(req.comments or []) + [{
            "actor": actor.email, "comment": comment.strip(),
            "at": datetime.now(UTC).isoformat(),
        }]
    db.flush()
    record(
        db, action=AuditAction.UPDATED, object_type="assurance_request",
        object_id=req.id, actor_id=actor.id, actor_label=actor.email,
        entity_id=req.entity_id, metric_code=req.metric_code,
        new_value={"status": req.status.value, "action": action},
        reason=comment, request_id=request_id,
    )
    return req


def visible_request_ids(db: Session, user: AppUser) -> set[uuid.UUID] | None:
    """None = unrestricted (ADMIN). Assessor sees own requests; data owners
    see requests assigned to them; reviewers see their scoped entities."""
    if user.role == UserRole.ADMIN:
        return None
    if user.role == UserRole.ASSESSOR:
        return set(
            db.scalars(
                select(AssuranceRequest.id).where(AssuranceRequest.requested_by == user.id)
            ).all()
        )
    if user.role == UserRole.DATA_OWNER:
        return set(
            db.scalars(
                select(AssuranceRequest.id).where(AssuranceRequest.assignee_id == user.id)
            ).all()
        )
    from app.entities.service import get_descendant_ids

    scoped: set[uuid.UUID] = set()
    for scope in user.entity_scopes:
        scoped |= get_descendant_ids(db, scope.entity_id)
    return set(
        db.scalars(
            select(AssuranceRequest.id).where(AssuranceRequest.entity_id.in_(scoped))
        ).all()
    )
