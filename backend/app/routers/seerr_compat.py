"""A deliberately small Seerr-compatible surface for Moonfin.

This router translates Seerr-shaped HTTP requests into Nexview's own account
and policy model. It is not a second source of truth and it must never call
Radarr, Sonarr, or NexCrate around Nexview's request services.

The compatibility session uses Seerr's conventional ``connect.sid`` cookie so
Moonfin can retain it. Its value is a signed Nexview token and is validated
with the same account, password-change, and logout checks as the native API.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import __version__, meldungen
from ..config import get_settings
from ..db import get_db
from ..deps import has_any_user, require_adult, user_from_token
from ..models import MediaRequest, MediaType, User
from ..schemas_requests import RequestCreate
from ..services import sitzung
from ..services.settings_service import load_settings
from . import details, discover
from . import mediaserver as mediaserver_router
from . import requests as requests_router

router = APIRouter(prefix="/api/v1", tags=["moonfin-seerr-compat"])

# Deliberate additions to the otherwise frozen native v1 surface. Keeping the
# list here makes the compatibility boundary reviewable without pretending
# these Seerr-shaped responses are part of Nexview's native v1 contract.
SEERR_ZUGESAGT = (
    "/api/v1/status",
    "/api/v1/settings/public",
    "/api/v1/auth/jellyfin",
    "/api/v1/auth/me",
    "/api/v1/auth/logout",
    "/api/v1/search",
    "/api/v1/discover/movies",
    "/api/v1/discover/tv",
    "/api/v1/discover/trending",
    "/api/v1/discover/movies/top",
    "/api/v1/discover/tv/top",
    "/api/v1/discover/movies/upcoming",
    "/api/v1/discover/tv/upcoming",
    "/api/v1/movie/{tmdb_id}",
    "/api/v1/movie/{tmdb_id}/recommendations",
    "/api/v1/movie/{tmdb_id}/similar",
    "/api/v1/tv/{tmdb_id}",
    "/api/v1/tv/{tmdb_id}/recommendations",
    "/api/v1/tv/{tmdb_id}/similar",
    "/api/v1/service/radarr",
    "/api/v1/service/radarr/{server_id}",
    "/api/v1/service/sonarr",
    "/api/v1/service/sonarr/{server_id}",
    "/api/v1/request",
    "/api/v1/request/count",
    "/api/v1/request/{request_id}",
    "/api/v1/user/{user_id}/quota",
)

# Seerr permission bits used by Moonfin, matching the client's SeerrPermission
# bitmask (lib/data/services/seerr/seerr_models.dart). We only advertise
# capabilities that are enforced by Nexview; having a bit never bypasses a
# native policy check.
ADMIN = 2
MANAGE_REQUESTS = 16
REQUEST = 32
AUTO_APPROVE = 128
REQUEST_4K = 1024
VIEW_ISSUES = 2097152
CREATE_ISSUES = 4194304
MANAGE_ISSUES = 1048576


def require_compat_adult(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    session: Annotated[str | None, Cookie(alias="connect.sid")] = None,
) -> User:
    """Authenticate a Moonfin cookie through Nexview's normal token guard."""
    if not session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=meldungen.meldung("not_signed_in", "Du bist nicht angemeldet."),
        )
    try:
        # Only accept the session type issued by compatibility login. Accepting
        # access cookies would bypass refresh-cookie logout revocation.
        user = user_from_token(request, session, db, token_type="refresh")
        # Moonfin's generic Seerr views are not age-filtered. Denying child
        # accounts is safer than bypassing Nexview's dedicated kids surface.
        return require_adult(user)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=meldungen.meldung("not_signed_in", "Du bist nicht angemeldet."),
            ) from exc
        raise


CompatUser = Annotated[User, Depends(require_compat_adult)]


def _permissions(user: User) -> int:
    permissions = REQUEST | VIEW_ISSUES | CREATE_ISSUES
    if user.auto_approve:
        permissions |= AUTO_APPROVE
    if user.can_request_uhd_movies or user.can_request_uhd_series:
        permissions |= REQUEST_4K
    if user.can_approve:
        permissions |= MANAGE_REQUESTS
    if user.is_admin:
        permissions |= ADMIN | MANAGE_ISSUES
    return permissions


def _seerr_user(user: User) -> dict:
    created = user.created_at.isoformat() if user.created_at else None
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "avatar": user.avatar_url,
        "permissions": _permissions(user),
        "userType": 2,
        "plexUsername": None,
        "jellyfinUsername": user.mediaserver_username,
        "createdAt": created,
        # Nexview deliberately has no mutable updated-at column on users.
        "updatedAt": created,
        "requestCount": 0,
    }


def _image_path(url: str | None) -> str | None:
    """Turn Nexview's complete TMDB image URL back into Seerr's image path."""
    if not url:
        return None
    marker = "/t/p/"
    if marker not in url:
        return url
    rest = url.split(marker, 1)[1]
    return "/" + rest.split("/", 1)[1] if "/" in rest else None


def _media_status(value: Any) -> int:
    """Translate Nexview state to Seerr's MediaStatus enum."""
    raw = getattr(value, "value", value) or "not_requested"
    return {
        "pending_approval": 2,
        "approved": 3,
        "searching": 3,
        "processing": 3,
        "partially_available": 4,
        "downloaded": 5,
        "available": 5,
    }.get(str(raw), 1)


def _request_status(value: Any) -> int:
    """Translate Nexview state to Seerr's MediaRequestStatus enum."""
    raw = str(getattr(value, "value", value) or "")
    return {
        "pending_approval": 1,
        "approved": 2,
        "searching": 2,
        "processing": 2,
        "downloaded": 5,
        "rejected": 3,
        "cancelled": 3,
        "failed": 4,
    }.get(raw, 1)


def _seerr_media(item: Any) -> dict:
    kind = getattr(item, "media_type", "movie")
    media_type = str(getattr(kind, "value", kind))
    title = getattr(item, "title", "")
    release_date = getattr(item, "release_date", None)
    result = {
        "id": item.tmdb_id,
        "mediaType": media_type,
        "title": title if media_type == "movie" else None,
        "name": title if media_type == "tv" else None,
        "originalTitle": (
            getattr(item, "original_title", None) if media_type == "movie" else None
        ),
        "originalName": (
            getattr(item, "original_title", None) if media_type == "tv" else None
        ),
        "overview": getattr(item, "overview", ""),
        "posterPath": _image_path(getattr(item, "poster_url", None)),
        "backdropPath": _image_path(getattr(item, "backdrop_url", None)),
        "releaseDate": release_date if media_type == "movie" else None,
        "firstAirDate": release_date if media_type == "tv" else None,
        "voteAverage": getattr(item, "vote_average", 0.0),
        "voteCount": getattr(item, "vote_count", 0),
        "genreIds": getattr(item, "genre_ids", []),
        "originalLanguage": getattr(item, "original_language", None),
        "originCountry": getattr(item, "origin_country", []),
        "popularity": 0,
        "genres": [
            {"id": genre_id, "name": name}
            for genre_id, name in zip(
                getattr(item, "genre_ids", []), getattr(item, "genres", []), strict=False
            )
        ],
        "mediaInfo": {
            "id": item.tmdb_id,
            "tmdbId": item.tmdb_id,
            "tvdbId": getattr(item, "tvdb_id", None),
            "status": _media_status(getattr(item, "status", None)),
            "status4k": _media_status(getattr(item, "status_uhd", None)),
            "requests": [],
        },
    }
    runtime = getattr(item, "runtime_minutes", None)
    if runtime is not None:
        result["runtime"] = runtime
    seasons = getattr(item, "seasons", [])
    if seasons:
        result["seasons"] = [
            {
                "id": season.season_number,
                "seasonNumber": season.season_number,
                "name": season.name,
                "episodeCount": season.episode_count,
                "airDate": season.air_date,
                "overview": season.overview,
                "posterPath": _image_path(season.poster_url),
            }
            for season in seasons
        ]
        result["numberOfSeasons"] = len([s for s in seasons if s.season_number > 0])
    cast = getattr(item, "cast", [])
    crew = getattr(item, "crew", [])
    if cast or crew:
        result["credits"] = {
            "cast": [
                {
                    "id": person.person_id,
                    "name": person.name,
                    "character": person.character,
                    "profilePath": _image_path(person.photo_url),
                }
                for person in cast
            ],
            "crew": [
                {"id": person.person_id, "name": person.name, "job": person.job}
                for person in crew
            ],
        }
    return result


def _seerr_request(item: Any, user: User) -> dict:
    kind = getattr(item, "media_type", "movie")
    media_type = str(getattr(kind, "value", kind))
    tier = str(getattr(item, "tier", "standard"))
    requested_at = getattr(item, "requested_at", None)
    updated_at = (
        getattr(item, "completed_at", None)
        or getattr(item, "approved_at", None)
        or requested_at
    )
    raw_status = str(
        getattr(getattr(item, "status", ""), "value", getattr(item, "status", ""))
    )
    media_status = 5 if raw_status == "downloaded" else _media_status(raw_status)
    return {
        "id": item.id,
        "status": _request_status(getattr(item, "status", None)),
        "media": {
            "id": item.tmdb_id,
            "tmdbId": item.tmdb_id,
            "tvdbId": getattr(item, "tvdb_id", None),
            "mediaType": media_type,
            "status": media_status,
            "status4k": media_status if tier == "uhd" else 1,
        },
        "requestedBy": _seerr_user(user),
        "modifiedBy": None,
        "type": media_type,
        "is4k": tier == "uhd",
        "seasons": (
            [{"seasonNumber": item.season}]
            if getattr(item, "season", None) is not None
            else []
        ),
        "createdAt": requested_at.isoformat() if requested_at else None,
        "updatedAt": updated_at.isoformat() if updated_at else None,
    }


def _discover_page(page: Any) -> dict:
    return {
        "page": page.page,
        "totalPages": page.total_pages,
        "totalResults": page.total_results,
        "results": [_seerr_media(item) for item in page.items],
    }


def _sort_from_seerr(value: str) -> Literal["newest", "rating", "popular"]:
    if value.startswith("vote_average"):
        return "rating"
    if value.startswith("popularity"):
        return "popular"
    return "newest"


def _first_integer(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split(",", 1)[0])
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=meldungen.meldung(
                "filter_invalid", "Dieser Zahlenfilter ist ungueltig."
            ),
        ) from None


async def _native_discover(
    media_type: Literal["movie", "tv"],
    user: User,
    db: Session,
    *,
    page: int,
    sort_by: str,
    genre: str | None = None,
    language: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_rating: float | None = None,
) -> Any:
    """Call the native policy-aware discover handler with explicit defaults."""
    return await discover.discover(
        media_type,
        user,
        db,
        date_from=date_from,
        date_to=date_to,
        language=language,
        region=None,
        genre_id=_first_integer(genre),
        sort=_sort_from_seerr(sort_by),
        page=page,
        feature_length=False,
        min_rating=min_rating,
        hide_unrated=False,
        released_in_region=False,
        studio_id=None,
        known_only=False,
    )


class SeerrRequestCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    media_type: Literal["movie", "tv"] = Field(alias="mediaType")
    media_id: int = Field(alias="mediaId", ge=1)
    is_4k: bool = Field(default=False, alias="is4k")
    seasons: list[int] | Literal["all"] | None = None
    tvdb_id: int | None = Field(default=None, alias="tvdbId", ge=1)
    profile_id: int | None = Field(default=None, alias="profileId", ge=1)
    root_folder: str | None = Field(default=None, alias="rootFolder", max_length=500)


class SeerrJellyfinLogin(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.get("/status")
def seerr_status() -> dict:
    """The fields Moonfin probes before accepting a configured Seerr URL."""
    return {
        "version": __version__,
        "commitTag": "nexview-moonfin",
        "updateAvailable": False,
        "commitsBehind": 0,
    }


@router.get("/settings/public")
def public_settings(db: Annotated[Session, Depends(get_db)]) -> dict:
    settings = load_settings(db)

    def uhd_enabled(media_type: MediaType) -> bool:
        return any(item.klasse == "uhd" for item in settings.fassungen_fuer(media_type))

    return {
        "initialized": has_any_user(db),
        "applicationTitle": "Nexview",
        "applicationUrl": "",
        "localLogin": False,
        "mediaServerType": 2,
        "movie4kEnabled": uhd_enabled(MediaType.movie),
        "series4kEnabled": uhd_enabled(MediaType.tv),
    }


@router.get("/auth/me")
def auth_me(user: CompatUser) -> dict:
    return _seerr_user(user)


@router.post("/auth/jellyfin")
async def auth_jellyfin(
    payload: SeerrJellyfinLogin,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Use Nexview's Jellyfin account policy and issue Moonfin's session cookie."""
    native = mediaserver_router.PasswortAnmeldung(
        provider="jellyfin", username=payload.username, password=payload.password
    )
    user = await mediaserver_router.passwort_benutzer(native, request, db)
    settings = get_settings()
    sitzung.starten(
        response,
        request,
        user,
        cookie_name="connect.sid",
        cookie_path=f"{settings.url_base}/api/v1",
    )
    return _seerr_user(user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    sitzung.beenden(
        response,
        request,
        db,
        cookie_name="connect.sid",
        cookie_path=f"{get_settings().url_base}/api/v1",
    )


def _service_servers(settings: Any, media_type: MediaType) -> list[dict]:
    configured = settings.fassungen_fuer(media_type)
    versions = (
        [(version.name, version.klasse) for version in configured]
        if configured
        else [("Full HD", "standard"), ("4K", "uhd")]
    )
    servers = []
    for index, (name, version_class) in enumerate(versions, start=1):
        is_4k = version_class == "uhd"
        servers.append(
            {
                "id": index,
                "name": f"Nexview / NexCrate — {name}",
                "is4k": is_4k,
                "isDefault": index == 1,
                "activeProfileId": index,
                "activeDirectory": "/media",
                "activeTags": [],
            }
        )
    return servers


@router.get("/service/radarr")
def radarr_servers(user: CompatUser, db: Annotated[Session, Depends(get_db)]) -> list[dict]:
    return _service_servers(load_settings(db), MediaType.movie)


@router.get("/service/sonarr")
def sonarr_servers(user: CompatUser, db: Annotated[Session, Depends(get_db)]) -> list[dict]:
    return _service_servers(load_settings(db), MediaType.tv)


def _service_details(settings: Any, media_type: MediaType, server_id: int) -> dict:
    servers = _service_servers(settings, media_type)
    server = next((item for item in servers if item["id"] == server_id), None)
    if server is None:
        raise HTTPException(
            status_code=404,
            detail=meldungen.meldung("not_found", "Nicht gefunden."),
        )
    return {
        "server": server,
        "profiles": [{"id": server_id, "name": server["name"].split("—", 1)[-1].strip()}],
        "rootFolders": [{"id": 1, "path": "/media", "freeSpace": None}],
        "languageProfiles": [],
        "tags": [],
    }


@router.get("/service/radarr/{server_id}")
def radarr_details(
    server_id: int, user: CompatUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    return _service_details(load_settings(db), MediaType.movie, server_id)


@router.get("/service/sonarr/{server_id}")
def sonarr_details(
    server_id: int, user: CompatUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    return _service_details(load_settings(db), MediaType.tv, server_id)


@router.get("/search")
async def search(
    query: Annotated[str, Query(min_length=2, max_length=120)],
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, le=500)] = 1,
) -> dict:
    movie = await discover.search("movie", user, db, query, page)
    series = await discover.search("tv", user, db, query, page)
    results = [_seerr_media(item) for item in [*movie.items, *series.items]]
    return {
        "page": page,
        "totalPages": max(movie.total_pages, series.total_pages),
        "totalResults": movie.total_results + series.total_results,
        "results": results,
    }


@router.get("/discover/movies")
async def discover_movies(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, le=500)] = 1,
    sort_by: Annotated[str, Query(alias="sortBy", max_length=40)] = "popularity.desc",
    genre: Annotated[str | None, Query(max_length=80)] = None,
    language: Annotated[str | None, Query(max_length=5)] = None,
    vote_average_gte: Annotated[
        float | None, Query(alias="voteAverageGte", ge=0, le=10)
    ] = None,
    primary_release_date_gte: Annotated[
        str | None, Query(alias="primaryReleaseDateGte", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ] = None,
    primary_release_date_lte: Annotated[
        str | None, Query(alias="primaryReleaseDateLte", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ] = None,
) -> dict:
    result = await _native_discover(
        "movie",
        user,
        db,
        page=page,
        sort_by=sort_by,
        genre=genre,
        language=language,
        date_from=primary_release_date_gte,
        date_to=primary_release_date_lte,
        min_rating=vote_average_gte,
    )
    return _discover_page(result)


@router.get("/discover/tv")
async def discover_tv(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, le=500)] = 1,
    sort_by: Annotated[str, Query(alias="sortBy", max_length=40)] = "popularity.desc",
    genre: Annotated[str | None, Query(max_length=80)] = None,
    language: Annotated[str | None, Query(max_length=5)] = None,
    vote_average_gte: Annotated[
        float | None, Query(alias="voteAverageGte", ge=0, le=10)
    ] = None,
    first_air_date_gte: Annotated[
        str | None, Query(alias="firstAirDateGte", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ] = None,
    first_air_date_lte: Annotated[
        str | None, Query(alias="firstAirDateLte", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ] = None,
) -> dict:
    result = await _native_discover(
        "tv",
        user,
        db,
        page=page,
        sort_by=sort_by,
        genre=genre,
        language=language,
        date_from=first_air_date_gte,
        date_to=first_air_date_lte,
        min_rating=vote_average_gte,
    )
    return _discover_page(result)


@router.get("/discover/trending")
async def discover_trending(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, le=500)] = 1,
) -> dict:
    movie = await _native_discover("movie", user, db, page=page, sort_by="popularity.desc")
    tv = await _native_discover("tv", user, db, page=page, sort_by="popularity.desc")
    items = []
    for index in range(max(len(movie.items), len(tv.items))):
        if index < len(movie.items):
            items.append(_seerr_media(movie.items[index]))
        if index < len(tv.items):
            items.append(_seerr_media(tv.items[index]))
    return {
        "page": page,
        "totalPages": max(movie.total_pages, tv.total_pages),
        "totalResults": movie.total_results + tv.total_results,
        "results": items,
    }


@router.get("/discover/movies/top")
async def top_movies(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    page = offset // limit + 1
    return _discover_page(
        await _native_discover("movie", user, db, page=page, sort_by="vote_average.desc")
    )


@router.get("/discover/tv/top")
async def top_tv(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    page = offset // limit + 1
    return _discover_page(
        await _native_discover("tv", user, db, page=page, sort_by="vote_average.desc")
    )


@router.get("/discover/movies/upcoming")
async def upcoming_movies(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, le=500)] = 1,
) -> dict:
    return _discover_page(
        await _native_discover(
            "movie",
            user,
            db,
            page=page,
            sort_by="primary_release_date.asc",
            date_from=date.today().isoformat(),
        )
    )


@router.get("/discover/tv/upcoming")
async def upcoming_tv(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, le=500)] = 1,
) -> dict:
    return _discover_page(
        await _native_discover(
            "tv",
            user,
            db,
            page=page,
            sort_by="first_air_date.asc",
            date_from=date.today().isoformat(),
        )
    )


async def _detail(
    media_type: Literal["movie", "tv"], tmdb_id: int, user: User, db: Session
) -> dict:
    item = await details.title_detail(media_type, tmdb_id, user, db)
    return _seerr_media(item)


@router.get("/movie/{tmdb_id}")
async def movie_detail(
    tmdb_id: int, user: CompatUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    return await _detail("movie", tmdb_id, user, db)


@router.get("/tv/{tmdb_id}")
async def tv_detail(
    tmdb_id: int, user: CompatUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    return await _detail("tv", tmdb_id, user, db)


async def _recommendations(
    media_type: Literal["movie", "tv"],
    tmdb_id: int,
    user: User,
    db: Session,
    page: int,
) -> dict:
    selection = await details.recommendations(
        media_type, tmdb_id, user, db, runde=page - 1
    )
    return {
        "page": selection.runde + 1,
        "totalPages": selection.runden,
        "totalResults": len(selection.items),
        "results": [_seerr_media(item) for item in selection.items],
    }


@router.get("/movie/{tmdb_id}/recommendations")
@router.get("/movie/{tmdb_id}/similar")
async def movie_recommendations(
    tmdb_id: int,
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> dict:
    return await _recommendations("movie", tmdb_id, user, db, page)


@router.get("/tv/{tmdb_id}/recommendations")
@router.get("/tv/{tmdb_id}/similar")
async def tv_recommendations(
    tmdb_id: int,
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> dict:
    return await _recommendations("tv", tmdb_id, user, db, page)


@router.post("/request", status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: SeerrRequestCreate,
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    season: int | None = None
    if isinstance(payload.seasons, list):
        if len(payload.seasons) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=meldungen.meldung(
                    "moonfin_one_season",
                    "Nexview nimmt ueber Moonfin derzeit eine Staffel oder alle Staffeln je Anfrage an.",
                ),
            )
        season = payload.seasons[0] if payload.seasons else None

    # ⚠️ Moonfin's profileId and rootFolder are synthetic selectors built from
    # the service list we return. They are NOT real Radarr/Sonarr ids and must
    # never reach the native procurement path, which would treat them as
    # authoritative. Nexview decides the profile and root folder itself.
    native = RequestCreate(
        media_type=MediaType(payload.media_type),
        tmdb_id=payload.media_id,
        tier="uhd" if payload.is_4k else "standard",
        tvdb_id=payload.tvdb_id,
        season=season,
    )
    result = await requests_router.create_request(native, user, db)
    return _seerr_request(result, user)


@router.get("/request")
def list_requests(
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
    take: Annotated[int, Query(ge=1, le=100)] = 20,
    skip: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Return only the caller's requests; Moonfin does not gain an admin backdoor."""
    all_items = requests_router.my_requests(user, db)
    total = len(all_items)
    return {
        "pageInfo": {
            "pages": max(1, (total + take - 1) // take),
            "page": skip // take + 1,
            "results": total,
        },
        "results": [_seerr_request(item, user) for item in all_items[skip : skip + take]],
    }


@router.get("/request/count")
def request_count(
    user: CompatUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    items = requests_router.my_requests(user, db)
    states = [str(getattr(item.status, "value", item.status)) for item in items]
    media = [str(getattr(item.media_type, "value", item.media_type)) for item in items]
    return {
        "total": len(items),
        "movie": media.count("movie"),
        "tv": media.count("tv"),
        "pending": sum(state in {"pending_approval", "deferred"} for state in states),
        "approved": states.count("approved"),
        "declined": sum(state in {"rejected", "cancelled", "failed"} for state in states),
        "processing": states.count("searching"),
        "available": states.count("downloaded"),
        "completed": states.count("downloaded"),
    }


@router.get("/request/{request_id}")
def request_detail(
    request_id: int,
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    item = db.get(MediaRequest, request_id)
    if item is None or (item.user_id != user.id and not user.can_approve):
        raise HTTPException(
            status_code=404,
            detail=meldungen.meldung("request_not_found", "Anfrage nicht gefunden."),
        )
    owner = db.get(User, item.user_id) or user
    return _seerr_request(item, owner)


@router.delete("/request/{request_id}")
async def delete_request(
    request_id: int,
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    # The native handler repeats ownership and lifecycle checks and performs
    # the actual procurement cancellation. The adapter never edits state.
    item = await requests_router.cancel_own_request(request_id, user, db)
    return _seerr_request(item, user)


@router.get("/user/{user_id}/quota")
def user_quota(
    user_id: int,
    user: CompatUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    if user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail=meldungen.meldung(
                "quota_foreign", "Dieses Kontingent gehoert zu einem anderen Konto."
            ),
        )
    native = requests_router.read_quota(user, db)

    def quota_item(item: Any) -> dict:
        return {
            "limit": item.limit,
            "used": item.used,
            "remaining": item.remaining,
            "days": 0,
        }

    return {"movie": quota_item(native.movie), "tv": quota_item(native.tv)}
