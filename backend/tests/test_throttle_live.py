"""Login-throttling behaviour over a real socket.

Starlette's TestClient hardcodes the ASGI peer to ``("testclient", 50000)``,
which is not an IP address, so every proxy branch in ``_login_throttle_key``
short-circuits to "return the raw peer" and the interesting behaviour is
invisible. These tests start a real uvicorn process on 127.0.0.1 so the peer is
an actual private address and the header handling is exercised end to end.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ACCESS_TOKEN = "a" * 64
SECRET_KEY = "b" * 64


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_ready(base_url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/healthz", timeout=0.5, trust_env=False)
            return
        except httpx.HTTPError:
            time.sleep(0.15)
    raise RuntimeError("uvicorn did not become ready in time")


@pytest.fixture()
def start_server(tmp_path: Path):
    """Start uvicorn with the given TRUST_PROXY_CLIENT_IP setting."""
    processes: list[subprocess.Popen] = []

    def _start(*, trust_proxy: bool) -> str:
        config = tmp_path / "tracks.yaml"
        config.write_text("categories: []\ntracks: []\n", encoding="utf-8")
        media = tmp_path / "media"
        media.mkdir(exist_ok=True)
        port = _free_port()
        env = {
            **os.environ,
            "ACCESS_TOKEN": ACCESS_TOKEN,
            "APP_SECRET_KEY": SECRET_KEY,
            "AUDIO_ALLOWED_HOSTS": "audio.example.com",
            "TRACKS_CONFIG": str(config),
            "DB_PATH": str(tmp_path / "catalog.db"),
            "MEDIA_DIR": str(media),
            "COOKIE_SECURE": "false",
            "TRUST_PROXY_CLIENT_IP": "true" if trust_proxy else "false",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
             # Same flag as the Dockerfile: uvicorn otherwise rewrites the ASGI
             # peer from X-Forwarded-For for local peers and the app's own
             # peer check would never see the real socket address.
             "--no-proxy-headers"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_until_ready(base_url)
        except RuntimeError:
            process.terminate()
            raise
        return base_url

    yield _start

    for process in processes:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _client(base_url: str) -> httpx.Client:
    # trust_env=False: a system HTTP proxy would swallow 127.0.0.1 requests and
    # turn every client into the same upstream address.
    return httpx.Client(base_url=base_url, timeout=5, trust_env=False)


def test_rotating_forwarded_headers_cannot_escape_the_throttle(start_server):
    base_url = start_server(trust_proxy=False)
    with _client(base_url) as client:
        for index in range(8):
            # A direct client can invent any of these; none may pick the bucket.
            headers = {
                "X-Forwarded-For": f"198.51.100.{index}",
                "X-Client-IP": f"203.0.113.{index}",
                "CF-Connecting-IP": f"192.0.2.{index}",
            }
            response = client.post("/api/v1/auth/session", json={"token": f"wrong-{index}"}, headers=headers)
            assert response.status_code == 401
        blocked = client.post(
            "/api/v1/auth/session",
            json={"token": "wrong-final"},
            headers={"CF-Connecting-IP": "192.0.2.250", "X-Client-IP": "203.0.113.250"},
        )
        assert blocked.status_code == 429


def test_successful_login_does_not_reset_the_window(start_server):
    base_url = start_server(trust_proxy=False)
    with _client(base_url) as client:
        for _ in range(7):
            assert client.post("/api/v1/auth/session", json={"token": "wrong"}).status_code == 401
        # A valid login used to clear the counter here, which let one guest token
        # reset the window as often as it liked.
        assert client.post("/api/v1/auth/session", json={"token": ACCESS_TOKEN}).status_code == 200
        assert client.post("/api/v1/auth/session", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/v1/auth/session", json={"token": "wrong"}).status_code == 429


def test_trusted_client_ip_gives_each_client_its_own_bucket(start_server):
    base_url = start_server(trust_proxy=True)
    with _client(base_url) as first, _client(base_url) as second:
        for _ in range(8):
            assert first.post(
                "/api/v1/auth/session",
                json={"token": "wrong"},
                headers={"X-Client-IP": "203.0.113.10"},
            ).status_code == 401
        assert first.post(
            "/api/v1/auth/session",
            json={"token": "wrong"},
            headers={"X-Client-IP": "203.0.113.10"},
        ).status_code == 429
        # A different resolved client is unaffected by the first one's window.
        assert second.post(
            "/api/v1/auth/session",
            json={"token": "wrong"},
            headers={"X-Client-IP": "203.0.113.11"},
        ).status_code == 401
