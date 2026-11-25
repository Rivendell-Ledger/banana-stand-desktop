"""Remember me on the sign-in page: longer session plus a pre-filled name."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import config, db
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


def sign_in(client: TestClient, remember: bool):
    page = client.get("/login")
    data = {"csrf": csrf_of(page.text), "email": "Alex", "password": PASSWORD}
    if remember:
        data["remember"] = "1"
    return client.post("/login", data=data, follow_redirects=False)


def cookie_header(response, name: str) -> str:
    found = [c for c in response.headers.get_list("set-cookie") if c.startswith(name + "=")]
    assert found, f"no {name} cookie in {response.headers.get_list('set-cookie')}"
    return found[-1]


def test_remember_me_checkbox_is_on_the_login_card(client):
    make_household(client)
    client.post("/logout", data={"csrf": csrf_of(client.get("/").text)}, follow_redirects=False)
    page = client.get("/login")
    assert page.status_code == 200
    assert 'name="remember"' in page.text
    assert "Remember me" in page.text


def test_remember_me_extends_the_session_and_stores_the_name(client):
    make_household(client)
    client.post("/logout", data={"csrf": csrf_of(client.get("/").text)}, follow_redirects=False)

    r = sign_in(client, remember=True)
    assert r.status_code == 303
    session = cookie_header(r, "bs_session")
    assert f"Max-Age={config.REMEMBER_DAYS * 86400}" in session
    assert cookie_header(r, "bs_last_user")
    assert f"Max-Age={config.REMEMBER_DAYS * 86400}" in cookie_header(r, "bs_last_user")


def test_without_remember_me_the_session_is_short_and_the_name_is_forgotten(client):
    make_household(client)
    client.post("/logout", data={"csrf": csrf_of(client.get("/").text)}, follow_redirects=False)

    r = sign_in(client, remember=False)
    assert r.status_code == 303
    assert f"Max-Age={config.SESSION_DAYS * 86400}" in cookie_header(r, "bs_session")
    # The name cookie is cleared rather than left behind.
    assert 'bs_last_user=""' in cookie_header(r, "bs_last_user")


def test_remembered_name_is_prefilled_on_the_next_visit(client):
    make_household(client)
    client.post("/logout", data={"csrf": csrf_of(client.get("/").text)}, follow_redirects=False)
    sign_in(client, remember=True)
    client.post("/logout", data={"csrf": csrf_of(client.get("/").text)}, follow_redirects=False)

    page = client.get("/login")
    assert 'value="alex"' in page.text
    assert re.search(r'name="remember"[^>]*checked', page.text)


def test_failed_attempt_keeps_the_checkbox_checked(client):
    make_household(client)
    client.post("/logout", data={"csrf": csrf_of(client.get("/").text)}, follow_redirects=False)

    page = client.get("/login")
    r = client.post(
        "/login",
        data={"csrf": csrf_of(page.text), "email": "Alex", "password": "wrong-one",
              "remember": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert re.search(r'name="remember"[^>]*checked', r.text)
    assert 'value="Alex"' in r.text or 'value="alex"' in r.text
