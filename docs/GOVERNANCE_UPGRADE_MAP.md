# ESG Trust & Governance Upgrade — Implementation Map

Produced after forensic inspection of the repository (per the master prompt §1).
Principle: **extend, never rebuild**. Every new capability is grafted onto an
existing entity/service listed below.

## 1. What exists today (source of truth)

| Concern | Existing mechanism | File |
| --- | --- | --- |
| Work item | `Assignment` (metric/entity/period unique, owner, due date, status state machine) | `app/models/collection.py`, `app/collection/service.py` |
| Value + versioning | `MetricValue` immutable versions (raw/normalized, formula_id + formula_version + formula_inputs) | `app/models/collection.py` |
| Review workflow | `NOT_STARTED→IN_PROGRESS→SUBMITTED→UNDER_REVIEW→APPROVED→LOCKED` + correction/rejection branches | `app/collection/service.py` |
| Exceptions | `ValidationException` OPEN→EXPLAINED→RESOLVED | `app/models/validation.py`, `app/validation/engine.py` |
| Evidence | `Evidence` SHA-256, linked to `metric_value` | `app/evidence/*` |
| Lineage (upstream) | `/api/v1/lineage/{entity}/{metric}/{period}` read model | `app/lineage/router.py` |
| Dependency / staleness | formula dependents recompute (`recompute_dependents`) + `mark_stale_upstream` consolidation traces | `app/calculation/service.py`, `app/consolidation/service.py` |
| Audit | append-only `audit_event` + DB trigger | `app/audit/*` |
| BRSR Core readiness | dashboard `core_readiness` | `app/dashboard/router.py` |
| Notifications | `notify()` + Celery beat reminder scan | `app/notifications/*`, `app/workers/*` |
| Roles/scoping | `UserRole` + `UserEntityScope`, enforced in `auth/deps.py` | `app/auth/*` |
| Assurance readiness | ASSESSOR role, read-only scoping | `app/seed.py`, `app/auth/deps.py` |

## 2. Extension decisions (feature → existing foundation)

1. **Risk tiers** → new `metric_control_policy` table keyed `(framework_version_id, metric_code)` —
   NOT a change to `ValidationSeverity` (different concept). Seeded defaults derived from
   `brsr_core` / `evidence_required` metadata.
2. **Risk-adaptive workflow** → extend `collection/service.review()` with policy gates:
   evidence package completeness, independent reviewer ≠ owner, second approval via new
   `PENDING_SECOND_APPROVAL` assignment status (enum value added by migration) and
   `reviewed_by_user_id` / `second_approved_by_user_id` columns on `assignment`.
3. **Separation of duties** → server-side only: owner/self-submission cannot approve;
   second approver ≠ reviewer ≠ owner; assessor stays read-only (existing `save_value` guard);
   locked period/assignment guards already exist and are kept.
4. **Control Passport** → new aggregation endpoint
   `GET /api/v1/assignments/{id}/control-passport` assembling ONLY existing data
   (assignment, metric definition, policy, value, formula, evidence, exceptions, review
   state, lineage summary, audit, downstream impact) + blocking-reason/next-action logic.
5. **Ripple engine** → new `app/impact/` module wrapping `recompute_dependents` +
   `mark_stale_upstream` + BRSR-Core transitive closure; writes `impact_event` rows,
   raises remediation exceptions per policy, notifies affected owners/reviewers.
6. **Bidirectional dependency graph** → new endpoints
   `GET /api/v1/assignments/{id}/dependencies/upstream|downstream` built from
   formula `input_metric_codes` (derived), ratio num/den, and the entity hierarchy
   (consolidation). Existing lineage page remains the upstream renderer.
7. **Exception → remediation** → extend `ValidationException` (INVESTIGATING status,
   category, assigned_to, due_date, action_required, escalation_level, comments) +
   actions endpoint; no new competing model.
8. **Assurance Room** → new `AssuranceRequest` model + `app/assurance/` module; ASSESSOR
   role reused; assessor never gains value-write paths.
9. **Control Tower** → new `GET /api/v1/dashboard/control-tower` (read-only aggregation);
   existing dashboard summary untouched.
10. **SLA/escalation** → extend Celery `scan_reminders` with risk-tier-sensitive
    escalation, deduped by `escalation_event` table, audited as `ESCALATED` audit events.
11. **Methodology governance** → `MetricValue` already stores formula_version +
    resolved inputs; add `methodology_version`/`assumptions` snapshot columns. Historical
    rows are immutable by design — nothing is rewritten.
12. **Manager orchestration** → `POST /api/v1/assignments/{id}/reassign` (old owner, new
    owner, reason audited), `POST /api/v1/assignments/{id}/reopen` (post-approval change
    enters impact process), reviewer/second-approver assignment endpoint, control-policy
    GET/PUT. Reassignment history = audit events (the immutable mechanism).

## 3. New backend modules

```
app/models/governance.py      MetricControlPolicy, AssuranceRequest, ImpactEvent,
                              EscalationEvent + enums (risk tier, assurance states)
app/governance/policies.py    policy resolution + defaults + blocking-reason engine
app/impact/service.py         ripple propagation
app/impact/router.py          impact + dependency graph endpoints
app/passport/router.py        control passport endpoint
app/assurance/service.py      assurance request workflow
app/assurance/router.py       /api/v1/assurance/requests
migrations/versions/*         additive migration (new enums/tables/columns)
```

Existing files touched: `models/__init__.py`, `collection/service.py` (policy gates, SoD,
reopen, second approval), `collection/router.py`, `validation/models + engine + router`
(remediation), `evidence` (category), `dashboard/router.py` (control tower), `main.py`
(router registration), `seed.py`/`seed_demo.py` (policies + demo scenario), `workers/tasks.py`
(escalation scan).

## 4. Frontend additions (same design language)

- `features/governance/governance.ts` — typed API client for new endpoints.
- `app/passport/[id]/page.tsx` — Metric Control Passport (tabs/sections).
- `app/assurance/page.tsx` — Assurance Room.
- `app/page.tsx` — governance strip (control tower block) added above existing analytics.
- `app/assignments/[id]/page.tsx` — risk badge, control-state chips, impact banner,
  passport link, reopen (manager), second-approval action.
- `app/review/page.tsx` — risk badges + passport links.
- `app/exceptions/page.tsx` — remediation actions (assign/investigate/resolve).
- `components/AppShellNav.tsx` — Assurance nav item for ASSESSOR/REVIEWER/ESG_MANAGER/ADMIN.

## 5. Test plan (backend, against real Postgres in Docker)

`tests/integration/test_governance.py` covers: SoD (self-approval rejected, second-approval
sequencing, assessor write rejected), risk-adaptive paths (STANDARD/HIGH/CRITICAL), workflow
bypass rejection, evidence gate blocking, impact propagation correctness (unrelated metrics
untouched, BRSR KPI stale, readiness blocked), remediation lifecycle, assurance lifecycle,
reassignment audit, escalation dedup.

## 6. Honest-limitation notes

- No fabricated compliance/AI/integration features are added (master prompt §25).
- Demo data is labelled synthetic; demo credentials stay in `seed.py` as before.
- Tests require Docker (Postgres via `run.sh --test`); in environments without Docker only
  import/syntax checks and pure-unit tests can run.
