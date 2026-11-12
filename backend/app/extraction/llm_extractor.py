"""AI extractor (OpenAI). Every value must come with a verbatim source quote, which is
verified against the document's own text before the value is trusted."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..llm import LLMClient, pdf_block

_NULLABLE = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_FIELD = {"type": "object", "properties": {"value": _NULLABLE, "source_quote": _NULLABLE},
          "required": ["value", "source_quote"], "additionalProperties": False}
_HEADER = ["vendor_name", "vendor_tax_id", "vendor_address", "invoice_number", "invoice_date", "due_date",
           "po_number", "currency", "subtotal", "taxable_amount", "tax_amount", "discount", "shipping", "grand_total",
           "document_type", "original_invoice_number", "bank_account", "bank_ifsc", "irn"]
_LINE = {"type": "object", "properties": {k: _NULLABLE for k in
                                          ["description", "item_code", "quantity", "uom", "unit_price", "tax_rate",
                                           "line_total", "source_quote"]},
         "required": ["description", "item_code", "quantity", "uom", "unit_price", "tax_rate", "line_total",
                      "source_quote"], "additionalProperties": False}
_TAX = {"type": "object", "properties": {"kind": {"type": "string"}, "rate": _NULLABLE, "amount": _NULLABLE},
        "required": ["kind", "rate", "amount"], "additionalProperties": False}

SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {"type": "object", "properties": {k: _FIELD for k in _HEADER}, "required": _HEADER,
                   "additionalProperties": False},
        "line_items": {"type": "array", "items": _LINE},
        "tax_components": {"type": "array", "items": _TAX},
        "date_format_hint": {"type": "string", "enum": ["DMY", "MDY", "YMD", "UNKNOWN"]},
        "notes": {"type": "string"},
    },
    "required": ["fields", "line_items", "tax_components", "date_format_hint", "notes"],
    "additionalProperties": False,
}

SYSTEM = """You extract data from vendor invoices for an accounts-payable team. Accuracy matters more than completeness.

Rules:
- Extract only what is printed on the document. If a value is not printed, return null for both value and source_quote. Never infer, calculate or guess a missing value.
- The vendor is the SELLER who issued the invoice. The buyer is {company}; never return the buyer's name or tax ID as the vendor's.
- vendor_tax_id: the seller's GSTIN, EU VAT ID or US EIN exactly as printed (no spaces).
- Numbers: plain decimals with a dot as decimal separator and no thousands separators ("500000.00", "2450.00"). Percentages without the % sign ("18").
- Dates: ISO YYYY-MM-DD. Set date_format_hint to how the document writes dates.
- currency: ISO code INR, EUR or USD (or another ISO code if clearly printed).
- discount and shipping: positive numbers if printed, else null.
- tax_amount: the total tax only if a total-tax figure is printed; list each printed tax line (CGST, SGST, IGST, VAT, SALES_TAX) in tax_components.
- Line items: one entry per printed row, in order. If a row has no quantity or unit price, leave those null.
- document_type: INVOICE, CREDIT_NOTE or DEBIT_NOTE, from the document's title. For a credit or debit note, invoice_number is the NOTE's own number and original_invoice_number is the invoice it refers to ("against invoice no."); null for an ordinary invoice.
- Amounts on a credit note: return them as positive numbers.
- bank_account: the vendor's bank account number or IBAN printed for payment (no spaces); bank_ifsc: the IFSC, SWIFT/BIC or routing code printed next to it.
- irn: the 64-character e-invoice IRN (Invoice Reference Number) if printed, else null.
- source_quote: copy a short verbatim snippet (max ~80 characters) from the document that contains the value, so it can be verified. For a line item, quote the row's description text.
- notes: one or two sentences on anything unusual (e.g. a field that is unreadable, handwritten corrections). Empty string if nothing."""


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def quote_verified(quote: str | None, doc_text_norm: str, threshold: int = 85) -> bool:
    if not quote:
        return False
    q = _norm(quote)
    if not q:
        return False
    if q in doc_text_norm:
        return True
    return fuzz.partial_ratio(q, doc_text_norm) >= threshold


def extract_with_llm(llm: LLMClient, pdf_bytes: bytes, doc_text: str, company_name: str) -> dict:
    """Returns the raw LLM JSON plus a `verified` flag per field/line."""
    data = llm.json_call(
        purpose="extraction",
        system=SYSTEM.format(company=company_name),
        user_content=[pdf_block(pdf_bytes), {"type": "text", "text": "Extract this invoice."}],
        schema=SCHEMA, effort="medium",
    )
    text_norm = _norm(doc_text)
    for name, f in data["fields"].items():
        f["verified"] = bool(f.get("value")) and quote_verified(f.get("source_quote"), text_norm)
    for li in data["line_items"]:
        li["verified"] = quote_verified(li.get("source_quote") or li.get("description"), text_norm, 80)
    data["text_layer_available"] = bool(text_norm)
    return data
