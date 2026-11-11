from datetime import date
from decimal import Decimal

from app.duplicates import invoice_number_similarity
from app.extraction.parsers import gstin_is_valid, make_gstin, parse_date, repair_gstin
from app.money import fmt_money, parse_amount, pct_diff


def test_parse_amount_formats():
    assert parse_amount("5,00,000.00") == Decimal("500000.00")
    assert parse_amount("1,234.56") == Decimal("1234.56")
    assert parse_amount("9.800,00") == Decimal("9800.00")
    assert parse_amount("Rs. 4500/-") == Decimal("4500")
    assert parse_amount("€ 1.234.567,89") == Decimal("1234567.89")
    assert parse_amount("(120.00)") == Decimal("-120.00")
    assert parse_amount("no number") is None


def test_money_is_decimal_exact():
    assert pct_diff(Decimal("1000"), Decimal("1080")) == Decimal("8.00")
    assert fmt_money(Decimal("500000"), "INR") == "₹5,00,000.00"
    assert fmt_money(Decimal("1566.5"), "USD") == "$1,566.50"


def test_gstin_checksum_and_ocr_repair():
    g = make_gstin("07", "AAKCT9087N")
    assert gstin_is_valid(g)
    assert not gstin_is_valid(g[:-1] + ("A" if g[-1] != "A" else "B"))
    assert repair_gstin(g.replace("0", "O", 1)) == g        # O/0 confusion
    assert repair_gstin(g[:13] + "2" + g[13:]) == g          # inserted character ('1Z' read as '12Z')


def test_parse_date_conventions():
    assert parse_date("14/03/2026") == (date(2026, 3, 14), False)
    assert parse_date("03/04/2026", "DMY") == (date(2026, 4, 3), True)
    assert parse_date("03/04/2026", "MDY") == (date(2026, 3, 4), True)
    assert parse_date("14-Oct-2026")[0] == date(2026, 10, 14)
    assert parse_date("March 5, 2026")[0] == date(2026, 3, 5)


def test_invoice_number_similarity_distinguishes_ocr_from_sequence():
    assert invoice_number_similarity("INV-0045", "INV-0O45", 2)[0] >= 0.95      # O vs 0
    assert invoice_number_similarity("RI-2026-0045", "RI-2026-8045", 2)[0] >= 0.85  # 0 vs 8 misread
    assert invoice_number_similarity("GLF/HK/2026/10", "GLF/HK/2026/11", 2)[0] <= 0.1  # next in sequence


def test_ai_merge_policy_never_trusts_unverified_values():
    from app.extraction.service import finalize, merge
    from app.schemas import ExtractedInvoice, FieldValue

    rule = ExtractedInvoice(fields={
        "invoice_number": FieldValue(value="INV-1", raw="INV-1", confidence=0.99, status="EXTRACTED", source="text_layer"),
        "grand_total": FieldValue(value="100.00", raw="100.00", confidence=0.99, status="EXTRACTED", source="text_layer"),
    }, meta={"date_order": "DMY"})

    def f(v, verified):
        return {"value": v, "source_quote": v, "verified": verified}

    ai = {"fields": {"invoice_number": f("INV-1", True),          # agrees -> high confidence
                     "grand_total": f("118.00", True),            # disagrees -> UNCERTAIN
                     "po_number": f("PO-77", True),               # only AI, verified -> EXTRACTED
                     "due_date": f("2026-12-01", False)},         # only AI, not in document -> UNCERTAIN
          "line_items": [], "tax_components": [], "date_format_hint": "DMY", "notes": ""}
    out = finalize(merge(rule, ai, scanned=False), 0.8)
    assert out.fields["invoice_number"].status == "EXTRACTED" and out.fields["invoice_number"].confidence >= 0.95
    assert out.fields["grand_total"].status == "UNCERTAIN" and "disagree" in out.fields["grand_total"].note
    assert out.fields["po_number"].status == "EXTRACTED" and out.fields["po_number"].source == "llm"
    assert out.fields["due_date"].status == "UNCERTAIN"
    assert out.fields["vendor_tax_id"].status == "MISSING"
