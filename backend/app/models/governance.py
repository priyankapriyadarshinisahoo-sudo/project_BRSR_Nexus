"""Governance & assurance models: metric control policies, assurance
requests, change-impact events and escalation dedup.

These extend (never replace) the existing work items: policies attach to
metric definitions, impact events reference assignments/traces, assurance
requests reference assignments, escalation dedup references assignments.
"""
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    AssuranceRequestStatus,
    AssuranceRequestType,
    ImpactItemStatus,
    MetricRiskTier,
)


class MetricControlPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Risk/control policy for one metric in one framework version.

    Deliberately separate from ValidationSeverity: a risk tier decides the
    CONTROL PATH (review stages, evidence package, approvals), not how loud a
    single validation finding is. Absent row ⇒ STANDARD default policy.
    """

    __tablename__ = "metric_control_policy"
    __table_args__ = (
        UniqueConstraint("framework_version_id", "metric_code"),
        Index("ix_control_policy_risk", "framework_version_id", "risk_tier"),
    )

    framework_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("framework_version.id", ondelete="CASCADE"), nullable=False
    )
    metric_code: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_tier: Mapped[MetricRiskTier] = mapped_column(
        Enum(MetricRiskTier, name="metric_risk_tier", native_enum=True),
        nullable=False,
        default=MetricRiskTier.STANDARD,
    )
    risk_basis: Mapped[str | None] = mapped_column(Text)
    # JSONB list of required evidence categories, e.g.
    # ["source_data", "methodology", "emission_factor", "supporting_doc"]
    required_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    methodology_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_independent_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_second_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sla_days: Mapped[int | None] = mapped_column(Integer)
    # e.g. ["OWNER", "REVIEWER", "ESG_MANAGER"] — recipients per escalation level
    escalation_policy: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # impact policy: source changes on this metric require downstream re-verification
    impact_reverification_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))


class AssuranceRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Assessor-raised evidence/clarification/correction request. Tracked work
    item; never grants the assessor any write path on official values."""

    __tablename__ = "assurance_request"
    __table_args__ = (
        Index("ix_assurance_request_period", "period_id", "status"),
        Index("ix_assurance_request_assignee", "assignee_id"),
    )

    request_type: Mapped[AssuranceRequestType] = mapped_column(
        Enum(AssuranceRequestType, name="assurance_request_type", native_enum=True),
        nullable=False,
    )
    status: Mapped[AssuranceRequestStatus] = mapped_column(
        Enum(AssuranceRequestStatus, name="assurance_request_status", native_enum=True),
        nullable=False,
        default=AssuranceRequestStatus.OPEN,
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reporting_period.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity.id", ondelete="RESTRICT"), nullable=False
    )
    metric_code: Mapped[str] = mapped_column(String(100), nullable=False)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assignment.id", ondelete="SET NULL")
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    response_text: Mapped[str | None] = mapped_column(Text)
    response_evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.id"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    due_date: Mapped[date | None] = mapped_column(Date)
    comments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImpactEvent(UUIDPrimaryKeyMixin, Base):
    """One row per affected downstream item of a source change. Created by the
    ripple engine from REAL dependency relationships — never guessed."""

    __tablename__ = "impact_event"
    __table_args__ = (
        Index("ix_impact_event_source", "source_entity_id", "source_metric_code", "period_id"),
        Index("ix_impact_event_status", "status"),
    )

    source_assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assignment.id", ondelete="CASCADE"), nullable=False
    )
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity.id", ondelete="CASCADE"), nullable=False
    )
    source_metric_code: Mapped[str] = mapped_column(String(100), nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reporting_period.id", ondelete="CASCADE"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(64))
    new_value: Mapped[str | None] = mapped_column(String(64))
    # derived | consolidation | brsr_disclosure | report_snapshot
    affected_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    affected_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entity.id", ondelete="CASCADE")
    )
    affected_metric_code: Mapped[str | None] = mapped_column(String(100))
    affected_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assignment.id", ondelete="CASCADE")
    )
    affected_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("consolidation_trace.id", ondelete="CASCADE")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ImpactItemStatus] = mapped_column(
        Enum(ImpactItemStatus, name="impact_item_status", native_enum=True),
        nullable=False,
        default=ImpactItemStatus.PENDING,
    )
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EscalationEvent(UUIDPrimaryKeyMixin, Base):
    """Dedup ledger for SLA escalations: one row per (assignment, level) so
    repeated scans never spam. The human-readable trail lives in audit_event."""

    __tablename__ = "escalation_event"
    __table_args__ = (
        UniqueConstraint("assignment_id", "level"),
        Index("ix_escalation_event_assignment", "assignment_id"),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assignment.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    recipients: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
