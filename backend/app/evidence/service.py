"""Evidence service: bytes in S3-compatible storage (RustFS), metadata in PostgreSQL, SHA-256 integrity,
signed time-limited downloads, soft delete. Private bucket — no public access.
"""
import uuid
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.config import get_settings
from app.infra import storage
from app.models import AppUser, Assignment, Evidence, MetricValue
from app.models.enums import AuditAction

# mime type -> allowed file extensions
MIME_EXTENSIONS = {
    "application/pdf": {".pdf"},
    "text/csv": {".csv"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
}


class EvidenceError(Exception):
    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _validate_file(filename: str, mime: str, size_bytes: int) -> None:
    settings = get_settings()
    if mime not in settings.allowed_mime_list:
        raise EvidenceError(f"MIME type {mime!r} is not allowed")
    if size_bytes > settings.max_evidence_file_size_mb * 1024 * 1024:
        raise EvidenceError(
            f"File exceeds the {settings.max_evidence_file_size_mb} MB limit"
        )
    if size_bytes == 0:
        raise EvidenceError("Empty files are not valid evidence")
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed_exts = MIME_EXTENSIONS.get(mime, set())
    if ext not in allowed_exts:
        raise EvidenceError(
            f"Extension {ext!r} does not match MIME type {mime!r} (allowed: {sorted(allowed_exts)})"
        )


def _safe_filename(filename: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename)
    return keep[:200] or "evidence.bin"


def upload_evidence(
    db: Session,
    *,
    metric_value_id: uuid.UUID,
    filename: str,
    mime_type: str,
    content: bytes,
    actor: AppUser,
    category: str | None = None,
    request_id: str | None = None,
) -> tuple[Evidence, Evidence | None]:
    """Store the file in object storage and its metadata in PostgreSQL (same commit as
    the caller's transaction). Returns (evidence, duplicate_of) where
    duplicate_of is an existing evidence row with the identical SHA-256."""
    _validate_file(filename, mime_type, len(content))

    value = db.get(MetricValue, metric_value_id)
    if value is None:
        raise EvidenceError("Metric value not found", 404)
    assignment = db.get(Assignment, value.assignment_id)
    if assignment is None:
        raise EvidenceError("Assignment not found", 500)

    import hashlib

    sha256 = hashlib.sha256(content).hexdigest()

    duplicate_of = db.scalar(
        select(Evidence).where(
            Evidence.sha256_hash == sha256, Evidence.is_deleted.is_(False)
        )
    )

    settings = get_settings()
    evidence_id = uuid.uuid4()
    object_key = (
        f"evidence/{assignment.entity_id}/{value.assignment_id}/"
        f"{value.id}/{evidence_id}/{_safe_filename(filename)}"
    )
    client = storage.get_client()
    try:
        client.put_object(
            settings.s3_bucket,
            object_key,
            BytesIO(content),
            length=len(content),
            content_type=mime_type,
        )
    except Exception as exc:
        raise EvidenceError(f"Object storage rejected the upload: {exc}", 502)

    evidence = Evidence(
        id=evidence_id,
        metric_value_id=value.id,
        object_key=object_key,
        original_filename=_safe_filename(filename),
        mime_type=mime_type,
        size_bytes=len(content),
        sha256_hash=sha256,
        uploaded_by=actor.id,
        category=category,
    )
    db.add(evidence)
    db.flush()
    record(
        db, action=AuditAction.CREATED, object_type="evidence", object_id=evidence.id,
        actor_id=actor.id, actor_label=actor.email,
        entity_id=assignment.entity_id, metric_code=assignment.metric_code,
        evidence_reference=evidence.id,
        new_value={"filename": evidence.original_filename, "size": evidence.size_bytes,
                   "sha256": sha256, "duplicate_of": str(duplicate_of.id) if duplicate_of else None},
        request_id=request_id,
    )
    return evidence, duplicate_of


def download_url(evidence: Evidence, expires_hours: int = 1) -> str:
    """Pre-signed, time-limited GET URL for the private object."""
    return storage.presigned_get(evidence.object_key, expires_hours)


def soft_delete(
    db: Session,
    evidence: Evidence,
    actor: AppUser,
    request_id: str | None = None,
) -> None:
    from datetime import UTC, datetime

    evidence.is_deleted = True
    evidence.deleted_at = datetime.now(UTC)
    db.flush()
    record(
        db, action=AuditAction.UPDATED, object_type="evidence", object_id=evidence.id,
        actor_id=actor.id, actor_label=actor.email,
        new_value={"is_deleted": True}, reason="evidence soft-deleted",
        request_id=request_id,
    )
