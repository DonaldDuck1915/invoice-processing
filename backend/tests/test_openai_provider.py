"""OpenAI provider: selection, request shape (PDF + strict JSON schema), usage/cost logging and error
handling, exercised against a stand-in for OpenAI's client - no network, no credits used."""
import base64
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.config import Settings
from app.llm import LLMClient, LLMUnavailable, pdf_block

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}


def settings(**kw):
    base = dict(openai_api_key=None, llm_enabled=True)
    return Settings(_env_file=None, **{**base, **kw})


def resp(text, input_tokens=1000, output_tokens=200, cached=0, status="completed", incomplete=None, refusal=False):
    content = [SimpleNamespace(type="refusal", refusal="no")] if refusal else [SimpleNamespace(type="output_text")]
    return SimpleNamespace(
        model="gpt-6-luna-2026-10-01", status=status, output_text=text,
        incomplete_details=SimpleNamespace(reason=incomplete) if incomplete else None,
        output=[SimpleNamespace(type="message", content=content)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              input_tokens_details=SimpleNamespace(cached_tokens=cached),
                              output_tokens_details=SimpleNamespace(reasoning_tokens=50)))


def api_error(cls, status, message):
    r = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return cls(message, response=r, body={"error": {"message": message}})


class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def create(self, **kw):
        self.calls.append(kw)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def client_with(*outcomes, **settings_kw):
    c = LLMClient()
    c.settings = settings(openai_api_key="sk-test", **settings_kw)
    c._openai = SimpleNamespace(responses=FakeResponses(outcomes))
    return c


def test_openai_is_the_only_provider():
    assert settings(openai_api_key="sk").ai_provider == "openai"
    assert settings(openai_api_key="sk").ai_model == "gpt-6-luna"
    assert settings().ai_provider is None and settings().ai_model is None      # no key -> rules-only
    assert settings(openai_api_key="   ").ai_provider is None
    assert settings(openai_api_key="sk", llm_enabled=False).ai_provider is None
    # old provider settings in an existing .env are ignored rather than breaking startup
    assert settings(openai_api_key="sk", gemini_api_key="g", llm_provider="gemini").ai_provider == "openai"


def test_openai_sends_pdf_and_strict_schema_and_logs_cost():
    c = client_with(resp(json.dumps({"x": "hi"}), input_tokens=3000, output_tokens=400, cached=1000))
    pdf = b"%PDF-1.4 test"
    out = c.json_call("extraction", "sys prompt", [pdf_block(pdf), {"type": "text", "text": "go"}], SCHEMA,
                      effort="high")
    assert out == {"x": "hi"}
    kw = c._openai.responses.calls[0]
    assert kw["model"] == "gpt-6-luna" and kw["instructions"] == "sys prompt" and kw["store"] is False
    file_part, text_part = kw["input"][0]["content"]
    assert file_part["type"] == "input_file"
    assert file_part["file_data"] == "data:application/pdf;base64," + base64.b64encode(pdf).decode()
    assert text_part == {"type": "input_text", "text": "go"}
    assert kw["text"]["format"] == {"type": "json_schema", "name": "result", "schema": SCHEMA, "strict": True}
    assert kw["reasoning"] == {"effort": "medium"}  # extraction ("high") -> medium; everything else low
    call = c.calls[-1]
    assert call["provider"] == "openai" and call["ok"] and call["served_by"].startswith("gpt-6-luna")
    # 2000 uncached x $0.10 + 1000 cached x $0.01 + 400 out x $0.50, per 1M tokens
    assert call["cost_usd"] == pytest.approx((2000 * 0.10 + 1000 * 0.01 + 400 * 0.50) / 1e6)


def test_fast_model_and_pdf_detail_settings():
    c = client_with(resp('{"x": "a"}'), resp('{"x": "b"}'), openai_fast_model="gpt-6-luna-mini",
                    openai_pdf_detail="low")
    c.json_call("extraction", "s", [pdf_block(b"%PDF-1.4")], SCHEMA)
    c.json_call("decision_summary", "s", [{"type": "text", "text": "facts"}], SCHEMA, effort="low")
    first, second = c._openai.responses.calls
    assert first["model"] == "gpt-6-luna" and first["input"][0]["content"][0]["detail"] == "low"
    assert second["model"] == "gpt-6-luna-mini" and second["reasoning"] == {"effort": "low"}


def test_reasoning_rejected_is_retried_without_it():
    c = client_with(api_error(openai.BadRequestError, 400, "Unsupported parameter: 'reasoning.effort'"),
                    resp('{"x": "ok"}'))
    assert c.json_call("decision_summary", "s", [{"type": "text", "text": "t"}], SCHEMA) == {"x": "ok"}
    first, second = c._openai.responses.calls
    assert "reasoning" in first and "reasoning" not in second
    assert c.calls[-1]["reasoning"] == "default"


@pytest.mark.parametrize("error,message", [
    (api_error(openai.AuthenticationError, 401, "Incorrect API key"), "OPENAI_API_KEY"),
    (api_error(openai.RateLimitError, 429, "You exceeded your current quota"), "credits / quota exhausted"),
    (api_error(openai.RateLimitError, 429, "Rate limit reached for requests"), "rate limit"),
    (api_error(openai.NotFoundError, 404, "model not found"), "OPENAI_MODEL"),
])
def test_errors_are_explained(error, message):
    c = client_with(error)
    with pytest.raises(LLMUnavailable, match=message):
        c.json_call("extraction", "s", [{"type": "text", "text": "t"}], SCHEMA)
    assert len(c._openai.responses.calls) == 1  # not retried on another model: it would not help


def test_overloaded_model_falls_back():
    c = client_with(api_error(openai.InternalServerError, 503, "overloaded"), resp('{"x": "ok"}'),
                    openai_fallback_model="gpt-6-terra")
    assert c.json_call("extraction", "s", [{"type": "text", "text": "t"}], SCHEMA) == {"x": "ok"}
    assert [k["model"] for k in c._openai.responses.calls] == ["gpt-6-luna", "gpt-6-terra"]


@pytest.mark.parametrize("response,message", [
    (resp("", status="incomplete", incomplete="max_output_tokens"), "cut off"),
    (resp("", status="incomplete", incomplete="content_filter"), "declined"),
    (resp("", refusal=True), "declined"),
    (resp("not json"), "malformed JSON"),
])
def test_bad_responses_are_rejected(response, message):
    c = client_with(response)
    with pytest.raises(LLMUnavailable, match=message):
        c.json_call("extraction", "s", [{"type": "text", "text": "t"}], SCHEMA)
