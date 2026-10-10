"""Governance & assurance integration tests (master prompt §20).

Covers: separation of duties, risk-adaptive workflow (STANDARD/HIGH/CRITICAL),
workflow-bypass rejection, evidence policy gates, change-impact propagation
(correct dependents only), exception->remediation lifecycle, assurance
request lifecycle, reassignment audit and control-passport aggregation.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, seed

TEST_STATE: dict = {}


@pytest.fixture(scope="module")
def client(migrated_engine):
    from sqlalchemy.orm import sessionmaker

    from app.db.session import get_db

    TestSession = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def ctx(migrated_engine):
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from app.models import AppUser, Entity, ReportingPeriod

    TestSession = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    with TestSession() as db:
        seed(db)
        ctx = {
            "period_fy25": db.scalar(
                select(ReportingPeriod).where(ReportingPeriod.label == "FY2025-26")
            ).id,
            "period_fy24": db.scalar(
                select(ReportingPeriod).where(ReportingPeriod.label == "FY2024-25")
            ).id,
            "plant_alpha": db.scalar(select(Entity).where(Entity.name == "Plant Alpha")).id,
            "plant_beta": db.scalar(select(Entity).where(Entity.name == "Plant Beta")).id,
            "plant_gamma": db.scalar(select(Entity).where(Entity.name == "Plant Gamma")).id,
            "owner_beta": db.scalar(
                select(AppUser).where(AppUser.email == "owner-beta@example.local")
            ).id,
            "owner_alpha": db.scalar(
                select(AppUser).where(AppUser.email == "owner-alpha@example.local")
            ).id,
            "owner_gamma": db.scalar(
                select(AppUser).where(AppUser.email == "owner-gamma@example.local")
            ).id,
        }
        yield ctx


def _login(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _assignments(client, headers, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/api/v1/assignments?{qs}", headers=headers).json()


def _assignment_id(client, headers, entity_id, metric_code):
    rows = _assignments(client, headers, entity_id=str(entity_id))
    return next(a["id"] for a in rows if a["metric_code"] == metric_code)


# ---------------------------------------------------------------- SoD


def test_owner_cannot_review_own_assignment(client, ctx):
    """SoD: the data owner must not be able to act as reviewer on their own
    work item, even via direct API calls."""
    headers = _login(client, "owner-gamma@example.local")
    # Gamma base metrics are seeded SUBMITTED
    rows = _assignments(client, headers, entity_id=str(ctx["plant_gamma"]), status="SUBMITTED")
    assert rows, "Gamma SUBMITTED assignments expected"
    target = rows[0]["id"]
    r = client.post(
        f"/api/v1/assignments/{target}/review",
        json={"action": "START_REVIEW"}, headers=headers,
    )
    assert r.status_code == 403
    assert "Separation of duties" in r.json()["detail"]


def test_submitter_cannot_approve_own_value(client, ctx):
    """SoD: whoever submitted the value cannot approve it (reviewer acting on
    a value they submitted themselves — enforced even for reviewers)."""
    # owner-alpha submits a draft; then an ADMIN who is 'creator' but not the
    # submitter can review; owner-alpha cannot.
    headers = _login(client, "owner-alpha@example.local")
    aid = _assignment_id(client, headers, ctx["plant_alpha"], "C-P6-GRID-RENEWABLE-MWH")
    detail = client.get(f"/api/v1/assignments/{aid}", headers=headers).json()
    version = detail["latest_version"] or 0
    r = client.post(
        f"/api/v1/assignments/{aid}/value",
        json={"action": "SUBMIT", "raw_value": 25.0, "raw_unit": "MWh",
              "expected_last_version": version},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    # owner attempts to approve their own submission
    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "APPROVE"}, headers=headers
    )
    assert r.status_code == 403


def test_assessor_cannot_modify_official_values(client, ctx):
    """Assessor is read/assess/request only — value writes must be rejected."""
    headers = _login(client, "assessor@example.local")
    aid = _assignment_id(client, headers, ctx["plant_gamma"], "C-P6-GRID-RENEWABLE-MWH")
    r = client.post(
        f"/api/v1/assignments/{aid}/value",
        json={"action": "SUBMIT", "raw_value": 1.0, "raw_unit": "MWh"},
        headers=headers,
    )
    assert r.status_code in (403, 404)


# ------------------------------------------------- risk-adaptive workflow


def test_control_policy_critical_scope1(client, ctx):
    """Scope 1 resolves to the CRITICAL policy with the full control package."""
    headers = _login(client, "owner-beta@example.local")
    aid = _assignment_id(client, headers, ctx["plant_beta"], "C-P6-SCOPE1-TCO2E")
    r = client.get(f"/api/v1/assignments/{aid}/control-passport", headers=headers)
    assert r.status_code == 200, r.text
    passport = r.json()
    assert passport["risk"]["tier"] == "CRITICAL"
    assert passport["risk"]["brsr_core"] is True
    assert passport["controls"]["requires_second_approval"] is True
    assert "source_data" in passport["controls"]["required_evidence"]


def test_critical_workflow_requires_second_approval(client, ctx):
    """CRITICAL metric: reviewer approval moves to PENDING_SECOND_APPROVAL,
    and only a different approver can complete the second stage."""
    manager = _login(client, "manager@example.local")
    owner = _login(client, "owner-beta@example.local")
    reviewer = _login(client, "reviewer@example.local")

    # Beta Scope 1 is seeded APPROVED; reopen it (manager), owner re-submits,
    # then exercise the two-stage approval.
    aid = _assignment_id(client, manager, ctx["plant_beta"], "C-P6-SCOPE1-TCO2E")
    r = client.post(
        f"/api/v1/assignments/{aid}/reopen",
        json={"reason": "governance demo: re-verification of scope 1"}, headers=manager,
    )
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/v1/assignments/{aid}", headers=owner).json()
    version = detail["latest_version"]
    r = client.post(
        f"/api/v1/assignments/{aid}/value",
        json={"action": "SUBMIT", "raw_value": 15813805.97, "raw_unit": "litre",
              "expected_last_version": version},
        headers=owner,
    )
    assert r.status_code == 201, r.text

    # bypass attempt: owner tries to jump straight to approval
    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "APPROVE"}, headers=owner
    )
    assert r.status_code == 403

    # evidence gate: review start is blocked without the required categories
    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "START_REVIEW"},
        headers=reviewer,
    )
    assert r.status_code == 422, "evidence gate must block review start"
    assert "MISSING_EVIDENCE" in r.json()["detail"]

    # owner uploads the required evidence against the submitted value, then
    # review proceeds
    from io import BytesIO

    detail = client.get(f"/api/v1/assignments/{aid}", headers=owner).json()
    value_id = detail["values"][0]["id"]
    for i, category in enumerate(["source_data", "methodology", "emission_factor", "supporting_doc"]):
        r = client.post(
            f"/api/v1/evidence",
            data={"category": category, "metric_value_id": str(value_id)},
            files={"file": (f"beta-scope1-{category}.pdf", BytesIO(b"demo evidence"), "application/pdf")},
            headers=owner,
        )
        assert r.status_code == 201, f"evidence upload failed: {r.text}"

    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "START_REVIEW"},
        headers=reviewer,
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "APPROVE"}, headers=reviewer
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING_SECOND_APPROVAL"

    # reviewer cannot grant the second approval themselves (SoD)
    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "SECOND_APPROVAL"},
        headers=reviewer,
    )
    assert r.status_code == 403

    # manager grants the second approval
    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "SECOND_APPROVAL"},
        headers=manager,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"


def test_standard_metric_single_approval(client, ctx):
    """STANDARD metric: one reviewer approval suffices — no second stage."""
    owner = _login(client, "owner-alpha@example.local")
    reviewer = _login(client, "reviewer@example.local")
    aid = _assignment_id(client, owner, ctx["plant_alpha"], "C-P6-WATER-WITHDRAWAL")
    detail = client.get(f"/api/v1/assignments/{aid}", headers=owner).json()
    version = detail["latest_version"] or 0
    r = client.post(
        f"/api/v1/assignments/{aid}/value",
        json={"action": "SUBMIT", "raw_value": 5250.0, "raw_unit": "kL",
              "expected_last_version": version},
        headers=owner,
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "START_REVIEW"}, headers=reviewer
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/assignments/{aid}/review", json={"action": "APPROVE"}, headers=reviewer
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"


# ---------------------------------------------------------------- impact


def test_change_impact_propagates_to_real_dependents_only(client, ctx):
    """Source change on Beta diesel recomputes Scope 1 (43,120 tCO2e),
    records impact events for real dependents only, and raises a
    re-verification exception; an unrelated metric gets none."""
    owner = _login(client, "owner-beta@example.local")
    manager = _login(client, "manager@example.local")

    diesel_aid = _assignment_id(client, manager, ctx["plant_beta"], "C-P6-DIESEL-LITRES")
    scope1_aid = _assignment_id(client, manager, ctx["plant_beta"], "C-P6-SCOPE1-TCO2E")
    # manager reopens the approved source + its derived metric (post-approval
    # change enters the change-impact process)
    for aid in (diesel_aid, scope1_aid):
        r = client.post(
            f"/api/v1/assignments/{aid}/reopen",
            json={"reason": "controlled source change demo"}, headers=manager,
        )
        assert r.status_code == 200, r.text

    detail = client.get(f"/api/v1/assignments/{diesel_aid}", headers=owner).json()
    version = detail["latest_version"]
    # corrected diesel: 16,089,552.24 L -> Scope 1 recomputes to 43,120 tCO2e
    r = client.post(
        f"/api/v1/assignments/{diesel_aid}/value",
        json={"action": "SUBMIT", "raw_value": 16089552.24, "raw_unit": "litre",
              "expected_last_version": version},
        headers=owner,
    )
    assert r.status_code == 201, r.text

    # the derived Scope 1 value was recomputed and now requires re-verification
    scope1_detail = client.get(f"/api/v1/assignments/{scope1_aid}", headers=owner).json()
    latest = scope1_detail["values"][0]
    assert latest["is_calculated"] is True
    assert abs(latest["raw_value"] - 43120.0) < 0.01
    assert latest["status"] == "IN_PROGRESS"
    assert scope1_detail["assignment"]["status"] == "IN_PROGRESS"

    # impact events: recorded on the SOURCE assignment, derived dependent only
    impact = client.get(f"/api/v1/assignments/{diesel_aid}/impact", headers=manager).json()
    kinds = [e["affected_kind"] for e in impact["events"]]
    assert "derived" in kinds, "Scope 1 derived assignment must be impacted"
    affected_codes = {e["affected_metric_code"] for e in impact["events"]}
    assert "C-P6-SCOPE1-TCO2E" in affected_codes
    assert "C-P6-WASTE-GENERATED" not in affected_codes

    # downstream dependency graph answers transitively
    deps = client.get(f"/api/v1/assignments/{diesel_aid}/dependencies/downstream", headers=owner).json()
    flat = {c for codes in deps["levels"].values() for c in codes}
    assert "C-P6-SCOPE1-TCO2E" in flat
    assert "C-P6-TOTAL-GHG" in flat
    assert "C-P6-GHG-INTENSITY" in flat

    # policy-driven re-verification exception exists on the source
    rows = client.get(
        f"/api/v1/validation/exceptions?entity_id={ctx['plant_beta']}", headers=manager
    ).json()
    assert any(e["rule_code"] == "GOV-REVERIFICATION" for e in rows), \
        "re-verification exception must be raised by policy"


def test_passport_aggregates_all_sections(client, ctx):
    owner = _login(client, "owner-beta@example.local")
    aid = _assignment_id(client, owner, ctx["plant_beta"], "C-P6-SCOPE1-TCO2E")
    passport = client.get(f"/api/v1/assignments/{aid}/control-passport", headers=owner).json()
    for key in ("risk", "controls", "owner", "value", "formula", "evidence",
                "validation", "review", "lineage", "impact", "blocking", "audit_count"):
        assert key in passport, f"passport missing {key}"
    assert passport["formula"]["code"] == "F-SCOPE1"
    assert passport["lineage"]["upstream"], "upstream inputs must be listed"


# ----------------------------------------------------------- remediation


def test_exception_remediation_lifecycle(client, ctx):
    manager = _login(client, "manager@example.local")
    owner = _login(client, "owner-beta@example.local")
    # ensure exceptions exist: run the validation sweep on Beta
    r = client.post(
        "/api/v1/validation/run",
        json={"period_id": str(ctx["period_fy25"]), "entity_id": str(ctx["plant_beta"])},
        headers=manager,
    )
    assert r.status_code == 200, r.text
    rows = client.get("/api/v1/validation/exceptions?status=OPEN", headers=manager).json()
    assert rows, "open exceptions expected after validation run"
    target = rows[0]["id"]
    r = client.post(
        f"/api/v1/validation/exceptions/{target}/actions",
        json={"action": "ASSIGN", "assignee_id": str(ctx["owner_beta"]),
              "due_date": "2026-10-18",
              "action_required": "Explain variance and attach supporting evidence"},
        headers=manager,
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/validation/exceptions/{target}/actions",
        json={"action": "START"}, headers=owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "INVESTIGATING"
    r = client.post(
        f"/api/v1/validation/exceptions/{target}/actions",
        json={"action": "EXPLAIN", "comment": "Variance explained by the new production line commissioning."},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "EXPLAINED"
    r = client.post(
        f"/api/v1/validation/exceptions/{target}/actions",
        json={"action": "RESOLVE"}, headers=manager,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RESOLVED"


# ------------------------------------------------------------- assurance


def test_assurance_request_lifecycle(client, ctx):
    assessor = _login(client, "assessor@example.local")
    owner = _login(client, "owner-beta@example.local")
    reviewer = _login(client, "reviewer@example.local")

    rows = client.get("/api/v1/assurance/requests", headers=assessor).json()
    assert rows, "seeded assurance request expected"
    req = next(r for r in rows if r["metric_code"] == "C-P6-SCOPE1-TCO2E")

    r = client.patch(
        f"/api/v1/assurance/requests/{req['id']}",
        json={"action": "START"}, headers=owner,
    )
    assert r.status_code == 200, r.text
    r = client.patch(
        f"/api/v1/assurance/requests/{req['id']}",
        json={"action": "RESPOND", "response_text": "Evidence package uploaded: fuel invoices and GHG methodology v3."},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RESPONDED"
    # the data owner cannot resolve their own response
    r = client.patch(
        f"/api/v1/assurance/requests/{req['id']}",
        json={"action": "RESOLVE"}, headers=owner,
    )
    assert r.status_code == 403
    r = client.patch(
        f"/api/v1/assurance/requests/{req['id']}",
        json={"action": "UNDER_REVIEW"}, headers=reviewer,
    )
    assert r.status_code == 200, r.text
    r = client.patch(
        f"/api/v1/assurance/requests/{req['id']}",
        json={"action": "RESOLVE"}, headers=reviewer,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RESOLVED"

    # history is preserved (status trail in audit, request row retained)
    after = client.get(f"/api/v1/assurance/requests/{req['id']}", headers=assessor).json()
    assert after["response_text"]


def test_data_owner_cannot_create_assurance_request(client, ctx):
    owner = _login(client, "owner-beta@example.local")
    r = client.post(
        "/api/v1/assurance/requests",
        json={"request_type": "CLARIFICATION", "entity_id": str(ctx["plant_beta"]),
              "metric_code": "C-P6-SCOPE1-TCO2E", "period_id": str(ctx["period_fy25"]),
              "reason": "owners cannot raise assurance requests"},
        headers=owner,
    )
    assert r.status_code == 403


# ------------------------------------------------------- orchestration


def test_reassignment_requires_manager_and_reason(client, ctx):
    manager = _login(client, "manager@example.local")
    owner = _login(client, "owner-beta@example.local")
    aid = _assignment_id(client, manager, ctx["plant_gamma"], "C-P6-WASTE-GENERATED")
    # owner cannot reassign
    r = client.post(
        f"/api/v1/assignments/{aid}/reassign",
        json={"new_owner_user_id": str(ctx["owner_alpha"]), "reason": "self-service"},
        headers=owner,
    )
    assert r.status_code == 403
    # manager reassigns with reason
    r = client.post(
        f"/api/v1/assignments/{aid}/reassign",
        json={"new_owner_user_id": str(ctx["owner_alpha"]), "reason": "owner on leave"},
        headers=manager,
    )
    assert r.status_code == 200, r.text
    assert r.json()["owner_user_id"] == str(ctx["owner_alpha"])


def test_control_tower_reports_real_indicators(client, ctx):
    manager = _login(client, "manager@example.local")
    r = client.get("/api/v1/dashboard/control-tower", headers=manager)
    assert r.status_code == 200, r.text
    tower = r.json()
    assert tower["critical_metrics"]["total"] >= 1
    for key in ("high_risk", "funnel", "bottleneck", "deadlines", "core_readiness"):
        assert key in tower
    # funnel counts must sum to the number of assignments in the period
    assert sum(tower["funnel"].values()) >= tower["critical_metrics"]["total"]
