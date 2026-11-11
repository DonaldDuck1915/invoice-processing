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
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_enabled: bool = True
    llm_timeout_seconds: float = 120.0

    reviewer_username: str = "reviewer"
    reviewer_password: str = "review123"
    reviewer_display_name: str = "AP Reviewer"
    auth_secret: str = "change-me-in-production"
    token_ttl_hours: int = 12

    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_mb: int = 10
    max_pages: int = 10
    seed_on_startup: bool = True
    tesseract_cmd: str | None = None

    @property
    def llm_available(self) -> bool:
        return bool(self.llm_enabled and (self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")))

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
