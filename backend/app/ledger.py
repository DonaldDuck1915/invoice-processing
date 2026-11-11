"""PO consumption ledger. Remaining balance is always derived from ACTIVE rows.

- One row per (PO line, invoice) - enforced by a unique constraint, so an invoice can never be counted twice.
- Reprocessing an invoice updates its own rows in place; rows for lines it no longer bills are REVERSED.
- Snapshots exclude the invoice being processed, so a re-run compares against everything *else*.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .money import D, ZERO, q2


def consumption_by_line(db: Session, exclude_invoice_id: int | None = None) -> dict[int, tuple[Decimal, Decimal]]:
    rows = db.execute(select(m.POConsumption.po_line_id, m.POConsumption.quantity, m.POConsumption.amount,
                             m.POConsumption.invoice_id).where(m.POConsumption.status == "ACTIVE")).all()
    out: dict[int, tuple[Decimal, Decimal]] = {}
    for line_id, qty, amt, inv_id in rows:
        if exclude_invoice_id is not None and inv_id == exclude_invoice_id:
            continue
        q, a = out.get(line_id, (ZERO, ZERO))
        out[line_id] = (q + qty, a + amt)
    return out


def po_snapshot(po: m.PurchaseOrder, consumed: dict[int, tuple[Decimal, Decimal]]) -> dict:
    lines = []
    total_consumed = ZERO
    for ln in po.lines:
        cq, ca = consumed.get(ln.id, (ZERO, ZERO))
        total_consumed += ca
        lines.append({"id": ln.id, "line_no": ln.line_no, "item_code": ln.item_code, "description": ln.description,
                      "uom": ln.uom, "quantity": ln.quantity, "unit_price": ln.unit_price, "tax_rate": ln.tax_rate,
                      "line_amount": ln.line_amount, "consumed_qty": cq, "consumed_amount": ca,
                      "remaining_qty": ln.quantity - cq, "remaining_amount": ln.line_amount - ca})
    return {"id": po.id, "po_number": po.po_number, "vendor_id": po.vendor_id, "vendor_name": po.vendor.name,
            "vendor_code": po.vendor.code, "po_date": po.po_date.isoformat(), "status": po.status,
            "currency": po.currency, "description": po.description, "subtotal": po.subtotal,
            "tax_total": po.tax_total, "total_amount": po.total_amount,
            "qty_tolerance_pct": po.qty_tolerance_pct, "price_tolerance_pct": po.price_tolerance_pct,
            "amount_tolerance_pct": po.amount_tolerance_pct, "consumed_amount": total_consumed,
            "remaining_amount": po.subtotal - total_consumed, "lines": lines}


def record_consumption(db: Session, invoice_id: int, po: m.PurchaseOrder, pairs: list[dict]) -> list[dict]:
    """Write (or update) this invoice's consumption for each matched line. Returns what was recorded."""
    by_no = {ln.line_no: ln for ln in po.lines}
    existing = {r.po_line_id: r for r in db.scalars(select(m.POConsumption).where(m.POConsumption.invoice_id == invoice_id))}
    recorded, touched = [], set()
    for p in pairs:
        line = by_no.get(p["po_line_no"])
        qty = D(p["invoice"]["quantity"])
        if line is None or qty is None:
            continue
        amount = D(p["invoice"]["line_total"]) or q2(qty * line.unit_price)
        row = existing.get(line.id)
        if row:
            row.quantity, row.amount, row.status = qty, amount, "ACTIVE"
        else:
            db.add(m.POConsumption(po_line_id=line.id, invoice_id=invoice_id, quantity=qty, amount=amount))
        touched.add(line.id)
        recorded.append({"po_line_no": line.line_no, "quantity": str(qty), "amount": str(amount)})
    for line_id, row in existing.items():
        if line_id not in touched and row.status == "ACTIVE":
            row.status = "REVERSED"
    return recorded


def reverse_consumption(db: Session, invoice_id: int) -> int:
    n = 0
    for row in db.scalars(select(m.POConsumption).where(m.POConsumption.invoice_id == invoice_id,
                                                         m.POConsumption.status == "ACTIVE")):
        row.status = "REVERSED"
        n += 1
    return n
