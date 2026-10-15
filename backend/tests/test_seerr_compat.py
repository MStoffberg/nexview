"""Seerr-shaped compatibility API used by Moonfin.

The adapter is deliberately narrower than Seerr itself: these tests hold the
contract Moonfin consumes while Nexview remains the source of truth for
identity, permissions, requests, and procurement.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.models import MediaType, RequestStatus, User
from app.schemas_media import MediaItem, MediaPage

from .conftest import ADMIN


def _moonfin_session(client: TestClient) -> dict:
    created = client.post("/api/setup/admin", json=ADMIN)
    assert created.status_code == 201, created.text
    token = client.cookies.get("nexview_refresh")
    assert token
    client.cookies.set("connect.sid", token, path="/")
    return created.json()


def _media_item(media_type: MediaType, tmdb_id: int, title: str) -> MediaItem:
    return MediaItem(media_type=media_type, tmdb_id=tmdb_id, title=title)


def test_seerr_status_is_public_and_identifies_nexview(client: TestClient) -> None:
    response = client.get("/api/v1/status")

    assert response.status_code == 200
    assert response.json()["version"]
    assert response.json()["updateAvailable"] is False
    assert response.json()["commitTag"] == "nexview-moonfin"


def test_seerr_auth_me_accepts_the_moonfin_session_cookie(client: TestClient) -> None:
    _moonfin_session(client)

    response = client.get("/api/v1/auth/me", headers={"Authorization": ""})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] > 0
    assert body["username"] == ADMIN["username"]
    assert body["displayName"] == ADMIN["username"]
    assert body["permissions"] & 2  # Seerr ADMIN
    assert body["permissions"] & 16  # Seerr MANAGE_REQUESTS


def test_seerr_auth_me_rejects_an_invalid_cookie(client: TestClient) -> None:
    client.cookies.set("connect.sid", "not-a-session", path="/")

    response = client.get("/api/v1/auth/me", headers={"Authorization": ""})

    assert response.status_code == 403


def test_seerr_compatibility_logout_revokes_the_session(client: TestClient) -> None:
    _moonfin_session(client)
    stolen = client.cookies.get("connect.sid")
    assert stolen

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    client.cookies.set("connect.sid", stolen, path="/")
    rejected = client.get("/api/v1/auth/me", headers={"Authorization": ""})
    assert rejected.status_code == 403


def test_seerr_compatibility_rejects_child_sessions(client: TestClient) -> None:
    admin = _moonfin_session(client)
    created = client.post(
        "/api/children",
        json={"username": "moonfin-kind", "password": "kind-passwort", "age": 8},
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert created.status_code == 201, created.text

    login = client.post(
        "/api/auth/login",
        json={"username": "moonfin-kind", "password": "kind-passwort"},
        headers={"Authorization": ""},
    )
    assert login.status_code == 200, login.text
    client.cookies.set("connect.sid", client.cookies.get("nexview_refresh"), path="/")

    response = client.get("/api/v1/auth/me", headers={"Authorization": ""})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "not_for_children"


def test_seerr_public_settings_have_the_fields_moonfin_reads(client: TestClient) -> None:
    response = client.get("/api/v1/settings/public")

    assert response.status_code == 200
    body = response.json()
    assert body["applicationTitle"] == "Nexview"
    assert body["initialized"] is False
    assert body["movie4kEnabled"] is False
    assert body["series4kEnabled"] is False


def test_seerr_service_list_maps_nexview_versions_to_standard_and_4k(
    client: TestClient,
) -> None:
    _moonfin_session(client)
    response = client.get("/api/v1/service/radarr", headers={"Authorization": ""})

    assert response.status_code == 200, response.text
    servers = response.json()
    assert servers[0]["name"].endswith("Full HD")
    assert servers[0]["is4k"] is False
    assert any(server["name"].endswith("4K") and server["is4k"] for server in servers)


def test_seerr_search_combines_movies_and_series(
    client: TestClient, monkeypatch
) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat

    async def fake_search(media_type, user, db, q, page=1):
        assert q == "Alien"
        title = "Alien" if media_type == "movie" else "Alien Earth"
        return MediaPage(
            page=page,
            total_pages=1,
            total_results=1,
            items=[
                MediaItem(
                    media_type=MediaType(media_type),
                    tmdb_id=10 if media_type == "movie" else 20,
                    title=title,
                    poster_url="https://image.tmdb.org/t/p/w500/poster.jpg",
                    release_date="2026-01-02",
                )
            ],
        )

    monkeypatch.setattr(seerr_compat.discover, "search", fake_search)
    response = client.get("/api/v1/search?query=Alien", headers={"Authorization": ""})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totalResults"] == 2
    assert [item["mediaType"] for item in body["results"]] == ["movie", "tv"]
    assert body["results"][0]["posterPath"] == "/poster.jpg"


def test_seerr_movie_detail_translates_availability(
    client: TestClient, monkeypatch
) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat

    async def fake_detail(media_type, tmdb_id, user, db):
        assert media_type == "movie"
        assert tmdb_id == 42
        return MediaItem(
            media_type=MediaType.movie,
            tmdb_id=42,
            title="The Answer",
            overview="Everything.",
            status="downloaded",
            vote_average=8.5,
            runtime_minutes=110,
        )

    monkeypatch.setattr(seerr_compat.details, "title_detail", fake_detail)
    response = client.get("/api/v1/movie/42", headers={"Authorization": ""})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == 42
    assert body["title"] == "The Answer"
    assert body["mediaInfo"]["status"] == 5  # Seerr AVAILABLE


def test_seerr_recommendations_use_nexview_status_enrichment(
    client: TestClient, monkeypatch
) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat

    async def fake_recommendations(media_type, tmdb_id, user, db, runde=0):
        assert (media_type, tmdb_id, runde) == ("movie", 42, 0)
        return type(
            "Selection",
            (),
            {
                "items": [_media_item(MediaType.movie, 502, "Related")],
                "runde": 0,
                "runden": 1,
            },
        )()

    monkeypatch.setattr(seerr_compat.details, "recommendations", fake_recommendations)
    response = client.get(
        "/api/v1/movie/42/recommendations?page=1", headers={"Authorization": ""}
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["title"] == "Related"


def test_seerr_request_creation_maps_4k_to_nexview_uhd(
    client: TestClient, monkeypatch
) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat

    captured = {}

    async def fake_create(payload, user, db):
        captured["payload"] = payload
        return type(
            "FakeRequest",
            (),
            {
                "id": 7,
                "media_type": MediaType.movie,
                "tmdb_id": 42,
                "title": "The Answer",
                "poster_path": None,
                "release_date": "2026-01-02",
                "status": RequestStatus.pending_approval,
                "tier": "uhd",
                "requested_at": datetime.now(UTC),
                "approved_at": None,
                "completed_at": None,
                "season": None,
                "episodes": None,
            },
        )()

    monkeypatch.setattr(seerr_compat.requests_router, "create_request", fake_create)
    response = client.post(
        "/api/v1/request",
        json={"mediaType": "movie", "mediaId": 42, "is4k": True},
        headers={"Authorization": ""},
    )

    assert response.status_code == 201, response.text
    assert captured["payload"].media_type == MediaType.movie
    assert captured["payload"].tier == "uhd"
    assert response.json()["status"] == 1  # Seerr PENDING APPROVAL
    assert response.json()["is4k"] is True


def test_seerr_jellyfin_login_issues_a_long_lived_moonfin_cookie(
    client: TestClient, monkeypatch
) -> None:
    client.post("/api/setup/admin", json=ADMIN)
    from app.routers import seerr_compat

    async def fake_login_user(payload, request, db):
        assert payload.provider == "jellyfin"
        assert payload.username == "admin"
        return db.query(User).filter(User.username == "admin").one()

    monkeypatch.setattr(
        seerr_compat.mediaserver_router, "passwort_benutzer", fake_login_user
    )
    response = client.post(
        "/api/v1/auth/jellyfin",
        json={"username": "admin", "password": "jellyfin-password"},
        headers={"Authorization": ""},
    )

    assert response.status_code == 200, response.text
    assert response.json()["username"] == "admin"
    assert client.cookies.get("connect.sid")

    # The compatibility cookie is a refresh token, not a 30-minute access
    # token. It must still authenticate later API calls through the same
    # account/session validity checks as Nexview's own refresh route.
    me = client.get("/api/v1/auth/me", headers={"Authorization": ""})
    assert me.status_code == 200, me.text


def test_seerr_request_list_is_paginated_and_scoped_to_the_current_user(
    client: TestClient, monkeypatch
) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat

    def fake_mine(user, db):
        now = datetime.now(UTC)
        return [
            type(
                "FakeRequest",
                (),
                {
                    "id": number,
                    "media_type": MediaType.movie,
                    "tmdb_id": 100 + number,
                    "title": f"Movie {number}",
                    "status": RequestStatus.pending_approval,
                    "tier": "standard",
                    "requested_at": now,
                    "approved_at": None,
                    "completed_at": None,
                    "season": None,
                },
            )()
            for number in (3, 2, 1)
        ]

    monkeypatch.setattr(seerr_compat.requests_router, "my_requests", fake_mine)
    response = client.get(
        "/api/v1/request?take=1&skip=1", headers={"Authorization": ""}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pageInfo"] == {"pages": 3, "page": 2, "results": 3}
    assert [item["id"] for item in body["results"]] == [2]


def test_seerr_request_count_uses_only_visible_requests(client: TestClient, monkeypatch) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat

    def fake_mine(user, db):
        return [
            type("R", (), {"media_type": MediaType.movie, "status": RequestStatus.pending_approval})(),
            type("R", (), {"media_type": MediaType.tv, "status": RequestStatus.downloaded})(),
        ]

    monkeypatch.setattr(seerr_compat.requests_router, "my_requests", fake_mine)
    response = client.get("/api/v1/request/count", headers={"Authorization": ""})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "total": 2,
        "movie": 1,
        "tv": 1,
        "pending": 1,
        "approved": 0,
        "declined": 0,
        "processing": 0,
        "available": 1,
        "completed": 1,
    }


def test_seerr_discover_movies_delegates_to_nexview_policy(
    client: TestClient, monkeypatch
) -> None:
    _moonfin_session(client)
    from app.routers import seerr_compat

    captured: dict = {}

    async def fake_discover(media_type, user, db, **filters):
        captured.update(media_type=media_type, **filters)
        return MediaPage(
            page=2,
            total_pages=4,
            total_results=70,
            items=[_media_item(MediaType.movie, 501, "Discovery")],
        )

    monkeypatch.setattr(seerr_compat.discover, "discover", fake_discover)
    response = client.get(
        "/api/v1/discover/movies?page=2&sortBy=vote_average.desc&genre=28",
        headers={"Authorization": ""},
    )

    assert response.status_code == 200, response.text
    assert captured["media_type"] == "movie"
    assert captured["sort"] == "rating"
    assert captured["genre_id"] == 28
    assert response.json()["results"][0]["title"] == "Discovery"
