"""Passwords, sessions, CSRF, and the login throttle."""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

from . import config

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


NAME_RE = re.compile(r"^[\w .'\-]{2,40}$", re.UNICODE)


def normalize_login(value: str) -> str:
    """One handle for a name: collapse whitespace, case-fold, keep the words."""
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def valid_name(value: str) -> bool:
    return bool(NAME_RE.match((value or "").strip()))


def valid_login(value: str) -> bool:
    """The login handle is local to this machine: an email address or a plain username."""
    return bool(EMAIL_RE.match(value) or USERNAME_RE.match(value))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, Exception):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < config.MIN_PASSWORD_LENGTH:
        return f"Use at least {config.MIN_PASSWORD_LENGTH} characters."
    if password.lower() in {"password", "bananastand", "letmein1234"}:
        return "That password is too common."
    return None


HINT_MAX = 80


def clean_hint(value: str) -> str:
    """A hint is displayed before sign-in, so keep it one line and short."""
    return " ".join((value or "").split())[:HINT_MAX]


def hint_problem(value: str) -> str | None:
    text = " ".join((value or "").split())
    if len(text) > HINT_MAX:
        return f"Keep the hint to {HINT_MAX} characters or fewer."
    if text and len(text) < 3:
        return "The hint needs at least 3 characters, or leave it empty."
    return None


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def constant_time_equal(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return secrets.compare_digest(a, b)


class LoginThrottle:
    """In-memory per-process throttle: 5 failures per (ip,email) per 15 minutes."""

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _key(self, ip: str, email: str) -> str:
        return f"{ip}|{email.lower()}"

    def blocked(self, ip: str, email: str) -> bool:
        dq = self._hits[self._key(ip, email)]
        cutoff = time.time() - self.window
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq) >= self.max_attempts

    def fail(self, ip: str, email: str) -> None:
        self._hits[self._key(ip, email)].append(time.time())

    def reset(self, ip: str, email: str) -> None:
        self._hits.pop(self._key(ip, email), None)

    def retry_after(self, ip: str, email: str) -> int:
        dq = self._hits[self._key(ip, email)]
        if not dq:
            return 0
        return max(1, int(self.window - (time.time() - dq[0])))


throttle = LoginThrottle(config.LOGIN_MAX_ATTEMPTS, config.LOGIN_WINDOW_MINUTES * 60)
