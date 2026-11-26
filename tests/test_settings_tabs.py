"""Settings is split into tabs; the Profile tab owns name, username and household."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import db, security
from app.main import app

PASSWORD = "correct horse battery staple"


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


def make_household(client: TestClient):
    page = client.get("/setup")
    return client.post(
        "/setup",
        data={"csrf": csrf_of(page.text), "household": "The Banana Stand", "name": "Alex",
              "email": "", "password": PASSWORD, "password2": PASSWORD, "hint": ""},
        follow_redirects=False,
    )


def test_each_tab_renders_only_its_own_section(client):
    make_household(client)

    page = client.get("/settings?tab=security")
    assert "Change password" in page.text
    assert "Connect a bank" not in page.text

    page = client.get("/settings?tab=bank")
    assert "Bank connections" in page.text
    assert "Change password" not in page.text

    page = client.get("/settings?tab=phone")
    assert "Allow devices on this network to connect" in page.text
    assert "Change password" not in page.text

    page = client.get("/settings?tab=about")
    assert "MIT" in page.text
    assert "Change password" not in page.text


def test_unknown_tab_falls_back_to_profile(client):
    make_household(client)
    page = client.get("/settings?tab=nonsense")
    assert page.status_code == 200
    assert "Save profile" in page.text


def test_profile_saves_name_username_household_and_buffer(client):
    make_household(client)
    token = csrf_of(client.get("/settings?tab=profile").text)
    r = client.post(
        "/settings/profile",
        data={"csrf": token, "display_name": "Alexandra", "username": "Alexandra",
              "household": "The New Stand", "buffer": "2000"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    user = db.query_one("SELECT email, display_name FROM users")
    assert user is not None
    assert user["email"] == "alexandra"      # handles are normalised
    assert user["display_name"] == "Alexandra"

    household = db.query_one("SELECT name, buffer_cents FROM households")
    assert household is not None
    assert household["name"] == "The New Stand"
    assert household["buffer_cents"] == 200000


def test_username_is_what_the_sign_in_page_accepts(client):
    make_household(client)
    token = csrf_of(client.get("/settings?tab=profile").text)
    client.post(
        "/settings/profile",
        data={"csrf": token, "display_name": "Alex", "username": "zach",
              "household": "The Banana Stand", "buffer": "1500"},
        headers={"X-Requested-With": "fetch"},
    )

    page = client.get("/login")
    assert page.status_code == 200
    ok = client.post("/login", data={"csrf": csrf_of(page.text), "email": "zach",
                                     "password": PASSWORD, "remember": ""},
                     follow_redirects=False)
    assert ok.status_code in (302, 303)


def test_a_username_already_in_use_is_rejected(client):
    make_household(client)
    row = db.query_one("SELECT household_id FROM users")
    assert row is not None
    hid = row["household_id"]
    db.execute(
        "INSERT INTO users(household_id, email, display_name, password_hash, created_at)"
        " VALUES (?,?,?,?,?)",
        (hid, "taken", "Someone", security.hash_password(PASSWORD), db.utcnow()),
    )

    token = csrf_of(client.get("/settings?tab=profile").text)
    r = client.post(
        "/settings/profile",
        data={"csrf": token, "display_name": "Alex", "username": "taken",
              "household": "The Banana Stand", "buffer": "1500"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 400
    assert "already taken" in r.json()["error"]


def test_installing_from_the_app_is_refused_outside_the_windows_build(client):
    make_household(client)
    token = csrf_of(client.get("/settings?tab=updates").text)
    r = client.post("/settings/install", data={"csrf": token},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400
    assert "Windows build" in r.json()["error"]
