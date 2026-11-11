"""End-to-end scenarios: every demo PDF through the full pipeline (rules-only extraction)."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import models as m
from app.extraction.pdf_reader import DocumentError
from app.ingestion import ingest
from app.llm import LLMClient
from app.pipeline import confirm_po, correct_fields, final_decision, process_invoice, resolve_duplicate

from .conftest import SAMPLES, FakeLLM

TODAY = date(2026, 11, 10)


def run(db, name, llm=None, filename=None):
    data = (SAMPLES / name).read_bytes()
    inv = ingest(db, data, filename or name, "application/pdf", "test")
    r = process_invoice(db, inv.id, llm=llm or LLMClient(), today=TODAY)
    db.refresh(inv)
    return inv, r


def rules_of(r):
    return {x["rule"] for x in r.decision["reasons"]}


def test_happy_path_digital_approved(db):
    inv, r = run(db, "01_happy_path_digital.pdf")
    assert inv.decision == "APPROVED", r.decision
    assert r.matching["method"] == "EXPLICIT" and r.matching["selected_po"] == "PO-2005"
    assert all(not v["exceeded"] for v in r.matching["variances"])


def test_happy_path_scanned_ocr_approved(db):
    inv, r = run(db, "02_happy_path_scanned.pdf")
    assert inv.is_scanned
    assert inv.decision == "APPROVED", r.decision


def test_ec1_split_invoices_track_cumulative_consumption(db):
    a, ra = run(db, "03_ec1_split_a_60_chairs.pdf")
    b, rb = run(db, "04_ec1_split_b_40_chairs.pdf")
    assert a.decision == b.decision == "APPROVED"
    qty_var = next(v for v in rb.matching["variances"] if v["field"] == "quantity")
    assert qty_var["expected"] == "40" and qty_var["actual"] == "40"  # remaining after invoice A
    rows = db.scalars(select(m.POConsumption).where(m.POConsumption.status == "ACTIVE")).all()
    po_line = db.scalar(select(m.POLine).join(m.PurchaseOrder).where(m.PurchaseOrder.po_number == "PO-2001"))
    assert sum(r.quantity for r in rows if r.po_line_id == po_line.id) == Decimal("100")
    # re-processing A must not double count it
    process_invoice(db, a.id, "REPROCESS", llm=LLMClient(), today=TODAY)
    rows = db.scalars(select(m.POConsumption).where(m.POConsumption.status == "ACTIVE",
                                                    m.POConsumption.po_line_id == po_line.id)).all()
    assert sum(r.quantity for r in rows) == Decimal("100") and len(rows) == 2
    c, rc = run(db, "05_ec1_split_c_overbill_5_chairs.pdf")
    assert c.decision == "REJECTED" and "R6" in rules_of(rc)


def test_ec2_duplicate_with_modified_details(db):
    orig, _ = run(db, "06_ec2_original_eur.pdf")
    assert orig.decision == "APPROVED"
    resend, r = run(db, "07_ec2_resend_scanned_ocr_number.pdf")
    assert r.duplicates["status"] == "PROBABLE"
    assert resend.decision == "MANUAL_REVIEW" and "M4" in rules_of(r)
    assert "R6" not in rules_of(r)  # over-consumption is attributed to the duplicate, not hard-rejected
    exact, r2 = run(db, "06_ec2_original_eur.pdf", filename="renamed-copy.pdf")
    assert r2.duplicates["status"] == "CONFIRMED" and exact.decision == "REJECTED"


def test_ec2_recurring_same_amount_is_not_duplicate(db):
    a, _ = run(db, "08_ec2_recurring_jan.pdf")
    b, r = run(db, "09_ec2_recurring_feb_same_amount.pdf")
    assert r.duplicates["status"] == "NEW"
    assert b.decision == "APPROVED", r.decision


def test_ec3_price_over_tolerance_goes_to_review(db):
    inv, r = run(db, "10_ec3_price_8pct_over.pdf")
    assert inv.decision == "MANUAL_REVIEW" and "M5" in rules_of(r)
    v = next(v for v in r.matching["variances"] if v["field"] == "unit_price")
    assert (v["expected"], v["actual"], v["pct_diff"], v["tolerance"], v["exceeded"]) == \
           ("1000", "1080.00", "8.00", "5", True)
    assert v["invoice_line_no"] == 1
    ok, r2 = run(db, "11_ec3_price_3pct_within.pdf")
    assert ok.decision == "APPROVED", r2.decision


def test_ec4_ambiguous_missing_po_requires_confirmation(db):
    inv, r = run(db, "12_ec4_no_po_ambiguous.pdf", llm=FakeLLM())
    assert inv.decision == "MANUAL_REVIEW" and r.matching["ambiguous"] and r.matching["selected_po"] is None
    scores = {c["po_number"]: c["score"] for c in r.matching["candidates"]}
    assert set(scores) == {"PO-101", "PO-102", "PO-103"}
    po = db.scalar(select(m.PurchaseOrder).where(m.PurchaseOrder.po_number == "PO-101"))
    r2 = confirm_po(db, inv, po, "Confirmed with vendor by phone", "test", llm=FakeLLM())
    assert r2.matching["method"] == "HUMAN_CONFIRMED"
    db.refresh(inv)
    # the single bundled line has no counterpart on PO-101, so it still needs a human decision
    assert inv.decision == "MANUAL_REVIEW"
    final_decision(db, inv, "APPROVED", "Bundled furniture order verified against delivery note", "test")
    db.refresh(inv)
    assert inv.decision == "APPROVED" and inv.decided_by == "HUMAN"


def test_ec4_clear_lines_auto_selected_with_ai_but_not_without(db):
    inv, r = run(db, "13_ec4_no_po_clear_lines.pdf", llm=FakeLLM())
    assert r.matching["selected_po"] == "PO-101" and r.matching["method"] == "INFERRED"
    assert inv.decision == "APPROVED", r.decision


def test_ec4_clear_lines_without_ai_needs_confirmation(db):
    inv, r = run(db, "13_ec4_no_po_clear_lines.pdf")
    assert r.matching["proposed_po"] == "PO-101" and r.matching["selected_po"] is None
    assert inv.decision == "MANUAL_REVIEW"


@pytest.mark.parametrize("name,decision,rule", [
    ("14_reject_cancelled_po.pdf", "REJECTED", "R2"),
    ("15_reject_po_of_other_vendor.pdf", "REJECTED", "R5"),
    ("16_review_blocked_vendor.pdf", "MANUAL_REVIEW", "M9"),
    ("17_review_pending_vendor.pdf", "MANUAL_REVIEW", "M9"),
    ("18_review_currency_mismatch.pdf", "MANUAL_REVIEW", "M8"),
    ("19_review_missing_number_math_error.pdf", "MANUAL_REVIEW", "M1"),
    ("20_review_low_quality_scan.pdf", "MANUAL_REVIEW", "M2"),
])
def test_rule_scenarios(db, name, decision, rule):
    inv, r = run(db, name)
    assert inv.decision == decision, r.decision
    assert rule in rules_of(r)


def test_blocked_vendor_is_flagged(db):
    _, r = run(db, "16_review_blocked_vendor.pdf")
    assert "VENDOR_BLOCKED" in r.decision["flags"]


def test_math_error_also_flagged(db):
    _, r = run(db, "19_review_missing_number_math_error.pdf")
    assert "M7" in rules_of(r)


def test_closed_po_with_no_balance_rejected(db):
    """PO-2004 is closed and fully consumed by a seeded historical invoice."""
    inv, r = run(db, "10_ec3_price_8pct_over.pdf")
    corr = correct_fields(db, inv, {"po_number": "PO-2004"}, None, "Vendor says it was for the drum order", "test")
    db.refresh(inv)
    assert inv.decision == "REJECTED" and "R3" in rules_of(corr)


def test_correction_creates_new_run_and_keeps_history(db):
    inv, r = run(db, "19_review_missing_number_math_error.pdf")
    r2 = correct_fields(db, inv, {"invoice_number": "ASC-10233"},
                        [{"description": "Safety helmet ANSI Z89.1", "item_code": "SH-100", "quantity": "50",
                          "unit_price": "18.50", "line_total": "925.00"},
                         {"description": "Cut resistant gloves (pair)", "item_code": "GL-220", "quantity": "100",
                          "unit_price": "6.25", "line_total": "625.00"}],
                        "Vendor confirmed number and corrected line 2", "test")
    assert r2.run_no == 2
    fields = r2.extraction["result"]["fields"]
    assert fields["invoice_number"]["status"] == "CORRECTED"
    assert "M1" not in rules_of(r2)
    db.refresh(inv)
    # the printed subtotal/grand total still reflect the vendor's arithmetic error
    assert inv.decision == "MANUAL_REVIEW"
    events = db.scalars(select(m.AuditEvent.event_type).where(m.AuditEvent.invoice_id == inv.id)).all()
    assert "HUMAN_ACTION" in events and events.count("DECISION_GENERATED") == 2


def test_resolve_probable_duplicate_as_duplicate(db):
    run(db, "06_ec2_original_eur.pdf")
    resend, _ = run(db, "07_ec2_resend_scanned_ocr_number.pdf")
    r = resolve_duplicate(db, resend, "IS_DUPLICATE", "Same goods and date - vendor resent", "test")
    db.refresh(resend)
    assert resend.decision == "REJECTED" and "R1" in rules_of(r)


@pytest.mark.parametrize("name,code", [("21_error_corrupt.pdf", None), ("22_error_not_a_pdf.pdf", "NOT_A_PDF")])
def test_bad_documents_rejected_gracefully(db, name, code):
    with pytest.raises(DocumentError) as e:
        ingest(db, (SAMPLES / name).read_bytes(), name, "application/pdf", "test")
    if code:
        assert e.value.code == code


def test_audit_trail_has_every_stage(db):
    inv, _ = run(db, "01_happy_path_digital.pdf")
    events = db.scalars(select(m.AuditEvent.event_type).where(m.AuditEvent.invoice_id == inv.id)
                        .order_by(m.AuditEvent.seq)).all()
    for stage in ["DOCUMENT_UPLOADED", "DATA_EXTRACTED", "VALIDATION_COMPLETED", "PO_IDENTIFIED", "MATCHING_COMPLETED",
                  "DUPLICATE_CHECK_COMPLETED", "DECISION_GENERATED", "PO_CONSUMPTION_RECORDED"]:
        assert stage in events
