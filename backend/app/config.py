"""Application settings (environment) and business-rule loading."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

RULES_FILE = Path(__file__).with_name("rules.yaml")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./invoice_ai.db"
    # AI provider: "auto" uses Gemini if GEMINI_API_KEY is set, else Claude if ANTHROPIC_API_KEY is set.
    llm_provider: str = "auto"            # auto | gemini | anthropic
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"           # reads the PDF (extraction) - accuracy first
    gemini_fast_model: str | None = "gemini-3.5-flash-lite"  # summary, recommendation, description meaning
    gemini_fallback_model: str | None = None  # tried when the first model is overloaded; empty = the other model
    gemini_thinking: str = "auto"         # auto | low | medium | high | default (model's own setting)
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"    # Claude model id
    llm_enabled: bool = True
    llm_timeout_seconds: float = 60.0     # a slow AI call falls back to rules-only instead of stalling a run

    reviewer_username: str = "reviewer"
    reviewer_password: str = "review123"
    reviewer_display_name: str = "AP Reviewer"
    auth_secret: str = "change-me-in-production"
    token_ttl_hours: int = 12

    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_mb: int = 10
    max_pages: int = 10
    seed_on_startup: bool = True
    allow_demo_reset: bool = True        # set false in production: /admin/reset-demo wipes all data
    tesseract_cmd: str | None = None

    # Optional e-mail intake (IMAP). Leave IMAP_HOST empty to disable.
    imap_host: str | None = None
    imap_port: int = 993
    imap_ssl: bool = True
    imap_user: str | None = None
    imap_password: str | None = None
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = 60
    imap_max_per_poll: int = 25

    login_max_attempts: int = 5          # failed logins per client within the window below
    login_window_seconds: int = 300

    @property
    def insecure_defaults(self) -> list[str]:
        """Settings still at their published demo values."""
        out = []
        if self.auth_secret == "change-me-in-production":
            out.append("AUTH_SECRET")
        if self.reviewer_password == "review123":
            out.append("REVIEWER_PASSWORD")
        return out

    @property
    def ai_provider(self) -> str | None:
        """Which provider is active, or None when AI is off / unconfigured."""
        if not self.llm_enabled:
            return None
        gem = bool((self.gemini_api_key or "").strip())
        ant = bool((self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip())
        pref = (self.llm_provider or "auto").strip().lower()
        if pref in ("gemini", "google"):
            return "gemini" if gem else None
        if pref in ("anthropic", "claude"):
            return "anthropic" if ant else None
        return "gemini" if gem else ("anthropic" if ant else None)

    @property
    def ai_model(self) -> str | None:
        p = self.ai_provider
        return self.gemini_model if p == "gemini" else (self.llm_model if p == "anthropic" else None)

    @property
    def llm_available(self) -> bool:
        return self.ai_provider is not None

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Render provides postgres:// URLs; SQLAlchemy wants postgresql://
    for prefix in ("postgres://", "postgresql://"):
        if s.database_url.startswith(prefix):
            s.database_url = "postgresql+psycopg://" + s.database_url[len(prefix):]
    return s


def load_default_rules() -> dict[str, Any]:
    with RULES_FILE.open() as f:
        return yaml.safe_load(f)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def rules_hash(rules: dict) -> str:
    return hashlib.sha256(json.dumps(rules, sort_keys=True, default=str).encode()).hexdigest()[:12]
