from __future__ import annotations

import asyncio
import typing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.admin import (
    DownloadBody,
    DownloadJob,
    DownloadManager,
    allocate_media_stem,
    make_slug,
    youtube_video_id,
)


@pytest.fixture()
def admin_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    config = tmp_path / "tracks.yaml"
    config.write_text(
        """
categories:
  - id: music
    name: 音乐
  - id: learning
    name: 学习
tracks:
  - id: local-track
    title: Local Track
    artist: Tester
    category_id: music
    url: https://audio.example.com/media/local-track.m4a
    mime_type: audio/mp4
    enabled: true
    order: 3
""",
        encoding="utf-8",
    )
    media = tmp_path / "media"
    media.mkdir()
    (media / "local-track.m4a").write_bytes(b"\x00\x01\x02audio")
    monkeypatch.setenv("ACCESS_TOKEN", "a" * 64)
    monkeypatch.setenv("APP_SECRET_KEY", "b" * 64)
    monkeypatch.setenv("AUDIO_ALLOWED_HOSTS", "audio.example.com")
    monkeypatch.setenv("TRACKS_CONFIG", str(config))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("MEDIA_DIR", str(media))
    monkeypatch.setenv("PUBLIC_MEDIA_BASE", "https://audio.example.com/media")

    client_context = TestClient(main.create_app())
    client_context.headers.update({"Origin": "http://testserver"})
    with client_context as entered:
        yield entered


def _login(client: TestClient) -> None:
    response = client.post("/api/v1/auth/session", json={"token": "a" * 64})
    assert response.status_code == 200


def test_admin_requires_auth(admin_client: TestClient):
    assert admin_client.get("/api/v1/admin/tracks").status_code == 401


def test_admin_list_reports_local_file_status(admin_client: TestClient):
    _login(admin_client)
    response = admin_client.get("/api/v1/admin/tracks")
    assert response.status_code == 200
    tracks = response.json()
    assert len(tracks) == 1
    entry = tracks[0]
    assert entry["file"]["source"] == "local"
    assert entry["file"]["exists"] is True
    assert entry["file"]["size_bytes"] == 8
    assert entry["file"]["file_name"] == "local-track.m4a"


def test_admin_categories_include_empty(admin_client: TestClient):
    _login(admin_client)
    payload = admin_client.get("/api/v1/admin/categories")
    assert payload.status_code == 200
    by_id = {item["id"]: item for item in payload.json()}
    assert set(by_id) == {"music", "learning"}
    assert by_id["learning"]["count"] == 0
    assert by_id["music"]["count"] == 1
    public = {item["id"] for item in admin_client.get("/api/v1/categories").json()}
    assert public == {"music"}


def test_admin_add_and_delete_direct_track(admin_client: TestClient, tmp_path: Path):
    _login(admin_client)
    added = admin_client.post("/api/v1/admin/tracks", json={
        "title": "Remote Track",
        "url": "https://audio.example.com/audio/track.mp3",
        "category_id": "music",
        "mime_type": "audio/mpeg",
    })
    assert added.status_code == 200
    new_id = added.json()["id"]

    listed = {item["id"]: item for item in admin_client.get("/api/v1/admin/tracks").json()}
    assert new_id in listed
    assert listed[new_id]["file"]["source"] == "remote"

    # The new track must be playable through the public catalog endpoint.
    catalog = admin_client.get("/api/v1/tracks").json()
    assert any(item["id"] == new_id for item in catalog)

    deleted = admin_client.delete(f"/api/v1/admin/tracks/{new_id}")
    assert deleted.status_code == 200
    remaining = {item["id"] for item in admin_client.get("/api/v1/admin/tracks").json()}
    assert new_id not in remaining
    assert "local-track" in remaining


def test_admin_add_rejects_disallowed_host(admin_client: TestClient):
    _login(admin_client)
    response = admin_client.post("/api/v1/admin/tracks", json={
        "title": "Evil",
        "url": "https://evil.example.com/a.mp3",
        "category_id": "music",
    })
    assert response.status_code == 400


def test_admin_add_rejects_bad_mime(admin_client: TestClient):
    _login(admin_client)
    response = admin_client.post("/api/v1/admin/tracks", json={
        "title": "Html",
        "url": "https://audio.example.com/a.mp3",
        "category_id": "music",
        "mime_type": "text/html",
    })
    assert response.status_code == 422


def test_admin_patch_toggles_enabled(admin_client: TestClient):
    _login(admin_client)
    patched = admin_client.patch("/api/v1/admin/tracks/local-track", json={"enabled": False, "title": "Renamed"})
    assert patched.status_code == 200
    entry = next(item for item in admin_client.get("/api/v1/admin/tracks").json() if item["id"] == "local-track")
    assert entry["enabled"] is False
    assert entry["title"] == "Renamed"
    # Disabled tracks disappear from the public catalog.
    assert all(item["id"] != "local-track" for item in admin_client.get("/api/v1/tracks").json())


def test_admin_patch_null_leaves_fields_unchanged(admin_client: TestClient):
    _login(admin_client)
    before = next(item for item in admin_client.get("/api/v1/admin/tracks").json() if item["id"] == "local-track")
    patched = admin_client.patch("/api/v1/admin/tracks/local-track", json={"order": None, "enabled": None})
    assert patched.status_code == 200
    assert patched.json()["updated"] == []
    after = next(item for item in admin_client.get("/api/v1/admin/tracks").json() if item["id"] == "local-track")
    assert after["order"] == before["order"] == 3
    assert after["enabled"] is True


def test_admin_writes_reject_malformed_origin(admin_client: TestClient):
    _login(admin_client)
    bad = {"Origin": "http://[::1"}
    created = admin_client.post("/api/v1/admin/tracks", headers=bad, json={
        "title": "Nope",
        "url": "https://audio.example.com/a.mp3",
        "category_id": "music",
    })
    patched = admin_client.patch("/api/v1/admin/tracks/local-track", headers=bad, json={"title": "Nope"})
    deleted = admin_client.delete("/api/v1/admin/tracks/local-track", headers=bad)
    assert created.status_code == 403
    assert patched.status_code == 403
    assert deleted.status_code == 403
    assert "Internal server error" not in created.text


def test_admin_delete_local_file(admin_client: TestClient):
    _login(admin_client)
    response = admin_client.delete("/api/v1/admin/tracks/local-track", params={"delete_file": "true"})
    assert response.status_code == 200
    assert response.json()["file_removed"] is True


def test_admin_download_rejected_without_cookies(admin_client: TestClient):
    _login(admin_client)
    response = admin_client.post("/api/v1/admin/downloads", json={
        "youtube_url": "https://www.youtube.com/watch?v=7qc9cHuJ96U",
        "category_id": "music",
    })
    assert response.status_code == 503


def test_youtube_video_id_parsing():
    assert youtube_video_id("https://www.youtube.com/watch?v=7qc9cHuJ96U") == "7qc9cHuJ96U"
    assert youtube_video_id("https://youtu.be/7qc9cHuJ96U") == "7qc9cHuJ96U"
    assert youtube_video_id("https://www.youtube.com/shorts/7qc9cHuJ96U") == "7qc9cHuJ96U"
    assert youtube_video_id("https://www.youtube.com/watch?v=bad!") is None
    assert youtube_video_id("https://evil.com/watch?v=7qc9cHuJ96U") is None


def test_make_slug_sanitizes():
    assert make_slug("Hello World!", "x") == "hello-world"
    assert make_slug("   ", "fallback") == "fallback"
    assert make_slug("週末 dance", "x") == "dance"


def test_media_stem_does_not_collide(tmp_path: Path):
    first = allocate_media_stem(tmp_path, "a-b", "job-1")
    (tmp_path / f"{first}.m4a").write_bytes(b"a")
    second = allocate_media_stem(tmp_path, "a-b", "job-1")
    assert first != second
    assert first.startswith("a-b-")
    assert not list(tmp_path.glob(f"{second}.*"))


def test_cookie_copy_is_per_job(tmp_path: Path):
    source = tmp_path / "cookies.txt"
    source.write_bytes(b"session=1\n")
    cache = tmp_path / "cache"
    cache.mkdir()
    settings = SimpleNamespace(ytdl_cookies=source, ytdl_cache=cache, ytdl_proxy="", ytdl_pot_url="", media_dir=tmp_path)
    job_a = DownloadJob("job-a", "https://www.youtube.com/watch?v=7qc9cHuJ96U", "7qc9cHuJ96U", "song", "song-job-a", "music", "", "", "")
    job_b = DownloadJob("job-b", "https://www.youtube.com/watch?v=7qc9cHuJ96U", "7qc9cHuJ96U", "song", "song-job-b", "music", "", "", "")
    path_a = DownloadManager._writable_cookies(settings, job_a)
    path_b = DownloadManager._writable_cookies(settings, job_b)
    assert path_a != path_b
    assert path_a.read_bytes() == path_b.read_bytes() == b"session=1\n"
    command = DownloadManager._build_command(settings, job_a, path_a)
    assert "--remote-components" not in command
    assert not any(part.startswith("ejs:") for part in command)
    assert f"{job_a.file_stem}.%(ext)s" in command[-2]


def test_download_history_is_capped():
    manager = DownloadManager()
    for index in range(100):
        job = DownloadJob(
            job_id=f"job-{index}",
            youtube_url="https://www.youtube.com/watch?v=7qc9cHuJ96U",
            video_id="7qc9cHuJ96U",
            slug="song",
            file_stem=f"song-{index}",
            category_id="music",
            title="",
            artist="",
            description="",
            state="done",
            created_at=float(index),
        )
        manager.jobs[job.job_id] = job
    manager._prune_jobs()
    assert len(manager.jobs) <= 50
    assert "job-99" in manager.jobs
    assert "job-0" not in manager.jobs


def test_shutdown_cancels_inflight_tasks():
    manager = DownloadManager()
    job = DownloadJob(
        job_id="job-live",
        youtube_url="https://www.youtube.com/watch?v=7qc9cHuJ96U",
        video_id="7qc9cHuJ96U",
        slug="song",
        file_stem="song-live",
        category_id="music",
        title="",
        artist="",
        description="",
        state="downloading",
    )

    async def scenario():
        async def sleeper():
            await asyncio.sleep(30)

        job.task = asyncio.create_task(sleeper())
        manager.jobs[job.job_id] = job
        await manager.shutdown()

    asyncio.run(scenario())
    assert job.task is not None and job.task.cancelled()


def test_build_command_annotations_resolve():
    hints = typing.get_type_hints(DownloadManager._build_command)
    assert hints["cookies_path"] is Path


def test_download_manager_limits_concurrency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """#7: max_concurrent 必须真实生效——queued 任务等待空位而非并行开跑。"""
    manager = DownloadManager(max_concurrent=2)
    media = tmp_path / "media"
    media.mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "ck.txt").write_bytes(b"cookies")
    state = {"peak": 0, "current": 0}

    async def fake_pipeline(request, job, cookies_path):
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.08)
        state["current"] -= 1
        (media / f"{job.file_stem}.m4a").write_bytes(b"audio")
        job.state = "done"

    monkeypatch.setattr(manager, "_pipeline", fake_pipeline)
    monkeypatch.setattr(manager, "_register_track", lambda request, job: None)

    settings = SimpleNamespace(
        downloads_enabled=True,
        ytdl_cookies=tmp_path / "ck.txt",
        ytdl_cache=tmp_path / "cache",
        ytdl_proxy="",
        ytdl_pot_url="",
        media_dir=media,
        public_media_base="https://audio.example.com/media",
        allowed_audio_hosts=frozenset({"audio.example.com"}),
    )
    catalog = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(categories=[SimpleNamespace(id="music")], tracks=[]),
        get_category=lambda cid: True,
        update=lambda mutator: None,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, catalog=catalog, http=None)))

    async def scenario():
        bodies = [
            DownloadBody(
                youtube_url=f"https://www.youtube.com/watch?v={'A' * 10}{i}",
                category_id="music",
                name=f"t{i}",
                title=f"T{i}",
                artist="X",
            )
            for i in range(5)
        ]
        jobs = [await manager.submit(request, body) for body in bodies]
        await asyncio.gather(*(job.task for job in jobs if job.task))
        return jobs

    jobs = asyncio.run(scenario())
    assert state["peak"] <= 2, f"峰值并发 {state['peak']} 超过 max_concurrent=2"
    assert all(job.state == "done" for job in jobs)
    assert sum(1 for job in jobs if job.state == "done") == 5


def test_two_tier_token_lifecycle(admin_client: TestClient):
    """#两级 Token:主 Token 管理访客令牌;访客令牌无管理权;删除即吊销会话。"""
    _login(admin_client)

    # 主 Token 生成访客令牌:明文只返回一次,不落库不回显
    created = admin_client.post("/api/v1/admin/tokens", json={"name": "家人"})
    assert created.status_code == 201
    payload = created.json()
    plaintext = payload["token"]
    assert plaintext.startswith("ck_")
    assert "token_hash" not in payload  # 哈希永不回显;明文仅此一次返回

    tokens = admin_client.get("/api/v1/admin/tokens").json()
    assert [t["name"] for t in tokens] == ["家人"]
    assert all("token" not in t and "token_hash" not in t for t in tokens)

    # 访客令牌登录 → secondary 会话
    guest = TestClient(admin_client.app)
    login_response = guest.post("/api/v1/auth/session", json={"token": plaintext})
    assert login_response.status_code == 200
    assert login_response.json()["tier"] == "secondary"
    assert guest.get("/api/v1/auth/me").json() == {"authenticated": True, "tier": "secondary"}

    # 访客能用播放面
    assert guest.get("/api/v1/tracks").status_code == 200

    # 访客不能进管理面,更不能管理令牌
    assert guest.get("/api/v1/admin/tracks").status_code == 403
    assert guest.get("/api/v1/admin/tokens").status_code == 403
    assert guest.post("/api/v1/admin/tokens", json={"name": "越权"}).status_code == 403
    assert guest.delete(f"/api/v1/admin/tokens/{payload['id']}").status_code == 403
    guest_download = guest.post("/api/v1/admin/downloads", json={
        "youtube_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
        "category_id": "music",
    })
    assert guest_download.status_code == 403

    # 主 Token 删除令牌 → 访客会话立即失效;主会话不受影响
    deleted = admin_client.delete(f"/api/v1/admin/tokens/{payload['id']}")
    assert deleted.status_code == 200
    assert guest.get("/api/v1/auth/me").json() == {"authenticated": False, "tier": None}
    assert guest.get("/api/v1/tracks").status_code == 401
    assert admin_client.get("/api/v1/auth/me").json()["authenticated"] is True


def test_two_tier_unknown_token_rejected(admin_client: TestClient):
    _login(admin_client)
    response = admin_client.post("/api/v1/auth/session", json={"token": "ck_" + "0" * 48})
    assert response.status_code == 401


def test_all_module_annotations_resolve():
    """所有模块级注解必须能在运行时解析(from __future__ annotations 会掩盖
    未导入的类型名;get_type_hints 是唯一可靠检测)。"""
    import inspect as _inspect
    import typing as _typing
    from app import admin, catalog, main, media, origin, settings

    for module in (admin, catalog, main, media, origin, settings):
        for name, obj in vars(module).items():
            if not (_inspect.isclass(obj) or _inspect.isfunction(obj)):
                continue
            # 只检查本模块定义的对象(导入的第三方库函数不管)
            if getattr(obj, "__module__", "") != module.__name__:
                continue
            try:
                _typing.get_type_hints(obj)
            except NameError as exc:  # pragma: no cover - 失败即缺陷
                raise AssertionError(f"{module.__name__}.{name} 注解无法解析: {exc}") from exc
