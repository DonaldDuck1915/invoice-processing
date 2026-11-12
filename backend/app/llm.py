"""AI layer used for extraction, description meaning and summaries.

Two interchangeable providers - Google Gemini and Anthropic Claude - behind one `json_call`.
The model never computes amounts or decides outcomes. Every call is logged (purpose, provider,
model, latency, token usage, outcome) so it can be shown in the audit trail.

Content blocks are provider-neutral: {"type": "text", "text": ...} and {"type": "pdf", "data": bytes}.
"""
from __future__ import annotations

import base64
import copy
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


def gemini_schema(schema: Any) -> Any:
    """Gemini accepts JSON Schema but not every keyword; drop `additionalProperties` (it only
    restates 'no extra keys', which `properties` + `required` already imply)."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


class LLMClient:
    def __init__(self):
        self.settings = get_settings()
        self.calls: list[dict[str, Any]] = []
        self._cache: dict[str, Any] = {}
        self._anthropic = None
        self._gemini = None
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

    # Short, high-volume reasoning (meaning of descriptions, summary + recommendation) can use a
    # cheaper / faster model than reading the PDF, where accuracy matters most.
    FAST_PURPOSES = {"decision_summary", "description_meaning"}

    def model_for(self, purpose: str) -> str | None:
        if self.provider == "gemini":
            if purpose in self.FAST_PURPOSES and self.settings.gemini_fast_model:
                return self.settings.gemini_fast_model
            return self.settings.gemini_model
        return self.model_name

    def models_for(self, purpose: str) -> list[str | None]:
        """Preferred model first, then the fallback (Gemini only) for when the first is overloaded."""
        first = self.model_for(purpose)
        if self.provider != "gemini":
            return [first]
        fallbacks = [m for m in (self.settings.gemini_fallback_model, self.settings.gemini_model,
                                 self.settings.gemini_fast_model) if m]
        return [first] + [m for m in dict.fromkeys(fallbacks) if m != first][:1]

    @staticmethod
    def _transient(error: str) -> bool:
        """Overloaded / timed out / unreachable - worth trying another model, never worth waiting again."""
        return error.startswith("Connection") or error.startswith("API error 5")

    # ------------------------------------------------------------------ public
    def json_call(self, purpose: str, system: str, user_content: list[dict], schema: dict,
                  effort: str = "medium", max_tokens: int = 16000) -> dict:
        """Ask the model for JSON matching `schema`. Raises LLMUnavailable on any failure.

        A model that is overloaded or times out is skipped for the rest of this run (an invoice never
        waits for the same slow model twice) and the fallback model is tried instead; when every model
        is unavailable the caller continues rules-only."""
        if not self.available:
            raise LLMUnavailable("AI is not configured (set GEMINI_API_KEY or ANTHROPIC_API_KEY).")
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

    def _attempt(self, purpose: str, model: str | None, system: str, user_content: list[dict], schema: dict,
                 effort: str, max_tokens: int, key: str) -> dict:
        t0 = time.time()
        entry: dict[str, Any] = {"purpose": purpose, "provider": self.provider, "model": model, "ok": False}
        try:
            if self.provider == "gemini":
                text = self._call_gemini(system, user_content, schema, max_tokens, entry, effort)
            else:
                text = self._call_anthropic(system, user_content, schema, effort, max_tokens, entry)
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

    # ------------------------------------------------------------------ Gemini
    def _gemini_client(self):
        if self._gemini is None:
            from google import genai
            from google.genai import types

            self._gemini = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_seconds * 1000)))
        return self._gemini

    def _call_gemini(self, system: str, blocks: list[dict], schema: dict, max_tokens: int, entry: dict,
                     effort: str = "medium") -> str:
        from google.genai import errors, types

        parts = []
        for b in blocks:
            if b["type"] == "pdf":
                parts.append(types.Part.from_bytes(data=b["data"], mime_type="application/pdf"))
            else:
                parts.append(types.Part.from_text(text=b["text"]))
        model = entry.get("model") or self.settings.gemini_model
        level = (self.settings.gemini_thinking or "auto").strip().lower()
        if level == "auto":  # follow the caller: extraction reasons more than a summary does
            level = {"low": "low", "medium": "low", "high": "medium"}.get(effort, "low")

        def config(with_thinking: bool):
            return types.GenerateContentConfig(
                system_instruction=system, temperature=0.0, response_mime_type="application/json",
                response_json_schema=gemini_schema(schema),
                # thinking tokens count towards the output limit, so leave generous headroom
                max_output_tokens=max(max_tokens, 16000),
                thinking_config=types.ThinkingConfig(thinking_level=level.upper())
                if with_thinking and level != "default" else None)

        contents = [types.Content(role="user", parts=parts)]
        try:
            try:
                resp = self._gemini_client().models.generate_content(model=model, contents=contents,
                                                                     config=config(True))
            except errors.APIError as e:
                if getattr(e, "code", None) == 400 and "thinking" in str(e).lower():
                    # this model does not accept the thinking level - use its default instead
                    log.info("model %s rejected thinking_level=%s; retrying with its default", model, level)
                    resp = self._gemini_client().models.generate_content(model=model, contents=contents,
                                                                         config=config(False))
                else:
                    raise
            entry["thinking_level"] = level
        except errors.APIError as e:
            code = getattr(e, "code", None)
            msg = str(getattr(e, "message", "") or e)
            entry["error"] = f"API error {code}"
            if code in (401, 403) or (code == 400 and "api key" in msg.lower()):
                raise LLMUnavailable("Gemini rejected the API key - check GEMINI_API_KEY.") from e
            if code == 404:
                raise LLMUnavailable(f"Gemini model '{model}' was not found - "
                                     "set GEMINI_MODEL to a model listed in Google AI Studio.") from e
            if code == 429:
                raise LLMUnavailable("Gemini rate limit or quota reached.") from e
            raise LLMUnavailable(f"Gemini service error ({code}): {msg[:160]}") from e
        except Exception as e:  # noqa: BLE001 - network/timeout errors from the HTTP layer
            entry["error"] = "Connection error / timeout"
            log.warning("gemini call failed: %s", e)
            raise LLMUnavailable("Could not reach the Gemini service.") from e

        entry["served_by"] = getattr(resp, "model_version", None)
        u = getattr(resp, "usage_metadata", None)
        if u is not None:
            entry["input_tokens"] = getattr(u, "prompt_token_count", None)
            entry["output_tokens"] = getattr(u, "candidates_token_count", None)
        cand = (resp.candidates or [None])[0]
        reason = str(getattr(getattr(cand, "finish_reason", None), "value", getattr(cand, "finish_reason", "")) or "")
        if cand is None:
            block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            raise LLMUnavailable(f"Gemini returned no answer{f' (blocked: {block})' if block else ''}.")
        if reason == "MAX_TOKENS":
            raise LLMUnavailable("The AI response was cut off (max tokens).")
        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
            raise LLMUnavailable(f"Gemini declined to answer ({reason.lower()}).")
        text = resp.text
        if not text:
            raise LLMUnavailable("The AI returned no text.")
        return text

    # ------------------------------------------------------------------ Anthropic
    def _anthropic_client(self):
        if self._anthropic is None:
            import anthropic

            kwargs = {"timeout": self.settings.llm_timeout_seconds, "max_retries": 2}
            if self.settings.anthropic_api_key:
                kwargs["api_key"] = self.settings.anthropic_api_key
            self._anthropic = anthropic.Anthropic(**kwargs)
        return self._anthropic

    def _call_anthropic(self, system: str, blocks: list[dict], schema: dict, effort: str, max_tokens: int,
                        entry: dict) -> str:
        import anthropic

        content = []
        for b in blocks:
            if b["type"] == "pdf":
                content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                               "data": base64.b64encode(b["data"]).decode()}})
            else:
                content.append({"type": "text", "text": b["text"]})
        params = dict(
            model=self.settings.llm_model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            try:
                resp = self._anthropic_client().beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            except (anthropic.BadRequestError, TypeError) as e:
                log.info("retrying without server-side fallbacks: %s", e)
                resp = self._anthropic_client().messages.create(**params)
        except anthropic.AuthenticationError as e:
            entry["error"] = "Invalid API key"
            raise LLMUnavailable("Claude authentication failed - check ANTHROPIC_API_KEY.") from e
        except anthropic.RateLimitError as e:
            entry["error"] = "Rate limited"
            raise LLMUnavailable("AI rate limit reached.") from e
        except anthropic.APIStatusError as e:
            entry["error"] = f"API error {e.status_code}"
            raise LLMUnavailable(f"AI service error ({e.status_code}).") from e
        except anthropic.APIConnectionError as e:
            entry["error"] = "Connection error / timeout"
            raise LLMUnavailable("Could not reach the AI service.") from e
        entry["served_by"] = getattr(resp, "model", None)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            entry["input_tokens"] = getattr(usage, "input_tokens", None)
            entry["output_tokens"] = getattr(usage, "output_tokens", None)
        if resp.stop_reason == "refusal":
            raise LLMUnavailable("The AI model declined the request.")
        if resp.stop_reason == "max_tokens":
            raise LLMUnavailable("The AI response was cut off (max_tokens).")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if not text:
            raise LLMUnavailable("The AI returned no text.")
        return text
