"""PO identification, line alignment and variance calculation.

POs are passed in as plain dicts (see pipeline.po_snapshot) including ledger figures
(consumed / remaining per line, excluding the invoice being processed) - no DB access here.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..extraction.parsers import levenshtein, normalize_ref, ocr_normalize
from ..money import D, ZERO, pct_diff, q2, s
from ..schemas import ExtractedInvoice, Variance
from .semantic import SemanticScorer, lexical

try:
    from scipy.optimize import linear_sum_assignment
except Exception:  # noqa: BLE001
    linear_sum_assignment = None


# ----------------------------------------------------------------------------- helpers

def _inv_lines(inv: ExtractedInvoice) -> list[dict]:
    return [{"line_no": li.line_no, "description": li.description or "", "item_code": li.item_code,
             "quantity": D(li.quantity), "unit_price": D(li.unit_price), "tax_rate": D(li.tax_rate),
             "line_total": D(li.line_total)} for li in inv.line_items]


def _inv_subtotal(inv: ExtractedInvoice) -> Decimal | None:
    lines = [D(li.line_total) for li in inv.line_items]
    if lines and all(x is not None for x in lines):
        return sum(lines, ZERO)
    return D(inv.get("subtotal")) or D(inv.get("taxable_amount"))


def _code_eq(a: str | None, b: str | None) -> bool:
    return bool(a and b and normalize_ref(a) == normalize_ref(b))


def line_similarity(inv_line: dict, po_line: dict, po_number: str, sem: dict) -> dict:
    lex = lexical(inv_line["description"], po_line["description"])
    code = _code_eq(inv_line.get("item_code"), po_line.get("item_code"))
    sem_score = sem["pairs"].get(f"{inv_line['line_no']}|{po_number}|{po_line['line_no']}") if sem.get("available") else None
    if code:
        combined = 1.0
    elif sem_score is not None:
        combined = round(0.6 * sem_score + 0.4 * lex, 3)
    else:
        combined = lex
    return {"lexical": lex, "semantic": sem_score, "code_match": code, "combined": combined}


def align(inv_lines: list[dict], po: dict, sem: dict, min_sim: float) -> tuple[list[dict], list[int]]:
    """Optimal one-to-one assignment of invoice lines to PO lines. Returns (pairs, unmatched_invoice_line_nos)."""
    po_lines = po["lines"]
    if not inv_lines or not po_lines:
        return [], [li["line_no"] for li in inv_lines]
    sims = [[line_similarity(il, pl, po["po_number"], sem) for pl in po_lines] for il in inv_lines]
    pairs_idx: list[tuple[int, int]] = []
    if linear_sum_assignment is not None:
        import numpy as np

        cost = np.array([[-x["combined"] for x in row] for row in sims])
        r, c = linear_sum_assignment(cost)
        pairs_idx = list(zip(r.tolist(), c.tolist()))
    else:  # greedy fallback
        used = set()
        for i, row in enumerate(sims):
            j = max((j for j in range(len(row)) if j not in used), key=lambda j: row[j]["combined"], default=None)
            if j is not None:
                used.add(j)
                pairs_idx.append((i, j))
    pairs, matched = [], set()
    for i, j in pairs_idx:
        if sims[i][j]["combined"] >= min_sim:
            pairs.append({"invoice_line": inv_lines[i], "po_line": po_lines[j], "similarity": sims[i][j]})
            matched.add(i)
    unmatched = [inv_lines[i]["line_no"] for i in range(len(inv_lines)) if i not in matched]
    return pairs, unmatched


# ----------------------------------------------------------------------------- candidate scoring

def score_candidate(inv: ExtractedInvoice, po: dict, sem: dict, rules: dict) -> dict:
    m = rules["matching"]
    w = m["weights"]
    inv_lines = _inv_lines(inv)
    pairs, unmatched = align(inv_lines, po, sem, float(m["line_min_similarity"]))
    signals: dict[str, dict] = {}

    if sem.get("available") and po["po_number"] in sem["po"]:
        p = sem["po"][po["po_number"]]
        signals["semantic"] = {"value": p["score"], "evidence": p["reason"]}
    else:
        signals["semantic"] = {"value": None, "evidence": sem.get("error") or "AI meaning signal unavailable"}

    if inv_lines:
        best = [max((lexical(il["description"], pl["description"]) for pl in po["lines"]), default=0) for il in inv_lines]
        val = round(sum(best) / len(best), 3)
        signals["lexical"] = {"value": val, "evidence": f"Average best wording similarity across {len(best)} line(s): {val:.0%}"}
    else:
        signals["lexical"] = {"value": None, "evidence": "No invoice lines"}

    inv_codes = [normalize_ref(li["item_code"]) for li in inv_lines if li.get("item_code")]
    po_codes = {normalize_ref(pl["item_code"]) for pl in po["lines"] if pl.get("item_code")}
    if inv_codes and po_codes:
        hit = [c for c in inv_codes if c in po_codes]
        signals["item_code"] = {"value": round(len(hit) / len(inv_codes), 3),
                                "evidence": f"{len(hit)}/{len(inv_codes)} invoice item codes found on this PO"}
    else:
        signals["item_code"] = {"value": None, "evidence": "No item codes on the invoice to compare"}

    if inv_lines:
        tol = float(_tol(po, rules, "price"))
        fits = []
        for pr in pairs:
            il, pl = pr["invoice_line"], pr["po_line"]
            price_fit = qty_fit = 0.0
            if il["unit_price"] is not None and pl["unit_price"]:
                dev = abs(float((il["unit_price"] - pl["unit_price"]) / pl["unit_price"] * 100))
                price_fit = max(0.0, 1 - dev / (2 * tol if tol else 10))
            rem = pl["remaining_qty"]
            if il["quantity"] is not None and rem is not None:
                qty_fit = 1.0 if il["quantity"] <= rem else max(0.0, float(1 - (il["quantity"] - rem) / max(rem, Decimal(1))))
            fits.append((price_fit + qty_fit) / 2)
        fits += [0.0] * len(unmatched)
        val = round(sum(fits) / len(fits), 3) if fits else 0.0
        signals["qty_price"] = {"value": val, "evidence": f"{len(pairs)} line(s) aligned; quantity/price fit {val:.0%}"
                                + (f"; {len(unmatched)} line(s) with no counterpart" if unmatched else "")}
    else:
        signals["qty_price"] = {"value": None, "evidence": "No invoice lines"}

    inv_amt = _inv_subtotal(inv)
    remaining = po["remaining_amount"]
    if inv_amt is not None and remaining is not None:
        val = 0.0 if remaining <= 0 else round(max(0.0, 1 - float(abs(inv_amt - remaining) / remaining)), 3)
        signals["amount"] = {"value": val, "evidence": f"Invoice net {s(q2(inv_amt))} vs PO remaining {s(q2(remaining))}"}
    else:
        signals["amount"] = {"value": None, "evidence": "Amount not available"}

    inv_date = inv.get("invoice_date")
    if inv_date:
        d, pd = date.fromisoformat(inv_date), date.fromisoformat(po["po_date"])
        days = (d - pd).days
        val = 0.0 if days < 0 else (1.0 if days <= int(rules["dates"]["max_days_after_po"]) else 0.3)
        signals["date"] = {"value": val, "evidence": f"Invoice {days} day(s) after PO date {pd}" if days >= 0
                           else f"Invoice dated {-days} day(s) BEFORE the PO was raised"}
    else:
        signals["date"] = {"value": None, "evidence": "No invoice date"}

    earned = avail = 0.0
    for k, sig in signals.items():
        sig["weight"] = w[k]
        if sig["value"] is not None:
            sig["points"] = round(sig["value"] * w[k], 2)
            earned += sig["points"]
            avail += w[k]
        else:
            sig["points"] = None
    score = round(earned / avail * 100, 1) if avail else 0.0
    desc_keys = [k for k in ("semantic", "lexical", "item_code") if signals[k]["value"] is not None]
    desc_avail = sum(w[k] for k in desc_keys)
    desc_evidence = (sum(signals[k]["points"] for k in desc_keys) / desc_avail) if desc_avail else 0.0
    return {"po_number": po["po_number"], "po_id": po["id"], "status": po["status"], "description": po["description"],
            "po_date": po["po_date"], "subtotal": s(po["subtotal"]), "remaining_amount": s(remaining),
            "score": score, "signals": signals, "description_evidence": round(desc_evidence, 3),
            "eligible": True, "lines_aligned": len(pairs), "lines_unmatched": unmatched}


def _tol(po: dict, rules: dict, kind: str) -> Decimal:
    t = rules["tolerances"]
    key = {"qty": ("qty_tolerance_pct", "quantity_pct"), "price": ("price_tolerance_pct", "unit_price_pct"),
           "amount": ("amount_tolerance_pct", "total_amount_pct")}[kind]
    v = po.get(key[0])
    return D(v) if v is not None else D(t[key[1]])


def _eligibility(po: dict) -> tuple[bool, str | None]:
    if po["status"] == "CANCELLED":
        return False, "PO is cancelled"
    if po["status"] == "CLOSED" and (po["remaining_amount"] or ZERO) <= 0:
        return False, "PO is closed and fully consumed"
    if (po["remaining_amount"] or ZERO) <= 0:
        return False, "PO has no remaining balance"
    return True, None


# ----------------------------------------------------------------------------- variances

def compute_variances(inv: ExtractedInvoice, po: dict, pairs: list[dict], unmatched: list[int],
                      rules: dict) -> tuple[list[Variance], list[dict]]:
    pol = rules["policy"]
    t = rules["tolerances"]
    qty_tol, price_tol, amt_tol = _tol(po, rules, "qty"), _tol(po, rules, "price"), _tol(po, rules, "amount")
    under_ok = bool(pol["allow_under_billing"])
    variances: list[Variance] = []
    issues: list[dict] = []
    expected_net = ZERO
    expected_tax = ZERO

    for pr in pairs:
        il, pl = pr["invoice_line"], pr["po_line"]
        ln, pn = il["line_no"], pl["line_no"]
        label = f"Line {ln} - {il['description'][:40]}"
        # quantity vs remaining
        if il["quantity"] is not None:
            rem = pl["remaining_qty"]
            limit = rem * (1 + qty_tol / 100)
            exceeded = il["quantity"] > limit
            variances.append(Variance(
                scope="LINE", field="quantity", label=label, invoice_line_no=ln, po_line_no=pn, expected=s(rem),
                actual=s(il["quantity"]), abs_diff=s(il["quantity"] - rem), pct_diff=s(pct_diff(rem, il["quantity"])),
                tolerance=s(qty_tol), tolerance_kind="pct", exceeded=exceeded,
                direction="OVER" if il["quantity"] > rem else "UNDER" if il["quantity"] < rem else "EQUAL",
                note=f"PO qty {s(pl['quantity'])}, previously invoiced {s(pl['consumed_qty'])}, remaining {s(rem)}."))
            if exceeded:
                issues.append({"code": "QTY_EXCEEDS_REMAINING", "line": ln, "po_line": pn,
                               "message": (f"Line {ln} ({il['description'][:40]}): invoiced quantity {s(il['quantity'])} "
                                           f"exceeds remaining PO quantity {s(rem)} (PO {s(pl['quantity'])}, "
                                           f"already invoiced {s(pl['consumed_qty'])}). Cumulative would be "
                                           f"{s(pl['consumed_qty'] + il['quantity'])}."),
                               "evidence": {"po_qty": s(pl["quantity"]), "previously_invoiced": s(pl["consumed_qty"]),
                                            "this_invoice": s(il["quantity"]), "remaining": s(rem)}})
        # unit price
        if il["unit_price"] is not None:
            pct = pct_diff(pl["unit_price"], il["unit_price"])
            over = il["unit_price"] > pl["unit_price"]
            exceeded = pct is not None and (abs(pct) > price_tol if not under_ok else (over and pct > price_tol))
            variances.append(Variance(
                scope="LINE", field="unit_price", label=label, invoice_line_no=ln, po_line_no=pn,
                expected=s(pl["unit_price"]), actual=s(il["unit_price"]), abs_diff=s(il["unit_price"] - pl["unit_price"]),
                pct_diff=s(pct), tolerance=s(price_tol), tolerance_kind="pct", exceeded=exceeded,
                direction="OVER" if over else "UNDER" if il["unit_price"] < pl["unit_price"] else "EQUAL"))
            if exceeded:
                issues.append({"code": "PRICE_OVER_TOLERANCE", "line": ln, "po_line": pn,
                               "message": (f"Line {ln} ({il['description'][:40]}{', ' + pl['item_code'] if pl.get('item_code') else ''}): "
                                           f"unit price {s(il['unit_price'])} vs PO {s(pl['unit_price'])} = "
                                           f"{s(pct)}% variance, above the {s(price_tol)}% tolerance."),
                               "evidence": {"expected": s(pl["unit_price"]), "actual": s(il["unit_price"]),
                                            "pct": s(pct), "tolerance_pct": s(price_tol)}})
        # tax rate
        if il["tax_rate"] is not None:
            mismatch = il["tax_rate"] != pl["tax_rate"]
            variances.append(Variance(
                scope="LINE", field="tax_rate", label=label, invoice_line_no=ln, po_line_no=pn,
                expected=s(pl["tax_rate"]), actual=s(il["tax_rate"]), abs_diff=s(il["tax_rate"] - pl["tax_rate"]),
                tolerance="0", tolerance_kind="exact", exceeded=mismatch, direction="NA"))
            if mismatch:
                issues.append({"code": "TAX_RATE_MISMATCH", "line": ln,
                               "message": f"Line {ln}: tax rate {s(il['tax_rate'])}% differs from PO rate {s(pl['tax_rate'])}%."})
        # line amount
        if il["quantity"] is not None and il["line_total"] is not None:
            exp_amt = q2(il["quantity"] * pl["unit_price"])
            pct = pct_diff(exp_amt, il["line_total"])
            exceeded = pct is not None and pct > amt_tol
            variances.append(Variance(
                scope="LINE", field="line_amount", label=label, invoice_line_no=ln, po_line_no=pn, expected=s(exp_amt),
                actual=s(il["line_total"]), abs_diff=s(il["line_total"] - exp_amt), pct_diff=s(pct),
                tolerance=s(amt_tol), tolerance_kind="pct", exceeded=exceeded,
                direction="OVER" if il["line_total"] > exp_amt else "UNDER" if il["line_total"] < exp_amt else "EQUAL",
                note="Expected = invoiced quantity x PO unit price."))
            expected_net += exp_amt
            # tax at the PO's rate on what was actually invoiced (price variance is reported separately)
            expected_tax += q2(il["line_total"] * pl["tax_rate"] / 100)

    for ln in unmatched:
        il = next(x for x in _inv_lines(inv) if x["line_no"] == ln)
        issues.append({"code": "UNMATCHED_INVOICE_LINE", "line": ln,
                       "message": f"Invoice line {ln} ({il['description'][:50]}) has no matching line on {po['po_number']}."})

    # header level
    cur = inv.get("currency")
    if cur and cur != po["currency"]:
        issues.append({"code": "CURRENCY_MISMATCH",
                       "message": f"Invoice currency {cur} differs from PO currency {po['currency']}.",
                       "evidence": {"expected": po["currency"], "actual": cur}})
    variances.append(Variance(scope="HEADER", field="currency", label="Currency", expected=po["currency"],
                              actual=cur or "(not printed)", tolerance_kind="exact",
                              exceeded=bool(cur and cur != po["currency"])))
    inv_net = _inv_subtotal(inv)
    if inv_net is not None and pairs:
        pct = pct_diff(expected_net, inv_net)
        exceeded = pct is not None and pct > amt_tol
        variances.append(Variance(scope="HEADER", field="net_amount", label="Net amount (before tax)",
                                  expected=s(expected_net), actual=s(inv_net), abs_diff=s(inv_net - expected_net),
                                  pct_diff=s(pct), tolerance=s(amt_tol), tolerance_kind="pct", exceeded=exceeded,
                                  direction="OVER" if inv_net > expected_net else "UNDER" if inv_net < expected_net else "EQUAL",
                                  note="Expected = sum of invoiced quantities x PO unit prices."))
        if exceeded:
            issues.append({"code": "TOTAL_OVER_TOLERANCE",
                           "message": f"Net amount {s(inv_net)} is {s(pct)}% above the PO-priced value {s(expected_net)} "
                                      f"(tolerance {s(amt_tol)}%).",
                           "evidence": {"expected": s(expected_net), "actual": s(inv_net), "pct": s(pct)}})
        # cumulative value against PO
        consumed = po["consumed_amount"]
        cumulative = consumed + inv_net
        limit = po["subtotal"] * (1 + amt_tol / 100)
        exceeded = cumulative > limit
        variances.append(Variance(scope="HEADER", field="cumulative_value", label="Cumulative invoiced value vs PO",
                                  expected=s(po["subtotal"]), actual=s(cumulative), abs_diff=s(cumulative - po["subtotal"]),
                                  pct_diff=s(pct_diff(po["subtotal"], cumulative)), tolerance=s(amt_tol),
                                  tolerance_kind="pct", exceeded=exceeded,
                                  direction="OVER" if cumulative > po["subtotal"] else "UNDER" if cumulative < po["subtotal"] else "EQUAL",
                                  note=f"Previously invoiced {s(consumed)} + this invoice {s(inv_net)}."))
        if exceeded:
            issues.append({"code": "CUMULATIVE_VALUE_EXCEEDED",
                           "message": f"Cumulative invoiced value {s(cumulative)} exceeds PO value {s(po['subtotal'])} "
                                      f"by more than {s(amt_tol)}%.",
                           "evidence": {"po_value": s(po["subtotal"]), "previously_invoiced": s(consumed),
                                        "this_invoice": s(inv_net)}})
    tax = D(inv.get("tax_amount"))
    if tax is not None and pairs:
        allowed = D(t["tax_abs_header"]) + D(t["tax_abs_per_line"]) * len(pairs)
        exceeded = abs(tax - expected_tax) > allowed
        variances.append(Variance(scope="HEADER", field="tax", label="Tax at PO rates", expected=s(expected_tax),
                                  actual=s(tax), abs_diff=s(tax - expected_tax), pct_diff=s(pct_diff(expected_tax, tax)),
                                  tolerance=s(allowed), tolerance_kind="abs", exceeded=exceeded,
                                  direction="OVER" if tax > expected_tax else "UNDER" if tax < expected_tax else "EQUAL",
                                  note="PO tax rates applied to the invoiced line amounts; rounding allowance, not a percentage."))
        if exceeded and not any(i["code"] == "TAX_RATE_MISMATCH" for i in issues):
            issues.append({"code": "TAX_VARIANCE", "message": f"Tax {s(tax)} differs from tax at PO rates "
                                                              f"{s(expected_tax)} beyond the rounding allowance {s(allowed)}."})
    shipping = D(inv.get("shipping")) or ZERO
    if shipping > 0:
        variances.append(Variance(scope="HEADER", field="shipping", label="Freight / shipping (not on PO)", expected="0",
                                  actual=s(shipping), abs_diff=s(shipping), tolerance="0", tolerance_kind="exact",
                                  exceeded=True))
        issues.append({"code": "UNPLANNED_CHARGES", "message": f"Freight/shipping of {s(shipping)} is charged but not on the PO."})
    discount = D(inv.get("discount")) or ZERO
    if discount > 0:
        variances.append(Variance(scope="HEADER", field="discount", label="Discount", expected="0", actual=s(discount),
                                  tolerance_kind="exact", exceeded=False, note="Discount in the company's favour - informational."))
    grand = D(inv.get("grand_total"))
    if grand is not None and pairs:
        exp_grand = expected_net - discount + expected_tax
        pct = pct_diff(exp_grand, grand)
        variances.append(Variance(scope="HEADER", field="grand_total", label="Grand total vs PO pricing",
                                  expected=s(q2(exp_grand)), actual=s(grand), abs_diff=s(q2(grand - exp_grand)),
                                  pct_diff=s(pct), tolerance=s(amt_tol), tolerance_kind="pct",
                                  exceeded=pct is not None and pct > amt_tol,
                                  direction="OVER" if grand > exp_grand else "UNDER" if grand < exp_grand else "EQUAL"))
    return variances, issues


# ----------------------------------------------------------------------------- main entry

def match_po(inv: ExtractedInvoice, vendor: dict | None, vendor_pos: list[dict], all_pos: dict[str, dict],
             rules: dict, scorer: SemanticScorer, confirmed_po: dict | None = None) -> dict:
    m = rules["matching"]
    res = {"method": "NONE", "selected_po": None, "selected_po_id": None, "ambiguous": False, "candidates": [],
           "issues": [], "pairs": [], "unmatched_lines": [], "variances": [], "explanation": "",
           "explicit_reference": inv.get("po_number"), "semantic": {"available": False, "error": None}}
    selected: dict | None = None

    if confirmed_po is not None:
        selected, res["method"] = confirmed_po, "HUMAN_CONFIRMED"
        res["explanation"] = f"{confirmed_po['po_number']} was confirmed by a reviewer."
    elif inv.get("po_number"):
        ref = normalize_ref(inv.get("po_number"))
        hit = next((p for n, p in all_pos.items() if normalize_ref(n) == ref), None)
        if hit:
            if vendor and hit["vendor_id"] != vendor["id"]:
                res["method"] = "EXPLICIT"
                res["issues"].append({"code": "PO_VENDOR_MISMATCH",
                                      "message": f"Cited {hit['po_number']} belongs to vendor {hit['vendor_name']}, "
                                                 f"not to {vendor['name']}.",
                                      "evidence": {"po_vendor": hit["vendor_name"], "invoice_vendor": vendor["name"]}})
                res["cited_po"] = {"po_number": hit["po_number"], "vendor": hit["vendor_name"]}
                res["explanation"] = "The PO number on the invoice belongs to a different vendor."
                return res
            selected, res["method"] = hit, "EXPLICIT"
            res["explanation"] = f"PO number {hit['po_number']} printed on the invoice."
        else:
            fuzzy = [p for p in vendor_pos if ocr_normalize(p["po_number"]) == ocr_normalize(ref)
                     or levenshtein(normalize_ref(p["po_number"]), ref) <= int(m["fuzzy_po_max_edits"])]
            if len(fuzzy) == 1:
                selected, res["method"] = fuzzy[0], "EXPLICIT_FUZZY"
                res["issues"].append({"code": "PO_FUZZY_CORRECTED",
                                      "message": f"Invoice cites {inv.get('po_number')}, which does not exist; "
                                                 f"closest vendor PO is {fuzzy[0]['po_number']} (likely a misread)."})
                res["explanation"] = f"PO reference {inv.get('po_number')} read as {fuzzy[0]['po_number']}."
            else:
                res["issues"].append({"code": "PO_NOT_FOUND",
                                      "message": f"PO {inv.get('po_number')} cited on the invoice does not exist."})

    if selected is None:
        # inferred identification among the vendor's POs
        if vendor is None:
            res["explanation"] = "No PO reference and the vendor is unknown - candidate POs cannot be searched."
            res["issues"].append({"code": "PO_NOT_IDENTIFIED", "message": res["explanation"]})
            return res
        if not inv.get("po_number"):
            res["issues"].append({"code": "PO_REFERENCE_MISSING", "message": "The invoice does not cite a PO number."})
        eligible, ineligible = [], []
        for po in vendor_pos:
            ok, why = _eligibility(po)
            (eligible if ok else ineligible).append((po, why))
        sem = scorer.score([{"line_no": li.line_no, "description": li.description or ""} for li in inv.line_items],
                           [{"po_number": p["po_number"], "description": p["description"],
                             "lines": [{"line_no": ln["line_no"], "description": ln["description"]} for ln in p["lines"]]}
                            for p, _ in eligible]) if eligible else {"available": False, "error": "no eligible POs",
                                                                       "po": {}, "pairs": {}}
        res["semantic"] = {"available": sem["available"], "error": sem.get("error")}
        cands = sorted((score_candidate(inv, p, sem, rules) for p, _ in eligible), key=lambda c: -c["score"])
        for p, why in ineligible:
            cands.append({"po_number": p["po_number"], "po_id": p["id"], "status": p["status"],
                          "description": p["description"], "po_date": p["po_date"], "subtotal": s(p["subtotal"]),
                          "remaining_amount": s(p["remaining_amount"]), "score": None, "eligible": False,
                          "ineligible_reason": why, "signals": {}})
        res["candidates"] = cands
        res["method"] = "INFERRED"
        scored = [c for c in cands if c["eligible"]]
        if not scored:
            res["explanation"] = f"The vendor has no open PO with remaining balance ({len(ineligible)} closed/cancelled)."
            res["issues"].append({"code": "PO_NOT_IDENTIFIED", "message": res["explanation"]})
            return res
        top = scored[0]
        second = scored[1] if len(scored) > 1 else None
        margin = top["score"] - (second["score"] if second else 0)
        reasons = []
        if top["score"] < float(m["select_min_score"]):
            reasons.append(f"top score {top['score']} is below the {m['select_min_score']} minimum")
        if second and margin < float(m["select_margin"]):
            reasons.append(f"{top['po_number']} leads {second['po_number']} by only {margin:.1f} points "
                           f"(needs {m['select_margin']})")
        if top["description_evidence"] < float(m["min_description_evidence"]):
            reasons.append(f"description/code evidence for {top['po_number']} is weak "
                           f"({top['description_evidence']:.0%}); amount alone cannot select a PO")
        if not sem["available"] and m.get("require_semantic_for_auto_select", True):
            reasons.append("the AI meaning signal is unavailable, so an inferred match needs human confirmation")
        res["proposed_po"] = top["po_number"]
        res["margin"] = round(margin, 1)
        if reasons:
            res["ambiguous"] = True
            res["explanation"] = (f"No PO selected automatically: " + "; ".join(reasons) +
                                  f". Best candidate: {top['po_number']} ({top['score']}).")
            res["issues"].append({"code": "PO_AMBIGUOUS", "message": res["explanation"],
                                  "evidence": {"candidates": [(c["po_number"], c["score"]) for c in scored[:3]]}})
            return res
        selected = next(p for p, _ in eligible if p["po_number"] == top["po_number"])
        res["explanation"] = (f"{top['po_number']} inferred with score {top['score']}"
                              + (f", {margin:.1f} points ahead of {second['po_number']}" if second else "") + ".")

    # status of the selected PO
    res["selected_po"], res["selected_po_id"] = selected["po_number"], selected["id"]
    if selected["status"] == "CANCELLED":
        res["issues"].append({"code": "PO_CANCELLED", "message": f"{selected['po_number']} is CANCELLED."})
    elif selected["status"] == "CLOSED":
        if (selected["remaining_amount"] or ZERO) <= 0:
            res["issues"].append({"code": "PO_CLOSED_NO_BALANCE",
                                  "message": f"{selected['po_number']} is CLOSED with no remaining quantity."})
        else:
            res["issues"].append({"code": "PO_CLOSED_WITH_BALANCE",
                                  "message": f"{selected['po_number']} is CLOSED but still has "
                                             f"{s(selected['remaining_amount'])} unbilled."})
    sem_for_lines = {"available": False, "pairs": {}, "po": {}}
    pairs, unmatched = align(_inv_lines(inv), selected, sem_for_lines, float(m["line_min_similarity"]))
    if unmatched and scorer.available:
        # one more attempt with the meaning signal before declaring lines unmatched
        sem_for_lines = scorer.score([{"line_no": li.line_no, "description": li.description or ""} for li in inv.line_items],
                                     [{"po_number": selected["po_number"], "description": selected["description"],
                                       "lines": [{"line_no": ln["line_no"], "description": ln["description"]}
                                                 for ln in selected["lines"]]}])
        pairs, unmatched = align(_inv_lines(inv), selected, sem_for_lines, float(m["line_min_similarity"]))
    variances, issues = compute_variances(inv, selected, pairs, unmatched, rules)
    res["issues"] += issues
    res["variances"] = [v.model_dump() for v in variances]
    res["unmatched_lines"] = unmatched
    res["pairs"] = [{"invoice_line_no": p["invoice_line"]["line_no"], "invoice_description": p["invoice_line"]["description"],
                     "po_line_no": p["po_line"]["line_no"], "po_description": p["po_line"]["description"],
                     "po_item_code": p["po_line"].get("item_code"), "similarity": p["similarity"],
                     "invoice": {k: s(p["invoice_line"][k]) if k != "item_code" else p["invoice_line"][k]
                                 for k in ("item_code", "quantity", "unit_price", "tax_rate", "line_total")},
                     "po": {"quantity": s(p["po_line"]["quantity"]), "unit_price": s(p["po_line"]["unit_price"]),
                            "tax_rate": s(p["po_line"]["tax_rate"]), "line_amount": s(p["po_line"]["line_amount"]),
                            "consumed_qty": s(p["po_line"]["consumed_qty"]),
                            "remaining_qty": s(p["po_line"]["remaining_qty"]), "uom": p["po_line"].get("uom")}}
                    for p in pairs]
    return res
