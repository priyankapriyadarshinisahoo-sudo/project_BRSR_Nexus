"""Reporting periods, assignments and immutable MetricValue versions."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AssignmentStatus, MetricValueStatus


class ReportingPeriod(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reporting_period"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="dates_ordered"),
    )

    label: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    framework_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("framework_version.id", ondelete="RESTRICT"), nullable=False
    )
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))

    framework_version = relationship("FrameworkVersion")


class Assignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assignment"
    __table_args__ = (
        UniqueConstraint("metric_code", "entity_id", "period_id"),
        Index("ix_assignment_period_status", "period_id", "status"),
        Index("ix_assignment_owner", "owner_user_id"),
    )

    metric_code: Mapped[str] = mapped_column(String(100), nullable=False)
    framework_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("framework_version.id", ondelete="RESTRICT"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity.id", ondelete="RESTRICT"), nullable=False
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reporting_period.id", ondelete="RESTRICT"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    # manager orchestration: explicit reviewer / second-level approver (optional;
    # when NULL any role-eligible reviewer may act, SoD still applies)
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    second_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    # risk-adaptive review bookkeeping (denormalized for the Control Passport)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    second_approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    second_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus, name="assignment_status", native_enum=True),
        nullable=False,
        default=AssignmentStatus.NOT_STARTED,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )

    values: Mapped[list["MetricValue"]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )


class MetricValue(UUIDPrimaryKeyMixin, Base):
    """Immutable value version. Corrections create a new row; never an UPDATE."""

    __tablename__ = "metric_value"
    __table_args__ = (
        UniqueConstraint("assignment_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_metric_value_assignment", "assignment_id"),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assignment.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    raw_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    raw_unit: Mapped[str | None] = mapped_column(String(50))
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    normalized_unit: Mapped[str | None] = mapped_column(String(50))
    qualitative_value: Mapped[str | None] = mapped_column(Text)
    is_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_calculated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    formula_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("formula_definition.id", ondelete="SET NULL")
    )
    formula_version: Mapped[int | None] = mapped_column(Integer)
    formula_inputs: Mapped[dict | None] = mapped_column(JSONB)
    # governed methodology snapshot (feature 11): historical rows are immutable
    methodology_version: Mapped[str | None] = mapped_column(String(50))
    methodology_notes: Mapped[str | None] = mapped_column(Text)
    assumptions: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[MetricValueStatus] = mapped_column(
        Enum(MetricValueStatus, name="metric_value_status", native_enum=True),
        nullable=False,
        default=MetricValueStatus.IN_PROGRESS,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    assignment: Mapped[Assignment] = relationship(back_populates="values")
