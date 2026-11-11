"""Description-meaning signal.

The AI sees ONLY descriptions (never amounts, so the amount signal is not double-counted) and
returns 0..1 scores with a one-line reason. Lexical similarity (rapidfuzz) is always computed
in code as an independent check. When the AI is unavailable the signal is marked unavailable
and matching becomes stricter, never more lenient.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..llm import LLMClient, LLMUnavailable

STOP = {"the", "and", "with", "for", "of", "as", "per", "a", "an", "to", "nos", "pcs", "-", "&"}


def lexical(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    ta = " ".join(w for w in re.findall(r"[a-z0-9]+", a.lower()) if w not in STOP)
    tb = " ".join(w for w in re.findall(r"[a-z0-9]+", b.lower()) if w not in STOP)
    if not ta or not tb:
        return 0.0
    return round((fuzz.token_set_ratio(ta, tb) * 0.5 + fuzz.token_sort_ratio(ta, tb) * 0.5) / 100, 3)


_NUM = {"type": "number"}
SCHEMA = {
    "type": "object",
    "properties": {
        "po_scores": {"type": "array", "items": {
            "type": "object", "properties": {"po_number": {"type": "string"}, "score": _NUM, "reason": {"type": "string"}},
            "required": ["po_number", "score", "reason"], "additionalProperties": False}},
        "line_scores": {"type": "array", "items": {
            "type": "object", "properties": {"invoice_line": {"type": "integer"}, "po_number": {"type": "string"},
                                             "po_line": {"type": "integer"}, "score": _NUM},
            "required": ["invoice_line", "po_number", "po_line", "score"], "additionalProperties": False}},
    },
    "required": ["po_scores", "line_scores"],
    "additionalProperties": False,
}

SYSTEM = """You compare what an invoice bills for with what purchase orders (POs) ordered, judging MEANING only.
You are given descriptions only - no amounts, quantities or prices. Do not guess about amounts.

Score 0.0-1.0:
- 1.0: clearly the same goods/services (synonyms, abbreviations, model numbers all count).
- 0.6-0.8: same kind of item, details differ or are vague.
- 0.3-0.5: same broad category only (e.g. "office supplies" vs "office furniture").
- 0.0-0.2: unrelated.
A vague invoice description must NOT score high against several POs; vagueness means moderate scores.
po_scores: one entry per PO, how well the invoice as a whole matches that PO, with a one-sentence reason quoting the
key words that drove the score. line_scores: one entry for every (invoice line, PO line) pair."""


class SemanticScorer:
    def __init__(self, llm: LLMClient | None):
        self.llm = llm

    @property
    def available(self) -> bool:
        return bool(self.llm and self.llm.available)

    def score(self, invoice_lines: list[dict], candidates: list[dict]) -> dict:
        """invoice_lines: [{line_no, description}], candidates: [{po_number, description, lines:[{line_no, description}]}]"""
        out = {"available": False, "error": None, "po": {}, "pairs": {}}
        if not self.available:
            out["error"] = "AI not configured"
            return out
        if not invoice_lines or not candidates:
            out["error"] = "nothing to compare"
            return out
        text = ["INVOICE LINES:"] + [f"  line {li['line_no']}: {li['description']}" for li in invoice_lines]
        text.append("\nCANDIDATE POs:")
        for po in candidates:
            text.append(f"  {po['po_number']} - {po.get('description') or ''}")
            text += [f"    line {ln['line_no']}: {ln['description']}" for ln in po["lines"]]
        try:
            data = self.llm.json_call("description_meaning", SYSTEM, [{"type": "text", "text": "\n".join(text)}],
                                      SCHEMA, effort="low", max_tokens=8000)
        except LLMUnavailable as e:
            out["error"] = str(e)
            return out
        valid = {po["po_number"] for po in candidates}
        for p in data.get("po_scores", []):
            if p["po_number"] in valid:
                out["po"][p["po_number"]] = {"score": max(0.0, min(1.0, float(p["score"]))), "reason": p["reason"]}
        for ls in data.get("line_scores", []):
            if ls["po_number"] in valid:
                out["pairs"][f"{ls['invoice_line']}|{ls['po_number']}|{ls['po_line']}"] = max(0.0, min(1.0, float(ls["score"])))
        out["available"] = bool(out["po"])
        if not out["available"]:
            out["error"] = "AI returned no usable scores"
        return out
