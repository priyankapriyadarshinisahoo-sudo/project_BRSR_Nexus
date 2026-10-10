"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import { StyledSelect } from "@/components/StyledSelect";
import {
  assuranceAction,
  ASSURANCE_STATUS_COLORS,
  createAssuranceRequest,
  listAssuranceRequests,
  type AssuranceRequestRow,
} from "@/features/governance/governance";
import { api } from "@/lib/api";

const STATUS_OPTIONS = [
  { value: "OPEN", label: "Open" },
  { value: "ASSIGNED", label: "Assigned" },
  { value: "IN_PROGRESS", label: "In progress" },
  { value: "RESPONDED", label: "Responded" },
  { value: "UNDER_REVIEW", label: "Under review" },
  { value: "RESOLVED", label: "Resolved" },
  { value: "REJECTED", label: "Rejected" },
];

const TYPE_OPTIONS = [
  { value: "EVIDENCE", label: "Evidence request" },
  { value: "CLARIFICATION", label: "Clarification" },
  { value: "CORRECTION", label: "Correction" },
];

interface Option {
  value: string;
  label: string;
}

export default function AssurancePage() {
  const { user, loading } = useAuth();
  const [rows, setRows] = useState<AssuranceRequestRow[] | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [response, setResponse] = useState<Record<string, string>>({});
  const [showForm, setShowForm] = useState(false);
  const [entities, setEntities] = useState<Option[]>([]);
  const [periods, setPeriods] = useState<Option[]>([]);
  const [form, setForm] = useState({
    request_type: "EVIDENCE",
    entity_id: "",
    metric_code: "",
    period_id: "",
    reason: "",
  });

  const reload = useCallback(() => {
    listAssuranceRequests(filter ? { status: filter } : {})
      .then(setRows)
      .catch((e) => setError(String(e.message || e)));
  }, [filter]);

  useEffect(() => {
    if (!loading && user) reload();
  }, [loading, user, reload]);

  useEffect(() => {
    if (!user || user.role !== "ASSESSOR") return;
    Promise.all([
      api<{ id: string; name: string }[]>("/api/v1/entities"),
      api<{ id: string; label: string }[]>("/api/v1/reporting-periods"),
    ])
      .then(([e, p]) => {
        setEntities(e.map((x) => ({ value: x.id, label: x.name })));
        setPeriods(p.map((x) => ({ value: x.id, label: x.label })));
      })
      .catch(() => undefined);
  }, [user]);

  if (loading) return <main className="page"><div className="state">Loading …</div></main>;
  if (!user) return null;
  if (error)
    return (
      <main className="page">
        <div className="state error">Error: {error}</div>
      </main>
    );

  const isAssessor = user.role === "ASSESSOR" || user.role === "ADMIN";
  const canRespond = (r: AssuranceRequestRow) =>
    user.role === "ADMIN" || (r.assignee_id && r.assignee_id === user.id);
  const canReview = (r: AssuranceRequestRow) =>
    user.role === "ADMIN" ||
    user.role === "REVIEWER" ||
    user.role === "ESG_MANAGER" ||
    r.requested_by === user.id;

  const act = (r: AssuranceRequestRow, action: string, extra: Record<string, unknown> = {}) => {
    assuranceAction(r.id, { action, ...extra })
      .then(() => {
        setNotice(`Request ${action.toLowerCase()} applied.`);
        reload();
      })
      .catch((e) => setError(String(e.message || e)));
  };

  const submit = () => {
    createAssuranceRequest({
      request_type: form.request_type,
      entity_id: form.entity_id,
      metric_code: form.metric_code,
      period_id: form.period_id,
      reason: form.reason,
    })
      .then(() => {
        setNotice("Assurance request created.");
        setShowForm(false);
        setForm({ request_type: "EVIDENCE", entity_id: "", metric_code: "", period_id: "", reason: "" });
        reload();
      })
      .catch((e) => setError(String(e.message || e)));
  };

  return (
    <main className="page">
      <div className="detail-header rise rise-d1">
        <div>
          <h1>Assurance Room</h1>
          <p className="hint">
            Assessor workspace: request evidence, clarifications and corrections as tracked work
            items. Assessors can inspect scoped BRSR Core data but never modify official values.
          </p>
        </div>
        {isAssessor && (
          <button className="primarybtn" onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Close" : "New request"}
          </button>
        )}
      </div>

      {notice && <div className="form-success">{notice}</div>}

      {showForm && (
        <div className="glass-card rise">
          <h3>Raise assurance request</h3>
          <div className="form-grid">
            <StyledSelect
              value={form.request_type}
              options={TYPE_OPTIONS}
              onChange={(v) => setForm({ ...form, request_type: v })}
              ariaLabel="Request type"
            />
            <StyledSelect
              value={form.entity_id}
              options={entities}
              onChange={(v) => setForm({ ...form, entity_id: v })}
              placeholder="Entity…"
              ariaLabel="Entity"
            />
            <StyledSelect
              value={form.period_id}
              options={periods}
              onChange={(v) => setForm({ ...form, period_id: v })}
              placeholder="Period…"
              ariaLabel="Period"
            />
            <input
              className="text-input"
              placeholder="Metric code (e.g. C-P6-SCOPE1-TCO2E)"
              value={form.metric_code}
              onChange={(e) => setForm({ ...form, metric_code: e.target.value })}
            />
          </div>
          <textarea
            className="comment-box"
            placeholder="Reason — e.g. 'Provide supporting evidence for this KPI'"
            value={form.reason}
            onChange={(e) => setForm({ ...form, reason: e.target.value })}
          />
          <button className="primarybtn" disabled={!form.entity_id || !form.metric_code || !form.period_id || !form.reason} onClick={submit}>
            Create request
          </button>
        </div>
      )}

      <div className="filter-row rise rise-d2">
        <StyledSelect
          value={filter}
          options={[{ value: "", label: "All statuses" }, ...STATUS_OPTIONS]}
          onChange={setFilter}
          ariaLabel="Status filter"
        />
      </div>

      {!rows ? (
        <div className="state">Loading …</div>
      ) : rows.length === 0 ? (
        <div className="state">No assurance requests.</div>
      ) : (
        <table className="data-table rise rise-d2">
          <thead>
            <tr>
              <th>Type</th>
              <th>Metric</th>
              <th>Entity</th>
              <th>Status</th>
              <th>Reason / response</th>
              <th>Due</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>
                  <span className="badge evidence">{r.request_type}</span>
                </td>
                <td className="mono">{r.metric_code}</td>
                <td>{r.entity_name ?? "—"}</td>
                <td>
                  <span
                    className="status-badge"
                    style={{
                      borderColor: ASSURANCE_STATUS_COLORS[r.status],
                      color: ASSURANCE_STATUS_COLORS[r.status],
                    }}
                  >
                    {r.status.replace(/_/g, " ")}
                  </span>
                </td>
                <td>
                  <div>{r.reason}</div>
                  {r.response_text && (
                    <div className="explanation">Response: {r.response_text}</div>
                  )}
                  {r.comments.length > 0 && (
                    <div className="hint">{r.comments.length} comment(s)</div>
                  )}
                </td>
                <td>{r.due_date ?? "—"}</td>
                <td>
                  {canRespond(r) && ["ASSIGNED", "OPEN", "IN_PROGRESS"].includes(r.status) && (
                    <div className="actionrow">
                      <button className="ghostbtn" onClick={() => act(r, "START")}>
                        Start
                      </button>
                      <textarea
                        className="comment-box"
                        placeholder="Response with evidence reference…"
                        value={response[r.id] ?? ""}
                        onChange={(e) => setResponse({ ...response, [r.id]: e.target.value })}
                      />
                      <button
                        className="ghostbtn"
                        disabled={(response[r.id] ?? "").trim().length < 5}
                        onClick={() => act(r, "RESPOND", { response_text: response[r.id] })}
                      >
                        Respond
                      </button>
                    </div>
                  )}
                  {canReview(r) && r.status === "RESPONDED" && (
                    <div className="actionrow">
                      <button className="ghostbtn" onClick={() => act(r, "UNDER_REVIEW")}>
                        Take under review
                      </button>
                      <button className="primarybtn" onClick={() => act(r, "RESOLVE")}>
                        Resolve
                      </button>
                    </div>
                  )}
                  {canReview(r) && r.status === "UNDER_REVIEW" && (
                    <div className="actionrow">
                      <button className="primarybtn" onClick={() => act(r, "RESOLVE")}>
                        Resolve
                      </button>
                      <button
                        className="ghostbtn danger"
                        onClick={() => act(r, "REJECT", { comment: "Response insufficient" })}
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
