"""Credit and debit notes.

A note is processed against the ORIGINAL invoice it cites, not matched to a PO from scratch:

  credit note  reduces what we owe on the original. Lines that return goods (same unit price as the
               original) give the quantity back to the PO line; price/discount lines only reduce value.
               The ledger records the note as negative consumption, so the PO balance goes back up.
  debit note   raises what we owe (a vendor-side price revision or extra charge). Value lines add to the
               PO's consumed value; lines at the original unit price add quantity and must fit the PO.

Checks: the original must exist (same vendor), be approved, and be in the same currency; a credit note
may not credit more than the original (less earlier credits, plus earlier debits) nor return more than
was billed per line.
"""
from __future__ import annotations

from decimal import Decimal

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .extraction.parsers import normalize_ref
from .money import D, ZERO, fmt_money, q2, s
from .schemas import ExtractedInvoice

LABEL = {"CREDIT_NOTE": "Credit note", "DEBIT_NOTE": "Debit note"}


def document_type(inv: ExtractedInvoice) -> str:
    v = inv.get("document_type")
    return v if v in ("CREDIT_NOTE", "DEBIT_NOTE") else "INVOICE"


def issue(rules: dict, policy_key: str, code: str, rule: str, message: str, evidence: dict | None = None) -> dict:
    return {"code": code, "rule": rule, "outcome": rules["policy"].get(policy_key, "MANUAL_REVIEW"),
            "message": message, "evidence": evidence or {}}


def find_original(db: Session, invoice: m.Invoice, vendor_id: int | None, ref: str | None) -> m.Invoice | None:
    if not ref:
        return None
    norm = normalize_ref(ref)
    q = select(m.Invoice).where(m.Invoice.id != invoice.id, m.Invoice.status == "PROCESSED",
                                (m.Invoice.document_type.is_(None)) | (m.Invoice.document_type == "INVOICE"))
    if vendor_id is not None:
        q = q.where(m.Invoice.vendor_id == vendor_id)
    rows = db.scalars(q.order_by(m.Invoice.id.desc())).all()
    exact = [r for r in rows if r.invoice_number_norm == norm]
    if exact:
        return next((r for r in exact if r.decision == "APPROVED"), exact[0])
    close = [r for r in rows if r.invoice_number_norm and Levenshtein.distance(r.invoice_number_norm, norm) <= 1]
    return close[0] if len(close) == 1 else None


def _latest(db: Session, inv_id: int) -> m.ProcessingRun | None:
    return db.scalar(select(m.ProcessingRun).where(m.ProcessingRun.invoice_id == inv_id)
                     .order_by(m.ProcessingRun.run_no.desc()).limit(1))


def prior_notes(db: Session, original_id: int, exclude_id: int | None = None) -> list[m.Invoice]:
    q = select(m.Invoice).where(m.Invoice.original_invoice_id == original_id, m.Invoice.decision == "APPROVED")
    if exclude_id is not None:
        q = q.where(m.Invoice.id != exclude_id)
    return list(db.scalars(q.order_by(m.Invoice.id)))


def _line_sim(li, pair: dict) -> float:
    code = (li.item_code or "").strip().upper()
    if code and code in {(pair["invoice"].get("item_code") or "").upper(), (pair.get("po_item_code") or "").upper()}:
        return 1.0
    a = (li.description or "").lower()
    b = f"{pair.get('invoice_description') or ''} {pair.get('po_description') or ''}".lower()
    return fuzz.token_set_ratio(a, b) / 100 if a and b else 0.0


def _empty_match(explanation: str) -> dict:
    return {"method": "VIA_ORIGINAL_INVOICE", "selected_po": None, "selected_po_id": None, "ambiguous": False,
            "candidates": [], "issues": [], "pairs": [], "unmatched_lines": [], "variances": [],
            "explanation": explanation, "explicit_reference": None, "semantic": {"available": False, "error": None}}


def process_note(db: Session, invoice: m.Invoice, inv: ExtractedInvoice, vendor: dict | None, snaps: dict,
                 rules: dict) -> tuple[dict, dict, list[dict]]:
    """Returns (match, note_info, control_issues) for a credit or debit note."""
    kind = document_type(inv)
    label = LABEL[kind]
    tol = D(rules["notes"]["amount_abs"])
    min_sim = float(rules["notes"]["line_min_similarity"])
    ref = inv.get("original_invoice_number")
    issues: list[dict] = []
    total = abs(D(inv.get("grand_total")) or ZERO)
    cur = inv.get("currency")
    info = {"type": kind, "original_reference": ref, "original": None, "amount": s(total), "lines": []}

    original = find_original(db, invoice, vendor["id"] if vendor else None, ref)
    if not ref:
        issues.append(issue(rules, "note_without_original", "NOTE_NO_ORIGINAL_REFERENCE", "M24",
                            f"{label} does not say which invoice it adjusts - find the original invoice and "
                            "correct the 'original invoice number' field."))
        return _empty_match(f"{label} without an original-invoice reference."), info, issues
    if original is None:
        issues.append(issue(rules, "note_without_original", "NOTE_ORIGINAL_NOT_FOUND", "M24",
                            f"{label} refers to invoice {ref}, which is not in the system for this vendor. "
                            "Process the original first, or correct the reference.", {"reference": ref}))
        return _empty_match(f"Original invoice {ref} not found."), info, issues

    others = prior_notes(db, original.id, exclude_id=invoice.id)
    credited = sum((abs(o.grand_total or ZERO) for o in others if o.document_type == "CREDIT_NOTE"), ZERO)
    debited = sum((abs(o.grand_total or ZERO) for o in others if o.document_type == "DEBIT_NOTE"), ZERO)
    orig_total = original.grand_total or ZERO
    info["original"] = {"id": original.id, "invoice_number": original.invoice_number, "decision": original.decision,
                        "grand_total": s(orig_total), "currency": original.currency,
                        "po_number": original.po_reference, "credited_before": s(credited),
                        "debited_before": s(debited), "tds_section": original.tds_section,
                        "had_tds": bool(original.tds_amount and original.tds_amount > 0)}
    invoice.original_invoice_id = original.id

    if original.decision != "APPROVED":
        issues.append(issue(rules, "note_without_original", "NOTE_ORIGINAL_NOT_APPROVED", "M24",
                            f"Original invoice {original.invoice_number} is {(original.decision or 'not decided').lower().replace('_', ' ')}; "
                            f"a {label.lower()} can only be applied to an approved invoice.",
                            {"original_id": original.id}))
    if cur and original.currency and cur != original.currency:
        issues.append(issue(rules, "currency_mismatch", "NOTE_CURRENCY_MISMATCH", "M8",
                            f"{label} is in {cur} but invoice {original.invoice_number} is in {original.currency}."))
    if kind == "CREDIT_NOTE":
        available = orig_total - credited + debited
        info["available_to_credit"] = s(available)
        if total > available + tol:
            issues.append(issue(rules, "credit_exceeds_original", "CREDIT_EXCEEDS_ORIGINAL", "M24",
                                f"Credit note for {fmt_money(total, cur)} exceeds what is left on invoice "
                                f"{original.invoice_number} ({fmt_money(available, cur)} after earlier notes).",
                                {"credit": s(total), "available": s(available)}))

    # --------------------------------------------------------------- lines -> original's PO lines
    orun = _latest(db, original.id)
    opairs = (orun.matching or {}).get("pairs", []) if orun else []
    po_snap = snaps.get(original.po_reference) if original.po_reference else None
    if po_snap is None and original.po_id:
        po_snap = next((sn for sn in snaps.values() if sn["id"] == original.po_id), None)
    returned_before: dict[int, Decimal] = {}
    for o in others:
        if o.document_type != "CREDIT_NOTE":
            continue
        r = _latest(db, o.id)
        for p in (r.matching or {}).get("pairs", []) if r else []:
            q_ = D(p["invoice"]["quantity"]) or ZERO
            returned_before[p["po_line_no"]] = returned_before.get(p["po_line_no"], ZERO) - q_
    sign = Decimal(-1) if kind == "CREDIT_NOTE" else Decimal(1)
    pairs, unmatched = [], []
    by_po_line = {ln["line_no"]: ln for ln in (po_snap or {}).get("lines", [])}
    for li in inv.line_items:
        amount = abs(D(li.line_total) or ZERO)
        best = max(opairs, key=lambda p: _line_sim(li, p), default=None)
        sim = _line_sim(li, best) if best else 0.0
        if best is None or sim < min_sim:
            best, sim = (opairs[0] if opairs else None), sim
            note = "No matching line on the original; value allocated to its first PO line."
            unmatched.append(li.line_no)
        else:
            note = None
        if best is None:
            continue
        qty, price = abs(D(li.quantity) or ZERO), abs(D(li.unit_price) or ZERO)
        oprice = D(best["invoice"].get("unit_price")) or ZERO
        goods = bool(qty and price and oprice and abs(price - oprice) <= max(Decimal("0.01"), oprice * Decimal("0.01")))
        q_ = qty if goods else ZERO
        line_info = {"line_no": li.line_no, "description": li.description, "po_line_no": best["po_line_no"],
                     "kind": ("RETURN" if kind == "CREDIT_NOTE" else "EXTRA_QUANTITY") if goods else "VALUE",
                     "quantity": s(q_), "amount": s(q2(amount)), "similarity": round(sim, 2), "note": note}
        info["lines"].append(line_info)
        if kind == "CREDIT_NOTE" and goods:
            billed = D(best["invoice"].get("quantity")) or ZERO
            left = billed - returned_before.get(best["po_line_no"], ZERO)
            if q_ > left:
                issues.append(issue(rules, "credit_exceeds_original", "CREDIT_QTY_EXCEEDS_BILLED", "M24",
                                    f"Line {li.line_no} returns {q_} but only {left} of '{best['invoice_description']}' "
                                    f"remain on invoice {original.invoice_number}.", {"returned": s(q_), "left": s(left)}))
        pl = by_po_line.get(best["po_line_no"], {})
        pairs.append({"invoice_line_no": li.line_no, "invoice_description": li.description,
                      "po_line_no": best["po_line_no"], "po_description": best.get("po_description"),
                      "po_item_code": best.get("po_item_code"), "similarity": round(sim, 3),
                      "invoice": {"item_code": li.item_code, "quantity": s(sign * q_), "unit_price": s(price) if price else None,
                                  "tax_rate": li.tax_rate, "line_total": s(sign * q2(amount))},
                      "po": {"quantity": s(pl.get("quantity")), "unit_price": s(pl.get("unit_price")),
                             "tax_rate": s(pl.get("tax_rate")), "line_amount": s(pl.get("line_amount")),
                             "consumed_qty": s(pl.get("consumed_qty")), "remaining_qty": s(pl.get("remaining_qty")),
                             "uom": pl.get("uom")}})

    match = _empty_match("")
    match.update(pairs=pairs, unmatched_lines=unmatched)
    if po_snap:
        match.update(selected_po=po_snap["po_number"], selected_po_id=po_snap["id"])
    net = sum((abs(D(p["invoice"]["line_total"]) or ZERO) for p in pairs), ZERO)
    if kind == "DEBIT_NOTE" and po_snap:
        amt_tol = D(rules["tolerances"]["total_amount_pct"])
        cap = po_snap["subtotal"] * (1 + amt_tol / 100)
        if po_snap["consumed_amount"] + net > cap:
            match["issues"].append({"code": "CUMULATIVE_VALUE_EXCEEDED",
                                    "message": f"Debit note takes {po_snap['po_number']} to "
                                               f"{fmt_money(po_snap['consumed_amount'] + net, cur)} invoiced, above its "
                                               f"value {fmt_money(po_snap['subtotal'], cur)} + {amt_tol}% tolerance.",
                                    "evidence": {"consumed": s(po_snap["consumed_amount"]), "debit": s(net)}})
        for p in pairs:
            q_ = D(p["invoice"]["quantity"]) or ZERO
            rem = D(p["po"]["remaining_qty"])
            if q_ > 0 and rem is not None and q_ > rem:
                match["issues"].append({"code": "QTY_EXCEEDS_REMAINING", "line": p["invoice_line_no"],
                                        "message": f"Debit note line {p['invoice_line_no']} bills {q_} more but only "
                                                   f"{rem} remain on {po_snap['po_number']} line {p['po_line_no']}.",
                                        "evidence": {"invoiced": s(q_), "remaining": s(rem)}})
        issues.append(issue(rules, "debit_note", "DEBIT_NOTE_RAISED", "M25",
                            f"Vendor debit note adds {fmt_money(total, cur)} to invoice {original.invoice_number} - "
                            "confirm the reason (price revision / extra charge) with the buyer."))
    returned = [ln for ln in info["lines"] if ln["kind"] == "RETURN"]
    if kind == "CREDIT_NOTE":
        new_total = orig_total - credited + debited - total
        info["original_net_after"] = s(new_total)
        match["explanation"] = (f"Credit note against invoice {original.invoice_number}: payable falls from "
                                f"{fmt_money(orig_total - credited + debited, cur)} to {fmt_money(new_total, cur)}"
                                + (f"; {sum(D(r['quantity']) for r in returned)} unit(s) returned to {po_snap['po_number']}"
                                   if returned and po_snap else "")
                                + ("" if po_snap else "; the original has no PO, so no PO balance changes") + ".")
    else:
        info["original_net_after"] = s(orig_total - credited + debited + total)
        match["explanation"] = (f"Debit note against invoice {original.invoice_number}: adds {fmt_money(total, cur)}"
                                + (f" on {po_snap['po_number']}" if po_snap else "") + ".")
    return match, info, issues


def adjustments(db: Session, invoice: m.Invoice) -> dict | None:
    """Credit / debit notes applied to an invoice, and its net amount after them."""
    notes = list(db.scalars(select(m.Invoice).where(m.Invoice.original_invoice_id == invoice.id).order_by(m.Invoice.id)))
    if not notes:
        return None
    total = invoice.grand_total or ZERO
    net = total
    rows = []
    for n in notes:
        amt = abs(n.grand_total or ZERO)
        signed = -amt if n.document_type == "CREDIT_NOTE" else amt
        if n.decision == "APPROVED":
            net += signed
        rows.append({"id": n.id, "number": n.invoice_number, "type": n.document_type, "amount": s(signed),
                     "decision": n.decision, "status": n.status, "invoice_date": n.invoice_date})
    return {"notes": rows, "original_total": s(total), "net_total": s(q2(net)), "currency": invoice.currency}
