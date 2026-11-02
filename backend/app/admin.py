"""Admin API: manage the track catalog and drive server-side YouTube downloads.

Every route requires the same authenticated session as the rest of the API.
Download jobs reuse the server-side yt-dlp recipe (cookies + PO token provider
+ optional SOCKS egress) and register the resulting file into the catalog.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from .catalog import ALLOWED_MIME_TYPES, CatalogDocument, Track
from .media import resolve_local_media
from .origin import ensure_same_origin

logger = logging.getLogger("audio_gateway.admin")

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}
_PATH_ID_RE = re.compile(r"(?:/videos/|/embed/|/shorts/|/live/|^/)([A-Za-z0-9_-]{11})(?:[/?]|$)")

DOWNLOAD_TIMEOUT_SECONDS = 900
JOB_HISTORY_LIMIT = 50
_ACTIVE_STATES = {"queued", "downloading", "remuxing"}


def youtube_video_id(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "") not in _YOUTUBE_HOSTS:
        return None
    if parsed.hostname == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", query_id):
        return query_id
    match = _PATH_ID_RE.search(parsed.path)
    return match.group(1) if match else None


def make_slug(raw: str, fallback: str) -> str:
    base = _SLUG_RE.sub("-", raw.strip().lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:48].strip("-")
    return base or fallback


def _next_order(tracks: list[Track], category_id: str) -> int:
    return max((track.order for track in tracks if track.category_id == category_id), default=0) + 1


def _allocate_track_id(tracks: list[Track], base: str) -> str:
    candidate = re.sub(r"[^a-zA-Z0-9_-]", "-", make_slug(base, "track"))[:63].strip("-_") or "track"
    if not _ID_RE.fullmatch(candidate):
        candidate = "track"
    taken = {track.id for track in tracks}
    if candidate not in taken:
        return candidate
    for index in range(2, 1000):
        proposal = f"{candidate[:55]}-{index}"
        if proposal not in taken:
            return proposal
    raise HTTPException(status_code=409, detail="Could not allocate a unique track id")


def allocate_media_stem(media_dir: Path, slug: str, job_id: str) -> str:
    """File stem unique even when two jobs share a display slug."""
    base = f"{slug}-{job_id}".strip("-")[:80].strip("-") or job_id
    stem = base
    for index in range(2, 1000):
        if not any(media_dir.glob(f"{stem}.*")):
            return stem
        stem = f"{base[:70].rstrip('-')}-{index}"
    raise HTTPException(status_code=409, detail="Could not allocate a unique media file name")


def _kill_process_tree(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            return


class DirectTrackBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=12, max_length=4096)
    category_id: str = Field(min_length=1, max_length=64)
    artist: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=500)
    mime_type: str = Field(default="audio/mp4", max_length=40)

    @field_validator("mime_type")
    @classmethod
    def valid_mime_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_MIME_TYPES:
            raise ValueError(f"unsupported mime_type: {value}")
        return normalized


class DownloadBody(BaseModel):
    youtube_url: str = Field(min_length=12, max_length=4096)
    category_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)
    title: str = Field(default="", max_length=160)
    artist: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=500)


class TokenCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class TrackPatchBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    artist: str | None = Field(default=None, max_length=120)
    category_id: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    order: int | None = Field(default=None, ge=0, le=100000)


@dataclass
class DownloadJob:
    job_id: str
    youtube_url: str
    video_id: str
    slug: str
    file_stem: str
    category_id: str
    title: str
    artist: str
    description: str
    state: str = "queued"  # queued|downloading|remuxing|done|error
    progress: float = 0.0
    error: str = ""
    track_id: str = ""
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = field(default=None, repr=False)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)


class DownloadManager:
    """Runs yt-dlp jobs and registers finished tracks."""

    def __init__(self, max_concurrent: int = 2):
        self.max_concurrent = max_concurrent
        self._slots = asyncio.Semaphore(max_concurrent)
        self.jobs: dict[str, DownloadJob] = {}
        self._counter = 0

    async def submit(self, request: Request, body: DownloadBody) -> DownloadJob:
        settings = request.app.state.settings
        if not settings.downloads_enabled or settings.ytdl_cookies is None:
            raise HTTPException(status_code=503, detail="下载功能未启用:缺少 YTDLP_COOKIES 配置")
        video_id = youtube_video_id(body.youtube_url)
        if not video_id:
            raise HTTPException(status_code=400, detail="不是有效的 YouTube 视频 URL")
        catalog = request.app.state.catalog
        if not any(cat.id == body.category_id for cat in catalog.snapshot().categories):
            raise HTTPException(status_code=400, detail="分类不存在")

        snapshot = catalog.snapshot()
        track_id = _allocate_track_id(snapshot.tracks, body.name or video_id)
        self._counter += 1
        job_id = f"job-{int(time.time())}-{self._counter}"
        slug = make_slug(body.name or video_id, video_id)
        job = DownloadJob(
            job_id=job_id,
            youtube_url=f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id,
            slug=slug,
            file_stem=allocate_media_stem(settings.media_dir, slug, job_id),
            category_id=body.category_id,
            title=body.title,
            artist=body.artist,
            description=body.description,
            track_id=track_id,
        )
        self.jobs[job.job_id] = job
        # 真排队:任务创建后进入 queued,由 _run 的信号量决定何时开跑
        job.state = "queued"
        job.task = asyncio.create_task(self._run(request, job))
        self._prune_jobs()
        return job

    def _is_finished(self, job: DownloadJob) -> bool:
        if job.state in _ACTIVE_STATES:
            return False
        return job.task is None or job.task.done()

    def _prune_jobs(self) -> None:
        overflow = len(self.jobs) - JOB_HISTORY_LIMIT
        if overflow <= 0:
            return
        finished = sorted(
            (job for job in self.jobs.values() if self._is_finished(job)),
            key=lambda item: item.created_at,
        )
        for job in finished[:overflow]:
            self.jobs.pop(job.job_id, None)

    async def shutdown(self) -> None:
        pending: list[asyncio.Task] = []
        for job in list(self.jobs.values()):
            _kill_process_tree(job.process)
            if job.task is not None and not job.task.done():
                job.task.cancel()
                pending.append(job.task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _subprocess_env(settings) -> dict[str, str]:
        env: dict[str, str] = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"}
        if settings.ytdl_proxy:
            proxy = settings.ytdl_proxy
            env.update({
                "ALL_PROXY": proxy, "all_proxy": proxy,
                "HTTPS_PROXY": proxy, "https_proxy": proxy,
                "HTTP_PROXY": proxy, "http_proxy": proxy,
                "NO_PROXY": "127.0.0.1,localhost",
            })
        return env

    @staticmethod
    def _build_command(settings, job: DownloadJob, cookies_path: Path) -> list[str]:
        # Challenge scripts come from the installed yt-dlp-ejs package.
        # Do not allow yt-dlp to download solver code at request time.
        command = [
            "yt-dlp",
            "--no-remote-components",
            "--js-runtimes", "deno",
            "--cookies", str(cookies_path),
            "--cache-dir", str(settings.ytdl_cache),
            "--no-playlist",
            # -x + audio-format:上游是 m4a 还是 webm/opus,统一产出单一 .m4a
            "-x", "--audio-format", "m4a",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "-o", str(settings.media_dir / f"{job.file_stem}.%(ext)s"),
        ]
        if settings.ytdl_pot_url:
            command += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={settings.ytdl_pot_url}"]
        command.append(job.youtube_url)
        return command

    @staticmethod
    def _writable_cookies(settings, job: DownloadJob) -> Path:
        """yt-dlp rewrites the cookie jar, so each job gets its own copy."""
        source = settings.ytdl_cookies
        if source is None:
            raise HTTPException(status_code=503, detail="下载功能未启用:缺少 YTDLP_COOKIES 配置")
        target = settings.ytdl_cache / f"cookies.{job.job_id}.txt"
        target.write_bytes(source.read_bytes())
        return target

    async def _run(self, request: Request, job: DownloadJob) -> None:
        settings = request.app.state.settings
        cookies_path: Path | None = None
        try:
            settings.ytdl_cache.mkdir(parents=True, exist_ok=True)
            cookies_path = self._writable_cookies(settings, job)
            async with self._slots:
                await asyncio.wait_for(
                    self._pipeline(request, job, cookies_path),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            _kill_process_tree(job.process)
            job.state = "error"
            job.error = "下载超时（15 分钟）"
        except asyncio.CancelledError:
            _kill_process_tree(job.process)
            if job.state in _ACTIVE_STATES:
                job.state = "error"
                job.error = "已取消"
            raise
        except HTTPException as exc:
            job.state = "error"
            job.error = exc.detail if isinstance(exc.detail, str) else "download failed"
        except Exception as exc:  # noqa: BLE001 - report the failure on the job, never crash the API
            logger.exception("Download job %s failed", job.job_id)
            job.state = "error"
            job.error = f"{exc.__class__.__name__}: {exc}"[:300]
        finally:
            if cookies_path is not None:
                cookies_path.unlink(missing_ok=True)
            self._prune_jobs()

    async def _pipeline(self, request: Request, job: DownloadJob, cookies_path: Path) -> None:
        settings = request.app.state.settings
        job.state = "downloading"
        process = await asyncio.create_subprocess_exec(
            *self._build_command(settings, job, cookies_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._subprocess_env(settings),
            start_new_session=True,
        )
        job.process = process
        if process.stdout is None:
            _kill_process_tree(process)
            job.state = "error"
            job.error = "yt-dlp 没有标准输出"
            return
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            match = re.search(r"\[download\]\s+(\d{1,3}(?:\.\d)?)%", line)
            if match:
                job.progress = min(float(match.group(1)), 99.9)
            elif line.startswith("ERROR"):
                job.error = line[:300]
        code = await process.wait()
        if code != 0:
            job.state = "error"
            job.error = job.error or f"yt-dlp 退出码 {code}"
            return

        job.state = "remuxing"
        source = settings.media_dir / f"{job.file_stem}.m4a"
        fixed = settings.media_dir / f"{job.file_stem}.fix.m4a"
        remux = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-c", "copy", str(fixed),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        job.process = remux
        await remux.communicate()
        if remux.returncode == 0:
            try:
                fixed_stat = fixed.stat()
            except FileNotFoundError:
                fixed_stat = None
            if fixed_stat is not None and stat.S_ISREG(fixed_stat.st_mode):
                source.unlink(missing_ok=True)
                fixed.replace(source)

        if not job.title.strip() or not job.artist.strip():
            await self._fill_metadata(request, job)

        try:
            self._register_track(request, job)
        except Exception:
            # 注册失败时不留孤儿音频文件(注册是唯一合法出站引用)
            (settings.media_dir / f"{job.file_stem}.m4a").unlink(missing_ok=True)
            raise
        job.progress = 100.0
        job.state = "done"
        logger.info("Download job %s finished -> track %s", job.job_id, job.track_id)

    async def _fill_metadata(self, request: Request, job: DownloadJob) -> None:
        try:
            response = await request.app.state.http.get(
                "https://www.youtube.com/oembed",
                params={"url": job.youtube_url, "format": "json"},
                timeout=8,
            )
            if response.status_code == 200:
                payload = response.json()
                job.title = job.title or str(payload.get("title", ""))[:160]
                job.artist = job.artist or str(payload.get("author_name", ""))[:120]
        except (httpx.HTTPError, ValueError):
            pass

    def _register_track(self, request: Request, job: DownloadJob) -> None:
        settings = request.app.state.settings
        if not settings.public_media_base:
            raise HTTPException(status_code=500, detail="PUBLIC_MEDIA_BASE 未配置,无法注册下载的文件")
        file_name = f"{job.file_stem}.m4a"
        file_path = settings.media_dir / file_name
        try:
            file_stat = file_path.stat()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail="下载完成但音频文件缺失") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise HTTPException(status_code=500, detail="下载完成但音频文件缺失")

        catalog = request.app.state.catalog

        def _mutator(doc):
            tracks = [track for track in doc.tracks if track.id != job.track_id]
            track = Track(
                id=job.track_id,
                title=job.title or job.video_id,
                artist=job.artist or "Unknown artist",
                category_id=job.category_id,
                description=job.description,
                url=f"{settings.public_media_base}/{file_name}",
                mime_type="audio/mp4",
                enabled=True,
                order=_next_order(tracks, job.category_id),
            )
            tracks.append(track)
            return CatalogDocument(categories=doc.categories, tracks=tracks)

        catalog.update(_mutator)


def _file_status(settings, track: Track) -> dict[str, object]:
    """Inspect the audio source for a track: local file stat or remote marker.

    本地判定复用 media.resolve_local_media——与流端点共用同一套路径解析,
    避免管理页显示"就绪"而实际播放 404 的不一致。
    """
    base = settings.public_media_base
    is_local_url = bool(base) and track.url.startswith(f"{base}/")
    if not is_local_url:
        return {"source": "remote", "exists": None, "url_host": urlsplit(track.url).hostname or ""}

    path = resolve_local_media(settings, track.url)
    name = track.url[len(base) + 1:]
    if path is None:
        return {"source": "local", "exists": False, "file_name": name, "detail": "意外的媒体路径"}
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        return {"source": "local", "exists": False, "file_name": name}
    if not stat.S_ISREG(file_stat.st_mode):
        return {"source": "local", "exists": False, "file_name": name, "detail": "不是普通文件"}
    return {
        "source": "local",
        "exists": True,
        "size_bytes": file_stat.st_size,
        "file_name": name,
    }


def create_admin_router(require_primary: Callable) -> APIRouter:
    """管理面(曲库/下载/令牌)仅主 Token 会话可用;访客令牌一律 403。"""
    router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_primary)])

    @router.get("/categories")
    async def list_admin_categories(request: Request) -> list[dict[str, object]]:
        snapshot = request.app.state.catalog.snapshot()
        counts: dict[str, int] = {}
        for track in snapshot.tracks:
            counts[track.category_id] = counts.get(track.category_id, 0) + 1
        categories = [
            {
                "id": cat.id,
                "name": cat.name,
                "description": cat.description,
                "count": counts.get(cat.id, 0),
                "order": cat.order,
            }
            for cat in snapshot.categories
        ]
        categories.sort(key=lambda item: (int(item["order"]), str(item["name"]).casefold()))
        return categories

    @router.get("/tracks")
    async def list_admin_tracks(request: Request) -> list[dict[str, object]]:
        settings = request.app.state.settings
        snapshot = request.app.state.catalog.snapshot()
        names = {cat.id: cat.name for cat in snapshot.categories}
        result = []
        for track in snapshot.tracks:
            entry: dict[str, object] = {
                "id": track.id,
                "title": track.title,
                "artist": track.artist,
                "category_id": track.category_id,
                "category_name": names.get(track.category_id, ""),
                "description": track.description,
                "mime_type": track.mime_type,
                "enabled": track.enabled,
                "order": track.order,
                "url_host": urlsplit(track.url).hostname or "",
                "file": _file_status(settings, track),
            }
            result.append(entry)
        result.sort(key=lambda item: (item["category_id"], item["order"], str(item["title"]).casefold()))
        return result

    @router.post("/tracks")
    async def add_direct_track(body: DirectTrackBody, request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        settings = request.app.state.settings
        catalog = request.app.state.catalog
        probe = Track(
            id="probe", title=body.title, category_id=body.category_id,
            url=body.url, mime_type=body.mime_type,
        )
        try:
            probe.validate_source_host(settings.allowed_audio_hosts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def _mutator(doc):
            if not any(cat.id == body.category_id for cat in doc.categories):
                raise HTTPException(status_code=400, detail="分类不存在")
            track = Track(
                id=_allocate_track_id(doc.tracks, body.title),
                title=body.title,
                artist=body.artist or "Unknown artist",
                category_id=body.category_id,
                description=body.description,
                url=body.url,
                mime_type=body.mime_type,
                enabled=True,
                order=_next_order(doc.tracks, body.category_id),
            )
            return CatalogDocument(categories=doc.categories, tracks=doc.tracks + [track])

        try:
            registered = catalog.update(_mutator)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        added = registered.tracks[-1]
        return {"id": added.id, "title": added.title}

    @router.patch("/tracks/{track_id}")
    async def patch_track(track_id: str, body: TrackPatchBody, request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        catalog = request.app.state.catalog
        # Explicit null means "leave unchanged", not "coerce to False/0".
        updates = body.model_dump(exclude_unset=True, exclude_none=True)

        def _mutator(doc):
            index = next((i for i, track in enumerate(doc.tracks) if track.id == track_id), None)
            if index is None:
                raise HTTPException(status_code=404, detail="Track not found")
            if "category_id" in updates and not any(cat.id == updates["category_id"] for cat in doc.categories):
                raise HTTPException(status_code=400, detail="分类不存在")
            current = doc.tracks[index]
            fields = {
                "title": current.title, "artist": current.artist, "category_id": current.category_id,
                "description": current.description, "enabled": current.enabled, "order": current.order,
            }
            fields.update(updates)
            updated = Track(
                id=current.id, title=fields["title"], artist=fields["artist"],
                category_id=fields["category_id"], description=fields["description"],
                url=current.url, mime_type=current.mime_type,
                enabled=fields["enabled"], order=fields["order"],
            )
            tracks = doc.tracks[:index] + [updated] + doc.tracks[index + 1:]
            return CatalogDocument(categories=doc.categories, tracks=tracks)

        catalog.update(_mutator)
        return {"id": track_id, "updated": sorted(updates.keys())}

    @router.delete("/tracks/{track_id}")
    async def delete_track(track_id: str, request: Request, delete_file: bool = False) -> dict[str, object]:
        ensure_same_origin(request)
        settings = request.app.state.settings
        catalog = request.app.state.catalog
        target: list[Track] = []

        def _mutator(doc):
            found = next((item for item in doc.tracks if item.id == track_id), None)
            if found is None:
                raise HTTPException(status_code=404, detail="Track not found")
            target.append(found)
            return CatalogDocument(categories=doc.categories, tracks=[t for t in doc.tracks if t.id != track_id])

        catalog.update(_mutator)
        track = target[0] if target else None
        removed_file = False
        base = settings.public_media_base
        if delete_file and base and track is not None and track.url.startswith(f"{base}/"):
            name = track.url[len(base) + 1:]
            path = (settings.media_dir / name).resolve()
            try:
                is_file = "/" not in name and path.is_relative_to(settings.media_dir.resolve()) and stat.S_ISREG(path.stat().st_mode)
            except FileNotFoundError:
                is_file = False
            if is_file:
                path.unlink()
                removed_file = True
        return {"id": track_id, "deleted": True, "file_removed": removed_file}

    @router.get("/tokens")
    async def list_tokens(request: Request) -> list[dict[str, object]]:
        return request.app.state.catalog.list_access_tokens()

    @router.post("/tokens", status_code=201)
    async def create_token(body: TokenCreateBody, request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="名称不能为空")
        plaintext, meta = request.app.state.catalog.create_access_token(name)
        # 明文仅在本次响应返回一次;服务端只存 SHA-256 哈希
        return {"id": meta["id"], "name": meta["name"], "token": plaintext, "created_at": meta["created_at"]}

    @router.delete("/tokens/{token_id}")
    async def delete_token(token_id: str, request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        if not request.app.state.catalog.delete_access_token(token_id):
            raise HTTPException(status_code=404, detail="Token not found")
        return {"id": token_id, "deleted": True}

    @router.post("/downloads", status_code=202)
    async def create_download(body: DownloadBody, request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        job = await request.app.state.downloads.submit(request, body)
        return {"job_id": job.job_id, "state": job.state, "slug": job.slug, "file_stem": job.file_stem}

    @router.get("/downloads")
    async def list_downloads(
        request: Request,
        limit: int = Query(default=JOB_HISTORY_LIMIT, ge=1, le=JOB_HISTORY_LIMIT),
    ) -> list[dict[str, object]]:
        jobs = sorted(
            request.app.state.downloads.jobs.values(),
            key=lambda item: item.created_at, reverse=True,
        )[:limit]
        return [{
            "job_id": job.job_id, "state": job.state, "progress": job.progress,
            "slug": job.slug, "title": job.title, "category_id": job.category_id,
            "track_id": job.track_id, "error": job.error,
            "youtube_video_id": job.video_id, "created_at": job.created_at,
        } for job in jobs]

    return router
