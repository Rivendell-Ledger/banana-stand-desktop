"""Phase 0 acceptance: setup, login, session, CSRF, health, headers."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

PASSWORD = "correct horse battery staple"


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    db.migrate()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def create_household(client, email="alex@example.com"):
    page = client.get("/setup")
    assert page.status_code == 200
    token = csrf_of(page.text)
    return client.post(
        "/setup",
        data={
            "csrf": token,
            "household": "The Banana Stand",
            "name": "Alex",
            "email": email,
            "password": PASSWORD,
            "password2": PASSWORD,
        },
        follow_redirects=False,
    )


def sign_in(client, email="alex@example.com", password=PASSWORD):
    page = client.get("/login")
    token = csrf_of(page.text)
    return client.post(
        "/login",
        data={"csrf": token, "email": email, "password": password},
        follow_redirects=False,
    )


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert "version" in body


def test_first_run_redirects_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_setup_rejects_bad_csrf(client):
    client.get("/setup")
    r = client.post(
        "/setup",
        data={"csrf": "wrong", "household": "X", "name": "Z", "email": "z@example.com",
              "password": PASSWORD, "password2": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Session expired" in r.text


def test_setup_creates_household_and_user(client):
    r = create_household(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?created=1"
    assert db.count("users") == 1
    row = db.query_one("SELECT email, display_name FROM users")
    assert row["email"] == "alex@example.com"
    assert row["display_name"] == "Alex"
    # the door closes behind you
    again = client.get("/setup", follow_redirects=False)
    assert again.status_code == 303
    assert again.headers["location"] == "/login"


def test_login_rejects_wrong_password(client):
    create_household(client)
    r = sign_in(client, password="wrong password entirely")
    assert r.status_code == 401
    assert "incorrect" in r.text.lower()


def test_login_then_home_then_logout(client):
    create_household(client)
    r = sign_in(client)
    assert r.status_code == 303
    # Desktop builds route a fresh household into the guided bank-connect step.
    assert r.headers["location"] == "/setup/connect"
    assert "bs_session" in r.cookies or "bs_session" in client.cookies

    home = client.get("/")
    assert home.status_code == 200
    assert "Coming up" in home.text
    assert "Alex" in home.text
    assert "% of month" in home.text

    page = client.get("/")
    token = csrf_of(page.text)
    out = client.post("/logout", data={"csrf": token}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_security_headers(client):
    r = client.get("/healthz")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"


def test_404_page(client):
    r = client.get("/nope")
    assert r.status_code == 404
    assert "huge mistake" in r.text


def test_password_hashing_roundtrip():
    from app.security import hash_password, verify_password

    h = hash_password(PASSWORD)
    assert h.startswith("$argon2id$")
    assert verify_password(h, PASSWORD)
    assert not verify_password(h, PASSWORD + "x")


def test_privacy_mode_is_one_header_toggle_and_survives_reloads(client):
    """Server-rendered from the cookie, so nothing is legible before the blur lands."""
    create_household(client)
    sign_in(client)
    plain = client.get("/")
    assert 'class="is-private"' not in plain.text
    assert plain.text.count("data-privacy") == 1          # the header toggle
    assert 'class="mny"' in plain.text                    # every amount is blur-able

    client.cookies.set("bs_privacy", "1")
    hidden = client.get("/")
    assert 'class="is-private"' in hidden.text
    assert hidden.text.count("data-privacy") == 1
