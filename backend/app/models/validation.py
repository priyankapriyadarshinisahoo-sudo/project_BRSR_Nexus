"""Versioned validation rules and their raised exceptions."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import (
    ValidationExceptionStatus,
    ValidationRuleClass,
    ValidationSeverity,
)


class ValidationRule(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "validation_rule"
    __table_args__ = (
        UniqueConstraint("framework_version_id", "rule_code", "version"),
        Index("ix_validation_rule_target", "framework_version_id", "target_metric_code"),
    )

    framework_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("framework_version.id", ondelete="RESTRICT"), nullable=False
    )
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_class: Mapped[ValidationRuleClass] = mapped_column(
        Enum(ValidationRuleClass, name="validation_rule_class", native_enum=True), nullable=False
    )
    target_metric_code: Mapped[str | None] = mapped_column(String(100))
    severity: Mapped[ValidationSeverity] = mapped_column(
        Enum(ValidationSeverity, name="validation_severity", native_enum=True), nullable=False
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # draft | submit | review | recalc
    applies_on: Mapped[str] = mapped_column(String(20), nullable=False, default="submit")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    message_template: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ValidationException(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "validation_exception"
    __table_args__ = (
        Index("ix_validation_exception_period_status", "period_id", "status"),
        Index("ix_validation_exception_value", "metric_value_id"),
    )

    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("validation_rule.id", ondelete="SET NULL")
    )
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[ValidationSeverity] = mapped_column(
        Enum(ValidationSeverity, name="validation_severity", native_enum=True),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity.id", ondelete="RESTRICT"), nullable=False
    )
    metric_code: Mapped[str | None] = mapped_column(String(100))
    period_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reporting_period.id", ondelete="CASCADE")
    )
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assignment.id", ondelete="CASCADE")
    )
    metric_value_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("metric_value.id", ondelete="CASCADE")
    )
    observed_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    # remediation workflow (feature 8): accountable corrective action
    category: Mapped[str | None] = mapped_column(String(50))
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    action_required: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[ValidationExceptionStatus] = mapped_column(
        Enum(ValidationExceptionStatus, name="validation_exception_status", native_enum=True),
        nullable=False,
        default=ValidationExceptionStatus.OPEN,
    )
    explanation: Mapped[str | None] = mapped_column(Text)
    explained_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    explained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
