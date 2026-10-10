// Governance & assurance API client (control passport, impact, policies,
// assurance room, control tower, remediation).
import { api } from "@/lib/api";

export interface ControlPolicy {
  id: string | null;
  framework_version_id: string;
  metric_code: string;
  risk_tier: "STANDARD" | "HIGH" | "CRITICAL";
  risk_basis: string | null;
  required_evidence: string[];
  methodology_required: boolean;
  requires_independent_review: boolean;
  requires_second_approval: boolean;
  sla_days: number | null;
  escalation_policy: string[];
  impact_reverification_required: boolean;
  brsr_core: boolean;
  metric_label: string | null;
}

export interface PassportValueHistory {
  id: string;
  version: number;
  raw_value: number | null;
  status: string;
  is_calculated: boolean;
}

export interface ControlPassport {
  assignment_id: string;
  metric_code: string;
  metric_label: string;
  entity_id: string;
  entity_name: string;
  period_id: string;
  period_label: string;
  status: string;
  risk: { tier: "STANDARD" | "HIGH" | "CRITICAL"; basis: string | null; brsr_core: boolean };
  controls: {
    required_evidence: string[];
    methodology_required: boolean;
    requires_independent_review: boolean;
    requires_second_approval: boolean;
    sla_days: number | null;
    escalation_policy: string[];
  };
  owner: string;
  reviewer: string | null;
  second_approver: string | null;
  value: {
    version: number;
    raw_value: number | null;
    raw_unit: string | null;
    normalized_value: number | null;
    normalized_unit: string | null;
    status: string;
    is_calculated: boolean;
    methodology_version: string | null;
    methodology_notes: string | null;
    assumptions: Record<string, unknown> | null;
    history: PassportValueHistory[];
  } | null;
  formula: {
    code: string;
    name: string;
    version: number;
    expression: string;
    input_metric_codes: string[];
    constants: Record<string, unknown>;
    resolved_inputs: Record<string, string>;
  } | null;
  evidence: { required: string[]; present: string[]; missing: string[]; count: number };
  validation: { total: number; blocking: number; open: number; investigating: number; explained: number };
  review: {
    reviewed_by: string | null;
    reviewed_at: string | null;
    second_approved_by: string | null;
    second_approved_at: string | null;
    second_approval_required: boolean;
  };
  lineage: {
    kind: string;
    is_stale: boolean;
    stale_reason: string | null;
    contributors: number;
    upstream: Record<string, string[]>;
    downstream: Record<string, string[]>;
  };
  audit_count: number;
  impact: { events: number; derived: number; consolidations: number; brsr: number };
  blocking: { code: string; detail: string; next_action: string }[];
}

export interface ImpactEventRow {
  id: string;
  affected_kind: string;
  affected_entity_id: string | null;
  affected_metric_code: string | null;
  reason: string;
  status: string;
  created_at: string;
}

export interface ImpactSummary {
  source_metric_code: string;
  levels: Record<string, string[]>;
  events: ImpactEventRow[];
  summary: { derived: number; consolidations: number; brsr: number };
}

export interface AssuranceRequestRow {
  id: string;
  request_type: "EVIDENCE" | "CLARIFICATION" | "CORRECTION";
  status: string;
  entity_id: string;
  entity_name: string | null;
  metric_code: string;
  assignment_id: string | null;
  requested_by: string;
  assignee_id: string | null;
  reason: string;
  response_text: string | null;
  response_evidence_id: string | null;
  due_date: string | null;
  comments: { actor: string; comment: string | null; at: string }[];
  created_at: string;
  resolved_at: string | null;
}

export interface ControlTowerSummary {
  period_id: string;
  period_label: string;
  period_locked: boolean;
  critical_metrics: { total: number; ready: number; blocked: number; pending_review: number };
  high_risk: {
    open_blocking_exceptions: number;
    missing_evidence: number;
    stale_metrics: number;
    overdue_owners: number;
    reviewer_backlog: number;
    open_assurance_requests: number;
  };
  funnel: Record<string, number>;
  bottleneck: { stage: string | null; count: number; detail: string | null };
  deadlines: {
    next_critical_deadline: string | null;
    overdue_count: number;
    overdue: {
      assignment_id: string;
      metric_code: string;
      entity_name: string;
      due_date: string;
      status: string;
    }[];
  };
  core_readiness: {
    total: number;
    approved: number;
    ready: boolean;
    label: string;
    blockers: { missing_approval: number; blocking_exceptions: number; stale_traces: number };
  };
}

export const RISK_COLORS: Record<string, string> = {
  STANDARD: "#38bdf8",
  HIGH: "#fbbf24",
  CRITICAL: "#f87171",
};

export const ASSURANCE_STATUS_COLORS: Record<string, string> = {
  OPEN: "#f87171",
  ASSIGNED: "#a78bfa",
  IN_PROGRESS: "#38bdf8",
  RESPONDED: "#fbbf24",
  UNDER_REVIEW: "#f59e0b",
  RESOLVED: "#10b981",
  REJECTED: "#ef4444",
};

export const getControlPassport = (assignmentId: string) =>
  api<ControlPassport>(`/api/v1/assignments/${assignmentId}/control-passport`);

export const getImpact = (assignmentId: string) =>
  api<ImpactSummary>(`/api/v1/assignments/${assignmentId}/impact`);

export const getDependencies = (assignmentId: string, direction: "upstream" | "downstream") =>
  api<{ metric_code: string; levels: Record<string, string[]> }>(
    `/api/v1/assignments/${assignmentId}/dependencies/${direction}`,
  );

export const reopenAssignment = (id: string, reason: string) =>
  api<{ id: string; status: string }>(`/api/v1/assignments/${id}/reopen`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

export const reassignAssignment = (id: string, newOwnerUserId: string, reason: string) =>
  api<{ id: string; status: string }>(`/api/v1/assignments/${id}/reassign`, {
    method: "POST",
    body: JSON.stringify({ new_owner_user_id: newOwnerUserId, reason }),
  });

export const getControlTower = () => api<ControlTowerSummary>("/api/v1/dashboard/control-tower");

export const listAssuranceRequests = (params: Record<string, string> = {}) => {
  const qs = new URLSearchParams(params).toString();
  return api<AssuranceRequestRow[]>(`/api/v1/assurance/requests${qs ? `?${qs}` : ""}`);
};

export const createAssuranceRequest = (body: {
  request_type: string;
  entity_id: string;
  metric_code: string;
  period_id: string;
  reason: string;
  assignment_id?: string;
}) =>
  api<AssuranceRequestRow>("/api/v1/assurance/requests", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const assuranceAction = (
  id: string,
  body: {
    action: string;
    comment?: string;
    response_text?: string;
    assignee_id?: string;
  },
) =>
  api<AssuranceRequestRow>(`/api/v1/assurance/requests/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const remediationAction = (
  exceptionId: string,
  body: {
    action: string;
    comment?: string;
    assignee_id?: string;
    due_date?: string;
    action_required?: string;
  },
) =>
  api<{ id: string; status: string }>(
    `/api/v1/validation/exceptions/${exceptionId}/actions`,
    { method: "POST", body: JSON.stringify(body) },
  );

export const uploadEvidenceWithCategory = (
  metricValueId: string,
  file: File,
  category?: string,
) => {
  const form = new FormData();
  form.append("file", file);
  form.append("metric_value_id", metricValueId);
  if (category) form.append("category", category);
  return api<{ id: string }>("/api/v1/evidence", { method: "POST", body: form });
};
