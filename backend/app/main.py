from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import secrets
import stat
import threading
import typing
from contextlib import asynccontextmanager
from email.utils import formatdate
from pathlib import Path
from time import monotonic, time
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlsplit

import anyio
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from .admin import DownloadManager, create_admin_router
from .catalog import Catalog, Track
from .media import resolve_local_media
from .origin import ensure_same_origin
from .settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("audio_gateway")
SESSION_COOKIE = "car_audio_session"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
FORWARDED_RANGE_RE = re.compile(r"^bytes=(?:\d+-\d*|-\d+)$")
RESPONSE_HEADERS = {
    "content-type", "content-length", "content-range", "accept-ranges",
    "etag", "last-modified", "content-encoding",
}


class LoginBody(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class LoginThrottle:
    """Small in-process guard against repeated token guessing.

    A successful login never clears the failure history for a key: otherwise any
    holder of a valid guest token could reset the counter at will and keep
    guessing forever (7 wrong + 1 right, repeated). Failures therefore decay
    only by ageing out of the window.
    """

    def __init__(self, limit: int = 8, window_seconds: int = 900):
        self.limit = limit
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = monotonic()
        with self._lock:
            recent = [stamp for stamp in self._attempts.get(key, []) if now - stamp < self.window_seconds]
            self._attempts[key] = recent
            if len(recent) >= self.limit:
                raise HTTPException(status_code=429, detail="Too many login attempts; try again later")
            if len(self._attempts) > 2048:
                self._attempts = {ip: stamps for ip, stamps in self._attempts.items() if stamps and now - stamps[-1] < self.window_seconds}

    def fail(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(monotonic())


def create_upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0),
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
        trust_env=False,
        headers={"User-Agent": "PrivateCarAudioGateway/1.0", "Accept-Encoding": "identity"},
    )


def _login_throttle_key(
    peer_host: str | None,
    forwarded_for: str | None,
    client_ip: str | None = None,
    *,
    trust_proxy: bool = False,
) -> str:
    """Derive the login throttle key from the socket peer and proxy chain.

    Forwarded addresses are believed only when two conditions both hold:

    1. ``TRUST_PROXY_CLIENT_IP`` is enabled — the operator has confirmed a
       reverse proxy sits in front of the API and overwrites the header;
    2. the socket peer is a private address — the request really arrived from
       that proxy instead of straight from the internet.

    ``X-Client-IP`` wins when present: nginx (scripts/vhost.py) overwrites it
    with ``$remote_addr`` after Cloudflare real-IP resolution, so it survives
    the extra Caddy hop that turns X-Forwarded-For's tail into the proxy's own
    address. Without it, only the *last* X-Forwarded-For element is used — the
    one the immediate proxy appended itself, so a client-supplied prefix cannot
    influence the key. CF-Connecting-IP is never read: Caddy cannot tell a
    forged value from a real one, and nothing guarantees it is overwritten.

    The key is length-capped so oversized headers cannot bloat throttle state.
    """
    peer = peer_host or "unknown"
    if not trust_proxy:
        return peer
    try:
        if not ipaddress.ip_address(peer).is_private:
            return peer
    except ValueError:
        return peer
    candidate = (client_ip or "").strip()
    if candidate:
        return candidate[:64]
    chain = [part.strip() for part in (forwarded_for or "").split(",") if part.strip()]
    if not chain:
        return peer
    return chain[-1][:64]


def _safe_https_host(url: str, allowed_hosts: frozenset[str]) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return False
        return (
            parsed.scheme == "https"
            and "." in host
            and host != "localhost"
            and not host.endswith((".localhost", ".local", ".internal"))
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and host in allowed_hosts
        )
    except ValueError:
        return False


def _public_track(track: Track, category_name: str) -> dict[str, object]:
    # Deliberately omit the origin URL: it may contain signed query credentials.
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "category_id": track.category_id,
        "category_name": category_name,
        "description": track.description,
        "cover_url": track.cover_url,
        "mime_type": track.mime_type,
    }


def _parse_byte_range(value: str, size: int) -> tuple[int, int] | None:
    """Parse a single RFC 9110 byte range against ``size``.

    Returns the inclusive ``(start, end)`` pair, or None when the range is
    malformed or unsatisfiable (the caller answers 416 in that case).
    """
    if not value.startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if "," in spec or "-" not in spec:
        return None
    raw_start, _, raw_end = spec.partition("-")
    raw_start, raw_end = raw_start.strip(), raw_end.strip()
    if not raw_start:
        if not raw_end.isdigit():
            return None
        suffix = int(raw_end)
        if suffix <= 0 or size == 0:
            return None
        return max(size - suffix, 0), size - 1
    if not raw_start.isdigit():
        return None
    start = int(raw_start)
    if start >= size:
        return None
    if not raw_end:
        return start, size - 1
    if not raw_end.isdigit():
        return None
    end = int(raw_end)
    if end < start:
        return None
    return start, min(end, size - 1)


def _open_at(path: Path, start: int) -> typing.BinaryIO:
    handle = path.open("rb")
    try:
        handle.seek(start)
    except OSError:
        handle.close()
        raise
    return handle


async def _stream_file(path: Path, start: int, length: int, block_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """Yield a byte slice of ``path`` without blocking the event loop.

    The handle is closed in ``finally`` so a client that seeks away mid-stream
    (very common in a car browser) releases the descriptor immediately instead
    of waiting for garbage collection.
    """
    handle = await anyio.to_thread.run_sync(_open_at, path, start)
    try:
        remaining = length
        while remaining > 0:
            chunk = await anyio.to_thread.run_sync(handle.read, min(block_size, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk
    finally:
        await anyio.to_thread.run_sync(handle.close)


def _local_media_response(path: Path, request: Request, media_type: str) -> Response:
    """Serve a downloaded file to an authenticated caller.

    Starlette 0.38's FileResponse ignores Range, which breaks seeking in the
    car browser, so the 200/206/304/416 semantics are implemented here.
    """
    try:
        file_stat = path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="Audio file not found at source")
    if not stat.S_ISREG(file_stat.st_mode):
        raise HTTPException(status_code=404, detail="Audio file not found at source")

    size = file_stat.st_size
    etag = f'"{file_stat.st_mtime_ns:x}-{size:x}"'
    headers = {
        "Cache-Control": "private, no-store",
        "Vary": "Cookie",
        "X-Content-Type-Options": "nosniff",
        "X-Accel-Buffering": "no",
        "Accept-Ranges": "bytes",
        "Content-Type": media_type or "application/octet-stream",
        "ETag": etag,
        "Last-Modified": formatdate(file_stat.st_mtime, usegmt=True),
    }

    if_none_match = request.headers.get("if-none-match", "").strip()
    if if_none_match:
        candidates = {tag.strip() for tag in if_none_match.split(",")}
        if "*" in candidates or etag in candidates or f"W/{etag}" in candidates:
            return Response(status_code=304, headers=headers)

    range_value = request.headers.get("range", "").strip()
    if range_value:
        if_range = request.headers.get("if-range", "").strip()
        if if_range and if_range != etag:
            # Stale validator: the client must receive the whole current entity.
            range_value = ""

    if range_value:
        parsed = _parse_byte_range(range_value, size)
        if parsed is None:
            headers["Content-Range"] = f"bytes */{size}"
            return Response(status_code=416, headers=headers)
        start, end = parsed
        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)
        status_code = 206
    else:
        start, length = 0, size
        headers["Content-Length"] = str(size)
        status_code = 200

    if request.method.upper() == "HEAD":
        return Response(status_code=status_code, headers=headers)
    return StreamingResponse(
        _stream_file(path, start, length),
        status_code=status_code,
        headers=headers,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.catalog = Catalog(settings.db_path, settings.allowed_audio_hosts, yaml_seed=settings.tracks_config)
    session_key = hmac.digest(
        settings.app_secret_key.encode("utf-8"),
        settings.access_token.encode("utf-8"),
        "sha256",
    )
    app.state.serializer = URLSafeTimedSerializer(session_key, salt="car-audio-session-v1")
    app.state.http = create_upstream_client()
    app.state.login_throttle = LoginThrottle()
    app.state.downloads = DownloadManager()
    yield
    await app.state.downloads.shutdown()
    await app.state.http.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Private Car Audio Gateway",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def _session_payload(request: Request) -> dict[str, Any] | None:
        """校验 Bearer/会话 Cookie,返回会话载荷;无效返回 None。

        - Bearer Token 恒为主层级(配置文件里的 ACCESS_TOKEN)。
        - Cookie 会话携带 tier(primary/secondary);secondary 会话额外校验
          其令牌仍存在——管理页删除令牌后,该令牌签发的会话立即失效。
        - 旧版会话(无 tier 字段)视为主层级:那些会话签发时只存在主 Token,
          因此按主层级处理与当时的实际权限一致。
        - 登出会把 jti 写入吊销表,重放旧 Cookie 不再通过。
        """
        settings: Settings = request.app.state.settings
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
            if hmac.compare_digest(supplied.encode("utf-8"), settings.access_token.encode("utf-8")):
                return {"v": 1, "tier": "primary"}
            return None

        cookie = request.cookies.get(SESSION_COOKIE, "")
        if not cookie:
            return None
        try:
            payload = request.app.state.serializer.loads(cookie, max_age=settings.session_ttl_seconds)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return None
        tier = payload.get("tier", "primary")
        if tier not in {"primary", "secondary"}:
            return None
        jti = payload.get("jti")
        if jti and request.app.state.catalog.is_session_revoked(jti):
            return None
        if tier == "secondary":
            token_id = payload.get("tid")
            if not token_id or not request.app.state.catalog.access_token_exists(token_id):
                return None
        return {**payload, "tier": tier}

    def require_auth(request: Request) -> None:
        if _session_payload(request) is None:
            raise HTTPException(status_code=401, detail="Authentication required")

    def require_primary(request: Request) -> None:
        payload = _session_payload(request)
        if payload is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if payload.get("tier") != "primary":
            raise HTTPException(status_code=403, detail="此操作需要主 Token 会话")

    @app.middleware("http")
    async def private_api_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
            response.headers.setdefault("Vary", "Cookie")
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(create_admin_router(require_primary))

    @app.post("/api/v1/auth/session")
    async def create_session(body: LoginBody, request: Request, response: Response) -> dict[str, object]:
        ensure_same_origin(request)
        settings: Settings = request.app.state.settings
        client_ip = _login_throttle_key(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            request.headers.get("x-client-ip"),
            trust_proxy=settings.trust_proxy_client_ip,
        )
        request.app.state.login_throttle.check(client_ip)
        if hmac.compare_digest(body.token.encode("utf-8"), settings.access_token.encode("utf-8")):
            tier, token_id = "primary", None
        else:
            row = request.app.state.catalog.find_access_token_by_secret(body.token)
            if row is None:
                request.app.state.login_throttle.fail(client_ip)
                raise HTTPException(status_code=401, detail="Invalid access token")
            tier, token_id = "secondary", row["id"]
            request.app.state.catalog.touch_access_token(row["id"])
        session_payload: dict[str, Any] = {
            "v": 1,
            "iat": int(time()),
            "tier": tier,
            # 会话标识:登出时按它写吊销记录,旧 Cookie 重放即失效。
            "jti": secrets.token_urlsafe(16),
        }
        if token_id:
            session_payload["tid"] = token_id
        signed_session = request.app.state.serializer.dumps(session_payload)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=signed_session,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/api",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True, "tier": tier}

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        ensure_same_origin(request)
        settings: Settings = request.app.state.settings
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if cookie:
            try:
                payload = request.app.state.serializer.loads(cookie, max_age=settings.session_ttl_seconds)
            except (BadSignature, SignatureExpired):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("jti"), str):
                # 无状态签名 Cookie 只能靠服务端吊销表作废;不写这条记录,
                # 登出就只是删了客户端副本,截获的旧 Cookie 仍能用到过期。
                request.app.state.catalog.revoke_session(payload["jti"], settings.session_ttl_seconds)
        response.delete_cookie(SESSION_COOKIE, path="/api", secure=settings.cookie_secure, httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": False}

    @app.get("/api/v1/auth/me")
    async def auth_me(request: Request) -> dict[str, object]:
        # Returns 200 with authenticated:false for anonymous visitors so the
        # sign-in probe does not pollute the browser console with 401 noise.
        payload = _session_payload(request)
        if payload is None:
            return {"authenticated": False, "tier": None}
        return {"authenticated": True, "tier": payload.get("tier", "primary")}

    @app.get("/api/v1/categories", dependencies=[Depends(require_auth)])
    async def list_categories(request: Request) -> list[dict[str, object]]:
        snapshot = request.app.state.catalog.snapshot()
        counts: dict[str, int] = {}
        for track in snapshot.tracks:
            if track.enabled:
                counts[track.category_id] = counts.get(track.category_id, 0) + 1
        categories = [
            {"id": cat.id, "name": cat.name, "description": cat.description, "count": counts.get(cat.id, 0)}
            for cat in snapshot.categories if counts.get(cat.id, 0) > 0
        ]
        return sorted(categories, key=lambda item: next(c.order for c in snapshot.categories if c.id == item["id"]))

    @app.get("/api/v1/tracks", dependencies=[Depends(require_auth)])
    async def list_tracks(request: Request, category: str | None = None) -> list[dict[str, object]]:
        snapshot = request.app.state.catalog.snapshot()
        names = {cat.id: cat.name for cat in snapshot.categories}
        tracks = [track for track in snapshot.tracks if track.enabled and (category is None or track.category_id == category)]
        tracks.sort(key=lambda item: (item.order, item.title.casefold()))
        return [_public_track(track, names[track.category_id]) for track in tracks]

    @app.get("/api/v1/tracks/{track_id}", dependencies=[Depends(require_auth)])
    async def get_track(track_id: str, request: Request) -> dict[str, object]:
        track = request.app.state.catalog.get_track(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")
        snapshot = request.app.state.catalog.snapshot()
        category = next((item.name for item in snapshot.categories if item.id == track.category_id), "")
        return _public_track(track, category)

    @app.api_route("/api/v1/stream/{track_id}", methods=["GET", "HEAD"], dependencies=[Depends(require_auth)])
    async def stream_track(track_id: str, request: Request):
        track = request.app.state.catalog.get_track(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")

        range_value = request.headers.get("range")
        if range_value and (len(range_value) > 80 or not FORWARDED_RANGE_RE.fullmatch(range_value.strip())):
            raise HTTPException(status_code=416, detail="Only one valid byte range is supported")

        # Downloaded tracks live on this host. Read them directly instead of
        # round-tripping through PUBLIC_MEDIA_BASE, which the gateway no longer
        # exposes as static files (Caddy answers /media/* with 404).
        local_path = resolve_local_media(request.app.state.settings, track.url)
        if local_path is not None:
            return _local_media_response(local_path, request, track.mime_type)

        forward_headers = {"Accept": track.mime_type, "Accept-Encoding": "identity"}
        for header in ("range", "if-range", "if-none-match", "if-modified-since"):
            value = request.headers.get(header)
            if value:
                forward_headers[header] = value.strip() if header == "range" else value

        method = request.method.upper()
        client: httpx.AsyncClient = request.app.state.http
        current_url = track.url
        upstream: httpx.Response | None = None

        for redirect_number in range(4):
            if not _safe_https_host(current_url, request.app.state.settings.allowed_audio_hosts):
                raise HTTPException(status_code=502, detail="Upstream redirect target is not allow-listed")
            try:
                upstream_request = client.build_request(method, current_url, headers=forward_headers)
                upstream = await client.send(upstream_request, stream=True)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Upstream request failed for track %s: %s", track.id, exc.__class__.__name__)
                raise HTTPException(status_code=502, detail="Audio source is temporarily unavailable") from exc

            if upstream.status_code not in REDIRECT_STATUSES:
                break
            location = upstream.headers.get("location")
            await upstream.aclose()
            if not location or redirect_number == 3:
                raise HTTPException(status_code=502, detail="Audio source redirect limit exceeded")
            current_url = urljoin(current_url, location)

        if upstream is None:
            raise HTTPException(status_code=502, detail="Audio source is temporarily unavailable")
        if upstream.status_code in {404, 410}:
            await upstream.aclose()
            raise HTTPException(status_code=404, detail="Audio file not found at source")
        if upstream.status_code in {401, 403}:
            await upstream.aclose()
            raise HTTPException(status_code=502, detail="Audio source denied the request; check its URL or authorization")
        if upstream.status_code not in {200, 206, 304, 416}:
            await upstream.aclose()
            raise HTTPException(status_code=502, detail=f"Unexpected audio source status: {upstream.status_code}")
        if upstream.status_code == 206 and not upstream.headers.get("content-range"):
            await upstream.aclose()
            raise HTTPException(status_code=502, detail="Audio source returned an invalid partial response")
        content_encoding = upstream.headers.get("content-encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity"}:
            await upstream.aclose()
            raise HTTPException(status_code=502, detail="Audio source must return identity-encoded bytes for reliable byte-range seeking")

        response_headers: dict[str, str] = {
            "Cache-Control": "private, no-store",
            "Vary": "Cookie",
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
        }
        for header in RESPONSE_HEADERS:
            value = upstream.headers.get(header)
            if value:
                response_headers[header] = value

        actual_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if upstream.status_code in {200, 206}:
            if actual_type == "application/octet-stream" or not actual_type:
                response_headers["content-type"] = track.mime_type
            elif not actual_type.startswith("audio/"):
                await upstream.aclose()
                raise HTTPException(status_code=502, detail="Configured source did not return an audio media type")

        if upstream.status_code == 416:
            # The gateway intentionally returns an empty error body, not the origin's HTML error page.
            # Do not forward that page's Content-Length or Content-Type with an empty response.
            response_headers.pop("content-length", None)
            response_headers.pop("content-type", None)
            await upstream.aclose()
            return Response(status_code=416, headers=response_headers)
        if method == "HEAD" or upstream.status_code == 304:
            await upstream.aclose()
            return Response(status_code=upstream.status_code, headers=response_headers)

        async def body_iterator() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    if chunk:
                        yield chunk
            except httpx.HTTPError as exc:
                # Headers may already be sent; terminate the body without logging signed source URLs.
                logger.warning("Upstream stream interrupted for track %s: %s", track.id, exc.__class__.__name__)
                return
            finally:
                await upstream.aclose()

        return StreamingResponse(body_iterator(), status_code=upstream.status_code, headers=response_headers)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled server error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
