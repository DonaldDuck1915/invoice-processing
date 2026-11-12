"""Accounts-payable controls run after matching and the duplicate check, before the decision.

  approval   auto-approval limit per currency; above it only a manager may approve. Invoices just
             under the limit are flagged, and several of them from one vendor in a short window are
             reviewed as a possible split to stay below the limit.
  fraud      bank details that differ from the vendor master / earlier invoices, an amount far above the
             vendor's usual, an invoice dated before its PO, arrival in the last days of a month.
  einvoice   Indian GST e-invoice: IRN present and well-formed, signed QR code present and consistent
             with what is printed (seller/buyer GSTIN, number, total, IRN).
  tds        Indian TDS to withhold from the payment, by the vendor's section, with annual thresholds.

Every finding is an "issue" with an outcome taken from the policy (MANUAL_REVIEW / REJECT / FLAG);
the decision engine turns those into rule hits. FLAG only adds a visible risk flag.
"""
from __future__ import annotations

import calendar
import re
import statistics
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .extraction.parsers import normalize_ref
from .extraction.qr import parse_einvoice_qr
from .money import D, ZERO, fmt_money, q2, s
from .notes import document_type, issue
from .schemas import ExtractedInvoice

IRN_RE = re.compile(r"[0-9a-f]{64}")
QR_DOC_TYPES = {"INV": "INVOICE", "CRN": "CREDIT_NOTE", "DBN": "DEBIT_NOTE"}


def mask(acct: str | None) -> str:
    return "not printed" if not acct else ("••••" + acct[-4:] if len(acct) > 4 else acct)


def _history(db: Session, invoice: m.Invoice, vendor_id: int | None) -> list[m.Invoice]:
    if vendor_id is None:
        return []
    return list(db.scalars(select(m.Invoice).where(m.Invoice.vendor_id == vendor_id, m.Invoice.id != invoice.id,
                                                   m.Invoice.status == "PROCESSED")
                           .order_by(m.Invoice.id.desc())))


def _is_invoice(i: m.Invoice) -> bool:
    return (i.document_type or "INVOICE") == "INVOICE"


# ============================================================================ approval limits

def approval_check(inv: ExtractedInvoice, kind: str, amount: Decimal | None, cur: str | None, history: list[m.Invoice],
                   rules: dict, ref_date: date) -> tuple[dict, list[dict]]:
    cfg = rules["approval"]
    limit = D((cfg.get("auto_approve_limit") or {}).get(cur or ""))
    out = {"currency": cur, "amount": s(amount), "limit": s(limit), "level": "AUTO", "near_limit": False}
    issues: list[dict] = []
    manager_only = cfg.get("above_limit_approver", "MANAGER") == "MANAGER"
    held = "MANAGER" if manager_only else "REVIEW"      # level for invoices the limit keeps from auto-approval
    who = "a manager must approve it" if manager_only else "it needs a reviewer's or manager's approval"
    if kind == "CREDIT_NOTE":
        out["limit"] = None
        out["message"] = "Credit notes reduce what we owe - no approval limit applies."
        return out, issues
    if amount is None:
        out["message"] = "Amount unknown - approval limit not evaluated."
        return out, issues
    if limit is None:
        # No limit for this currency: never approve automatically - a person decides.
        out["level"] = held
        out["message"] = (f"No auto-approval limit is set for {cur} - {who}. "
                          f"A manager can add a {cur} limit on the Rules & data page." if cur else
                          f"The invoice currency is unknown, so no approval limit applies - {who}.")
        issues.append(issue(rules, "above_approval_limit", "NO_APPROVAL_LIMIT", "M18", out["message"],
                            {"amount": s(amount), "currency": cur}))
        return out, issues
    band = limit * (1 - D(cfg["near_limit_pct"]) / 100)
    if amount > limit:
        out["level"] = held
        out["message"] = f"{fmt_money(amount, cur)} is above the {fmt_money(limit, cur)} auto-approval limit - {who}."
        issues.append(issue(rules, "above_approval_limit", "ABOVE_APPROVAL_LIMIT", "M18", out["message"],
                            {"amount": s(amount), "limit": s(limit)}))
        return out, issues
    out["message"] = f"Within the {fmt_money(limit, cur)} auto-approval limit."
    if amount >= band:
        out["near_limit"] = True
        pct = q2((limit - amount) / limit * 100)
        out["message"] = (f"{fmt_money(amount, cur)} is just under the {fmt_money(limit, cur)} auto-approval limit "
                          f"({pct}% below).")
        issues.append(issue(rules, "near_limit", "JUST_UNDER_APPROVAL_LIMIT", "M19", out["message"],
                            {"amount": s(amount), "limit": s(limit)}))
        window = int(cfg["split_window_days"])
        near = [h for h in history if _is_invoice(h) and h.decision != "REJECTED" and h.currency == cur
                and h.grand_total is not None and band <= h.grand_total <= limit
                and abs(((h.invoice_date or h.created_at.date()) - ref_date).days) <= window]
        if near:
            combined = amount + sum((h.grand_total for h in near), ZERO)
            msg = (f"Possible split to stay under the approval limit: {len(near)} other just-under-limit invoice(s) "
                   f"from this vendor within {window} days ({', '.join(h.invoice_number or '#' + str(h.id) for h in near[:3])}); "
                   f"together {fmt_money(combined, cur)}.")
            issues.append(issue(rules, "split_below_limit", "SPLIT_BELOW_LIMIT", "M19", msg,
                                {"invoices": [h.id for h in near], "combined": s(combined), "limit": s(limit)}))
            out["level"] = held
            out["split_suspected"] = True
    return out, issues


# ============================================================================ fraud signals

def fraud_checks(inv: ExtractedInvoice, kind: str, vendor: dict | None, history: list[m.Invoice], match: dict,
                 snaps: dict, rules: dict, amount: Decimal | None, cur: str | None, received: date) -> tuple[dict, list[dict]]:
    cfg = rules["fraud"]
    issues: list[dict] = []
    bank = {"printed_account": mask(inv.get("bank_account")), "printed_ifsc": inv.get("bank_ifsc"),
            "master_account": mask(vendor.get("bank_account")) if vendor else None,
            "master_ifsc": vendor.get("bank_ifsc") if vendor else None, "status": "NOT_PRINTED"}
    acct, ifsc = inv.get("bank_account"), inv.get("bank_ifsc")
    if acct and vendor:
        m_acct, m_ifsc = vendor.get("bank_account"), vendor.get("bank_ifsc")
        prev = next((h for h in history if h.bank_account), None)
        if m_acct:
            ref, ref_ifsc, source = m_acct, m_ifsc, "the vendor master"
        elif prev:
            ref, ref_ifsc, source = prev.bank_account, prev.bank_ifsc, f"invoice {prev.invoice_number or '#' + str(prev.id)}"
        else:
            ref = ref_ifsc = source = None
        if ref is None:
            bank["status"] = "UNVERIFIED"
            bank["message"] = (f"Bank account {mask(acct)} cannot be verified - none on file for this vendor. "
                               "Confirm it with the vendor and add it to the vendor master.")
        elif normalize_ref(acct) != normalize_ref(ref) or (ifsc and ref_ifsc and ifsc != ref_ifsc):
            bank["status"] = "CHANGED"
            bank["message"] = (f"Bank details changed: the document asks for payment to {mask(acct)}"
                               f"{' (' + ifsc + ')' if ifsc else ''}, but {source} has {mask(ref)}"
                               f"{' (' + ref_ifsc + ')' if ref_ifsc else ''}. Confirm by calling the vendor on a known "
                               "number before paying - a common invoice-fraud pattern.")
            issues.append(issue(rules, "bank_details_changed", "BANK_DETAILS_CHANGED", "M20", bank["message"],
                                {"printed": mask(acct), "on_file": mask(ref), "source": source}))
        else:
            bank["status"] = "MATCH"
            bank["message"] = f"Bank account {mask(acct)} matches {source}."
    elif acct:
        bank["status"] = "UNVERIFIED"
        bank["message"] = "Vendor not identified - bank account not verified."
    else:
        bank["message"] = "No bank details printed on the document."

    signals: dict = {"bank": bank}
    # unusual amount vs this vendor's history
    if kind == "INVOICE" and amount is not None:
        past = [h.grand_total for h in history if _is_invoice(h) and h.decision != "REJECTED" and h.currency == cur
                and h.grand_total]
        if len(past) >= int(cfg["unusual_amount_min_history"]):
            med = Decimal(str(statistics.median(past)))
            mult = D(cfg["unusual_amount_multiple"])
            signals["amount_vs_history"] = {"median": s(q2(med)), "invoices": len(past), "ratio": s(q2(amount / med)) if med else None}
            if med and amount > med * mult:
                msg = (f"Unusual amount: {fmt_money(amount, cur)} is {q2(amount / med)}x this vendor's median invoice "
                       f"({fmt_money(med, cur)} over {len(past)} invoices).")
                issues.append(issue(rules, "unusual_amount", "UNUSUAL_AMOUNT", "M21", msg,
                                    {"amount": s(amount), "median": s(q2(med))}))
    # invoice dated before the PO it bills
    d = inv.get("invoice_date")
    po = snaps.get(match.get("selected_po") or "")
    if kind == "INVOICE" and d and po and date.fromisoformat(d) < date.fromisoformat(po["po_date"]):
        msg = (f"Invoice dated {d}, before {po['po_number']} was raised ({po['po_date']}) - goods/services may have "
               "been ordered without a PO (retro PO).")
        issues.append(issue(rules, "invoice_before_po", "INVOICE_BEFORE_PO", "M22", msg,
                            {"invoice_date": d, "po_date": po["po_date"]}))
    # arrival at month end (period-end pressure to push invoices through)
    last = calendar.monthrange(received.year, received.month)[1]
    days = int(cfg["month_end_days"])
    if days and received.day > last - days:
        msg = f"Received on {received.isoformat()}, in the last {days} day(s) of the month - check for period-end pressure."
        issues.append(issue(rules, "month_end_arrival", "RECEIVED_AT_MONTH_END", "F1", msg, {"received": received.isoformat()}))
    return signals, issues


# ============================================================================ e-invoice (India)

def einvoice_check(inv: ExtractedInvoice, kind: str, vendor: dict | None, cur: str | None, rules: dict) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    if not rules["einvoice"]["enabled"]:
        return {"status": "DISABLED", "message": "E-invoice checks are switched off."}, issues
    required = bool(vendor and vendor.get("einvoice_required")) and (cur or "INR") == "INR"
    irn = inv.get("irn")
    qrs = inv.meta.get("qr_codes")
    parsed = next((p for p in (parse_einvoice_qr(t) for t in qrs or []) if p), None)
    out = {"required": required, "irn": irn, "irn_valid": bool(irn and IRN_RE.fullmatch(irn)),
           "qr_found": bool(qrs), "qr_is_einvoice": parsed is not None, "qr": parsed, "checks": [],
           "signature_verified": False, "qr_scanned": qrs is not None}
    if not required and not irn and not parsed:
        out["status"], out["message"] = "NOT_APPLICABLE", "Vendor is not flagged for GST e-invoicing; no IRN/QR printed."
        return out, issues
    problems = []
    if irn and not out["irn_valid"]:
        problems.append(f"IRN {irn[:12]}… is not a 64-character hexadecimal reference")
    if required and not irn and not parsed:
        out["status"] = "MISSING"
        out["message"] = ("Vendor must issue GST e-invoices, but no IRN or signed QR code is on the document - "
                          "input tax credit may be refused. Ask the vendor for the e-invoice.")
        issues.append(issue(rules, "einvoice_missing", "EINVOICE_MISSING", "M23", out["message"]))
        return out, issues
    if required and qrs is not None and parsed is None:
        out["status"] = "MISSING"
        out["message"] = "IRN printed, but no readable e-invoice QR code was found on the document."
        issues.append(issue(rules, "einvoice_missing", "EINVOICE_QR_MISSING", "M23", out["message"]))
    if parsed:
        tol = D(rules["notes"]["amount_abs"])
        company = {t.upper() for t in rules.get("company", {}).get("tax_ids", [])}

        def chk(name, expected, actual, ok):
            out["checks"].append({"name": name, "printed": expected, "qr": actual, "ok": ok})
            if not ok:
                problems.append(f"{name}: printed {expected or '—'}, QR {actual or '—'}")

        chk("Seller GSTIN", inv.get("vendor_tax_id"), parsed["seller_gstin"],
            not inv.get("vendor_tax_id") or normalize_ref(inv.get("vendor_tax_id")) == normalize_ref(parsed["seller_gstin"]))
        if parsed["buyer_gstin"]:
            chk("Buyer GSTIN", ", ".join(sorted(company)) or None, parsed["buyer_gstin"],
                not company or parsed["buyer_gstin"].upper() in company)
        chk("Document number", inv.get("invoice_number"), parsed["doc_no"],
            not inv.get("invoice_number") or normalize_ref(inv.get("invoice_number")) == normalize_ref(parsed["doc_no"]))
        total, qtotal = D(inv.get("grand_total")), D(parsed["total"])
        chk("Invoice value", s(total), s(q2(qtotal)) if qtotal is not None else None, total is None or qtotal is None or abs(abs(total) - qtotal) <= tol)
        if irn and parsed["irn"]:
            chk("IRN", irn[:16] + "…", parsed["irn"][:16] + "…", irn == parsed["irn"].lower())
        if parsed["doc_type"]:
            chk("Document type", kind, QR_DOC_TYPES.get(parsed["doc_type"], parsed["doc_type"]),
                QR_DOC_TYPES.get(parsed["doc_type"]) in (None, kind))
    if problems:
        out["status"] = "MISMATCH"
        out["message"] = ("E-invoice check failed - " + "; ".join(problems) +
                          ". The document may have been altered after the IRN was issued.")
        issues.append(issue(rules, "einvoice_mismatch", "EINVOICE_MISMATCH", "M23", out["message"],
                            {"problems": problems}))
    elif out.get("status") != "MISSING":
        out["status"] = "OK"
        out["message"] = ("IRN and signed QR code are present and match the printed invoice "
                          "(QR signature not verified: the IRP public key is not configured)." if parsed
                          else "IRN present and well-formed (no QR code to cross-check).")
    return out, issues


# ============================================================================ TDS (India)

def _fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def tds_check(db: Session, invoice: m.Invoice, inv: ExtractedInvoice, kind: str, vendor: dict | None, cur: str | None,
              rules: dict, ref_date: date, note: dict | None) -> dict:
    cfg = rules["tds"]
    out = {"applicable": False, "section": None}
    if not cfg["enabled"]:
        return {**out, "message": "TDS calculation is switched off."}
    if not vendor or vendor.get("country") != "IN" or (cur or "INR") != "INR":
        return {**out, "message": "TDS applies to Indian vendors billing in INR only."}
    section = vendor.get("tds_section")
    if kind != "INVOICE":
        orig = (note or {}).get("original") or {}
        section = orig.get("tds_section")
        if not orig.get("had_tds"):
            return {**out, "message": "No TDS was withheld on the original invoice, so none is adjusted."}
    if not section:
        return {**out, "message": "No TDS section set for this vendor in master data - TDS not computed."}
    sec = (cfg.get("sections") or {}).get(section)
    if not sec:
        return {**out, "section": section, "message": f"TDS section {section} has no rate configured."}
    grand, tax = D(inv.get("grand_total")), D(inv.get("tax_amount"))
    base = (abs(grand) - abs(tax)) if grand is not None and tax is not None else D(inv.get("taxable_amount")) or D(inv.get("subtotal"))
    if base is None:
        return {**out, "section": section, "message": "Taxable value unknown - TDS not computed."}
    base = q2(abs(base))
    rate = D(sec["rate"])
    fy = _fy_start(ref_date)
    prior = db.scalars(select(m.Invoice).where(m.Invoice.vendor_id == vendor["id"], m.Invoice.id != invoice.id,
                                               m.Invoice.decision == "APPROVED", m.Invoice.tds_base.is_not(None))).all()
    agg = sum((p.tds_base for p in prior if p.invoice_date and _fy_start(p.invoice_date) == fy), ZERO)
    sign = Decimal(-1) if kind == "CREDIT_NOTE" else Decimal(1)
    single, annual = D(sec["single_threshold"]), D(sec["annual_threshold"])
    note_txt = None
    if kind != "INVOICE":
        taxable = base
        why = f"adjusts the TDS withheld on invoice {(note or {}).get('original', {}).get('invoice_number')}"
    elif sec.get("excess_only"):
        taxable = max(ZERO, agg + base - annual) - max(ZERO, agg - annual)
        why = (f"on the part of this financial year's purchases above {fmt_money(annual, 'INR')}"
               if taxable else f"not due - purchases this year ({fmt_money(agg + base, 'INR')}) are within "
                               f"{fmt_money(annual, 'INR')}")
    else:
        due = (single and base > single) or agg + base > annual
        taxable = base if due else ZERO
        why = ("on the full taxable value" if due else
               f"not due - below the {fmt_money(single, 'INR')} single-payment and {fmt_money(annual, 'INR')} annual thresholds")
        if due and agg and agg <= annual < agg + base and not (single and base > single):
            note_txt = (f"This invoice takes the year's total past {fmt_money(annual, 'INR')}: earlier invoices this "
                        f"year ({fmt_money(agg, 'INR')}) may need catch-up TDS.")
    amount = (taxable * rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    out.update(applicable=True, section=section, rate=s(rate), base=s(sign * base), taxable=s(sign * taxable),
               amount=s(sign * amount), financial_year=f"{fy.year}-{str(fy.year + 1)[2:]}", aggregate_before=s(agg),
               note=note_txt)
    gross = abs(grand) if grand is not None else None
    if kind == "CREDIT_NOTE":
        out["net_effect"] = s(-(gross - amount)) if gross is not None else None
        out["message"] = (f"TDS {section} @ {s(rate)}%: reverses {fmt_money(amount, 'INR')} of TDS ({why}); "
                          f"payable to the vendor falls by {fmt_money(gross - amount, 'INR') if gross is not None else 'n/a'}.")
    elif not amount:
        out["net_payable"] = s(gross) if gross is not None else None
        out["message"] = f"TDS {section} @ {s(rate)}%: nothing to withhold - {why.removeprefix('not due - ')}."
    else:
        out["net_payable"] = s(gross - amount) if gross is not None else None
        out["message"] = (f"TDS {section} @ {s(rate)}%: withhold {fmt_money(amount, 'INR')} ({why}); pay the vendor "
                          f"{fmt_money(gross - amount, 'INR') if gross is not None else 'n/a'} and deposit the TDS.")
    return out


# ============================================================================ entry point

def run_controls(db: Session, invoice: m.Invoice, inv: ExtractedInvoice, vendor: dict | None, match: dict, snaps: dict,
                 rules: dict, today: date, note: dict | None = None, note_issues: list[dict] | None = None) -> dict:
    kind = document_type(inv)
    cur = inv.get("currency") or (snaps[match["selected_po"]]["currency"] if match.get("selected_po") in snaps else None)
    amount = D(inv.get("grand_total"))
    amount = abs(amount) if amount is not None else None
    ref_date = date.fromisoformat(inv.get("invoice_date")) if inv.get("invoice_date") else today
    received = (invoice.created_at.date() if invoice.created_at else today)
    history = _history(db, invoice, vendor["id"] if vendor else None)
    approval, a_issues = approval_check(inv, kind, amount, cur, history, rules, ref_date)
    signals, f_issues = fraud_checks(inv, kind, vendor, history, match, snaps, rules, amount, cur, received)
    einv, e_issues = einvoice_check(inv, kind, vendor, cur, rules)
    tds = tds_check(db, invoice, inv, kind, vendor, cur, rules, ref_date, note)
    issues = (note_issues or []) + a_issues + f_issues + e_issues
    risk = [{"code": i["code"], "message": i["message"],
             "severity": "HIGH" if i["code"] in ("BANK_DETAILS_CHANGED", "EINVOICE_MISMATCH", "SPLIT_BELOW_LIMIT")
             else "LOW" if i["outcome"] == "FLAG" else "MEDIUM"}
            for i in f_issues + [x for x in a_issues if x["code"] != "ABOVE_APPROVAL_LIMIT"]
            + [x for x in e_issues if x["code"] == "EINVOICE_MISMATCH"]]
    return {"document_type": kind, "approval": approval, "signals": signals, "einvoice": einv, "tds": tds,
            "note": note, "issues": issues, "risk": risk, "received_on": received.isoformat()}
