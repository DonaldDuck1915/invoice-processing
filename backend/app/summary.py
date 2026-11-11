"""AI-written plain-language summary of a decision that has already been made by the rule engine.

It is labelled as AI output, never changes the decision, and is skipped silently when AI is unavailable.
"""
from __future__ import annotations

import json

from .llm import AI_DISCLAIMER, LLMClient, LLMUnavailable

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
          "additionalProperties": False}

SYSTEM = """You write a short plain-language summary of an invoice-processing result for an accounts-payable clerk.
The decision has ALREADY been made by deterministic business rules. Do not change it, question it, or add new findings.
Explain in 3-5 sentences: what the invoice is, what it was matched to, the decision, and the main reason(s) with the
key numbers exactly as given. If action is needed, end with the recommended next step. No headings, no bullet points."""


def ai_summary(llm: LLMClient, facts: dict) -> dict | None:
    if not llm.available:
        return None
    try:
        data = llm.json_call("decision_summary", SYSTEM,
                             [{"type": "text", "text": json.dumps(facts, default=str, indent=1)}], SCHEMA,
                             effort="low", max_tokens=4000)
    except LLMUnavailable as e:
        return {"text": None, "error": str(e), "label": "AI summary", "disclaimer": AI_DISCLAIMER}
    return {"text": data["summary"].strip(), "label": "AI summary", "disclaimer": AI_DISCLAIMER,
            "model": llm.settings.llm_model}
