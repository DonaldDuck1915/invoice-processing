"""Extraction orchestration: read PDF -> rule extractor -> (optional) AI extractor -> merge -> normalise.

Merge policy (per field):
  both agree                      -> EXTRACTED, high confidence, source "llm+rules"
  only AI, quote verified         -> EXTRACTED (0.88)
  only AI, quote NOT verified     -> UNCERTAIN (0.5)  - never trusted blindly
  disagree                        -> UNCERTAIN, both readings recorded in the note
  only rules                      -> rules result as-is
Confidence below `extraction.min_confidence` always becomes UNCERTAIN.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..llm import LLMClient, LLMUnavailable
from ..money import D, parse_amount, s
from ..schemas import DATE_FIELDS, HEADER_FIELDS, MONEY_FIELDS, ExtractedInvoice, FieldValue, LineItem, TaxComponent
from .llm_extractor import extract_with_llm
from .parsers import normalize_ref, normalize_tax_id, parse_date
from .pdf_reader import DocumentText, read_document
from .qr import decode_qr_codes
from .rule_extractor import extract_with_rules


def _normalise(name: str, value: str | None, prefer: str) -> str | None:
    if value in (None, ""):
        return None
    v = str(value).strip()
    if name in MONEY_FIELDS:
        amt = parse_amount(v)
        return s(abs(amt)) if amt is not None and name in ("discount", "shipping") else s(amt)
    if name in DATE_FIELDS:
        d, _ = parse_date(v, "DMY" if prefer == "DMY" else prefer)
        return d.isoformat() if d else None
    if name == "vendor_tax_id":
        return normalize_tax_id(v)
    if name in ("invoice_number", "po_number", "original_invoice_number"):
        return v.upper()
    if name == "document_type":
        t = re.sub(r"[^A-Z]", "", v.upper())
        return "CREDIT_NOTE" if "CREDIT" in t else "DEBIT_NOTE" if "DEBIT" in t else "INVOICE" if "INVOICE" in t else None
    if name in ("bank_account", "bank_ifsc"):
        return re.sub(r"[\s\-.]", "", v).upper() or None
    if name == "irn":
        return re.sub(r"\s", "", v).lower() or None
    if name == "currency":
        return v.upper()[:3]
    return v


def _same(name: str, a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    if name in MONEY_FIELDS:
        return D(a) == D(b)
    if name in ("invoice_number", "po_number", "vendor_tax_id", "original_invoice_number", "bank_account", "bank_ifsc",
                "irn"):
        return normalize_ref(a) == normalize_ref(b)
    if name in ("vendor_name", "vendor_address"):
        return fuzz.token_set_ratio(a.lower(), b.lower()) >= 90
    return a == b


def merge(rule: ExtractedInvoice, ai: dict | None, scanned: bool) -> ExtractedInvoice:
    if not ai:
        return rule
    out = ExtractedInvoice(meta={**rule.meta, "extractor": "llm+rules", "ai_notes": ai.get("notes")})
    hint = ai.get("date_format_hint") or "UNKNOWN"
    prefer = rule.meta.get("date_order", "DMY")
    if hint in ("DMY", "MDY"):
        prefer = hint
    for name in HEADER_FIELDS:
        r = rule.fields.get(name) or FieldValue()
        a = (ai.get("fields") or {}).get(name) or {}
        a_val = _normalise(name, a.get("value"), prefer)
        verified = bool(a.get("verified"))
        if name in DATE_FIELDS and a_val and r.value and r.value != a_val and r.note and "ambiguous" in r.note:
            # rules guessed the day/month order; the AI saw the whole document
            r = r.model_copy(update={"value": a_val})
        if a_val is None and r.value is None:
            out.fields[name] = r if r.status != "EXTRACTED" else FieldValue(status="MISSING")
        elif a_val is not None and r.value is not None and _same(name, a_val, r.value):
            out.fields[name] = r.model_copy(update={
                "value": r.value, "confidence": round(max(r.confidence, 0.95 if verified else 0.9), 3),
                "status": "EXTRACTED" if r.status != "DERIVED" else "DERIVED", "source": f"llm+{r.source}",
                "note": r.note if r.note and "ambiguous" not in r.note else None})
        elif a_val is not None and r.value is None:
            out.fields[name] = FieldValue(
                value=a_val, raw=a.get("source_quote"), confidence=0.88 if verified else 0.5,
                status="EXTRACTED" if verified else "UNCERTAIN", source="llm",
                note=None if verified else "AI value could not be located in the document text - please verify.")
        elif a_val is None and r.value is not None:
            out.fields[name] = r
        else:
            if r.confidence < 0.6 and verified:
                out.fields[name] = FieldValue(value=a_val, raw=a.get("source_quote"), confidence=0.85,
                                              status="EXTRACTED", source="llm",
                                              note=f"Rule parser read {r.value!r} with low confidence; AI reading used.")
            else:
                out.fields[name] = r.model_copy(update={
                    "status": "UNCERTAIN", "confidence": min(r.confidence, 0.55),
                    "note": f"Readings disagree: rule parser {r.value!r}, AI {a_val!r}."})

    ai_lines = ai.get("line_items") or []
    if ai_lines and (not rule.line_items or len(ai_lines) != len(rule.line_items)):
        lines = []
        for i, li in enumerate(ai_lines, 1):
            ok = bool(li.get("verified"))
            lines.append(LineItem(
                line_no=i, description=li.get("description"), item_code=li.get("item_code"),
                quantity=s(parse_amount(li.get("quantity"))), uom=li.get("uom"),
                unit_price=s(parse_amount(li.get("unit_price"))), tax_rate=s(parse_amount(li.get("tax_rate"))),
                line_total=s(parse_amount(li.get("line_total"))), confidence=0.88 if ok else 0.5,
                status="EXTRACTED" if ok else "UNCERTAIN", source="llm",
                note=None if ok else "AI line could not be located in the document text."))
        out.line_items = lines
        if rule.line_items:
            out.meta["line_item_note"] = (f"Rule parser found {len(rule.line_items)} line(s), AI found "
                                          f"{len(ai_lines)}; AI rows used.")
    else:
        merged = []
        for r_li, a_li in zip(rule.line_items, ai_lines or [None] * len(rule.line_items)):
            li = r_li.model_copy()
            if a_li:
                diffs = []
                for k in ("quantity", "unit_price", "line_total"):
                    av = parse_amount(a_li.get(k))
                    rv = D(getattr(r_li, k))
                    if av is not None and rv is not None and av != rv:
                        diffs.append(f"{k}: rules {rv}, AI {av}")
                if not li.item_code and a_li.get("item_code"):
                    li.item_code = a_li["item_code"]
                if a_li.get("description") and (not li.description or len(a_li["description"]) > len(li.description)):
                    li.description = a_li["description"]
                if diffs:
                    li.status, li.confidence = "UNCERTAIN", min(li.confidence, 0.55)
                    li.note = "Readings disagree - " + "; ".join(diffs)
                else:
                    li.confidence = round(max(li.confidence, 0.93), 3)
                    li.source = f"llm+{li.source}"
            merged.append(li)
        out.line_items = merged
    if ai.get("tax_components") and not rule.tax_components:
        out.tax_components = [TaxComponent(kind=t["kind"].upper().replace(" ", "_"), rate=t.get("rate"),
                                           amount=s(parse_amount(t.get("amount")))) for t in ai["tax_components"]]
    else:
        out.tax_components = rule.tax_components
    return out


def finalize(inv: ExtractedInvoice, min_conf: float) -> ExtractedInvoice:
    for name in HEADER_FIELDS:
        f = inv.fields.setdefault(name, FieldValue(status="MISSING"))
        if f.value is None and f.status in ("EXTRACTED",):
            f.status = "MISSING"
        if f.value is not None and f.status == "EXTRACTED" and f.confidence < min_conf:
            f.status = "UNCERTAIN"
            f.note = f.note or f"Confidence {f.confidence:.0%} is below the {min_conf:.0%} threshold."
    for li in inv.line_items:
        if li.status == "EXTRACTED" and li.confidence < min_conf:
            li.status = "UNCERTAIN"
            li.note = li.note or f"Row confidence {li.confidence:.0%} is below threshold."
    return inv


def apply_corrections(inv: ExtractedInvoice, corrections: dict | None) -> ExtractedInvoice:
    """Human corrections always win; the original reading is preserved in `raw`/`note`."""
    if not corrections:
        return inv
    prefer = inv.meta.get("date_order", "DMY")
    for name, value in (corrections.get("fields") or {}).items():
        old = inv.fields.get(name) or FieldValue()
        norm = _normalise(name, value, prefer) if value not in (None, "") else None
        inv.fields[name] = FieldValue(
            value=norm, raw=old.raw, confidence=1.0, status="CORRECTED" if norm is not None else "MISSING",
            source="human", page=old.page, note=f"Corrected by reviewer (was {old.value!r}).")
    if corrections.get("line_items") is not None:
        lines = []
        for i, li in enumerate(corrections["line_items"], 1):
            lines.append(LineItem(line_no=i, description=li.get("description"), item_code=li.get("item_code") or None,
                                  quantity=s(parse_amount(li.get("quantity"))), uom=li.get("uom") or None,
                                  unit_price=s(parse_amount(li.get("unit_price"))),
                                  tax_rate=s(parse_amount(li.get("tax_rate"))),
                                  line_total=s(parse_amount(li.get("line_total"))),
                                  confidence=1.0, status="CORRECTED", source="human"))
        inv.line_items = lines
    return inv


CRITICAL = ("vendor_tax_id", "invoice_number", "invoice_date", "grand_total", "currency")
STRICT_CONFIDENCE = 0.90
RX_BANK_HINT = re.compile(r"\b(?:a/?c\.?\s*no|account\s*(?:no|number)|iban|ifsc|swift)\b", re.I)
RX_IRN_HINT = re.compile(r"\bIRN\b")
RX_NOTE_HINT = re.compile(r"\b(?:credit|debit)\s*note\b", re.I)


def ai_not_needed(rule_inv: ExtractedInvoice, doc: DocumentText, rules: dict) -> tuple[bool, str]:
    """Strict test for skipping the AI reader. Only a clean digital invoice passes: the rule parser found
    every critical field with high confidence AND every sum reconciles (lines, subtotal, tax, grand total),
    so the arithmetic itself confirms the reading. Anything else gets the AI exactly as before.
    Returns (skip, reason)."""
    from ..extraction.parsers import tax_id_format_valid
    from ..validation import check_grand_total, check_line_math, check_mandatory, check_subtotal, check_tax

    if doc.is_scanned:
        return False, "scanned document"
    inv = finalize(rule_inv.model_copy(deep=True), float(rules["extraction"]["min_confidence"]))
    for name in CRITICAL:
        f = inv.field(name)
        if f.value is None or f.status != "EXTRACTED" or f.confidence < STRICT_CONFIDENCE:
            return False, f"{name.replace('_', ' ')} not read with high confidence"
    if not tax_id_format_valid(inv.get("vendor_tax_id")):
        return False, "vendor tax ID does not validate"
    if not inv.line_items or any(li.status != "EXTRACTED" for li in inv.line_items):
        return False, "line items incomplete"
    checks = check_mandatory(inv) + check_line_math(inv, rules) + check_tax(inv, rules) + \
        [check_subtotal(inv, rules), check_grand_total(inv, rules)]
    bad = [c for c in checks if c.status in ("FAIL", "SKIPPED")
           and (c.code.startswith(("V1.", "V2.", "V4.", "V5.")) or c.code == "V3.tax")]
    if bad:
        return False, f"{bad[0].name.lower()} does not reconcile"
    text = doc.text or ""
    # fields the rules read less reliably on unusual layouts: if the document mentions them, ask the AI
    if RX_BANK_HINT.search(text) and not inv.get("bank_account"):
        return False, "bank details printed but not read"
    if RX_IRN_HINT.search(text) and not inv.get("irn"):
        return False, "IRN printed but not read"
    if RX_NOTE_HINT.search(text) and not inv.get("original_invoice_number"):
        return False, "credit/debit note without a read original-invoice reference"
    return True, "clean digital invoice: all critical fields read and every total reconciles"


def extract(pdf_bytes: bytes, rules: dict, llm: LLMClient, settings,
            force_ai: bool = False) -> tuple[ExtractedInvoice, DocumentText]:
    doc = read_document(pdf_bytes, settings.max_pages, settings.tesseract_cmd)
    company = rules.get("company", {})
    rule_inv = extract_with_rules(doc, company.get("tax_ids"), company.get("name"))
    ai, ai_error = None, None
    skip, skip_reason = False, None
    if llm.available and not force_ai and getattr(settings, "ai_extraction", "auto") == "auto":
        skip, skip_reason = ai_not_needed(rule_inv, doc, rules)
    if llm.available and not skip:
        try:
            ai = extract_with_llm(llm, pdf_bytes, doc.text, company.get("name", "the buyer"))
        except LLMUnavailable as e:
            ai_error = str(e)
    inv = merge(rule_inv, ai, doc.is_scanned)
    try:
        inv.meta["qr_codes"] = decode_qr_codes(pdf_bytes, settings.max_pages)
    except Exception as e:  # noqa: BLE001 - QR reading is best-effort
        inv.meta["qr_codes"], inv.meta["qr_error"] = [], str(e)
    inv.meta["ai_used"] = ai is not None
    inv.meta["ai_error"] = ai_error
    inv.meta["ai_skipped"] = skip
    inv.meta["ai_skip_reason"] = skip_reason if skip else None
    inv.meta["warnings"] = doc.warnings
    return finalize(inv, float(rules["extraction"]["min_confidence"])), doc
