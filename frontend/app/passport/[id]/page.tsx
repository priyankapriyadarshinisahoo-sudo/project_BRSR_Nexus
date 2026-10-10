"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";

import { useAuth } from "@/components/AuthProvider";
import { GovernanceStepper } from "@/components/GovernanceStepper";
import {
  getControlPassport,
  getImpact,
  RISK_COLORS,
  type ControlPassport,
  type ImpactSummary,
} from "@/features/governance/governance";
import { STATUS_COLORS } from "@/features/collection/collection";

const RISK_LABEL: Record<string, string> = {
  STANDARD: "STANDARD",
  HIGH: "HIGH",
  CRITICAL: "CRITICAL",
};

function ControlChip({ label, ok, pending }: { label: string; ok: boolean; pending?: boolean }) {
  const color = ok ? "#10b981" : pending ? "#fbbf24" : "#ef4444";
  const mark = ok ? "✓" : pending ? "…" : "✗";
  return (
    <span className="badge" style={{ borderColor: color, color }}>
      {mark} {label}
    </span>
  );
}

function DepLevels({ levels }: { levels: Record<string, string[]> }) {
  const entries = Object.entries(levels);
  if (!entries.length) return <span className="hint">No metric-level dependencies.</span>;
  return (
    <div className="dep-flow">
      {entries.map(([level, codes], idx) => (
        <div key={level} className="dep-level">
          {idx > 0 && <span className="dep-arrow">→</span>}
          <div>
            <div className="hint">Level {level}</div>
            {codes.map((c) => (
              <div key={c} className="mono dep-code">
                {c}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function PassportPage() {
  const params = useParams<{ id: string }>();
  const { user, loading } = useAuth();
  const [passport, setPassport] = useState<ControlPassport | null>(null);
  const [impact, setImpact] = useState<ImpactSummary | null>(null);
  const [tab, setTab] = useState<string>("overview");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!params?.id) return;
    getControlPassport(params.id)
      .then(setPassport)
      .catch((e) => setError(String(e.message || e)));
    getImpact(params.id)
      .then(setImpact)
      .catch(() => setImpact(null));
  }, [params?.id]);

  useEffect(() => {
    if (!loading && user) load();
  }, [loading, user, load]);

  if (loading) return <main className="page"><div className="state">Loading …</div></main>;
  if (!user) return null;
  if (error) return <main className="page"><div className="state error">Error: {error}</div></main>;
  if (!passport) return <main className="page"><div className="state">Loading …</div></main>;

  const riskColor = RISK_COLORS[passport.risk.tier] ?? "#94a3b8";
  const tabs = [
    ["overview", "Overview"],
    ["controls", "Risk & Controls"],
    ["methodology", "Methodology"],
    ["validation", "Validation"],
    ["evidence", "Evidence"],
    ["review", "Review & Approval"],
    ["lineage", "Lineage & Impact"],
  ];

  return (
    <main className="page">
      <div className="detail-header rise rise-d1">
        <div>
          <h1>Control Passport — {passport.metric_label}</h1>
          <p className="hint">
            <span className="mono">{passport.metric_code}</span> · {passport.entity_name} ·{" "}
            {passport.period_label} · governed work item with owner, authority, evidence and
            dependency impact
          </p>
        </div>
        <div className="passport-headbadges">
          <span className="status-badge" style={{ borderColor: riskColor, color: riskColor }}>
            RISK: {RISK_LABEL[passport.risk.tier]}
          </span>
          {passport.risk.brsr_core && <span className="badge core">BRSR Core</span>}
          <span
            className="status-badge"
            style={{ borderColor: STATUS_COLORS[passport.status], color: STATUS_COLORS[passport.status] }}
          >
            {passport.status.replace(/_/g, " ")}
          </span>
        </div>
      </div>

      <GovernanceStepper status={passport.status} />

      {passport.blocking.length > 0 && (
        <div className="glass-card blocking-card rise">
          <strong>BLOCKED</strong>
          {passport.blocking.map((b) => (
            <div key={b.code} className="blocking-row">
              <span className="badge error">{b.code}</span>
              <div>
                <div>{b.detail}</div>
                <div className="hint">Next action: {b.next_action}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {passport.impact.events > 0 && (
        <div className="glass-card impact-banner rise">
          <strong>⚠ DATA CHANGE IMPACT</strong>
          <p>
            This source change affects {passport.impact.derived} calculation(s),{" "}
            {passport.impact.consolidations} consolidation(s) and {passport.impact.brsr} BRSR Core
            disclosure(s). Re-verification required.
          </p>
        </div>
      )}

      <div className="passport-grid rise rise-d2">
        <div className="glass-card passport-facts">
          <h3>Ownership & Authority</h3>
          <div className="fact"><span>Data owner</span><b>{passport.owner}</b></div>
          <div className="fact"><span>Reviewer</span><b>{passport.reviewer ?? "— any eligible reviewer"}</b></div>
          <div className="fact"><span>Second approver</span><b>{passport.second_approver ?? "—"}</b></div>
          {passport.risk.basis && <div className="fact"><span>Risk basis</span><b>{passport.risk.basis}</b></div>}
          {passport.controls.sla_days != null && (
            <div className="fact"><span>SLA</span><b>{passport.controls.sla_days} days</b></div>
          )}
        </div>
        <div className="glass-card passport-facts">
          <h3>Value</h3>
          {passport.value ? (
            <>
              <div className="kpi-value passport-value">
                {passport.value.normalized_value != null
                  ? passport.value.normalized_value.toLocaleString()
                  : "—"}{" "}
                <span className="kpi-unit">{passport.value.normalized_unit ?? ""}</span>
              </div>
              <div className="fact"><span>Version</span><b>v{passport.value.version} · {passport.value.status}</b></div>
              <div className="fact">
                <span>Control state</span>
                <b className="chiprow">
                  <ControlChip label="Evidence" ok={passport.evidence.missing.length === 0 && passport.evidence.required.length > 0} pending={passport.evidence.required.length === 0} />
                  <ControlChip label="Validation" ok={passport.validation.blocking === 0} pending={passport.validation.open > 0} />
                  <ControlChip label="Review" ok={!!passport.review.reviewed_by} pending={passport.status === "UNDER_REVIEW"} />
                  <ControlChip
                    label="Approval"
                    ok={["APPROVED", "LOCKED"].includes(passport.status)}
                    pending={passport.status === "PENDING_SECOND_APPROVAL"}
                  />
                </b>
              </div>
            </>
          ) : (
            <p className="hint">No value recorded yet.</p>
          )}
        </div>
      </div>

      <div className="filter-row rise rise-d3">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "primarybtn tabbtn" : "ghostbtn tabbtn"}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="glass-card rise rise-d3 passport-tab">
        {tab === "overview" && (
          <table className="data-table">
            <tbody>
              <tr><td>Value history</td><td>
                {passport.value?.history.map((h) => (
                  <div key={h.id} className="mono">
                    v{h.version}: {h.raw_value ?? "—"} {h.is_calculated ? "(calculated)" : ""} · {h.status}
                  </div>
                )) ?? "—"}
              </td></tr>
              <tr><td>Exceptions</td><td>
                {passport.validation.total} total · {passport.validation.blocking} blocking ·{" "}
                {passport.validation.open} open · {passport.validation.investigating} investigating
              </td></tr>
              <tr><td>Audit events</td><td>{passport.audit_count}</td></tr>
              <tr>
                <td>Lineage</td>
                <td>
                  <Link className="linkbtn" href={`/lineage/${passport.entity_id}/${passport.metric_code}/${passport.period_id}`}>
                    Where did this number come from? →
                  </Link>
                </td>
              </tr>
            </tbody>
          </table>
        )}

        {tab === "controls" && (
          <table className="data-table">
            <tbody>
              <tr><td>Required evidence</td><td>{passport.controls.required_evidence.join(", ") || "—"}</td></tr>
              <tr><td>Methodology mandatory</td><td>{passport.controls.methodology_required ? "Yes" : "No"}</td></tr>
              <tr><td>Independent review</td><td>{passport.controls.requires_independent_review ? "Required" : "Optional"}</td></tr>
              <tr><td>Second approval</td><td>{passport.controls.requires_second_approval ? "Required" : "Not required"}</td></tr>
              <tr><td>Escalation</td><td>{passport.controls.escalation_policy.join(" → ") || "Owner reminder"}</td></tr>
              <tr><td>Impact policy</td><td>{passport.controls.requires_second_approval ? "Re-verification required after source changes" : "Standard"}</td></tr>
            </tbody>
          </table>
        )}

        {tab === "methodology" && (
          passport.formula ? (
            <table className="data-table">
              <tbody>
                <tr><td>Formula</td><td className="mono">{passport.formula.code} · {passport.formula.name} (v{passport.formula.version})</td></tr>
                <tr><td>Expression</td><td className="mono">{passport.formula.expression}</td></tr>
                <tr><td>Inputs</td><td className="mono">{passport.formula.input_metric_codes.join(", ")}</td></tr>
                <tr><td>Resolved inputs</td><td className="mono">
                  {Object.entries(passport.formula.resolved_inputs).map(([k, v]) => `${k}=${v}`).join(" · ")}
                </td></tr>
                <tr><td>Methodology</td><td>
                  {passport.value?.methodology_notes ?? "Not recorded on the current value"}
                </td></tr>
              </tbody>
            </table>
          ) : (
            <p className="hint">This metric is directly reported; no formula applies. Methodology notes: {passport.value?.methodology_notes ?? "not recorded"}.</p>
          )
        )}

        {tab === "validation" && (
          <table className="data-table">
            <tbody>
              <tr><td>Blocking</td><td>{passport.validation.blocking}</td></tr>
              <tr><td>Open</td><td>{passport.validation.open}</td></tr>
              <tr><td>Investigating</td><td>{passport.validation.investigating}</td></tr>
              <tr><td>Explained</td><td>{passport.validation.explained}</td></tr>
            </tbody>
          </table>
        )}

        {tab === "evidence" && (
          <div>
            <div className="chiprow">
              {passport.evidence.required.map((c) => (
                <span
                  key={c}
                  className={passport.evidence.present.includes(c) ? "badge ok" : "badge error"}
                >
                  {c.replace(/_/g, " ")} {passport.evidence.present.includes(c) ? "✓" : "missing"}
                </span>
              ))}
              {!passport.evidence.required.length && <span className="hint">No evidence package required by policy.</span>}
            </div>
            <p className="hint">Evidence files are managed from the assignment screen; counts here reflect the current value version.</p>
          </div>
        )}

        {tab === "review" && (
          <table className="data-table">
            <tbody>
              <tr><td>Reviewed by</td><td>{passport.review.reviewed_by ?? "Pending"}</td></tr>
              <tr><td>Second approval</td><td>
                {passport.review.second_approval_required
                  ? (passport.review.second_approved_by
                      ? `Granted by ${passport.review.second_approved_by}`
                      : "Required — pending")
                  : "Not required by policy"}
              </td></tr>
            </tbody>
          </table>
        )}

        {tab === "lineage" && (
          <div>
            <h3>UPSTREAM — where the value comes from</h3>
            <DepLevels levels={passport.lineage.upstream} />
            <h3>DOWNSTREAM — what this value feeds</h3>
            <DepLevels levels={passport.lineage.downstream} />
            {impact && impact.events.length > 0 && (
              <>
                <h3>Recorded impact events</h3>
                <table className="data-table">
                  <thead>
                    <tr><th>Kind</th><th>Metric</th><th>Reason</th></tr>
                  </thead>
                  <tbody>
                    {impact.events.map((e) => (
                      <tr key={e.id}>
                        <td><span className="badge warn">{e.affected_kind}</span></td>
                        <td className="mono">{e.affected_metric_code ?? "—"}</td>
                        <td>{e.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        )}
      </div>
    </main>
  );
}
