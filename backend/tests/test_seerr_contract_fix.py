"""Additional RED contract tests for Moonfin adapter correctness."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import MediaType

from .conftest import ADMIN


def _moonfin_session(client: TestClient) -> dict:
    created = client.post("/api/setup/admin", json=ADMIN)
    assert created.status_code == 201, created.text
    token = client.cookies.get("nexview_refresh")
    assert token
    client.cookies.set("connect.sid", token, path="/")
    return created.json()


# ---------- Permission-bit alignment ----------

def test_permissions_use_client_bitmask(client: TestClient) -> None:
    """Moonfin client uses SeerrPermission bits: autoApprove=128, request4k=1024,
    manageIssues=1048576, viewIssues=2097152, createIssues=4194304.
    The adapter must emit those, not older/synthetic values."""
    _moonfin_session(client)
    body = client.get("/api/v1/auth/me", headers={"Authorization": ""}).json()
    # Admin must have ADMIN (2) and MANAGE_ISSUES (1048576)
    assert body["permissions"] & 2
    assert body["permissions"] & 1048576, "missing manageIssues bit (1048576)"
    # Basic user must have request (32) and not fake high bits
    assert body["permissions"] & 32


def test_permission_auto_approve_bit_is_128(client: TestClient) -> None:
    _moonfin_session(client)
    # Force a user that has auto_approve via monkeypatch isn't easy here,
    # so we assert the adapter defines AUTO_APPROVE = 128 not 64.
    from app.routers import seerr_compat
    assert seerr_compat.AUTO_APPROVE == 128, "AUTO_APPROVE must match SeerrPermission.autoApprove"
    assert seerr_compat.REQUEST_4K == 1024, "REQUEST_4K must match SeerrPermission.request4k"


# ---------- Synthetic profile / root folder must NOT reach procurement ----------

def test_create_request_ignores_synthetic_profile_and_root(client: TestClient, monkeypatch) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat
    captured: dict = {}

    async def fake_create(payload, user, db):
        captured["profile"] = payload.quality_profile_id
        captured["root"] = payload.root_folder_path
        # Return dummy request
        return type("R", (), {
            "id": 1, "media_type": MediaType.movie, "tmdb_id": 42,
            "title": "T", "poster_path": None, "release_date": None,
            "status": __import__("app.models", fromlist=["RequestStatus"]).RequestStatus.pending_approval,
            "tier": "standard", "requested_at": __import__("datetime", fromlist=["datetime"]).datetime.now(__import__("datetime", fromlist=["timezone"]).timezone.utc),
            "approved_at": None, "completed_at": None, "season": None, "episodes": None,
        })()

    monkeypatch.setattr(seerr_compat.requests_router, "create_request", fake_create)
    client.post(
        "/api/v1/request",
        json={"mediaType": "movie", "mediaId": 42, "is4k": False,
         "profileId": 1, "rootFolder": "/media"},
        headers={"Authorization": ""},
    )
    # Synthetic profile/root must NOT be forwarded to native procurement
    assert captured.get("profile") is None, "synthetic profile_id forwarded"
    assert captured.get("root") is None, "synthetic rootFolder /media forwarded"


# ---------- Request status mapping ----------

def test_request_status_downloaded_is_completed(client: TestClient, monkeypatch) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat
    # Direct unit check of mapping table
    assert seerr_compat._request_status("downloaded") == 5, "downloaded must be Seerr COMPLETED (5)"
    assert seerr_compat._request_status("pending_approval") == 1
    assert seerr_compat._request_status("approved") == 2
