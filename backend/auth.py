"""
Session handling for the operator console.

DEMO-GRADE accounts (one shared login from config), but the session rules are
the real ones you'd keep:
  * sliding idle timeout  - any authenticated request keeps the session alive,
                            so a person who walks away is signed out;
  * absolute lifetime cap - a session never lives longer than SESSION_TTL_SECONDS;
  * login throttling      - repeated wrong passwords from one address are locked out.

Before real use, replace the single shared login with a user table, hashed
passwords (bcrypt/argon2) or campus SSO. State lives in memory, so run one
worker process only.
"""
from __future__ import annotations

import secrets
import time

from fastapi import HTTPException, Header

import config

_SESSIONS: dict[str, dict] = {}        # token -> {"created": ts, "last": ts}
_FAILURES: dict[str, list[float]] = {}  # client ip -> timestamps of recent failed logins


def _recent_failures(ip: str) -> list[float]:
    cutoff = time.time() - config.LOGIN_LOCKOUT_SECONDS
    kept = [t for t in _FAILURES.get(ip, []) if t > cutoff]
    _FAILURES[ip] = kept
    return kept


def login(username: str, password: str, ip: str = "unknown") -> str:
    if len(_recent_failures(ip)) >= config.LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")
    ok_user = secrets.compare_digest(username.encode(), config.DEMO_USERNAME.encode())
    ok_pass = secrets.compare_digest(password.encode(), config.DEMO_PASSWORD.encode())
    if not (ok_user and ok_pass):
        _FAILURES.setdefault(ip, []).append(time.time())
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    _FAILURES.pop(ip, None)
    token = secrets.token_urlsafe(24)
    now = time.time()
    _SESSIONS[token] = {"created": now, "last": now}
    return token


def _valid(token: str | None, touch: bool = False) -> bool:
    """touch=True counts the call as user activity (resets the idle clock)."""
    if not token:
        return False
    s = _SESSIONS.get(token)
    if s is None:
        return False
    now = time.time()
    if now - s["created"] > config.SESSION_TTL_SECONDS or now - s["last"] > config.SESSION_IDLE_SECONDS:
        _SESSIONS.pop(token, None)
        return False
    if touch:
        s["last"] = now
    return True


def logout(token: str) -> None:
    _SESSIONS.pop(token, None)


def require_auth(authorization: str = Header(default="")) -> str:
    """FastAPI dependency for REST endpoints: `Authorization: Bearer <token>`."""
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not _valid(token, touch=True):
        raise HTTPException(status_code=401, detail="Sign in required.")
    return token
