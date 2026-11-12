"""AI review assistant: a plain-language summary plus a recommended next action for the reviewer.

One AI call per run produces both. The decision itself has already been made by the rule engine;
the AI only explains it and suggests how a reviewer could resolve it. Guardrails in code:
- the recommendation is advisory and labelled as AI output; nothing is applied automatically,
- a recommended action must come from a fixed list, a suggested PO must be one of the vendor's
  candidate POs, and "approve" is never offered for an invoice the rules rejected,
- if the AI is unavailable the invoice is processed exactly the same, without the box.
"""
from __future__ import annotations

import json

from .llm import AI_DISCLAIMER, LLMClient, LLMUnavailable

ACTIONS = {
    "APPROVE": "Approve",
    "REJECT": "Reject",
    "CONFIRM_PO": "Confirm the purchase order",
    "CORRECT_FIELDS": "Correct the extracted fields",
    "REQUEST_INFO_FROM_VENDOR": "Ask the vendor for information",
    "RESOLVE_DUPLICATE": "Resolve the possible duplicate",
    "ESCALATE": "Escalate (procurement / vendor management)",
}
_NULLABLE = {"anyOf": [{"type": "string"}, {"type": "null"}]}

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommendation": {
            "anyOf": [{"type": "null"}, {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "rationale": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "suggested_po": _NULLABLE,
                    "vendor_message": _NULLABLE,
                },
                "required": ["action", "confidence", "rationale", "steps", "suggested_po", "vendor_message"],
                "additionalProperties": False,
            }],
        },
    },
    "required": ["summary", "recommendation"],
    "additionalProperties": False,
}

SYSTEM = """You assist an accounts-payable reviewer. You receive the result of an invoice that was already processed
by deterministic business rules (decision, reasons, checks, PO candidates, variances, duplicate check).

1) summary: 3-5 plain sentences - what the invoice is, what it was matched to, the decision, and the main reason(s)
   with the key numbers exactly as given. Do not change or question the decision. No headings or bullet points.

2) recommendation: when the decision is MANUAL_REVIEW or REJECTED, recommend the single most sensible next action
   for the reviewer; when the decision is APPROVED, return null.
   - action: one of APPROVE, REJECT, CONFIRM_PO, CORRECT_FIELDS, REQUEST_INFO_FROM_VENDOR, RESOLVE_DUPLICATE, ESCALATE.
     Never recommend APPROVE for a REJECTED invoice.
   - confidence: low / medium / high - how clearly the facts support the action. Use low when facts are missing.
   - rationale: 1-3 sentences grounded ONLY in the facts given. Never invent numbers, POs, dates or policies.
   - steps: 2-4 short, concrete steps the reviewer should take in this app or with the vendor.
   - suggested_po: a PO number only if action is CONFIRM_PO and it is one of the listed candidates, else null.
   - vendor_message: if the vendor must be contacted (REQUEST_INFO_FROM_VENDOR, REJECT, or a duplicate), a short,
     polite e-mail body to the vendor quoting the invoice number and the specific issue; else null."""


HIGH_RISK = {"BANK_DETAILS_CHANGED", "EINVOICE_MISMATCH", "SPLIT_BELOW_LIMIT"}


def _guard(rec: dict | None, decision: str, candidates: list[str], facts: dict | None = None) -> dict | None:
    """Enforce the rules the prompt asks for - the model's output is never trusted blindly."""
    if not rec or decision == "APPROVED" or rec.get("action") not in ACTIONS:
        return None
    rec = dict(rec)
    warnings = []
    if decision == "REJECTED" and rec["action"] == "APPROVE":
        rec["action"] = "ESCALATE"
        warnings.append("The AI suggested approving a rejected invoice; changed to escalate.")
    risky = {f.get("code") for f in (facts or {}).get("control_findings") or []} & HIGH_RISK
    if rec["action"] == "APPROVE" and risky:
        rec["action"] = "ESCALATE"
        warnings.append(f"The AI suggested approving despite {', '.join(sorted(risky)).lower().replace('_', ' ')}; "
                        "changed to escalate.")
    if rec["action"] == "APPROVE" and ((facts or {}).get("approval") or {}).get("level") == "MANAGER":
        warnings.append("Above the auto-approval limit: only a manager can approve.")
    po = (rec.get("suggested_po") or "").strip().upper() or None
    if po and po not in candidates:
        warnings.append(f"The AI suggested {po}, which is not one of this vendor's candidate POs; ignored.")
        po = None
    rec["suggested_po"] = po if rec["action"] == "CONFIRM_PO" else None
    if rec["action"] == "CONFIRM_PO" and not rec["suggested_po"]:
        rec["action"] = "ESCALATE" if not candidates else rec["action"]
    rec["steps"] = [s.strip() for s in rec.get("steps") or [] if s and s.strip()][:4]
    rec["vendor_message"] = (rec.get("vendor_message") or "").strip() or None
    rec["action_label"] = ACTIONS[rec["action"]]
    rec["guard_warnings"] = warnings
    return rec


def ai_summary(llm: LLMClient, facts: dict) -> dict | None:
    """Returns {"text", "recommendation", ...} for run.ai_summary, or None when AI is not configured."""
    if not llm.available:
        return None
    try:
        data = llm.json_call("decision_summary", SYSTEM,
                             [{"type": "text", "text": json.dumps(facts, default=str, indent=1)}], SCHEMA,
                             effort="low", max_tokens=4000)
    except LLMUnavailable as e:
        return {"text": None, "recommendation": None, "error": str(e), "label": "AI summary",
                "disclaimer": AI_DISCLAIMER}
    candidates = [c["po_number"] for c in facts.get("po_candidates") or []]
    return {"text": (data.get("summary") or "").strip() or None, "label": "AI summary", "disclaimer": AI_DISCLAIMER,
            "recommendation": _guard(data.get("recommendation"), facts.get("decision"), candidates, facts),
            "model": llm.model_for("decision_summary"), "provider": llm.provider}
