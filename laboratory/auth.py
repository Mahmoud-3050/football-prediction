"""Session cookie auth for the laboratory dashboard."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Mapping
from urllib.parse import parse_qs

COOKIE_NAME = "pitch_lab_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
_MAX_BODY = 8_192


@dataclass(frozen=True)
class DashboardAuth:
    """Credentials for the dashboard. Empty password means auth is disabled."""

    username: str
    password: str
    session_secret: str
    cookie_secure: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def check_password(self, username: str, password: str) -> bool:
        if not self.enabled:
            return True
        return secrets.compare_digest(username, self.username) and secrets.compare_digest(
            password, self.password
        )

    def csrf_token(self) -> str:
        return hmac.new(
            self.session_secret.encode("utf-8"),
            b"login-csrf-v1",
            hashlib.sha256,
        ).hexdigest()[:32]

    def issue_session(self, username: str, *, now: int | None = None) -> str:
        issued = int(time.time() if now is None else now)
        expires = issued + SESSION_TTL_SECONDS
        payload = f"{username}:{expires}"
        signature = hmac.new(
            self.session_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{payload}:{signature}"

    def verify_session(self, token: str | None, *, now: int | None = None) -> bool:
        if not self.enabled:
            return True
        if not token or token.count(":") != 2:
            return False
        username, expires_raw, signature = token.split(":", 2)
        try:
            expires = int(expires_raw)
        except ValueError:
            return False
        current = int(time.time() if now is None else now)
        if expires < current:
            return False
        payload = f"{username}:{expires}"
        expected = hmac.new(
            self.session_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        return secrets.compare_digest(username, self.username)

    def session_cookie_header(self, token: str) -> str:
        parts = [
            f"{COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={SESSION_TTL_SECONDS}",
        ]
        if self.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_session_cookie_header(self) -> str:
        parts = [
            f"{COOKIE_NAME}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
        if self.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)


def load_dashboard_auth(environ: Mapping[str, str] | None = None) -> DashboardAuth:
    source = environ if environ is not None else os.environ
    username = (source.get("DASHBOARD_USER") or "admin").strip() or "admin"
    password = (source.get("DASHBOARD_PASSWORD") or "").strip()
    explicit = (source.get("DASHBOARD_SESSION_SECRET") or "").strip()
    if explicit:
        session_secret = explicit
    elif password:
        session_secret = hashlib.sha256(f"pitch-lab:{password}".encode("utf-8")).hexdigest()
    else:
        session_secret = "dev-unauthenticated"
    secure = (source.get("DASHBOARD_COOKIE_SECURE") or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return DashboardAuth(
        username=username,
        password=password,
        session_secret=session_secret,
        cookie_secure=secure,
    )


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def require_auth_for_host(host: str, auth: DashboardAuth) -> None:
    """Fail closed when exposing the dashboard off loopback without a password."""

    if is_loopback_host(host):
        return
    if not auth.enabled:
        raise SystemExit(
            "DASHBOARD_PASSWORD is required when binding the dashboard outside "
            "127.0.0.1/localhost. Set it in .env, or pass --host 127.0.0.1."
        )


def cookie_value(header: str | None, name: str = COOKIE_NAME) -> str | None:
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:
        return None
    morsel = jar.get(name)
    if morsel is None:
        return None
    value = morsel.value.strip()
    return value or None


def parse_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: (values[-1] if values else "") for key, values in parsed.items()}


def read_request_body(handler, *, limit: int = _MAX_BODY) -> bytes:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError:
        return b""
    if length < 0 or length > limit:
        return b""
    return handler.rfile.read(length)
