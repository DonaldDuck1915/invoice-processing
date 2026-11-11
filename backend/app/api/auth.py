"""Single hard-coded reviewer account with signed bearer tokens (HMAC-SHA256)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import get_settings

bearer = HTTPBearer(auto_error=False)


def _sign(payload: bytes) -> str:
    key = get_settings().auth_secret.encode()
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip("=")


def issue_token(username: str) -> str:
    s = get_settings()
    payload = json.dumps({"u": username, "exp": int(time.time()) + s.token_ttl_hours * 3600}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def verify_token(token: str) -> str | None:
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(payload)
        if data["exp"] < time.time():
            return None
        return data["u"]
    except Exception:  # noqa: BLE001
        return None


def check_credentials(username: str, password: str) -> bool:
    s = get_settings()
    return hmac.compare_digest(username, s.reviewer_username) and hmac.compare_digest(password, s.reviewer_password)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    user = verify_token(creds.credentials) if creds else None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail={"error_code": "UNAUTHORIZED",
                                                                    "message": "Sign in required."})
    return user
