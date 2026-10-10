"use client";

import { Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import { api } from "@/lib/api";
import { getControlTower, type ControlTowerSummary } from "@/features/governance/governance";

/* ---------------- animated counter ---------------- */
function useCountUp(target: number | null, duration = 900): string {
  const [value, setValue] = useState(0);
  const raf = useRef<number | null>(null);
  useEffect(() => {
    if (target === null) return;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      setValue(target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, [target, duration]);
  if (target === null) return "—";
  const rounded = Math.abs(target) >= 100 ? Math.round(value) : Math.round(value * 100) / 100;
  return rounded.toLocaleString();
}

const STATUS_ORDER = ["NOT_STARTED", "IN_PROGRESS", "SUBMITTED", "UNDER_REVIEW", "APPROVED", "LOCKED"];
const STATUS_COLORS: Record<string, string> = {
  NOT_STARTED: "#64748b",
  IN_PROGRESS: "#38bdf8",
  SUBMITTED: "#a78bfa",
  UNDER_REVIEW: "#fbbf24",
  APPROVED: "#10b981",
  LOCKED: "#34d399",
  NEEDS_CORRECTION: "#fb923c",
  REJECTED: "#f87171",
};

interface Summary {
  assignments_total: number;
  assignments_by_status: Record<string, number>;
  approved: number;
  overdue: { assignment_id: string; metric_code: string; entity_name: string; due_date: string; status: string }[];
  exceptions_open: Record<string, number>;
  top_exception_rules: { rule: string; count: number }[];
  core_readiness: { total: number; approved: number; evidence: number; blocking: number; ready: boolean; label: string };
  kpis: { metric_code: string; label: string; unit: string | null; fy24: number | null; fy25: number | null; yoy_pct: number | null }[];
  scope_split: Record<string, number | null>;
  entity_comparison: { entity: string; value: number; unit: string }[];
  ratio_demo: {
    metric: string; unit: string | null;
    plants: { entity: string; num: number; den: number; intensity: number }[];
    correct: number; naive_average: number; num_total: number; den_total: number;
  } | null;
  notes: string[];
}

const KPI_SHORT: Record<string, string> = {
  "C-P6-TOTAL-ENERGY": "Total energy",
  "C-P6-TOTAL-GHG": "Total GHG (S1+S2)",
  "C-P6-GHG-INTENSITY": "GHG intensity",
  "C-P6-ENERGY-INTENSITY": "Energy intensity",
  "C-P6-WATER-WITHDRAWAL": "Water withdrawal",
};

function KpiCard({ k, index }: { k: Summary["kpis"][number]; index: number }) {
  const fy24 = useCountUp(k.fy24);
  const awaiting = k.fy25 == null || k.fy25 === 0;
  const showYoY = !awaiting && k.yoy_pct != null;
  const yoy = k.yoy_pct ?? 0;
  const deltaCls = showYoY ? (yoy > 0 ? "bad" : "good") : "flat";
  return (
    <div className={`dash-card glass-card col-3 rise rise-d${(index % 4) + 1}`}>
      <div className="kpi-value">
        {fy24}
        <span className="kpi-unit">{k.unit}</span>
      </div>
      <div className="kpi-label">{KPI_SHORT[k.metric_code] ?? k.label} · FY2024-25</div>
      {showYoY ? (
        <span className={`delta-chip ${deltaCls}`}>
          {yoy > 0 ? "▲" : "▼"} {Math.abs(yoy)}% YoY
        </span>
      ) : (
        <span className="delta-chip flat">FY25 awaiting approvals</span>
      )}
    </div>
  );
}

export default function Home() {
  const { user, loading } = useAuth();
  const [data, setData] = useState<Summary | null>(null);
  const [tower, setTower] = useState<ControlTowerSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Summary>("/api/v1/dashboard/summary")
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load dashboard"));
    getControlTower()
      .then(setTower)
      .catch(() => setTower(null));
  }, []);

  const pipeline = useMemo(() => {
    if (!data) return [];
    return STATUS_ORDER.map((s) => ({
      key: s,
      label: s.replace(/_/g, " "),
      value: data.assignments_by_status[s] ?? 0,
      color: STATUS_COLORS[s],
    })).filter((s) => s.value > 0);
  }, [data]);

  const scopeData = useMemo(() => {
    if (!data) return [];
    return Object.entries(data.scope_split)
      .filter(([, v]) => v != null)
      .map(([name, value]) => ({
        name,
        value: value as number,
        fill: name === "Scope 1" ? "#10b981" : "#38bdf8",
      }));
  }, [data]);

  if (loading) {
    return (
      <main className="page">
        <div className="skeleton" style={{ height: "2rem", width: "18rem" }} />
        <div className="skeleton" style={{ height: "9rem", marginTop: "1rem" }} />
      </main>
    );
  }
  if (!user) return null;

  return (
    <main className="page">
      <h1 className="rise">Governance dashboard</h1>
      <p className="hint rise rise-d1">
        {user.role.replace("_", " ")} · every figure is live from the governed pipeline — approved
        data only, never estimated.
      </p>
      {error && <div className="state error" style={{ marginTop: "1rem" }}>{error}</div>}

      {tower && (
        <div className="dash-grid tower-strip rise rise-d1" style={{ marginTop: "1.2rem" }}>
          <div className="dash-card glass-card col-4">
            <h3>Critical metrics readiness</h3>
            <p className="sub">CRITICAL risk tier · {tower.period_label}</p>
            <div className="readiness-stats">
              <div>Total <b>{tower.critical_metrics.total}</b></div>
              <div>Ready <b>{tower.critical_metrics.ready}</b></div>
              <div>Blocked <b>{tower.critical_metrics.blocked}</b></div>
              <div>Pending review <b>{tower.critical_metrics.pending_review}</b></div>
            </div>
          </div>
          <div className="dash-card glass-card col-4">
            <h3>Open high-risk issues</h3>
            <p className="sub">Governance blockers from live system data</p>
            <div className="readiness-stats">
              <div>Blocking exceptions <b>{tower.high_risk.open_blocking_exceptions}</b></div>
              <div>Missing evidence <b>{tower.high_risk.missing_evidence}</b></div>
              <div>Stale metrics <b>{tower.high_risk.stale_metrics}</b></div>
              <div>Overdue owners <b>{tower.high_risk.overdue_owners}</b></div>
              <div>Reviewer backlog <b>{tower.high_risk.reviewer_backlog}</b></div>
              <div>Assurance requests <b>{tower.high_risk.open_assurance_requests}</b></div>
            </div>
          </div>
          <div className="dash-card glass-card col-4">
            <h3>Current bottleneck</h3>
            <p className="sub">Where the reporting cycle is blocked</p>
            {tower.bottleneck.stage ? (
              <>
                <div className="kpi-value" style={{ fontSize: "1.6rem" }}>
                  {tower.bottleneck.stage}
                </div>
                <p className="sub">
                  {tower.bottleneck.count} work item(s) · {tower.bottleneck.detail}
                </p>
              </>
            ) : (
              <p className="sub">No bottleneck detected.</p>
            )}
            <div className="readiness-stats">
              <div>Next deadline <b>{tower.deadlines.next_critical_deadline ?? "—"}</b></div>
              <div>Overdue <b>{tower.deadlines.overdue_count}</b></div>
            </div>
          </div>
        </div>
      )}

      {!data && !error && (
        <div className="dash-grid" style={{ marginTop: "1.2rem" }}>
          {[0, 1, 2, 3].map((i) => <div key={i} className="skeleton glass-card" style={{ height: "8rem" }} />)}
        </div>
      )}

      {data && (
        <div className="dash-grid" style={{ marginTop: "1.2rem" }}>

          {/* ---- BRSR Core readiness ring ---- */}
          <div className="dash-card glass-card col-4 rise rise-d1">
            <h3>BRSR Core readiness</h3>
            <p className="sub">Mandatory assessment/assurance subset</p>
            {(() => {
              const r = data.core_readiness;
              const pct = r.total ? Math.round((r.approved / r.total) * 100) : 0;
              const C = 2 * Math.PI * 44;
              const cls = r.label === "READY" ? "ready" : r.label.includes("PROVISIONAL") ? "progress" : "notready";
              return (
                <div className="readiness-ring">
                  <svg width="110" height="110">
                    <circle cx="55" cy="55" r="44" fill="none" strokeWidth="9" className="ring-bg" />
                    <circle cx="55" cy="55" r="44" fill="none" strokeWidth="9"
                      className="ring-fg"
                      stroke={cls === "ready" ? "#10b981" : cls === "progress" ? "#fbbf24" : "#f87171"}
                      strokeDasharray={C}
                      strokeDashoffset={C - (C * pct) / 100}
                      strokeLinecap="round" />
                    <text x="55" y="60" textAnchor="middle" fill="var(--text)"
                      fontSize="20" fontWeight="800" transform="rotate(90 55 55)">{pct}%</text>
                  </svg>
                  <div>
                    <span className={`ring-stamp ${cls}`}>{r.label}</span>
                    <div className="readiness-stats">
                      <div>Approved <b>{r.approved}/{r.total}</b></div>
                      <div>Evidence <b>{r.evidence}/{r.total}</b></div>
                      <div>Blocking <b>{r.blocking}</b></div>
                    </div>
                  </div>
                </div>
              );
            })()}
          </div>

          {/* ---- pipeline funnel ---- */}
          <div className="dash-card glass-card col-8 rise rise-d2">
            <h3>Collection pipeline — {data.assignments_total} assignments</h3>
            <p className="sub">Current reporting period, by governance state</p>
            <div className="pipeline-bar">
              {pipeline.map((s) => (
                <div key={s.key} className="pipeline-seg"
                  style={{ flexGrow: s.value, background: s.color }}
                  title={`${s.label}: ${s.value}`}>
                  {s.value > 3 ? s.value : ""}
                </div>
              ))}
            </div>
            <div className="pipeline-legend">
              {pipeline.map((s) => (
                <span key={s.key}>
                  <span style={{ color: s.color, fontWeight: 800 }}>●</span> {s.label} <b>{s.value}</b>
                </span>
              ))}
            </div>
            <div style={{ marginTop: "0.9rem" }}>
              <h3 style={{ fontSize: ".85rem" }}>Needs attention — overdue</h3>
              {data.overdue.length === 0 ? (
                <p className="hint" style={{ margin: ".3rem 0 0", fontSize: ".8rem" }}>
                  Nothing overdue — every assignment is on schedule.
                </p>
              ) : (
                data.overdue.slice(0, 4).map((o) => (
                  <div key={o.assignment_id} className="overdue-row">
                    <span>
                      <span className="mono">{o.metric_code}</span>{" "}
                      <span style={{ color: "var(--muted)" }}>· {o.entity_name}</span>
                    </span>
                    <span className="badge error">due {o.due_date}</span>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* ---- KPI cards with YoY ---- */}
          {data.kpis.map((k, i) => (
            <KpiCard key={k.metric_code} k={k} index={i} />
          ))}

          {/* ---- §5.6 consolidation correctness card ---- */}
          {data.ratio_demo && (
            <div className="dash-card glass-card vs-card col-6 rise">
              <h3>Why ratios are recomputed — never averaged</h3>
              <p className="sub">
                Real data: GHG intensity across 8 plants. Plant Delta is emissions-heavy (0.86),
                Plant Zeta efficient (0.07) — a naive average is distorted by both.
              </p>
              <div className="vs-numbers">
                <div>
                  <div className="vs-big wrong">{data.ratio_demo.naive_average}</div>
                  <div className="kpi-label">naive average of plant ratios</div>
                </div>
                <div style={{ fontSize: "1.3rem", color: "var(--muted)" }}>vs</div>
                <div>
                  <div className="vs-big right">{data.ratio_demo.correct}</div>
                  <div className="kpi-label">
                    Σ{data.ratio_demo.num_total.toLocaleString()} tCO₂e ÷ Σ{data.ratio_demo.den_total.toLocaleString()} ₹cr
                  </div>
                </div>
              </div>
              <div className="vs-bar">
                <div style={{ width: `${Math.min(100, (data.ratio_demo.correct / data.ratio_demo.naive_average) * 100)}%`, background: "var(--accent)" }} />
              </div>
              <p className="vs-note">
                The naive figure overstates group intensity by{" "}
                <b style={{ color: "var(--red)" }}>
                  {Math.round(((data.ratio_demo.naive_average - data.ratio_demo.correct) / data.ratio_demo.correct) * 100)}%
                </b>
                . The platform always recomputes (Σ numerator) ÷ (Σ denominator) at every level of
                the hierarchy — the single most common spreadsheet error in multi-entity ESG reporting.
              </p>
            </div>
          )}

          {/* ---- scope split ---- */}
          {scopeData.length > 0 && (
            <div className="dash-card glass-card col-3 rise">
              <h3>GHG footprint by scope</h3>
              <p className="sub">FY2024-25, group level (tCO₂e)</p>
              <div style={{ width: "100%", height: 190 }}>
                <ResponsiveContainer>
                  <PieChart>
                    <Pie data={scopeData} dataKey="value" nameKey="name" innerRadius={50}
                      outerRadius={78} paddingAngle={3} strokeWidth={0}>
                      {scopeData.map((s) => <Cell key={s.name} fill={s.fill} />)}
                    </Pie>
                    <Tooltip contentStyle={{ background: "#101b31", border: "1px solid rgba(94,118,153,.3)", borderRadius: 10, fontSize: 12 }} />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <div className="pipeline-legend">
                {scopeData.map((s) => (
                  <span key={s.name}>
                    <span style={{ color: s.fill, fontWeight: 800 }}>●</span> {s.name}{" "}
                    <b>{Math.round(s.value).toLocaleString()}</b>
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* ---- entity comparison ---- */}
          {data.entity_comparison.length > 0 && (
            <div className="dash-card glass-card col-6 rise">
              <h3>Energy by plant</h3>
              <p className="sub">FY2024-25 group consolidation (MWh)</p>
              <div style={{ width: "100%", height: 230 }}>
                <ResponsiveContainer>
                  <BarChart data={data.entity_comparison} margin={{ left: -18 }}>
                    <CartesianGrid stroke="rgba(148,163,184,.12)" vertical={false} />
                    <XAxis dataKey="entity" tick={{ fill: "#8fa3bd", fontSize: 10 }} interval={0}
                      angle={-30} textAnchor="end" height={58} />
                    <YAxis tick={{ fill: "#8fa3bd", fontSize: 10 }} />
                    <Tooltip cursor={{ fill: "rgba(56,189,248,.06)" }}
                      contentStyle={{ background: "#101b31", border: "1px solid rgba(94,118,153,.3)", borderRadius: 10, fontSize: 12 }} />
                    <Bar dataKey="value" radius={[6, 6, 0, 0]}>
                      {data.entity_comparison.map((c, i) => (
                        <Cell key={i} fill={i % 2 ? "#10b981" : "#38bdf8"} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* ---- exceptions ---- */}
          <div className="dash-card glass-card col-6 rise">
            <h3>Validation exceptions</h3>
            <p className="sub">Open items awaiting resolution</p>
            <div className="kpi-row" style={{ margin: "0.2rem 0 0.7rem" }}>
              <div className="kpi" style={{ padding: ".6rem .8rem" }}>
                <div className="kpi-value" style={{ fontSize: "1.3rem", color: "var(--red)" }}>
                  {data.exceptions_open.BLOCKING ?? 0}
                </div>
                <div className="kpi-label">blocking</div>
              </div>
              <div className="kpi" style={{ padding: ".6rem .8rem" }}>
                <div className="kpi-value" style={{ fontSize: "1.3rem", color: "var(--amber)" }}>
                  {data.exceptions_open.WARNING ?? 0}
                </div>
                <div className="kpi-label">warnings</div>
              </div>
            </div>
            <h3 style={{ fontSize: ".85rem" }}>Top rules firing</h3>
            {data.top_exception_rules.length === 0 ? (
              <p className="hint" style={{ fontSize: ".8rem", margin: ".3rem 0 0" }}>No open exceptions.</p>
            ) : (
              data.top_exception_rules.map((r) => (
                <div key={r.rule} className="overdue-row">
                  <span className="mono">{r.rule}</span>
                  <span className="badge warn">×{r.count}</span>
                </div>
              ))
            )}
          </div>

          {data.notes.map((n) => (
            <div key={n} className="state col-12" style={{ fontSize: ".82rem" }}>{n}</div>
          ))}
        </div>
      )}
    </main>
  );
}
