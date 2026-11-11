from fastapi.testclient import TestClient

from app.main import app

from .conftest import SAMPLES


def client_and_token(db):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "reviewer", "password": "review123"})
    assert r.status_code == 200
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def test_auth_required(db):
    c = TestClient(app)
    assert c.get("/api/v1/invoices").status_code == 401
    assert c.post("/api/v1/auth/login", json={"username": "reviewer", "password": "nope"}).status_code == 401
    assert c.get("/api/v1/invoices", headers={"Authorization": "Bearer forged.token"}).status_code == 401


def test_upload_process_and_report(db):
    c, h = client_and_token(db)
    pdf = (SAMPLES / "01_happy_path_digital.pdf").read_bytes()
    r = c.post("/api/v1/invoices", files={"file": ("inv.pdf", pdf, "application/pdf")}, headers=h)
    assert r.status_code == 202, r.text
    inv_id = r.json()["id"]
    d = c.get(f"/api/v1/invoices/{inv_id}", headers=h).json()  # background task ran after the response
    assert d["invoice"]["decision"] == "APPROVED"
    assert isinstance(d["invoice"]["grand_total"], str)  # money never serialised as float
    assert d["po"]["po_number"] == "PO-2005"
    assert c.get(f"/api/v1/invoices/{inv_id}/file", headers=h).content[:4] == b"%PDF"
    audit = c.get(f"/api/v1/invoices/{inv_id}/audit", headers=h).json()
    assert audit[0]["event_type"] == "DOCUMENT_UPLOADED"
    rep = c.get(f"/api/v1/invoices/{inv_id}/report", headers=h)
    assert rep.status_code == 200 and rep.json()["final_decision"] == "APPROVED"
    stats = c.get("/api/v1/dashboard/stats", headers=h).json()
    assert stats["approved"] == 1 and stats["total_value"]["INR"] == "174640.00"


def test_upload_rejects_non_pdf(db):
    c, h = client_and_token(db)
    r = c.post("/api/v1/invoices", files={"file": ("x.pdf", b"hello" * 50, "application/pdf")}, headers=h)
    assert r.status_code == 400 and r.json()["error_code"] == "NOT_A_PDF"


def test_review_actions_require_reason(db):
    c, h = client_and_token(db)
    pdf = (SAMPLES / "10_ec3_price_8pct_over.pdf").read_bytes()
    inv_id = c.post("/api/v1/invoices", files={"file": ("p.pdf", pdf, "application/pdf")}, headers=h).json()["id"]
    assert c.post(f"/api/v1/invoices/{inv_id}/final-decision", json={"decision": "APPROVED", "reason": ""},
                  headers=h).status_code == 422
    r = c.post(f"/api/v1/invoices/{inv_id}/final-decision",
               json={"decision": "APPROVED", "reason": "Price increase approved by procurement (mail 12-Oct)"}, headers=h)
    assert r.status_code == 200 and r.json()["invoice"]["decided_by"] == "HUMAN"
    po = c.get("/api/v1/purchase-orders/PO-2003", headers=h).json()
    assert po["lines"][0]["consumed_qty"] == "100"


def test_rules_override_validation(db):
    c, h = client_and_token(db)
    assert c.put("/api/v1/config/rules", json={"overrides": {"tolerances": {"nope": 1}}}, headers=h).status_code == 422
    r = c.put("/api/v1/config/rules", json={"overrides": {"tolerances": {"unit_price_pct": 10}}}, headers=h)
    assert r.json()["effective"]["tolerances"]["unit_price_pct"] == 10
    c.put("/api/v1/config/rules", json={"overrides": {}}, headers=h)


def test_import_procurement(db):
    c, h = client_and_token(db)
    body = {"vendors": [{"code": "V100", "name": "Real Vendor Ltd", "tax_id": "DE811569870", "country": "DE",
                         "default_currency": "EUR"}],
            "purchase_orders": [{"po_number": "PO-9001", "vendor": "V100", "po_date": "2026-10-01", "currency": "EUR",
                                 "lines": [{"description": "Widget", "quantity": "10", "unit_price": "5.50",
                                            "tax_rate": "19"}]}]}
    r = c.post("/api/v1/admin/import", json=body, headers=h)
    assert r.json() == {"vendors_added": 1, "purchase_orders_added": 1}
    po = c.get("/api/v1/purchase-orders/PO-9001", headers=h).json()
    assert po["subtotal"] == "55.00" and po["total_amount"] == "65.45"
