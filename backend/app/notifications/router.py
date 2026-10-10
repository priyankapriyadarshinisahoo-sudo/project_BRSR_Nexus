"""Notifications service + API (spec §22). In-app notifications created by
review actions and the Celery Beat reminder scan."""
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db.session import get_db
from app.models import AppUser, Assignment, Notification, UserEntityScope
from app.models.enums import (
    AssignmentStatus,
    AuditAction,
    NotificationType,
    UserRole,
)
from app.config import get_settings


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: NotificationType
    payload: dict
    read_at: datetime | None
    created_at: datetime


def notify(
    db: Session,
    recipient_id: uuid.UUID,
    type_: NotificationType,
    payload: dict,
) -> None:
    db.add(Notification(recipient_user_id=recipient_id, type=type_, payload=payload))
    db.flush()


router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def my_notifications(
    unread_only: bool = Query(default=False),
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationOut]:
    stmt = (
        select(Notification)
        .where(Notification.recipient_user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = db.scalars(stmt).all()
    return [
        NotificationOut(id=n.id, type=n.type, payload=n.payload, read_at=n.read_at,
                        created_at=n.created_at)
        for n in rows
    ]


@router.get("/unread-count")
def unread_count(user: AppUser = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    count = len(
        db.scalars(
            select(Notification.id).where(
                Notification.recipient_user_id == user.id,
                Notification.read_at.is_(None),
            )
        ).all()
    )
    return {"unread": count}


@router.post("/{notification_id}/read", status_code=204)
def mark_read(
    notification_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    n = db.get(Notification, notification_id)
    if n is None or n.recipient_user_id != user.id:
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    if n.read_at is None:
        n.read_at = datetime.now(UTC)
        db.commit()
    return None


# ------------------------------------------------------------------ beat scan
def scan_reminders(db: Session) -> dict:
    """Celery Beat scan (spec §22): due-soon, due-tomorrow and overdue
    notifications for assignment owners; idempotent per day per assignment."""
    settings = get_settings()
    today = date.today()
    created = 0
    open_assignments = list(
        db.scalars(
            select(Assignment).where(
                Assignment.due_date.is_not(None),
                Assignment.status.notin_(
                    [AssignmentStatus.APPROVED, AssignmentStatus.LOCKED]
                ),
            )
        ).all()
    )
    for a in open_assignments:
        days_left = (a.due_date - today).days
        if days_left < 0:
            type_, label = NotificationType.OVERDUE, f"overdue by {-days_left} day(s)"
        elif days_left == 0:
            type_, label = NotificationType.DUE_TOMORROW, "due tomorrow"
        elif days_left <= settings.reminder_days_before_due:
            type_, label = NotificationType.DUE_SOON, f"due in {days_left} day(s)"
        else:
            continue
        # idempotency: one notification of this type per assignment per day
        exists = db.scalar(
            select(Notification.id).where(
                Notification.recipient_user_id == a.owner_user_id,
                Notification.type == type_,
                Notification.payload["assignment_id"].astext == str(a.id),
                Notification.created_at >= datetime.now(UTC).replace(hour=0, minute=0, second=0),
            )
        )
        if exists:
            continue
        notify(db, a.owner_user_id, type_, {
            "assignment_id": str(a.id),
            "metric_code": a.metric_code,
            "due_date": str(a.due_date),
            "message": f"{a.metric_code} is {label}",
        })
        created += 1
    escalation = escalate_overdue(db, today)
    db.commit()
    return {"created": created, "scanned": len(open_assignments), "escalations": escalation}


def escalate_overdue(db: Session, today) -> dict:
    """SLA + escalation engine (feature 9): risk-sensitive escalation for
    overdue work items. Level 1 fires on the first overdue day, level 2 when
    overdue by more than 7 days on a CRITICAL/BRSR Core metric. Deduped by
    the escalation_event table (one row per assignment+level — no spam) and
    recorded in the audit trail."""
    from datetime import timedelta

    from sqlalchemy import select as _select

    from app.audit.service import record
    from app.governance.policies import get_policy
    from app.models import AppUser, EscalationEvent
    from app.models.enums import AuditAction, MetricRiskTier, UserRole

    fired = 0
    open_assignments = list(
        db.scalars(
            select(Assignment).where(
                Assignment.due_date.is_not(None),
                Assignment.due_date < today,
                Assignment.status.notin_(
                    [AssignmentStatus.APPROVED, AssignmentStatus.LOCKED]
                ),
            )
        ).all()
    )
    managers = list(
        db.scalars(
            select(AppUser).where(
                AppUser.role == UserRole.ESG_MANAGER, AppUser.is_active.is_(True)
            )
        ).all()
    )
    for a in open_assignments:
        policy = get_policy(db, a.framework_version_id, a.metric_code)
        tier = policy.risk_tier.value
        days_overdue = (today - a.due_date).days
        # level 1: overdue — owner already reminded; escalate beyond owner per policy
        if tier == MetricRiskTier.STANDARD and days_overdue <= 7:
            continue  # standard: owner reminder only
        level = 2 if (tier == MetricRiskTier.CRITICAL and days_overdue > 7) else 1
        exists = db.scalar(
            _select(EscalationEvent.id).where(
                EscalationEvent.assignment_id == a.id,
                EscalationEvent.level >= level,
            )
        )
        if exists:
            continue
        recipients: list[AppUser] = []
        if tier != MetricRiskTier.STANDARD:
            recipients.extend(managers)
        if tier == MetricRiskTier.CRITICAL and a.reviewer_user_id:
            reviewer = db.get(AppUser, a.reviewer_user_id)
            if reviewer is not None:
                recipients.append(reviewer)
        recipient_ids = [r.id for r in recipients if r.id != a.owner_user_id]
        db.add(EscalationEvent(
            assignment_id=a.id, level=level, risk_tier=tier,
            recipients=[str(r) for r in recipient_ids],
        ))
        for recipient_id in recipient_ids:
            notify(db, recipient_id, NotificationType.ESCALATION, {
                "assignment_id": str(a.id),
                "metric_code": a.metric_code,
                "due_date": str(a.due_date),
                "level": level,
                "risk_tier": tier,
                "message": f"ESCALATION L{level}: {a.metric_code} is {days_overdue} day(s) overdue "
                           f"(risk tier {tier})",
            })
        record(
            db, action=AuditAction.ESCALATED, object_type="assignment",
            object_id=a.id, actor_label="sla-engine",
            entity_id=a.entity_id, metric_code=a.metric_code,
            new_value={"level": level, "risk_tier": tier,
                       "days_overdue": days_overdue,
                       "recipients": len(recipient_ids)},
            reason="SLA escalation",
        )
        fired += 1
    return {"fired": fired, "scanned": len(open_assignments)}


# review-action notifications are emitted from the collection service
def notify_review(db: Session, assignment, type_: NotificationType, message: str) -> None:
    notify(db, assignment.owner_user_id, type_, {
        "assignment_id": str(assignment.id),
        "metric_code": assignment.metric_code,
        "message": message,
    })
