# Moonfin / Seerr compatibility

This fork keeps Nexview as the policy and request authority while exposing the
small Seerr-shaped API used by Moonfin. It does **not** replace Nexview's native
API and does not claim complete Seerr compatibility.

## Container image

After a tested change reaches `main`, GitHub Actions publishes the fork-owned
multi-architecture image:

```text
ghcr.io/mstoffberg/nexview:stoffcore
```

`latest` points at the same tested `main` build. `stoffcore` is recommended in
the homelab Compose file because it makes the custom origin explicit:

```yaml
services:
  nexview:
    image: ghcr.io/mstoffberg/nexview:stoffcore
    restart: unless-stopped
    ports:
      - "5173:8000"
    volumes:
      - /opt/media/config/nexview:/data
    environment:
      PUID: 1000
      PGID: 1000
      NEXVIEW_COOKIE_SECURE: auto
```

Keep `/data` on persistent, backed-up storage. Do not copy
`NEXVIEW_SECRET_KEY`, media-server tokens, or NexCrate credentials into Git.

## Moonfin configuration

Configure Moonfin's Seerr URL as the public Nexview URL. Moonfin signs in
through `/api/v1/auth/jellyfin`; Nexview applies its existing Jellyfin account
linking, auto-import, blocklist, rate-limit, and library-access checks.

The `connect.sid` cookie contains a signed Nexview refresh token. It is
HttpOnly, follows Nexview's configured refresh lifetime, and remains subject to
account disablement, password-change invalidation, and server-side logout.

Child accounts are intentionally denied on this generic compatibility API.
Moonfin's Seerr views are not age-filtered, so permitting them would bypass
Nexview's dedicated child-safe discovery policy.

## Implemented contract

- `GET /api/v1/status`
- `GET /api/v1/settings/public`
- `POST /api/v1/auth/jellyfin`
- `GET /api/v1/auth/me`
- `GET /api/v1/search`
- `GET /api/v1/discover/trending`
- `GET /api/v1/discover/movies`
- `GET /api/v1/discover/tv`
- `GET /api/v1/discover/{movies|tv}/{top|upcoming}`
- `GET /api/v1/movie/{tmdbId}`
- `GET /api/v1/tv/{tmdbId}`
- `GET /api/v1/{movie|tv}/{tmdbId}/{recommendations|similar}`
- `GET /api/v1/service/{radarr|sonarr}`
- `GET /api/v1/service/{radarr|sonarr}/{serverId}`
- `POST /api/v1/request`
- `GET /api/v1/request`
- `GET /api/v1/request/count`
- `GET /api/v1/request/{id}`
- `DELETE /api/v1/request/{id}`
- `GET /api/v1/user/{id}/quota`

Standard requests map to Nexview's primary version. Seerr's 4K flag maps to
Nexview's `uhd` version selection. Nexview still decides whether the user may
request that version and whether approval, quota, target, profile, or other
policy permits the request.

## Upstream security updates

`.github/workflows/upstream-sync.yml` checks `DerKezorm/nexview/main` every month.
When upstream changes:

1. it merges upstream into a temporary `chore/sync-upstream` branch;
2. conflicts stop the run and create/update an issue;
3. a clean merge must pass backend lint/tests and frontend build/lint/tests;
4. the workflow opens or refreshes a pull request into this fork's `main`;
5. merging that PR runs the normal image workflow and publishes the custom
   StoffCore image.

This deliberately avoids silently replacing custom compatibility code. The
homelab receives upstream security work after a clean merge, tests, and an
explicit PR merge.

## Source and licence

Nexview is AGPL-3.0. The corresponding modified source is published in this
fork, including this adapter and the image build definition. Preserve the
licence and source link when redistributing or exposing modified builds over a
network.
