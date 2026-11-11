"""Low-level parsers: dates, tax identifiers, OCR-confusion normalisation."""
from __future__ import annotations

import re
from datetime import date

GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
# Loose pattern tolerating OCR confusions (O/0, I/1, S/5 ...) — normalised afterwards
GSTIN_LOOSE_RE = re.compile(r"\b([0-9OIlS]{2}[A-Z0-9]{5}[0-9OIlSB]{4}[A-Z0-9][0-9A-Z]{1}[Z2][0-9A-Z])\b")
EU_VAT_RE = re.compile(r"\b((?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK)(?=[A-Z0-9]*\d{6})[A-Z0-9]{8,12})\b")
US_EIN_RE = re.compile(r"\b(\d{2}-\d{7})\b")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Characters OCR commonly confuses, mapped to a canonical form.
OCR_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0", "I": "1", "l": "1", "i": "1", "|": "1",
                                "S": "5", "s": "5", "B": "8", "Z": "2", "G": "6"})


def gstin_checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        val = GSTIN_CHARS.index(ch)
        prod = val * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_is_valid(gstin: str | None) -> bool:
    if not gstin or not GSTIN_RE.fullmatch(gstin):
        return False
    return gstin_checksum_char(gstin[:14]) == gstin[14]


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + gstin_checksum_char(base)


def classify_tax_id(tax_id: str | None) -> str | None:
    if not tax_id:
        return None
    t = tax_id.strip().upper()
    if GSTIN_RE.fullmatch(t):
        return "GSTIN"
    if EU_VAT_RE.fullmatch(t):
        return "EU_VAT"
    if US_EIN_RE.fullmatch(t):
        return "US_EIN"
    return "OTHER"


def tax_id_format_valid(tax_id: str | None) -> bool:
    kind = classify_tax_id(tax_id)
    if kind == "GSTIN":
        return gstin_is_valid(tax_id.strip().upper())
    return kind in ("EU_VAT", "US_EIN")


def normalize_tax_id(tax_id: str | None) -> str | None:
    if not tax_id:
        return None
    return re.sub(r"[\s.]", "", tax_id).upper()


def _repair15(c: str) -> str | None:
    if gstin_is_valid(c):
        return c
    digit_pos = {0, 1, 7, 8, 9, 10}
    alpha_pos = {2, 3, 4, 5, 6, 11}
    to_digit = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6", "Q": "0", "D": "0"})
    to_alpha = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"})
    chars = list(c)
    for i in range(15):
        if i in digit_pos:
            chars[i] = chars[i].translate(to_digit)
        elif i in alpha_pos:
            chars[i] = chars[i].translate(to_alpha)
    chars[13] = "Z"
    fixed = "".join(chars)
    return fixed if gstin_is_valid(fixed) else None


def repair_gstin(candidate: str) -> str | None:
    """Fix OCR confusions in a GSTIN using its fixed structure and checksum.

    Handles character confusions (O/0, I/1, S/5...) and one inserted or dropped-in
    character (OCR reading '1Z' as '12Z'). Only returns a value whose checksum validates.
    """
    c = re.sub(r"[^A-Z0-9]", "", candidate.upper())
    if len(c) == 15:
        return _repair15(c)
    if len(c) == 16:
        fixes = {f for i in range(16) if (f := _repair15(c[:i] + c[i + 1:]))}
        return fixes.pop() if len(fixes) == 1 else None
    return None


GSTIN_LABELLED_RE = re.compile(r"\bGST\s*IN\s*(?:NO\.?)?\s*[:#\-]?\s*([A-Z0-9]{14,17})\b", re.I)


def ocr_normalize(text: str | None) -> str:
    """Canonical form for comparing identifiers that may contain OCR errors."""
    if not text:
        return ""
    t = re.sub(r"[\s\-_/.#:]", "", text)
    return t.translate(OCR_CONFUSIONS).upper()


def normalize_ref(text: str | None) -> str:
    """Normalise invoice/PO numbers for exact comparison (no OCR mapping)."""
    if not text:
        return ""
    return re.sub(r"[\s\-_/.#:]", "", text).upper()


DATE_PATTERNS = [
    # 2026-03-14
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "YMD"),
    # 14/03/2026, 03-14-2026, 14.03.26
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b"), "XXY"),
    # 14 Mar 2026, 14-Mar-2026, 14th March, 2026
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})[\s\-,]+(\d{2,4})\b"), "DMonY"),
    # March 14, 2026
    (re.compile(r"\b([A-Za-z]{3,9})[\s\-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-]+(\d{4})\b"), "MonDY"),
]


def _year(y: str) -> int:
    v = int(y)
    return v + 2000 if v < 100 else v


def parse_date(text: str | None, prefer: str = "DMY") -> tuple[date | None, bool]:
    """Parse a date. Returns (date, ambiguous). `prefer` = DMY (India/EU) or MDY (US)."""
    if not text:
        return None, False
    for rx, kind in DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if kind == "YMD":
                return date(int(a), int(b), int(c)), False
            if kind == "XXY":
                x, y, yr = int(a), int(b), _year(c)
                ambiguous = x <= 12 and y <= 12 and x != y
                if x > 12:
                    return date(yr, y, x), False
                if y > 12:
                    return date(yr, x, y), False
                if prefer == "MDY":
                    return date(yr, x, y), ambiguous
                return date(yr, y, x), ambiguous
            if kind == "DMonY":
                mon = MONTHS.get(b[:3].lower())
                if mon:
                    return date(_year(c), mon, int(a)), False
            if kind == "MonDY":
                mon = MONTHS.get(a[:3].lower())
                if mon:
                    return date(int(c), mon, int(b)), False
        except ValueError:
            return None, False
    return None, False


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
