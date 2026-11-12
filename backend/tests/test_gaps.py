"""Tax-inclusive invoices, reviewer decisions on re-runs, PO balance on human approval, concurrency,
interrupted-run recovery, CSV PO import, batch upload, e-mail intake, live progress, rules validation."""
import json
import threading
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.api import routes
from app.csv_import import TEMPLATE, parse_procurement_csv
from app.db import SessionLocal
from app.email_intake import ingest_message, pdf_attachments
from app.ingestion import ingest
from app.jobs import recover_interrupted
from app.llm import LLMClient, LLMUnavailable
from app.main import app
from app.pipeline import POBalanceExceeded, final_decision, process_invoice
from app.seed import import_procurement
from app.tax_mode import detect_tax_inclusive

from .conftest import SAMPLES
from .test_scenarios import TODAY, rules_of, run


def login(c):
    r = c.post("/api/v1/auth/login", json={"username": "reviewer", "password": "review123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ----------------------------------------------------------------------------- tax-inclusive (EC5)

@pytest.mark.parametrize("text,hit", [
    ("All prices are inclusive of GST.", True), ("Rates incl. VAT", True), ("Includes IGST @ 18%", True),
    ("Total amount (taxes included)", True), ("TAX INVOICE  Sub Total  CGST @ 9%", False),
])
def test_tax_inclusive_detection(text, hit):
    assert (detect_tax_inclusive(text) is not None) == hit


def test_tax_inclusive_invoice_matched_on_net_values(db):
    inv, r = run(db, "23_ec5_tax_inclusive_prices.pdf")
    assert inv.decision == "APPROVED", r.decision
    checks = {c["code"]: c for c in r.validation}
    assert checks["V3.tax_mode"]["status"] == "PASS" and checks["V3.tax"]["status"] == "PASS"
    assert checks["V5.grand_total"]["status"] == "PASS"
    price = next(v for v in r.matching["variances"] if v["field"] == "unit_price")
    assert (price["expected"], price["actual"], price["exceeded"]) == ("2500", "2500", False)  # 2950 gross / 1.18
    assert r.matching["net_basis"]["net_subtotal"] == "12500.00"
    # the ledger records the net value, the same basis as the PO
    row = db.scalar(select(m.POConsumption).where(m.POConsumption.invoice_id == inv.id))
    assert str(row.amount) == "12500.00"
    # the printed (gross) figures are what the invoice header keeps
    assert str(inv.grand_total) == "14750.00"


# ----------------------------------------------------------------------------- reviewer decisions

def test_reprocess_keeps_reviewer_decision(db):
    inv, _ = run(db, "10_ec3_price_8pct_over.pdf")
    assert inv.decision == "MANUAL_REVIEW"
    final_decision(db, inv, "APPROVED", "Procurement approved the price increase", "test")
    r2 = process_invoice(db, inv.id, "REPROCESS", llm=LLMClient(), today=TODAY)
    db.refresh(inv)
    assert inv.decision == "APPROVED" and inv.decided_by == "HUMAN"
    assert inv.system_decision == "MANUAL_REVIEW" and r2.decision["decision"] == "MANUAL_REVIEW"
    events = db.scalars(select(m.AuditEvent.event_type).where(m.AuditEvent.invoice_id == inv.id)).all()
    assert "HUMAN_DECISION_KEPT" in events
    # the approval still counts against the PO after the re-run
    assert db.scalar(select(m.POConsumption).where(m.POConsumption.invoice_id == inv.id,
                                                    m.POConsumption.status == "ACTIVE")) is not None


def test_human_approval_cannot_overbill_po(db):
    run(db, "03_ec1_split_a_60_chairs.pdf")
    run(db, "04_ec1_split_b_40_chairs.pdf")
    over, r = run(db, "05_ec1_split_c_overbill_5_chairs.pdf")
    assert over.decision == "REJECTED" and "R6" in rules_of(r)
    with pytest.raises(POBalanceExceeded) as e:
        final_decision(db, over, "APPROVED", "Trying to force it through", "test")
    db.rollback()
    assert "PO-2001" in e.value.message and e.value.evidence[0]["remaining"] == "0"
    db.refresh(over)
    assert over.decision == "REJECTED"


def test_human_approval_rechecks_balance_consumed_since(db):
    """A review-queue invoice whose PO was used up by later approvals can no longer be approved."""
    review, _ = run(db, "10_ec3_price_8pct_over.pdf")  # 100 toner, price over tolerance -> review
    ok, _ = run(db, "11_ec3_price_3pct_within.pdf")    # 100 toner approved -> PO-2003 fully consumed
    assert review.decision == "MANUAL_REVIEW" and ok.decision == "APPROVED"
    with pytest.raises(POBalanceExceeded):
        final_decision(db, review, "APPROVED", "Price accepted", "test")
    db.rollback()


def test_api_returns_409_for_overbilling_approval(db):
    c = TestClient(app)
    h = login(c)
    run(db, "03_ec1_split_a_60_chairs.pdf")
    run(db, "04_ec1_split_b_40_chairs.pdf")
    over, _ = run(db, "05_ec1_split_c_overbill_5_chairs.pdf")
    r = c.post(f"/api/v1/invoices/{over.id}/final-decision", json={"decision": "APPROVED", "reason": "force"},
               headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "PO_BALANCE_EXCEEDED"


# ----------------------------------------------------------------------------- concurrency

def test_simultaneous_identical_uploads_are_not_both_approved(db):
    data = (SAMPLES / "01_happy_path_digital.pdf").read_bytes()
    ids = [ingest(db, data, f"copy-{i}.pdf", "application/pdf", "test").id for i in range(2)]
    barrier = threading.Barrier(2)

    def worker(invoice_id):
        s = SessionLocal()
        try:
            barrier.wait()
            process_invoice(s, invoice_id, llm=LLMClient(), today=TODAY)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    db.expire_all()
    decisions = sorted(db.get(m.Invoice, i).decision for i in ids)
    assert decisions == ["APPROVED", "REJECTED"], decisions


def test_interrupted_invoices_are_requeued(db):
    data = (SAMPLES / "01_happy_path_digital.pdf").read_bytes()
    inv = ingest(db, data, "stuck.pdf", "application/pdf", "test")
    inv.status, inv.stage = "PROCESSING", "MATCHING"   # as if the server died mid-run
    db.commit()
    ids = recover_interrupted()
    assert inv.id in ids
    for t in threading.enumerate():
        if t.name.startswith("process-"):
            t.join(120)
    db.expire_all()
    inv = db.get(m.Invoice, inv.id)
    assert inv.status == "PROCESSED" and inv.decision == "APPROVED"


# ----------------------------------------------------------------------------- live progress

def test_progress_endpoint_lists_stage_events(db):
    c = TestClient(app)
    h = login(c)
    pdf = (SAMPLES / "01_happy_path_digital.pdf").read_bytes()
    inv_id = c.post("/api/v1/invoices", files={"file": ("p.pdf", pdf, "application/pdf")}, headers=h).json()["id"]
    p = c.get(f"/api/v1/invoices/{inv_id}/progress", headers=h).json()
    types = [e["event_type"] for e in p["events"]]
    for t in ("DOCUMENT_UPLOADED", "DATA_EXTRACTED", "VALIDATION_COMPLETED", "MATCHING_COMPLETED",
              "DUPLICATE_CHECK_COMPLETED", "DECISION_GENERATED"):
        assert t in types
    assert p["invoice"]["stage"] == "DONE"


def test_stage_progress_is_committed_while_running(db, monkeypatch):
    """Another session must be able to see intermediate stages (that is what the live view polls)."""
    seen = []
    import app.pipeline as pipeline

    real = pipeline.check_duplicates

    def spy(*a, **kw):
        s = SessionLocal()
        try:
            inv = s.get(m.Invoice, invoice_id)
            seen.append((inv.status, inv.stage, s.scalar(select(m.ProcessingRun.matching).where(
                m.ProcessingRun.invoice_id == invoice_id)) is not None))
        finally:
            s.close()
        return real(*a, **kw)

    monkeypatch.setattr(pipeline, "check_duplicates", spy)
    invoice_id = ingest(db, (SAMPLES / "01_happy_path_digital.pdf").read_bytes(), "live.pdf", "application/pdf",
                        "test").id
    process_invoice(db, invoice_id, llm=LLMClient(), today=TODAY)
    assert seen == [("PROCESSING", "DUPLICATE_CHECK", True)]


def test_reprocess_runs_in_background_and_timestamps_carry_utc(db):
    c = TestClient(app)
    h = login(c)
    inv, _ = run(db, "01_happy_path_digital.pdf")
    r = c.post(f"/api/v1/invoices/{inv.id}/reprocess", json={"reason": "again"}, headers=h)
    assert r.status_code == 202 and r.json()["invoice"]["status"] == "PROCESSING"
    p = c.get(f"/api/v1/invoices/{inv.id}/progress", headers=h).json()  # background task has run by now
    assert p["invoice"]["status"] == "PROCESSED" and p["run_no"] == 2
    assert p["started_at"].endswith("+00:00")


def test_ai_timeout_is_not_waited_for_twice_in_one_run():
    class SlowGemini(LLMClient):
        tries = 0
        available = True
        provider = "gemini"

        def _call_gemini(self, system, blocks, schema, max_tokens, entry, effort="medium"):
            SlowGemini.tries += 1
            entry["error"] = "API error 504"
            raise LLMUnavailable("Gemini service error (504)")

    llm = SlowGemini()
    llm.settings = llm.settings.model_copy(update={"gemini_model": "main", "gemini_fast_model": "fast",
                                                   "gemini_fallback_model": None})
    for purpose in ("extraction", "decision_summary"):
        with pytest.raises(LLMUnavailable):
            llm.json_call(purpose, "sys", [{"type": "text", "text": purpose}], {"type": "object"})
    # extraction tried "main" then fell back to "fast"; both are now known to be down, so the
    # summary call waits for neither
    assert SlowGemini.tries == 2
    assert [c["model"] for c in llm.calls] == ["main", "fast", "fast", "main"]
    assert all("skipped" in c["error"] for c in llm.calls[2:])


def test_overloaded_model_falls_back_to_the_other_one():
    class BusyMain(LLMClient):
        available = True
        provider = "gemini"

        def _call_gemini(self, system, blocks, schema, max_tokens, entry, effort="medium"):
            if entry["model"] == "main":
                entry["error"] = "API error 503"
                raise LLMUnavailable("Gemini service error (503): high demand")
            return '{"ok": "yes"}'

    llm = BusyMain()
    llm.settings = llm.settings.model_copy(update={"gemini_model": "main", "gemini_fast_model": "fast",
                                                   "gemini_fallback_model": None})
    assert llm.json_call("extraction", "s", [{"type": "text", "text": "x"}], {"type": "object"}) == {"ok": "yes"}
    assert [(c["model"], c["ok"]) for c in llm.calls] == [("main", False), ("fast", True)]


def test_bad_key_is_not_retried_on_another_model():
    class BadKey(LLMClient):
        available = True
        provider = "gemini"
        tries = 0

        def _call_gemini(self, system, blocks, schema, max_tokens, entry, effort="medium"):
            BadKey.tries += 1
            entry["error"] = "API error 403"
            raise LLMUnavailable("Gemini rejected the API key - check GEMINI_API_KEY.")

    llm = BadKey()
    with pytest.raises(LLMUnavailable, match="API key"):
        llm.json_call("extraction", "s", [{"type": "text", "text": "x"}], {"type": "object"})
    assert BadKey.tries == 1


# ----------------------------------------------------------------------------- batch upload

def test_batch_upload_accepts_good_and_reports_bad(db):
    c = TestClient(app)
    h = login(c)
    files = [("files", ("a.pdf", (SAMPLES / "03_ec1_split_a_60_chairs.pdf").read_bytes(), "application/pdf")),
             ("files", ("b.pdf", (SAMPLES / "04_ec1_split_b_40_chairs.pdf").read_bytes(), "application/pdf")),
             ("files", ("bad.pdf", (SAMPLES / "22_error_not_a_pdf.pdf").read_bytes(), "application/pdf"))]
    r = c.post("/api/v1/invoices/batch", files=files, headers=h)
    assert r.status_code == 202, r.text
    body = r.json()
    assert len(body["accepted"]) == 2 and body["rejected"][0]["error_code"] == "NOT_A_PDF"
    decisions = [c.get(f"/api/v1/invoices/{a['id']}", headers=h).json()["invoice"]["decision"] for a in body["accepted"]]
    assert decisions == ["APPROVED", "APPROVED"]


# ----------------------------------------------------------------------------- CSV PO import

def test_csv_template_parses_into_one_po_with_two_lines():
    payload = parse_procurement_csv(TEMPLATE.encode())
    assert len(payload["purchase_orders"]) == 1 and len(payload["purchase_orders"][0]["lines"]) == 2
    assert payload["vendors"][0]["code"] == "V100"


def test_csv_accepts_spreadsheet_headers_and_semicolons(db):
    csv_text = ("PO No;Supplier Code;Supplier;GSTIN;Order Date;Item;Qty;Price;GST %\n"
                "PO-7001;V700;Delta Tools;27AAACD1234E1Z5;05/10/2026;Torque wrench;4;\"3,200\";18%\n")
    payload = parse_procurement_csv(csv_text.encode())
    res = import_procurement(db, payload)
    assert res == {"vendors_added": 1, "purchase_orders_added": 1}
    po = db.scalar(select(m.PurchaseOrder).where(m.PurchaseOrder.po_number == "PO-7001"))
    assert str(po.lines[0].unit_price) == "3200" and str(po.po_date) == "2026-10-05"


@pytest.mark.parametrize("csv_text,msg", [
    ("po_number,vendor_code\nPO-1,V1\n", "Missing column"),
    ("po_number,vendor_code,po_date,description,quantity,unit_price\nPO-1,V001,someday,x,1,2\n", "not a date"),
    ("po_number,vendor_code,po_date,description,quantity,unit_price\nPO-1,V001,2026-01-01,x,lots,2\n", "numbers"),
])
def test_csv_errors_name_the_problem(csv_text, msg):
    with pytest.raises(ValueError, match=msg):
        parse_procurement_csv(csv_text.encode())


def test_csv_import_endpoint(db):
    c = TestClient(app)
    h = login(c)
    r = c.post("/api/v1/admin/import-csv", files={"file": ("pos.csv", TEMPLATE.encode(), "text/csv")}, headers=h)
    assert r.status_code == 200 and r.json()["purchase_orders_added"] == 1, r.text
    r = c.post("/api/v1/admin/import-csv", files={"file": ("bad.csv", b"a,b\n1,2\n", "text/csv")}, headers=h)
    assert r.status_code == 422 and "Missing column" in r.json()["message"]


# ----------------------------------------------------------------------------- e-mail intake

def _mail(*attachments):
    msg = EmailMessage()
    msg["From"] = "Billing <billing@technova.example>"
    msg["Subject"] = "Invoice TN/26-27/0412"
    msg.set_content("Please find our invoice attached.")
    for name, data, ctype in attachments:
        maintype, subtype = ctype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg


def test_email_pdf_attachments_are_found():
    pdf = (SAMPLES / "01_happy_path_digital.pdf").read_bytes()
    msg = _mail(("inv.pdf", pdf, "application/pdf"), ("logo.png", b"\x89PNG....", "image/png"),
                ("scan", pdf, "application/octet-stream"))
    names = [n for n, _ in pdf_attachments(msg)]
    assert names == ["inv.pdf", "scan"]


def test_email_message_is_ingested_and_processed(db):
    pdf = (SAMPLES / "01_happy_path_digital.pdf").read_bytes()
    ids, errors = ingest_message(_mail(("inv.pdf", pdf, "application/pdf"),
                                       ("broken.pdf", b"not really a pdf" * 10, "application/pdf")).as_bytes())
    assert len(ids) == 1 and "broken.pdf" in errors[0]
    process_invoice(db, ids[0], llm=LLMClient(), today=TODAY)
    inv = db.get(m.Invoice, ids[0])
    db.refresh(inv)
    assert inv.decision == "APPROVED"
    ev = db.scalar(select(m.AuditEvent).where(m.AuditEvent.invoice_id == inv.id,
                                              m.AuditEvent.event_type == "RECEIVED_BY_EMAIL"))
    assert "billing@technova.example" in ev.message


def test_email_poll_without_configuration_is_explained(db):
    c = TestClient(app)
    h = login(c)
    r = c.post("/api/v1/admin/email/poll", headers=h)
    assert r.status_code == 409 and "not configured" in r.json()["message"]


# ----------------------------------------------------------------------------- rules + login hardening

@pytest.mark.parametrize("overrides,msg", [
    ({"policy": {"cancelled_po": "FOO"}}, "MANUAL_REVIEW"),
    ({"policy": {"allow_under_billing": "yes"}}, "true or false"),
    ({"extraction": {"min_confidence": 5}}, "between 0 and 1"),
    ({"duplicates": {"weights": {"invoice_number": 0.9}}}, "add up to 1"),
    ({"company": {"tax_ids": "27AAB"}}, "list"),
])
def test_rules_validation_rejects_bad_values(db, overrides, msg):
    c = TestClient(app)
    h = login(c)
    r = c.put("/api/v1/config/rules", json={"overrides": overrides}, headers=h)
    assert r.status_code == 422 and msg in r.json()["message"], r.text


def test_valid_rule_override_is_saved(db):
    c = TestClient(app)
    h = login(c)
    r = c.put("/api/v1/config/rules", json={"overrides": {"policy": {"cancelled_po": "MANUAL_REVIEW"},
                                                         "tolerances": {"unit_price_pct": 8}}}, headers=h)
    assert r.status_code == 200 and r.json()["effective"]["tolerances"]["unit_price_pct"] == 8


def test_login_is_throttled_after_repeated_failures(db):
    c = TestClient(app)
    try:
        codes = [c.post("/api/v1/auth/login", json={"username": "reviewer", "password": "wrong"}).status_code
                 for _ in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        # even the right password is refused while locked out
        assert c.post("/api/v1/auth/login", json={"username": "reviewer", "password": "review123"}).status_code == 429
    finally:
        routes._failed_logins.clear()


def test_health_reports_demo_defaults(db):
    h = TestClient(app).get("/api/v1/health").json()
    assert "REVIEWER_PASSWORD" in h["insecure_defaults"]  # tests run with the demo password
    assert h["email_intake"] is False


# ----------------------------------------------------------------------------- AI recommendation

class AdvisorLLM(LLMClient):
    """Returns a fixed summary + recommendation so the guardrails can be tested."""
    available = True
    provider = "gemini"

    def __init__(self, rec):
        super().__init__()
        self.rec = rec

    def json_call(self, purpose, system, user_content, schema, effort="medium", max_tokens=16000):
        if purpose == "decision_summary":
            self.facts = json.loads(user_content[0]["text"])
            return {"summary": "Summary.", "recommendation": self.rec}
        raise LLMUnavailable("not simulated")


def _rec(**kw):
    base = {"action": "CONFIRM_PO", "confidence": "medium", "rationale": "Lines point to PO-101.",
            "steps": ["Open PO matching", "Confirm PO-101"], "suggested_po": "PO-101", "vendor_message": None}
    return {**base, **kw}


def test_recommendation_for_review_invoice_is_stored_and_grounded(db):
    llm = AdvisorLLM(_rec())
    inv, r = run(db, "12_ec4_no_po_ambiguous.pdf", llm=llm)
    assert inv.decision == "MANUAL_REVIEW"
    rec = r.ai_summary["recommendation"]
    assert rec["action"] == "CONFIRM_PO" and rec["suggested_po"] == "PO-101" and rec["action_label"]
    # the AI received the candidate POs and the reasons, nothing it has to guess
    assert {c["po_number"] for c in llm.facts["po_candidates"]} >= {"PO-101", "PO-103"}
    assert llm.facts["decision"] == "MANUAL_REVIEW"


def test_recommendation_guardrails(db):
    _, r = run(db, "12_ec4_no_po_ambiguous.pdf", llm=AdvisorLLM(_rec(suggested_po="PO-9999")))
    rec = r.ai_summary["recommendation"]
    assert rec["suggested_po"] is None and "not one of this vendor's candidate POs" in rec["guard_warnings"][0]
    _, r = run(db, "14_reject_cancelled_po.pdf", llm=AdvisorLLM(_rec(action="APPROVE", suggested_po=None)))
    rec = r.ai_summary["recommendation"]
    assert rec["action"] == "ESCALATE" and "rejected invoice" in rec["guard_warnings"][0]


def test_no_recommendation_for_approved_invoice(db):
    inv, r = run(db, "01_happy_path_digital.pdf", llm=AdvisorLLM(_rec(action="REJECT", suggested_po=None)))
    assert inv.decision == "APPROVED" and r.ai_summary["recommendation"] is None
    assert r.ai_summary["text"] == "Summary."
