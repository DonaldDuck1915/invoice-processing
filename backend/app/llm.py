"""AI layer (OpenAI) used for extraction, description meaning, the summary and the review recommendation.

Everything goes through one `json_call` that returns JSON matching a strict schema. The model never
computes amounts or decides outcomes. Every call is logged (purpose, model, latency, tokens, cost,
outcome) so it can be shown in the audit trail and on the dashboard.

Content blocks: {"type": "text", "text": ...} and {"type": "pdf", "data": bytes}.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

AI_DISCLAIMER = "AI and can make mistakes. Please double-check responses."


class LLMUnavailable(Exception):
    pass


def pdf_block(data: bytes) -> dict:
    return {"type": "pdf", "data": data}


def _cache_key(purpose: str, system: str, blocks: list[dict], schema: dict) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([purpose, system, schema], sort_keys=True, default=str).encode())
    for b in blocks:
        if b["type"] == "pdf":
            h.update(hashlib.sha256(b["data"]).digest())
        else:
            h.update(b["text"].encode())
    return h.hexdigest()


class LLMClient:
    # Short reasoning over facts (meaning of descriptions, summary + recommendation) can use a
    # cheaper / faster model than reading the PDF, where accuracy matters most.
    FAST_PURPOSES = {"decision_summary", "description_meaning"}

    def __init__(self):
        self.settings = get_settings()
        self.calls: list[dict[str, Any]] = []
        self._cache: dict[str, Any] = {}
        self._openai = None
        self._down: dict[str, str] = {}  # model -> why it was unavailable in this run

    # ------------------------------------------------------------------ status
    @property
    def provider(self) -> str | None:
        return self.settings.ai_provider

    @property
    def available(self) -> bool:
        return self.provider is not None

    @property
    def model_name(self) -> str | None:
        return self.settings.ai_model

    def model_for(self, purpose: str) -> str:
        st = self.settings
        return st.openai_fast_model if purpose in self.FAST_PURPOSES and st.openai_fast_model else st.openai_model

    def models_for(self, purpose: str) -> list[str]:
        """Preferred model first, then one fallback for when the first is overloaded or times out."""
        first = self.model_for(purpose)
        st = self.settings
        others = [m for m in dict.fromkeys(x for x in (st.openai_fallback_model, st.openai_model,
                                                       st.openai_fast_model) if x) if m != first]
        return [first] + others[:1]

    @staticmethod
    def _transient(error: str) -> bool:
        """Overloaded / timed out / unreachable - worth trying another model, never worth waiting again."""
        return error.startswith("Connection") or error.startswith("API error 5")

    # ------------------------------------------------------------------ public
    def json_call(self, purpose: str, system: str, user_content: list[dict], schema: dict,
                  effort: str = "medium", max_tokens: int = 16000) -> dict:
        """Ask the model for JSON matching `schema`. Raises LLMUnavailable on any failure.

        A model that is overloaded or times out is skipped for the rest of this run (an invoice never
        waits for the same slow model twice) and the fallback model, if any, is tried instead; when no
        model is available the caller continues rules-only."""
        if not self.available:
            raise LLMUnavailable("AI is not configured (set OPENAI_API_KEY).")
        key = _cache_key(purpose, system, user_content, schema)
        if key in self._cache:
            self.calls.append({"purpose": purpose, "cached": True, "ok": True, "provider": self.provider})
            return self._cache[key]
        last: LLMUnavailable | None = None
        for model in self.models_for(purpose):
            if model in self._down:
                self.calls.append({"purpose": purpose, "provider": self.provider, "model": model, "ok": False,
                                   "error": f"skipped - {self._down[model]} earlier in this run", "latency_ms": 0})
                last = LLMUnavailable(f"{model} was unavailable earlier in this run ({self._down[model]}).")
                continue
            try:
                return self._attempt(purpose, model, system, user_content, schema, effort, max_tokens, key)
            except LLMUnavailable as e:
                last = e
                err = str(self.calls[-1].get("error") or e)
                if not self._transient(err):
                    raise  # bad key, quota, refusal, malformed output: another model will not help
                self._down[model] = err
        raise last or LLMUnavailable("No AI model is available.")

    def _attempt(self, purpose: str, model: str, system: str, user_content: list[dict], schema: dict,
                 effort: str, max_tokens: int, key: str) -> dict:
        t0 = time.time()
        entry: dict[str, Any] = {"purpose": purpose, "provider": self.provider, "model": model, "ok": False}
        try:
            text = self._call_openai(system, user_content, schema, max_tokens, entry, effort)
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                entry["error"] = "Invalid JSON"
                raise LLMUnavailable("The AI returned malformed JSON.") from e
            entry["ok"] = True
            self._cache[key] = data
            return data
        except LLMUnavailable as e:
            entry.setdefault("error", str(e))
            raise
        finally:
            entry["latency_ms"] = int((time.time() - t0) * 1000)
            self.calls.append(entry)

    # ------------------------------------------------------------------ OpenAI
    def _openai_client(self):
        if self._openai is None:
            import openai

            # no SDK retries: an overloaded model is handled by the fallback model instead of waiting again
            self._openai = openai.OpenAI(api_key=self.settings.openai_api_key, base_url=self.settings.openai_base_url,
                                         timeout=self.settings.llm_timeout_seconds, max_retries=0)
        return self._openai

    def _call_openai(self, system: str, blocks: list[dict], schema: dict, max_tokens: int, entry: dict,
                     effort: str = "medium") -> str:
        """Responses API with strict JSON-schema output; the PDF is sent inline as a file part."""
        import openai

        st = self.settings
        content = []
        for b in blocks:
            if b["type"] == "pdf":
                part = {"type": "input_file", "filename": "invoice.pdf",
                        "file_data": "data:application/pdf;base64," + base64.b64encode(b["data"]).decode()}
                if st.openai_pdf_detail in ("low", "high"):
                    part["detail"] = st.openai_pdf_detail
                content.append(part)
            else:
                content.append({"type": "input_text", "text": b["text"]})
        model = entry.get("model") or st.openai_model
        level = (st.openai_reasoning or "auto").strip().lower()
        if level == "auto":  # low for most calls, a little more for reading the PDF
            level = {"low": "low", "medium": "low", "high": "medium"}.get(effort, "low")
        params: dict[str, Any] = dict(
            model=model, instructions=system, input=[{"role": "user", "content": content}],
            text={"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": True}},
            # reasoning tokens count towards the output limit, so leave headroom
            max_output_tokens=max(max_tokens, 16000), store=False)
        if level != "default":
            params["reasoning"] = {"effort": level}
        client = self._openai_client()
        try:
            try:
                resp = client.responses.create(**params)
            except openai.BadRequestError as e:
                if "reasoning" in params and "reasoning" in str(e).lower():
                    log.info("model %s rejected reasoning effort %s; retrying with its default", model, level)
                    params.pop("reasoning")
                    resp = client.responses.create(**params)
                else:
                    raise
            entry["reasoning"] = params.get("reasoning", {}).get("effort", "default")
        except openai.AuthenticationError as e:
            entry["error"] = "Invalid API key"
            raise LLMUnavailable("OpenAI rejected the API key - check OPENAI_API_KEY.") from e
        except openai.PermissionDeniedError as e:
            entry["error"] = "API error 403"
            raise LLMUnavailable(f"This OpenAI key has no access to model '{model}'.") from e
        except openai.NotFoundError as e:
            entry["error"] = "API error 404"
            raise LLMUnavailable(f"OpenAI model '{model}' was not found - check OPENAI_MODEL.") from e
        except openai.RateLimitError as e:
            quota = "quota" in str(e).lower() or "insufficient" in str(e).lower()
            entry["error"] = "Quota exhausted" if quota else "Rate limited"
            raise LLMUnavailable("OpenAI credits / quota exhausted - check billing." if quota
                                 else "OpenAI rate limit reached.") from e
        except openai.APIStatusError as e:
            entry["error"] = f"API error {e.status_code}"
            raise LLMUnavailable(f"OpenAI service error ({e.status_code}).") from e
        except openai.APIConnectionError as e:  # includes timeouts
            entry["error"] = "Connection error / timeout"
            raise LLMUnavailable("Could not reach the OpenAI service.") from e

        entry["served_by"] = getattr(resp, "model", None)
        u = getattr(resp, "usage", None)
        if u is not None:
            cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
            entry["input_tokens"] = u.input_tokens
            entry["output_tokens"] = u.output_tokens
            entry["cached_input_tokens"] = cached
            entry["reasoning_tokens"] = getattr(getattr(u, "output_tokens_details", None), "reasoning_tokens", None)
            entry["cost_usd"] = round(((u.input_tokens - cached) * st.openai_price_input_per_m
                                       + cached * st.openai_price_cached_input_per_m
                                       + u.output_tokens * st.openai_price_output_per_m) / 1_000_000, 6)
        if getattr(resp, "status", None) == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
            if reason == "content_filter":
                raise LLMUnavailable("OpenAI declined to answer (content filter).")
            raise LLMUnavailable("The AI response was cut off (max output tokens).")
        for item in getattr(resp, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise LLMUnavailable("The AI model declined the request.")
        text = resp.output_text
        if not text:
            raise LLMUnavailable("The AI returned no text.")
        return text
