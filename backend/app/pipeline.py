"""Processing pipeline orchestration and human-review actions.

Stages: extraction -> vendor resolution -> validation -> PO matching -> duplicate check -> decision
-> ledger -> AI summary. Every stage writes an audit event. Each run stores its full results and the
rules snapshot it used. Corrections / PO confirmation / duplicate resolution start a new run that
re-uses the stored extraction (no new AI extraction call).
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from . import models as m
from .audit import log
from .controls import run_controls
from .config import deep_merge, get_settings, load_default_rules, rules_hash
from .decision import decide
from .duplicates import check_duplicates, fingerprint
from .extraction.parsers import normalize_ref
from .extraction.pdf_reader import DocumentError
from .extraction.service import apply_corrections, extract, finalize
from .ledger import consumption_by_line, po_snapshot, record_consumption, reverse_consumption
from .llm import LLMClient
from .matching.po_matcher import match_po
from .matching.semantic import SemanticScorer
from .matching.vendor import resolve_vendor
from .money import D
from .notes import document_type, process_note
from .schemas import ExtractedInvoice
from .summary import ai_summary
from .tax_mode import net_view
from .validation import run_validation

log_ = logging.getLogger(__name__)

# Matching -> duplicate check -> decision -> ledger must see a consistent PO ledger and the full
# invoice history, so that section runs for one invoice at a time. Extraction (the slow part,
# OCR / AI) still runs in parallel.
_LEDGER_LOCK = threading.RLock()
_PG_LOCK_KEY = 74_240_001


class LedgerBusy(Exception):
    pass


class LedgerSection:
    """Process-wide lock, plus a Postgres advisory lock so several API workers also serialise.

    The advisory lock lives on a dedicated connection held for the whole section. (Taking it through the
    request's Session was a bug: the pipeline commits after every stage, which hands the Session's
    connection back to the pool still holding the lock, and the unlock could then run on another
    connection - leaking the lock and blocking every later invoice at the matching stage.)"""

    WAIT_SECONDS = 180

    def __init__(self, db: Session):
        self.db = db
        self.pg = db.get_bind().dialect.name == "postgresql"
        self.conn = None

    def __enter__(self):
        if not _LEDGER_LOCK.acquire(timeout=self.WAIT_SECONDS):
            raise LedgerBusy("Another invoice has held the PO ledger for over 3 minutes; try again.")
        if self.pg:
            try:
                self.conn = self.db.get_bind().connect()
                deadline = time.monotonic() + self.WAIT_SECONDS
                while not self.conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _PG_LOCK_KEY}).scalar():
                    if time.monotonic() > deadline:
                        raise LedgerBusy("Another worker has held the PO ledger for over 3 minutes; try again.")
                    time.sleep(0.25)
            except BaseException:
                self._close()
                _LEDGER_LOCK.release()
                raise
        return self

    def _close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    def __exit__(self, *exc):
        try:
            if self.conn is not None:
                try:
                    self.conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _PG_LOCK_KEY})
                    self.conn.commit()
                except Exception:  # noqa: BLE001 - never return a connection that may still hold the lock
                    self.conn.invalidate()
                self._close()
        finally:
            _LEDGER_LOCK.release()


class ManagerApprovalRequired(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class POBalanceExceeded(Exception):
    def __init__(self, message: str, evidence: list[dict]):
        super().__init__(message)
        self.message, self.evidence = message, evidence


def _stage(db: Session, invoice: m.Invoice, stage: str) -> None:
    """Commit progress so the live run view (another request) sees each stage as it starts."""
    invoice.stage = stage
    db.commit()


def effective_rules(db: Session) -> dict:
    base = load_default_rules()
    ov = db.scalar(select(m.RuleOverride).order_by(m.RuleOverride.id.desc()).limit(1))
    return deep_merge(base, ov.overrides) if ov else base


def latest_run(db: Session, invoice_id: int) -> m.ProcessingRun | None:
    return db.scalar(select(m.ProcessingRun).where(m.ProcessingRun.invoice_id == invoice_id)
                     .order_by(m.ProcessingRun.run_no.desc()).limit(1))


def _vendor_dict(v: m.Vendor) -> dict:
    return {"id": v.id, "code": v.code, "name": v.name, "tax_id": v.tax_id, "tax_id_type": v.tax_id_type,
            "country": v.country, "status": v.status, "default_currency": v.default_currency,
            "bank_account": v.bank_account, "bank_ifsc": v.bank_ifsc, "einvoice_required": bool(v.einvoice_required),
            "tds_section": v.tds_section}


def _history(db: Session, invoice: m.Invoice) -> list[dict]:
    rows = db.scalars(select(m.Invoice).where(m.Invoice.id != invoice.id, m.Invoice.status == "PROCESSED")
                      .options(selectinload(m.Invoice.runs))).all()
    out = []
    for r in rows:
        if r.decision == "REJECTED":
            continue  # a rejected invoice may legitimately be re-submitted
        lines = []
        last = max(r.runs, key=lambda x: x.run_no) if r.runs else None
        if last and last.extraction:
            lines = [{"qty": D(li.get("quantity")), "amount": D(li.get("line_total"))}
                     for li in last.extraction["result"].get("line_items", [])]
        out.append({"id": r.id, "filename": r.original_filename, "file_sha256": r.file_sha256, "vendor_id": r.vendor_id,
                    "vendor_tax_id": r.vendor_tax_id, "invoice_number": r.invoice_number,
                    "invoice_date": r.invoice_date.isoformat() if r.invoice_date else None,
                    "grand_total": r.grand_total, "po_reference": r.po_reference, "lines": lines,
                    "content_fingerprint": r.content_fingerprint, "decision": r.decision,
                    "document_type": r.document_type or "INVOICE"})
    return out


@dataclass
class ReadResult:
    """What the read half (extraction + validation) hands to the decide half."""
    invoice: m.Invoice
    run: m.ProcessingRun
    inv: ExtractedInvoice
    vres: dict
    checks: list
    rules: dict
    llm: LLMClient
    rn: int
    today: date
    stage_errors: list[str] = field(default_factory=list)


def process_invoice(db: Session, invoice_id: int, trigger: str = "INITIAL", actor: str = "SYSTEM",
                    llm: LLMClient | None = None, today: date | None = None) -> m.ProcessingRun:
    """Read then decide one invoice. Batches use read_invoice / decide_invoice separately so several
    invoices are read in parallel while decisions stay in upload order (see jobs.process_many)."""
    res = read_invoice(db, invoice_id, trigger, actor, llm, today)
    return decide_invoice(db, res) if isinstance(res, ReadResult) else res


def _fail(db: Session, invoice_id: int, rn: int, e: Exception) -> None:
    db.rollback()
    invoice = db.get(m.Invoice, invoice_id)
    if isinstance(e, DocumentError):
        invoice.status, invoice.stage, invoice.error = "FAILED", "FAILED", e.message
        log(db, invoice.id, "PROCESSING_FAILED", e.message, {"code": e.code}, rn)
    else:
        log_.exception("pipeline failed")
        invoice.status, invoice.stage, invoice.error = "FAILED", "FAILED", f"Unexpected error: {e}"
        log(db, invoice.id, "PROCESSING_FAILED", f"Unexpected error: {e}", {"trace": traceback.format_exc()[-2000:]}, rn)
    db.commit()


def read_invoice(db: Session, invoice_id: int, trigger: str = "INITIAL", actor: str = "SYSTEM",
                 llm: LLMClient | None = None, today: date | None = None) -> ReadResult | m.ProcessingRun:
    """The slow, independent half: extraction (OCR / AI) -> vendor -> validation. Safe to run for
    several invoices at once - it only reads master data and writes this invoice's own run.
    Returns a ReadResult, or the run (already marked FAILED) when the document cannot be processed."""
    settings = get_settings()
    llm = llm or LLMClient()
    today = today or date.today()
    invoice = db.get(m.Invoice, invoice_id)
    rules = effective_rules(db)
    prev = latest_run(db, invoice.id)
    run = m.ProcessingRun(invoice_id=invoice.id, run_no=(prev.run_no if prev else 0) + 1, trigger=trigger,
                          rules_snapshot=rules, rules_hash=rules_hash(rules))
    db.add(run)
    invoice.status, invoice.stage, invoice.error = "PROCESSING", "EXTRACTING", None
    db.flush()
    rn = run.run_no
    log(db, invoice.id, "PROCESSING_STARTED", f"Run {rn} started ({trigger.lower().replace('_', ' ')}).",
        {"rules_hash": run.rules_hash, "ai_available": llm.available}, rn, actor)
    db.commit()
    stage_errors: list[str] = []
    try:
        # ------------------------------------------------------------------ extraction
        if trigger not in ("INITIAL", "RE_EXTRACT") and prev is not None and prev.extraction and prev.extraction.get("base"):
            base = ExtractedInvoice.model_validate(prev.extraction["base"])
            log(db, invoice.id, "EXTRACTION_REUSED", f"Re-used extraction from run {prev.run_no}.", {}, rn)
        else:
            doc = db.get(m.Document, invoice.document_id)
            try:
                # a reviewer's "re-extract" always asks the AI, even for a clean invoice
                base, doc_text = extract(doc.content, rules, llm, settings, force_ai=trigger == "RE_EXTRACT")
                invoice.is_scanned = doc_text.is_scanned
                invoice.page_count = doc_text.page_count
            except DocumentError:
                raise
            except Exception as e:  # noqa: BLE001 - extraction must never crash the pipeline
                log_.exception("extraction failed")
                stage_errors.append(f"Extraction failed: {e}")
                base = finalize(ExtractedInvoice(meta={"extractor": "failed", "error": str(e)}),
                                float(rules["extraction"]["min_confidence"]))
            counts = {}
            for f in base.fields.values():
                counts[f.status] = counts.get(f.status, 0) + 1
            log(db, invoice.id, "DATA_EXTRACTED",
                f"Extracted {counts.get('EXTRACTED', 0)} fields, {len(base.line_items)} line item(s); "
                f"{counts.get('MISSING', 0)} missing, {counts.get('UNCERTAIN', 0)} uncertain. "
                f"{'Scanned document (OCR)' if base.meta.get('is_scanned') else 'Digital text layer'}; "
                + ("AI reader not needed - " + base.meta["ai_skip_reason"] + "." if base.meta.get("ai_skipped") else
                   f"AI extraction {'used' if base.meta.get('ai_used') else 'not used' + (' - ' + base.meta['ai_error'] if base.meta.get('ai_error') else '')}."),
                {"status_counts": counts, "extractor": base.meta.get("extractor"),
                 "ai_skipped": bool(base.meta.get("ai_skipped")),
                 "ocr_mean_conf": base.meta.get("ocr_mean_conf")}, rn)
        inv = apply_corrections(base.model_copy(deep=True), invoice.corrections)
        inv = finalize(inv, float(rules["extraction"]["min_confidence"]))
        if invoice.corrections:
            log(db, invoice.id, "CORRECTIONS_APPLIED",
                f"Applied reviewer corrections to {len((invoice.corrections or {}).get('fields', {}))} field(s)"
                + (" and line items" if invoice.corrections.get("line_items") is not None else "") + ".",
                invoice.corrections, rn)
        for w in base.meta.get("warnings") or []:
            if "OCR" in w:
                stage_errors.append(w + " Install Tesseract (see README) or correct the fields manually.")
        if inv.meta.get("tax_inclusive"):
            log(db, invoice.id, "TAX_INCLUSIVE_DETECTED",
                f"Prices include tax ({inv.meta.get('tax_inclusive_evidence')!r}); net amounts are derived from the "
                "line tax rates for PO matching.", {"evidence": inv.meta.get("tax_inclusive_evidence")}, rn)
        run.extraction = {"base": base.model_dump(), "result": inv.model_dump()}
        _stage(db, invoice, "VALIDATING")

        # ------------------------------------------------------------------ vendor + validation
        vendors = [_vendor_dict(v) for v in db.scalars(select(m.Vendor))]
        vres = resolve_vendor(inv, vendors)
        checks = run_validation(inv, vres, rules, today)
        run.validation = [c.model_dump() for c in checks]
        fails = [c for c in checks if c.status == "FAIL"]
        log(db, invoice.id, "VALIDATION_COMPLETED",
            f"{sum(c.status == 'PASS' for c in checks)} passed, {len(fails)} failed, "
            f"{sum(c.status in ('WARN', 'SKIPPED') for c in checks)} warnings/skipped."
            + (f" Vendor: {vres['vendor']['name']} via {vres['method']}." if vres["vendor"] else " Vendor not identified."),
            {"failed": [c.code for c in fails]}, rn)
        _stage(db, invoice, "MATCHING")
        return ReadResult(invoice=invoice, run=run, inv=inv, vres=vres, checks=checks, rules=rules, llm=llm, rn=rn,
                          today=today, stage_errors=stage_errors)
    except Exception as e:  # noqa: BLE001 - DocumentError and anything unexpected end the run as FAILED
        _fail(db, invoice_id, rn, e)
        return run


def decide_invoice(db: Session, r: ReadResult, summarise: bool = True):
    """The fast, order-sensitive half: PO matching -> duplicates -> controls -> decision -> ledger.
    Runs for one invoice at a time (LedgerSection), in upload order within a batch. With summarise=False
    it returns the facts for summarise_invoice (which a batch runs in parallel), else the finished run."""
    try:
        with LedgerSection(db):
            run, facts = _match_decide_record(db, r.invoice, r.run, r.inv, r.vres, r.checks, r.rules, r.llm,
                                              r.stage_errors, r.rn, r.today)
    except Exception as e:  # noqa: BLE001
        _fail(db, r.invoice.id, r.rn, e)
        return r.run if summarise else None
    return summarise_invoice(db, r, facts) if summarise else facts


def _match_decide_record(db: Session, invoice: m.Invoice, run: m.ProcessingRun, inv: ExtractedInvoice, vres: dict,
                         checks: list, rules: dict, llm: LLMClient, stage_errors: list[str], rn: int,
                         today: date | None = None) -> m.ProcessingRun:
    """PO matching -> duplicate check -> decision -> ledger -> AI summary. Runs inside LedgerSection,
    so the PO balances and the invoice history it reads cannot change underneath it."""
    # ---------------------------------------------------------------------- PO matching
    # Tax-inclusive invoices are matched on net (ex-tax) values, the basis PO prices use.
    inv_match = net_view(inv)
    consumed = consumption_by_line(db, exclude_invoice_id=invoice.id)
    pos = db.scalars(select(m.PurchaseOrder).options(selectinload(m.PurchaseOrder.lines),
                                                     selectinload(m.PurchaseOrder.vendor))).all()
    snaps = {p.po_number: po_snapshot(p, consumed) for p in pos}
    vendor = vres["vendor"]
    vendor_pos = [sn for sn in snaps.values() if vendor and sn["vendor_id"] == vendor["id"]]
    confirmed = None
    if invoice.confirmed_po_id:
        cp = db.get(m.PurchaseOrder, invoice.confirmed_po_id)
        confirmed = snaps.get(cp.po_number) if cp else None
    kind = document_type(inv)
    note_info, note_issues = None, []
    if kind == "INVOICE":
        invoice.original_invoice_id = None
        match = match_po(inv_match, vendor, vendor_pos, snaps, rules, SemanticScorer(llm), confirmed)
    else:
        # credit / debit notes are processed against the original invoice they cite, not a PO search
        match, note_info, note_issues = process_note(db, invoice, inv_match, vendor, snaps, rules)
        log(db, invoice.id, "NOTE_LINKED" if note_info.get("original") else "NOTE_ORIGINAL_MISSING",
            f"{kind.replace('_', ' ').title()}: {match['explanation']}",
            {"original": note_info.get("original"), "lines": note_info.get("lines")}, rn)
    if inv_match is not inv:
        match["net_basis"] = inv_match.meta.get("net_conversion")
    run.matching = match
    if match.get("method") == "INFERRED" or match.get("candidates"):
        log(db, invoice.id, "PO_CANDIDATES_RANKED",
            f"Ranked {len(match['candidates'])} candidate PO(s): " +
            ", ".join(f"{c['po_number']}={c['score'] if c['score'] is not None else 'ineligible'}"
                      for c in match["candidates"][:4]) +
            (f". AI meaning signal: {'available' if match['semantic']['available'] else 'unavailable'}."),
            {"candidates": [{"po": c["po_number"], "score": c["score"]} for c in match["candidates"]]}, rn)
    log(db, invoice.id, "PO_IDENTIFIED" if match["selected_po"] else "PO_NOT_IDENTIFIED",
        match["explanation"] or (f"Selected {match['selected_po']}" if match["selected_po"] else "No PO selected."),
        {"method": match["method"], "selected_po": match["selected_po"], "ambiguous": match["ambiguous"]}, rn)
    exceeded = [v for v in match["variances"] if v["exceeded"]]
    log(db, invoice.id, "MATCHING_COMPLETED",
        f"{len(match['pairs'])} line(s) matched, {len(match['unmatched_lines'])} unmatched; "
        f"{len(match['variances'])} variance(s) computed, {len(exceeded)} outside tolerance.",
        {"issues": [i["code"] for i in match["issues"]]}, rn)
    _stage(db, invoice, "DUPLICATE_CHECK")

    # ---------------------------------------------------------------------- duplicates
    history = [h for h in _history(db, invoice) if h["document_type"] == kind]
    dup = check_duplicates(inv, invoice.file_sha256, vendor["id"] if vendor else None, history, rules)
    run.duplicates = dup
    log(db, invoice.id, "DUPLICATE_CHECK_COMPLETED", dup["explanation"],
        {"status": dup["status"], "fingerprint": dup.get("fingerprint")}, rn)
    _stage(db, invoice, "CONTROLS")

    # ---------------------------------------------------------------------- AP controls
    controls = run_controls(db, invoice, inv, vendor, match, snaps, rules, today or date.today(), note_info, note_issues)
    run.controls = controls
    blocking = [i for i in controls["issues"] if i["outcome"] != "FLAG"]
    log(db, invoice.id, "CONTROLS_COMPLETED",
        f"Approval: {controls['approval'].get('message')} "
        f"E-invoice: {controls['einvoice'].get('status', 'n/a').lower().replace('_', ' ')}. "
        f"TDS: {controls['tds'].get('message')} "
        f"{len(blocking)} control finding(s) need review, {len(controls['issues']) - len(blocking)} flag(s).",
        {"issues": [i["code"] for i in controls["issues"]], "approval_level": controls["approval"].get("level")}, rn)
    _stage(db, invoice, "DECIDING")

    # ---------------------------------------------------------------------- decision
    decision = decide(inv, checks, vres, match, dup, rules, invoice.duplicate_resolution, stage_errors, controls)
    run.decision = decision.model_dump()
    log(db, invoice.id, "DECISION_GENERATED", f"{decision.decision}: {decision.summary}",
        {"decision": decision.decision, "rules": [r.rule for r in decision.reasons]}, rn)

    # ---------------------------------------------------------------------- denormalised header
    invoice.vendor_id = vendor["id"] if vendor else None
    invoice.po_id = match["selected_po_id"]
    invoice.vendor_tax_id = inv.get("vendor_tax_id")
    name_field = inv.field("vendor_name")
    invoice.vendor_name = vendor["name"] if vendor else (
        name_field.value if name_field.status in ("EXTRACTED", "CORRECTED") else None)
    invoice.invoice_number = inv.get("invoice_number")
    invoice.invoice_number_norm = normalize_ref(inv.get("invoice_number")) or None
    invoice.invoice_date = date.fromisoformat(inv.get("invoice_date")) if inv.get("invoice_date") else None
    invoice.currency = inv.get("currency") or (snaps[match["selected_po"]]["currency"] if match["selected_po"] else None)
    invoice.grand_total = D(inv.get("grand_total"))
    invoice.po_reference = match["selected_po"] or inv.get("po_number")
    invoice.content_fingerprint = fingerprint(inv)
    invoice.document_type = kind
    invoice.approval_level = decision.approval_level
    invoice.risk_flags = [r["code"] for r in controls["risk"]] or None
    invoice.bank_account, invoice.bank_ifsc = inv.get("bank_account"), inv.get("bank_ifsc")
    tds = controls["tds"]
    invoice.tds_section = tds.get("section") if tds.get("applicable") else None
    invoice.tds_base = D(tds.get("taxable")) if tds.get("applicable") else None
    invoice.tds_amount = D(tds.get("amount")) if tds.get("applicable") else None
    invoice.system_decision = decision.decision

    # A reviewer's final decision is never silently overwritten by a re-run: the system's new
    # view is stored and logged next to it, and the reviewer can change their decision explicitly.
    human_final = invoice.decided_by == "HUMAN" and invoice.decision in ("APPROVED", "REJECTED")
    effective = invoice.decision if human_final else decision.decision
    if human_final:
        log(db, invoice.id, "HUMAN_DECISION_KEPT",
            f"Reviewer decision {invoice.decision} kept; this run's system decision is {decision.decision}.",
            {"human_decision": invoice.decision, "system_decision": decision.decision}, rn)
    else:
        invoice.decision, invoice.decided_by = decision.decision, "SYSTEM"

    # ---------------------------------------------------------------------- ledger
    if effective == "APPROVED" and match["selected_po_id"]:
        po = db.get(m.PurchaseOrder, match["selected_po_id"])
        recorded = record_consumption(db, invoice.id, po, match["pairs"])
        log(db, invoice.id, "PO_CONSUMPTION_RECORDED",
            f"Recorded consumption on {po.po_number}: " +
            ", ".join(f"line {r['po_line_no']} qty {r['quantity']}" for r in recorded) + ".", {"lines": recorded}, rn)
    elif effective != "APPROVED":
        n = reverse_consumption(db, invoice.id)
        if n:
            log(db, invoice.id, "PO_CONSUMPTION_REVERSED",
                f"Reversed {n} consumption row(s) from an earlier approval.", {}, rn)
    _stage(db, invoice, "SUMMARISING")

    # ---------------------------------------------------------------------- AI summary
    facts = {"decision": decision.decision, "deterministic_summary": decision.summary,
             "reasons": [r.message for r in decision.reasons], "next_action": decision.next_action,
             "vendor": invoice.vendor_name, "invoice_number": invoice.invoice_number,
             "grand_total": str(invoice.grand_total) if invoice.grand_total is not None else None,
             "currency": invoice.currency, "po": match["selected_po"], "po_method": match["method"],
             "po_explanation": match["explanation"], "duplicates": dup["explanation"],
             "variances_outside_tolerance": [{k: v[k] for k in ("label", "field", "expected", "actual", "pct_diff",
                                                                "tolerance")} for v in exceeded],
             # context the recommendation needs (facts only - the AI never sees raw documents here)
             "vendor_status": vendor["status"] if vendor else "NOT_IN_MASTER_DATA",
             "failed_checks": [{"check": c.name, "message": c.message} for c in checks if c.status == "FAIL"],
             "uncertain_or_missing_fields": sorted(n for n, f in inv.fields.items()
                                                   if f.status in ("UNCERTAIN", "MISSING")
                                                   and n in ("vendor_tax_id", "invoice_number", "invoice_date",
                                                             "grand_total", "po_number", "currency")),
             "po_candidates": [{"po_number": c["po_number"], "score": c["score"], "description": c.get("description"),
                                "remaining_amount": c.get("remaining_amount")}
                               for c in match.get("candidates", []) if c.get("eligible")][:5],
             "duplicate_status": dup["status"],
             "invoice_line_count": len(inv.line_items),
             "tax_inclusive": bool(inv.meta.get("tax_inclusive")),
             "document_type": kind,
             "approval": {k: controls["approval"].get(k) for k in ("level", "limit", "message")},
             "control_findings": [{"code": i["code"], "outcome": i["outcome"], "message": i["message"]}
                                  for i in controls["issues"]],
             "tds": controls["tds"].get("message"),
             "original_invoice": (note_info or {}).get("original")}
    # The decision is final from here on: mark the invoice PROCESSED now (duplicate checks and credit-note
    # lookups of the NEXT invoice only see processed invoices), and write the AI summary afterwards,
    # outside the ledger lock, so slow AI calls never hold up the next invoice's decision.
    invoice.status = "PROCESSED"
    run.llm_calls = list(llm.calls)
    db.commit()
    return run, facts


def summarise_invoice(db: Session, r: ReadResult, facts: dict) -> m.ProcessingRun:
    """AI summary + recommendation for a decided invoice; runs outside the ledger lock (in parallel for
    batches). A failure here never changes the decision - the run simply has no AI summary."""
    run, invoice = r.run, r.invoice
    try:
        run.ai_summary = ai_summary(r.llm, facts)
        if run.ai_summary and run.ai_summary.get("text"):
            log(db, invoice.id, "AI_SUMMARY_GENERATED", "AI summary generated (labelled as AI output).", {}, r.rn)
    except Exception:  # noqa: BLE001
        log_.exception("AI summary failed for invoice %s", invoice.id)
        db.rollback()
    run.llm_calls = list(r.llm.calls)
    run.finished_at = datetime.now(timezone.utc)
    invoice.stage = "DONE"
    db.commit()
    return run


# ============================================================================ human review

def _review(db: Session, invoice: m.Invoice, action: str, before, after, reason: str, user: str):
    db.add(m.ReviewAction(invoice_id=invoice.id, action_type=action, before=before, after=after, reason=reason,
                          user_name=user))
    log(db, invoice.id, "HUMAN_ACTION", f"{user}: {action.replace('_', ' ').lower()} - {reason}",
        {"action": action, "before": before, "after": after}, getattr(latest_run(db, invoice.id), "run_no", None), user)


def correct_fields(db: Session, invoice: m.Invoice, fields: dict, line_items: list | None, reason: str, user: str,
                   llm: LLMClient | None = None):
    before = dict(invoice.corrections or {})
    corr = {"fields": {**(before.get("fields") or {}), **fields}}
    if line_items is not None:
        corr["line_items"] = line_items
    elif before.get("line_items") is not None:
        corr["line_items"] = before["line_items"]
    invoice.corrections = corr
    _review(db, invoice, "CORRECT_FIELDS", before, corr, reason, user)
    db.commit()
    return process_invoice(db, invoice.id, "CORRECTION", user, llm)


def confirm_po(db: Session, invoice: m.Invoice, po: m.PurchaseOrder, reason: str, user: str,
               llm: LLMClient | None = None):
    _review(db, invoice, "CONFIRM_PO", {"po_id": invoice.confirmed_po_id}, {"po_id": po.id, "po_number": po.po_number},
            reason, user)
    invoice.confirmed_po_id = po.id
    db.commit()
    return process_invoice(db, invoice.id, "PO_CONFIRMED", user, llm)


def resolve_duplicate(db: Session, invoice: m.Invoice, resolution: str, reason: str, user: str,
                      llm: LLMClient | None = None):
    _review(db, invoice, "RESOLVE_DUPLICATE", {"resolution": invoice.duplicate_resolution}, {"resolution": resolution},
            reason, user)
    invoice.duplicate_resolution = resolution
    db.commit()
    return process_invoice(db, invoice.id, "DUPLICATE_RESOLVED", user, llm)


def _check_po_balance(db: Session, invoice: m.Invoice, run: m.ProcessingRun, po: m.PurchaseOrder) -> None:
    """Re-check the matched lines against the PO balance *now*. Other invoices may have been approved
    since this one was processed, so the run's stored variances can be stale."""
    consumed = consumption_by_line(db, exclude_invoice_id=invoice.id)
    rules = run.rules_snapshot or effective_rules(db)
    tol = D(po.qty_tolerance_pct) if po.qty_tolerance_pct is not None else D(rules["tolerances"]["quantity_pct"])
    by_no = {ln.line_no: ln for ln in po.lines}
    over = []
    for p in run.matching.get("pairs", []):
        line, qty = by_no.get(p["po_line_no"]), D(p["invoice"]["quantity"])
        if line is None or qty is None:
            continue
        remaining = line.quantity - consumed.get(line.id, (D("0"), D("0")))[0]
        if qty > remaining * (1 + tol / 100):
            over.append({"po_line_no": line.line_no, "description": line.description, "invoiced": str(qty),
                         "remaining": str(remaining)})
    if over:
        detail = "; ".join(f"line {o['po_line_no']} invoices {o['invoiced']} but only {o['remaining']} remain"
                           for o in over)
        raise POBalanceExceeded(f"Approving would over-bill {po.po_number}: {detail}. Amend the PO or reject.", over)


def manager_required(db: Session, run: m.ProcessingRun | None) -> bool:
    """True when this run's invoice was held back by the approval limit and only a manager may approve it now."""
    level = ((run.decision or {}).get("approval_level") if run else None) or "AUTO"
    if level == "AUTO":
        return False
    return effective_rules(db)["approval"].get("above_limit_approver", "MANAGER") == "MANAGER"


def final_decision(db: Session, invoice: m.Invoice, decision: str, reason: str, user: str,
                   role: str = "REVIEWER") -> dict:
    with LedgerSection(db):
        return _final_decision(db, invoice, decision, reason, user, role)


def _final_decision(db: Session, invoice: m.Invoice, decision: str, reason: str, user: str,
                    role: str = "REVIEWER") -> dict:
    run = latest_run(db, invoice.id)
    level = ((run.decision or {}).get("approval_level") if run else None) or "AUTO"
    # The CURRENT setting decides who may approve an invoice the limit held back (it may have changed since the run).
    if decision == "APPROVED" and level != "AUTO" and manager_required(db, run) and role != "MANAGER":
        approval = (run.controls or {}).get("approval") or {}
        raise ManagerApprovalRequired((approval.get("message") or "This invoice needs manager approval.")
                                      + " Sign in as a manager to approve it; a reviewer can still reject it.")
    if decision == "APPROVED" and run and run.matching and run.matching.get("selected_po_id"):
        _check_po_balance(db, invoice, run, db.get(m.PurchaseOrder, run.matching["selected_po_id"]))
    before = {"decision": invoice.decision, "decided_by": invoice.decided_by}
    invoice.decision, invoice.decided_by = decision, "HUMAN"
    _review(db, invoice, "FINAL_" + decision, before, {"decision": decision}, reason, user)
    note = None
    if decision == "APPROVED":
        match = run.matching if run else None
        if match and match.get("selected_po_id"):
            po = db.get(m.PurchaseOrder, match["selected_po_id"])
            recorded = record_consumption(db, invoice.id, po, match["pairs"])
            log(db, invoice.id, "PO_CONSUMPTION_RECORDED",
                f"Human approval: recorded consumption on {po.po_number} for {len(recorded)} line(s).",
                {"lines": recorded}, run.run_no if run else None, user)
        else:
            note = "No PO is linked to this invoice, so no PO consumption was recorded."
            log(db, invoice.id, "PO_CONSUMPTION_SKIPPED", note, {}, run.run_no if run else None, user)
    else:
        n = reverse_consumption(db, invoice.id)
        if n:
            log(db, invoice.id, "PO_CONSUMPTION_REVERSED", f"Reversed {n} consumption row(s).", {},
                run.run_no if run else None, user)
    log(db, invoice.id, "FINAL_DECISION", f"Final decision by {user}"
        f"{' (manager)' if role == 'MANAGER' else ''}: {decision}. Reason: {reason}",
        {"decision": decision, "role": role, "approval_level": level}, run.run_no if run else None, user)
    db.commit()
    return {"decision": decision, "note": note}
