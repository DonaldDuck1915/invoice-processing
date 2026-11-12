"""Decimal-only money helpers. Floats are never used for financial values."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")
CENT = Decimal("0.01")

CURRENCY_SYMBOLS = {
    "₹": "INR", "RS.": "INR", "RS": "INR", "INR": "INR", "RUPEES": "INR",
    "€": "EUR", "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "$": "USD", "USD": "USD", "US$": "USD", "DOLLARS": "USD",
    "£": "GBP", "GBP": "GBP", "AED": "AED", "SGD": "SGD", "AUD": "AUD", "CAD": "CAD", "CHF": "CHF",
    "JPY": "JPY", "CNY": "CNY", "SAR": "SAR", "HKD": "HKD", "NZD": "NZD",
}
# Built in; more currencies are enabled by giving them an approval limit on the Rules & data page.
SUPPORTED_CURRENCIES = {"INR", "EUR", "USD"}
SYMBOLS = {"INR": "₹", "EUR": "€", "USD": "$", "GBP": "£"}


def supported_currencies(rules: dict | None = None) -> set[str]:
    """Built-in currencies plus every currency that has an approval limit configured."""
    limits = ((rules or {}).get("approval") or {}).get("auto_approve_limit") or {}
    return SUPPORTED_CURRENCIES | {c for c, v in limits.items() if v is not None}


def D(value) -> Decimal | None:
    """Coerce to Decimal; None/'' stay None. Never goes through float for str input."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def q2(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def s(value: Decimal | None) -> str | None:
    """Serialise a Decimal for JSON (string keeps exactness)."""
    if value is None:
        return None
    return format(value, "f")


def pct_diff(expected: Decimal | None, actual: Decimal | None) -> Decimal | None:
    if expected is None or actual is None or expected == 0:
        return None
    return ((actual - expected) / expected * 100).quantize(CENT, rounding=ROUND_HALF_UP)


_AMOUNT_RE = re.compile(r"[-(]?\d[\d,.\s']*\d(?:\)?)|[-(]?\d")


def parse_amount(text: str | None) -> Decimal | None:
    """Parse '5,00,000.00', '1,234.56', '1.234,56', '€ 99', '(120.00)', 'Rs. 4500/-'.

    Returns None if no unambiguous number is present.
    """
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    t = re.sub(r"(?i)(inr|rs\.?|usd|us\$|eur|€|₹|\$|/-)", " ", t).strip()
    negative = t.startswith("(") and t.endswith(")") or t.startswith("-")
    m = _AMOUNT_RE.search(t)
    if not m:
        return None
    num = re.sub(r"[\s'()\-]", "", m.group(0))
    if not num:
        return None
    has_dot, has_comma = "." in num, "," in num
    if has_dot and has_comma:
        # whichever separator comes last is the decimal separator
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif has_comma:
        parts = num.split(",")
        # '1234,56' (European decimals) vs '1,234' / '5,00,000' (grouping)
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            num = num.replace(",", ".")
        else:
            num = num.replace(",", "")
    elif has_dot and num.count(".") > 1:
        # '1.234.567' European grouping
        head, _, tail = num.rpartition(".")
        num = head.replace(".", "") + ("." + tail if len(tail) in (1, 2) else tail)
    try:
        value = Decimal(num)
    except InvalidOperation:
        return None
    return -value if negative else value


def detect_currency(text: str) -> tuple[str | None, int]:
    """Return (currency, evidence_count) from free text."""
    counts: dict[str, int] = {}
    up = text.upper()
    for token, code in CURRENCY_SYMBOLS.items():
        if token in ("$",):
            n = up.count("$") - up.count("US$")
        elif len(token) <= 3 and token.isalpha():
            n = len(re.findall(rf"\b{re.escape(token)}\b", up))
        else:
            n = up.count(token)
        if n:
            counts[code] = counts.get(code, 0) + n
    if not counts:
        return None, 0
    best = max(counts, key=counts.get)
    return best, counts[best]


def fmt_money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "—"
    v = q2(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    whole, frac = f"{v:.2f}".split(".")
    if currency == "INR":
        # Indian grouping: 5,00,000.00
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
            whole = f"{head},{tail}"
    else:
        whole = f"{int(whole):,}"
    sym = SYMBOLS.get(currency or "", f"{currency} " if currency else "")
    return f"{sign}{sym}{whole}.{frac}"
