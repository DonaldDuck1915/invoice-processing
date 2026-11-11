"""Thin wrapper around the Claude API used for extraction, description meaning and summaries.

The model never computes amounts or decides outcomes. Every call is logged (purpose, model,
latency, token usage, outcome) so it can be shown in the audit trail.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any

import anthropic

from .config import get_settings

log = logging.getLogger(__name__)

AI_DISCLAIMER = "AI and can make mistakes. Please double-check responses."


class LLMUnavailable(Exception):
    pass


class LLMClient:
    def __init__(self):
        self.settings = get_settings()
        self.calls: list[dict[str, Any]] = []
        self._cache: dict[str, Any] = {}
        self._client: anthropic.Anthropic | None = None

    @property
    def available(self) -> bool:
        return self.settings.llm_available

    def _get(self) -> anthropic.Anthropic:
        if self._client is None:
            kwargs = {"timeout": self.settings.llm_timeout_seconds, "max_retries": 2}
            if self.settings.anthropic_api_key:
                kwargs["api_key"] = self.settings.anthropic_api_key
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def json_call(self, purpose: str, system: str, user_content: list[dict], schema: dict,
                  effort: str = "medium", max_tokens: int = 16000) -> dict:
        """Call Claude with a strict JSON schema output. Raises LLMUnavailable on any failure."""
        if not self.available:
            raise LLMUnavailable("AI is not configured (no ANTHROPIC_API_KEY).")
        key = hashlib.sha256(json.dumps([purpose, system, user_content, schema], sort_keys=True,
                                        default=str).encode()).hexdigest()
        if key in self._cache:
            self.calls.append({"purpose": purpose, "cached": True, "ok": True})
            return self._cache[key]
        t0 = time.time()
        entry: dict[str, Any] = {"purpose": purpose, "model": self.settings.llm_model, "ok": False}
        params = dict(
            model=self.settings.llm_model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user_content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            try:
                resp = self._get().beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            except (anthropic.BadRequestError, TypeError) as e:
                # fallback parameter not accepted for this model/account: plain request
                log.info("retrying without server-side fallbacks: %s", e)
                resp = self._get().messages.create(**params)
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
            data = json.loads(text)
            entry["ok"] = True
            self._cache[key] = data
            return data
        except anthropic.AuthenticationError as e:
            entry["error"] = "Invalid API key"
            raise LLMUnavailable("AI authentication failed - check ANTHROPIC_API_KEY.") from e
        except anthropic.RateLimitError as e:
            entry["error"] = "Rate limited"
            raise LLMUnavailable("AI rate limit reached.") from e
        except anthropic.APIStatusError as e:
            entry["error"] = f"API error {e.status_code}"
            raise LLMUnavailable(f"AI service error ({e.status_code}).") from e
        except anthropic.APIConnectionError as e:
            entry["error"] = "Connection error / timeout"
            raise LLMUnavailable("Could not reach the AI service.") from e
        except json.JSONDecodeError as e:
            entry["error"] = "Invalid JSON"
            raise LLMUnavailable("The AI returned malformed JSON.") from e
        except LLMUnavailable as e:
            entry["error"] = str(e)
            raise
        finally:
            entry["latency_ms"] = int((time.time() - t0) * 1000)
            self.calls.append(entry)


def pdf_block(data: bytes) -> dict:
    return {"type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}}
