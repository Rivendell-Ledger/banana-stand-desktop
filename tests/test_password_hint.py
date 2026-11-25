"""Password hints, setup sign-in, and changing credentials from Settings."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "correct horse battery staple 2"


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field"
    return m.group(1)


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


def create_household(client: TestClient, hint: str = "the first dog", email: str = ""):
    page = client.get("/setup")
    return client.post(
        "/setup",
        data={
            "csrf": csrf_of(page.text),
            "household": "The Banana Stand",
            "name": "Alex",
            "email": email,
            "password": PASSWORD,
            "password2": PASSWORD,
            "hint": hint,
        },
        follow_redirects=False,
    )


def sign_in(client: TestClient, email: str = "Alex", password: str = PASSWORD):
    page = client.get("/login")
    return client.post(
        "/login",
        data={"csrf": csrf_of(page.text), "email": email, "password": password},
        follow_redirects=False,
    )


def test_setup_signs_the_owner_in(client):
    """No second login screen: setup ends with a live session."""
    r = create_household(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/connect"
    assert client.cookies.get("bs_session")
    # The home page renders instead of bouncing to /login.
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200


def test_hint_is_stored_and_returned(client):
    create_household(client, hint="  the   first dog ")
    page = client.get("/login")
    token = csrf_of(page.text)
    r = client.post("/login/hint", data={"csrf": token, "handle": "Alex"})
    assert r.status_code == 200
    assert r.json()["hint"] == "the first dog"


def test_hint_lookup_is_empty_for_unknown_names(client):
    create_household(client)
    token = csrf_of(client.get("/login").text)
    r = client.post("/login/hint", data={"csrf": token, "handle": "nobody"})
    assert r.status_code == 200
    assert r.json()["hint"] == ""


def test_hint_lookup_rejects_missing_csrf(client):
    create_household(client)
    r = client.post("/login/hint", data={"handle": "Alex"})
    assert r.status_code == 400


def test_hint_is_optional(client):
    create_household(client, hint="")
    token = csrf_of(client.get("/login").text)
    assert client.post("/login/hint", data={"csrf": token, "handle": "Alex"}).json()["hint"] == ""


def test_overlong_hint_is_refused_at_setup(client):
    r = create_household(client, hint="x" * 81)
    assert r.status_code == 400
    assert "80" in r.text


def test_password_change_requires_the_current_password(client):
    create_household(client)
    token = csrf_of(client.get("/settings").text)
    r = client.post(
        "/settings/password",
        data={"csrf": token, "current": "wrong", "password": NEW_PASSWORD, "password2": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "pwerr" in r.headers["location"]
    # The old password still works.
    with TestClient(app) as other:
        assert sign_in(other).status_code == 303


def test_password_change_and_other_sessions_are_dropped(client):
    create_household(client)
    other = TestClient(app)
    with other:
        assert sign_in(other).status_code == 303
        assert other.get("/settings", follow_redirects=False).status_code == 200

        token = csrf_of(client.get("/settings").text)
        r = client.post(
            "/settings/password",
            data={"csrf": token, "current": PASSWORD, "password": NEW_PASSWORD, "password2": NEW_PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "saved=password" in r.headers["location"]

        # The session that made the change survives; the other one does not.
        assert client.get("/settings", follow_redirects=False).status_code == 200
        assert other.get("/settings", follow_redirects=False).status_code == 303

    with TestClient(app) as fresh:
        assert sign_in(fresh, password=PASSWORD).status_code == 401
        assert sign_in(fresh, password=NEW_PASSWORD).status_code == 303


def test_hint_can_be_changed_and_cleared(client):
    create_household(client, hint="old hint")
    token = csrf_of(client.get("/settings").text)
    r = client.post("/settings/hint", data={"csrf": token, "hint": "new hint"},
                    follow_redirects=False)
    assert "saved=hint" in r.headers["location"]
    assert client.get("/settings").text.count("new hint") >= 1

    token = csrf_of(client.get("/settings").text)
    client.post("/settings/hint", data={"csrf": token, "hint": ""}, follow_redirects=False)
    token = csrf_of(client.get("/login").text)
    assert client.post("/login/hint", data={"csrf": token, "handle": "Alex"}).json()["hint"] == ""


def test_settings_requires_csrf_for_password_and_hint(client):
    create_household(client)
    before = db.query_one("SELECT password_hash, password_hint FROM users WHERE id = 1")
    assert before is not None
    client.post("/settings/password", data={"current": PASSWORD, "password": NEW_PASSWORD,
                                            "password2": NEW_PASSWORD}, follow_redirects=False)
    client.post("/settings/hint", data={"hint": "sneaky"}, follow_redirects=False)
    after = db.query_one("SELECT password_hash, password_hint FROM users WHERE id = 1")
    assert after is not None
    assert before["password_hash"] == after["password_hash"]
    assert before["password_hint"] == after["password_hint"]
