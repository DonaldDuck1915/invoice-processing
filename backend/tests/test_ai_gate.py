"""The AI reader is skipped only for clean digital invoices (all critical fields read, every total reconciles)."""
from datetime import date

from app.ingestion import ingest
from app.pipeline import process_invoice

from .conftest import SAMPLES, FakeLLM
from .test_scenarios import needs_ocr

TODAY = date(2026, 11, 10)


class CountingLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.extractions = 0

    def json_call(self, purpose, *a, **kw):
        if purpose == "extraction":
            self.extractions += 1
        return super().json_call(purpose, *a, **kw)


def run(db, name, llm, trigger="INITIAL"):
    inv = ingest(db, (SAMPLES / name).read_bytes(), name, "application/pdf", "test")
    r = process_invoice(db, inv.id, trigger, llm=llm, today=TODAY)
    db.refresh(inv)
    return inv, r


def test_clean_digital_invoice_skips_the_ai_reader(db):
    llm = CountingLLM()
    inv, r = run(db, "01_happy_path_digital.pdf", llm)
    meta = r.extraction["result"]["meta"]
    assert llm.extractions == 0 and meta["ai_skipped"] and "reconciles" in meta["ai_skip_reason"]
    assert inv.decision == "APPROVED" and "AI_NOT_NEEDED" in r.decision["flags"]
    assert "AI_NOT_USED" not in r.decision["flags"]


def test_invoice_whose_totals_do_not_reconcile_gets_the_ai(db):
    llm = CountingLLM()
    _, r = run(db, "19_review_missing_number_math_error.pdf", llm)     # no number + a line-maths error
    assert llm.extractions == 1 and not r.extraction["result"]["meta"]["ai_skipped"]


def test_clean_invoice_without_po_is_still_skipped(db):
    llm = CountingLLM()
    _, r = run(db, "12_ec4_no_po_ambiguous.pdf", llm)                  # clean -> skipped
    assert llm.extractions == 0


@needs_ocr
def test_scanned_invoice_always_gets_the_ai(db):
    llm = CountingLLM()
    run(db, "02_happy_path_scanned.pdf", llm)
    assert llm.extractions == 1


def test_reextract_and_setting_force_the_ai(db, monkeypatch):
    from app.config import get_settings

    llm = CountingLLM()
    inv, _ = run(db, "01_happy_path_digital.pdf", llm)
    process_invoice(db, inv.id, "RE_EXTRACT", llm=llm, today=TODAY)
    assert llm.extractions == 1                                          # a reviewer's re-extract asks the AI
    monkeypatch.setattr(get_settings(), "ai_extraction", "always")
    run(db, "03_ec1_split_a_60_chairs.pdf", llm)
    assert llm.extractions == 2
