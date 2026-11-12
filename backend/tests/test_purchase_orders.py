"""Entering a purchase order by hand (POST /purchase-orders) and using it straight away for matching."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.main import app

from .test_scenarios import run


def login(c):
    return {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "reviewer",
                                                                             "password": "review123"}).json()["token"]}


def body(**kw):
    base = {"po_number": "po-9001", "vendor_code": "V004", "po_date": "2026-10-01", "description": "Spare keyboards",
            "lines": [{"item_code": "KB-01", "description": "USB keyboard", "uom": "nos", "quantity": "10",
                       "unit_price": "1250.50", "tax_rate": "18"},
                      {"description": "Keyboard cover", "quantity": "10", "unit_price": "99.99"}]}
    return {**base, **kw}


def test_create_po_computes_totals_and_is_listed(db):
    c = TestClient(app)
    h = login(c)
    r = c.post("/api/v1/purchase-orders", json=body(), headers=h)
    assert r.status_code == 201, r.text
    po = r.json()
    assert po["po_number"] == "PO-9001" and po["currency"] == "INR" and po["status"] == "APPROVED"
    assert po["subtotal"] == "13504.90"            # 10 x 1250.50 + 10 x 99.99, computed server-side
    assert po["tax_total"] == "2250.90"            # 18% on line 1 only
    assert po["remaining_amount"] == "13504.90"
    assert [ln["uom"] for ln in po["lines"]] == ["NOS", None]
    assert "PO-9001" in [p["po_number"] for p in c.get("/api/v1/purchase-orders", headers=h).json()]
    ev = db.scalar(select(m.AuditEvent).where(m.AuditEvent.event_type == "PO_CREATED"))
    assert ev.invoice_id is None and "PO-9001" in ev.message and ev.actor == "reviewer"


@pytest.mark.parametrize("override,status,code", [
    ({"po_number": "PO-2001"}, 409, "PO_EXISTS"),
    ({"vendor_code": "V999"}, 422, "UNKNOWN_VENDOR"),
    ({"vendor_code": "V007"}, 422, "VENDOR_BLOCKED"),
    ({"currency": "GBP"}, 422, "UNSUPPORTED_CURRENCY"),
    ({"po_date": (date.today() + timedelta(days=2)).isoformat()}, 422, "FUTURE_DATE"),
    ({"lines": []}, 422, "INVALID_REQUEST"),
    ({"lines": [{"description": "x", "quantity": "0", "unit_price": "5"}]}, 422, "INVALID_REQUEST"),
    ({"lines": [{"description": "x", "quantity": "1", "unit_price": "5", "tax_rate": "150"}]}, 422, "INVALID_REQUEST"),
    ({"po_number": "bad number!"}, 422, "INVALID_REQUEST"),
])
def test_create_po_rejects_bad_input(db, override, status, code):
    c = TestClient(app)
    r = c.post("/api/v1/purchase-orders", json=body(**override), headers=login(c))
    assert r.status_code == status and r.json()["error_code"] == code, r.text


def test_create_po_requires_sign_in(db):
    assert TestClient(app).post("/api/v1/purchase-orders", json=body()).status_code == 401


def test_new_po_is_used_for_matching(db):
    """The invoice for PO-2005 is matched once that PO exists - here we re-create the scenario with a new PO
    number by correcting the invoice's PO reference to a hand-entered PO with the same lines."""
    c = TestClient(app)
    h = login(c)
    r = c.post("/api/v1/purchase-orders", json=body(po_number="PO-7777", description="Monitors (re-issued)", lines=[
        {"item_code": "MON-27", "description": "27-inch IPS LED monitor", "uom": "NOS", "quantity": "10",
         "unit_price": "14000", "tax_rate": "18"},
        {"item_code": "MS-WL-01", "description": "Wireless optical mouse", "uom": "NOS", "quantity": "10",
         "unit_price": "800", "tax_rate": "18"}]), headers=h)
    assert r.status_code == 201, r.text
    inv, _ = run(db, "01_happy_path_digital.pdf")
    r = c.patch(f"/api/v1/invoices/{inv.id}/fields", json={"fields": {"po_number": "PO-7777"},
                                                           "reason": "Vendor re-issued the PO"}, headers=h)
    d = r.json()
    assert d["run"]["matching"]["selected_po"] == "PO-7777" and d["invoice"]["decision"] == "APPROVED"
