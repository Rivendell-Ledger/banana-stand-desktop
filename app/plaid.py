"""Plaid transport, token encryption, and the pieces shared by link and sync.

Design rules:
- No bank credentials ever touch this app; Plaid Link collects them in its own iframe.
- Access tokens are encrypted at rest and never logged. Errors are logged as codes only.
- Sandbox is the default. Real institutions require PLAID_ENV=production explicitly.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import config

logger = logging.getLogger("banana.plaid")

BASE_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

PRODUCTS = ["transactions"]
# Investment accounts (brokerage, IRA, 529) only return holdings with this product.
# "required_if_supported" keeps the consent minimal for plain bank links.
REQUIRED_IF_SUPPORTED = ["investments"]
HISTORY_DAYS = 730  # Plaid's maximum; the default of 90 is why a ledger starts three months back
COUNTRY_CODES = ["US"]
PLAID_VERSION = "2020-09-14"


class PlaidError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------- config
def client_id() -> str:
    return (os.environ.get("PLAID_CLIENT_ID") or getattr(config, "PLAID_CLIENT_ID", "")).strip()


def secret() -> str:
    return (os.environ.get("PLAID_SECRET") or getattr(config, "PLAID_SECRET", "")).strip()


def env_name() -> str:
    value = (os.environ.get("PLAID_ENV") or getattr(config, "PLAID_ENV", "sandbox")).strip().lower()
    return value if value in BASE_URLS else "sandbox"


def configured() -> bool:
    return bool(client_id() and secret())


def max_items() -> int:
    try:
        return int(os.environ.get("PLAID_MAX_ITEMS") or getattr(config, "PLAID_MAX_ITEMS", 10))
    except (TypeError, ValueError):
        return 10


def webhook_url() -> str:
    return (os.environ.get("PLAID_WEBHOOK_URL") or "").strip()


# ---------------------------------------------------------------- token vault
def _key_path() -> Path:
    return Path(config.SECRET_DIR) / "fernet.key"


def _fernet(create: bool = True) -> Fernet:
    path = _key_path()
    if not path.exists():
        if not create:
            raise PlaidError("key_missing",
                             "The encryption key is missing from this data folder.")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write through a temp file: a crash must not leave a half-written key.
        temp = path.with_suffix(".key.tmp")
        temp.write_bytes(Fernet.generate_key())
        os.replace(temp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    try:
        return Fernet(path.read_bytes().strip())
    except (ValueError, TypeError) as exc:
        raise PlaidError("key_invalid", "The encryption key is unreadable.") from exc


def ensure_key() -> Path:
    """Create the token-encryption key if it does not exist yet, and return its path."""
    _fernet()
    return _key_path()


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_token(value: str) -> str:
    try:
        # Never create a key here: decrypting with a fresh key would silently turn
        # every stored token into "unreadable" and make the old recovery kit look wrong.
        return _fernet(create=False).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise PlaidError("token_unreadable", "Stored Plaid token could not be decrypted.") from exc


# ---------------------------------------------------------------- HTTP
def _post(path: str, payload: dict) -> dict:
    if not configured():
        raise PlaidError("not_configured", "Plaid is not configured yet.", status=400)
    url = BASE_URLS[env_name()] + path
    headers = {
        "PLAID-CLIENT-ID": client_id(),
        "PLAID-SECRET": secret(),
        "Plaid-Version": PLAID_VERSION,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=25.0)
    except httpx.HTTPError as exc:
        logger.warning("plaid network error on %s: %s", path, type(exc).__name__)
        raise PlaidError("network", "Could not reach Plaid. Try again.") from exc

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        code = body.get("error_code") or f"http_{resp.status_code}"
        message = body.get("error_message") or "Plaid rejected the request."
        logger.warning("plaid error on %s: %s", path, code)
        status = 400 if code in {"INVALID_REQUEST", "INVALID_INPUT"} else 502
        raise PlaidError(code, message, status=status)
    return resp.json()


# ---------------------------------------------------------------- endpoints
def create_link_token(user_id: str, access_token: str | None = None,
                      additional_products: list[str] | None = None) -> str:
    payload: dict = {
        "client_name": config.APP_NAME,
        "language": "en",
        "country_codes": COUNTRY_CODES,
        "user": {"client_user_id": str(user_id)},
        "webhook": webhook_url() or None,
    }
    if access_token:
        # Update mode: repair an existing Item instead of minting a new one, or
        # collect consent for a product the Item was not linked with.
        payload["access_token"] = access_token
        payload.pop("webhook", None)
        if additional_products:
            payload["additional_consented_products"] = list(additional_products)
    else:
        payload["products"] = PRODUCTS
        payload["required_if_supported_products"] = REQUIRED_IF_SUPPORTED
        # Plaid's default is 90 days of history, and an Item can never widen it afterwards —
        # only a fresh Item can. Ask for the maximum two years up front.
        payload["transactions"] = {"days_requested": HISTORY_DAYS}
    data = _post("/link/token/create", {k: v for k, v in payload.items() if v is not None})
    return data["link_token"]


def exchange_public_token(public_token: str) -> dict:
    return _post("/item/public_token/exchange", {"public_token": public_token})


def item_get(access_token: str) -> dict:
    return _post("/item/get", {"access_token": access_token})


def institutions_get_by_id(institution_id: str) -> dict:
    data = _post(
        "/institutions/get_by_id",
        {"institution_id": institution_id, "country_codes": COUNTRY_CODES},
    )
    return data.get("institution", {})


def accounts_get(access_token: str) -> list[dict]:
    return _post("/accounts/get", {"access_token": access_token}).get("accounts", [])


def transactions_sync(access_token: str, cursor: str | None) -> dict:
    payload = {"access_token": access_token, "count": 500}
    if cursor:
        payload["cursor"] = cursor
    return _post("/transactions/sync", payload)


def refresh_investments(access_token: str) -> dict:
    """Ask the institution for fresh positions (per-item cap: 1/min, 10/hour, 20/day)."""
    return _post("/investments/refresh", {"access_token": access_token})


def investments_holdings_get(access_token: str) -> dict:
    """Securities + positions for an Item that has consented to investments."""
    return _post("/investments/holdings/get", {"access_token": access_token})


def investments_transactions_get(access_token: str, start_date: str, end_date: str,
                                 count: int = 500) -> dict:
    return _post(
        "/investments/transactions/get",
        {"access_token": access_token, "start_date": start_date, "end_date": end_date,
         "options": {"count": count}},
    )


def item_remove(access_token: str) -> dict:
    return _post("/item/remove", {"access_token": access_token})


# ---------------------------------------------------------------- webhooks
def verify_webhook(jwt_body: str, raw_body: bytes) -> dict:
    """Verify Plaid's Plaid-Verification JWT (ES256) and bind it to this body."""
    import hashlib
    import time

    import jwt  # PyJWT

    header = jwt.get_unverified_header(jwt_body)
    key_id = header.get("kid")
    if not key_id:
        raise PlaidError("invalid_webhook", "Webhook token is missing its key id.")

    key_resp = _post("/webhook_verification_key/get", {"key_id": key_id})
    public_key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(key_resp["key"]))
    claims = jwt.decode(jwt_body, public_key, algorithms=["ES256"])

    expected = hashlib.sha256(raw_body).hexdigest()
    if not secrets.compare_digest(claims.get("request_body_sha256", ""), expected):
        raise PlaidError("invalid_webhook", "Webhook body does not match its signature.")
    if abs(time.time() - float(claims.get("iat", 0))) > 300:
        raise PlaidError("invalid_webhook", "Webhook token is stale.")
    return claims
