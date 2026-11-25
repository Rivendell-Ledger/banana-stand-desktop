"""Settings saves: one section reports at a time, and fetch calls get JSON back."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import db
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


def test_only_the_section_that_saved_reports_it(client):
    make_household(client)

    page = client.get("/settings?saved=password")
    assert page.text.count("Password changed.") == 1
    assert "Hint saved." not in page.text
    assert "Household saved." not in page.text

    page = client.get("/settings?saved=household")
    assert page.text.count("Household saved.") == 1
    assert "Password changed." not in page.text


def test_hint_save_reports_only_the_hint(client):
    make_household(client)
    token = csrf_of(client.get("/settings").text)
    r = client.post("/settings/hint", data={"csrf": token, "hint": "the first dog"},
                    follow_redirects=True)
    assert "Hint saved." in r.text
    assert "Password changed." not in r.text
    assert "Household saved." not in r.text


def test_fetch_save_returns_json_instead_of_a_redirect(client):
    make_household(client)
    token = csrf_of(client.get("/settings").text)
    r = client.post(
        "/settings/hint",
        data={"csrf": token, "hint": "the first dog"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Hint saved."}


def test_fetch_save_reports_bad_input_as_json(client):
    make_household(client)
    token = csrf_of(client.get("/settings").text)
    r = client.post(
        "/settings/password",
        data={"csrf": token, "current": "wrong", "password": "new-password-1",
              "password2": "new-password-1"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert "incorrect" in r.json()["error"]


def test_fetch_household_save_updates_the_row(client):
    make_household(client)
    token = csrf_of(client.get("/settings").text)
    r = client.post(
        "/settings/household",
        data={"csrf": token, "household": "The New Stand", "buffer": "1500"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.json()["ok"] is True
    row = db.query_one("SELECT name, buffer_cents FROM households")
    assert row is not None
    assert row["name"] == "The New Stand"
    assert row["buffer_cents"] == 150000
