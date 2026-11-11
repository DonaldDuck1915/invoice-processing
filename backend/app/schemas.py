"""Data-transfer objects shared between pipeline stages.

Extraction produces an ExtractedInvoice. Validation, matching, duplicate detection and
the decision engine consume it — they never touch the PDF.
All money/quantity values are strings holding exact decimals.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

FieldStatus = Literal["EXTRACTED", "MISSING", "UNCERTAIN", "CORRECTED", "DERIVED"]

HEADER_FIELDS = [
    "vendor_name", "vendor_tax_id", "vendor_address", "invoice_number", "invoice_date", "due_date",
    "po_number", "currency", "subtotal", "taxable_amount", "tax_amount", "discount", "shipping", "grand_total",
]
MONEY_FIELDS = {"subtotal", "taxable_amount", "tax_amount", "discount", "shipping", "grand_total"}
DATE_FIELDS = {"invoice_date", "due_date"}


class FieldValue(BaseModel):
    value: str | None = None           # normalised value (ISO date, decimal string, upper-case id)
    raw: str | None = None             # exactly what was read from the document
    confidence: float = 0.0
    status: FieldStatus = "MISSING"
    source: str | None = None          # text_layer | ocr | llm | llm+rules | derived | human
    page: int | None = None
    note: str | None = None


class LineItem(BaseModel):
    line_no: int
    description: str | None = None
    item_code: str | None = None
    quantity: str | None = None
    uom: str | None = None
    unit_price: str | None = None
    tax_rate: str | None = None
    line_total: str | None = None
    confidence: float = 0.0
    status: FieldStatus = "EXTRACTED"
    source: str | None = None
    note: str | None = None


class TaxComponent(BaseModel):
    kind: str                          # CGST | SGST | IGST | VAT | SALES_TAX | TAX
    rate: str | None = None
    amount: str | None = None


class ExtractedInvoice(BaseModel):
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    line_items: list[LineItem] = Field(default_factory=list)
    tax_components: list[TaxComponent] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def get(self, name: str) -> str | None:
        f = self.fields.get(name)
        return f.value if f else None

    def field(self, name: str) -> FieldValue:
        return self.fields.get(name) or FieldValue()


class CheckResult(BaseModel):
    code: str
    name: str
    status: Literal["PASS", "FAIL", "WARN", "SKIPPED"]
    category: Literal["MISSING", "INCORRECT", "UNCERTAIN", "OK", "INFO"] = "OK"
    severity: Literal["INFO", "REVIEW", "REJECT"] = "INFO"
    message: str
    expected: str | None = None
    actual: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Variance(BaseModel):
    scope: Literal["LINE", "HEADER"]
    field: str
    label: str
    invoice_line_no: int | None = None
    po_line_no: int | None = None
    expected: str | None = None
    actual: str | None = None
    abs_diff: str | None = None
    pct_diff: str | None = None
    tolerance: str | None = None
    tolerance_kind: str | None = None  # pct | abs | exact
    exceeded: bool = False
    direction: Literal["OVER", "UNDER", "EQUAL", "NA"] = "NA"
    note: str | None = None


class RuleHit(BaseModel):
    rule: str
    outcome: Literal["REJECT", "MANUAL_REVIEW", "INFO"]
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    depends_on_unreliable_data: bool = False


class Decision(BaseModel):
    decision: Literal["APPROVED", "REJECTED", "MANUAL_REVIEW"]
    summary: str
    reasons: list[RuleHit]
    passed_checks: list[str]
    failed_checks: list[str]
    next_action: str
    flags: list[str] = Field(default_factory=list)
