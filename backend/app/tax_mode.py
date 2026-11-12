"""Tax-inclusive ("tax embedded") invoices.

Some vendors print gross prices: "Prices are inclusive of GST", with the tax only shown as
"of which GST 18%: 2,250.00". Purchase orders are always priced net of tax, so such an invoice
must not be compared gross-to-net (that would look like an 18% price variance).

- detect_tax_inclusive(): finds the statement on the document (evidence is kept for the audit trail).
- included_tax(): the tax contained in the gross line totals, for the validation checks.
- net_view(): a copy of the invoice with line prices/totals converted to net, used for PO matching
  and therefore for the PO ledger. The printed values stay untouched everywhere else.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .money import D, ZERO, q2, s
from .schemas import ExtractedInvoice, FieldValue

RX_TAX_INCLUSIVE = re.compile(
    r"\b(?:"
    r"(?:prices?|amounts?|rates?|totals?)\s+(?:are\s+|is\s+)?inclusive\s+of\s+(?:all\s+)?(?:taxes|tax|gst|vat)"
    r"|inclusive\s+of\s+(?:all\s+)?(?:taxes|tax|gst|vat)"
    r"|incl\.?\s+(?:of\s+)?(?:all\s+)?(?:taxes|tax|gst|vat)"
    r"|(?:gst|vat|taxes|tax)\s+(?:is\s+|are\s+)?included"
    r"|includes?\s+(?:gst|igst|vat|tax)"
    r")\b", re.I)

UNIT_PRICE_Q = Decimal("0.0001")


def detect_tax_inclusive(text: str) -> str | None:
    """Returns the matching phrase (the evidence) or None."""
    m = RX_TAX_INCLUSIVE.search(text or "")
    return m.group(0) if m else None


def _header_rate(inv: ExtractedInvoice) -> Decimal | None:
    """One overall rate from the printed tax components (CGST 9 + SGST 9 = 18, or IGST 18)."""
    rates = [D(c.rate) for c in inv.tax_components if c.rate]
    if not rates or any(r is None for r in rates):
        return None
    kinds = {c.kind for c in inv.tax_components}
    if kinds & {"CGST", "SGST", "UTGST"}:
        return sum(rates, ZERO)
    return rates[0] if len(set(rates)) == 1 else None


def line_rate(inv: ExtractedInvoice, li) -> tuple[Decimal | None, str]:
    r = D(li.tax_rate)
    if r is not None:
        return r, "line tax rate"
    r = _header_rate(inv)
    return (r, "printed tax rate") if r is not None else (None, "no tax rate printed")


def included_tax(inv: ExtractedInvoice) -> Decimal | None:
    """Tax contained in gross line totals: gross - gross / (1 + rate)."""
    if not inv.line_items:
        return None
    total = ZERO
    for li in inv.line_items:
        gross = D(li.line_total)
        rate, _ = line_rate(inv, li)
        if gross is None or rate is None:
            return None
        total += gross - q2(gross / (1 + rate / 100))
    return q2(total)


def net_view(inv: ExtractedInvoice) -> ExtractedInvoice:
    """Same invoice with net (ex-tax) line prices and totals. Returns `inv` itself when prices are net."""
    if not inv.meta.get("tax_inclusive"):
        return inv
    out = inv.model_copy(deep=True)
    converted, net_sum, complete = [], ZERO, True
    for li in out.line_items:
        rate, source = line_rate(inv, li)
        gross_total, gross_price = D(li.line_total), D(li.unit_price)
        if rate is None or gross_total is None:
            complete = False
            converted.append({"line_no": li.line_no, "converted": False, "reason": source})
            continue
        factor = 1 + rate / 100
        net_total = q2(gross_total / factor)
        li.line_total = s(net_total)
        if gross_price is not None:
            li.unit_price = s((gross_price / factor).quantize(UNIT_PRICE_Q).normalize())
        li.note = ((li.note + " ") if li.note else "") + f"Net of {s(rate)}% tax (printed gross {s(gross_total)})."
        net_sum += net_total
        converted.append({"line_no": li.line_no, "converted": True, "rate": s(rate), "rate_source": source,
                          "gross_total": s(gross_total), "net_total": s(net_total)})
    if complete and out.line_items:
        for name in ("subtotal", "taxable_amount"):
            out.fields[name] = FieldValue(value=s(net_sum), confidence=0.95, status="DERIVED", source="derived",
                                          note="Net of tax, derived from the tax-inclusive line totals.")
    out.meta["net_conversion"] = {"complete": complete, "net_subtotal": s(net_sum) if complete else None,
                                  "lines": converted}
    return out
