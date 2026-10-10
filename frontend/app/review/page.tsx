"use client";

import { useCallback, useEffect, useState } from "react";

import { Assignment, listAssignments, reviewAssignment, STATUS_COLORS } from "@/features/collection/collection";
import { RISK_COLORS } from "@/features/governance/governance";
import { api } from "@/lib/api";

interface PolicyLite {
  metric_code: string;
  risk_tier: "STANDARD" | "HIGH" | "CRITICAL";
}

export default function ReviewQueuePage() {
  const [rows, setRows] = useState<Assignment[] | null>(null);
  const [riskByMetric, setRiskByMetric] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [commentFor, setCommentFor] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    return Promise.all([
      listAssignments({ status: "SUBMITTED" }),
      listAssignments({ status: "UNDER_REVIEW" }),
      listAssignments({ status: "PENDING_SECOND_APPROVAL" }),
      api<PolicyLite[]>("/api/v1/control-policies").catch(() => []),
    ])
      .then(([submitted, underReview, secondApproval, policies]) => {
        setRows([...submitted, ...underReview, ...secondApproval]);
        setRiskByMetric(
          Object.fromEntries(policies.map((p) => [p.metric_code, p.risk_tier])),
        );
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load review queue"));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  async function act(id: string, action: string, withComment?: string) {
    setBusy(true);
    setError(null);
    try {
      await reviewAssignment(id, action, withComment);
      setCommentFor(null);
      setComment("");
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  if (error && rows === null)
    return <main className="page"><div className="state error">Error: {error}</div></main>;
  if (rows === null) return <main className="page"><div className="state">Loading review queue…</div></main>;

  return (
    <main className="page">
      <h1>Review Queue</h1>
      <p className="hint">
        {rows.length} submissions awaiting review in your scope. Approval is blocked while
        unresolved BLOCKING validation exceptions exist.
      </p>
      {error && <div className="auth-error">{error}</div>}
      {rows.length === 0 ? (
        <div className="state">Queue is empty — nothing to review.</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>Metric</th><th>Risk</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const risk = riskByMetric[a.metric_code] ?? "STANDARD";
              const riskColor = RISK_COLORS[risk] ?? "#94a3b8";
              return (
              <tr key={a.id}>
                <td className="mono">
                  {a.metric_code}{" "}
                  <a className="linkbtn" href={`/assignments/${a.id}`}>view</a>{" "}
                  <a className="linkbtn" href={`/passport/${a.id}`}>passport</a>
                </td>
                <td>
                  <span className="status-badge" style={{ borderColor: riskColor, color: riskColor }}>
                    {risk}
                  </span>
                </td>
                <td>
                  <span
                    className="status-badge"
                    style={{ borderColor: STATUS_COLORS[a.status], color: STATUS_COLORS[a.status] }}
                  >
                    {a.status.replace(/_/g, " ")}
                  </span>
                </td>
                <td>
                  {a.status === "SUBMITTED" && (
                    <button className="ghostbtn" disabled={busy} onClick={() => act(a.id, "START_REVIEW")}>
                      Open review
                    </button>
                  )}
                  {a.status === "UNDER_REVIEW" && (
                    <>
                      <button className="primarybtn" disabled={busy} onClick={() => act(a.id, "APPROVE")}>
                        Approve
                      </button>{" "}
                      <button
                        className="ghostbtn"
                        disabled={busy}
                        onClick={() => setCommentFor(commentFor === a.id ? null : a.id)}
                      >
                        Needs correction / Reject
                      </button>
                    </>
                  )}
                  {a.status === "PENDING_SECOND_APPROVAL" && (
                    <button className="primarybtn" disabled={busy} onClick={() => act(a.id, "SECOND_APPROVAL")}>
                      Grant second approval
                    </button>
                  )}
                  {commentFor === a.id && (
                    <div className="comment-box">
                      <textarea
                        rows={2}
                        placeholder="Review comment (required)"
                        value={comment}
                        onChange={(e) => setComment(e.target.value)}
                      />
                      <button
                        className="ghostbtn"
                        disabled={busy || !comment.trim()}
                        onClick={() => act(a.id, "NEEDS_CORRECTION", comment)}
                      >
                        Needs correction
                      </button>{" "}
                      <button
                        className="ghostbtn danger"
                        disabled={busy || !comment.trim()}
                        onClick={() => act(a.id, "REJECT", comment)}
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}
