"""Deterministic, offline field extractor: label anchors + regex + column-aware table parsing.

Works on positioned words from either the text layer or OCR, so the same logic serves
digital and scanned invoices. Confidence = mean source-word confidence x parser certainty.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from ..money import D, detect_currency, parse_amount, s
from ..schemas import ExtractedInvoice, FieldValue, LineItem, TaxComponent
from ..tax_mode import detect_tax_inclusive
from .parsers import (EU_VAT_RE, GSTIN_LABELLED_RE, GSTIN_LOOSE_RE, GSTIN_RE, US_EIN_RE, classify_tax_id,
                      gstin_is_valid, parse_date, repair_gstin)
from .pdf_reader import DocumentText, Line, Word

_ID = r"([A-Z0-9][A-Z0-9\-/]{1,30})"
RX_INVOICE_NO = re.compile(r"\b(?:tax\s+)?(?:invoice|inv|bill)\s*(?:no|number|num|#|id)\.?\s*[:#\-]?\s*" + _ID, re.I)
RX_INVOICE_DATE = re.compile(r"\b(?:invoice|inv|bill)\.?\s*date\s*[:\-]?\s*(.+)", re.I)
RX_DATE_GENERIC = re.compile(r"(?<!due\s)(?<!po\s)(?<!order\s)\bdated?\s*[:\-]\s*(.+)", re.I)
RX_DUE_DATE = re.compile(r"\b(?:due\s*date|payment\s*due|due\s*on|pay\s*by)\s*[:\-]?\s*(.+)", re.I)
RX_PO = re.compile(r"\b(?:p\.?\s?o\.?|purchase\s*order|order)\s*(?:no|number|ref(?:erence)?|#)?\.?\s*[:#\-]?\s*" + _ID, re.I)
RX_VENDOR_LABEL = re.compile(r"^\s*(?:supplier|vendor|from|seller|sold\s*by|billed\s*by)\s*[:\-]\s*(.+)", re.I)
RX_NOTE_TITLE = re.compile(r"\b(credit|debit)\s*note\b", re.I)
RX_NOTE_NO = re.compile(r"\b(?:credit|debit)\s*note\s*(?:no|number|num|#)\.?\s*[:#\-]?\s*" + _ID, re.I)
RX_ORIGINAL_INVOICE = re.compile(r"\b(?:against|ref(?:erence)?\.?\s*(?:to)?|original|orig\.?)\s*(?:tax\s+)?(?:invoice|inv|bill)"
                                 r"\s*(?:no|number|num|#)?\.?\s*[:#\-]?\s*" + _ID, re.I)
RX_IRN = re.compile(r"\bIRN\s*(?:no\.?)?\s*[:\-]?\s*([0-9a-fA-F]{64})\b")
RX_BANK_ACCOUNT = re.compile(r"\b(?:a/?c|account|acct)\.?\s*(?:no|number|num|#)\.?\s*[:\-]?\s*([0-9][0-9\s\-]{7,22}[0-9])", re.I)
RX_IBAN = re.compile(r"\bIBAN\s*[:\-]?\s*([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?)", re.I)
RX_IFSC = re.compile(r"\b(?:IFSC|SWIFT|BIC|routing)\s*(?:code|no\.?|number)?\s*[:\-]?\s*([A-Z0-9]{8,11})\b", re.I)
RX_CURRENCY_LABEL = re.compile(r"\b(?i:currency)\s*[:\-]?\s*([A-Z]{3})\b")  # any ISO 4217 code after "Currency:"

RX_SUBTOTAL = re.compile(r"\bsub\s*-?\s*total\b", re.I)
RX_TAXABLE = re.compile(r"\btaxable\s*(?:value|amount)?\b", re.I)
RX_TAX_TOTAL = re.compile(r"\b(?:total\s*tax|tax\s*total|tax\s*amount|total\s*gst|gst\s*total|total\s*vat|vat\s*total)\b", re.I)
RX_DISCOUNT = re.compile(r"\b(?:discount|less\s*:)", re.I)
RX_SHIPPING = re.compile(r"\b(?:shipping|freight|delivery\s*charges?|packing|handling)\b", re.I)
RX_GRAND = re.compile(r"\b(?:grand\s*total|total\s*amount\s*(?:due|payable)?|amount\s*(?:due|payable)|invoice\s*total|"
                      r"balance\s*due|net\s*payable|total\s*payable|total\s*due)\b", re.I)
RX_TOTAL_PLAIN = re.compile(r"^\s*total\b", re.I)
RX_TAX_COMPONENT = re.compile(r"\b(CGST|SGST|UTGST|IGST|VAT|SALES\s*TAX|GST)\b\s*(?:@\s*)?\(?\s*(\d{1,2}(?:\.\d+)?)?\s*%?", re.I)
RX_TABLE_STOP = re.compile(r"^\s*(?:sub\s*-?\s*total|total|taxable|amount\s*in\s*words|cgst|sgst|igst|vat|tax|"
                           r"discount|shipping|freight|grand)\b", re.I)
RX_TABLE_STOP_ANY = re.compile(r"\b(?:sub\s*-?\s*total|grand\s*total|amount\s*in\s*words|taxable\s*value)\b", re.I)
NUM_TOKEN = re.compile(r"^[\(\-]?[\d][\d,.']*\)?%?$")


@dataclass
class Hit:
    text: str
    conf: float
    page: int


def _fv(value, raw, conf, page, source, note=None, factor=1.0) -> FieldValue:
    c = round(min(conf * factor, 0.99), 3)
    return FieldValue(value=value, raw=raw, confidence=c, status="EXTRACTED", source=source, page=page, note=note)


def _missing(note=None) -> FieldValue:
    return FieldValue(status="MISSING", confidence=0.0, note=note)


def _first(lines: list[Line], rx: re.Pattern, need_digit=False, reverse=False) -> tuple[re.Match, Line] | None:
    seq = reversed(lines) if reverse else lines
    for ln in seq:
        for m in rx.finditer(ln.text):
            val = m.group(1) if m.groups() else m.group(0)
            if need_digit and not re.search(r"\d", val or ""):
                continue
            return m, ln
    return None


def _last_amount(text: str):
    tokens = re.findall(r"\(?-?\s?[\d][\d,.']*\d\)?|\b\d\b", text)
    for t in reversed(tokens):
        v = parse_amount(t)
        if v is not None:
            return v, t
    return None, None


# ----------------------------------------------------------------------------- line items

COL_RULES = [
    ("code", re.compile(r"\b(hsn|sac|sku|code|part|art)\b|item\s*(#|no\b)", re.I)),
    ("rate", re.compile(r"\b(unit\s*price|rate|price|unit\s*cost)\b", re.I)),
    ("tax", re.compile(r"\b(tax|gst|vat)\s*(%|rate)|%", re.I)),
    ("amount", re.compile(r"\b(amount|line\s*total|total|value)\b", re.I)),
    ("qty", re.compile(r"\b(qty|quantity|qnty)\b", re.I)),
    ("uom", re.compile(r"\b(uom|unit|units)\b", re.I)),
    ("desc", re.compile(r"\b(description|item|particulars|product|service|details)\b", re.I)),
    ("idx", re.compile(r"^(#|s\.?\s?no\.?|sr\.?|sl\.?|no\.?)$", re.I)),
]


def _is_header(line: Line) -> bool:
    t = line.text.lower()
    has_qty = bool(re.search(r"\b(qty|quantity|qnty)\b", t))
    has_amt = bool(re.search(r"\b(amount|total|value)\b", t))
    has_rate = bool(re.search(r"\b(rate|price|unit\s*cost)\b", t))
    has_desc = bool(re.search(r"\b(description|item|particulars|product|service|details)\b", t))
    return has_amt and (has_qty or has_rate) and (has_desc or has_rate)


def _header_cells(line: Line) -> list[tuple[str, float, float, str]]:
    words = line.words
    gaps = [b.x0 - a.x1 for a, b in zip(words, words[1:])]
    char_w = statistics.median([(w.x1 - w.x0) / max(len(w.text), 1) for w in words]) if words else 5
    cells: list[list[Word]] = [[words[0]]] if words else []
    for w, gap in zip(words[1:], gaps):
        if gap < char_w * 1.6:
            cells[-1].append(w)
        else:
            cells.append([w])
    out = []
    for cell in cells:
        label = " ".join(w.text for w in cell)
        kind = "other"
        for k, rx in COL_RULES:
            if rx.search(label):
                kind = k
                break
        out.append((kind, cell[0].x0, cell[-1].x1, label))
    return out


def _assign(words: list[Word], cells) -> dict[str, list[Word]]:
    bounds = []
    # Text columns (description) extend right up to the next header; numeric columns split at midpoints.
    cut = []
    for i in range(len(cells) - 1):
        kind, _, x1, _ = cells[i]
        cut.append(cells[i + 1][1] - 4 if kind in ("desc", "other") else (x1 + cells[i + 1][1]) / 2)
    for i in range(len(cells)):
        left = -1e9 if i == 0 else cut[i - 1]
        right = 1e9 if i == len(cells) - 1 else cut[i]
        bounds.append((left, right))
    out: dict[str, list[Word]] = {}
    for w in words:
        for (kind, *_), (left, right) in zip(cells, bounds):
            if left <= w.xc < right:
                out.setdefault(kind, []).append(w)
                break
    return out


def _parse_items(lines: list[Line]) -> tuple[list[LineItem], int | None, int | None]:
    """Returns (items, header_index, end_index) in `lines` coordinates."""
    header_idx = next((i for i, ln in enumerate(lines) if _is_header(ln)), None)
    if header_idx is None:
        return [], None, None
    cells = _header_cells(lines[header_idx])
    kinds = {c[0] for c in cells}
    if "desc" not in kinds:
        # first non-numeric column becomes the description
        cells = [("desc" if i == 0 and c[0] in ("other", "idx") else c[0], *c[1:]) for i, c in enumerate(cells)]
    items: list[LineItem] = []
    end_idx = len(lines)
    header_page = lines[header_idx].page
    for j in range(header_idx + 1, len(lines)):
        ln = lines[j]
        if (RX_TABLE_STOP.search(ln.text) or RX_TABLE_STOP_ANY.search(ln.text)
                or (ln.page != header_page and _is_header(ln))):
            end_idx = j
            break
        cols = _assign(ln.words, cells)
        txt = lambda k: " ".join(w.text for w in cols.get(k, [])).strip()  # noqa: E731
        qty_t, rate_t, amt_t = txt("qty"), txt("rate"), txt("amount")
        amount = parse_amount(amt_t) if amt_t else None
        if amount is None:
            # continuation of a multi-line description
            extra = txt("desc") or ln.text
            if items and extra and not re.search(r"\d{3,}", extra):
                items[-1].description = f"{items[-1].description} {extra}".strip()
                continue
            if items:
                end_idx = j
                break
            continue
        qty = parse_amount(qty_t) if qty_t else None
        rate = parse_amount(rate_t) if rate_t else None
        tax_t = txt("tax").replace("%", "")
        tax = parse_amount(tax_t) if tax_t else None
        uom = txt("uom") or None
        if qty is not None and uom is None and qty_t:
            m = re.match(r"^\s*([\d.,]+)\s*([A-Za-z]+)\s*$", qty_t)
            if m:
                qty, uom = parse_amount(m.group(1)), m.group(2)
        desc = txt("desc") or txt("other") or None
        code = txt("code") or None
        if not code and desc:
            m = re.search(r"\[([A-Z0-9\-]{3,})\]|\(([A-Z]{2,}-?\d{2,})\)", desc)
            if m:
                code = m.group(1) or m.group(2)
        conf = statistics.fmean(w.conf for w in ln.words) if ln.words else 0.5
        note = None
        if qty is None and rate is None:
            qty, rate, note = D("1"), amount, "Bundled line: no qty/rate printed; treated as 1 x amount (derived)."
        items.append(LineItem(
            line_no=len(items) + 1, description=desc, item_code=code, quantity=s(qty), uom=uom,
            unit_price=s(rate), tax_rate=s(tax), line_total=s(amount), confidence=round(conf, 3),
            status="EXTRACTED", source="ocr" if conf < 0.98 else "text_layer", note=note,
        ))
    return items, header_idx, end_idx


# ----------------------------------------------------------------------------- main

def extract_with_rules(doc: DocumentText, company_tax_ids: list[str] | None = None,
                       company_name: str | None = None) -> ExtractedInvoice:
    company_ids = {t.upper() for t in (company_tax_ids or [])}
    lines = doc.lines
    segs = [c for ln in lines for c in ln.clusters()]  # column segments for label:value fields
    full = doc.text
    src = "ocr" if doc.is_scanned else "text_layer"
    inv = ExtractedInvoice()
    f = inv.fields

    # --- vendor tax id (exclude our own) ---
    tax_hit = None
    for ln in segs:
        found = [(m.group(1), "GSTIN", 1.0) for m in GSTIN_RE.finditer(ln.text.upper())]
        if not found and doc.is_scanned:
            raws = [m.group(1) for m in GSTIN_LABELLED_RE.finditer(ln.text.upper())]
            raws += [m.group(1) for m in GSTIN_LOOSE_RE.finditer(ln.text.upper())]
            for raw in raws:
                fixed = repair_gstin(raw)
                if fixed:
                    found.append((fixed, "GSTIN", 0.9))
                    break
        found += [(m.group(1), "EU_VAT", 0.97) for m in EU_VAT_RE.finditer(ln.text.upper())]
        if re.search(r"\b(EIN|TAX\s*ID|TIN|FEIN)\b", ln.text, re.I):
            found += [(m.group(1), "US_EIN", 0.97) for m in US_EIN_RE.finditer(ln.text)]
        for val, kind, factor in found:
            if val in company_ids:
                continue
            note = None
            if kind == "GSTIN" and not gstin_is_valid(val):
                factor *= 0.6
                note = "GSTIN checksum does not validate."
            elif factor < 1 and kind == "GSTIN":
                note = "OCR characters corrected using GSTIN structure and checksum."
            tax_hit = _fv(val, val, ln.conf, ln.page, src, note, factor)
            if kind == "GSTIN" and gstin_is_valid(val):
                # a validating checksum is strong evidence on its own, whatever the OCR word confidence
                tax_hit.confidence = max(tax_hit.confidence, 0.85 if factor < 1 else 0.92)
            break
        if tax_hit:
            break
    f["vendor_tax_id"] = tax_hit or _missing("No vendor GSTIN / VAT ID / EIN found.")

    # --- vendor name ---
    hit = _first(segs, RX_VENDOR_LABEL)
    if hit:
        m, ln = hit
        f["vendor_name"] = _fv(m.group(1).strip(), m.group(1).strip(), ln.conf, ln.page, src, factor=0.95)
    else:
        name = None
        for ln in lines[:8]:
            seg = ln.clusters()[0]
            t = re.sub(r"(?i)\b(tax|commercial|proforma)?\s*invoice\b", "", seg.text).strip(" -:|")
            if (len(t) >= 3 and re.search(r"[A-Za-z]{3}", t)
                    and not re.search(r"bill|gstin|vat|page|original|copy|date|\d{4,}", t, re.I)
                    and not (company_name and company_name.lower() in t.lower())):
                name = (t, seg)
                break
        f["vendor_name"] = (_fv(name[0], name[0], name[1].conf, name[1].page, src,
                                "Taken from the document heading (no explicit label).", 0.8)
                            if name else _missing())

    # --- document type (invoice / credit note / debit note) and, for notes, the original invoice ---
    kind = None
    for ln in lines[:12]:
        mm = RX_NOTE_TITLE.search(ln.text)
        if mm:
            kind = (mm.group(1).upper() + "_NOTE", ln)
            break
    if kind:
        f["document_type"] = _fv(kind[0], kind[1].text.strip()[:40], kind[1].conf, kind[1].page, src)
    else:
        f["document_type"] = FieldValue(value="INVOICE", confidence=0.5, status="DERIVED", source="derived",
                                        note="No credit/debit note title found; treated as an invoice.")
    orig = _first(segs, RX_ORIGINAL_INVOICE, need_digit=True)
    if orig:
        mm, ln = orig
        raw = mm.group(1).strip().rstrip(".-/")
        f["original_invoice_number"] = _fv(raw.upper(), raw, ln.conf, ln.page, src)
    else:
        f["original_invoice_number"] = _missing("No reference to an original invoice." if kind else None)

    # --- invoice number (for a note: the note's own number) ---
    hit = _first(segs, RX_NOTE_NO, need_digit=True) if kind else None
    if not hit:
        hit = next(((mm, ln) for ln in segs for mm in RX_INVOICE_NO.finditer(ln.text)
                    if re.search(r"\d", mm.group(1)) and not RX_ORIGINAL_INVOICE.search(ln.text)), None)
    if hit:
        m, ln = hit
        raw = m.group(1).strip().rstrip(".-/")
        f["invoice_number"] = _fv(raw.upper(), raw, ln.conf, ln.page, src)
    else:
        f["invoice_number"] = _missing("No invoice number label found.")

    # --- currency ---
    m = RX_CURRENCY_LABEL.search(full)
    if m:
        f["currency"] = _fv(m.group(1).upper(), m.group(0), doc.pages[0].mean_conf, 1, src)
    else:
        cur, n = detect_currency(full)
        f["currency"] = (_fv(cur, cur, doc.pages[0].mean_conf, 1, src, f"Inferred from {n} currency mark(s).",
                             0.9 if n >= 2 else 0.75) if cur else _missing("No currency symbol or code found."))

    # Regional date convention: US vendors write MM/DD, Indian/EU vendors DD/MM. When the region is
    # evidenced by the tax ID or currency, a numeric date like 02/11/2026 is not ambiguous.
    tax_kind = classify_tax_id(tax_hit.value) if tax_hit else None
    if tax_kind == "US_EIN" or (tax_kind is None and f["currency"].value == "USD"):
        prefer, region = "MDY", "US convention (MM/DD)"
    elif tax_kind in ("GSTIN", "EU_VAT") or f["currency"].value in ("INR", "EUR"):
        prefer, region = "DMY", ("Indian" if tax_kind == "GSTIN" or f["currency"].value == "INR" else "European") + " convention (DD/MM)"
    else:
        prefer, region = "DMY", None

    # --- dates ---
    def date_field(rx, generic=None):
        h = _first(segs, rx)
        factor = 1.0
        if not h and generic is not None:
            h, factor = _first(segs, generic), 0.85
        if not h:
            return _missing()
        mm, ln = h
        d, ambiguous = parse_date(mm.group(1), prefer)
        if d is None:
            fv = _fv(None, mm.group(1).strip()[:40], ln.conf, ln.page, src, "Date text found but could not be parsed.")
            fv.status, fv.confidence = "UNCERTAIN", 0.3
            return fv
        if ambiguous and region:
            return _fv(d.isoformat(), mm.group(1).strip()[:40], ln.conf, ln.page, src,
                       f"Numeric date read as {region}, based on the vendor's region.", factor)
        note = f"Day/month order ambiguous; read as {prefer}." if ambiguous else None
        return _fv(d.isoformat(), mm.group(1).strip()[:40], ln.conf, ln.page, src, note, 0.7 if ambiguous else factor)

    f["invoice_date"] = date_field(RX_INVOICE_DATE, RX_DATE_GENERIC)
    f["due_date"] = date_field(RX_DUE_DATE)

    # --- PO reference ---
    po_hit = None
    for ln in segs:
        for mm in RX_PO.finditer(ln.text):
            val = mm.group(1).strip().rstrip(".-/")
            if not re.search(r"\d", val) or val.upper().startswith("BOX"):
                continue
            if f["invoice_number"].value and val.upper() == f["invoice_number"].value:
                continue
            po_hit = _fv(val.upper(), val, ln.conf, ln.page, src)
            break
        if po_hit:
            break
    f["po_number"] = po_hit or _missing("No PO reference on the invoice.")

    # --- line items ---
    items, header_idx, end_idx = _parse_items(lines)
    inv.line_items = items
    tail = lines[end_idx:] if end_idx is not None else lines

    # --- totals (search below the item table, bottom-up) ---
    def amount_field(rx, exclude=None, absolute=False):
        for ln in reversed(tail):
            if not rx.search(ln.text) or (exclude and exclude.search(ln.text)) or re.search(r"words", ln.text, re.I):
                continue
            after = ln.text[rx.search(ln.text).end():]
            v, raw = _last_amount(after)
            if v is None:
                continue
            if absolute:
                v = abs(v)
            return _fv(s(v), raw, ln.conf, ln.page, src)
        return None

    f["subtotal"] = amount_field(RX_SUBTOTAL) or _missing()
    f["taxable_amount"] = amount_field(RX_TAXABLE) or _missing()
    f["discount"] = amount_field(RX_DISCOUNT, absolute=True) or _missing()
    f["shipping"] = amount_field(RX_SHIPPING) or _missing()
    f["grand_total"] = (amount_field(RX_GRAND)
                        or amount_field(RX_TOTAL_PLAIN, exclude=re.compile(r"sub|tax|gst|vat|qty|quantity", re.I))
                        or _missing("No grand total found."))

    # --- tax components ---
    comps: list[TaxComponent] = []
    for ln in tail:
        t = ln.text
        if re.search(r"\b(ID|No\.?|Reg|Number|GSTIN)\b|words|total\s*tax|tax\s*total", t, re.I):
            continue
        m = RX_TAX_COMPONENT.search(t)
        if not m:
            continue
        v, _ = _last_amount(t[m.end():])
        if v is None:
            continue
        kind = re.sub(r"\s+", "_", m.group(1).upper())
        rate = m.group(2)
        comps.append(TaxComponent(kind="SALES_TAX" if kind == "SALES_TAX" else kind, rate=rate, amount=s(v)))
    inv.tax_components = comps
    tax_total = amount_field(RX_TAX_TOTAL)
    if tax_total:
        f["tax_amount"] = tax_total
    elif comps:
        total = sum((D(c.amount) for c in comps), D("0"))
        f["tax_amount"] = FieldValue(value=s(total), raw=" + ".join(c.amount for c in comps), confidence=0.9,
                                     status="DERIVED", source="derived",
                                     note="Sum of printed tax components (no total-tax line).")
    else:
        f["tax_amount"] = _missing()

    # --- payment details and e-invoice reference ---
    def simple(rx, clean):
        h = _first(segs, rx)
        if not h:
            return _missing()
        mm, ln = h
        return _fv(clean(mm.group(1)), mm.group(1).strip(), ln.conf, ln.page, src)

    nospace = lambda v: re.sub(r"[\s\-]", "", v).upper()  # noqa: E731
    f["bank_account"] = simple(RX_IBAN, nospace)
    if f["bank_account"].value is None:
        f["bank_account"] = simple(RX_BANK_ACCOUNT, nospace)
    f["bank_ifsc"] = simple(RX_IFSC, nospace)
    irn = None
    for ln in lines:  # the IRN is a long token; read it from the full line, not a column segment
        mm = RX_IRN.search(ln.text.replace(" ", "")) or RX_IRN.search(ln.text)
        if mm:
            irn = _fv(mm.group(1).lower(), mm.group(1), ln.conf, ln.page, src)
            break
    f["irn"] = irn or _missing()

    incl = detect_tax_inclusive(full)
    inv.meta = {
        "extractor": "rules", "is_scanned": doc.is_scanned, "pages": doc.page_count,
        "ocr_mean_conf": round(doc.ocr_mean_conf, 3) if doc.ocr_mean_conf is not None else None,
        "date_order": prefer, "table_header_found": header_idx is not None,
        "tax_inclusive": incl is not None, "tax_inclusive_evidence": incl,
    }
    return inv
