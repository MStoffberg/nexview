"""Independent regression checks for compatibility session revocation."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import User, utcnow

from .conftest import ADMIN


def test_compat_access_cookie_cannot_survive_logout(client: TestClient) -> None:
    created = client.post("/api/setup/admin", json=ADMIN)
    assert created.status_code == 201
    access = created.json()["access_token"]
    client.cookies.clear()
    client.cookies.set("connect.sid", access, path="/api/v1")
    before = client.get("/api/v1/auth/me")
    # An access cookie may be rejected entirely, or supported and revocable.
    assert before.status_code in (200, 403)
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.set("connect.sid", access, path="/api/v1")
    assert client.get("/api/v1/auth/me").status_code == 403


def test_compat_cookie_rejected_after_account_suspension(client: TestClient) -> None:
    assert client.post("/api/setup/admin", json=ADMIN).status_code == 201
    client.cookies.set("connect.sid", client.cookies.get("nexview_refresh"), path="/api/v1")
    assert client.get("/api/v1/auth/me").status_code == 200
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == ADMIN["username"]))
        user.is_active = False
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 403


def test_compat_cookie_rejected_after_password_change(client: TestClient) -> None:
    assert client.post("/api/setup/admin", json=ADMIN).status_code == 201
    client.cookies.set("connect.sid", client.cookies.get("nexview_refresh"), path="/api/v1")
    assert client.get("/api/v1/auth/me").status_code == 200
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == ADMIN["username"]))
        user.password_changed_at = utcnow() + timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 403
