"""Collection API: assignments and their value/review lifecycle."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_scoped_entity_ids, require_roles
from app.collection import service
from app.db.session import get_db
from app.logging import request_id_var
from app.models import AppUser, Assignment, Entity, MetricValue, ReportingPeriod
from app.models.enums import AssignmentStatus, UserRole

router = APIRouter(prefix="/api/v1/assignments", tags=["assignments"])
require_creator = require_roles(UserRole.ADMIN, UserRole.ESG_MANAGER)


class AssignmentCreate(BaseModel):
    metric_code: str
    entity_id: uuid.UUID
    period_id: uuid.UUID
    owner_user_id: uuid.UUID
    due_date: str | None = None


class AssignmentOut(BaseModel):
    id: uuid.UUID
    metric_code: str
    entity_id: uuid.UUID
    period_id: uuid.UUID
    owner_user_id: uuid.UUID
    status: AssignmentStatus
    due_date: str | None


class ValueSave(BaseModel):
    action: str = Field(pattern="^(SAVE_DRAFT|SUBMIT)$")
    raw_value: float | None = None
    raw_unit: str | None = None
    qualitative_value: str | None = None
    expected_last_version: int | None = None
    methodology_version: str | None = None
    methodology_notes: str | None = None
    assumptions: dict | None = None


class ValueOut(BaseModel):
    id: uuid.UUID
    version: int
    raw_value: float | None
    raw_unit: str | None
    normalized_value: float | None
    normalized_unit: str | None
    qualitative_value: str | None
    status: str
    is_calculated: bool = False
    formula_version: int | None = None
    formula_inputs: dict | None = None
    submitted_at: str | None


class ReviewAction(BaseModel):
    action: str = Field(
        pattern="^(START_REVIEW|APPROVE|SECOND_APPROVAL|NEEDS_CORRECTION|REJECT)$"
    )
    comment: str | None = None


class AssignmentDetail(BaseModel):
    assignment: AssignmentOut
    entity_name: str
    period_label: str
    metric: dict
    values: list[ValueOut]
    latest_version: int | None


def _assignment_out(a: Assignment) -> AssignmentOut:
    return AssignmentOut(
        id=a.id, metric_code=a.metric_code, entity_id=a.entity_id, period_id=a.period_id,
        owner_user_id=a.owner_user_id, status=a.status, due_date=str(a.due_date) if a.due_date else None,
    )


def _value_out(v: MetricValue) -> ValueOut:
    return ValueOut(
        id=v.id, version=v.version,
        raw_value=float(v.raw_value) if v.raw_value is not None else None,
        raw_unit=v.raw_unit,
        normalized_value=float(v.normalized_value) if v.normalized_value is not None else None,
        normalized_unit=v.normalized_unit,
        qualitative_value=v.qualitative_value, status=v.status.value,
        is_calculated=v.is_calculated, formula_version=v.formula_version,
        formula_inputs=v.formula_inputs,
        submitted_at=v.submitted_at.isoformat() if v.submitted_at else None,
    )


def _load_scoped(db: Session, assignment_id: uuid.UUID, user: AppUser,
                 scoped_ids: set[uuid.UUID]) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    # DATA_OWNER works their own assignments regardless of entity-scope grants
    # (mirrors visible_assignment_ids); REVIEWER/MANAGEMENT rely on scopes.
    is_owner = user.role == UserRole.DATA_OWNER and assignment.owner_user_id == user.id
    if not is_owner and user.role not in (UserRole.ADMIN, UserRole.ESG_MANAGER) \
            and assignment.entity_id not in scoped_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Assignment outside your authorized scope")
    return assignment


@router.post("", response_model=AssignmentOut, status_code=201)
def create_assignment(
    body: AssignmentCreate,
    request: Request,
    user: AppUser = Depends(require_creator),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> AssignmentOut:
    if user.role == UserRole.ESG_MANAGER and body.entity_id not in scoped_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Entity outside your authorized scope")
    try:
        due = body.due_date
        assignment = service.create_assignment(
            db, metric_code=body.metric_code, entity_id=body.entity_id,
            period_id=body.period_id, owner_user_id=body.owner_user_id,
            created_by=user, due_date=due, request_id=request_id_var.get(),
        )
        db.commit()
    except service.CollectionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(assignment)
    return _assignment_out(assignment)


@router.get("", response_model=list[AssignmentOut])
def list_assignments(
    period_id: uuid.UUID | None = Query(default=None),
    entity_id: uuid.UUID | None = Query(default=None),
    assignment_status: AssignmentStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AssignmentOut]:
    visible = service.visible_assignment_ids(db, user)
    stmt = select(Assignment).order_by(Assignment.metric_code)
    if visible is not None:
        if not visible:
            return []
        stmt = stmt.where(Assignment.id.in_(visible))
    if period_id:
        stmt = stmt.where(Assignment.period_id == period_id)
    if entity_id:
        stmt = stmt.where(Assignment.entity_id == entity_id)
    if assignment_status:
        stmt = stmt.where(Assignment.status == assignment_status)
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return [_assignment_out(a) for a in rows]


@router.get("/{assignment_id}", response_model=AssignmentDetail)
def get_assignment(
    assignment_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> AssignmentDetail:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    entity = db.get(Entity, assignment.entity_id)
    period = db.get(ReportingPeriod, assignment.period_id)
    from app.models import MetricDefinition

    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.framework_version_id == assignment.framework_version_id,
            MetricDefinition.metric_code == assignment.metric_code,
        )
    )
    values = list(
        db.scalars(
            select(MetricValue)
            .where(MetricValue.assignment_id == assignment.id)
            .order_by(MetricValue.version.desc())
        ).all()
    )
    metric_meta = {}
    if metric is not None:
        metric_meta = {
            "metric_code": metric.metric_code, "label": metric.label,
            "description": metric.description, "data_type": metric.data_type.value,
            "unit_family": metric.unit_family, "allowed_units": metric.allowed_units,
            "canonical_unit": metric.canonical_unit, "required": metric.required,
            "evidence_required": metric.evidence_required, "brsr_core": metric.brsr_core,
            "section": metric.section, "principle": metric.principle,
        }
    last = values[0] if values else None
    return AssignmentDetail(
        assignment=_assignment_out(assignment),
        entity_name=entity.name if entity else "?",
        period_label=period.label if period else "?",
        metric=metric_meta,
        values=[_value_out(v) for v in values],
        latest_version=last.version if last else None,
    )


@router.post("/{assignment_id}/value", response_model=ValueOut, status_code=201)
def save_value(
    assignment_id: uuid.UUID,
    body: ValueSave,
    request: Request,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> ValueOut:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    try:
        value = service.save_value(
            db, assignment, action=body.action,
            payload={"raw_value": body.raw_value, "raw_unit": body.raw_unit,
                     "qualitative_value": body.qualitative_value,
                     "methodology_version": body.methodology_version,
                     "methodology_notes": body.methodology_notes,
                     "assumptions": body.assumptions},
            actor=user, expected_last_version=body.expected_last_version,
            request_id=request_id_var.get(),
        )
        db.commit()
    except service.CollectionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(value)
    return _value_out(value)


@router.post("/{assignment_id}/review", response_model=AssignmentOut)
def review_assignment(
    assignment_id: uuid.UUID,
    body: ReviewAction,
    request: Request,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> AssignmentOut:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    try:
        assignment = service.review(
            db, assignment, action=body.action, actor=user, comment=body.comment,
            request_id=request_id_var.get(),
        )
        db.commit()
    except service.CollectionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(assignment)
    return _assignment_out(assignment)


class ReopenBody(BaseModel):
    reason: str


class ReassignBody(BaseModel):
    new_owner_user_id: uuid.UUID
    reason: str


class ReviewersBody(BaseModel):
    reviewer_user_id: uuid.UUID | None = None
    second_approver_user_id: uuid.UUID | None = None


@router.post("/{assignment_id}/reopen", response_model=AssignmentOut)
def reopen_assignment(
    assignment_id: uuid.UUID,
    body: ReopenBody,
    request: Request,
    user: AppUser = Depends(require_creator),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> AssignmentOut:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    try:
        assignment = service.reopen(
            db, assignment, actor=user, reason=body.reason,
            request_id=request_id_var.get(),
        )
        db.commit()
    except service.CollectionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(assignment)
    return _assignment_out(assignment)


@router.post("/{assignment_id}/reassign", response_model=AssignmentOut)
def reassign_assignment(
    assignment_id: uuid.UUID,
    body: ReassignBody,
    request: Request,
    user: AppUser = Depends(require_creator),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> AssignmentOut:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    try:
        assignment = service.reassign(
            db, assignment, new_owner_id=body.new_owner_user_id, actor=user,
            reason=body.reason, request_id=request_id_var.get(),
        )
        db.commit()
    except service.CollectionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(assignment)
    return _assignment_out(assignment)


@router.post("/{assignment_id}/reviewers", response_model=AssignmentOut)
def set_reviewers(
    assignment_id: uuid.UUID,
    body: ReviewersBody,
    request: Request,
    user: AppUser = Depends(require_creator),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> AssignmentOut:
    assignment = _load_scoped(db, assignment_id, user, scoped_ids)
    try:
        assignment = service.set_reviewers(
            db, assignment, actor=user,
            reviewer_user_id=body.reviewer_user_id,
            second_approver_user_id=body.second_approver_user_id,
            request_id=request_id_var.get(),
        )
        db.commit()
    except service.CollectionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(assignment)
    return _assignment_out(assignment)
