"""Evidence API: upload, metadata, signed download, soft delete."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_scoped_entity_ids
from app.db.session import get_db
from app.evidence import service
from app.logging import request_id_var
from app.models import AppUser, Assignment, Evidence, MetricValue
from app.models.enums import UserRole

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])


class EvidenceOut(BaseModel):
    id: uuid.UUID
    metric_value_id: uuid.UUID
    category: str | None = None
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256_hash: str
    uploaded_by: uuid.UUID
    uploaded_at: object
    is_deleted: bool


def _out(e: Evidence) -> EvidenceOut:
    return EvidenceOut(
        id=e.id, metric_value_id=e.metric_value_id, category=e.category,
        original_filename=e.original_filename, mime_type=e.mime_type,
        size_bytes=e.size_bytes, sha256_hash=e.sha256_hash,
        uploaded_by=e.uploaded_by, uploaded_at=e.uploaded_at, is_deleted=e.is_deleted,
    )


def _load_scoped(db: Session, evidence_id: uuid.UUID, user: AppUser,
                 scoped_ids: set[uuid.UUID]) -> Evidence:
    evidence = db.get(Evidence, evidence_id)
    if evidence is None or evidence.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence not found")
    value = db.get(MetricValue, evidence.metric_value_id)
    assignment = db.get(Assignment, value.assignment_id) if value else None
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    if user.role not in (UserRole.ADMIN, UserRole.ESG_MANAGER) and assignment.entity_id not in scoped_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Evidence outside your authorized scope")
    return evidence


def _assignment_for_value(db: Session, metric_value_id: uuid.UUID) -> Assignment:
    value = db.get(MetricValue, metric_value_id)
    if value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric value not found")
    return db.get(Assignment, value.assignment_id)


@router.post("", response_model=EvidenceOut, status_code=201)
async def upload_evidence(
    request: Request,
    file: UploadFile,
    metric_value_id: uuid.UUID,
    category: str | None = None,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> EvidenceOut:
    assignment = _assignment_for_value(db, metric_value_id)
    if user.role not in (UserRole.ADMIN, UserRole.ESG_MANAGER):
        if assignment.entity_id not in scoped_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Entity outside your authorized scope")
        if assignment.owner_user_id != user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Only the assignment owner may upload evidence")
    content = await file.read()
    try:
        evidence, duplicate_of = service.upload_evidence(
            db, metric_value_id=metric_value_id,
            filename=file.filename or "evidence.bin",
            mime_type=file.content_type or "application/octet-stream",
            content=content, actor=user, category=category,
            request_id=request_id_var.get(),
        )
        db.commit()
    except service.EvidenceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    db.refresh(evidence)
    out = _out(evidence)
    if duplicate_of is not None:
        # informational: identical file already attached elsewhere
        out = out.model_copy(update={"original_filename": out.original_filename})
        import logging

        logging.getLogger(__name__).info(
            "duplicate evidence uploaded", extra={"job_id": str(duplicate_of.id)}
        )
    return out


@router.get("/{evidence_id}", response_model=EvidenceOut)
def get_evidence(
    evidence_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> EvidenceOut:
    return _out(_load_scoped(db, evidence_id, user, scoped_ids))


@router.get("/{evidence_id}/download")
def download_evidence(
    evidence_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> dict:
    evidence = _load_scoped(db, evidence_id, user, scoped_ids)
    return {"url": service.download_url(evidence), "expires_in_hours": 1}


@router.get("/by-value/{metric_value_id}", response_model=list[EvidenceOut])
def list_for_value(
    metric_value_id: uuid.UUID,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> list[EvidenceOut]:
    assignment = _assignment_for_value(db, metric_value_id)
    if user.role not in (UserRole.ADMIN, UserRole.ESG_MANAGER) and assignment.entity_id not in scoped_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Entity outside your authorized scope")
    rows = list(
        db.scalars(
            select(Evidence)
            .where(Evidence.metric_value_id == metric_value_id, Evidence.is_deleted.is_(False))
            .order_by(Evidence.uploaded_at.desc())
        ).all()
    )
    return [_out(e) for e in rows]


@router.delete("/{evidence_id}", status_code=204)
def delete_evidence(
    evidence_id: uuid.UUID,
    request: Request,
    user: AppUser = Depends(get_current_user),
    scoped_ids: set[uuid.UUID] = Depends(get_scoped_entity_ids),
    db: Session = Depends(get_db),
) -> None:
    evidence = _load_scoped(db, evidence_id, user, scoped_ids)
    assignment = _assignment_for_value(db, evidence.metric_value_id)
    if user.role not in (UserRole.ADMIN, UserRole.ESG_MANAGER) and assignment.owner_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the assignment owner may delete evidence")
    service.soft_delete(db, evidence, user, request_id=request_id_var.get())
    db.commit()
