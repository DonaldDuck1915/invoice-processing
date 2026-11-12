"""Deterministic validation checks over an ExtractedInvoice. Pure functions; no DB access.

Each check reports PASS / FAIL / WARN / SKIPPED and classifies a failure as
MISSING (not found), INCORRECT (found but contradicts arithmetic/rules) or UNCERTAIN (low confidence).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .extraction.parsers import classify_tax_id, gstin_is_valid
from .money import D, ZERO, q2, s, supported_currencies
from .schemas import CheckResult, ExtractedInvoice
from .tax_mode import included_tax

CRITICAL_LABELS = {"vendor_identity": "Vendor identification (tax ID or name)", "invoice_number": "Invoice number",
                   "invoice_date": "Invoice date", "grand_total": "Grand total"}


def _money(inv: ExtractedInvoice, name: str) -> Decimal | None:
    f = inv.fields.get(name)
    return D(f.value) if f and f.value is not None else None


def check_mandatory(inv: ExtractedInvoice) -> list[CheckResult]:
    out = []
    tax, name = inv.field("vendor_tax_id"), inv.field("vendor_name")
    vendor_present = tax.value is not None or name.value is not None
    out.append(CheckResult(
        code="V1.vendor_identity", name="Mandatory: vendor identification",
        status="PASS" if vendor_present else "FAIL", category="OK" if vendor_present else "MISSING",
        severity="INFO" if vendor_present else "REVIEW",
        message=("Vendor identified by " + ("tax ID " + tax.value if tax.value else "name only"))
        if vendor_present else "Neither a vendor tax ID nor a vendor name could be found."))
    for fname in ("invoice_number", "invoice_date", "grand_total"):
        f = inv.field(fname)
        ok = f.value is not None
        out.append(CheckResult(
            code=f"V1.{fname}", name=f"Mandatory: {CRITICAL_LABELS[fname].lower()}",
            status="PASS" if ok else "FAIL", category="OK" if ok else "MISSING",
            severity="INFO" if ok else "REVIEW",
            message=f"{CRITICAL_LABELS[fname]} present: {f.value}" if ok
            else f"{CRITICAL_LABELS[fname]} is missing from the invoice{' (' + f.note + ')' if f.note else ''}.",
            actual=f.value))
    return out


def check_line_math(inv: ExtractedInvoice, rules: dict) -> list[CheckResult]:
    tol = D(rules["tolerances"]["rounding_abs"])
    out = []
    if not inv.line_items:
        return [CheckResult(code="V2.line_math", name="Line arithmetic", status="SKIPPED", category="MISSING",
                            severity="REVIEW", message="No line items could be extracted.")]
    for li in inv.line_items:
        qty, price, total = D(li.quantity), D(li.unit_price), D(li.line_total)
        code = f"V2.line_{li.line_no}"
        if qty is None or price is None or total is None:
            out.append(CheckResult(code=code, name=f"Line {li.line_no} arithmetic", status="SKIPPED",
                                   category="MISSING", severity="INFO",
                                   message=f"Line {li.line_no}: quantity, unit price or total not printed - cannot verify."))
            continue
        expected = q2(qty * price)
        allowed = max(tol, q2(qty * Decimal("0.005")))  # unit prices printed to 2dp can drift on large qty
        diff = abs(expected - total)
        ok = diff <= allowed
        out.append(CheckResult(
            code=code, name=f"Line {li.line_no} arithmetic", status="PASS" if ok else "FAIL",
            category="OK" if ok else "INCORRECT", severity="INFO" if ok else "REVIEW",
            expected=s(expected), actual=s(total),
            message=(f"Line {li.line_no}: {qty} x {price} = {expected} matches {total}." if ok else
                     f"Line {li.line_no} ({li.description or 'item'}): {qty} x {price} = {expected}, "
                     f"but the invoice states {total} (difference {q2(diff)})."),
            details={"difference": s(q2(diff)), "allowed": s(allowed)}))
    return out


def _lines_sum(inv: ExtractedInvoice) -> Decimal | None:
    totals = [D(li.line_total) for li in inv.line_items]
    if not totals or any(t is None for t in totals):
        return None
    return sum(totals, ZERO)


def check_subtotal(inv: ExtractedInvoice, rules: dict) -> CheckResult:
    tol = D(rules["tolerances"]["subtotal_abs"])
    lines = _lines_sum(inv)
    subtotal = _money(inv, "subtotal")
    label = "subtotal"
    if subtotal is None and _money(inv, "discount") in (None, ZERO):
        subtotal, label = _money(inv, "taxable_amount"), "taxable value"
    if lines is None or subtotal is None:
        return CheckResult(code="V4.subtotal", name="Subtotal = sum of lines", status="SKIPPED", category="MISSING",
                           severity="INFO", message="Subtotal or line totals not available - check skipped.")
    diff = abs(lines - subtotal)
    ok = diff <= tol
    return CheckResult(code="V4.subtotal", name="Subtotal = sum of lines", status="PASS" if ok else "FAIL",
                       category="OK" if ok else "INCORRECT", severity="INFO" if ok else "REVIEW",
                       expected=s(lines), actual=s(subtotal),
                       message=(f"Sum of line totals {lines} matches the {label}." if ok else
                                f"Sum of line totals is {lines} but the {label} is {subtotal} (difference {q2(diff)})."))


def _tax_base(inv: ExtractedInvoice):
    subtotal = _money(inv, "subtotal") or _lines_sum(inv) or _money(inv, "taxable_amount")
    discount = _money(inv, "discount") or ZERO
    return subtotal, discount


def check_tax(inv: ExtractedInvoice, rules: dict) -> list[CheckResult]:
    t = rules["tolerances"]
    out = []
    stated = _money(inv, "tax_amount")
    comps = inv.tax_components
    # CGST must equal SGST (intra-state GST is split 50/50)
    cg = next((c for c in comps if c.kind == "CGST"), None)
    sg = next((c for c in comps if c.kind in ("SGST", "UTGST")), None)
    if cg and sg and cg.amount and sg.amount:
        diff = abs(D(cg.amount) - D(sg.amount))
        ok = diff <= D(t["tax_abs_header"])
        out.append(CheckResult(code="V3.gst_split", name="CGST = SGST", status="PASS" if ok else "FAIL",
                               category="OK" if ok else "INCORRECT", severity="INFO" if ok else "REVIEW",
                               expected=cg.amount, actual=sg.amount,
                               message="CGST and SGST are equal." if ok else
                               f"CGST {cg.amount} and SGST {sg.amount} should be equal."))
    if any(c.kind == "IGST" for c in comps) and cg:
        out.append(CheckResult(code="V3.gst_type", name="GST type consistency", status="FAIL", category="INCORRECT",
                               severity="REVIEW", message="Both IGST and CGST are charged - only one regime applies."))
    rates = [D(li.tax_rate) for li in inv.line_items]
    totals = [D(li.line_total) for li in inv.line_items]
    if inv.meta.get("tax_inclusive"):
        out.append(CheckResult(code="V3.tax_mode", name="Tax presentation", status="PASS",
                               message=f"Prices include tax ({inv.meta.get('tax_inclusive_evidence')!r}); "
                                       "the tax is contained in the line totals, not added on top."))
        return out + [_check_included_tax(inv, stated, rules)]
    if stated is None:
        out.append(CheckResult(code="V3.tax", name="Tax calculation", status="SKIPPED", category="MISSING",
                               severity="INFO", message="No tax amount printed - tax check skipped."))
        return out
    if not inv.line_items or any(r is None for r in rates) or any(x is None for x in totals):
        # fall back to the printed component rates
        rate_set = {D(c.rate) for c in comps if c.rate}
        subtotal, discount = _tax_base(inv)
        if subtotal is None or not rate_set:
            out.append(CheckResult(code="V3.tax", name="Tax calculation", status="SKIPPED", category="MISSING",
                                   severity="INFO", message="Tax rates per line not printed - tax amount cannot be recomputed."))
            return out
        expected = q2((subtotal - discount) * sum(rate_set, ZERO) / 100)
        n = 1
    else:
        subtotal, discount = _tax_base(inv)
        factor = (subtotal - discount) / subtotal if subtotal and discount else Decimal(1)
        expected = q2(sum((q2(tot * r / 100) for tot, r in zip(totals, rates)), ZERO) * factor)
        n = len(totals)
    allowed = D(t["tax_abs_header"]) + D(t["tax_abs_per_line"]) * n
    diff = abs(expected - stated)
    ok = diff <= allowed
    out.append(CheckResult(code="V3.tax", name="Tax calculation", status="PASS" if ok else "FAIL",
                           category="OK" if ok else "INCORRECT", severity="INFO" if ok else "REVIEW",
                           expected=s(expected), actual=s(stated),
                           message=(f"Tax {stated} agrees with the recomputed {expected} (rounding allowance {allowed})."
                                    if ok else f"Tax stated as {stated} but line rates give {expected} "
                                               f"(difference {q2(diff)}, rounding allowance {allowed})."),
                           details={"allowed": s(allowed)}))
    return out


def _check_included_tax(inv: ExtractedInvoice, stated: Decimal | None, rules: dict) -> CheckResult:
    expected = included_tax(inv)
    if expected is None:
        return CheckResult(code="V3.tax", name="Tax calculation", status="SKIPPED", category="MISSING", severity="INFO",
                           message="Tax-inclusive invoice without a tax rate per line - included tax cannot be recomputed.")
    if stated is None:
        return CheckResult(code="V3.tax", name="Tax calculation", status="PASS", expected=s(expected),
                           message=f"No tax amount printed; the line totals contain {expected} tax at the printed rates.")
    t = rules["tolerances"]
    allowed = D(t["tax_abs_header"]) + D(t["tax_abs_per_line"]) * len(inv.line_items)
    diff = abs(expected - stated)
    ok = diff <= allowed
    return CheckResult(code="V3.tax", name="Tax calculation", status="PASS" if ok else "FAIL",
                       category="OK" if ok else "INCORRECT", severity="INFO" if ok else "REVIEW",
                       expected=s(expected), actual=s(stated), details={"allowed": s(allowed), "tax_inclusive": True},
                       message=(f"Included tax {stated} agrees with gross - gross/(1+rate) = {expected}." if ok else
                                f"Included tax stated as {stated} but the gross line totals contain {expected} "
                                f"(difference {q2(diff)}, rounding allowance {allowed})."))


def check_grand_total(inv: ExtractedInvoice, rules: dict) -> CheckResult:
    tol = D(rules["tolerances"]["grand_total_abs"])
    grand = _money(inv, "grand_total")
    subtotal, discount = _tax_base(inv)
    inclusive = bool(inv.meta.get("tax_inclusive"))
    # with tax-inclusive prices the tax is already inside the subtotal - it is not added again
    tax = ZERO if inclusive else (_money(inv, "tax_amount") or ZERO)
    shipping = _money(inv, "shipping") or ZERO
    if grand is None or subtotal is None:
        return CheckResult(code="V5.grand_total", name="Grand total arithmetic", status="SKIPPED", category="MISSING",
                           severity="INFO", message="Grand total or subtotal missing - check skipped.")
    expected = q2(subtotal - discount + tax + shipping)
    diff = abs(expected - grand)
    ok = diff <= tol
    formula = (f"{subtotal} (tax included) - {discount} discount + {shipping} shipping = {expected}" if inclusive else
               f"{subtotal} - {discount} discount + {tax} tax + {shipping} shipping = {expected}")
    return CheckResult(code="V5.grand_total", name="Grand total arithmetic", status="PASS" if ok else "FAIL",
                       category="OK" if ok else "INCORRECT", severity="INFO" if ok else "REVIEW",
                       expected=s(expected), actual=s(grand),
                       message=(f"Grand total {grand} = {formula}." if ok else
                                f"Grand total is {grand} but {formula} (difference {q2(diff)})."))


def check_dates(inv: ExtractedInvoice, rules: dict, today: date) -> list[CheckResult]:
    r = rules["dates"]
    out = []
    f = inv.field("invoice_date")
    if f.value is None:
        if f.raw:
            out.append(CheckResult(code="V6.invoice_date", name="Invoice date valid", status="FAIL",
                                   category="INCORRECT", severity="REVIEW",
                                   message=f"Invoice date {f.raw!r} could not be interpreted as a valid date."))
        return out
    d = date.fromisoformat(f.value)
    if d > today + timedelta(days=int(r["max_future_days"])):
        out.append(CheckResult(code="V6.future_date", name="Invoice date not in future", status="FAIL",
                               category="INCORRECT", severity="REVIEW", actual=f.value, expected=f"<= {today}",
                               message=f"Invoice is dated {d}, which is in the future (today is {today})."))
    elif d < today - timedelta(days=int(r["max_age_days"])):
        out.append(CheckResult(code="V6.stale_date", name="Invoice date not too old", status="FAIL",
                               category="INCORRECT", severity="REVIEW", actual=f.value,
                               message=f"Invoice is dated {d}, more than {r['max_age_days']} days ago."))
    else:
        out.append(CheckResult(code="V6.invoice_date", name="Invoice date valid", status="PASS", actual=f.value,
                               message=f"Invoice date {d} is plausible."))
    due = inv.field("due_date")
    if due.value:
        dd = date.fromisoformat(due.value)
        ok = dd >= d
        out.append(CheckResult(code="V6.due_date", name="Due date after invoice date", status="PASS" if ok else "FAIL",
                               category="OK" if ok else "INCORRECT", severity="INFO" if ok else "REVIEW",
                               message=f"Due date {dd} is {'on/after' if ok else 'BEFORE'} the invoice date {d}."))
    return out


def check_vendor(inv: ExtractedInvoice, vendor_res: dict, rules: dict) -> list[CheckResult]:
    out = []
    tax = inv.field("vendor_tax_id").value
    if tax:
        kind = classify_tax_id(tax)
        valid = gstin_is_valid(tax) if kind == "GSTIN" else kind in ("EU_VAT", "US_EIN")
        out.append(CheckResult(code="V7.tax_id_format", name="Vendor tax ID format", status="PASS" if valid else "WARN",
                               category="OK" if valid else "INCORRECT", severity="INFO",
                               message=(f"{kind} {tax} is well-formed" + (" and its checksum validates." if kind == "GSTIN" else ".")
                                        if valid else f"Tax ID {tax} does not match a known GSTIN/VAT/EIN format.")))
    v = vendor_res.get("vendor")
    if v is None:
        out.append(CheckResult(code="V7.vendor_known", name="Vendor exists in master data", status="FAIL",
                               category="INCORRECT" if tax else "MISSING", severity="REVIEW",
                               message=vendor_res.get("message") or "Vendor not found in the vendor master."))
        return out
    out.append(CheckResult(code="V7.vendor_known", name="Vendor exists in master data", status="PASS",
                           message=f"Matched vendor {v['code']} {v['name']} by {vendor_res['method']}."))
    if v["status"] == "APPROVED":
        out.append(CheckResult(code="V7.vendor_approved", name="Vendor approved", status="PASS",
                               message=f"Vendor {v['name']} is APPROVED."))
    else:
        policy = rules["policy"]["unapproved_vendor"]
        out.append(CheckResult(code="V7.vendor_approved", name="Vendor approved", status="FAIL", category="INCORRECT",
                               severity="REJECT" if policy == "REJECT" else "REVIEW",
                               message=f"Vendor {v['name']} has status {v['status']} - not approved for payment.",
                               details={"vendor_status": v["status"]}))
    return out


def check_confidence(inv: ExtractedInvoice, rules: dict) -> list[CheckResult]:
    critical = {"vendor_tax_id", "invoice_number", "invoice_date", "grand_total"}
    out = []
    uncertain_crit = [n for n in critical if inv.field(n).status == "UNCERTAIN"]
    # vendor name only matters when there is no tax ID
    if inv.field("vendor_tax_id").value is None and inv.field("vendor_name").status == "UNCERTAIN":
        uncertain_crit.append("vendor_name")
    other = [n for n, f in inv.fields.items() if f.status == "UNCERTAIN" and n not in uncertain_crit
             and n not in ("vendor_name", "vendor_address")]
    lines = [li.line_no for li in inv.line_items if li.status == "UNCERTAIN"]
    if uncertain_crit:
        details = {n: {"value": inv.field(n).value, "confidence": inv.field(n).confidence,
                       "note": inv.field(n).note} for n in uncertain_crit}
        out.append(CheckResult(code="V8.critical_confidence", name="Critical fields read reliably", status="FAIL",
                               category="UNCERTAIN", severity="REVIEW", details=details,
                               message="Low-confidence reading of: " + ", ".join(sorted(uncertain_crit)) + "."))
    else:
        out.append(CheckResult(code="V8.critical_confidence", name="Critical fields read reliably", status="PASS",
                               message="All critical fields were read with sufficient confidence."))
    if lines:
        out.append(CheckResult(code="V8.line_confidence", name="Line items read reliably", status="FAIL",
                               category="UNCERTAIN", severity="REVIEW",
                               message=f"Line item(s) {', '.join(map(str, lines))} could not be read reliably."))
    if other:
        out.append(CheckResult(code="V8.other_confidence", name="Other fields", status="WARN", category="UNCERTAIN",
                               severity="INFO", message="Uncertain non-critical field(s): " + ", ".join(sorted(other)) + "."))
    meta = inv.meta
    if meta.get("is_scanned") and meta.get("ocr_mean_conf") is not None and meta["ocr_mean_conf"] < 0.6:
        out.append(CheckResult(code="V8.ocr_quality", name="Scan quality", status="FAIL", category="UNCERTAIN",
                               severity="REVIEW",
                               message=f"Scan quality is poor (mean OCR confidence {meta['ocr_mean_conf']:.0%})."))
    return out


def check_currency(inv: ExtractedInvoice, rules: dict | None = None) -> CheckResult:
    f = inv.field("currency")
    if f.value is None:
        return CheckResult(code="V9.currency", name="Currency identified", status="WARN", category="MISSING",
                           severity="INFO", message="No currency printed; the PO/vendor currency will be assumed (flagged).")
    supported = supported_currencies(rules)
    if f.value not in supported:
        return CheckResult(code="V9.currency", name="Currency identified", status="FAIL", category="INCORRECT",
                           severity="REVIEW",
                           message=f"Currency {f.value} is not set up (set up: {', '.join(sorted(supported))}). "
                                   "A manager can add it with an approval limit on the Rules & data page.")
    return CheckResult(code="V9.currency", name="Currency identified", status="PASS", message=f"Currency {f.value}.")


def run_validation(inv: ExtractedInvoice, vendor_res: dict, rules: dict, today: date) -> list[CheckResult]:
    results: list[CheckResult] = []
    results += check_mandatory(inv)
    results += check_line_math(inv, rules)
    results += check_tax(inv, rules)
    results.append(check_subtotal(inv, rules))
    results.append(check_grand_total(inv, rules))
    results += check_dates(inv, rules, today)
    results += check_vendor(inv, vendor_res, rules)
    results += check_confidence(inv, rules)
    results.append(check_currency(inv, rules))
    return results
