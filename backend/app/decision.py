"""Rule-based decision engine.

All rules are evaluated (no short-circuit) so the report lists every hit.
Precedence: REJECT > MANUAL_REVIEW > APPROVED.
A REJECT that depends on data we could not read reliably is downgraded to MANUAL_REVIEW:
the system never hard-rejects on an uncertain reading.
"""
from __future__ import annotations

from .money import fmt_money
from .schemas import CheckResult, Decision, ExtractedInvoice, RuleHit

RELIABLE = ("EXTRACTED", "CORRECTED", "DERIVED")

NEXT_ACTIONS = {
    "R1": "Do not pay. Notify the vendor that this invoice was already received and processed.",
    "R2": "Return the invoice to the vendor - the PO was cancelled. Ask procurement whether a new PO is needed.",
    "R3": "Return the invoice - the PO is closed and fully billed. Ask the vendor for a credit note or the correct PO.",
    "R4": "Do not pay a blocked vendor. Escalate to vendor management.",
    "R5": "Reject and ask the vendor to re-issue the invoice against their own PO.",
    "R6": "Reject the excess quantity. Ask the vendor for a corrected invoice or obtain a PO amendment.",
    "M1": "Obtain the missing information from the vendor (or correct the field if it is printed) and re-run.",
    "M2": "Check the highlighted fields against the PDF and correct them if needed.",
    "M3": "Confirm the correct PO from the ranked candidates.",
    "M4": "Compare with the possible original and resolve: duplicate (reject) or not a duplicate (re-run).",
    "M5": "Obtain approval for the price variance from procurement, or request a credit note from the vendor.",
    "M6": "Obtain approval for the amount variance, or request a corrected invoice.",
    "M7": "Ask the vendor to clarify the calculation, or correct a misread figure.",
    "M8": "Confirm the invoicing currency with the vendor and procurement.",
    "M9": "Verify the vendor's status/identity with vendor management before approving.",
    "M10": "Check whether the unmatched line was ordered; approve manually or reject that line.",
    "M11": "Confirm the invoice date with the vendor.",
    "M12": "Confirm with procurement whether the closed PO may still be billed.",
    "M13": "Confirm the corrected PO reference.",
    "M14": "Check tax rates and amounts against the PO and GST/VAT rules.",
    "M15": "Confirm whether freight/other charges were agreed.",
    "M16": "Review the document manually; automatic extraction failed.",
    "M17": "Confirm the invoice currency.",
    "M18": "Approve or reject it manually: the auto-approval limit does not allow automatic approval.",
    "M19": "Check whether one purchase was split into several invoices to stay below the approval limit.",
    "M20": "Do not pay to the new account until the change is confirmed with the vendor on a known phone number.",
    "M21": "Confirm the unusually large amount with the buyer / procurement before paying.",
    "M22": "Ask procurement why the invoice predates the PO (retro PO) and get it approved.",
    "M23": "Ask the vendor for a valid GST e-invoice (IRN + signed QR code) before claiming input tax credit.",
    "M24": "Link the note to the correct, approved original invoice (or correct its amounts) and re-run.",
    "M25": "Confirm the debit note's reason with the buyer, then approve or reject it.",
    "R8": "Return the document to the vendor.",
}


def _reliable(inv: ExtractedInvoice, *names: str) -> bool:
    return all(inv.field(n).status in RELIABLE for n in names if inv.field(n).value is not None)


def decide(inv: ExtractedInvoice, checks: list[CheckResult], vendor_res: dict, match: dict, dup: dict,
           rules: dict, duplicate_resolution: str | None = None, stage_errors: list[str] | None = None,
           controls: dict | None = None) -> Decision:
    pol = rules["policy"]
    hits: list[RuleHit] = []
    issues = {i["code"]: i for i in match.get("issues", [])}
    issue_list = match.get("issues", [])
    cur = inv.get("currency") or ""
    flags: list[str] = []

    def hit(rule, outcome, message, evidence=None, unreliable=False):
        hits.append(RuleHit(rule=rule, outcome=outcome, message=message, evidence=evidence or {},
                            depends_on_unreliable_data=unreliable))

    # ---------------------------------------------------------------- hard rejects
    if dup["status"] == "CONFIRMED" and duplicate_resolution != "NOT_DUPLICATE":
        hit("R1", "REJECT", dup["explanation"], {"matched_invoice_id": dup["best"]["invoice_id"]})
        flags.append("CONFIRMED_DUPLICATE")
    elif duplicate_resolution == "IS_DUPLICATE":
        hit("R1", "REJECT", "A reviewer confirmed this invoice is a duplicate.")
    if "PO_CANCELLED" in issues:
        hit("R2", "REJECT" if pol["cancelled_po"] == "REJECT" else "MANUAL_REVIEW", issues["PO_CANCELLED"]["message"],
            unreliable=match["method"] == "EXPLICIT" and not _reliable(inv, "po_number"))
    if "PO_CLOSED_NO_BALANCE" in issues:
        hit("R3", "REJECT" if pol["closed_po_no_balance"] == "REJECT" else "MANUAL_REVIEW",
            issues["PO_CLOSED_NO_BALANCE"]["message"],
            unreliable=match["method"] == "EXPLICIT" and not _reliable(inv, "po_number"))
    v = vendor_res.get("vendor")
    if v and v["status"] != "APPROVED":
        flags.append(f"VENDOR_{v['status']}")
        if pol["unapproved_vendor"] == "REJECT" and v["status"] == "BLOCKED":
            hit("R4", "REJECT", f"Vendor {v['name']} is BLOCKED.", {"vendor_status": v["status"]})
        else:
            hit("M9", "MANUAL_REVIEW", f"FLAG: vendor {v['name']} is {v['status']} (not approved) - "
                                       "requires vendor-management sign-off before payment.",
                {"vendor_status": v["status"], "vendor_code": v["code"]})
    if "PO_VENDOR_MISMATCH" in issues:
        hit("R5", "REJECT", issues["PO_VENDOR_MISMATCH"]["message"], issues["PO_VENDOR_MISMATCH"].get("evidence"),
            unreliable=not _reliable(inv, "po_number", "vendor_tax_id") or vendor_res.get("weak", False))
    for i in issue_list:
        if i["code"] == "QTY_EXCEEDS_REMAINING":
            line = next((li for li in inv.line_items if li.line_no == i.get("line")), None)
            unreliable = bool(line and line.status not in RELIABLE)
            if dup["status"] == "PROBABLE":
                hit("M4", "MANUAL_REVIEW", i["message"] + " The excess is explained by the probable duplicate.",
                    i.get("evidence"))
            elif pol["qty_exceeds_remaining"] == "REJECT":
                hit("R6", "REJECT", i["message"], i.get("evidence"), unreliable=unreliable)
            else:
                hit("M6", "MANUAL_REVIEW", i["message"], i.get("evidence"))

    # ---------------------------------------------------------------- manual review
    for c in checks:
        if c.status != "FAIL":
            continue
        if c.code.startswith("V1."):
            hit("M1", "MANUAL_REVIEW", c.message)
        elif c.code.startswith("V8."):
            hit("M2", "MANUAL_REVIEW", c.message, c.details)
        elif c.code.split(".")[0] in ("V2", "V3", "V4", "V5"):
            hit("M7", "MANUAL_REVIEW", c.message, {"expected": c.expected, "actual": c.actual})
        elif c.code.startswith("V6."):
            hit("M11", "MANUAL_REVIEW", c.message)
        elif c.code == "V7.vendor_known":
            hit("M9", "MANUAL_REVIEW", c.message)
        elif c.code == "V9.currency":
            hit("M17", "MANUAL_REVIEW", c.message)
    if vendor_res.get("weak") and v:
        hit("M9", "MANUAL_REVIEW", f"Vendor matched by name only ({v['name']}), not by tax ID.")
    if vendor_res.get("conflict"):
        hit("M9", "MANUAL_REVIEW", "Vendor identity conflict: " + vendor_res["conflict"])
        flags.append("VENDOR_IDENTITY_CONFLICT")
    for code, rule in [("PO_AMBIGUOUS", "M3"), ("PO_NOT_IDENTIFIED", "M3"), ("PO_NOT_FOUND", "M3")]:
        if code in issues and not match.get("selected_po"):
            hit(rule, "MANUAL_REVIEW", issues[code]["message"], issues[code].get("evidence"))
    is_note = match.get("method") == "VIA_ORIGINAL_INVOICE"
    if not is_note and not match.get("selected_po") and not any(h.rule in ("M3", "R5") for h in hits):
        hit("M3", "MANUAL_REVIEW", match.get("explanation") or "No purchase order could be identified.")
    if dup["status"] == "PROBABLE" and duplicate_resolution != "NOT_DUPLICATE" and not any(h.rule == "M4" for h in hits):
        hit("M4", "MANUAL_REVIEW", dup["explanation"], {"matched_invoice_id": dup["best"]["invoice_id"],
                                                         "score": dup["best"]["score"]})
    if dup["status"] == "PROBABLE":
        flags.append("PROBABLE_DUPLICATE")
    simple = {"PRICE_OVER_TOLERANCE": "M5", "TOTAL_OVER_TOLERANCE": "M6", "CUMULATIVE_VALUE_EXCEEDED": "M6",
              "CURRENCY_MISMATCH": "M8", "UNMATCHED_INVOICE_LINE": "M10", "PO_CLOSED_WITH_BALANCE": "M12",
              "PO_FUZZY_CORRECTED": "M13", "TAX_RATE_MISMATCH": "M14", "TAX_VARIANCE": "M14",
              "UNPLANNED_CHARGES": "M15"}
    for i in issue_list:
        rule = simple.get(i["code"])
        if not rule:
            continue
        if i["code"] == "CUMULATIVE_VALUE_EXCEEDED" and dup["status"] == "PROBABLE":
            hit("M4", "MANUAL_REVIEW", i["message"] + " The excess is explained by the probable duplicate.",
                i.get("evidence"))
            continue
        if i["code"] == "CURRENCY_MISMATCH" and pol["currency_mismatch"] == "REJECT":
            hit("R7", "REJECT", i["message"], i.get("evidence"))
        else:
            hit(rule, "MANUAL_REVIEW", i["message"], i.get("evidence"))
    # ---------------------------------------------------------------- AP controls
    unreliable_by_code = {"BANK_DETAILS_CHANGED": not _reliable(inv, "bank_account"),
                          "EINVOICE_MISMATCH": not _reliable(inv, "irn", "grand_total", "invoice_number")}
    for i in (controls or {}).get("issues", []):
        if i["outcome"] == "FLAG":
            flags.append(i["code"])
            continue
        outcome = "REJECT" if i["outcome"] == "REJECT" else "MANUAL_REVIEW"
        hit(i["rule"] if outcome == "MANUAL_REVIEW" else "R8", outcome, i["message"], i.get("evidence"),
            unreliable=unreliable_by_code.get(i["code"], False))
        flags.append(i["code"])
    approval = (controls or {}).get("approval") or {}
    for err in stage_errors or []:
        hit("M16", "MANUAL_REVIEW", err)
    if inv.field("currency").value is None:
        flags.append("CURRENCY_ASSUMED")
    if inv.meta.get("is_scanned"):
        flags.append("SCANNED")
    if not inv.meta.get("ai_used"):
        flags.append("AI_NOT_USED")

    # ---------------------------------------------------------------- precedence
    for h in hits:
        if h.outcome == "REJECT" and h.depends_on_unreliable_data:
            h.outcome = "MANUAL_REVIEW"
            h.message += " (Would be a rejection, but it depends on a value that was not read reliably.)"
    rejects = [h for h in hits if h.outcome == "REJECT"]
    reviews = [h for h in hits if h.outcome == "MANUAL_REVIEW"]
    passed = [c.name for c in checks if c.status == "PASS"]
    failed = [c.name for c in checks if c.status == "FAIL"]
    ref = inv.get("invoice_number") or "(no number)"
    vendor_name = v["name"] if v else (inv.get("vendor_name") or "unknown vendor")
    total = fmt_money(_dec(inv.get("grand_total")), cur) if inv.get("grand_total") else "unknown amount"

    if rejects:
        decision = "REJECTED"
        summary = f"Rejected: {rejects[0].message}"
        if len(rejects) > 1:
            summary += f" (+{len(rejects) - 1} more hard failure(s))"
        next_action = NEXT_ACTIONS.get(rejects[0].rule, "Return the invoice to the vendor.")
    elif reviews:
        decision = "MANUAL_REVIEW"
        top = "; ".join(h.message.rstrip(".") for h in _dedupe(reviews)[:3])
        summary = f"Manual review needed: {top}."
        next_action = NEXT_ACTIONS.get(_dedupe(reviews)[0].rule, "Review the flagged items.")
        if _dedupe(reviews)[0].rule == "M18" and approval.get("level") == "MANAGER":
            next_action = "Route to a manager: the auto-approval limit requires a manager's approval."
    else:
        decision = "APPROVED"
        n_var = sum(1 for x in match.get("variances", []) if not x["exceeded"])
        summary = (f"Approved: invoice {ref} from {vendor_name} for {total} matches {match['selected_po']} "
                   f"({match['method'].replace('_', ' ').lower()} match); {len(passed)} checks passed and all "
                   f"{n_var} variances are within tolerance; no duplicate found.")
        next_action = "Post the invoice for payment on its due date."
        tds = (controls or {}).get("tds") or {}
        if tds.get("applicable") and D_(tds.get("amount")):
            next_action = (f"Post for payment: pay the vendor {fmt_money(_dec(tds['net_payable']), cur)} and deposit TDS "
                           f"{fmt_money(_dec(tds['amount']), cur)} under section {tds['section']}.")
        if is_note:
            kind = (controls or {}).get("document_type", "CREDIT_NOTE")
            summary = (f"Approved: {'credit' if kind == 'CREDIT_NOTE' else 'debit'} note {ref} from {vendor_name} for "
                       f"{total}. {match.get('explanation') or ''}").strip()
            next_action = ("Apply the credit against the vendor's next payment." if kind == "CREDIT_NOTE"
                           else "Pay the debit note together with the original invoice.")
    return Decision(decision=decision, summary=summary, reasons=_dedupe(rejects) + _dedupe(reviews),
                    passed_checks=passed, failed_checks=failed, next_action=next_action, flags=sorted(set(flags)),
                    approval_level=approval.get("level", "AUTO"))


def D_(v):
    from .money import D

    x = D(v)
    return x is not None and x != 0


def _dec(v):
    from .money import D

    return D(v)


def _dedupe(hits: list[RuleHit]) -> list[RuleHit]:
    seen, out = set(), []
    for h in hits:
        key = (h.rule, h.message)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out
