"""Application settings (environment) and business-rule loading."""
from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

RULES_FILE = Path(__file__).with_name("rules.yaml")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./invoice_ai.db"
    # AI (OpenAI). Without a key the app runs rules-only.
    openai_api_key: str | None = None
    openai_model: str = "gpt-6-luna"      # reads the PDF (extraction)
    openai_fast_model: str | None = None  # summary, recommendation, description meaning; empty = OPENAI_MODEL
    openai_fallback_model: str | None = None  # tried when the first model is overloaded / times out
    openai_reasoning: str = "auto"        # auto | none | minimal | low | medium | high | default
    openai_pdf_detail: str = "auto"       # auto | low | high - how PDF pages are rendered (low = fewer tokens)
    openai_base_url: str | None = None    # only for an OpenAI-compatible proxy / Azure endpoint
    # USD per 1M tokens, used to show the cost of each run (gpt-6-luna list prices; adjust if they change)
    openai_price_input_per_m: float = 0.10
    openai_price_cached_input_per_m: float = 0.01
    openai_price_output_per_m: float = 0.50
    llm_enabled: bool = True
    # auto: skip the AI reader for clean digital invoices (all critical fields read, every total
    # reconciles); always: send every invoice to the AI reader
    ai_extraction: str = "auto"
    llm_timeout_seconds: float = 60.0
    # Throughput: invoices read (OCR / AI) at the same time, and the cap on simultaneous OpenAI requests
    # across the whole process. Calls that hit a rate limit or a 5xx are retried with exponential back-off.
    read_concurrency: int = 3
    openai_max_concurrency: int = 4
    openai_max_retries: int = 3           # retries per call on 429 (rate limit) / 5xx
    openai_backoff_seconds: float = 1.0   # first back-off; doubles each retry (plus jitter), capped at 20 s
    openai_requests_per_minute: int = 0   # 0 = no client-side limit; set to your OpenAI tier's RPM to pace calls     # a slow AI call falls back to rules-only instead of stalling a run

    reviewer_username: str = "reviewer"
    reviewer_password: str = "review123"
    reviewer_display_name: str = "AP Reviewer"
    # Second account with the MANAGER role: the only one allowed to approve invoices above the
    # auto-approval limit. Leave MANAGER_PASSWORD empty to disable it.
    manager_username: str = "manager"
    manager_password: str = "manage123"
    manager_display_name: str = "AP Manager"
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

    # Guest mode: "Continue as guest" gives each visitor a private sandbox with the demo data.
    guest_mode: bool = True
    guest_ttl_hours: int = 24             # a sandbox unused this long is deleted
    guest_max_workspaces: int = 100       # live sandboxes at once
    guest_per_ip_per_hour: int = 5        # new sandboxes one client may create per hour
    guest_max_invoices: int = 150         # uploads per sandbox (each may call the AI)
    guest_data_dir: str = "./guest_data"  # SQLite only; on Postgres each sandbox is a schema

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
        if self.manager_password == "manage123":
            out.append("MANAGER_PASSWORD")
        return out

    @property
    def ai_provider(self) -> str | None:
        """"openai" when AI is enabled and a key is set, else None (rules-only)."""
        return "openai" if self.llm_enabled and (self.openai_api_key or "").strip() else None

    @property
    def ai_model(self) -> str | None:
        return self.openai_model if self.ai_provider else None

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
