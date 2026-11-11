"""Seeding demo data and importing real vendor / PO data (JSON)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import models as m
from .extraction.parsers import classify_tax_id, normalize_tax_id, normalize_ref
from .money import D, q2
from .seed_data import HISTORICAL_INVOICES, PURCHASE_ORDERS, VENDORS


class ImportError_(ValueError):
    pass


def _po_from_dict(db: Session, data: dict, vendor_by_code: dict[str, m.Vendor]) -> m.PurchaseOrder:
    vendor = vendor_by_code.get(data["vendor"])
    if vendor is None:
        raise ImportError_(f"PO {data.get('po_number')}: unknown vendor code {data.get('vendor')!r}")
    po = m.PurchaseOrder(
        po_number=data["po_number"].strip().upper(), vendor=vendor, po_date=date.fromisoformat(data["po_date"]),
        status=data.get("status", "APPROVED").upper(), currency=data.get("currency", vendor.default_currency).upper(),
        description=data.get("description"),
        qty_tolerance_pct=D(data.get("qty_tolerance_pct")), price_tolerance_pct=D(data.get("price_tolerance_pct")),
        amount_tolerance_pct=D(data.get("amount_tolerance_pct")),
    )
    subtotal, tax = Decimal("0"), Decimal("0")
    for i, line in enumerate(data["lines"], start=1):
        if isinstance(line, dict):
            code, desc, uom = line.get("item_code"), line["description"], line.get("uom")
            qty, price, rate = D(line["quantity"]), D(line["unit_price"]), D(line.get("tax_rate", "0"))
        else:
            code, desc, uom, qty, price, rate = line[0], line[1], line[2], D(line[3]), D(line[4]), D(line[5])
        amount = q2(qty * price)
        subtotal += amount
        tax += q2(amount * rate / 100)
        po.lines.append(m.POLine(line_no=i, item_code=code, description=desc, uom=uom, quantity=qty,
                                 unit_price=price, tax_rate=rate, line_amount=amount))
    po.subtotal, po.tax_total, po.total_amount = q2(subtotal), q2(tax), q2(subtotal + tax)
    db.add(po)
    return po


def reset_all(db: Session) -> None:
    for model in (m.AuditEvent, m.ReviewAction, m.POConsumption, m.ProcessingRun, m.Invoice, m.Document,
                  m.POLine, m.PurchaseOrder, m.Vendor, m.RuleOverride):
        db.execute(delete(model))
    db.commit()


def import_procurement(db: Session, payload: dict, replace: bool = False) -> dict:
    """payload = {"vendors": [...], "purchase_orders": [...]} in the same shape as seed_data.py."""
    if replace:
        reset_all(db)
    vendor_by_code = {v.code: v for v in db.scalars(select(m.Vendor))}
    added_v = added_p = 0
    for v in payload.get("vendors", []):
        if v["code"] in vendor_by_code:
            continue
        tax_id = normalize_tax_id(v.get("tax_id"))
        vendor = m.Vendor(code=v["code"], name=v["name"], tax_id=tax_id, tax_id_type=classify_tax_id(tax_id),
                          country=v.get("country", "IN"), address=v.get("address"),
                          status=v.get("status", "APPROVED").upper(),
                          default_currency=v.get("default_currency", "INR").upper())
        db.add(vendor)
        vendor_by_code[v["code"]] = vendor
        added_v += 1
    existing_pos = {p for p in db.scalars(select(m.PurchaseOrder.po_number))}
    for p in payload.get("purchase_orders", []):
        if p["po_number"].strip().upper() in existing_pos:
            continue
        _po_from_dict(db, p, vendor_by_code)
        added_p += 1
    db.commit()
    return {"vendors_added": added_v, "purchase_orders_added": added_p}


def seed_demo(db: Session, reset: bool = False) -> dict:
    if reset:
        reset_all(db)
    if db.scalar(select(m.Vendor.id).limit(1)) is not None:
        return {"seeded": False}
    result = import_procurement(db, {"vendors": VENDORS, "purchase_orders": PURCHASE_ORDERS})
    for h in HISTORICAL_INVOICES:
        vendor = db.scalar(select(m.Vendor).where(m.Vendor.code == h["vendor"]))
        po = db.scalar(select(m.PurchaseOrder).where(m.PurchaseOrder.po_number == h["po_number"]))
        doc = m.Document(content=b"%PDF-1.4 historical placeholder (seeded record, no original file)")
        db.add(doc)
        db.flush()
        inv = m.Invoice(original_filename=f"historical-{h['invoice_number'].replace('/', '-')}.pdf", document_id=doc.id,
                        file_sha256="historical-" + h["invoice_number"], file_size=0, page_count=1, is_scanned=False,
                        status="PROCESSED", stage="DONE", decision="APPROVED", decided_by="SYSTEM",
                        system_decision="APPROVED", vendor_id=vendor.id, po_id=po.id, vendor_tax_id=vendor.tax_id,
                        vendor_name=vendor.name, invoice_number=h["invoice_number"],
                        invoice_number_norm=normalize_ref(h["invoice_number"]),
                        invoice_date=date.fromisoformat(h["invoice_date"]), currency=h["currency"],
                        grand_total=D(h["grand_total"]), po_reference=po.po_number)
        db.add(inv)
        db.flush()
        for code, qty in h["consume"]:
            line = next(ln for ln in po.lines if ln.item_code == code)
            db.add(m.POConsumption(po_line_id=line.id, invoice_id=inv.id, quantity=D(qty),
                                   amount=q2(D(qty) * line.unit_price)))
        db.add(m.AuditEvent(invoice_id=inv.id, run_no=0, seq=1, event_type="HISTORICAL_RECORD_SEEDED",
                            message="Historical approved invoice seeded so PO consumption history exists.", payload={}))
    db.commit()
    return {"seeded": True, **result}
