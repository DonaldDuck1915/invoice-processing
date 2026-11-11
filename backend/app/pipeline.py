"""Processing pipeline orchestration and human-review actions.

Stages: extraction -> vendor resolution -> validation -> PO matching -> duplicate check -> decision
-> ledger -> AI summary. Every stage writes an audit event. Each run stores its full results and the
rules snapshot it used. Corrections / PO confirmation / duplicate resolution start a new run that
re-uses the stored extraction (no new AI extraction call).
"""
from __future__ import annotations

import logging
import traceback
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import models as m
from .audit import log
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
from .schemas import ExtractedInvoice
from .summary import ai_summary
from .validation import run_validation

log_ = logging.getLogger(__name__)


def effective_rules(db: Session) -> dict:
    base = load_default_rules()
    ov = db.scalar(select(m.RuleOverride).order_by(m.RuleOverride.id.desc()).limit(1))
    return deep_merge(base, ov.overrides) if ov else base


def latest_run(db: Session, invoice_id: int) -> m.ProcessingRun | None:
    return db.scalar(select(m.ProcessingRun).where(m.ProcessingRun.invoice_id == invoice_id)
                     .order_by(m.ProcessingRun.run_no.desc()).limit(1))


def _vendor_dict(v: m.Vendor) -> dict:
    return {"id": v.id, "code": v.code, "name": v.name, "tax_id": v.tax_id, "tax_id_type": v.tax_id_type,
            "country": v.country, "status": v.status, "default_currency": v.default_currency}


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
                    "content_fingerprint": r.content_fingerprint, "decision": r.decision})
    return out


def process_invoice(db: Session, invoice_id: int, trigger: str = "INITIAL", actor: str = "SYSTEM",
                    llm: LLMClient | None = None, today: date | None = None) -> m.ProcessingRun:
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
                base, doc_text = extract(doc.content, rules, llm, settings)
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
                f"AI extraction {'used' if base.meta.get('ai_used') else 'not used' + (' - ' + base.meta['ai_error'] if base.meta.get('ai_error') else '')}.",
                {"status_counts": counts, "extractor": base.meta.get("extractor"),
                 "ocr_mean_conf": base.meta.get("ocr_mean_conf")}, rn)
        inv = apply_corrections(base.model_copy(deep=True), invoice.corrections)
        inv = finalize(inv, float(rules["extraction"]["min_confidence"]))
        if invoice.corrections:
            log(db, invoice.id, "CORRECTIONS_APPLIED",
                f"Applied reviewer corrections to {len((invoice.corrections or {}).get('fields', {}))} field(s)"
                + (" and line items" if invoice.corrections.get("line_items") is not None else "") + ".",
                invoice.corrections, rn)
        run.extraction = {"base": base.model_dump(), "result": inv.model_dump()}
        invoice.stage = "VALIDATING"

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
        invoice.stage = "MATCHING"

        # ------------------------------------------------------------------ PO matching
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
        scorer = SemanticScorer(llm)
        match = match_po(inv, vendor, vendor_pos, snaps, rules, scorer, confirmed)
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
        invoice.stage = "DUPLICATE_CHECK"

        # ------------------------------------------------------------------ duplicates
        dup = check_duplicates(inv, invoice.file_sha256, vendor["id"] if vendor else None, _history(db, invoice), rules)
        run.duplicates = dup
        log(db, invoice.id, "DUPLICATE_CHECK_COMPLETED", dup["explanation"],
            {"status": dup["status"], "fingerprint": dup.get("fingerprint")}, rn)
        invoice.stage = "DECIDING"

        # ------------------------------------------------------------------ decision
        decision = decide(inv, checks, vres, match, dup, rules, invoice.duplicate_resolution, stage_errors)
        run.decision = decision.model_dump()
        log(db, invoice.id, "DECISION_GENERATED", f"{decision.decision}: {decision.summary}",
            {"decision": decision.decision, "rules": [r.rule for r in decision.reasons]}, rn)

        # ------------------------------------------------------------------ denormalised header
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
        invoice.system_decision = decision.decision
        invoice.decision, invoice.decided_by = decision.decision, "SYSTEM"

        # ------------------------------------------------------------------ ledger
        if decision.decision == "APPROVED" and match["selected_po_id"]:
            po = db.get(m.PurchaseOrder, match["selected_po_id"])
            recorded = record_consumption(db, invoice.id, po, match["pairs"])
            log(db, invoice.id, "PO_CONSUMPTION_RECORDED",
                f"Recorded consumption on {po.po_number}: " +
                ", ".join(f"line {r['po_line_no']} qty {r['quantity']}" for r in recorded) + ".", {"lines": recorded}, rn)
        else:
            n = reverse_consumption(db, invoice.id)
            if n:
                log(db, invoice.id, "PO_CONSUMPTION_REVERSED",
                    f"Reversed {n} consumption row(s) from an earlier approval.", {}, rn)

        # ------------------------------------------------------------------ AI summary
        facts = {"decision": decision.decision, "deterministic_summary": decision.summary,
                 "reasons": [r.message for r in decision.reasons], "next_action": decision.next_action,
                 "vendor": invoice.vendor_name, "invoice_number": invoice.invoice_number,
                 "grand_total": str(invoice.grand_total) if invoice.grand_total is not None else None,
                 "currency": invoice.currency, "po": match["selected_po"], "po_method": match["method"],
                 "po_explanation": match["explanation"], "duplicates": dup["explanation"],
                 "variances_outside_tolerance": [{k: v[k] for k in ("label", "field", "expected", "actual", "pct_diff",
                                                                    "tolerance")} for v in exceeded]}
        run.ai_summary = ai_summary(llm, facts)
        if run.ai_summary and run.ai_summary.get("text"):
            log(db, invoice.id, "AI_SUMMARY_GENERATED", "AI summary generated (labelled as AI output).", {}, rn)
        run.llm_calls = llm.calls
        run.finished_at = datetime.now(timezone.utc)
        invoice.status, invoice.stage = "PROCESSED", "DONE"
        db.commit()
        return run
    except DocumentError as e:
        db.rollback()
        invoice = db.get(m.Invoice, invoice_id)
        invoice.status, invoice.stage, invoice.error = "FAILED", "FAILED", e.message
        log(db, invoice.id, "PROCESSING_FAILED", e.message, {"code": e.code}, rn)
        db.commit()
        return run
    except Exception as e:  # noqa: BLE001
        log_.exception("pipeline failed")
        db.rollback()
        invoice = db.get(m.Invoice, invoice_id)
        invoice.status, invoice.stage, invoice.error = "FAILED", "FAILED", f"Unexpected error: {e}"
        log(db, invoice.id, "PROCESSING_FAILED", f"Unexpected error: {e}", {"trace": traceback.format_exc()[-2000:]}, rn)
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


def final_decision(db: Session, invoice: m.Invoice, decision: str, reason: str, user: str) -> dict:
    run = latest_run(db, invoice.id)
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
    log(db, invoice.id, "FINAL_DECISION", f"Final decision by {user}: {decision}. Reason: {reason}",
        {"decision": decision}, run.run_no if run else None, user)
    db.commit()
    return {"decision": decision, "note": note}
