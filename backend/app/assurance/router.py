"""Assurance Room API (feature 7): /api/v1/assurance/requests."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assurance import service
from app.auth.deps import get_current_user
from app.db.session import get_db
from app.logging import request_id_var
from app.models import AppUser, AssuranceRequest, Entity
from app.models.enums import AssuranceRequestStatus, AssuranceRequestType

router = APIRouter(prefix="/api/v1/assurance", tags=["assurance"])


class RequestOut(BaseModel):
    id: uuid.UUID
    request_type: AssuranceRequestType
    status: AssuranceRequestStatus
    entity_id: uuid.UUID
    entity_name: str | None = None
    metric_code: str
    assignment_id: uuid.UUID | None
    requested_by: uuid.UUID
    assignee_id: uuid.UUID | None
    reason: str
    response_text: str | None
    response_evidence_id: uuid.UUID | None
    due_date: str | None
    comments: list
    created_at: object
    resolved_at: object | None


class RequestCreate(BaseModel):
    request_type: AssuranceRequestType
    entity_id: uuid.UUID
    metric_code: str
    period_id: uuid.UUID
    reason: str
    assignment_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    due_date: str | None = None


class RequestAction(BaseModel):
    action: str
    comment: str | None = None
    response_text: str | None = None
    response_evidence_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None


def _out(db: Session, req: AssuranceRequest) -> RequestOut:
    entity = db.get(Entity, req.entity_id)
    return RequestOut(
        id=req.id, request_type=req.request_type, status=req.status,
        entity_id=req.entity_id, entity_name=entity.name if entity else None,
        metric_code=req.metric_code, assignment_id=req.assignment_id,
        requested_by=req.requested_by, assignee_id=req.assignee_id,
        reason=req.reason, response_text=req.response_text,
        response_evidence_id=req.response_evidence_id,
        due_date=str(req.due_date) if req.due_date else None,
        comments=req.comments or [],
        created_at=req.created_at, resolved_at=req.resolved_at,
    )


def _load(db: Session, request_id: uuid.UUID, user: AppUser) -> AssuranceRequest:
    req = db.get(AssuranceRequest, request_id)
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    visible = service.visible_request_ids(db, user)
    if visible is not None and req.id not in visible:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Request outside your authorized scope")
    return req


@router.get("/requests", response_model=list[RequestOut])
def list_requests(
    request_status: AssuranceRequestStatus | None = Query(default=None, alias="status"),
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RequestOut]:
    visible = service.visible_request_ids(db, user)
    stmt = select(AssuranceRequest).order_by(AssuranceRequest.created_at.desc())
    if visible is not None:
        if not visible:
            return []
        stmt = stmt.where(AssuranceRequest.id.in_(visible))
    if request_status:
        stmt = stmt.where(AssuranceRequest.status == request_status)
    rows = db.scalars(stmt.limit(200)).all()
    return [_out(db, r) for r in rows]


@router.post("/requests", response_model=RequestOut, status_code=201)
def create_request(
    body: RequestCreate,
    request: Request,
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestOut:
    try:
        due = body.due_date
        from datetime import date, timedelta

        if due is None:
            due = str((date.today() + timedelta(days=14)))
        req = service.create_request(
            db, requester=user, request_type=body.request_type,
            entity_id=body.entity_id, metric_code=body.metric_code,
            period_id=body.period_id, reason=body.reason,
            assignment_id=body.assignment_id, assignee_id=body.assignee_id,
            due_date=due, request_id=request_id_var.get(),
        )
        db.commit()
    except service.AssuranceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(req)
    return _out(db, req)


@router.get("/requests/{request_id}", response_model=RequestOut)
def get_request(
    request_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestOut:
    return _out(db, _load(db, request_id, user))


@router.patch("/requests/{request_id}", response_model=RequestOut)
def act_on_request(
    request_id: uuid.UUID,
    body: RequestAction,
    request: Request,
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestOut:
    req = _load(db, request_id, user)
    try:
        req = service.transition(
            db, req, action=body.action, actor=user, comment=body.comment,
            response_text=body.response_text,
            response_evidence_id=body.response_evidence_id,
            assignee_id=body.assignee_id, request_id=request_id_var.get(),
        )
        db.commit()
    except service.AssuranceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(req)
    return _out(db, req)
