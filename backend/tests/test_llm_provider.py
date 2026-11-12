"""Provider selection and the Gemini call path, exercised against a stand-in for Google's client."""
import json

import pytest
from google.genai import errors, types

from app import llm as llm_mod
from app.config import Settings
from app.llm import LLMClient, LLMUnavailable, gemini_schema, pdf_block

SCHEMA = {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
          "required": ["x"], "additionalProperties": False}


def settings(**kw):
    base = dict(gemini_api_key=None, anthropic_api_key=None, llm_provider="auto", llm_enabled=True)
    return Settings(_env_file=None, **{**base, **kw})


def test_provider_selection(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert settings().ai_provider is None
    assert settings(gemini_api_key="g").ai_provider == "gemini"
    assert settings(anthropic_api_key="a").ai_provider == "anthropic"
    assert settings(gemini_api_key="g", anthropic_api_key="a").ai_provider == "gemini"       # auto prefers Gemini
    assert settings(gemini_api_key="g", anthropic_api_key="a", llm_provider="anthropic").ai_provider == "anthropic"
    assert settings(gemini_api_key="g", llm_provider="anthropic").ai_provider is None        # forced but no key
    assert settings(gemini_api_key="g", llm_enabled=False).ai_provider is None
    assert settings(gemini_api_key="  ").ai_provider is None


def test_gemini_schema_drops_additional_properties_only():
    out = gemini_schema(SCHEMA)
    assert "additionalProperties" not in out and out["properties"]["x"]["anyOf"][1] == {"type": "null"}
    assert SCHEMA["additionalProperties"] is False  # original untouched


class FakeModels:
    def __init__(self, outcome):
        self.outcome, self.kwargs = outcome, None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeGenai:
    def __init__(self, outcome):
        self.models = FakeModels(outcome)


def response(text, finish="STOP"):
    cand = types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]),
                           finish_reason=finish)
    return types.GenerateContentResponse(candidates=[cand], model_version="gemini-test")


def client_with(outcome):
    c = LLMClient()
    c.settings = settings(gemini_api_key="key", gemini_model="gemini-test")
    c._gemini = FakeGenai(outcome)
    return c


def test_gemini_success_sends_pdf_schema_and_logs_call():
    c = client_with(response(json.dumps({"x": "hello"})))
    out = c.json_call("extraction", "sys", [pdf_block(b"%PDF-1.4 test"), {"type": "text", "text": "go"}], SCHEMA)
    assert out == {"x": "hello"}
    kw = c._gemini.models.kwargs
    assert kw["model"] == "gemini-test"
    parts = kw["contents"][0].parts
    assert parts[0].inline_data.mime_type == "application/pdf" and parts[1].text == "go"
    cfg = kw["config"]
    assert cfg.response_mime_type == "application/json" and cfg.system_instruction == "sys" and cfg.temperature == 0
    assert "additionalProperties" not in json.dumps(cfg.response_json_schema)
    assert c.calls[-1]["provider"] == "gemini" and c.calls[-1]["ok"]
    c.json_call("extraction", "sys", [pdf_block(b"%PDF-1.4 test"), {"type": "text", "text": "go"}], SCHEMA)
    assert c.calls[-1].get("cached")  # identical request is served from cache


@pytest.mark.parametrize("outcome,needle", [
    (errors.ClientError(400, {"error": {"message": "API key not valid. Please pass a valid API key."}}), "GEMINI_API_KEY"),
    (errors.ClientError(403, {"error": {"message": "denied"}}), "GEMINI_API_KEY"),
    (errors.ClientError(404, {"error": {"message": "model not found"}}), "GEMINI_MODEL"),
    (errors.ClientError(429, {"error": {"message": "quota"}}), "rate limit"),
    (errors.ServerError(503, {"error": {"message": "overloaded"}}), "503"),
    (ConnectionError("boom"), "Could not reach"),
    (response("not json"), "malformed"),
    (response("{}", finish="MAX_TOKENS"), "cut off"),
    (response("{}", finish="SAFETY"), "declined"),
    (types.GenerateContentResponse(candidates=[]), "no answer"),
])
def test_gemini_failures_become_llm_unavailable(outcome, needle):
    c = client_with(outcome)
    with pytest.raises(LLMUnavailable) as e:
        c.json_call("p", "s", [{"type": "text", "text": "t"}], SCHEMA)
    assert needle.lower() in str(e.value).lower()
    assert c.calls[-1]["ok"] is False


def test_unconfigured_ai_is_unavailable():
    c = LLMClient()
    c.settings = settings()
    assert not c.available
    with pytest.raises(LLMUnavailable):
        c.json_call("p", "s", [{"type": "text", "text": "t"}], SCHEMA)


# ----------------------------------------------------------------------------- model routing + thinking

def test_fast_model_for_short_tasks_and_low_thinking():
    c = client_with(response(json.dumps({"x": "ok"})))
    c.settings = settings(gemini_api_key="key", gemini_model="main-model", gemini_fast_model="fast-model")
    c.json_call("extraction", "sys", [{"type": "text", "text": "a"}], SCHEMA, effort="high")
    assert c._gemini.models.kwargs["model"] == "main-model"
    assert c._gemini.models.kwargs["config"].thinking_config.thinking_level == types.ThinkingLevel.MEDIUM
    c.json_call("decision_summary", "sys", [{"type": "text", "text": "b"}], SCHEMA, effort="low")
    assert c._gemini.models.kwargs["model"] == "fast-model"
    assert c._gemini.models.kwargs["config"].thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert c.calls[-1]["model"] == "fast-model"


def test_thinking_level_rejected_by_model_is_retried_without_it():
    class Models:
        def __init__(self):
            self.configs = []

        def generate_content(self, **kw):
            self.configs.append(kw["config"])
            if kw["config"].thinking_config is not None:
                raise errors.ClientError(400, {"error": {"message": "thinking_level is not supported"}})
            return response(json.dumps({"x": "ok"}))

    c = client_with(None)
    c._gemini.models = Models()
    assert c.json_call("decision_summary", "sys", [{"type": "text", "text": "c"}], SCHEMA) == {"x": "ok"}
    assert len(c._gemini.models.configs) == 2 and c._gemini.models.configs[1].thinking_config is None
