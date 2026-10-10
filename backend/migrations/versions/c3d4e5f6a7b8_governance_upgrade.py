"""ESG trust & governance upgrade: control policies, assurance requests,
impact events, escalation dedup, remediation fields, methodology snapshot.

Additive only — no existing column is dropped or retyped.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- new native enum types ----
    op.execute("CREATE TYPE metric_risk_tier AS ENUM ('STANDARD', 'HIGH', 'CRITICAL')")
    op.execute(
        "CREATE TYPE assurance_request_type AS ENUM ('EVIDENCE', 'CLARIFICATION', 'CORRECTION')"
    )
    op.execute(
        "CREATE TYPE assurance_request_status AS ENUM ('OPEN', 'ASSIGNED', 'IN_PROGRESS', "
        "'RESPONDED', 'UNDER_REVIEW', 'RESOLVED', 'REJECTED')"
    )
    op.execute("CREATE TYPE impact_item_status AS ENUM ('PENDING', 'RECOMPUTED', 'REVERIFIED')")

    # ---- new enum values on existing types (PG12+ allows inside txn) ----
    op.execute("ALTER TYPE assignment_status ADD VALUE IF NOT EXISTS 'PENDING_SECOND_APPROVAL'")
    op.execute("ALTER TYPE validation_exception_status ADD VALUE IF NOT EXISTS 'INVESTIGATING'")
    op.execute("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'IMPACT_DETECTED'")
    op.execute("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'ESCALATION'")
    op.execute("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'ASSURANCE_REQUEST'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'REOPENED'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'SECOND_APPROVAL_GRANTED'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'REASSIGNED'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'ESCALATED'")

    # ---- metric_control_policy ----
    op.create_table(
        'metric_control_policy',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('framework_version_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('framework_version.id', ondelete='CASCADE'), nullable=False),
        sa.Column('metric_code', sa.String(100), nullable=False),
        sa.Column('risk_tier', sa.Enum(name='metric_risk_tier', create_type=False), nullable=False),
        sa.Column('risk_basis', sa.Text()),
        sa.Column('required_evidence', pg.JSONB(), nullable=False, server_default='[]'),
        sa.Column('methodology_required', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('requires_independent_review', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('requires_second_approval', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sla_days', sa.Integer()),
        sa.Column('escalation_policy', pg.JSONB(), nullable=False, server_default='[]'),
        sa.Column('impact_reverification_required', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('updated_by', pg.UUID(as_uuid=True), sa.ForeignKey('app_user.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('framework_version_id', 'metric_code', name='uq_control_policy_metric'),
    )
    op.create_index('ix_control_policy_risk', 'metric_control_policy',
                    ['framework_version_id', 'risk_tier'])

    # ---- assurance_request ----
    op.create_table(
        'assurance_request',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('request_type', sa.Enum(name='assurance_request_type', create_type=False), nullable=False),
        sa.Column('status', sa.Enum(name='assurance_request_status', create_type=False), nullable=False),
        sa.Column('period_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('reporting_period.id', ondelete='CASCADE'), nullable=False),
        sa.Column('entity_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('entity.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('metric_code', sa.String(100), nullable=False),
        sa.Column('assignment_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('assignment.id', ondelete='SET NULL')),
        sa.Column('requested_by', pg.UUID(as_uuid=True),
                  sa.ForeignKey('app_user.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('assignee_id', pg.UUID(as_uuid=True), sa.ForeignKey('app_user.id')),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('response_text', sa.Text()),
        sa.Column('response_evidence_id', pg.UUID(as_uuid=True), sa.ForeignKey('evidence.id')),
        sa.Column('reviewed_by', pg.UUID(as_uuid=True), sa.ForeignKey('app_user.id')),
        sa.Column('due_date', sa.Date()),
        sa.Column('comments', pg.JSONB(), nullable=False, server_default='[]'),
        sa.Column('resolved_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_assurance_request_period', 'assurance_request', ['period_id', 'status'])
    op.create_index('ix_assurance_request_assignee', 'assurance_request', ['assignee_id'])

    # ---- impact_event ----
    op.create_table(
        'impact_event',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('source_assignment_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('assignment.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_entity_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('entity.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_metric_code', sa.String(100), nullable=False),
        sa.Column('period_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('reporting_period.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_version', sa.Integer(), nullable=False),
        sa.Column('old_value', sa.String(64)),
        sa.Column('new_value', sa.String(64)),
        sa.Column('affected_kind', sa.String(30), nullable=False),
        sa.Column('affected_entity_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('entity.id', ondelete='CASCADE')),
        sa.Column('affected_metric_code', sa.String(100)),
        sa.Column('affected_assignment_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('assignment.id', ondelete='CASCADE')),
        sa.Column('affected_trace_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('consolidation_trace.id', ondelete='CASCADE')),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.Enum(name='impact_item_status', create_type=False), nullable=False),
        sa.Column('notified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_by', pg.UUID(as_uuid=True), sa.ForeignKey('app_user.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_impact_event_source', 'impact_event',
                    ['source_entity_id', 'source_metric_code', 'period_id'])
    op.create_index('ix_impact_event_status', 'impact_event', ['status'])

    # ---- escalation_event (dedup ledger) ----
    op.create_table(
        'escalation_event',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('assignment_id', pg.UUID(as_uuid=True),
                  sa.ForeignKey('assignment.id', ondelete='CASCADE'), nullable=False),
        sa.Column('level', sa.Integer(), nullable=False),
        sa.Column('risk_tier', sa.String(20), nullable=False),
        sa.Column('recipients', pg.JSONB(), nullable=False, server_default='[]'),
        sa.Column('fired_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('assignment_id', 'level', name='uq_escalation_assignment_level'),
    )
    op.create_index('ix_escalation_event_assignment', 'escalation_event', ['assignment_id'])

    # ---- new columns on existing tables ----
    op.add_column('assignment', sa.Column('reviewer_user_id', pg.UUID(as_uuid=True),
                                          sa.ForeignKey('app_user.id')))
    op.add_column('assignment', sa.Column('second_approver_user_id', pg.UUID(as_uuid=True),
                                          sa.ForeignKey('app_user.id')))
    op.add_column('assignment', sa.Column('reviewed_by_user_id', pg.UUID(as_uuid=True),
                                          sa.ForeignKey('app_user.id')))
    op.add_column('assignment', sa.Column('reviewed_at', sa.DateTime(timezone=True)))
    op.add_column('assignment', sa.Column('second_approved_by_user_id', pg.UUID(as_uuid=True),
                                          sa.ForeignKey('app_user.id')))
    op.add_column('assignment', sa.Column('second_approved_at', sa.DateTime(timezone=True)))

    op.add_column('metric_value', sa.Column('methodology_version', sa.String(50)))
    op.add_column('metric_value', sa.Column('methodology_notes', sa.Text()))
    op.add_column('metric_value', sa.Column('assumptions', pg.JSONB()))

    op.add_column('validation_exception', sa.Column('category', sa.String(50)))
    op.add_column('validation_exception', sa.Column('assigned_to', pg.UUID(as_uuid=True),
                                                    sa.ForeignKey('app_user.id')))
    op.add_column('validation_exception', sa.Column('action_required', sa.Text()))
    op.add_column('validation_exception', sa.Column('due_date', sa.DateTime(timezone=True)))
    op.add_column('validation_exception', sa.Column('escalation_level', sa.Integer(),
                                                    nullable=False, server_default='0'))
    op.add_column('validation_exception', sa.Column('comments', pg.JSONB(),
                                                    nullable=False, server_default='[]'))

    op.add_column('evidence', sa.Column('category', sa.String(50)))


def downgrade() -> None:
    op.drop_column('evidence', 'category')
    op.drop_column('validation_exception', 'comments')
    op.drop_column('validation_exception', 'escalation_level')
    op.drop_column('validation_exception', 'due_date')
    op.drop_column('validation_exception', 'action_required')
    op.drop_column('validation_exception', 'assigned_to')
    op.drop_column('validation_exception', 'category')
    op.drop_column('metric_value', 'assumptions')
    op.drop_column('metric_value', 'methodology_notes')
    op.drop_column('metric_value', 'methodology_version')
    op.drop_column('assignment', 'second_approved_at')
    op.drop_column('assignment', 'second_approved_by_user_id')
    op.drop_column('assignment', 'reviewed_at')
    op.drop_column('assignment', 'reviewed_by_user_id')
    op.drop_column('assignment', 'second_approver_user_id')
    op.drop_column('assignment', 'reviewer_user_id')
    op.drop_index('ix_escalation_event_assignment', 'escalation_event')
    op.drop_table('escalation_event')
    op.drop_index('ix_impact_event_status', 'impact_event')
    op.drop_index('ix_impact_event_source', 'impact_event')
    op.drop_table('impact_event')
    op.drop_index('ix_assurance_request_assignee', 'assurance_request')
    op.drop_index('ix_assurance_request_period', 'assurance_request')
    op.drop_table('assurance_request')
    op.drop_index('ix_control_policy_risk', 'metric_control_policy')
    op.drop_table('metric_control_policy')
    # enum value removal is not supported by PostgreSQL; leave in place
