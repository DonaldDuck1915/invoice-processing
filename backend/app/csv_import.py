"""Purchase orders from a spreadsheet (CSV export), one row per PO line.

AP teams keep POs in a spreadsheet, so this accepts that shape directly. Rows of the same
po_number are grouped into one PO; vendor columns are only needed for vendors that do not
exist yet. Excel / Google Sheets: File -> Save as / Download -> CSV.
"""
from __future__ import annotations

import csv
import io

from .extraction.parsers import parse_date

REQUIRED = ["po_number", "vendor_code", "po_date", "description", "quantity", "unit_price"]
COLUMNS = ["po_number", "vendor_code", "vendor_name", "vendor_tax_id", "vendor_country", "vendor_status",
           "currency", "po_date", "po_status", "po_description", "item_code", "description", "uom",
           "quantity", "unit_price", "tax_rate"]

TEMPLATE = (",".join(COLUMNS) + "\n"
            "PO-5001,V100,Comfort Chairs Pvt Ltd,27ABCDE1234F1Z5,IN,APPROVED,INR,2026-10-01,APPROVED,"
            "Chairs for Pune office,CH-01,Office chair,NOS,10,4500,18\n"
            "PO-5001,V100,,,,,INR,2026-10-01,APPROVED,Chairs for Pune office,CH-02,Chair armrest kit,NOS,10,500,18\n")

# Common header spellings from real spreadsheets -> our column names
ALIASES = {"po": "po_number", "po_no": "po_number", "po_#": "po_number", "purchase_order": "po_number",
           "vendor": "vendor_code", "supplier_code": "vendor_code", "supplier": "vendor_name",
           "gstin": "vendor_tax_id", "vat_id": "vendor_tax_id", "tax_id": "vendor_tax_id",
           "date": "po_date", "order_date": "po_date", "status": "po_status", "item": "description",
           "item_description": "description", "qty": "quantity", "price": "unit_price", "rate": "unit_price",
           "gst_%": "tax_rate", "tax_%": "tax_rate", "gst": "tax_rate", "code": "item_code", "sku": "item_code"}


def _key(header: str) -> str:
    k = header.strip().lower().replace(" ", "_").replace(".", "")
    return ALIASES.get(k, k)


def _clean_number(v: str) -> str:
    return v.replace(",", "").replace("%", "").strip()


def parse_procurement_csv(data: bytes) -> dict:
    """CSV bytes -> {"vendors": [...], "purchase_orders": [...]} for seed.import_procurement.
    Raises ValueError with the row number for anything it cannot use."""
    text = data.decode("utf-8-sig", errors="strict") if isinstance(data, bytes) else data
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("The CSV has no header row.")
    header = {_key(h): h for h in reader.fieldnames if h}
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ValueError(f"Missing column(s): {', '.join(missing)}. Expected: {', '.join(COLUMNS)}")
    vendors: dict[str, dict] = {}
    pos: dict[str, dict] = {}
    for n, raw in enumerate(reader, start=2):  # row 1 is the header
        row = {k: (raw.get(h) or "").strip() for k, h in header.items()}
        if not any(row.values()):
            continue
        for c in REQUIRED:
            if not row.get(c):
                raise ValueError(f"Row {n}: {c} is empty.")
        d, _ = parse_date(row["po_date"], "DMY")
        if d is None:
            raise ValueError(f"Row {n}: po_date {row['po_date']!r} is not a date.")
        code = row["vendor_code"]
        if row.get("vendor_name") and code not in vendors:
            vendors[code] = {"code": code, "name": row["vendor_name"], "tax_id": row.get("vendor_tax_id") or None,
                             "country": (row.get("vendor_country") or "IN").upper(),
                             "status": (row.get("vendor_status") or "APPROVED").upper(),
                             "default_currency": (row.get("currency") or "INR").upper()}
        number = row["po_number"].upper()
        po = pos.setdefault(number, {"po_number": number, "vendor": code, "po_date": d.isoformat(),
                                     "status": (row.get("po_status") or "APPROVED").upper(),
                                     "currency": (row.get("currency") or "").upper() or None,
                                     "description": row.get("po_description") or row["description"], "lines": []})
        if po["vendor"] != code:
            raise ValueError(f"Row {n}: {number} appears with two vendors ({po['vendor']} and {code}).")
        try:
            qty, price = _clean_number(row["quantity"]), _clean_number(row["unit_price"])
            float(qty), float(price)
        except ValueError:
            raise ValueError(f"Row {n}: quantity / unit_price must be numbers.") from None
        po["lines"].append({"item_code": row.get("item_code") or None, "description": row["description"],
                            "uom": row.get("uom") or None, "quantity": qty, "unit_price": price,
                            "tax_rate": _clean_number(row.get("tax_rate") or "0") or "0"})
    for po in pos.values():
        if po["currency"] is None:
            po.pop("currency")  # falls back to the vendor's default currency
    if not pos:
        raise ValueError("The CSV contains no PO rows.")
    return {"vendors": list(vendors.values()), "purchase_orders": list(pos.values())}
