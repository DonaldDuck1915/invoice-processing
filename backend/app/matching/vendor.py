"""Vendor resolution: tax ID exact -> tax ID OCR-tolerant -> fuzzy name (weak)."""
from __future__ import annotations

from rapidfuzz import fuzz

from ..extraction.parsers import normalize_tax_id, ocr_normalize
from ..schemas import ExtractedInvoice


def _name_sim(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a.lower(), b.lower()) / 100


def resolve_vendor(inv: ExtractedInvoice, vendors: list[dict]) -> dict:
    tax = normalize_tax_id(inv.get("vendor_tax_id"))
    name = inv.get("vendor_name")
    res = {"vendor": None, "method": None, "confidence": 0.0, "weak": False, "conflict": None, "message": None,
           "name_candidates": []}
    if tax:
        exact = [v for v in vendors if v.get("tax_id") and normalize_tax_id(v["tax_id"]) == tax]
        if not exact:
            exact = [v for v in vendors if v.get("tax_id") and ocr_normalize(v["tax_id"]) == ocr_normalize(tax)]
            method = "tax ID (OCR-tolerant match)"
        else:
            method = "tax ID (exact)"
        if len(exact) == 1:
            v = exact[0]
            res.update(vendor=v, method=method, confidence=1.0 if "exact" in method else 0.9)
            # identity conflict: tax ID says vendor A, printed name clearly another master-data vendor
            if name and _name_sim(name, v["name"]) < 0.6:
                other = max(vendors, key=lambda o: _name_sim(name, o["name"]))
                if other["code"] != v["code"] and _name_sim(name, other["name"]) >= 0.92:
                    res["conflict"] = (f"Tax ID {tax} belongs to {v['name']} but the invoice is issued in the name "
                                       f"of {other['name']}.")
            return res
    if name:
        scored = sorted(((round(_name_sim(name, v["name"]), 3), v) for v in vendors), key=lambda x: -x[0])
        res["name_candidates"] = [{"code": v["code"], "name": v["name"], "similarity": sc} for sc, v in scored[:3]]
        if scored and scored[0][0] >= 0.9 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
            res.update(vendor=scored[0][1], method="name (fuzzy - no tax ID match)", confidence=scored[0][0],
                       weak=True)
            if tax:
                res["message"] = (f"Tax ID {tax} is not on file; vendor matched by name only "
                                  f"({scored[0][1]['name']}).")
            return res
    res["message"] = (f"No vendor in master data has tax ID {tax}." if tax
                      else "The vendor could not be identified (no tax ID, no matching name).")
    return res
