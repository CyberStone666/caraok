from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main


class _TestByteStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "tracks.yaml"
    config.write_text(
        """
categories:
  - id: podcast
    name: 播客
tracks:
  - id: demo
    title: Demo Audio
    artist: Tester
    category_id: podcast
    url: https://media.example.com/demo.mp3?signature=secret
    mime_type: audio/mpeg
    enabled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ACCESS_TOKEN", "a" * 64)
    monkeypatch.setenv("APP_SECRET_KEY", "b" * 64)
    monkeypatch.setenv("AUDIO_ALLOWED_HOSTS", "media.example.com")
    monkeypatch.setenv("TRACKS_CONFIG", str(config))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("COOKIE_SECURE", "false")

    mock_state = {"redirect_to": None, "content_encoding": None, "requests": []}

    def mock_upstream_client():
        async def handler(request: httpx.Request) -> httpx.Response:
            mock_state["requests"].append(request)
            if mock_state["redirect_to"]:
                return httpx.Response(302, headers={"Location": mock_state["redirect_to"]}, stream=_TestByteStream(b""))
            if request.headers.get("range") == "bytes=999-":
                return httpx.Response(
                    416,
                    headers={"Content-Range": "bytes */100", "Content-Length": "27", "Content-Type": "text/html"},
                    stream=_TestByteStream(b"<html>range error</html>"),
                )
            if request.headers.get("range") == "bytes=0-3":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Type": "audio/mpeg",
                        "Content-Length": "4",
                        "Content-Range": "bytes 0-3/100",
                        "Accept-Ranges": "bytes",
                        "ETag": '"test-etag"',
                    },
                    stream=_TestByteStream(b"ABCD"),
                )
            headers = {"Content-Type": "audio/mpeg", "Content-Length": "4", "Accept-Ranges": "bytes"}
            if mock_state["content_encoding"]:
                headers["Content-Encoding"] = mock_state["content_encoding"]
            return httpx.Response(200, headers=headers, stream=_TestByteStream(b"TEST"))

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    monkeypatch.setattr(main, "create_upstream_client", mock_upstream_client)
    app = main.create_app()
    with TestClient(app) as test_client:
        test_client.mock_state = mock_state
        test_client.catalog_path = config
        yield test_client


def login(client: TestClient) -> None:
    response = client.post("/api/v1/auth/session", json={"token": "a" * 64})
    assert response.status_code == 200


def test_api_requires_authentication(client: TestClient):
    response = client.get("/api/v1/tracks")
    assert response.status_code == 401


def test_login_and_catalog_do_not_expose_source_url(client: TestClient):
    login(client)
    response = client.get("/api/v1/tracks")
    assert response.status_code == 200
    assert response.json()[0]["id"] == "demo"
    assert "url" not in response.json()[0]
    assert "signature" not in response.text


def test_range_is_forwarded_and_partial_response_is_preserved(client: TestClient):
    login(client)
    response = client.get("/api/v1/stream/demo", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"ABCD"
    assert response.headers["content-range"] == "bytes 0-3/100"
    assert response.headers["accept-ranges"] == "bytes"


def test_invalid_multiple_ranges_are_rejected(client: TestClient):
    login(client)
    response = client.get("/api/v1/stream/demo", headers={"Range": "bytes=0-2,5-7"})
    assert response.status_code == 416


def test_upstream_416_does_not_copy_error_page_body_headers(client: TestClient):
    login(client)
    response = client.get("/api/v1/stream/demo", headers={"Range": "bytes=999-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */100"
    assert response.headers["content-length"] == "0"
    assert "content-type" not in response.headers
    assert response.content == b""


def test_session_cookie_is_httponly_and_samesite_strict(client: TestClient):
    response = client.post("/api/v1/auth/session", json={"token": "a" * 64})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "car_audio_session" in cookie


def test_cross_origin_login_is_rejected(client: TestClient):
    response = client.post(
        "/api/v1/auth/session",
        headers={"Origin": "https://evil.example"},
        json={"token": "a" * 64},
    )
    assert response.status_code == 403


def test_redirect_to_non_allowlisted_host_is_blocked(client: TestClient):
    login(client)
    client.mock_state["redirect_to"] = "https://169.254.169.254/latest/meta-data/"
    response = client.get("/api/v1/stream/demo")
    assert response.status_code == 502
    assert len(client.mock_state["requests"]) == 1


def test_login_attempts_are_throttled(client: TestClient):
    for _ in range(8):
        response = client.post("/api/v1/auth/session", json={"token": "wrong-token"})
        assert response.status_code == 401
    response = client.post("/api/v1/auth/session", json={"token": "wrong-token"})
    assert response.status_code == 429


def test_catalog_updates_live_via_admin(client: TestClient):
    login(client)
    patched = client.patch("/api/v1/admin/tracks/demo", json={"title": "Updated Audio"})
    assert patched.status_code == 200
    response = client.get("/api/v1/tracks")
    assert response.status_code == 200
    assert response.json()[0]["title"] == "Updated Audio"


def test_head_request_returns_metadata_without_body(client: TestClient):
    login(client)
    response = client.head("/api/v1/stream/demo")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"].startswith("audio/mpeg")


def test_logout_revokes_browser_session(client: TestClient):
    login(client)
    assert client.get("/api/v1/auth/me").json() == {"authenticated": True, "tier": "primary"}
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me").json() == {"authenticated": False, "tier": None}
    # Protected surface must still reject the revoked session outright.
    assert client.get("/api/v1/tracks").status_code == 401


def test_invalid_admin_update_keeps_last_known_good_catalog(client: TestClient):
    login(client)
    # A rejected update (unknown category) must leave the catalog untouched.
    rejected = client.patch("/api/v1/admin/tracks/demo", json={"category_id": "ghost"})
    assert rejected.status_code == 400
    response = client.get("/api/v1/tracks")
    assert response.status_code == 200
    assert response.json()[0]["title"] == "Demo Audio"


def test_compressed_upstream_representation_is_rejected_for_range_safety(client: TestClient):
    login(client)
    client.mock_state["content_encoding"] = "gzip"
    response = client.get("/api/v1/stream/demo")
    assert response.status_code == 502


def test_rotating_access_token_invalidates_existing_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "tracks.yaml"
    config.write_text("categories: []\ntracks: []\n", encoding="utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "b" * 64)
    monkeypatch.setenv("AUDIO_ALLOWED_HOSTS", "media.example.com")
    monkeypatch.setenv("TRACKS_CONFIG", str(config))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("COOKIE_SECURE", "false")

    monkeypatch.setenv("ACCESS_TOKEN", "a" * 64)
    app_before_rotation = main.create_app()
    with TestClient(app_before_rotation) as before:
        login(before)
        cookie = before.cookies.get(main.SESSION_COOKIE)
        assert cookie

    monkeypatch.setenv("ACCESS_TOKEN", "c" * 64)
    app_after_rotation = main.create_app()
    with TestClient(app_after_rotation) as after:
        after.cookies.set(main.SESSION_COOKIE, cookie)
        response = after.get("/api/v1/auth/me")
        assert response.status_code == 200
        assert response.json() == {"authenticated": False, "tier": None}
        assert after.get("/api/v1/tracks").status_code == 401


def test_invalid_cookie_secure_setting_fails_closed(monkeypatch: pytest.MonkeyPatch):
    from app.settings import Settings

    monkeypatch.setenv("ACCESS_TOKEN", "a" * 64)
    monkeypatch.setenv("APP_SECRET_KEY", "b" * 64)
    monkeypatch.setenv("AUDIO_ALLOWED_HOSTS", "media.example.com")
    monkeypatch.setenv("COOKIE_SECURE", "maybe")
    with pytest.raises(RuntimeError, match="COOKIE_SECURE must be a boolean value"):
        Settings.from_env()


def test_invalid_allowed_hosts_entries_fail_closed(monkeypatch: pytest.MonkeyPatch):
    from app.settings import Settings

    monkeypatch.setenv("ACCESS_TOKEN", "a" * 64)
    monkeypatch.setenv("APP_SECRET_KEY", "b" * 64)
    monkeypatch.setenv("COOKIE_SECURE", "false")

    for bad_entry in (
        "https://media.example.com",
        "media.example.com:443",
        "media.example.com/audio",
        "media .example.com",
        "localhost",
    ):
        monkeypatch.setenv("AUDIO_ALLOWED_HOSTS", bad_entry)
        with pytest.raises(RuntimeError, match="not a bare hostname"):
            Settings.from_env()


def test_redirect_validator_rejects_ip_literal_even_if_allowlisted():
    assert not main._safe_https_host(
        "https://169.254.169.254/latest/meta-data/",
        frozenset({"169.254.169.254"}),
    )


def test_throttle_key_ignores_forwarded_headers_by_default():
    # TRUST_PROXY_CLIENT_IP is off: nothing in the request may pick the bucket.
    assert main._login_throttle_key("172.18.0.5", "1.2.3.4, 203.0.113.7", "203.0.113.42") == "172.18.0.5"


def test_throttle_key_prefers_explicit_client_ip_when_trusted():
    # nginx overwrites X-Client-IP with $remote_addr; it survives the Caddy hop
    # that turns the X-Forwarded-For tail into the proxy's own address.
    key = main._login_throttle_key("172.18.0.5", "1.2.3.4", "203.0.113.42", trust_proxy=True)
    assert key == "203.0.113.42"


def test_throttle_key_trusts_only_last_forwarded_hop_when_trusted():
    # Earlier elements are client-supplied and must be ignored.
    key = main._login_throttle_key("172.18.0.5", "1.2.3.4, 203.0.113.7", trust_proxy=True)
    assert key == "203.0.113.7"


def test_throttle_key_ignores_forwarding_for_public_peers():
    assert main._login_throttle_key("93.184.216.34", "1.2.3.4", "203.0.113.42", trust_proxy=True) == "93.184.216.34"


def test_throttle_key_falls_back_to_peer_on_missing_or_invalid_input():
    assert main._login_throttle_key(None, None) == "unknown"
    assert main._login_throttle_key("172.18.0.5", None) == "172.18.0.5"
    assert main._login_throttle_key("172.18.0.5", "   ") == "172.18.0.5"
    assert main._login_throttle_key("172.18.0.5", ",,,") == "172.18.0.5"
    # Unparseable peer (e.g. the test client host) keeps the raw peer value.
    assert main._login_throttle_key("testclient", "1.2.3.4, 203.0.113.7", trust_proxy=True) == "testclient"


def test_throttle_key_caps_length_against_oversized_headers():
    huge_chain = f"1.2.3.4, {'a' * 4096}"
    key = main._login_throttle_key("10.0.0.9", huge_chain, trust_proxy=True)
    assert key == "a" * 64
    assert len(main._login_throttle_key("10.0.0.9", "1.2.3.4", "b" * 4096, trust_proxy=True)) == 64


def test_successful_login_does_not_reset_failure_counter(client: TestClient):
    # Regression for the "7 wrong + 1 right, repeat" loop: any guest token holder
    # could otherwise clear the window and guess without limit.
    for _ in range(7):
        assert client.post("/api/v1/auth/session", json={"token": "wrong-token"}).status_code == 401
    login(client)
    assert client.post("/api/v1/auth/session", json={"token": "wrong-token"}).status_code == 401
    assert client.post("/api/v1/auth/session", json={"token": "wrong-token"}).status_code == 429


def test_legacy_session_without_tier_is_treated_as_primary(client: TestClient):
    # Sessions issued before the two-tier split belonged to the only token that
    # existed, so they must keep admin access instead of silently getting 403.
    cookie = client.app.state.serializer.dumps({"v": 1, "iat": 0})
    client.cookies.set(main.SESSION_COOKIE, cookie, path="/")
    assert client.get("/api/v1/admin/tracks").status_code == 200


def test_unknown_session_tier_is_rejected(client: TestClient):
    cookie = client.app.state.serializer.dumps({"v": 1, "iat": 0, "tier": "root"})
    client.cookies.set(main.SESSION_COOKIE, cookie, path="/")
    assert client.get("/api/v1/tracks").status_code == 401


def test_logout_revokes_the_session_server_side(client: TestClient):
    login(client)
    stolen = client.cookies.get(main.SESSION_COOKIE)
    assert client.get("/api/v1/tracks").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200
    # Replaying the pre-logout cookie must fail: deleting the browser copy alone
    # leaves a stolen cookie valid for the whole TTL.
    client.cookies.set(main.SESSION_COOKIE, stolen, path="/")
    assert client.get("/api/v1/tracks").status_code == 401


def test_malformed_session_cookie_yields_401_not_500(client: TestClient):
    login(client)
    client.cookies.set(main.SESSION_COOKIE, "!!!not-a-valid-signature!!!")
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "tier": None}
    # The malformed cookie must fail closed on the protected surface, never 500.
    assert client.get("/api/v1/tracks").status_code == 401
    assert client.get("/api/v1/admin/tracks").status_code == 401
