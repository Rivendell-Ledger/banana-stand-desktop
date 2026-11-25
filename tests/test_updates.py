"""Opt-in update checks: off by default, cached, and never a source of page errors."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import config, db, updates
from app.main import app

PASSWORD = "correct horse battery staple"


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field"
    return m.group(1)


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    db.migrate()
    monkeypatch.setattr(updates, "_fetch_latest", lambda: pytest.fail("network call while disabled"))
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


def test_parse_version_is_numeric_not_lexical():
    assert updates.parse_version("v1.21.9") < updates.parse_version("v1.21.10")
    assert updates.parse_version("1.21.12") == (1, 21, 12)
    assert updates.parse_version("v1.21.12-beta.1") == (1, 21, 12)
    assert updates.is_newer("1.21.13", "1.21.12")
    assert not updates.is_newer("1.21.12", "1.21.12")
    assert not updates.is_newer("1.21.11", "1.21.12")


def test_disabled_by_default_and_never_calls_out(client, monkeypatch):
    make_household(client)
    assert updates.enabled(1) is False
    assert updates.refresh(1) is None
    assert updates.notice(1) is None
    # The fixture's _fetch_latest raises if anything tries to reach GitHub.
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200
    assert "is available" not in page.text


def test_enabled_check_records_a_newer_release(client, monkeypatch):
    make_household(client)
    calls = []

    def fake_fetch():
        calls.append(1)
        return {"latest": "1.21.13", "url": "https://example.test/rel", "published": "", "checked_at": 0}

    monkeypatch.setattr(updates, "_fetch_latest", fake_fetch)
    updates.set_enabled(1, True)
    updates.refresh(1)
    assert len(calls) == 1

    notice = updates.notice(1)
    assert notice is not None
    assert notice["latest"] == "1.21.13"
    assert notice["current"] == config.VERSION

    page = client.get("/", follow_redirects=False)
    assert "Version 1.21.13 is available" in page.text
    assert "https://example.test/rel" in page.text


def test_the_answer_is_cached_between_renders(client, monkeypatch):
    make_household(client)
    calls = []

    def fake_fetch():
        calls.append(1)
        return {"latest": "1.21.13", "url": "https://example.test/rel", "published": "", "checked_at": 0}

    monkeypatch.setattr(updates, "_fetch_latest", fake_fetch)
    updates.set_enabled(1, True)
    updates.refresh(1)
    client.get("/")
    client.get("/transactions")
    updates.refresh(1)  # still inside the six-hour window
    assert len(calls) == 1


def test_same_or_older_release_shows_no_banner(client, monkeypatch):
    make_household(client)
    monkeypatch.setattr(updates, "_fetch_latest", lambda: {
        "latest": config.VERSION, "url": "https://example.test/rel", "published": "", "checked_at": 0})
    updates.set_enabled(1, True)
    updates.refresh(1)
    assert updates.notice(1) is None


def test_offline_failure_is_silent_and_still_stamped(client, monkeypatch):
    make_household(client)
    monkeypatch.setattr(updates, "_fetch_latest", lambda: (_ for _ in ()).throw(OSError("offline")))
    updates.set_enabled(1, True)
    updates.refresh(1)
    assert updates.notice(1) is None
    record = updates._record(1)
    assert record.get("checked_at")  # stamped so a dead network is not retried per request


def test_settings_toggle_persists_and_clears_the_cache(client, monkeypatch):
    make_household(client)
    monkeypatch.setattr(updates, "_fetch_latest", lambda: {
        "latest": "1.21.13", "url": "https://example.test/rel", "published": "", "checked_at": 0})
    token = csrf_of(client.get("/settings").text)
    r = client.post("/settings/updates", data={"csrf": token, "check": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert updates.enabled(1) is True

    token = csrf_of(client.get("/settings").text)
    client.post("/settings/updates", data={"csrf": token}, follow_redirects=False)
    assert updates.enabled(1) is False
    assert updates.notice(1) is None


def test_toggle_requires_csrf(client):
    make_household(client)
    client.post("/settings/updates", data={"check": "1"}, follow_redirects=False)
    assert updates.enabled(1) is False
