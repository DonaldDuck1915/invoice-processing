"""Generate demo invoice PDFs (digital + simulated scans) for every scenario.

    python scripts/generate_samples.py [out_dir]

Layouts differ deliberately (labels, column order, number formats, tax presentation) so
extraction is exercised across "vendors". Scans are rasterised, rotated and noised.
"""
from __future__ import annotations

import io
import random
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.seed_data import COMPANY, PURCHASE_ORDERS, VENDORS  # noqa: E402

VENDOR = {v["code"]: v for v in VENDORS}
PO = {p["po_number"]: p for p in PURCHASE_ORDERS}
C = Decimal("0.01")


def q(v: Decimal) -> Decimal:
    return v.quantize(C, rounding=ROUND_HALF_UP)


def fmt(v: Decimal, style: str) -> str:
    v = q(v)
    whole, frac = f"{abs(v):.2f}".split(".")
    if style == "IN" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        rev = head[::-1]
        head = ",".join(rev[i:i + 2] for i in range(0, len(rev), 2))[::-1]
        whole = f"{head},{tail}"
    elif style in ("US", "EU"):
        whole = f"{int(whole):,}"
    if style == "EU":
        whole = whole.replace(",", ".")
        return f"{'-' if v < 0 else ''}{whole},{frac}"
    return f"{'-' if v < 0 else ''}{whole}.{frac}"


def fmt_qty(v: Decimal) -> str:
    return format(v.normalize(), "f")


def compute(spec):
    lines = []
    for code, desc, uom, qty, price, rate in spec["lines"]:
        qty, price, rate = Decimal(qty), Decimal(price), Decimal(rate)
        amt = q(qty * price)
        lines.append({"code": code, "desc": desc, "uom": uom, "qty": qty, "price": price, "rate": rate, "amount": amt})
    for idx, wrong in spec.get("wrong_line_total", {}).items():
        lines[idx]["amount"] = Decimal(wrong)
    subtotal = sum((ln["amount"] for ln in lines), Decimal("0"))
    discount = Decimal(spec.get("discount", "0"))
    shipping = Decimal(spec.get("shipping", "0"))
    taxable = subtotal - discount
    tax = q(sum((q(ln["amount"] * ln["rate"] / 100) for ln in lines), Decimal("0")) * (taxable / subtotal if subtotal else 1))
    grand = Decimal(spec["grand_total"]) if "grand_total" in spec else q(taxable + tax + shipping)
    return lines, subtotal, discount, shipping, taxable, tax, grand


# ----------------------------------------------------------------------------- layouts

def layout_classic(c: canvas.Canvas, spec, v):
    """Indian GST tax invoice with grid table and CGST/SGST or IGST."""
    lines, subtotal, discount, shipping, taxable, tax, grand = compute(spec)
    W, H = A4
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(W / 2, H - 40, "TAX INVOICE")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 75, v["name"])
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 90, v["address"])
    c.drawString(40, H - 103, f"GSTIN: {v['tax_id']}")
    y = H - 135
    c.setFont("Helvetica", 10)
    left = ["Bill To:", COMPANY["name"], COMPANY["address"][:48], f"GSTIN: {COMPANY['gstin']}"]
    right = []
    if spec.get("number"):
        right.append(f"Invoice No: {spec['number']}")
    right += [f"Invoice Date: {spec['date']}"]
    if spec.get("due"):
        right.append(f"Due Date: {spec['due']}")
    if spec.get("po"):
        right.append(f"PO No: {spec['po']}")
    right.append(f"Currency: {spec['currency']}")
    for i in range(max(len(left), len(right))):
        if i < len(left):
            c.drawString(40, y, left[i])
        if i < len(right):
            c.drawString(360, y, right[i])
        y -= 14
    y -= 16
    cols = [(40, "#"), (60, "Item Code"), (130, "Description"), (330, "Qty"), (365, "UOM"), (405, "Rate"),
            (470, "GST %"), (515, "Amount")]
    c.setFont("Helvetica-Bold", 9)
    c.line(38, y + 12, 565, y + 12)
    for x, label in cols:
        c.drawString(x, y, label)
    c.line(38, y - 5, 565, y - 5)
    y -= 20
    c.setFont("Helvetica", 9)
    st = spec.get("numfmt", "IN")
    for i, ln in enumerate(lines, 1):
        c.drawString(40, y, str(i))
        c.drawString(60, y, ln["code"] or "")
        c.drawString(130, y, ln["desc"][:44])
        c.drawRightString(350, y, fmt_qty(ln["qty"]))
        c.drawString(365, y, ln["uom"])
        c.drawRightString(455, y, fmt(ln["price"], st))
        c.drawRightString(495, y, fmt_qty(ln["rate"]))
        c.drawRightString(565, y, fmt(ln["amount"], st))
        y -= 16
    c.line(38, y + 6, 565, y + 6)
    y -= 14

    def tot(label, val, bold=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.drawString(360, y, label)
        c.drawRightString(565, y, fmt(val, st))
        y -= 15

    tot("Sub Total", subtotal)
    if discount:
        tot("Discount", discount)
    if spec.get("tax_mode") == "IGST":
        rate = lines[0]["rate"]
        tot(f"IGST @ {fmt_qty(rate)}%", tax)
    else:
        rate = lines[0]["rate"] / 2
        half = q(tax / 2)
        tot(f"CGST @ {fmt_qty(rate)}%", half)
        tot(f"SGST @ {fmt_qty(rate)}%", tax - half)
    if shipping:
        tot("Freight Charges", shipping)
    tot(f"Grand Total ({spec['currency']})", grand, bold=True)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, y - 10, "Amount in words: as per figures above. Subject to Pune jurisdiction.")
    c.drawString(40, y - 40, f"For {v['name']} - Authorised Signatory")


def layout_minimal(c: canvas.Canvas, spec, v):
    """Minimal layout: 'From:' vendor, 'Bill No.', 'Dated', column order differs, single tax line."""
    lines, subtotal, discount, shipping, taxable, tax, grand = compute(spec)
    W, H = A4
    st = spec.get("numfmt", "IN")
    c.setFont("Helvetica-Bold", 22)
    c.drawString(40, H - 55, "INVOICE")
    c.setFont("Helvetica", 10)
    y = H - 90
    for text in [f"From: {v['name']}", v["address"],
                 (f"VAT ID: {v['tax_id']}" if v["country"] == "DE" else f"GSTIN {v['tax_id']}")]:
        c.drawString(40, y, text)
        y -= 14
    y -= 8
    meta = []
    if spec.get("number"):
        meta.append(f"Bill No. {spec['number']}")
    meta.append(f"Dated: {spec['date']}")
    if spec.get("po"):
        meta.append(f"Your PO Ref: {spec['po']}")
    meta.append(f"Currency: {spec['currency']}")
    for text in meta:
        c.drawString(40, y, text)
        y -= 14
    c.drawString(330, H - 90, "Billed to:")
    c.drawString(330, H - 104, COMPANY["name"])
    c.drawString(330, H - 118, f"GSTIN {COMPANY['gstin']}")
    y -= 20
    cols = [(40, "Description", "l"), (250, "HSN", "l"), (365, "Quantity", "r"), (435, "Unit Price", "r"),
            (480, "Tax %", "r"), (560, "Line Total", "r")]
    c.setFont("Helvetica-Bold", 9)
    for x, label, align in cols:
        (c.drawString if align == "l" else c.drawRightString)(x, y, label)
    y -= 18
    c.setFont("Helvetica", 9)
    for ln in lines:
        c.drawString(40, y, ln["desc"][:40])
        c.drawString(250, y, ln["code"] or "")
        c.drawRightString(365, y, fmt_qty(ln["qty"]))
        c.drawRightString(435, y, fmt(ln["price"], st))
        c.drawRightString(480, y, fmt_qty(ln["rate"]))
        c.drawRightString(560, y, fmt(ln["amount"], st))
        y -= 15
    y -= 15
    c.setFont("Helvetica", 10)
    rows = [("Taxable Value", taxable if not discount else subtotal)]
    if discount:
        rows.append(("Discount", discount))
    label = "VAT" if spec.get("tax_mode") == "VAT" else "IGST"
    rows.append((f"{label} {fmt_qty(lines[0]['rate'])}%", tax))
    if shipping:
        rows.append(("Shipping", shipping))
    rows.append(("Total Amount Payable", grand))
    for label, val in rows:
        c.drawString(330, y, label)
        c.drawRightString(560, y, fmt(val, st))
        y -= 15
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, 60, "Thank you for your business.")


def layout_eu(c: canvas.Canvas, spec, v):
    lines, subtotal, discount, shipping, taxable, tax, grand = compute(spec)
    W, H = A4
    st = "EU"
    c.setFont("Helvetica-Bold", 15)
    c.drawString(40, H - 50, v["name"])
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 64, v["address"])
    c.drawString(40, H - 77, f"VAT ID: {v['tax_id']}")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(560, H - 50, "COMMERCIAL INVOICE")
    c.setFont("Helvetica", 10)
    y = H - 115
    info = [f"Invoice #: {spec['number']}", f"Invoice Date: {spec['date']}"]
    if spec.get("due"):
        info.append(f"Payment Due: {spec['due']}")
    if spec.get("po"):
        info.append(f"Order Ref: {spec['po']}")
    info.append(f"Currency: {spec['currency']}")
    for t in info:
        c.drawString(40, y, t)
        y -= 14
    c.drawString(330, H - 115, "Customer:")
    c.drawString(330, H - 129, COMPANY["name"])
    c.drawString(330, H - 143, "Pune, India")
    y -= 18
    cols = [(40, "Pos", "l"), (70, "Code", "l"), (130, "Description", "l"), (360, "Qty", "r"), (375, "Unit", "l"),
            (470, "Unit Price", "r"), (505, "VAT %", "r"), (560, "Amount", "r")]
    c.setFont("Helvetica-Bold", 9)
    for x, label, a in cols:
        (c.drawString if a == "l" else c.drawRightString)(x, y, label)
    y -= 18
    c.setFont("Helvetica", 9)
    for i, ln in enumerate(lines, 1):
        c.drawString(40, y, str(i))
        c.drawString(70, y, ln["code"])
        c.drawString(130, y, ln["desc"][:40])
        c.drawRightString(360, y, fmt_qty(ln["qty"]))
        c.drawString(375, y, ln["uom"])
        c.drawRightString(470, y, fmt(ln["price"], st))
        c.drawRightString(505, y, fmt_qty(ln["rate"]))
        c.drawRightString(560, y, fmt(ln["amount"], st))
        y -= 15
    y -= 15
    c.setFont("Helvetica", 10)
    for label, val in [("Subtotal", subtotal), (f"VAT {fmt_qty(lines[0]['rate'])}% (reverse charge)", tax),
                       (f"Total Due {spec['currency']}", grand)]:
        c.drawString(330, y, label)
        c.drawRightString(560, y, fmt(val, st))
        y -= 15
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, 70, "Reverse charge: VAT to be accounted for by the recipient.")


def layout_us(c: canvas.Canvas, spec, v):
    lines, subtotal, discount, shipping, taxable, tax, grand = compute(spec)
    W, H = A4
    st = "US"
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 50, v["name"].upper())
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 64, v["address"])
    c.drawString(40, H - 77, f"EIN: {v['tax_id']}")
    c.setFont("Helvetica", 10)
    y = H - 110
    info = []
    if spec.get("number"):
        info.append(f"Invoice Number: {spec['number']}")
    info.append(f"Invoice Date: {spec['date']}")
    if spec.get("due"):
        info.append(f"Due Date: {spec['due']}")
    if spec.get("po"):
        info.append(f"P.O. Number: {spec['po']}")
    for t in info:
        c.drawString(40, y, t)
        y -= 14
    c.drawString(330, H - 110, "Ship To / Bill To:")
    c.drawString(330, H - 124, COMPANY["name"])
    y -= 18
    cols = [(40, "Item #", "l"), (110, "Description", "l"), (380, "Qty", "r"), (470, "Unit Price", "r"),
            (560, "Amount", "r")]
    c.setFont("Helvetica-Bold", 9)
    for x, label, a in cols:
        (c.drawString if a == "l" else c.drawRightString)(x, y, label)
    y -= 18
    c.setFont("Helvetica", 9)
    for ln in lines:
        c.drawString(40, y, ln["code"])
        c.drawString(110, y, ln["desc"][:44])
        c.drawRightString(380, y, fmt_qty(ln["qty"]))
        c.drawRightString(470, y, fmt(ln["price"], st))
        c.drawRightString(560, y, fmt(ln["amount"], st))
        y -= 15
    y -= 15
    c.setFont("Helvetica", 10)
    rows = [("Subtotal", subtotal), (f"Sales Tax ({fmt_qty(lines[0]['rate'])}%)", tax)]
    if shipping:
        rows.append(("Shipping & Handling", shipping))
    rows.append((f"Amount Due {spec['currency']}", grand))
    for label, val in rows:
        c.drawString(330, y, label)
        c.drawRightString(560, y, fmt(val, st))
        y -= 15


def layout_inclusive(c: canvas.Canvas, spec, v):
    """Retail-style invoice: prices INCLUDE GST; tax is only shown as 'Includes IGST @ x%'."""
    W, H = A4
    st = "IN"
    lines = []
    for code, desc, uom, qty, price, rate in spec["lines"]:
        qty, rate = Decimal(qty), Decimal(rate)
        gross_price = q(Decimal(price) * (1 + rate / 100))
        lines.append({"code": code, "desc": desc, "uom": uom, "qty": qty, "price": gross_price, "rate": rate,
                      "amount": q(qty * gross_price)})
    gross = sum((ln["amount"] for ln in lines), Decimal("0"))
    tax = sum((ln["amount"] - q(ln["amount"] / (1 + ln["rate"] / 100)) for ln in lines), Decimal("0"))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 50, v["name"])
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 64, v["address"])
    c.drawString(40, H - 77, f"GSTIN: {v['tax_id']}")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(560, H - 50, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    y = H - 110
    for t in [f"Invoice No: {spec['number']}", f"Invoice Date: {spec['date']}", f"PO No: {spec['po']}",
              f"Currency: {spec['currency']}"]:
        c.drawString(40, y, t)
        y -= 14
    c.drawString(330, H - 110, "Bill To:")
    c.drawString(330, H - 124, COMPANY["name"])
    c.drawString(330, H - 138, f"GSTIN: {COMPANY['gstin']}")
    y -= 6
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(40, y, "All prices are inclusive of GST.")
    y -= 22
    cols = [(40, "Item Code", "l"), (120, "Description", "l"), (330, "Qty", "r"), (345, "UOM", "l"),
            (440, "Rate", "r"), (500, "GST %", "r"), (560, "Amount", "r")]
    c.setFont("Helvetica-Bold", 9)
    for x, label, a in cols:
        (c.drawString if a == "l" else c.drawRightString)(x, y, label)
    y -= 18
    c.setFont("Helvetica", 9)
    for ln in lines:
        c.drawString(40, y, ln["code"])
        c.drawString(120, y, ln["desc"][:40])
        c.drawRightString(330, y, fmt_qty(ln["qty"]))
        c.drawString(345, y, ln["uom"])
        c.drawRightString(440, y, fmt(ln["price"], st))
        c.drawRightString(500, y, fmt_qty(ln["rate"]))
        c.drawRightString(560, y, fmt(ln["amount"], st))
        y -= 15
    y -= 15
    c.setFont("Helvetica", 10)
    for label, val, bold in [("Sub Total", gross, False), (f"Grand Total ({spec['currency']})", gross, True),
                             (f"Includes IGST @ {fmt_qty(lines[0]['rate'])}%", tax, False)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.drawString(330, y, label)
        c.drawRightString(560, y, fmt(val, st))
        y -= 15


LAYOUTS = {"classic": layout_classic, "minimal": layout_minimal, "eu": layout_eu, "us": layout_us,
           "inclusive": layout_inclusive}


def render_pdf(spec) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {spec.get('number') or ''}")
    LAYOUTS[spec["layout"]](c, spec, VENDOR[spec["vendor"]])
    c.showPage()
    c.save()
    return buf.getvalue()


def scan(pdf_bytes: bytes, quality: str = "normal", seed: int = 7) -> bytes:
    """Rasterise to an image-only PDF with rotation/noise, like a desk scanner."""
    rnd = random.Random(seed)
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    dpi = 200 if quality == "normal" else 85
    for page in src:
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csGRAY)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
        img = img.rotate(rnd.uniform(0.4, 0.9) * (1 if quality == "normal" else 2.2), expand=False, fillcolor=255,
                         resample=Image.BICUBIC)
        px = img.load()
        noise = 0.004 if quality == "normal" else 0.04
        for _ in range(int(img.width * img.height * noise)):
            x, y = rnd.randrange(img.width), rnd.randrange(img.height)
            px[x, y] = rnd.choice((0, 90, 200))
        if quality != "normal":
            img = img.filter(ImageFilter.GaussianBlur(1.1))
        jpg = io.BytesIO()
        img.save(jpg, "JPEG", quality=70 if quality == "normal" else 25)
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=jpg.getvalue())
    return out.tobytes()


def po_lines(po_number, qty_override=None, price_override=None):
    lines = []
    for i, (code, desc, uom, qty, price, rate) in enumerate(PO[po_number]["lines"]):
        q_ = (qty_override or {}).get(i, qty)
        p_ = (price_override or {}).get(i, price)
        lines.append((code, desc, uom, q_, p_, rate))
    return lines


SCENARIOS = [
    # ---- happy paths
    {"file": "01_happy_path_digital.pdf", "layout": "classic", "vendor": "V004", "number": "TN/26-27/0412",
     "date": "28/10/2026", "due": "27/11/2026", "po": "PO-2005", "currency": "INR", "tax_mode": "IGST",
     "lines": po_lines("PO-2005"),
     "expect": "APPROVED", "story": "Clean digital invoice, explicit PO, all within tolerance."},
    {"file": "02_happy_path_scanned.pdf", "layout": "classic", "vendor": "V004", "number": "TN/26-27/0431",
     "date": "02/11/2026", "po": "PO-2006", "currency": "INR", "tax_mode": "IGST", "lines": po_lines("PO-2006"),
     "scan": "normal", "expect": "APPROVED", "story": "Scanned image PDF; OCR path."},
    # ---- EC1 split invoices
    {"file": "03_ec1_split_a_60_chairs.pdf", "layout": "classic", "vendor": "V001", "number": "ESS/26-27/0142",
     "date": "15/10/2026", "po": "PO-2001", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": po_lines("PO-2001", {0: "60"}), "expect": "APPROVED", "story": "First of two split invoices (60/100)."},
    {"file": "04_ec1_split_b_40_chairs.pdf", "layout": "classic", "vendor": "V001", "number": "ESS/26-27/0167",
     "date": "30/10/2026", "po": "PO-2001", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": po_lines("PO-2001", {0: "40"}), "expect": "APPROVED", "story": "Second split invoice (40/100) - PO now fully consumed."},
    {"file": "05_ec1_split_c_overbill_5_chairs.pdf", "layout": "classic", "vendor": "V001", "number": "ESS/26-27/0181",
     "date": "05/11/2026", "po": "PO-2001", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": po_lines("PO-2001", {0: "5"}), "expect": "REJECTED", "story": "Cumulative 105 > PO 100."},
    # ---- EC2 duplicates
    {"file": "06_ec2_original_eur.pdf", "layout": "eu", "vendor": "V005", "number": "RI-2026-0045",
     "date": "2026-10-02", "due": "2026-11-01", "po": "PO-2007", "currency": "EUR", "tax_mode": "VAT",
     "lines": po_lines("PO-2007"), "expect": "APPROVED", "story": "Original EUR invoice."},
    {"file": "07_ec2_resend_scanned_ocr_number.pdf", "layout": "minimal", "vendor": "V005", "number": "RI-2026-8045",
     "date": "02.10.2026", "po": "PO-2007", "currency": "EUR", "tax_mode": "VAT", "numfmt": "EU",
     "lines": po_lines("PO-2007"), "scan": "normal", "expect": "MANUAL_REVIEW",
     "story": "Same invoice re-sent: new filename, different layout, scanned, invoice no. misread (0 -> 8)."},
    {"file": "08_ec2_recurring_jan.pdf", "layout": "classic", "vendor": "V008", "number": "GLF/HK/2026/10",
     "date": "31/10/2026", "po": "PO-2010", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": [("SVC-HK-M", "Housekeeping services - October 2026", "MONTH", "1", "40000", "18")],
     "expect": "APPROVED", "story": "Monthly service invoice."},
    {"file": "09_ec2_recurring_feb_same_amount.pdf", "layout": "classic", "vendor": "V008", "number": "GLF/HK/2026/11",
     "date": "07/11/2026", "po": "PO-2010", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": [("SVC-HK-M", "Housekeeping services - November 2026", "MONTH", "1", "40000", "18")],
     "expect": "APPROVED", "story": "Same vendor + same amount but a genuinely new invoice - must NOT be flagged."},
    # ---- EC3 price tolerance
    {"file": "10_ec3_price_8pct_over.pdf", "layout": "minimal", "vendor": "V002", "number": "NOE/26-27/0310",
     "date": "14-Oct-2026", "po": "PO-2003", "currency": "INR", "tax_mode": "IGST",
     "lines": po_lines("PO-2003", price_override={0: "1080"}), "expect": "MANUAL_REVIEW",
     "story": "Unit price 1,080 vs PO 1,000 = 8% > 5% tolerance."},
    {"file": "11_ec3_price_3pct_within.pdf", "layout": "minimal", "vendor": "V002", "number": "NOE/26-27/0322",
     "date": "21-Oct-2026", "po": "PO-2003", "currency": "INR", "tax_mode": "IGST",
     "lines": po_lines("PO-2003", price_override={0: "1030"}), "expect": "APPROVED",
     "story": "Unit price 1,030 = 3% - within tolerance."},
    # ---- EC4 missing PO
    {"file": "12_ec4_no_po_ambiguous.pdf", "layout": "classic", "vendor": "V003", "number": "PWI/0937",
     "date": "20/10/2026", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": [("", "Office supplies - assorted, as per order", "LOT", "1", "50000", "18")],
     "expect": "MANUAL_REVIEW", "story": "No PO number; 'office supplies' for INR 50,000 fits PO-101 and PO-103 equally."},
    {"file": "13_ec4_no_po_clear_lines.pdf", "layout": "classic", "vendor": "V003", "number": "PWI/0952",
     "date": "27/10/2026", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": [("FUR-CHR-05", "Ergonomic office chair with headrest", "NOS", "5", "6000", "18"),
               ("FUR-DSK-04", "Office desk 5ft with drawer", "NOS", "4", "5000", "18")],
     "expect": "APPROVED*", "story": "No PO number but line items/codes clearly point to PO-101 (*review if AI is off)."},
    # ---- other rule scenarios
    {"file": "14_reject_cancelled_po.pdf", "layout": "classic", "vendor": "V001", "number": "ESS/26-27/0150",
     "date": "18/10/2026", "po": "PO-2002", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": po_lines("PO-2002"), "expect": "REJECTED", "story": "Invoice against a cancelled PO."},
    {"file": "15_reject_po_of_other_vendor.pdf", "layout": "minimal", "vendor": "V002", "number": "NOE/26-27/0331",
     "date": "24-Oct-2026", "po": "PO-2005", "currency": "INR", "tax_mode": "IGST",
     "lines": po_lines("PO-2005"), "expect": "REJECTED", "story": "Cites a PO that belongs to a different vendor."},
    {"file": "16_review_blocked_vendor.pdf", "layout": "classic", "vendor": "V007", "number": "QPS/1189",
     "date": "22/10/2026", "po": "PO-2009", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": po_lines("PO-2009"), "expect": "MANUAL_REVIEW", "story": "Vendor is BLOCKED - flagged for review."},
    {"file": "17_review_pending_vendor.pdf", "layout": "classic", "vendor": "V009", "number": "SCS/OCT/26",
     "date": "31/10/2026", "po": "PO-2012", "currency": "INR", "tax_mode": "IGST", "lines": po_lines("PO-2012"),
     "expect": "MANUAL_REVIEW", "story": "Vendor approval PENDING - flagged for review."},
    {"file": "18_review_currency_mismatch.pdf", "layout": "us", "vendor": "V004", "number": "TN-EXP-0042",
     "date": "10/20/2026", "po": "PO-2011", "currency": "USD", "tax_mode": "SALES",
     "lines": [("LS-ALU", "Aluminium laptop stand adjustable", "EA", "5", "30", "0")], "expect": "MANUAL_REVIEW",
     "story": "USD invoice against an INR PO."},
    {"file": "19_review_missing_number_math_error.pdf", "layout": "us", "vendor": "V006", "number": None,
     "date": "10/12/2026", "due": "11/11/2026", "po": "PO-2008", "currency": "USD", "tax_mode": "SALES",
     "lines": po_lines("PO-2008"), "wrong_line_total": {1: "652.50"}, "expect": "MANUAL_REVIEW",
     "story": "No invoice number; line 2 total (652.50) != 100 x 6.25."},
    {"file": "20_review_low_quality_scan.pdf", "layout": "classic", "vendor": "V008", "number": "GLF/HK/2026/12",
     "date": "09/11/2026", "po": "PO-2010", "currency": "INR", "tax_mode": "CGST_SGST",
     "lines": [("SVC-HK-M", "Housekeeping services - deep clean", "MONTH", "1", "40000", "18")], "scan": "poor",
     "expect": "MANUAL_REVIEW", "story": "Barely legible scan - low OCR confidence."},
    # ---- EC5 tax embedded in the prices
    {"file": "23_ec5_tax_inclusive_prices.pdf", "layout": "inclusive", "vendor": "V004", "number": "TN/26-27/0457",
     "date": "06/11/2026", "po": "PO-2011", "currency": "INR", "lines": po_lines("PO-2011"), "expect": "APPROVED",
     "story": "Prices include 18% GST (2,950 per stand vs PO 2,500 net). Net values are derived before matching, "
              "so it is not mistaken for an 18% price variance."},
]


def main(out_dir: str | None = None, only: list[str] | None = None):
    out = Path(out_dir or Path(__file__).resolve().parents[1] / "samples")
    out.mkdir(parents=True, exist_ok=True)
    readme = ["# Demo invoices", "", "Upload in numeric order (some scenarios depend on earlier ones).", "",
              "| File | Expected | Scenario |", "|---|---|---|"]
    for spec in SCENARIOS:
        if only and spec["file"] not in only:
            readme.append(f"| {spec['file']} | {spec['expect']} | {spec['story']} |")
            continue
        data = render_pdf(spec)
        if spec.get("scan"):
            data = scan(data, spec["scan"])
        (out / spec["file"]).write_bytes(data)
        readme.append(f"| {spec['file']} | {spec['expect']} | {spec['story']} |")
    (out / "21_error_corrupt.pdf").write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog >>\n" + b"\x00garbage" * 20)
    (out / "22_error_not_a_pdf.pdf").write_bytes(b"This is a text file renamed to .pdf\n" * 5)
    readme += ["| 21_error_corrupt.pdf | Error | Truncated / corrupt PDF |",
               "| 22_error_not_a_pdf.pdf | Error | Not a PDF at all |", "",
               "Re-uploading `06_ec2_original_eur.pdf` a second time demonstrates a CONFIRMED duplicate (REJECTED)."]
    (out / "README.md").write_text("\n".join(readme) + "\n")
    print(f"Wrote {len(SCENARIOS) + 2} files to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
