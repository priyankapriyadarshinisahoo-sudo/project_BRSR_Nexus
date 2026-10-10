"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import { StyledSelect } from "@/components/StyledSelect";
import {
  EXCEPTION_STATUS_COLORS,
  explainException,
  listExceptions,
  remediationAction,
  resolveException,
  runValidation,
  SEVERITY_COLORS,
  ValidationException,
} from "@/features/validation/validation";

export default function ExceptionsPage() {
  const { user } = useAuth();
  const [rows, setRows] = useState<ValidationException[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("OPEN");
  const [explainFor, setExplainFor] = useState<string | null>(null);
  const [explanation, setExplanation] = useState("");
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const canRun = user?.role === "ADMIN" || user?.role === "ESG_MANAGER" || user?.role === "REVIEWER";
  const canResolve = canRun;
  const canExplain = user?.role === "DATA_OWNER" || canRun;

  const reload = useCallback(() => {
    return listExceptions(statusFilter ? { status: statusFilter } : {})
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load exceptions"));
  }, [statusFilter]);

  useEffect(() => {
    reload();
  }, [reload]);

  async function onRun() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // Run against the unlocked (active) reporting period; the dashboard
      // phase will add a proper period selector.
      const { listPeriods } = await import("@/features/reporting/reporting");
      const periods = await listPeriods();
      const active = periods.find((p) => !p.locked) ?? periods[0];
      const result = await runValidation(active.id);
      setNotice(
        `Validation completed: ${result.assignments} assignments checked, ` +
          `${result.raised} exceptions raised, ${result.auto_resolved} auto-resolved, ` +
          `${result.blocking} blocking findings.`
      );
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Validation run failed");
    } finally {
      setBusy(false);
    }
  }

  async function onExplain(id: string) {
    setBusy(true);
    setError(null);
    try {
      await explainException(id, explanation);
      setExplainFor(null);
      setExplanation("");
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Explain failed");
    } finally {
      setBusy(false);
    }
  }

  async function onResolve(id: string) {
    setBusy(true);
    setError(null);
    try {
      await resolveException(id);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Resolve failed");
    } finally {
      setBusy(false);
    }
  }

  async function onRemediate(id: string, action: string, comment?: string) {
    setBusy(true);
    setError(null);
    try {
      await remediationAction(id, { action, comment });
      setNotice(`Remediation action applied: ${action.toLowerCase().replace("_", " ")}.`);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remediation action failed");
    } finally {
      setBusy(false);
    }
  }

  if (rows === null && !error)
    return <main className="page"><div className="state">Loading exceptions…</div></main>;

  return (
    <main className="page">
      <h1>Validation Exceptions</h1>
      <p className="hint">
        BLOCKING exceptions prevent approval. WARNING exceptions require an explanation. Re-running
        validation auto-resolves OPEN exceptions whose condition cleared.
      </p>
      {error && <div className="auth-error">{error}</div>}
      {notice && <div className="form-success">{notice}</div>}

      <div className="filter-row">
        <StyledSelect
          ariaLabel="Exception status filter"
          value={statusFilter}
          options={[
            { value: "", label: "All statuses" },
            { value: "OPEN", label: "Open" },
            { value: "INVESTIGATING", label: "Investigating" },
            { value: "EXPLAINED", label: "Explained" },
            { value: "RESOLVED", label: "Resolved" },
          ]}
          onChange={setStatusFilter}
          placeholder="All statuses"
        />
        {canRun && (
          <button className="primarybtn" disabled={busy} onClick={onRun}>
            {busy ? "Running…" : "Run validation"}
          </button>
        )}
      </div>

      {rows !== null && rows.length === 0 ? (
        <div className="state">No exceptions match the current filter.</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Severity</th><th>Rule</th><th>Message</th><th>Status</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((e) => (
              <tr key={e.id}>
                <td>
                  <span className="sev" style={{
                    background: "transparent",
                    color: SEVERITY_COLORS[e.severity],
                    border: `1px solid ${SEVERITY_COLORS[e.severity]}`,
                  }}>
                    {e.severity}
                  </span>
                </td>
                <td className="mono">{e.rule_code}</td>
                <td>
                  {e.message}
                  {e.explanation && (
                    <div className="explanation">Explanation: {e.explanation}</div>
                  )}
                  {e.action_required && (
                    <div className="explanation">Required action: {e.action_required}</div>
                  )}
                  {e.category && (
                    <span className="badge warn">{e.category.replace(/_/g, " ")}</span>
                  )}{" "}
                  {e.assigned_to && <span className="badge">assigned</span>}{" "}
                  {!!e.escalation_level && (
                    <span className="badge error">escalated L{e.escalation_level}</span>
                  )}
                  {e.due_date && <div className="hint">Due {String(e.due_date).slice(0, 10)}</div>}
                  {e.comments && e.comments.length > 0 && (
                    <div className="hint">
                      <span
                        className="linkbtn"
                        style={{ cursor: "pointer" }}
                        onClick={() => setExpanded(expanded === e.id ? null : e.id)}
                      >
                        {e.comments.length} remediation entr{e.comments.length === 1 ? "y" : "ies"}
                      </span>
                      {expanded === e.id && (
                        <div className="explanation">
                          {e.comments.map((c, i) => (
                            <div key={i}>
                              {new Date(c.at).toLocaleString()} · {c.actor} · {c.action}
                              {c.comment ? `: ${c.comment}` : ""}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                  {e.assignment_id && (
                    <a className="linkbtn" href={`/passport/${e.assignment_id}`}>
                      Control Passport →
                    </a>
                  )}
                </td>
                <td>
                  <span
                    className="status-badge"
                    style={{
                      borderColor: EXCEPTION_STATUS_COLORS[e.status],
                      color: EXCEPTION_STATUS_COLORS[e.status],
                    }}
                  >
                    {e.status}
                  </span>
                </td>
                <td>
                  {canExplain && ["OPEN", "INVESTIGATING"].includes(e.status) && (
                    <>
                      {e.status === "OPEN" && (
                        <button
                          className="ghostbtn"
                          disabled={busy}
                          onClick={() => onRemediate(e.id, "START")}
                        >
                          Investigate
                        </button>{" "}
                      )}
                      <button
                        className="ghostbtn"
                        onClick={() => setExplainFor(explainFor === e.id ? null : e.id)}
                      >
                        Explain
                      </button>{" "}
                    </>
                  )}
                  {canResolve && e.status !== "RESOLVED" && (
                    <>
                      <button className="ghostbtn" disabled={busy} onClick={() => onResolve(e.id)}>
                        Resolve
                      </button>{" "}
                      {e.status === "OPEN" && (
                        <button
                          className="ghostbtn danger"
                          disabled={busy}
                          onClick={() => onRemediate(e.id, "ESCALATE", "Escalated from exceptions screen")}
                        >
                          Escalate
                        </button>
                      )}
                    </>
                  )}
                  {explainFor === e.id && (
                    <div className="comment-box">
                      <textarea
                        rows={2}
                        placeholder="Explain this exception (required)"
                        value={explanation}
                        onChange={(ev) => setExplanation(ev.target.value)}
                      />
                      <button
                        className="primarybtn"
                        disabled={busy || explanation.trim().length < 5}
                        onClick={() => onExplain(e.id)}
                      >
                        Submit explanation
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
