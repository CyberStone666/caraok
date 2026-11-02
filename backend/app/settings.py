from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("audio_gateway.settings")


@dataclass(frozen=True)
class Settings:
    access_token: str
    app_secret_key: str
    allowed_audio_hosts: frozenset[str]
    tracks_config: Path
    cookie_secure: bool
    session_ttl_seconds: int
    db_path: Path = Path("/app/data/caraok.db")
    media_dir: Path = Path("/app/media")
    public_media_base: str = ""
    ytdl_cookies: Path | None = None
    ytdl_cache: Path = Path("/app/.ytdl-cache")
    ytdl_proxy: str = ""
    ytdl_pot_url: str = ""
    downloads_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        access_token = os.getenv("ACCESS_TOKEN", "").strip()
        secret_key = os.getenv("APP_SECRET_KEY", "").strip()
        if len(access_token) < 32:
            raise RuntimeError("ACCESS_TOKEN must contain at least 32 characters")
        if len(secret_key) < 32:
            raise RuntimeError("APP_SECRET_KEY must contain at least 32 characters")

        hosts = frozenset(
            host.strip().lower().rstrip(".")
            for host in os.getenv("AUDIO_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        if not hosts:
            raise RuntimeError("AUDIO_ALLOWED_HOSTS must contain at least one exact hostname")
        for host in hosts:
            if "/" in host or ":" in host or "@" in host or " " in host or "." not in host:
                raise RuntimeError(
                    f"AUDIO_ALLOWED_HOSTS entry '{host}' is not a bare hostname; "
                    "use exact DNS names like cdn.example.com without scheme, port or path"
                )

        config_path = Path(os.getenv("TRACKS_CONFIG", "/app/config/tracks.yaml"))
        ttl = int(os.getenv("SESSION_TTL_SECONDS", "2592000"))
        if not 300 <= ttl <= 60 * 60 * 24 * 90:
            raise RuntimeError("SESSION_TTL_SECONDS must be between 300 and 7776000")

        secure_raw = os.getenv("COOKIE_SECURE", "true").strip().lower()
        if secure_raw not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise RuntimeError("COOKIE_SECURE must be a boolean value")
        cookie_secure = secure_raw in {"1", "true", "yes", "on"}

        cookies_path: Path | None = None
        cookies_raw = os.getenv("YTDLP_COOKIES", "").strip()
        if cookies_raw:
            cookies_path = Path(cookies_raw)
            if not cookies_path.is_file():
                # Downloads stay disabled but the audio gateway must keep working.
                logger.warning("YTDLP_COOKIES file does not exist; server-side downloads disabled: %s", cookies_raw)
                cookies_path = None
        downloads_enabled = bool(cookies_path)
        return cls(
            access_token=access_token,
            app_secret_key=secret_key,
            allowed_audio_hosts=hosts,
            tracks_config=config_path,
            db_path=Path(os.getenv("DB_PATH", "/app/data/caraok.db")),
            cookie_secure=cookie_secure,
            session_ttl_seconds=ttl,
            media_dir=Path(os.getenv("MEDIA_DIR", "/app/media")),
            public_media_base=os.getenv("PUBLIC_MEDIA_BASE", "").rstrip("/"),
            ytdl_cookies=cookies_path,
            ytdl_cache=Path(os.getenv("YTDLP_CACHE_DIR", "/app/.ytdl-cache")),
            ytdl_proxy=os.getenv("YTDLP_PROXY", "").strip(),
            ytdl_pot_url=os.getenv("YTDLP_POT_URL", "").strip(),
            downloads_enabled=downloads_enabled,
        )
