"""Evidence file metadata (bytes live in S3-compatible object storage)."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Evidence(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_metric_value", "metric_value_id"),
        Index("ix_evidence_sha256", "sha256_hash"),
    )

    metric_value_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("metric_value.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # evidence-policy category (feature 10): source_data | methodology |
    # emission_factor | supporting_doc | reviewer_confirmation | None (legacy)
    category: Mapped[str | None] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
