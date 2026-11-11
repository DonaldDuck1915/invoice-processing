"""Duplicate detection: exact rules + weighted multi-signal similarity + content fingerprint.

CONFIRMED  identical file bytes, or same vendor + same invoice number + same date + same amount
PROBABLE   weighted similarity >= threshold (e.g. re-sent copy with an OCR-garbled invoice number)
NEW        otherwise. Vendor + amount alone can never reach PROBABLE (max 0.45 by design).
"""
from __future__ import annotations

import hashlib
from datetime import date

from .extraction.parsers import normalize_ref, ocr_normalize
from .money import D, q2, s
from .schemas import ExtractedInvoice

# Pairs OCR commonly confuses - substituting these costs little; any other change means a different number.
CONFUSABLE = {frozenset(p) for p in ["08", "17", "56", "38", "68", "09", "5S", "0O", "1I", "1L", "2Z", "8B", "6G"]}


def _weighted_edit(a: str, b: str) -> float:
    """Edit distance where OCR-confusable substitutions cost 0.3 and replacing one digit with a
    non-confusable digit costs 5 (that is a different sequence number, not a misread)."""
    def indel(ch: str) -> float:  # dropping/adding a digit changes the number; a letter/symbol is a misread
        return 2.5 if ch.isdigit() else 1.0

    prev = [0.0]
    for cb in b:
        prev.append(prev[-1] + indel(cb))
    for i, ca in enumerate(a, 1):
        cur = [prev[0] + indel(ca)]
        for j, cb in enumerate(b, 1):
            if ca == cb:
                sub = 0.0
            elif frozenset((ca, cb)) in CONFUSABLE:
                sub = 0.3
            elif ca.isdigit() and cb.isdigit():
                sub = 5.0
            else:
                sub = 1.0
            cur.append(min(prev[j] + indel(ca), cur[j - 1] + indel(cb), prev[j - 1] + sub))
        prev = cur
    return prev[-1]


def invoice_number_similarity(a: str | None, b: str | None, max_edits: int) -> tuple[float, str]:
    if not a or not b:
        return 0.0, "invoice number missing on one side"
    na, nb = normalize_ref(a), normalize_ref(b)
    if na == nb:
        return 1.0, "identical"
    if ocr_normalize(a) == ocr_normalize(b):
        return 0.95, "identical after OCR-confusion normalisation (O/0, I/1, S/5 ...)"
    cost = _weighted_edit(na, nb)
    if cost <= 0.6 + 1e-9:
        return 0.85, f"differs only by OCR-confusable characters (cost {cost:.1f})"
    if cost <= max_edits:
        # an inserted/dropped character plus at most OCR-type confusions
        return 0.6, f"differs by an inserted/dropped or misread character (cost {cost:.1f})"
    return 0.1, "different invoice number"


def fingerprint(inv: ExtractedInvoice) -> str | None:
    """Format-independent content hash: vendor tax ID, date, total and (qty, amount) of every line."""
    tax, d, total = inv.get("vendor_tax_id"), inv.get("invoice_date"), inv.get("grand_total")
    if not (tax and d and total):
        return None
    lines = sorted(f"{s(D(li.quantity))}x{s(q2(D(li.line_total)) if li.line_total else None)}" for li in inv.line_items)
    raw = "|".join([normalize_ref(tax), d, s(q2(D(total))), *lines])
    return hashlib.sha256(raw.encode()).hexdigest()


def _line_similarity(a: list[dict], b: list[dict]) -> float:
    if not a or not b:
        return 0.0
    used, hits = set(), 0
    for la in a:
        for j, lb in enumerate(b):
            if j in used:
                continue
            if la["qty"] == lb["qty"] and la["amount"] is not None and lb["amount"] is not None \
                    and abs(la["amount"] - lb["amount"]) <= 1:
                used.add(j)
                hits += 1
                break
    return hits / max(len(a), len(b))


def header_of(inv: ExtractedInvoice) -> dict:
    return {"vendor_tax_id": normalize_ref(inv.get("vendor_tax_id")) or None, "invoice_number": inv.get("invoice_number"),
            "invoice_date": inv.get("invoice_date"), "grand_total": D(inv.get("grand_total")),
            "po_reference": normalize_ref(inv.get("po_number")) or None,
            "lines": [{"qty": D(li.quantity), "amount": D(li.line_total)} for li in inv.line_items]}


def check_duplicates(inv: ExtractedInvoice, file_sha256: str, vendor_id: int | None, history: list[dict],
                     rules: dict) -> dict:
    """history: [{id, filename, file_sha256, vendor_id, vendor_tax_id, invoice_number, invoice_date,
    grand_total, po_reference, lines, decision, created_at}] - excluding the invoice itself."""
    cfg = rules["duplicates"]
    w = cfg["weights"]
    me = header_of(inv)
    fp = fingerprint(inv)
    results = []
    for h in history:
        if h["file_sha256"] == file_sha256:
            results.append({"invoice_id": h["id"], "filename": h["filename"], "status": "CONFIRMED", "score": 1.0,
                            "rule": "Identical file (same SHA-256 hash).", "signals": {}, "decision": h["decision"]})
            continue
        same_vendor = (vendor_id is not None and h["vendor_id"] == vendor_id) or \
                      (me["vendor_tax_id"] and h["vendor_tax_id"] and normalize_ref(h["vendor_tax_id"]) == me["vendor_tax_id"])
        if not same_vendor:
            continue
        signals = {}
        num_sim, num_note = invoice_number_similarity(me["invoice_number"], h["invoice_number"],
                                                      int(cfg["invoice_no_max_edits"]))
        signals["invoice_number"] = {"value": num_sim, "note": f"{me['invoice_number']} vs {h['invoice_number']}: {num_note}"}
        if me["invoice_date"] and h["invoice_date"]:
            gap = abs((date.fromisoformat(me["invoice_date"]) - date.fromisoformat(h["invoice_date"])).days)
            dv = 1.0 if gap == 0 else (0.5 if gap <= int(cfg["date_window_days"]) else 0.0)
            signals["date"] = {"value": dv, "note": f"{me['invoice_date']} vs {h['invoice_date']} ({gap} day(s) apart)"}
        else:
            signals["date"] = {"value": 0.0, "note": "date missing on one side"}
        if me["grand_total"] is not None and h["grand_total"] is not None:
            diff = abs(me["grand_total"] - D(h["grand_total"]))
            signals["amount"] = {"value": 1.0 if diff <= D(cfg["amount_abs"]) else 0.0,
                                 "note": f"{s(me['grand_total'])} vs {s(D(h['grand_total']))}"}
        else:
            signals["amount"] = {"value": 0.0, "note": "amount missing on one side"}
        po_same = bool(me["po_reference"] and h["po_reference"] and me["po_reference"] == normalize_ref(h["po_reference"]))
        signals["po_reference"] = {"value": 1.0 if po_same else 0.0,
                                   "note": f"{me['po_reference'] or '-'} vs {h['po_reference'] or '-'}"}
        ls = _line_similarity(me["lines"], h.get("lines") or [])
        signals["line_items"] = {"value": round(ls, 3), "note": f"{ls:.0%} of lines have identical quantity and amount"}
        signals["fingerprint"] = {"value": 1.0 if fp and fp == h.get("content_fingerprint") else 0.0,
                                  "note": "content fingerprint " + ("identical" if fp and fp == h.get("content_fingerprint")
                                                                    else "differs")}
        score = round(sum(sig["value"] * float(w[k]) for k, sig in signals.items()), 3)
        exact_rule = (num_sim == 1.0 and signals["date"]["value"] == 1.0 and signals["amount"]["value"] == 1.0)
        status = "CONFIRMED" if exact_rule else ("PROBABLE" if score >= float(cfg["probable_threshold"]) else "NEW")
        results.append({"invoice_id": h["id"], "filename": h["filename"], "status": status, "score": score,
                        "signals": signals, "decision": h["decision"],
                        "rule": ("Same vendor, invoice number, date and amount." if exact_rule else
                                 f"Weighted similarity {score:.2f} (threshold {cfg['probable_threshold']}).")})
    order = {"CONFIRMED": 2, "PROBABLE": 1, "NEW": 0}
    results.sort(key=lambda r: (-order[r["status"]], -r["score"]))
    best = results[0] if results else None
    status = best["status"] if best else "NEW"
    return {"status": status, "best": best if best and best["status"] != "NEW" else None,
            "closest": results[:3], "compared": len(history), "fingerprint": fp,
            "explanation": (f"{status.title()} duplicate of invoice #{best['invoice_id']} ({best['filename']}): {best['rule']}"
                            if best and status != "NEW" else
                            f"No duplicate found among {len(history)} previously processed invoice(s)"
                            + (f"; closest match scored {results[0]['score']:.2f}." if results else "."))}
