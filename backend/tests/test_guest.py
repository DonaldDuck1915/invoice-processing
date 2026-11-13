"""Guest mode: every guest gets a private sandbox with the demo data, and can switch reviewer/manager."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.workspaces import drop_workspace, open_session, sweep_expired

from .conftest import SAMPLES

API = "/api/v1"


@pytest.fixture(autouse=True)
def _fresh_guest_limits():
    from app.api import routes

    routes._guest_starts.clear()
    yield
    routes._guest_starts.clear()


def guest(c):
    r = c.post(f"{API}/auth/guest")
    assert r.status_code == 200, r.text
    body = r.json()
    return {"Authorization": "Bearer " + body["token"]}, body


def uploads(c, h):
    return [i for i in c.get(f"{API}/invoices", headers=h).json() if not i["filename"].startswith("historical-")]


def upload(c, h, name):
    r = c.post(f"{API}/invoices", headers=h, files={"file": (name, (SAMPLES / name).read_bytes(), "application/pdf")})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def workspace_of(h):
    from app.api.auth import token_payload

    return token_payload(h["Authorization"].split()[1])["w"]


def test_guest_gets_private_seeded_sandbox(db):
    c = TestClient(app)
    ha, body = guest(c)
    assert body["user"]["guest"] and body["user"]["role"] == "REVIEWER" and body["user"]["name"] == "Guest evaluator"
    me = c.get(f"{API}/auth/me", headers=ha).json()
    assert me["username"] == "guest-reviewer"
    assert len(c.get(f"{API}/vendors", headers=ha).json()) == 10          # demo data seeded
    inv_id = upload(c, ha, "01_happy_path_digital.pdf")                    # processed in the guest's sandbox
    d = c.get(f"{API}/invoices/{inv_id}", headers=ha).json()
    assert d["invoice"]["decision"] == "APPROVED"

    hb, _ = guest(c)                                                       # a second evaluator
    assert uploads(c, hb) == []
    assert upload(c, hb, "01_happy_path_digital.pdf")                      # not a duplicate for B
    b_list = uploads(c, hb)
    assert b_list[0]["decision"] == "APPROVED"
    # the real (main) database never sees guest uploads
    main_names = [i.original_filename for i in db.scalars(select(m.Invoice))]
    assert "01_happy_path_digital.pdf" not in main_names
    hr = {"Authorization": "Bearer " + c.post(f"{API}/auth/login", json={"username": "reviewer",
                                                                          "password": "review123"}).json()["token"]}
    assert all(i["filename"] != "01_happy_path_digital.pdf" for i in c.get(f"{API}/invoices", headers=hr).json())
    for h in (ha, hb):
        drop_workspace(workspace_of(h))


def test_guest_can_switch_to_manager_within_own_sandbox(db):
    c = TestClient(app)
    h, _ = guest(c)
    r = c.put(f"{API}/config/rules", headers=h, json={"overrides": {"approval": {"auto_approve_limit": {"INR": 1}}}})
    assert r.status_code == 403                                            # reviewer view
    sw = c.post(f"{API}/auth/guest/role", headers=h, json={"role": "MANAGER"}).json()
    hm = {"Authorization": "Bearer " + sw["token"]}
    assert sw["user"]["role"] == "MANAGER" and workspace_of(hm) == workspace_of(h)
    r = c.put(f"{API}/config/rules", headers=hm, json={"overrides": {"approval": {"auto_approve_limit": {"INR": 1}}}})
    assert r.status_code == 200
    # only this sandbox changed: the real instance keeps its limit
    from app.pipeline import effective_rules
    assert effective_rules(db)["approval"]["auto_approve_limit"]["INR"] == 500000
    back = c.post(f"{API}/auth/guest/role", headers=hm, json={"role": "REVIEWER"}).json()
    assert back["user"]["role"] == "REVIEWER"
    hr = {"Authorization": "Bearer " + c.post(f"{API}/auth/login", json={"username": "reviewer",
                                                                          "password": "review123"}).json()["token"]}
    assert c.post(f"{API}/auth/guest/role", headers=hr, json={"role": "MANAGER"}).status_code == 403
    drop_workspace(workspace_of(h))


def test_guest_reset_and_restrictions(db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allow_demo_reset", False)
    c = TestClient(app)
    h, _ = guest(c)
    upload(c, h, "01_happy_path_digital.pdf")
    assert c.post(f"{API}/admin/reset-demo", headers=h).status_code == 200  # own sandbox: always allowed
    assert uploads(c, h) == []
    assert c.post(f"{API}/admin/email/poll", headers=h).status_code == 403
    assert c.get(f"{API}/admin/email", headers=h).json()["enabled"] is False
    monkeypatch.setattr(s, "guest_max_invoices", 1)
    upload(c, h, "01_happy_path_digital.pdf")
    r = c.post(f"{API}/invoices", headers=h, files={"file": ("x.pdf", (SAMPLES / "03_ec1_split_a_60_chairs.pdf").read_bytes(),
                                                              "application/pdf")})
    assert r.status_code == 429 and r.json()["error_code"] == "GUEST_LIMIT"
    drop_workspace(workspace_of(h))


def test_expired_sandbox_is_deleted_and_token_refused(db):
    c = TestClient(app)
    h, _ = guest(c)
    ws = workspace_of(h)
    main = SessionLocal()
    row = main.get(m.GuestWorkspace, ws)
    row.last_used_at = datetime.now(timezone.utc) - timedelta(hours=get_settings().guest_ttl_hours + 1)
    main.commit()
    main.close()
    from app import workspaces
    workspaces._touched.clear()
    assert sweep_expired() >= 1
    r = c.get(f"{API}/invoices", headers=h)
    assert r.status_code == 401 and r.json()["error_code"] == "GUEST_EXPIRED"


def test_guest_rate_limit_and_switch_off(db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "guest_per_ip_per_hour", 2)
    c = TestClient(app)
    made = [guest(c)[0] for _ in range(2)]
    r = c.post(f"{API}/auth/guest")
    assert r.status_code == 429
    monkeypatch.setattr(s, "guest_mode", False)
    assert c.post(f"{API}/auth/guest").status_code == 403
    assert c.get(f"{API}/health").json()["guest_mode"] is False
    for h in made:
        drop_workspace(workspace_of(h))


def test_guest_token_cannot_be_used_without_its_workspace(db):
    from app.api.auth import issue_token

    c = TestClient(app)
    forged = issue_token("guest-manager")          # even a correctly signed guest token needs a workspace
    assert c.get(f"{API}/invoices", headers={"Authorization": "Bearer " + forged}).status_code == 401
    bogus = issue_token("guest-reviewer", "0123456789ab")   # well-formed id that does not exist
    assert c.get(f"{API}/invoices", headers={"Authorization": "Bearer " + bogus}).status_code == 401


def test_sandbox_session_is_separate(db):
    c = TestClient(app)
    h, _ = guest(c)
    ws = workspace_of(h)
    s = open_session(ws)
    try:
        s.add(m.UserProfile(username="guest-reviewer", display_name="Evaluator A"))
        s.commit()
    finally:
        s.close()
    assert c.get(f"{API}/auth/me", headers=h).json()["name"] == "Evaluator A"
    assert db.get(m.UserProfile, "guest-reviewer") is None
    drop_workspace(ws)
