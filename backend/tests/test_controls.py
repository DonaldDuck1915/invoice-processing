"""AP controls: approval limits, credit/debit notes, fraud signals, GST e-invoice and TDS."""
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.controls import fraud_checks, tds_check
from app.main import app
from app.notes import adjustments
from app.pipeline import effective_rules, final_decision
from app.schemas import ExtractedInvoice, FieldValue

from .test_scenarios import rules_of, run


def login(c, user="reviewer", pw="review123"):
    r = c.post("/api/v1/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}, r.json()["user"]


def consumed(db, po_number, line_no=1):
    line = db.scalar(select(m.POLine).join(m.PurchaseOrder).where(m.PurchaseOrder.po_number == po_number,
                                                                  m.POLine.line_no == line_no))
    rows = db.scalars(select(m.POConsumption).where(m.POConsumption.po_line_id == line.id,
                                                    m.POConsumption.status == "ACTIVE")).all()
    return sum((r.quantity for r in rows), Decimal(0)), sum((r.amount for r in rows), Decimal(0))


def fake_inv(**fields):
    return ExtractedInvoice(fields={k: FieldValue(value=v, status="EXTRACTED", confidence=0.99) for k, v in fields.items()})


# ============================================================================ credit / debit notes

def test_credit_note_nets_original_and_returns_po_quantity(db):
    orig, _ = run(db, "03_ec1_split_a_60_chairs.pdf")
    assert consumed(db, "PO-2001")[0] == 60
    note, r = run(db, "24_credit_note_return_5_chairs.pdf")
    assert note.document_type == "CREDIT_NOTE" and note.original_invoice_id == orig.id
    assert note.decision == "APPROVED", r.decision
    assert r.controls["approval"]["level"] == "AUTO"          # credit notes are exempt from the limit
    assert consumed(db, "PO-2001") == (Decimal(55), Decimal("275000.00"))
    adj = adjustments(db, orig)
    assert adj["net_total"] == "324500.00" and adj["notes"][0]["amount"] == "-29500.00"
    # a reviewer rejecting the note afterwards gives the quantity back to the original invoice
    final_decision(db, note, "REJECTED", "Return not received", "reviewer")
    assert consumed(db, "PO-2001")[0] == 60
    assert adjustments(db, orig)["net_total"] == "354000.00"


def test_credit_note_without_original_goes_to_review(db):
    note, r = run(db, "24_credit_note_return_5_chairs.pdf")
    assert note.decision == "MANUAL_REVIEW"
    assert "NOTE_ORIGINAL_NOT_FOUND" in r.decision["flags"] and "M24" in rules_of(r)
    assert consumed(db, "PO-2001")[0] == 0


def test_credit_note_cannot_exceed_original(db):
    run(db, "03_ec1_split_a_60_chairs.pdf")
    c = TestClient(app)
    h, _ = login(c)
    note, _ = run(db, "24_credit_note_return_5_chairs.pdf")
    # pretend the vendor credits 70 chairs - more than the 60 billed and more than the invoice value
    r = c.patch(f"/api/v1/invoices/{note.id}/fields", headers=h, json={
        "fields": {"grand_total": "413000.00", "subtotal": "350000.00", "tax_amount": "63000.00"},
        "line_items": [{"description": "Ergonomic mesh office chair", "item_code": "CH-ERG-01", "quantity": "70",
                        "unit_price": "5000", "tax_rate": "18", "line_total": "350000.00"}],
        "reason": "test over-credit"}).json()
    assert r["invoice"]["decision"] == "MANUAL_REVIEW"
    codes = {i["code"] for i in r["run"]["controls"]["issues"]}
    assert {"CREDIT_EXCEEDS_ORIGINAL", "CREDIT_QTY_EXCEEDS_BILLED"} <= codes


def test_debit_note_adds_value_and_needs_review(db):
    orig, _ = run(db, "01_happy_path_digital.pdf")
    note, r = run(db, "25_debit_note_freight.pdf")
    assert note.document_type == "DEBIT_NOTE" and note.original_invoice_id == orig.id
    assert note.decision == "MANUAL_REVIEW" and "M25" in rules_of(r)
    assert r.matching["selected_po"] == "PO-2005" and r.matching["pairs"][0]["invoice"]["quantity"] == "0"
    final_decision(db, note, "APPROVED", "Freight agreed by buyer", "reviewer")
    assert consumed(db, "PO-2005") == (Decimal(10), Decimal("143500.00"))   # line 1: 140000 + 3500 freight
    assert adjustments(db, orig)["net_total"] == "178770.00"                 # 174640 + 4130


# ============================================================================ approval limits

def manager_only(c, on=True):
    hm, _ = login(c, "manager", "manage123")
    body = {"overrides": {"approval": {"above_limit_approver": "MANAGER"}} if on else {}}
    assert c.put("/api/v1/config/rules", headers=hm, json=body).status_code == 200


def test_above_limit_reviewer_can_approve_by_default(db):
    inv, r = run(db, "26_limit_above_manager.pdf")
    assert inv.decision == "MANUAL_REVIEW" and inv.approval_level == "REVIEW" and "M18" in rules_of(r)
    assert "reviewer's or manager's approval" in r.controls["approval"]["message"]
    c = TestClient(app)
    h, _ = login(c)
    d = c.get(f"/api/v1/invoices/{inv.id}", headers=h).json()
    assert d["manager_required"] is False
    res = c.post(f"/api/v1/invoices/{inv.id}/final-decision", headers=h,
                 json={"decision": "APPROVED", "reason": "Capex approved in budget"})
    assert res.status_code == 200 and res.json()["invoice"]["decision"] == "APPROVED"


def test_above_limit_needs_manager_when_set(db):
    c = TestClient(app)
    manager_only(c)
    inv, r = run(db, "26_limit_above_manager.pdf")
    assert inv.decision == "MANUAL_REVIEW" and inv.approval_level == "MANAGER" and "M18" in rules_of(r)
    h, user = login(c)
    assert user["role"] == "REVIEWER"
    res = c.post(f"/api/v1/invoices/{inv.id}/final-decision", headers=h,
                 json={"decision": "APPROVED", "reason": "looks fine"})
    assert res.status_code == 403 and res.json()["error_code"] == "MANAGER_APPROVAL_REQUIRED"
    hm, mgr = login(c, "manager", "manage123")
    assert mgr["role"] == "MANAGER"
    res = c.post(f"/api/v1/invoices/{inv.id}/final-decision", headers=hm,
                 json={"decision": "APPROVED", "reason": "Capex approved in budget"})
    assert res.status_code == 200 and res.json()["invoice"]["decision"] == "APPROVED"
    ev = db.scalars(select(m.AuditEvent).where(m.AuditEvent.invoice_id == inv.id,
                                               m.AuditEvent.event_type == "FINAL_DECISION")).all()[-1]
    assert ev.actor == "manager" and ev.payload["role"] == "MANAGER"
    manager_only(c, False)


def test_setting_applies_to_invoices_already_in_review(db):
    """The setting at approval time counts, not the one the invoice was processed with."""
    inv, _ = run(db, "26_limit_above_manager.pdf")           # processed while reviewers may approve
    c = TestClient(app)
    manager_only(c)                                            # then the manager restricts it
    h, _ = login(c)
    assert c.get(f"/api/v1/invoices/{inv.id}", headers=h).json()["manager_required"] is True
    res = c.post(f"/api/v1/invoices/{inv.id}/final-decision", headers=h, json={"decision": "APPROVED", "reason": "ok ok"})
    assert res.status_code == 403
    manager_only(c, False)
    res = c.post(f"/api/v1/invoices/{inv.id}/final-decision", headers=h, json={"decision": "APPROVED", "reason": "ok ok"})
    assert res.status_code == 200
    bad = c.put("/api/v1/config/rules", headers=login(c, "manager", "manage123")[0],
                json={"overrides": {"approval": {"above_limit_approver": "ANYONE"}}})
    assert bad.status_code == 422


def test_reviewer_can_still_reject_above_limit(db):
    inv, _ = run(db, "26_limit_above_manager.pdf")
    c = TestClient(app)
    h, _ = login(c)
    res = c.post(f"/api/v1/invoices/{inv.id}/final-decision", headers=h, json={"decision": "REJECTED", "reason": "Not budgeted"})
    assert res.status_code == 200


def test_just_under_limit_flagged_and_split_reviewed(db):
    a, ra = run(db, "27_limit_just_under_a.pdf")
    assert a.decision == "APPROVED" and "JUST_UNDER_APPROVAL_LIMIT" in ra.decision["flags"]
    b, rb = run(db, "28_limit_just_under_b_split.pdf")
    assert b.decision == "MANUAL_REVIEW" and "SPLIT_BELOW_LIMIT" in rb.decision["flags"]
    assert b.approval_level == "REVIEW"


# ============================================================================ fraud signals

def test_bank_details_changed(db):
    inv, r = run(db, "29_fraud_bank_details_changed.pdf")
    assert inv.decision == "MANUAL_REVIEW" and "M20" in rules_of(r)
    bank = r.controls["signals"]["bank"]
    assert bank["status"] == "CHANGED" and "3344" in bank["message"] and "771100223344" not in bank["message"]
    assert "BANK_DETAILS_CHANGED" in inv.risk_flags


def test_invoice_dated_before_po(db):
    inv, r = run(db, "30_fraud_invoice_before_po.pdf")
    assert inv.decision == "MANUAL_REVIEW" and "M22" in rules_of(r)
    assert r.controls["signals"]["bank"]["status"] == "MATCH"


def test_unusual_amount_and_month_end(db):
    rules = effective_rules(db)
    past = [SimpleNamespace(document_type="INVOICE", decision="APPROVED", currency="INR", grand_total=Decimal(v),
                            bank_account=None) for v in ("40000", "42000", "41000")]
    vendor = {"id": 1, "bank_account": None, "bank_ifsc": None}
    match = {"selected_po": None}
    _, issues = fraud_checks(fake_inv(), "INVOICE", vendor, past, match, {}, rules, Decimal("200000"), "INR",
                             date(2026, 10, 31))
    codes = {i["code"]: i for i in issues}
    assert codes["UNUSUAL_AMOUNT"]["outcome"] == "MANUAL_REVIEW"
    assert codes["RECEIVED_AT_MONTH_END"]["outcome"] == "FLAG"
    _, issues = fraud_checks(fake_inv(), "INVOICE", vendor, past, match, {}, rules, Decimal("45000"), "INR",
                             date(2026, 10, 15))
    assert not issues
    # bank account seen on an earlier invoice is the reference when the vendor master has none
    past[0].bank_account, past[0].bank_ifsc, past[0].invoice_number, past[0].id = "111122223333", None, "X/1", 9
    sig, issues = fraud_checks(fake_inv(bank_account="999988887777"), "INVOICE", vendor, past, match, {}, rules,
                               Decimal("45000"), "INR", date(2026, 10, 15))
    assert sig["bank"]["status"] == "CHANGED" and "invoice X/1" in sig["bank"]["message"]


# ============================================================================ e-invoice

def test_einvoice_valid_qr_approved_with_tds(db):
    inv, r = run(db, "31_einvoice_valid_qr.pdf")
    e = r.controls["einvoice"]
    assert e["status"] == "OK" and e["qr_is_einvoice"] and all(c["ok"] for c in e["checks"]), e
    assert inv.decision == "APPROVED", r.decision
    assert inv.tds_section == "194C" and inv.tds_amount == Decimal("3600")
    assert r.controls["tds"]["net_payable"] == "208800.00"
    assert "2,08,800.00" in r.decision["next_action"]


def test_einvoice_missing(db):
    inv, r = run(db, "32_einvoice_missing_irn.pdf")
    assert inv.decision == "MANUAL_REVIEW" and r.controls["einvoice"]["status"] == "MISSING" and "M23" in rules_of(r)


def test_einvoice_qr_contradicts_printed_total(db):
    inv, r = run(db, "33_einvoice_qr_mismatch.pdf")
    e = r.controls["einvoice"]
    assert e["status"] == "MISMATCH" and inv.decision == "MANUAL_REVIEW"
    bad = [c for c in e["checks"] if not c["ok"]]
    assert [c["name"] for c in bad] == ["Invoice value"] and "EINVOICE_MISMATCH" in inv.risk_flags


# ============================================================================ TDS

def _vendor(db, code):
    v = db.scalar(select(m.Vendor).where(m.Vendor.code == code))
    return {"id": v.id, "country": v.country, "tds_section": v.tds_section}


def test_tds_thresholds(db):
    rules = effective_rules(db)
    me = SimpleNamespace(id=-1)
    small = fake_inv(grand_total="23600.00", tax_amount="3600.00", invoice_date="2026-11-01")
    t = tds_check(db, me, small, "INVOICE", _vendor(db, "V008"), "INR", rules, date(2026, 11, 1), None)
    assert t["applicable"] and t["amount"] == "0"          # 20,000 < 30,000 single and < 1,00,000 annual
    big = fake_inv(grand_total="47200.00", tax_amount="7200.00", invoice_date="2026-11-01")
    t = tds_check(db, me, big, "INVOICE", _vendor(db, "V008"), "INR", rules, date(2026, 11, 1), None)
    assert t["amount"] == "800" and t["net_payable"] == "46400.00"
    # 194Q: only the part of the year's purchases above the threshold is taxed
    rules["tds"]["sections"]["194Q"]["annual_threshold"] = 100000
    goods = fake_inv(grand_total="177000.00", tax_amount="27000.00", invoice_date="2026-11-01")
    t = tds_check(db, me, goods, "INVOICE", _vendor(db, "V001"), "INR", rules, date(2026, 11, 1), None)
    assert t["taxable"] == "50000.00" and t["amount"] == "50"   # 0.1% of the 50,000 above 1,00,000
    t = tds_check(db, me, goods, "INVOICE", _vendor(db, "V005"), "EUR", rules, date(2026, 11, 1), None)
    assert not t["applicable"]


# ============================================================================ master data / settings

def test_bank_details_change_is_manager_only(db):
    c = TestClient(app)
    h, _ = login(c)
    body = {"bank_account": "1234 5678 9012", "bank_ifsc": "HDFC0009999", "reason": "Vendor letter dated 1 Nov"}
    assert c.patch("/api/v1/vendors/V004", headers=h, json=body).status_code == 403
    assert c.patch("/api/v1/vendors/V004", headers=h, json={"tds_section": "194j", "reason": "services"}).status_code == 200
    hm, _ = login(c, "manager", "manage123")
    r = c.patch("/api/v1/vendors/V004", headers=hm, json=body)
    assert r.status_code == 200
    v = next(x for x in r.json() if x["code"] == "V004")
    assert v["bank_account"] == "123456789012" and v["tds_section"] == "194J"
    ev = db.scalar(select(m.AuditEvent).where(m.AuditEvent.event_type == "VENDOR_UPDATED")
                   .order_by(m.AuditEvent.id.desc()).limit(1))
    assert "123456789012" not in str(ev.payload) and ev.actor == "manager"
    assert c.patch("/api/v1/vendors/V004", headers=h, json={"tds_section": "999", "reason": "x x"}).status_code == 422


def test_flag_policy_only_for_flaggable_rules(db):
    c = TestClient(app)
    h, _ = login(c, "manager", "manage123")
    ok = c.put("/api/v1/config/rules", headers=h, json={"overrides": {"policy": {"near_limit": "MANUAL_REVIEW",
                                                                                "month_end_arrival": "FLAG"}}})
    assert ok.status_code == 200, ok.text
    bad = c.put("/api/v1/config/rules", headers=h, json={"overrides": {"policy": {"cancelled_po": "FLAG"}}})
    assert bad.status_code == 422
    c.put("/api/v1/config/rules", headers=h, json={"overrides": {}})


def test_only_manager_changes_approval_limits(db):
    c = TestClient(app)
    h, _ = login(c)
    new = {"approval": {"auto_approve_limit": {"INR": 900000}}}
    r = c.put("/api/v1/config/rules", headers=h, json={"overrides": new})
    assert r.status_code == 403 and r.json()["error_code"] == "MANAGER_REQUIRED"
    # a reviewer can still change other rules
    assert c.put("/api/v1/config/rules", headers=h, json={"overrides": {"tolerances": {"unit_price_pct": 6}}}).status_code == 200
    hm, _ = login(c, "manager", "manage123")
    r = c.put("/api/v1/config/rules", headers=hm, json={"overrides": new})
    assert r.status_code == 200 and r.json()["effective"]["approval"]["auto_approve_limit"]["INR"] == 900000
    ev = db.scalar(select(m.AuditEvent).where(m.AuditEvent.event_type == "APPROVAL_LIMITS_CHANGED")
                   .order_by(m.AuditEvent.id.desc()).limit(1))
    assert ev.actor == "manager"
    inv, _ = run(db, "26_limit_above_manager.pdf")      # 5,52,240 is now within the raised limit
    assert inv.approval_level == "AUTO"
    assert c.put("/api/v1/config/rules", headers=hm, json={"overrides": {}}).status_code == 200


def test_any_currency_gets_its_own_limit(db):
    from app.controls import approval_check
    from app.validation import check_currency

    rules = effective_rules(db)
    gbp = fake_inv(currency="GBP")
    assert check_currency(gbp, rules).status == "FAIL"          # not set up yet
    a, issues = approval_check(gbp, "INVOICE", Decimal("5000"), "GBP", [], rules, date(2026, 11, 1))
    assert a["level"] == "REVIEW" and issues[0]["code"] == "NO_APPROVAL_LIMIT"   # no limit -> a person approves, never auto

    c = TestClient(app)
    hm, _ = login(c, "manager", "manage123")
    r = c.put("/api/v1/config/rules", headers=hm, json={"overrides": {"approval": {"auto_approve_limit": {"GBP": 12000}}}})
    assert r.status_code == 200, r.text
    assert "GBP" in r.json()["supported_currencies"]
    rules = effective_rules(db)
    assert check_currency(gbp, rules).status == "PASS"
    a, issues = approval_check(gbp, "INVOICE", Decimal("5000"), "GBP", [], rules, date(2026, 11, 1))
    assert a["level"] == "AUTO" and not issues
    a, _ = approval_check(gbp, "INVOICE", Decimal("13000"), "GBP", [], rules, date(2026, 11, 1))
    assert a["level"] == "REVIEW" and "£13,000.00" in a["message"]
    # a PO can now be raised in GBP
    po = {"po_number": "PO-GB1", "vendor_code": "V004", "po_date": "2026-10-01", "currency": "GBP",
          "lines": [{"description": "Licence", "quantity": "1", "unit_price": "900"}]}
    assert c.post("/api/v1/purchase-orders", headers=hm, json=po).status_code == 201
    # removing a currency's limit (null) sends its invoices to a manager again
    c.put("/api/v1/config/rules", headers=hm, json={"overrides": {"approval": {"auto_approve_limit": {"USD": None}}}})
    a, _ = approval_check(fake_inv(), "INVOICE", Decimal("10"), "USD", [], effective_rules(db), date(2026, 11, 1))
    assert a["level"] == "REVIEW"
    for bad in ({"gbp": 1}, {"POUND": 1}, {"GBP": -5}, {"GBP": "lots"}):
        r = c.put("/api/v1/config/rules", headers=hm, json={"overrides": {"approval": {"auto_approve_limit": bad}}})
        assert r.status_code == 422, bad
    c.put("/api/v1/config/rules", headers=hm, json={"overrides": {}})
