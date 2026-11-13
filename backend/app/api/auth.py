"""Two configured accounts (AP reviewer, AP manager) plus guests, with signed bearer tokens (HMAC-SHA256).

A guest token also names the guest's private workspace ("w") and acts as "guest-reviewer" or
"guest-manager"; switching role issues a new token for the same workspace."""
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


GUEST_USERS = {"guest-reviewer": "REVIEWER", "guest-manager": "MANAGER"}


def issue_token(username: str, workspace: str | None = None) -> str:
    s = get_settings()
    hours = s.guest_ttl_hours if workspace else s.token_ttl_hours
    data = {"u": username, "exp": int(time.time()) + hours * 3600}
    if workspace:
        data["w"] = workspace
    payload = json.dumps(data).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def token_payload(token: str) -> dict | None:
    """The verified, unexpired token payload, or None."""
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(payload)
        if data["exp"] < time.time():
            return None
        if data["u"] in GUEST_USERS and not data.get("w"):
            return None  # a guest identity is only valid inside its own workspace
        return data
    except Exception:  # noqa: BLE001
        return None


def verify_token(token: str) -> str | None:
    data = token_payload(token)
    return data["u"] if data else None


def is_guest(username: str) -> bool:
    return username in GUEST_USERS


def check_credentials(username: str, password: str) -> bool:
    s = get_settings()
    ok = hmac.compare_digest(username, s.reviewer_username) and hmac.compare_digest(password, s.reviewer_password)
    if s.manager_password:
        ok |= hmac.compare_digest(username, s.manager_username) and hmac.compare_digest(password, s.manager_password)
    return ok


def user_role(username: str) -> str:
    if username in GUEST_USERS:
        return GUEST_USERS[username]
    s = get_settings()
    return "MANAGER" if s.manager_password and username == s.manager_username else "REVIEWER"


def user_info(username: str, profile=None) -> dict:
    """`profile` is the user's saved UserProfile row (name / designation / photo they chose), if any."""
    s = get_settings()
    role = user_role(username)
    guest = username in GUEST_USERS
    if guest:
        default_name = "Guest evaluator"
        default_designation = "Guest · manager view" if role == "MANAGER" else "Guest · reviewer view"
    elif role == "MANAGER":
        default_name, default_designation = s.manager_display_name, "Accounts Payable Manager"
    else:
        default_name, default_designation = s.reviewer_display_name, "Accounts Payable"
    return {"username": username, "role": role, "guest": guest,
            "name": profile.display_name if profile and profile.display_name else default_name,
            "designation": profile.designation if profile and profile.designation else default_designation,
            "avatar": profile.avatar if profile else None}


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    user = verify_token(creds.credentials) if creds else None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail={"error_code": "UNAUTHORIZED",
                                                                    "message": "Sign in required."})
    return user
