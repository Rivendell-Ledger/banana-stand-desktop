"""The Banana Stand — Phase 0 application shell."""
from __future__ import annotations

import calendar
import json
import logging
import math
import os
import secrets
import socket
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import httpx
from logging.handlers import RotatingFileHandler
from typing import Any

from markupsafe import Markup, escape
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware

from . import (alerts, bitcoin, budgets, categories, config, csv_import, db, forecast, goals,
               maintenance, market, money, networth, performance, plaid, plaid_sync, portfolio,
               recovery, recurring, reports, retirement, rules, security, settings_store, txns)

# ---------------------------------------------------------------- logging
logger = logging.getLogger("banana")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    # A windowed PyInstaller build has no stderr; StreamHandler(None) would raise
    # inside emit for every record.
    if sys.stderr is not None:
        _stdout = logging.StreamHandler()
        _stdout.setFormatter(_fmt)
        logger.addHandler(_stdout)
    try:
        _file = RotatingFileHandler(
            config.LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        _file.setFormatter(_fmt)
        logger.addHandler(_file)
    except OSError:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    applied = db.migrate()
    if applied:
        logger.info("applied migrations: %s", ", ".join(applied))
    # Desktop installs keep Plaid keys in the database, not a .env file.
    settings_store.plaid_bootstrap()
    try:
        for row in db.query_all("SELECT id FROM households"):
            networth.recalc(row["id"])
            txns.ensure_system_categories(row["id"])
    except Exception:  # noqa: BLE001
        logger.exception("net worth startup snapshot failed")
    logger.info("banana stand %s ready on %s", config.VERSION, config.DB_PATH)
    yield


app = FastAPI(title=config.APP_NAME, version=config.VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
# The frozen app cannot pick up edited templates; skip the per-render mtime checks.
templates.env.auto_reload = not getattr(sys, "frozen", False)
def _money_span(cents, **kwargs):
    """Every amount in the UI wears the same hook, so privacy mode can blur all of them."""
    return Markup(f'<span class="mny">{escape(money.format_money(cents, **kwargs))}</span>')


templates.env.filters["money"] = _money_span
templates.env.filters["money_plain"] = money.format_money  # for attributes/titles
templates.env.filters["pct"] = money.format_percent


def evaluate_alerts(household_id: int):
    from . import alerts as _alerts
    return _alerts.evaluate(household_id)


def _day_label(dt) -> str:
    """'Nov 24' — the percent-dash day directive is glibc-only and raises on Windows."""
    return f"{dt.strftime('%b')} {dt.day}"


def _date_label(value: str) -> str:
    try:
        return _day_label(datetime.strptime(value, "%Y-%m-%d"))
    except (TypeError, ValueError):
        return value


def _date_label_year(value: str) -> str:
    """'Nov 24, 2025' — for windows that cross a year boundary."""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        return f"{_day_label(dt)}, {dt.year}"
    except (TypeError, ValueError):
        return value


def _relative_date(value: str) -> str:
    """Human distance from today: 'today', 'in 5 days', '3 days ago'."""
    if not value:
        return ""
    try:
        target = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return str(value)
    delta = (target - date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 0 < delta < 31:
        return f"in {delta} days"
    if -31 < delta < 0:
        return f"{-delta} days ago"
    return _day_label(target)


def _local_dt(value: str) -> str:
    """UTC timestamps rendered in the household's timezone."""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value or "")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(ZoneInfo(config.TIMEZONE))
    hour = local.hour % 12 or 12
    return f"{_day_label(local)}, {hour}:{local.minute:02d} {local.strftime('%p')}"


templates.env.filters["datefmt"] = _date_label
templates.env.filters["datefmt_year"] = _date_label_year
templates.env.filters["localdt"] = _local_dt
templates.env.filters["datelong"] = lambda d: f"{d:%b} {d.day}, {d.year}"
templates.env.filters["reldate"] = _relative_date
templates.env.filters["usd"] = lambda v: money.format_money(int(round(float(v or 0) * 100)))
templates.env.filters["btc"] = lambda v: money.format_btc(v)


# ---------------------------------------------------------------- middleware
@app.middleware("http")
async def harden(request: Request, call_next):
    csrf = request.cookies.get("bs_csrf")
    issued = None
    if not csrf:
        csrf = security.new_csrf_token()
        issued = csrf
    request.state.csrf = csrf

    response = await call_next(request)

    if request.url.path.startswith("/static/") and response.status_code in (200, 304):
        if request.query_params.get("v"):
            # Every asset URL carries the app version, so this is safe forever.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=3600")

    if issued:
        response.set_cookie(
            "bs_csrf",
            issued,
            max_age=config.SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https://*.plaid.com; style-src 'self'; "
        "script-src 'self' https://cdn.plaid.com; font-src 'self'; "
        "connect-src 'self' https://cdn.plaid.com https://*.plaid.com; "
        "frame-src https://cdn.plaid.com https://*.plaid.com; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(), payment=()"
    if config.is_production():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------------------------------------------------------------- helpers
def _cookie_secure(request: Request) -> bool:
    """Secure whenever the browser reached us over TLS, proxied or not."""
    if config.is_production():
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def safe_next(value: str | None, fallback: str = "/transactions") -> str:
    """Only same-site absolute paths. Blocks //evil.com, /\\evil.com and control characters."""
    if not value:
        return fallback
    value = value.strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return fallback
    if any(ord(ch) < 0x20 for ch in value):
        return fallback
    return value


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def redirect(url: str, status: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status)


def set_session_cookie(response, request: Request, token: str) -> None:
    response.set_cookie(
        "bs_session",
        token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


_NO_USER = object()


def current_user(request: Request) -> dict[str, Any] | None:
    cached = getattr(request.state, "user_cached", _NO_USER)
    if cached is not _NO_USER:
        return cached
    token = request.cookies.get("bs_session")
    if not token:
        request.state.user_cached = None
        return None
    row = db.query_one(
        """
        SELECT s.id AS session_id, s.expires_at, u.id, u.email, u.display_name,
               u.household_id, h.name AS household_name, h.buffer_cents
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        JOIN households h ON h.id = u.household_id
        WHERE s.token_hash = ?
        """,
        (security.hash_token(token),),
    )
    if row is None:
        request.state.user_cached = None
        return None
    if row["expires_at"] <= db.utcnow():
        db.execute("DELETE FROM sessions WHERE id = ?", (row["session_id"],))
        request.state.user_cached = None
        return None
    user = dict(row)
    request.state.user_cached = user
    return user


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def has_users() -> bool:
    return db.count("users") > 0


def audit(action: str, request: Request, user: dict | None = None, **meta) -> None:
    try:
        db.execute(
            "INSERT INTO audit_log(household_id, user_id, action, meta_json, ip, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                (user or {}).get("household_id"),
                (user or {}).get("id"),
                action,
                json.dumps(meta, separators=(",", ":")),
                client_ip(request),
                db.utcnow(),
            ),
        )
    except Exception:  # audit must never break a request
        logger.exception("audit write failed for %s", action)


def csrf_ok(request: Request, token: str) -> bool:
    return security.constant_time_equal(request.cookies.get("bs_csrf", ""), token)


def render(request: Request, template: str, status_code: int = 200, **ctx):
    now = db.local_now()
    hour = now.hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    base = {
        "request": request,
        "app_name": config.APP_NAME,
        "version": config.VERSION,
        "csrf": getattr(request.state, "csrf", ""),
        "user": current_user(request),
        "now": now,
        "greeting": greeting,
        "year": now.year,
    }
    base.update(ctx)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def month_progress() -> dict[str, Any]:
    now = db.local_now()
    days = calendar.monthrange(now.year, now.month)[1]
    pct = round(now.day / days * 100)
    return {"pct": pct, "label": now.strftime("%B").upper(), "day": now.day, "days": days}


# ---------------------------------------------------------------- routes
def _refresh_btc(household_id: int) -> None:
    """Re-price BTC accounts before a page that shows dollars (cached 60s)."""
    try:
        bitcoin.recompute(household_id)
    except Exception:  # a price hiccup must never break a page
        logger.warning("bitcoin refresh failed", exc_info=True)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not has_users():
        return redirect("/setup")
    user = current_user(request)
    if user is None:
        return redirect("/login")
    _refresh_btc(user["household_id"])
    accounts = networth.list_accounts(user["household_id"])
    held = portfolio.holdings(user["household_id"])
    held += portfolio.account_value_rows(user["household_id"], held)
    return render(
        request,
        "home.html",
        accounts=len(accounts),
        transactions=db.count("transactions"),
        totals=networth.totals(accounts),
        change=networth.change_since(user["household_id"]),
        safe=budgets.safe_to_spend(user["household_id"]),
        next_bill=forecast.next_bill(user["household_id"]),
        held=held,
        portfolio_totals=portfolio.totals(held),
        alert_counts=alerts.counts(evaluate_alerts(user["household_id"])),
        pending_review=db.query_all(
            """SELECT t.id, t.merchant, t.amount_cents, t.posted_on, a.name AS account_name
               FROM transactions t JOIN accounts a ON a.id = t.account_id
               WHERE t.household_id = ? AND t.is_pending = 1 AND t.deleted_at IS NULL
               ORDER BY t.posted_on DESC, t.id DESC""",
            (user["household_id"],),
        ),
        month=month_progress(),
        spend_curve=budgets.month_spend_curve(user["household_id"]),
        upcoming=forecast.events(user["household_id"], date.today(),
                                 date.today() + timedelta(days=30))[:4],
        upcoming_days=30,
        setup_pending=not settings_store.get(user["household_id"], "desktop_setup_done", False),
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if has_users():
        return redirect("/login")
    return render(request, "setup.html")


@app.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    csrf: str = Form(""),
    household: str = Form("The Banana Stand"),
    name: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    password2: str = Form(...),
):
    if has_users():
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return render(request, "setup.html", status_code=400, error="Session expired. Try again.", form={"household": household, "name": name})

    name = " ".join(name.split())[:40]
    if not security.valid_name(name):
        return render(request, "setup.html", status_code=400,
                      error="Your name needs 2-40 characters: letters, digits, spaces, dots, dashes or apostrophes.",
                      form={"household": household, "name": name})
    # The UI sends only a name; API callers may still supply an explicit handle.
    email = security.normalize_login(email or name)
    if password != password2:
        return render(request, "setup.html", status_code=400, error="The two passwords don't match.", form={"household": household, "name": name})
    problem = security.password_problem(password)
    if problem:
        return render(request, "setup.html", status_code=400, error=problem, form={"household": household, "name": name})

    now = db.utcnow()
    with db.session_scope() as conn:
        cur = conn.execute(
            "INSERT INTO households(name, currency, timezone, buffer_cents, week_start, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (household.strip() or config.APP_NAME, "USD", config.TIMEZONE, 150000, 6, now),
        )
        household_id = cur.lastrowid
        conn.execute(
            "INSERT INTO users(household_id, email, display_name, password_hash, created_at)"
            " VALUES (?,?,?,?,?)",
            (household_id, email, name.strip(), security.hash_password(password), now),
        )

    audit("setup.completed", request, {"household_id": household_id, "id": None, "email": email})
    logger.info("household created: %s", household_id)
    return redirect("/login?created=1")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, created: int = 0):
    if not has_users():
        return redirect("/setup")
    if current_user(request):
        return redirect("/")
    return render(request, "login.html", created=bool(created))


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    csrf: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
):
    if not csrf_ok(request, csrf):
        return render(request, "login.html", status_code=400, error="Session expired. Try again.", email=email)
    email = security.normalize_login(email)
    ip = client_ip(request)

    if security.throttle.blocked(ip, email):
        wait = security.throttle.retry_after(ip, email)
        return render(
            request,
            "login.html",
            status_code=429,
            error=f"Too many attempts. Try again in {max(1, wait // 60)} minutes.",
            email=email,
        )

    row = db.query_one(
        "SELECT id, household_id, email, display_name, password_hash FROM users WHERE email = ?",
        (email,),
    )
    if row is None or not security.verify_password(row["password_hash"], password):
        security.throttle.fail(ip, email)
        audit("login.failed", request, {"id": row["id"] if row else None, "household_id": row["household_id"] if row else None}, email=email)
        return render(request, "login.html", status_code=401, error="Email or password is incorrect.", email=email)

    security.throttle.reset(ip, email)
    token = security.new_session_token()
    now_dt = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO sessions(id, user_id, token_hash, created_at, last_seen_at, expires_at, ip, user_agent)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            secrets.token_hex(16),
            row["id"],
            security.hash_token(token),
            now_dt.isoformat(timespec="seconds"),
            now_dt.isoformat(timespec="seconds"),
            (now_dt + timedelta(days=config.SESSION_DAYS)).isoformat(timespec="seconds"),
            ip,
            request.headers.get("user-agent", "")[:300],
        ),
    )
    db.execute("UPDATE users SET last_login_at = ?, failed_logins = 0 WHERE id = ?", (db.utcnow(), row["id"]))
    audit("login.success", request, dict(row))

    response = redirect("/setup/connect" if not settings_store.get(
        row["household_id"], "desktop_setup_done", False) else "/")
    set_session_cookie(response, request, token)
    return response


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    user = current_user(request)
    if not csrf_ok(request, csrf):
        # A cross-site POST must not be able to sign the browser out.
        return redirect("/" if user else "/login")
    token = request.cookies.get("bs_session")
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (security.hash_token(token),))
    if user:
        audit("logout", request, user)
    response = redirect("/login")
    response.delete_cookie("bs_session", path="/")
    return response


@app.get("/healthz")
def healthz():
    ok = True
    try:
        db.query_one("SELECT 1")
    except Exception:
        ok = False
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "app": "banana-stand",
            "desktop": True,
            "db": "ok" if ok else "error",
            "version": config.VERSION,
            "last_sync_at": None,
            "counts": {"users": db.count("users"), "accounts": db.count("accounts"), "transactions": db.count("transactions")},
        }
    )


# ---------------------------------------------------------------- accounts
def _require_or_redirect(request: Request):
    return current_user(request)


def _account_form_error(request, status: int, error: str, form: dict[str, str]):
    return render(
        request,
        "account_new.html",
        status_code=status,
        error=error,
        form=form,
        types=networth.ACCOUNT_TYPES,
    )


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, archived: int = 0):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    _refresh_btc(user["household_id"])
    accounts = networth.list_accounts(user["household_id"], include_archived=bool(archived))
    # Archived accounts stay visible when asked for, but never count toward the balance sheet.
    live = [a for a in accounts if not a["archived_at"]]
    assets = [a for a in live if not a["is_liability"]]
    liabilities = [a for a in live if a["is_liability"]]
    archived_accounts = [a for a in accounts if a["archived_at"]]
    archived_total = db.query_one(
        "SELECT COUNT(*) AS n FROM accounts WHERE household_id = ? AND archived_at IS NOT NULL",
        (user["household_id"],),
    )["n"]
    return render(
        request,
        "accounts.html",
        accounts=accounts,
        assets=assets,
        liabilities=liabilities,
        asset_groups=networth.group_by_institution(assets),
        liability_groups=networth.group_by_institution(liabilities),
        totals=networth.totals(live),
        archived_view=bool(archived),
        archived_accounts=archived_accounts,
        archived_total=archived_total,
    )


@app.get("/accounts/new", response_class=HTMLResponse)
def account_new_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    return render(request, "account_new.html", types=networth.ACCOUNT_TYPES, form={})


@app.post("/accounts", response_class=HTMLResponse)
def account_create(
    request: Request,
    csrf: str = Form(""),
    type: str = Form(""),
    name: str = Form(""),
    institution: str = Form(""),
    mask: str = Form(""),
    balance: str = Form(""),
    credit_limit: str = Form(""),
    include_net: str = Form(""),
    include_budget: str = Form(""),
    include_portfolio: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    form = {"type": type, "name": name, "institution": institution, "mask": mask,
            "balance": balance, "credit_limit": credit_limit, "include_net": include_net,
            "include_budget": include_budget, "include_portfolio": include_portfolio}
    if not csrf_ok(request, csrf):
        return _account_form_error(request, 400, "Session expired. Try again.", form)
    if type not in networth.TYPE_LABELS:
        return _account_form_error(request, 400, "Pick an account type.", form)
    if not name.strip():
        return _account_form_error(request, 400, "Give the account a name.", form)
    units_sats = None
    if networth.is_bitcoin(type):
        units_sats = money.parse_btc(balance)
        if units_sats is None:
            return _account_form_error(request, 400, "Enter a bitcoin amount, like 0.125.", form)
        price = bitcoin.price_cents()
        cents = bitcoin.usd_cents(units_sats, price) if price else 0
    else:
        cents = money.parse_money(balance)
        if cents is None:
            return _account_form_error(request, 400, "That balance doesn't look like a number.", form)
    limit_cents = None
    if credit_limit.strip():
        limit_cents = money.parse_money(credit_limit)
        if limit_cents is None or limit_cents < 0:
            return _account_form_error(request, 400, "That credit limit doesn't look right.", form)
    mask_digits = "".join(ch for ch in mask if ch.isdigit())[-4:]

    now = db.utcnow()
    today = date.today().isoformat()
    with db.session_scope() as conn:
        cur = conn.execute(
            """INSERT INTO accounts(household_id, institution, name, mask, type, currency,
                    current_balance_cents, credit_limit_cents, include_in_net_worth,
                    include_in_budget, include_in_portfolio, is_manual, manual_balance_as_of,
                    created_at, updated_at, units_sats)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user["household_id"], institution.strip() or None, name.strip(), mask_digits or None,
             type, "USD", cents, limit_cents, 1 if include_net else 0,
             1 if include_budget and networth.TYPE_GROUPS[type] == "asset" else 0,
             1 if include_portfolio and networth.TYPE_GROUPS[type] == "asset" else 0,
             1, today, now, now, units_sats),
        )
        account_id = cur.lastrowid
        conn.execute(
            "INSERT INTO account_balances(account_id, as_of_date, balance_cents, available_cents, source, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (account_id, today, cents, None, "manual", now),
        )
    networth.recalc(user["household_id"])
    audit("account.created", request, user, account_id=account_id, type=type)
    return redirect(f"/accounts/{account_id}?created=1")


@app.get("/accounts/credit-cards", response_class=HTMLResponse)
def credit_cards_page(request: Request):
    """Must be declared before /accounts/{account_id}, or the int path swallows it."""
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    return render(request, "credit_cards.html",
                  cc=performance.credit_cards(user["household_id"]))


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(request: Request, account_id: int, created: int = 0, archived: int = 0):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    _refresh_btc(user["household_id"])
    account = networth.get_account(user["household_id"], account_id)
    if account is None:
        raise HTTPException(status_code=404)
    history = db.query_all(
        "SELECT as_of_date, balance_cents, source FROM account_balances"
        " WHERE account_id = ? ORDER BY as_of_date DESC LIMIT 24",
        (account_id,),
    )
    return render(
        request,
        "account_detail.html",
        account=account,
        history=[dict(r) for r in history],
        created=bool(created),
        archived_view=bool(archived),
        types=networth.ACCOUNT_TYPES,
        btc_price=bitcoin.price_cents() if account.get("units_sats") is not None else None,
    )


@app.post("/accounts/{account_id}/balance")
def account_set_balance(
    request: Request,
    account_id: int,
    csrf: str = Form(""),
    balance: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    account = networth.get_account(user["household_id"], account_id)
    if account is None:
        raise HTTPException(status_code=404)
    if not csrf_ok(request, csrf):
        return redirect(f"/accounts/{account_id}?error=balance")
    today = date.today().isoformat()
    if networth.is_bitcoin(account["type"]):
        units_sats = money.parse_btc(balance)
        if units_sats is None:
            return redirect(f"/accounts/{account_id}?error=balance")
        price = bitcoin.price_cents()
        cents = bitcoin.usd_cents(units_sats, price) if price else 0
        db.execute(
            "UPDATE accounts SET current_balance_cents = ?, units_sats = ?, manual_balance_as_of = ?,"
            " updated_at = ? WHERE id = ?",
            (cents, units_sats, today, db.utcnow(), account_id),
        )
    else:
        cents = money.parse_money(balance)
        if cents is None:
            return redirect(f"/accounts/{account_id}?error=balance")
        db.execute(
            "UPDATE accounts SET current_balance_cents = ?, manual_balance_as_of = ?, updated_at = ?"
            " WHERE id = ?",
            (cents, today, db.utcnow(), account_id),
        )
    networth.record_balance(account_id, cents, today)
    networth.recalc(user["household_id"])
    audit("account.balance", request, user, account_id=account_id, cents=cents)
    return redirect(f"/accounts/{account_id}")


@app.post("/accounts/{account_id}/edit")
def account_edit(
    request: Request,
    account_id: int,
    csrf: str = Form(""),
    name: str = Form(""),
    institution: str = Form(""),
    mask: str = Form(""),
    type: str = Form(""),
    credit_limit: str = Form(""),
    include_net: str = Form(""),
    include_budget: str = Form(""),
    include_portfolio: str = Form(""),
    tax_treatment: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    account = networth.get_account(user["household_id"], account_id)
    if account is None:
        raise HTTPException(status_code=404)
    if not csrf_ok(request, csrf):
        return redirect(f"/accounts/{account_id}")
    if not name.strip() or type not in networth.TYPE_LABELS:
        return redirect(f"/accounts/{account_id}?error=fields")
    limit_cents = money.parse_money(credit_limit) if credit_limit.strip() else None
    if credit_limit.strip() and (limit_cents is None or limit_cents < 0):
        return redirect(f"/accounts/{account_id}?error=fields")
    # A BTC account's truth is its satoshi quantity. Switching an existing account
    # into Bitcoin preserves today's dollar value by buying sats at the live price.
    units_sats = account.get("units_sats")
    cents = account["current_balance_cents"]
    if networth.is_bitcoin(type):
        price = bitcoin.price_cents()
        if not units_sats and price:
            units_sats = bitcoin.sats_for_cents(cents, price)
        if units_sats and price:
            cents = bitcoin.usd_cents(units_sats, price)
    else:
        units_sats = None
    tax = tax_treatment if tax_treatment in retirement.TAX_TREATMENTS else ""
    db.execute(
        """UPDATE accounts SET name = ?, institution = ?, mask = ?, type = ?, credit_limit_cents = ?,
               include_in_net_worth = ?, include_in_budget = ?, include_in_portfolio = ?,
               current_balance_cents = ?, units_sats = ?, tax_treatment = ?, updated_at = ?
               WHERE id = ?""",
        (name.strip(), institution.strip() or None,
         ("".join(ch for ch in mask if ch.isdigit())[-4:] or None), type, limit_cents,
         1 if include_net else 0,
         1 if include_budget and networth.TYPE_GROUPS[type] == "asset" else 0,
         1 if include_portfolio and networth.TYPE_GROUPS[type] == "asset" else 0,
         cents, units_sats, tax, db.utcnow(), account_id),
    )
    networth.recalc(user["household_id"])
    audit("account.edited", request, user, account_id=account_id)
    return redirect(f"/accounts/{account_id}")


@app.post("/accounts/{account_id}/archive")
def account_archive(request: Request, account_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/accounts/{account_id}")
    db.execute("UPDATE accounts SET archived_at = ?, updated_at = ? WHERE id = ? AND household_id = ?",
               (db.utcnow(), db.utcnow(), account_id, user["household_id"]))
    networth.recalc(user["household_id"])
    audit("account.archived", request, user, account_id=account_id)
    return redirect("/accounts")


@app.post("/accounts/{account_id}/restore")
def account_restore(request: Request, account_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/accounts?archived=1")
    db.execute("UPDATE accounts SET archived_at = NULL, updated_at = ? WHERE id = ? AND household_id = ?",
               (db.utcnow(), account_id, user["household_id"]))
    networth.recalc(user["household_id"])
    audit("account.restored", request, user, account_id=account_id)
    return redirect(f"/accounts/{account_id}")


@app.get("/net-worth", response_class=HTMLResponse)
def net_worth_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    _refresh_btc(user["household_id"])
    accounts = networth.list_accounts(user["household_id"])
    # The chosen frame sticks: a click writes the cookie, a plain nav reads it back.
    frame = (request.query_params.get("range") or request.cookies.get("nw_range") or "30d").lower()
    win = networth.window(user["household_id"], frame)
    rows = win["rows"] if win else []
    response = render(
        request,
        "net_worth.html",
        accounts=accounts,
        groups=networth.group_by_institution(accounts),
        totals=networth.totals(accounts),
        series=rows,
        change=win,
        ranges=networth.RANGES,
        range_key=win["key"] if win else "30d",
        chart=networth.sparkline_points(rows),
        charts=networth.chart_geometry(rows) if len(rows) > 1 else {},
        chart_data=networth.chart_payload(rows),
        first=rows[0] if rows else None,
    )
    response.set_cookie("nw_range", win["key"] if win else "30d", max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="lax")
    return response



# ---------------------------------------------------------------- transactions
def _tx_filters(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("q", "account_id", "category_id", "tag", "from", "to", "pending", "kind"):
        value = (request.query_params.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def _qs(filters: dict[str, str], **extra) -> str:
    parts = list(filters.items()) + [(k, v) for k, v in extra.items() if v not in (None, "")]
    return urlencode(parts)


def _wants_json(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request, page: int = 0, added: int = 0):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    txns.ensure_system_categories(hid)
    filters = _tx_filters(request)
    accounts = networth.list_accounts(hid)
    spend_group = (request.query_params.get("group") or "auto").strip()
    return render(
        request,
        "transactions.html",
        filters=filters,
        # Two-sided cash flow for every filter that can contain income — a filter narrows
        # the bars, never the chart's shape. Only a Transfers filter hides the card.
        spend=txns.spend_series(hid, filters, spend_group,
                                split_income=filters.get("kind") != "transfer"),
        spend_group=spend_group,
        result=txns.list_transactions(hid, filters, page=max(0, page)),
        summary=txns.summary(hid, filters),
        categories=txns.list_categories(hid),
        filter_categories=txns.categories_in_use(hid),
        accounts=accounts,
        tags=txns.list_tags(hid),
        qs=_qs(filters),
        today=txns.today_iso(),
        added=bool(added),
        default_account=accounts[0]["id"] if accounts else None,
    )


@app.post("/transactions")
def transaction_create(
    request: Request,
    csrf: str = Form(""),
    account_id: str = Form(""),
    posted_on: str = Form(""),
    direction: str = Form("out"),
    amount: str = Form(""),
    merchant: str = Form(""),
    category_id: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/transactions?error=csrf")
    hid = user["household_id"]
    account = networth.get_account(hid, int(account_id)) if account_id.isdigit() else None
    if account is None:
        return redirect("/transactions?error=account")
    cents = money.parse_money(amount)
    if cents is None or cents == 0 or not merchant.strip():
        return redirect("/transactions?error=fields")
    try:
        day = date.fromisoformat(posted_on).isoformat()
    except ValueError:
        day = txns.today_iso()
    signed = -abs(cents) if direction != "in" else abs(cents)
    cat = int(category_id) if category_id.isdigit() else None
    txn_id = txns.create(hid, account["id"], day, signed, merchant, category_id=cat)
    audit("txn.created", request, user, txn_id=txn_id, cents=signed)
    return redirect("/transactions?added=1")


@app.post("/transactions/bulk")
def transactions_bulk(
    request: Request,
    csrf: str = Form(""),
    ids: list[str] = Form([]),
    action: str = Form(""),
    op: str = Form(""),
    value: str = Form(""),
    next: str = Form("/transactions"),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/transactions?error=csrf")
    clean_ids = [int(i) for i in ids if str(i).isdigit()]
    action = (op or action).strip()
    count = txns.bulk(user["household_id"], clean_ids, action, value or None)
    audit("txn.bulk", request, user, op=action, count=count)
    target = safe_next(next, "/transactions")
    extra = ""
    if action == "delete" and count:
        # Hand back the ids so the banner can offer a real undo.
        keep = ",".join(str(i) for i in clean_ids[:200])
        extra = f"&undo={count}&ids={keep}"
    return redirect(f"{target}{'&' if '?' in target else '?'}bulk={count}{extra}")


@app.post("/transactions/review-all")
def transactions_review_all(request: Request, csrf: str = Form(""), next: str = Form("")):
    """Clear the whole pending-review queue in one tap."""
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/?error=csrf")
    hid = user["household_id"]
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM transactions WHERE household_id = ?"
        " AND is_pending = 1 AND deleted_at IS NULL", (hid,))
    waiting = int(row["n"] if row else 0)
    if waiting:
        db.execute(
            "UPDATE transactions SET is_pending = 0, updated_at = ?"
            " WHERE household_id = ? AND is_pending = 1 AND deleted_at IS NULL",
            (db.utcnow(), hid))
        audit("transactions.reviewed_all", request, user)
    if next.startswith("/") and not next.startswith("//"):
        return redirect(next)
    return redirect("/")


@app.post("/transactions/{txn_id}/posted")
def transaction_mark_posted(request: Request, txn_id: int, csrf: str = Form(""),
                            next: str = Form("")):
    """Reviewing a pending charge ends with one tap: it is no longer pending."""
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/transactions/{txn_id}?error=csrf")
    db.execute(
        "UPDATE transactions SET is_pending = 0, updated_at = ? WHERE id = ? AND household_id = ?",
        (db.utcnow(), txn_id, user["household_id"]))
    if next.startswith("/") and not next.startswith("//"):
        return redirect(next)
    return redirect(f"/transactions/{txn_id}?saved=1")


@app.post("/transactions/{txn_id}/update")
def transaction_update(
    request: Request,
    txn_id: int,
    csrf: str = Form(""),
    merchant: str = Form(""),
    posted_on: str = Form(""),
    amount: str = Form(""),
    category_id: str = Form(""),
    notes: str = Form(""),
    is_pending: str = Form(""),
    excluded: str = Form(""),
    is_transfer: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    if txns.get_transaction(hid, txn_id) is None:
        raise HTTPException(status_code=404)
    if not csrf_ok(request, csrf):
        return redirect(f"/transactions/{txn_id}")
    cents = money.parse_money(amount)
    if cents is None or not merchant.strip():
        return redirect(f"/transactions/{txn_id}?error=fields")
    try:
        day = date.fromisoformat(posted_on).isoformat()
    except ValueError:
        return redirect(f"/transactions/{txn_id}?error=fields")
    has_splits = db.query_one(
        "SELECT COUNT(*) AS n FROM transaction_splits WHERE transaction_id = ?", (txn_id,)
    )["n"] > 0
    db.execute(
        """UPDATE transactions SET merchant = ?, posted_on = ?, amount_cents = ?, category_id = ?,
               notes = ?, is_pending = ?, excluded = ?, is_transfer = ?, updated_at = ?
           WHERE id = ? AND household_id = ?""",
        (merchant.strip()[:120], day, cents,
         None if has_splits else (int(category_id) if category_id.isdigit() else None),
         notes.strip()[:500] or None, 1 if is_pending else 0, 1 if excluded else 0,
         1 if is_transfer else 0, db.utcnow(), txn_id, hid),
    )
    if category_id.isdigit() and not has_splits:
        db.execute("UPDATE transactions SET category_source = 'manual' WHERE id = ? AND household_id = ?",
                   (txn_id, hid))
    audit("txn.updated", request, user, txn_id=txn_id)
    return redirect(f"/transactions/{txn_id}?saved=1")


@app.post("/transactions/{txn_id}/field")
def transaction_field(
    request: Request,
    txn_id: int,
    csrf: str = Form(""),
    field: str = Form(""),
    value: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    txn = txns.get_transaction(hid, txn_id)
    if txn is None:
        raise HTTPException(status_code=404)
    if not csrf_ok(request, csrf):
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": "Session expired."}, status_code=400)
        return redirect(f"/transactions/{txn_id}")

    display: str | None = None
    kind = None
    if field == "merchant":
        value = value.strip()[:120]
        if not value:
            return JSONResponse({"ok": False, "error": "Merchant cannot be empty."}, status_code=400)
        display = value
    elif field == "notes":
        value = value.strip()[:500] or None
        display = value or ""
    elif field == "posted_on":
        try:
            value = date.fromisoformat(value).isoformat()
        except ValueError:
            return JSONResponse({"ok": False, "error": "Use YYYY-MM-DD."}, status_code=400)
        display = _date_label(value)
    elif field == "amount_cents":
        cents = money.parse_money(value)
        if cents is None:
            return JSONResponse({"ok": False, "error": "Not a number."}, status_code=400)
        value = cents
        display = money.format_money(cents)
        kind = "pos" if cents > 0 else "neg"
    elif field == "category_id":
        if value in ("", "none"):
            value = None
            display = "Uncategorized"
        elif value.isdigit():
            cats = {c["id"]: c for c in txns.list_categories(hid)}
            if int(value) not in cats:
                return JSONResponse({"ok": False, "error": "Unknown category."}, status_code=400)
            value = int(value)
            display = cats[value]["name"]
    elif field in ("is_pending", "excluded", "is_transfer"):
        value = 1 if value == "1" else 0
        display = "Yes" if value else "No"
    else:
        return JSONResponse({"ok": False, "error": "Field not editable."}, status_code=400)

    txns.update_field(hid, txn_id, field, value)
    if field == "category_id":
        db.execute("UPDATE transactions SET category_source = 'manual' WHERE id = ? AND household_id = ?",
                   (txn_id, hid))
    audit("txn.edited", request, user, txn_id=txn_id, field=field)
    if _wants_json(request):
        return JSONResponse({"ok": True, "display": display, "kind": kind})
    return redirect(f"/transactions/{txn_id}")


@app.get("/transactions/{txn_id}", response_class=HTMLResponse)
def transaction_detail(request: Request, txn_id: int):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    txn = txns.get_transaction(hid, txn_id)
    if txn is None:
        raise HTTPException(status_code=404)
    return render(
        request,
        "transaction_detail.html",
        txn=txn,
        categories=txns.list_categories(hid),
        split_total=sum(s["amount_cents"] for s in txn["splits"]),
    )


@app.post("/transactions/{txn_id}/tags")
def transaction_tags(
    request: Request, txn_id: int, csrf: str = Form(""), tags: str = Form("")
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/transactions/{txn_id}")
    if txns.get_transaction(user["household_id"], txn_id) is None:
        raise HTTPException(status_code=404)
    txns.set_tags(user["household_id"], txn_id, tags.split(","))
    audit("txn.tagged", request, user, txn_id=txn_id)
    return redirect(f"/transactions/{txn_id}")


@app.post("/transactions/{txn_id}/splits")
def transaction_splits(
    request: Request,
    txn_id: int,
    csrf: str = Form(""),
    split_category: list[str] = Form([]),
    split_amount: list[str] = Form([]),
    split_note: list[str] = Form([]),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/transactions/{txn_id}")
    hid = user["household_id"]
    rows = []
    for i, raw in enumerate(split_amount):
        cents = money.parse_money(raw)
        cat = split_category[i] if i < len(split_category) else ""
        note = split_note[i] if i < len(split_note) else ""
        if cents is None and not (cat or note):
            continue  # blank row
        rows.append({"category_id": int(cat) if cat.isdigit() else None,
                     "amount_cents": cents, "note": note})
    try:
        txns.set_splits(hid, txn_id, rows)
    except txns.SplitError as exc:
        return redirect(f"/transactions/{txn_id}?splits={str(exc).replace(' ', '+')}")
    audit("txn.split", request, user, txn_id=txn_id, lines=len(rows))
    return redirect(f"/transactions/{txn_id}?saved=1")


@app.post("/transactions/{txn_id}/splits/clear")
def transaction_splits_clear(
    request: Request, txn_id: int, csrf: str = Form(""), category_id: str = Form("")
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/transactions/{txn_id}")
    txns.clear_splits(user["household_id"], txn_id, int(category_id) if category_id.isdigit() else None)
    return redirect(f"/transactions/{txn_id}?saved=1")


@app.post("/transactions/{txn_id}/delete")
def transaction_delete(request: Request, txn_id: int, csrf: str = Form(""), next: str = Form("/transactions")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/transactions")
    txns.soft_delete(user["household_id"], txn_id)
    audit("txn.deleted", request, user, txn_id=txn_id)
    target = safe_next(next, "/transactions")
    return redirect(f"{target}{'&' if '?' in target else '?'}deleted={txn_id}")


@app.post("/transactions/{txn_id}/restore")
def transaction_restore(request: Request, txn_id: int, csrf: str = Form(""), next: str = Form("/trash")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/trash")
    txns.restore(user["household_id"], txn_id)
    audit("txn.restored", request, user, txn_id=txn_id)
    return redirect(safe_next(next, "/trash"))


@app.get("/api/categories")
def api_categories(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse(
        {"ok": True,
         "categories": [{"id": c["id"], "name": c["name"]} for c in txns.list_categories(user["household_id"])]}
    )


@app.get("/trash", response_class=HTMLResponse)
def trash_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    return render(request, "trash.html", rows=txns.list_deleted(user["household_id"]))



# ---------------------------------------------------------------- settings & Plaid
def _setting_get(household_id: int, key: str, default=None):
    row = db.query_one(
        "SELECT value_json FROM settings WHERE household_id = ? AND key = ?", (household_id, key)
    )
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except ValueError:
        return default


def _setting_set(household_id: int, key: str, value) -> None:
    db.execute(
        "INSERT INTO settings(household_id, key, value_json) VALUES (?,?,?)"
        " ON CONFLICT(household_id, key) DO UPDATE SET value_json = excluded.value_json",
        (household_id, key, json.dumps(value)),
    )


def _plaid_items(household_id: int) -> list[dict]:
    rows = db.query_all(
        """SELECT i.*, (SELECT COUNT(*) FROM accounts a
                          WHERE a.plaid_account_id IS NOT NULL
                            AND a.plaid_item_id = i.plaid_item_id) AS account_count,
                         (SELECT COUNT(*) FROM holdings h JOIN accounts a2 ON a2.id = h.account_id
                          WHERE a2.plaid_item_id = i.plaid_item_id AND h.source = 'plaid') AS holdings_count,
                         (SELECT COUNT(*) FROM accounts a3
                          WHERE a3.plaid_item_id = i.plaid_item_id
                            AND a3.type IN ('brokerage','retirement','hsa','other_asset')) AS investment_count
           FROM plaid_items i WHERE i.household_id = ? ORDER BY i.created_at""",
        (household_id,),
    )
    return [dict(r) for r in rows]


@app.get("/setup/connect", response_class=HTMLResponse)
def setup_connect_page(request: Request, saved: int = 0, connected: int = 0, error: str = ""):
    if not has_users():
        return redirect("/setup")
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    return render(
        request,
        "setup_connect.html",
        saved=bool(saved),
        connected=bool(connected),
        error=error,
        plaid_ready=plaid.configured(),
        plaid_env=plaid.env_name(),
        slots_used=_slots_used(user["household_id"]),
        slots_max=plaid.max_items(),
    )


def _slots_used(household_id: int) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM plaid_items WHERE household_id = ?", (household_id,))
    return int(row["n"]) if row else 0


def _plaid_cap(value: str, default: int = 10) -> int:
    """Item slots: keep a typo from disabling every Connect button (0)."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if 1 <= number <= 200 else default


def _plaid_env(value: str, default: str = "production") -> str:
    value = (value or "").strip().lower()
    return value if value in {"sandbox", "production"} else default


@app.post("/setup/plaid", response_class=HTMLResponse)
def setup_plaid(
    request: Request,
    csrf: str = Form(""),
    client_id: str = Form(""),
    secret: str = Form(""),
    environment: str = Form("production"),
    max_items: str = Form(""),
    ack: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    client_id = client_id.strip()
    secret = secret.strip()
    environment = (environment or "production").strip().lower()
    cap = _plaid_cap(max_items)
    stored = settings_store.stored_credentials(hid)
    linked = _slots_used(hid)
    rekeying = (client_id != (stored["client_id"] or "")
                or environment != (stored["env"] or "production"))
    problem = ""
    if not csrf_ok(request, csrf):
        problem = "Session expired. Try again."
    elif not client_id or not secret:
        problem = "Both the client ID and the secret are required."
    elif environment not in {"sandbox", "production"}:
        problem = "Choose production or sandbox."
    elif linked and rekeying:
        # The wizard form is hidden once keys exist, but a stale tab or a direct POST
        # must not silently orphan every stored access token.
        problem = ("You already have linked banks. Change keys in Settings → Plaid credentials, "
                   "where the app warns you that they must be linked again.")
    elif environment == "production" and ack != "1":
        problem = "Tick the real-connections box to continue."
    else:
        ok, message = settings_store.test_credentials(client_id, secret, environment, cap)
        if ok:
            settings_store.save_credentials(hid, client_id, secret, environment, cap)
            _LINK_TOKENS.clear()  # a cached Link token may belong to the old environment
            if environment == "production":
                settings_store.set(hid, "plaid_production_ack", True)
            audit("plaid.credentials", request, user, env=environment)
            return redirect("/setup/connect?saved=1")
        problem = message
    return render(
        request, "setup_connect.html", status_code=400,
        saved=False, error=problem, plaid_ready=plaid.configured(),
        plaid_env=environment, slots_used=_slots_used(hid), slots_max=plaid.max_items(),
        form={"client_id": client_id, "max_items": max_items},
    )


@app.post("/setup/connect/done")
def setup_connect_done(request: Request, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/setup/connect")
    return redirect("/setup/recovery")


@app.get("/setup/recovery", response_class=HTMLResponse)
def setup_recovery_page(request: Request, error: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    return render(request, "setup_recovery.html", error=error,
                  has_key=recovery.has_key(), min_pass=recovery.MIN_PASSPHRASE)


@app.post("/setup/recovery/export")
def setup_recovery_export(
    request: Request,
    csrf: str = Form(""),
    passphrase: str = Form(""),
    confirm: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/setup/recovery")
    if len(passphrase) < recovery.MIN_PASSPHRASE:
        return redirect(f"/setup/recovery?error=Use+at+least+{recovery.MIN_PASSPHRASE}+characters.")
    if passphrase != confirm:
        return redirect("/setup/recovery?error=The+passphrases+do+not+match.")
    try:
        content = recovery.export(passphrase, user["household_id"])
    except ValueError as exc:
        return redirect(f"/setup/recovery?error={quote(str(exc))}")
    settings_store.set(user["household_id"], "recovery_kit_exported_at", db.utcnow())
    audit("recovery.export", request, user)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="banana-stand-recovery-kit.txt"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/setup/recovery/done")
def setup_recovery_done(request: Request, csrf: str = Form(""), ack: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    if not csrf_ok(request, csrf):
        return redirect("/setup/recovery")
    exported = settings_store.get(hid, "recovery_kit_exported_at")
    if not exported and ack != "1":
        return redirect("/setup/recovery?error=Download+a+kit+or+tick+the+skip+box.")
    settings_store.set(hid, "desktop_setup_done", True)
    return redirect("/")


@app.get("/settings/plaid", response_class=HTMLResponse)
def plaid_settings_page(request: Request, saved: int = 0, error: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    stored = settings_store.stored_credentials(user["household_id"])
    return render(
        request, "plaid_settings.html", saved=bool(saved), error=error,
        stored=stored, plaid_ready=plaid.configured(), plaid_env=plaid.env_name(),
        slots_used=_slots_used(user["household_id"]), slots_max=plaid.max_items(),
    )


@app.post("/settings/plaid", response_class=HTMLResponse)
def plaid_settings_save(
    request: Request,
    csrf: str = Form(""),
    client_id: str = Form(""),
    secret: str = Form(""),
    environment: str = Form("production"),
    max_items: str = Form(""),
    ack: str = Form(""),
    confirm_rekey: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings/plaid?error=Session+expired.+Try+again.")
    hid = user["household_id"]
    stored = settings_store.stored_credentials(hid)
    client_id = client_id.strip() or stored["client_id"]
    secret = secret.strip() or stored["secret"]
    environment = _plaid_env(environment, stored["env"] or "production")
    cap = _plaid_cap(max_items, stored["max_items"] or 10)
    linked = _slots_used(hid)
    keys_changed = (client_id != (stored["client_id"] or "")
                    or environment != (stored["env"] or "production"))
    if linked and keys_changed and confirm_rekey != "1":
        return redirect(
            "/settings/plaid?error=You+have+linked+banks.+Changing+the+client+ID+or+"
            "environment+breaks+them.+Tick+the+re-link+box+to+continue."
        )
    if environment == "production" and ack != "1" and not settings_store.get(
            hid, "plaid_production_ack", False):
        return redirect("/settings/plaid?error=Tick+the+real-connections+box+to+continue.")
    ok, message = settings_store.test_credentials(client_id, secret, environment, cap)
    if not ok:
        return redirect(f"/settings/plaid?error={quote(message)}")
    settings_store.save_credentials(hid, client_id, secret, environment, cap)
    _LINK_TOKENS.clear()  # a cached Link token may belong to the old environment
    if environment == "production":
        settings_store.set(hid, "plaid_production_ack", True)
    audit("plaid.credentials", request, user, env=environment)
    return redirect("/settings/plaid?saved=1")


@app.get("/settings/recovery", response_class=HTMLResponse)
def recovery_page(request: Request, restored: int = 0, error: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    return render(request, "recovery_settings.html", restored=bool(restored), error=error,
                  has_key=recovery.has_key(), min_pass=recovery.MIN_PASSPHRASE)


@app.post("/settings/recovery/export")
def recovery_export(
    request: Request,
    csrf: str = Form(""),
    passphrase: str = Form(""),
    confirm: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings/recovery")
    if len(passphrase) < recovery.MIN_PASSPHRASE:
        return redirect(f"/settings/recovery?error=Use+at+least+{recovery.MIN_PASSPHRASE}+characters.")
    if passphrase != confirm:
        return redirect("/settings/recovery?error=The+passphrases+do+not+match.")
    audit("recovery.export", request, user)
    try:
        content = recovery.export(passphrase, user["household_id"])
    except ValueError as exc:
        return redirect(f"/settings/recovery?error={quote(str(exc))}")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="banana-stand-recovery-kit.txt"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/settings/recovery/import")
def recovery_import(
    request: Request,
    csrf: str = Form(""),
    passphrase: str = Form(""),
    kit: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings/recovery?error=Session+expired.+Try+again.")
    try:
        recovery.import_kit(kit, passphrase, user["household_id"])
    except (ValueError, OSError) as exc:
        return redirect(f"/settings/recovery?error={quote(str(exc))}")
    _LINK_TOKENS.clear()  # restored credentials may belong to a different environment
    audit("recovery.import", request, user)
    return redirect("/settings/recovery?restored=1")


@app.get("/settings/desktop", response_class=HTMLResponse)
def desktop_settings_page(request: Request, saved: int = 0, check: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    lan = bool(settings_store.get(user["household_id"], "desktop_lan", False))
    port = request.url.port or 8093
    candidates = _lan_candidates()
    primary = candidates[0] if candidates else ""
    return render(request, "desktop_settings.html", lan=lan, saved=bool(saved),
                  lan_url=f"http://{primary}:{port}" if primary else "",
                  alt_urls=[f"http://{ip}:{port}" for ip in candidates[1:4]],
                  has_address=bool(primary),
                  lan_active=os.environ.get("BANANA_LAN_ACTIVE") == "1",
                  check=check, port=port)


@app.get("/settings/desktop/check")
def desktop_settings_check(request: Request):
    """Ask this computer to fetch the address shown on the page.

    Success means the server is bound to the network and the address is right.
    A phone that still cannot connect is then a firewall or Wi-Fi problem, not
    an app problem.
    """
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not settings_store.get(user["household_id"], "desktop_lan", False):
        return redirect("/settings/desktop?check=off")
    port = request.url.port or 8093
    ip = _lan_ip()
    if not ip:
        return redirect("/settings/desktop?check=noip")
    url = f"http://{ip}:{port}/healthz"
    try:
        ok = httpx.get(url, timeout=2.5).status_code == 200
    except httpx.HTTPError:
        ok = False
    return redirect("/settings/desktop?check=" + ("ok" if ok else "fail"))


@app.post("/settings/desktop")
def desktop_settings_save(request: Request, csrf: str = Form(""), lan: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings/desktop")
    settings_store.set(user["household_id"], "desktop_lan", lan == "1")
    audit("desktop.lan", request, user, enabled=lan == "1")
    return redirect("/settings/desktop?saved=1")


def _private_v4(ip: str) -> bool:
    try:
        first, second = (int(part) for part in ip.split(".")[:2])
    except (ValueError, IndexError):
        return False
    if first == 10:
        return True
    if first == 192 and second == 168:
        return True
    return first == 172 and 16 <= second <= 31


def _lan_candidates() -> list[str]:
    """Private IPv4 addresses, best first.

    The address the OS would use for an outside route is normally the Wi-Fi or
    Ethernet adapter the phone shares. getaddrinfo can list Hyper-V, WSL, or
    VirtualBox adapters first, so they go after it rather than ahead of it.
    """
    found: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))  # TEST-NET; no packet is sent
        route_ip = sock.getsockname()[0]
    except OSError:
        route_ip = ""
    finally:
        sock.close()
    if _private_v4(route_ip):
        found.append(route_ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _private_v4(ip) and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def _lan_ip() -> str:
    candidates = _lan_candidates()
    return candidates[0] if candidates else ""


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, linked: int = 0, error: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    items = _plaid_items(hid)
    if linked and not settings_store.get(hid, "desktop_setup_done", False):
        # The wizard's Connect button lands here; bring the user back to the wizard.
        return redirect("/setup/connect?connected=1")
    return render(
        request,
        "settings.html",
        items=items,
        plaid_ready=plaid.configured(),
        plaid_env=plaid.env_name(),
        plaid_max=plaid.max_items(),
        slots_used=len(items),
        production_ack=bool(_setting_get(hid, "plaid_production_ack", False)),
        linked=bool(linked),
        error=error,
        accounts=networth.list_accounts(hid),
        backups=maintenance.backup_status(),
        household=db.query_one("SELECT name, buffer_cents FROM households WHERE id = ?", (hid,)),
        saved=request.query_params.get("saved"),
    )


@app.post("/settings/plaid/ack")
def settings_plaid_ack(request: Request, csrf: str = Form(""), ack: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings?error=csrf")
    _setting_set(user["household_id"], "plaid_production_ack", bool(ack))
    audit("plaid.ack", request, user, ack=bool(ack))
    return redirect("/settings")


# link token cache: key -> (token, expiry epoch). Cleared per process; 10 minutes is
# well inside Plaid's 30-minute token life.
_LINK_TOKENS: dict[str, tuple[str, float]] = {}


@app.post("/plaid/link-token")
def plaid_link_token(request: Request, csrf: str = Form(""), mode: str = Form("new"), item_id: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return JSONResponse({"ok": False, "error": "Signed out."}, status_code=401)
    if not csrf_ok(request, csrf):
        return JSONResponse({"ok": False, "error": "Session expired."}, status_code=400)
    if not plaid.configured():
        return JSONResponse({"ok": False, "error": "Plaid is not configured."}, status_code=400)

    hid = user["household_id"]
    if mode in ("update", "consent"):
        row = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?",
                           (int(item_id) if item_id.isdigit() else 0, hid))
        if row is None:
            return JSONResponse({"ok": False, "error": "Unknown connection."}, status_code=404)
    else:
        if plaid.env_name() == "production" and not _setting_get(hid, "plaid_production_ack", False):
            return JSONResponse({"ok": False, "error": "Confirm the production warning in Settings first."}, status_code=400)
        if len(_plaid_items(hid)) >= plaid.max_items():
            return JSONResponse({"ok": False, "error": "No Plaid Item slots left."}, status_code=400)

    # Plaid rate-limits repeated Link attempts, and a handed-out token stays valid for
    # 30 minutes, so reuse one for ten minutes instead of minting a fresh token per click.
    key = f"{hid}:{mode}:{item_id if mode in ('update', 'consent') else ''}"
    now = time.time()
    cached = _LINK_TOKENS.get(key)
    if cached and cached[1] > now:
        return JSONResponse({"ok": True, "link_token": cached[0], "env": plaid.env_name(), "cached": True})

    try:
        if mode in ("update", "consent"):
            token = plaid.decrypt_token(row["access_token_enc"])
            # Consent mode opens update mode purely to collect permission for a product
            # the Item was not linked with (investments), so Holdings can be pulled.
            extra = ["investments"] if mode == "consent" else None
            link_token = plaid.create_link_token(user["id"], access_token=token,
                                                 additional_products=extra)
        else:
            link_token = plaid.create_link_token(user["id"])
    except plaid.PlaidError as exc:
        audit("plaid.link_token_failed", request, user, code=exc.code)
        if exc.code in ("RATE_LIMIT", "RATE_LIMIT_EXCEEDED", "LINK_ATTEMPT_LIMIT"):
            return JSONResponse({
                "ok": False, "rate_limited": True, "retry_after": 600,
                "error": "Plaid is rate-limiting new Link attempts on this connection. "
                         "Wait a few minutes, then try once.",
            }, status_code=429)
        return JSONResponse({"ok": False, "error": exc.message or "Plaid rejected the request."},
                            status_code=502)

    _LINK_TOKENS[key] = (link_token, now + 600)
    return JSONResponse({"ok": True, "link_token": link_token, "env": plaid.env_name()})


def _refresh_plaid_item(household_id: int, item_id: int) -> str | None:
    """A connection just signed in again: mark it good, then pull transactions and holdings."""
    db.execute("UPDATE plaid_items SET status = 'good', error_code = NULL, updated_at = ? WHERE id = ?",
               (db.utcnow(), item_id))
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?",
                       (item_id, household_id))
    if row is None:
        return "missing"
    fresh = dict(row)
    summary = plaid_sync.sync_item(household_id, fresh)
    has_investments = db.query_one(
        "SELECT id FROM accounts WHERE plaid_item_id = ? AND type IN"
        " ('brokerage','retirement','crypto','investment') LIMIT 1",
        (fresh["plaid_item_id"],),
    )
    if has_investments:
        plaid_sync.sync_holdings(household_id, fresh)
    return summary.get("error")


@app.post("/plaid/reconnected")
def plaid_reconnected(request: Request, csrf: str = Form(""), item_id: str = Form("")):
    """Update mode finished without a public token (the common case): refresh in place."""
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings?error=csrf")
    item_id_int = int(item_id) if item_id.isdigit() else 0
    if db.query_one("SELECT id FROM plaid_items WHERE id = ? AND household_id = ?",
                    (item_id_int, user["household_id"])) is None:
        raise HTTPException(status_code=404)
    error = _refresh_plaid_item(user["household_id"], item_id_int)
    audit("plaid.reconnected", request, user, item=item_id_int)
    if error:
        return redirect(f"/settings?error={error}")
    return redirect("/settings?reconnected=1")


@app.post("/plaid/exchange")
def plaid_exchange(
    request: Request,
    csrf: str = Form(""),
    public_token: str = Form(""),
    institution_id: str = Form(""),
    institution_name: str = Form(""),
    item_id: str = Form(""),
    mode: str = Form(""),
):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf) or not public_token:
        return redirect("/settings?error=csrf")
    hid = user["household_id"]
    try:
        exchanged = plaid.exchange_public_token(public_token)
    except plaid.PlaidError as exc:
        audit("plaid.exchange_failed", request, user, code=exc.code)
        return redirect(f"/settings?error={exc.code}")

    plaid_item_id = exchanged.get("item_id", "")
    access_token = exchanged["access_token"]
    # Update mode ends with the *same* Item (sometimes a re-initialized one), so the
    # public token belongs to a connection we already hold — refresh it instead of
    # refusing it as a duplicate.
    target = None
    if item_id.isdigit():
        target = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?",
                              (int(item_id), hid))
    if target is not None:
        now = db.utcnow()
        new_plaid_id = plaid_item_id or target["plaid_item_id"]
        db.execute(
            """UPDATE plaid_items SET plaid_item_id = ?, access_token_enc = ?, status = 'good',
                   error_code = NULL, updated_at = ? WHERE id = ?""",
            (new_plaid_id, plaid.encrypt_token(access_token), now, target["id"]),
        )
        if new_plaid_id != target["plaid_item_id"]:
            # A re-initialized Item gets a new id; keep the accounts pointed at it.
            db.execute("UPDATE accounts SET plaid_item_id = ? WHERE plaid_item_id = ? AND household_id = ?",
                       (new_plaid_id, target["plaid_item_id"], hid))
        audit("plaid.reconnected", request, user, item=target["id"])
        error = _refresh_plaid_item(hid, target["id"])
        if error:
            return redirect(f"/settings?error={error}")
        return redirect("/settings?reconnected=1")

    if db.query_one("SELECT id FROM plaid_items WHERE plaid_item_id = ?", (plaid_item_id,)):
        # Link already counted this Item against the Trial cap. We cannot get the slot
        # back, but do not leave a live token with no row pointing at it.
        try:
            plaid.item_remove(access_token)
        except plaid.PlaidError:
            pass
        return redirect("/settings?error=already_linked")

    name = institution_name
    inst_id = institution_id
    try:
        info = plaid.item_get(access_token).get("item", {})
        inst_id = inst_id or info.get("institution_id", "")
        if inst_id:
            name = name or plaid.institutions_get_by_id(inst_id).get("name", "")
    except plaid.PlaidError:
        pass

    now = db.utcnow()
    item_pk = db.execute(
        """INSERT INTO plaid_items(household_id, plaid_item_id, access_token_enc, institution_id,
               institution_name, status, created_at, updated_at)
           VALUES (?,?,?,?,?,'good',?,?)""",
        (hid, plaid_item_id, plaid.encrypt_token(access_token), inst_id, name or "Linked institution", now, now),
    )
    audit("plaid.linked", request, user, item=item_pk, institution=name or inst_id)
    return redirect(f"/plaid/review?item={item_pk}")


@app.get("/plaid/review", response_class=HTMLResponse)
def plaid_review(request: Request, item: int = 0, error: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?", (item, hid))
    if row is None:
        raise HTTPException(status_code=404)
    try:
        accounts = plaid.accounts_get(plaid.decrypt_token(row["access_token_enc"]))
    except plaid.PlaidError as exc:
        return redirect(f"/settings?error={exc.code}")
    for acct in accounts:
        acct["suggested_type"] = plaid_sync.local_type(acct)
    all_accounts = networth.list_accounts(hid, include_archived=True)
    existing = [a for a in all_accounts if not a["plaid_account_id"]]
    known = {a["plaid_account_id"] for a in all_accounts if a["plaid_account_id"]}
    return render(
        request, "plaid_review.html",
        item=dict(row), accounts=accounts, existing=existing, error=error,
        institution=row["institution_name"], known=known,
    )


@app.post("/plaid/review")
async def plaid_review_save(request: Request):
    form = await request.form()
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return redirect("/settings?error=csrf")
    hid = user["household_id"]
    item = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?",
                        (int(form.get("item") or 0), hid))
    if item is None:
        raise HTTPException(status_code=404)
    try:
        remote = plaid.accounts_get(plaid.decrypt_token(item["access_token_enc"]))
    except plaid.PlaidError as exc:
        return redirect(f"/settings?error={exc.code}")

    by_id = {a["account_id"]: a for a in remote}
    for acct in remote:
        acct["_institution"] = item["institution_name"]
        acct["item_id"] = item["plaid_item_id"]
    choices = []
    for plaid_id in form.getlist("plaid_account_id"):
        raw = str(form.get(f"mode_{plaid_id}") or "new")
        mode, account_id = "new", None
        if raw == "skip":
            mode = "skip"
        elif raw.startswith("existing:"):
            mode, account_id = "existing", raw.split(":", 1)[1]
        choices.append({
            "plaid_account_id": plaid_id,
            "mode": mode,
            "account_id": account_id,
            "plaid_account": by_id.get(plaid_id),
        })
    result = plaid_sync.import_accounts(hid, item["id"], choices)
    summary = plaid_sync.sync_item(hid, dict(item))
    audit("plaid.synced", request, user, item=item["id"], **summary, **result)
    if summary.get("error"):
        return redirect(f"/settings?error={summary['error']}")
    return redirect("/settings?linked=1")


@app.post("/plaid/sync/{item_id}")
def plaid_sync_now(request: Request, item_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings?error=csrf")
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?",
                       (item_id, user["household_id"]))
    if row is None:
        raise HTTPException(status_code=404)
    summary = plaid_sync.sync_item(user["household_id"], dict(row))
    audit("plaid.sync_manual", request, user, item=item_id, **summary)
    return redirect(f"/settings?error={summary['error']}" if summary.get("error") else "/settings")


@app.post("/plaid/consented")
def plaid_consented(request: Request, csrf: str = Form(""), item_id: str = Form("")):
    """Link finished collecting investments consent: pull holdings straight away."""
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings?error=csrf")
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?",
                       (int(item_id) if item_id.isdigit() else 0, user["household_id"]))
    if row is None:
        raise HTTPException(status_code=404)
    summary = plaid_sync.sync_holdings(user["household_id"], dict(row))
    audit("plaid.holdings_synced", request, user, item=row["id"], **summary)
    if summary["error"]:
        return redirect(f"/settings?error={summary['error']}")
    return redirect("/portfolio?synced=1")


@app.post("/plaid/sync-holdings/{item_id}")
def plaid_sync_holdings(request: Request, item_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings?error=csrf")
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?",
                       (item_id, user["household_id"]))
    if row is None:
        raise HTTPException(status_code=404)
    summary = plaid_sync.sync_holdings(user["household_id"], dict(row))
    audit("plaid.holdings_synced", request, user, item=item_id, **summary)
    if summary["error"]:
        return redirect(f"/settings?error={summary['error']}")
    return redirect("/portfolio?synced=1")


@app.post("/plaid/remove/{item_id}")
def plaid_remove(request: Request, item_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings?error=csrf")
    hid = user["household_id"]
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ? AND household_id = ?", (item_id, hid))
    if row is None:
        raise HTTPException(status_code=404)
    try:
        plaid.item_remove(plaid.decrypt_token(row["access_token_enc"]))
    except plaid.PlaidError as exc:
        # A failed revoke must not destroy the only copy of the access token: without
        # it the Item lives on at Plaid and can never be cleaned up. Keep the row so
        # the user can retry. "Not found" means it is already gone.
        if exc.code not in {"ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN", "INVALID_ITEM_ID"}:
            message = f"Could not unlink this bank at Plaid ({exc.code}). The connection was kept so you can try again."
            return redirect(f"/settings?error={quote(message)}")
    db.execute("UPDATE accounts SET plaid_item_id = NULL WHERE household_id = ? AND plaid_item_id = ?",
               (hid, row["plaid_item_id"]))
    db.execute("UPDATE transactions SET plaid_item_id = NULL WHERE household_id = ? AND plaid_item_id = ?",
               (hid, item_id))
    db.execute("DELETE FROM plaid_items WHERE id = ?", (item_id,))
    audit("plaid.removed", request, user, item=item_id)
    return redirect("/settings")


@app.post("/webhooks/plaid")
async def plaid_webhook(request: Request):
    raw = await request.body()
    if not plaid.configured():
        return JSONResponse({"ok": False}, status_code=404)
    token = request.headers.get("plaid-verification", "")
    if not token:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        plaid.verify_webhook(token, raw)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False}, status_code=401)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"ok": False}, status_code=400)
    item_id = payload.get("item_id")
    if item_id:
        row = db.query_one("SELECT * FROM plaid_items WHERE plaid_item_id = ?", (item_id,))
        if row is not None:
            plaid_sync.sync_item(row["household_id"], dict(row))
    return JSONResponse({"ok": True})



# ---------------------------------------------------------------- categories
@app.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request, archived: int = 0):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    categories.ensure_taxonomy(hid)
    return render(
        request, "categories.html",
        tree=categories.tree(hid),
        flat=categories.list_categories(hid, include_archived=bool(archived)),
        archived_view=bool(archived),
    )


@app.post("/categories")
def category_create(request: Request, csrf: str = Form(""), name: str = Form(""),
                    kind: str = Form("expense"), parent_id: str = Form(""), color: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/categories?error=csrf")
    try:
        categories.create(user["household_id"], name, kind,
                          int(parent_id) if parent_id.isdigit() else None, color or None)
    except ValueError as exc:
        return redirect(f"/categories?error={str(exc).replace(' ', '+')}")
    return redirect("/categories")


@app.get("/categories/{category_id}", response_class=HTMLResponse)
def category_detail(request: Request, category_id: int):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    cat = categories.get_category(hid, category_id)
    if cat is None:
        raise HTTPException(status_code=404)
    others = [c for c in categories.list_categories(hid) if c["id"] != category_id]
    return render(request, "category_detail.html", cat=cat, others=others,
                  parents=[c for c in others if not c["parent_id"]])


@app.post("/categories/{category_id}/update")
def category_update(request: Request, category_id: int, csrf: str = Form(""),
                    name: str = Form(""), kind: str = Form(""), parent_id: str = Form(""),
                    color: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/categories/{category_id}")
    try:
        categories.update(user["household_id"], category_id, name=name or None, kind=kind or None,
                          parent_id=int(parent_id) if parent_id.isdigit() else None, color=color or None)
    except ValueError as exc:
        return redirect(f"/categories/{category_id}?error={str(exc).replace(' ', '+')}")
    return redirect("/categories")


@app.post("/categories/{category_id}/merge")
def category_merge(request: Request, category_id: int, csrf: str = Form(""), target_id: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/categories/{category_id}")
    try:
        moved = categories.merge(user["household_id"], category_id, int(target_id or 0))
    except ValueError as exc:
        return redirect(f"/categories/{category_id}?error={str(exc).replace(' ', '+')}")
    audit("category.merged", request, user, source=category_id, target=target_id, moved=moved)
    return redirect("/categories?merged=1")


@app.post("/categories/{category_id}/archive")
def category_archive(request: Request, category_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        try:
            categories.archive(user["household_id"], category_id)
        except ValueError:
            pass
    return redirect("/categories")


@app.post("/categories/{category_id}/restore")
def category_restore(request: Request, category_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        categories.restore(user["household_id"], category_id)
    return redirect("/categories?archived=1")


# ---------------------------------------------------------------- rules
def _rule_conditions(field0="", op0="", value0="", field1="", op1="", value1="",
                     field2="", op2="", value2="") -> list[dict]:
    return rules.parse_conditions([
        {"field": field0, "op": op0, "value": value0},
        {"field": field1, "op": op1, "value": value1},
        {"field": field2, "op": op2, "value": value2},
    ])


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    categories.ensure_taxonomy(hid)
    return render(
        request, "rules.html",
        rule_list=rules.list_rules(hid),
        aliases=rules.list_aliases(hid),
        cats=categories.list_categories(hid),
    )


@app.get("/rules/new", response_class=HTMLResponse)
def rule_new_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    categories.ensure_taxonomy(hid)
    return render(request, "rule_form.html", rule=None, cats=categories.list_categories(hid),
                  accounts=networth.list_accounts(hid), ops=rules.OPS, fields=rules.FIELD_LABELS)


@app.post("/rules/new")
def rule_create(request: Request, csrf: str = Form(""), name: str = Form(""),
                action_type: str = Form("category"), action_value: str = Form(""),
                apply_to: str = Form("uncategorized"),
                field0: str = Form(""), op0: str = Form(""), value0: str = Form(""),
                field1: str = Form(""), op1: str = Form(""), value1: str = Form(""),
                field2: str = Form(""), op2: str = Form(""), value2: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/rules/new?error=csrf")
    try:
        conditions = _rule_conditions(field0, op0, value0, field1, op1, value1, field2, op2, value2)
        if not name.strip():
            raise ValueError("Give the rule a name.")
        if action_type not in ("category", "tag", "transfer", "exclude"):
            raise ValueError("Pick an action.")
        rid = rules.create_rule(user["household_id"], name, conditions, action_type,
                                action_value.strip() or None, apply_to)
    except ValueError as exc:
        return redirect(f"/rules/new?error={str(exc).replace(' ', '+')}")
    audit("rule.created", request, user, rule=rid)
    return redirect(f"/rules/{rid}")


@app.get("/rules/{rule_id}", response_class=HTMLResponse)
def rule_detail(request: Request, rule_id: int, preview: int = 0, applied: int = 0):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    rule = rules.get_rule(hid, rule_id)
    if rule is None:
        raise HTTPException(status_code=404)
    preview_data = rules.preview(hid, rule["conditions"], rule["apply_to"]) if preview else None
    return render(request, "rule_form.html", rule=rule, cats=categories.list_categories(hid),
                  accounts=networth.list_accounts(hid), ops=rules.OPS, fields=rules.FIELD_LABELS,
                  preview_data=preview_data, applied=applied or None,
                  summary=rules.rule_summary(rule))


@app.post("/rules/{rule_id}/update")
def rule_update(request: Request, rule_id: int, csrf: str = Form(""), name: str = Form(""),
                action_type: str = Form("category"), action_value: str = Form(""),
                apply_to: str = Form("uncategorized"),
                field0: str = Form(""), op0: str = Form(""), value0: str = Form(""),
                field1: str = Form(""), op1: str = Form(""), value1: str = Form(""),
                field2: str = Form(""), op2: str = Form(""), value2: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/rules/{rule_id}")
    try:
        conditions = _rule_conditions(field0, op0, value0, field1, op1, value1, field2, op2, value2)
        rules.update_rule(
            user["household_id"], rule_id,
            name=name.strip()[:80], conditions_json=json.dumps(conditions),
            action_type=action_type, action_value=action_value.strip() or None, apply_to=apply_to,
        )
    except ValueError as exc:
        return redirect(f"/rules/{rule_id}?error={str(exc).replace(' ', '+')}")
    return redirect(f"/rules/{rule_id}")


@app.post("/rules/{rule_id}/toggle")
def rule_toggle(request: Request, rule_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        rule = rules.get_rule(user["household_id"], rule_id)
        if rule:
            rules.update_rule(user["household_id"], rule_id, enabled=0 if rule["enabled"] else 1)
    return redirect("/rules")


@app.post("/rules/{rule_id}/delete")
def rule_delete(request: Request, rule_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        rules.delete_rule(user["household_id"], rule_id)
    return redirect("/rules")


@app.post("/rules/{rule_id}/apply")
def rule_apply(request: Request, rule_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/rules/{rule_id}")
    count = rules.run_history(user["household_id"], rule_id)
    audit("rule.applied", request, user, rule=rule_id, count=count)
    return redirect(f"/rules/{rule_id}?applied={count}")


@app.post("/aliases")
def alias_create(request: Request, csrf: str = Form(""), raw: str = Form(""), alias: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        try:
            rules.set_alias(user["household_id"], raw, alias)
        except ValueError:
            pass
    return redirect("/rules")


@app.post("/aliases/apply")
def alias_apply(request: Request, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/rules")
    changed = rules.apply_alias_to_history(user["household_id"])
    audit("alias.applied", request, user, changed=changed)
    return redirect(f"/rules?aliased={changed}")


# ---------------------------------------------------------------- CSV import
@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    return render(request, "import_upload.html",
                  accounts=networth.list_accounts(hid),
                  profiles=csv_import.list_profiles(hid))


@app.post("/import/upload", response_class=HTMLResponse)
async def import_upload(request: Request, csrf: str = Form(""), account_id: str = Form(""),
                        file: UploadFile = File(...), profile_id: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/import?error=csrf")
    account = networth.get_account(user["household_id"], int(account_id)) if account_id.isdigit() else None
    if account is None:
        return redirect("/import?error=account")
    content = b""
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        content += chunk
        if len(content) > csv_import.MAX_BYTES:
            return redirect("/import?error=That+file+is+larger+than+10+MB.")
    try:
        token = csv_import.save_upload(file.filename or "import.csv", content)
        data = csv_import.read_rows(token)
    except csv_import.ImportError_ as exc:
        return redirect(f"/import?error={str(exc).replace(' ', '+')}")

    guess = csv_import.guess_columns(data["header"], data["rows"])
    mapping = {"date_format": csv_import.guess_date_format(data["rows"], guess["date_col"] or 0), **guess}
    if profile_id.isdigit():
        profile = next((p for p in csv_import.list_profiles(user["household_id"])
                        if p["id"] == int(profile_id)), None)
        if profile:
            mapping.update(csv_import.profile_mapping(profile))
            mapping["date_format"] = profile["date_format"]
    return render(request, "import_map.html", token=token, account=account, data=data,
                  mapping=mapping, profiles=csv_import.list_profiles(user["household_id"]),
                  filename=file.filename or "import.csv")


@app.post("/import/preview", response_class=HTMLResponse)
async def import_preview(request: Request):
    form = await request.form()
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return redirect("/import?error=csrf")
    account = networth.get_account(user["household_id"], int(form.get("account_id") or 0))
    if account is None:
        return redirect("/import?error=account")
    mapping = _mapping_from(form)
    try:
        preview = csv_import.build_preview(str(form.get("token")), account["id"], mapping)
        data = csv_import.read_rows(str(form.get("token")))
    except csv_import.ImportError_ as exc:
        return redirect(f"/import?error={str(exc).replace(' ', '+')}")
    return render(request, "import_preview.html", preview=preview, account=account, mapping=mapping,
                  token=str(form.get("token")), filename=str(form.get("filename") or "import.csv"),
                  profile_name=str(form.get("profile_name") or ""), data=data)


@app.post("/import/commit")
async def import_commit(request: Request):
    form = await request.form()
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return redirect("/import?error=csrf")
    account = networth.get_account(user["household_id"], int(form.get("account_id") or 0))
    if account is None:
        return redirect("/import?error=account")
    mapping = _mapping_from(form)
    profile_id = None
    profile_name = str(form.get("profile_name") or "").strip()
    if profile_name:
        profile_id = csv_import.save_profile(user["household_id"], profile_name, account["id"], mapping)
    try:
        result = csv_import.commit(str(form.get("token")), user["household_id"], account["id"], mapping,
                                   profile_id=profile_id, filename=str(form.get("filename") or ""))
    except csv_import.ImportError_ as exc:
        return redirect(f"/import?error={str(exc).replace(' ', '+')}")
    audit("import.committed", request, user, **result)
    return redirect(f"/transactions?imported={result['imported']}&dupes={result['duplicates']}")


def _mapping_from(form) -> dict:
    def idx(key: str):
        value = str(form.get(key) or "").strip()
        return int(value) if value.isdigit() else None

    return {
        "date_col": idx("date_col"),
        "desc_col": idx("desc_col"),
        "amount_col": idx("amount_col"),
        "debit_col": idx("debit_col"),
        "credit_col": idx("credit_col"),
        "category_col": idx("category_col"),
        "memo_col": idx("memo_col"),
        "date_format": str(form.get("date_format") or "%m/%d/%Y"),
        "invert_sign": bool(form.get("invert_sign")),
    }



# ---------------------------------------------------------------- budgets
@app.get("/budget", response_class=HTMLResponse)
def budget_page(request: Request, month: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    categories.ensure_taxonomy(hid)
    target = budgets.month_key(month or budgets.month_key())
    seeded = budgets.fill_from_previous(hid, target)
    if seeded:
        audit("budget.seeded", request, user, month=target, count=seeded)
    g = budgets.grid(hid, target)
    return render(
        request, "budget.html", g=g,
        safe=budgets.safe_to_spend(hid, g["month"], plan_grid=g),
        copied=request.query_params.get("copied"),
        auto=request.query_params.get("auto"),
        copy_previous=budgets.copy_previous_enabled(hid),
        hidden_cats=categories.hidden_from_budget(hid),
    )


@app.post("/budget/hide")
def budget_hide(request: Request, csrf: str = Form(""), month: str = Form(""),
                category_id: int = Form(0)):
    """Take a category off the Budget tab — it stays usable in the ledger's filters."""
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/budget?error=csrf")
    target = budgets.month_key(month)
    categories.set_budget_hidden(user["household_id"], category_id, True)
    audit("budget.category_hidden", request, user, month=target, category=category_id)
    return redirect(f"/budget?month={target}")


@app.post("/budget/unhide")
def budget_unhide(request: Request, csrf: str = Form(""), month: str = Form(""),
                  category_id: int = Form(0)):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/budget?error=csrf")
    target = budgets.month_key(month)
    categories.set_budget_hidden(user["household_id"], category_id, False)
    audit("budget.category_shown", request, user, month=target, category=category_id)
    return redirect(f"/budget?month={target}")


@app.post("/budget/copy-previous")
def budget_copy_previous_toggle(request: Request, csrf: str = Form(""), month: str = Form(""),
                                enabled: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/budget?error=csrf")
    target = budgets.month_key(month)
    hid = user["household_id"]
    budgets.set_copy_previous(hid, bool(enabled))
    if enabled:
        budgets.fill_from_previous(hid, target)
    audit("budget.copy_previous", request, user, month=target, enabled=bool(enabled))
    return redirect(f"/budget?month={target}")


@app.post("/budget/save")
async def budget_save(request: Request):
    form = await request.form()
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return redirect("/budget?error=csrf")
    hid = user["household_id"]
    month = budgets.month_key(str(form.get("month") or ""))
    for key in form.keys():
        if not key.startswith("cat_"):
            continue
        category_id = int(key.split("_", 1)[1])
        cents = money.parse_money(str(form.get(key) or "0")) or 0
        # Zero means "no budget for this category" — drop the row entirely. Rollover is
        # no longer a per-category choice; the month itself is copied from last month.
        if cents == 0:
            db.execute("DELETE FROM budgets WHERE household_id = ? AND month = ? AND category_id = ?",
                       (hid, month, category_id))
            continue
        budgets.set_budget(hid, month, category_id, cents)
    income = money.parse_money(str(form.get("expected_income") or "0")) or 0
    budgets.set_income(hid, month, income)
    audit("budget.saved", request, user, month=month)
    if _wants_json(request):
        g = budgets.grid(hid, month)
        return JSONResponse({
            "ok": True,
            "month": month,
            "budgeted_cents": g["budgeted_cents"],
            "spent_cents": g["all_spent_cents"],
            "expected_income_cents": g["expected_income_cents"],
            "left_to_budget_cents": g["left_to_budget_cents"],
        })
    return redirect(f"/budget?month={month}")


@app.post("/budget/copy")
def budget_copy(request: Request, csrf: str = Form(""), month: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/budget?error=csrf")
    target = budgets.month_key(month)
    copied = budgets.copy_month(user["household_id"], budgets.shift_month(target, -1), target)
    audit("budget.copied", request, user, month=target, count=copied)
    return redirect(f"/budget?month={target}&copied={copied}")


@app.post("/budget/auto")
def budget_auto(request: Request, csrf: str = Form(""), month: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/budget?error=csrf")
    target = budgets.month_key(month)
    count = budgets.auto_budget(user["household_id"], target)
    audit("budget.auto", request, user, month=target, count=count)
    return redirect(f"/budget?month={target}&auto={count}")



# ---------------------------------------------------------------- bills + forecast
@app.get("/bills", response_class=HTMLResponse)
def bills_page(request: Request, month: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    categories.ensure_taxonomy(hid)
    key = month if budgets.month_key(month) == month else budgets.month_key()
    all_rows = recurring.list_recurring(hid)
    first, last = budgets.month_bounds(key)
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    schedule: list[dict] = []
    seen: set[int] = set()
    income_total = bill_total = 0
    for row in all_rows:
        if row["status"] == "ended":
            continue
        for due in recurring.occurrences_between(row, start, end):
            schedule.append({"due": due, "row": row, "first": row["id"] not in seen})
            seen.add(row["id"])
            if row["kind"] == "income":
                income_total += row["amount_cents"]
            else:
                bill_total += row["amount_cents"]
    schedule.sort(key=lambda e: (e["due"], e["row"]["name"].lower()))
    cat_names = {c["id"]: c["name"] for c in categories.list_categories(hid)}
    def _listed(rows, kind):
        out = []
        for r in rows:
            if r["kind"] != kind or r["status"] in ("ended", "ignored"):
                continue
            item = dict(r)
            item["category_name"] = cat_names.get(r["category_id"], "")
            item["cadence_label"] = recurring.CADENCES.get(r["cadence"], ("Monthly", 30))[0]
            out.append(item)
        out.sort(key=lambda r: (r["next_due_on"], r["name"].lower()))
        return out
    bill_items = _listed(all_rows, "bill")
    income_items = _listed(all_rows, "income")
    by_day: dict[str, list[dict]] = {}
    for e in schedule:
        by_day.setdefault(e["due"].isoformat(), []).append(e)
    today_iso = date.today().isoformat()
    weeks: list[list[dict | None]] = []
    for week in budgets.month_grid(key):
        cells: list[dict | None] = []
        for cell in week:
            if cell is None:
                cells.append(None)
                continue
            items = []
            for e in by_day.get(cell["iso"], []):
                row = e["row"]
                items.append({
                    "id": row["id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "varies": bool(row["amount_varies"]),
                    "cadence": recurring.CADENCES[row["cadence"]][0],
                    "amount_cents": row["amount_cents"],
                    "amount": money.format_money(row["amount_cents"]),
                    "first": e["first"],
                })
            income_count = sum(1 for i in items if i["kind"] == "income")
            cells.append({
                **cell,
                "items": items,
                "income_count": income_count,
                "bill_count": len(items) - income_count,
                "is_today": cell["iso"] == today_iso,
            })
        weeks.append(cells)
    return render(
        request, "bills.html",
        month=key, label=budgets.month_label(key),
        prev=budgets.shift_month(key, -1), next=budgets.shift_month(key, 1),
        is_current=key == budgets.month_key(),
        schedule=schedule, weeks=weeks, income_total=income_total, bill_total=bill_total,
        bill_items=bill_items, income_items=income_items,
        suggested_count=sum(1 for r in all_rows if r["status"] == "suggested"),
        ended=[r for r in all_rows if r["status"] == "ended"],
        cats=categories.list_categories(hid),
        accounts=networth.list_accounts(hid),
        cadences=recurring.CADENCES,
        scanned=request.query_params.get("scanned"),
        saved=request.query_params.get("saved"),
    )


@app.post("/bills/scan")
def bills_scan(request: Request, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/bills?error=csrf")
    added = recurring.scan(user["household_id"])
    audit("recurring.scanned", request, user, added=added)
    return redirect(f"/bills?scanned={added}")


@app.get("/bills/new", response_class=HTMLResponse)
def bill_new_page(request: Request, kind: str = "bill"):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    categories.ensure_taxonomy(hid)
    return render(request, "bill_form.html", rec=None, kind=kind,
                  cats=categories.list_categories(hid),
                  accounts=networth.list_accounts(hid),
                  cadences=recurring.CADENCES,
                  today=db.local_now().date().isoformat())


@app.post("/bills/new")
def bill_new(request: Request, csrf: str = Form(""), name: str = Form(""),
             amount: str = Form(""), cadence: str = Form("monthly"),
             next_due_on: str = Form(""), kind: str = Form("bill"),
             category_id: str = Form(""), account_id: str = Form(""), notes: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/bills/new?error=csrf")
    try:
        cents = money.parse_money(amount) or 0
        rid = recurring.create(
            user["household_id"], name, cents, cadence, next_due_on, kind=kind,
            category_id=int(category_id) if category_id.isdigit() else None,
            account_id=int(account_id) if account_id.isdigit() else None,
            notes=notes or None,
        )
    except ValueError as exc:
        return redirect(f"/bills/new?error={str(exc).replace(' ', '+')}")
    audit("recurring.created", request, user, recurring=rid)
    return redirect("/bills?saved=1")


@app.get("/bills/{recurring_id}", response_class=HTMLResponse)
def bill_edit_page(request: Request, recurring_id: int):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    rec = recurring.get(hid, recurring_id)
    if rec is None:
        raise HTTPException(status_code=404)
    categories.ensure_taxonomy(hid)
    return render(request, "bill_form.html", rec=rec, kind=rec["kind"],
                  cats=categories.list_categories(hid),
                  accounts=networth.list_accounts(hid),
                  cadences=recurring.CADENCES, today=rec["next_due_on"])


@app.post("/bills/{recurring_id}/update")
def bill_update(request: Request, recurring_id: int, csrf: str = Form(""), name: str = Form(""),
                amount: str = Form(""), cadence: str = Form("monthly"), next_due_on: str = Form(""),
                kind: str = Form("bill"), category_id: str = Form(""), account_id: str = Form(""),
                notes: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect(f"/bills/{recurring_id}")
    try:
        due = db_date(next_due_on)
        recurring.update(
            user["household_id"], recurring_id,
            name=name.strip()[:60], amount_cents=max(1, money.parse_money(amount) or 0),
            cadence=cadence, next_due_on=due, kind=kind,
            category_id=int(category_id) if category_id.isdigit() else None,
            account_id=int(account_id) if account_id.isdigit() else None,
            anchor_day=int(due[8:10]),
            notes=notes or None,
        )
    except ValueError as exc:
        return redirect(f"/bills/{recurring_id}?error={str(exc).replace(' ', '+')}")
    return redirect("/bills?saved=1")


def db_date(value: str) -> str:
    from datetime import date as _date
    try:
        return _date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Pick a valid date.") from exc


@app.post("/bills/{recurring_id}/confirm")
def bill_confirm(request: Request, recurring_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        recurring.set_status(user["household_id"], recurring_id, "confirmed")
    return redirect("/bills")


@app.post("/bills/{recurring_id}/ignore")
def bill_ignore(request: Request, recurring_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        recurring.set_status(user["household_id"], recurring_id, "ignored")
    return redirect("/bills")


@app.post("/bills/{recurring_id}/end")
def bill_end(request: Request, recurring_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        recurring.set_status(user["household_id"], recurring_id, "ended")
    return redirect("/bills")


@app.post("/bills/{recurring_id}/delete")
def bill_delete(request: Request, recurring_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        recurring.delete(user["household_id"], recurring_id)
    return redirect("/bills")


@app.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request, days: str = "90"):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    try:
        window = int(days)
    except (TypeError, ValueError):
        window = 90
    fc = forecast.forecast(hid, window)
    return render(request, "forecast.html", fc=fc, days=fc["days"],
                  horizons=[(30, "30 days"), (60, "60 days"), (90, "90 days"), (180, "6 months")])



# ---------------------------------------------------------------- portfolio
@app.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    _refresh_btc(user["household_id"])
    # Like Net worth: a click writes the cookie, ordinary nav reads it back.
    frame = (request.query_params.get("range") or request.cookies.get("perf_range") or "1y").lower()
    if frame not in dict(performance.FRAMES):
        frame = "1y"
    response = render(request, "performance.html",
                      perf=performance.curve(user["household_id"],
                                             days=performance.frame_days(frame)),
                      ranges=performance.FRAMES, range_key=frame)
    response.set_cookie("perf_range", frame, max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="lax")
    return response


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    _refresh_btc(hid)
    rows = portfolio.holdings(hid)
    rows += portfolio.account_value_rows(hid, rows)
    return render(
        request, "portfolio.html",
        rows=rows,
        groups=portfolio.group_by_account(rows),
        totals=portfolio.totals(rows),
        manual_count=sum(1 for r in rows if r.get("source") != "plaid"),
        by_kind=portfolio.allocation(rows, "kind"),
        by_account=portfolio.allocation(rows, "account_name"),
        accounts=portfolio.investment_accounts(hid),
        all_accounts=networth.list_accounts(hid),
        opts=portfolio.options(hid),
        opt_summary=portfolio.option_summary(portfolio.options(hid)),
        kinds=portfolio.KINDS,
        today=db.local_now().date().isoformat(),
        saved=request.query_params.get("saved"),
    )


@app.post("/portfolio/holdings")
def portfolio_add(request: Request, csrf: str = Form(""), account_id: str = Form(""),
                  symbol: str = Form(""), name: str = Form(""), kind: str = Form("stock"),
                  quantity: str = Form(""), cost_basis: str = Form(""), price: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/portfolio?error=csrf")
    try:
        if not account_id.isdigit():
            raise ValueError("Pick an account.")
        qty = money.parse_quantity(quantity) if hasattr(money, "parse_quantity") else float(quantity or 0)
        portfolio.add_holding(
            user["household_id"], int(account_id), symbol, qty,
            money.parse_money(cost_basis) or 0, money.parse_money(price) or 0,
            name=name or None, kind=kind,
        )
    except ValueError as exc:
        return redirect(f"/portfolio?error={str(exc).replace(' ', '+')}")
    audit("holding.added", request, user, symbol=symbol.upper())
    return redirect("/portfolio?saved=1")


@app.post("/portfolio/holdings/save")
async def portfolio_save(request: Request):
    form = await request.form()
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return redirect("/portfolio?error=csrf")
    hid = user["household_id"]
    for key in form.keys():
        if not key.startswith("qty_"):
            continue
        holding_id = int(key.split("_", 1)[1])
        row = db.query_one("SELECT source FROM holdings WHERE id = ? AND household_id = ?",
                           (holding_id, hid))
        if row is None or row["source"] == "plaid":
            continue  # broker-synced rows are refreshed, never edited
        qty_raw = str(form.get(key) or "").strip()
        price_raw = str(form.get(f"price_{holding_id}") or "").strip()
        try:
            qty = float(qty_raw) if qty_raw else None
            price = money.parse_money(price_raw) if price_raw else None
        except ValueError:
            continue
        portfolio.update_holding(hid, holding_id, quantity=qty, price_cents=price)
    return redirect("/portfolio?saved=1")


@app.post("/portfolio/holdings/{holding_id}/delete")
def portfolio_delete(request: Request, holding_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        portfolio.delete_holding(user["household_id"], holding_id)
    return redirect("/portfolio")


@app.post("/portfolio/flows")
def portfolio_flow(request: Request, csrf: str = Form(""), occurred_on: str = Form(""),
                   amount: str = Form(""), note: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/portfolio?error=csrf")
    try:
        cents = money.parse_money(amount) or 0
        portfolio.add_flow(user["household_id"], db_date(occurred_on), cents, note=note or None)
    except ValueError as exc:
        return redirect(f"/portfolio?error={str(exc).replace(' ', '+')}")
    return redirect("/portfolio?saved=1")


@app.post("/portfolio/flows/{flow_id}/delete")
def portfolio_flow_delete(request: Request, flow_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        portfolio.delete_flow(user["household_id"], flow_id)
    return redirect("/portfolio")


@app.post("/portfolio/options")
def portfolio_option_add(request: Request, csrf: str = Form(""), underlying: str = Form(""),
                         option_type: str = Form("put"), strike: str = Form(""),
                         expiry_on: str = Form(""), contracts: str = Form("1"),
                         premium: str = Form(""), opened_on: str = Form(""), account_id: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/portfolio?error=csrf")
    try:
        portfolio.add_option(
            user["household_id"], underlying, option_type,
            money.parse_money(strike) or 0, db_date(expiry_on),
            int(contracts or 1), money.parse_money(premium) or 0, db_date(opened_on),
            account_id=int(account_id) if account_id.isdigit() else None,
        )
    except ValueError as exc:
        return redirect(f"/portfolio?error={str(exc).replace(' ', '+')}")
    return redirect("/portfolio?saved=1")


@app.post("/portfolio/options/{option_id}/close")
def portfolio_option_close(request: Request, option_id: int, csrf: str = Form(""),
                           close_price: str = Form("0"), status: str = Form("closed")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        portfolio.close_option(user["household_id"], option_id,
                               money.parse_money(close_price) or 0, status)
    return redirect("/portfolio")



# ---------------------------------------------------------------- reports
@app.get("/retirement", response_class=HTMLResponse)
def retirement_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    plan = retirement.load_plan(hid)
    # First visit only: seed from the books. Never re-seed a saved plan — "0" is a
    # real answer, and a falsy check here used to overwrite a deliberately blank salary.
    if not alerts.get_setting(hid, retirement.PLAN_KEY):
        plan["spending"] = round(retirement.default_spending(hid) / 100, 2)
        plan["income"][0]["gross"] = round(retirement.default_gross(hid) / 100, 2)
        plan["people"][0]["name"] = plan["people"][0]["name"] or user["display_name"]
    result = retirement.project(hid, plan)
    try:
        runs = max(200, min(int(request.query_params.get("runs") or 1000), 20000))
    except ValueError:
        runs = 1000
    mc = retirement.cached_monte_carlo(hid, plan, runs=runs) if result.get("ready") else None
    earliest = retirement.earliest_retirement(hid, plan) if result.get("ready") else None
    rows = retirement.plan_accounts(hid, plan)
    # Property and 529 savings sit in net worth but never fund ordinary withdrawals —
    # say so on the page, or their absence reads as a bug.
    held_ids = {a["id"] for a in rows}
    outside = [a for a in networth.list_accounts(hid)
               if a["id"] not in held_ids and not a.get("is_liability")
               and (a.get("type") or "") in ("real_estate", "other_asset", "529")
               and int(a.get("current_balance_cents") or 0) > 0]
    return render(request, "retirement.html", plan=plan, result=result, mc=mc, earliest=earliest,
                  account_rows=rows,
                  excluded_equity=sum(int(a["current_balance_cents"]) for a in outside),
                  excluded_equity_names=[a["name"] for a in outside])


@app.post("/retirement/save")
async def retirement_save(request: Request):
    form = await request.form()
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return redirect("/retirement?error=csrf")
    hid = user["household_id"]

    def num(name: str, default: float = 0.0) -> float:
        raw = str(form.get(name) or "").replace(",", "").replace("$", "").strip()
        try:
            value = float(raw) if raw else default
        except ValueError:
            return default
        # "nan" and "inf" parse fine and would poison every downstream comparison.
        return value if math.isfinite(value) else default

    def whole(name: str, default: int = 0) -> int:
        return int(round(num(name, default)))

    plan = retirement.load_plan(hid)
    own_account_ids = {a["id"] for a in networth.list_accounts(hid)}
    for i in (0, 1):
        person = plan["people"][i]
        person["name"] = str(form.get(f"p{i + 1}_name") or "").strip()[:40]
        person["birth_year"] = whole(f"p{i + 1}_birth_year", 0) or None
        person["retire_age"] = whole(f"p{i + 1}_retire_age", 65) or 65
        income = plan["income"][i]
        income["gross"] = num(f"gross{i + 1}")
        freq = str(form.get(f"freq{i + 1}") or "annual")
        income["frequency"] = freq if freq in retirement.PERIODS_PER_YEAR else "annual"
        income["raise_pct"] = num(f"raise_{i + 1}")
        income["savings_monthly"] = num(f"savings_{i + 1}")
        target = str(form.get(f"savings_acct_{i + 1}") or "").strip()
        income["savings_account_id"] = (
            int(target) if target.isdigit() and int(target) in own_account_ids else None)
        income["k401_pct"] = num(f"k401_{i + 1}")
        income["match_pct"] = num(f"match_{i + 1}")
        income["match_up_to_pct"] = num(f"match_cap_{i + 1}")
        plan["ss"][i]["pia"] = num(f"ss_pia{i + 1}")
        plan["ss"][i]["claim_age"] = whole(f"ss_claim{i + 1}", 67) or 67
    plan["spending"] = num("spending")
    plan["return_pct"] = num("return_pct", 6.0)
    plan["return_volatility_pct"] = num("return_volatility_pct", 15.0)
    plan["inflation_pct"] = num("inflation_pct", 3.0)
    plan["state_tax_pct"] = num("state_tax_pct", 5.75)
    plan["max_age"] = whole("max_age", 95) or 95
    plan["taxable_basis_pct"] = num("taxable_basis_pct", 70)
    plan["roth_conversion"] = num("roth_conversion")
    crypto_raw = str(form.get("crypto_return_pct") or "").strip()
    plan["crypto_return_pct"] = num("crypto_return_pct") if crypto_raw else None

    # Timed spending rows are addressed by index so an unchecked "inflates" box
    # cannot shift the others out of line. Blank rows are dropped, not saved as zeros.
    adjustments = []
    for idx in range(24):
        name = str(form.get(f"adj_name_{idx}") or "").strip()
        annual = num(f"adj_amount_{idx}")
        if not name and annual == 0:
            continue   # negatives are legitimate: a bill that stops
        adjustments.append({
            "name": name[:40] or "Adjustment",
            "annual": annual,
            "start_year": whole(f"adj_start_{idx}", 0) or None,
            "end_year": whole(f"adj_end_{idx}", 0) or None,
            "inflates": bool(str(form.get(f"adj_inflates_{idx}") or "").strip()),
        })
    plan["adjustments"] = adjustments

    # One basis override per taxable account, if the user typed one.
    basis = {}
    for key, value in form.items():
        if key.startswith("basis_acct_") and str(value).strip():
            try:
                basis[key.replace("basis_acct_", "")] = float(str(value).replace(",", "").replace("$", ""))
            except ValueError:
                continue
    plan["basis_by_account"] = basis

    plan["pension"]["monthly"] = num("pension_monthly")
    plan["pension"]["start_age"] = whole("pension_start", 65) or 65
    retirement.save_plan(hid, plan)
    audit("retirement.saved", request, user)
    return redirect("/retirement")


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, month: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    summary = reports.month_summary(hid, month or budgets.month_key())
    trend = reports.trend(hid, 12)
    scale = reports.trend_scale(max([t["expense_cents"] for t in trend] + [1]))
    return render(request, "reports.html", r=summary, trend=trend,
                  max_bar=scale["top_cents"], trend_scale=scale["ticks"])


@app.get("/reports/export.csv")
def reports_export(request: Request, month: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    month = month or budgets.month_key()
    start, end = reports._month_bounds(month)
    rows = reports.export_rows(hid, start, end)
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["Date", "Merchant", "Amount", "Category", "Account", "Notes"])
    def _safe(value) -> str:
        """A cell that starts with = + - @ is a formula in a spreadsheet. Neutralise text cells."""
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text

    for row in rows:
        writer.writerow([row["posted_on"], _safe(row["merchant"]), f"{row['amount_cents'] / 100:.2f}",
                         _safe(row["category"]), _safe(row["account"]), _safe(row["notes"])])
    audit("report.exported", request, user, month=month, rows=len(rows))
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="banana-stand-{month}.csv"'},
    )


# ---------------------------------------------------------------- goals
@app.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    return render(request, "goals.html", goal_list=goals.list_goals(hid),
                  accounts=networth.list_accounts(hid), saved=request.query_params.get("saved"),
                  today=db.local_now().date().isoformat())


@app.post("/goals")
def goal_create(request: Request, csrf: str = Form(""), name: str = Form(""),
                target: str = Form(""), target_date: str = Form(""),
                account_id: str = Form(""), notes: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/goals?error=csrf")
    try:
        goals.create(user["household_id"], name, money.parse_money(target) or 0,
                     target_date or None, int(account_id) if account_id.isdigit() else None,
                     notes or None)
    except ValueError as exc:
        return redirect(f"/goals?error={str(exc).replace(' ', '+')}")
    return redirect("/goals?saved=1")


@app.post("/goals/{goal_id}/contribute")
def goal_contribute(request: Request, goal_id: int, csrf: str = Form(""), amount: str = Form(""),
                    occurred_on: str = Form(""), note: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/goals?error=csrf")
    try:
        goals.contribute(user["household_id"], goal_id, money.parse_money(amount) or 0,
                         occurred_on or None, note or None)
    except ValueError as exc:
        return redirect(f"/goals?error={str(exc).replace(' ', '+')}")
    return redirect("/goals?saved=1")


@app.post("/goals/{goal_id}/update")
def goal_update(request: Request, goal_id: int, csrf: str = Form(""), name: str = Form(""),
                target: str = Form(""), target_date: str = Form(""), status: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/goals?error=csrf")
    fields: dict = {}
    if name:
        fields["name"] = name.strip()[:60]
    if target:
        fields["target_cents"] = money.parse_money(target) or 0
    if target_date:
        fields["target_date"] = target_date
    if status in ("active", "achieved", "paused"):
        fields["status"] = status
    goals.update(user["household_id"], goal_id, **fields)
    return redirect("/goals?saved=1")


@app.post("/goals/{goal_id}/delete")
def goal_delete(request: Request, goal_id: int, csrf: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if csrf_ok(request, csrf):
        goals.delete(user["household_id"], goal_id)
    return redirect("/goals")


# ---------------------------------------------------------------- alerts
@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    items = alerts.evaluate(hid)
    return render(request, "alerts.html", items=items, counts=alerts.counts(items),
                  settings={k: alerts.get_setting(hid, k) for k in alerts.DEFAULT_SETTINGS},
                  saved=request.query_params.get("saved"))


@app.post("/alerts/dismiss")
def alerts_dismiss(request: Request, csrf: str = Form(""), key: str = Form(""),
                   clear_all: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/alerts?error=csrf")
    hid = user["household_id"]
    if clear_all:
        alerts.dismiss(hid, [a["key"] for a in alerts.evaluate(hid, include_dismissed=True)])
    elif key:
        alerts.dismiss(hid, [key])
    return redirect("/alerts")


@app.post("/alerts/settings")
def alerts_settings(request: Request, csrf: str = Form(""), large_txn: str = Form(""),
                    bill_days: str = Form(""), forecast_days: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/alerts?error=csrf")
    hid = user["household_id"]
    if large_txn:
        alerts.set_setting(hid, "alert_large_txn_cents", money.parse_money(large_txn) or 50000)
    if bill_days.isdigit():
        alerts.set_setting(hid, "alert_low_days", max(1, min(30, int(bill_days))))
    if forecast_days.isdigit():
        alerts.set_setting(hid, "alert_forecast_days", max(7, min(180, int(forecast_days))))
    return redirect("/alerts?saved=1")


@app.post("/settings/household")
def settings_household(request: Request, csrf: str = Form(""), household: str = Form(""),
                       buffer: str = Form("")):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    if not csrf_ok(request, csrf):
        return redirect("/settings?error=csrf")
    hid = user["household_id"]
    name = (household or "").strip()[:80]
    current = db.query_one("SELECT name, buffer_cents FROM households WHERE id = ?", (hid,))
    cents = current["buffer_cents"]
    if buffer.strip():
        try:
            cents = max(0, money.parse_money(buffer) or 0)
        except ValueError:
            return redirect("/settings?error=That+buffer+is+not+a+number.")
    if name:
        db.execute("UPDATE households SET name = ?, buffer_cents = ? WHERE id = ?", (name, cents, hid))
    else:
        db.execute("UPDATE households SET buffer_cents = ? WHERE id = ?", (cents, hid))
    audit("household.updated", request, user, buffer_cents=cents)
    return redirect("/settings?saved=1")


@app.get("/settings/audit", response_class=HTMLResponse)
def audit_page(request: Request, action: str = ""):
    user = _require_or_redirect(request)
    if user is None:
        return redirect("/login")
    hid = user["household_id"]
    sql = "SELECT * FROM audit_log WHERE household_id = ?"
    args: list = [hid]
    if action:
        sql += " AND action = ?"
        args.append(action)
    sql += " ORDER BY id DESC LIMIT 200"
    rows = [dict(r) for r in db.query_all(sql, tuple(args))]
    actions = [r["action"] for r in db.query_all(
        "SELECT DISTINCT action FROM audit_log WHERE household_id = ? ORDER BY action", (hid,))]
    return render(request, "audit.html", rows=rows, actions=actions, action=action,
                  total=db.query_one("SELECT COUNT(*) AS n FROM audit_log WHERE household_id = ?",
                                     (hid,))["n"])


@app.exception_handler(404)
async def not_found(request: Request, _exc):
    return render(request, "404.html", status_code=404)
