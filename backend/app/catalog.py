"""Catalog storage: SQLite as the single source of truth.

Replaces the earlier hot-reloaded YAML file. The public interface is
unchanged (snapshot / get_track / replace_document), so the admin API and
stream endpoints need no knowledge of the storage layer. A legacy
tracks.yaml is imported once on first boot and left untouched afterwards.
"""

from __future__ import annotations

import ipaddress
import logging
import hashlib
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger("audio_gateway.catalog")

_ALLOWED_MIME_TYPES = {
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/flac",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/x-wav",
    "application/octet-stream",
}
ALLOWED_MIME_TYPES = _ALLOWED_MIME_TYPES
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    order_index INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT 'Unknown artist',
    category_id TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cover_url TEXT,
    url TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'audio/mpeg',
    enabled INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0
);
"""


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)
    order: int = 0

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("category id may only contain letters, numbers, '_' and '-'")
        return value


class Track(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=160)
    artist: str = Field(default="Unknown artist", max_length=120)
    category_id: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    cover_url: str | None = Field(default=None, max_length=2048)
    url: str = Field(min_length=1, max_length=4096, repr=False)
    mime_type: str = "audio/mpeg"
    enabled: bool = True
    order: int = 0

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("track id may only contain letters, numbers, '_' and '-'")
        return value

    @field_validator("mime_type")
    @classmethod
    def valid_mime_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_MIME_TYPES:
            raise ValueError(f"unsupported mime_type: {value}")
        return normalized

    @field_validator("cover_url")
    @classmethod
    def valid_cover_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("cover_url must be an HTTPS URL without embedded credentials")
        return value

    def validate_cover_host(self, allowed_hosts: frozenset[str]) -> None:
        if not self.cover_url:
            return
        parsed = urlsplit(self.cover_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not host
            or "." not in host
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or host not in allowed_hosts
        ):
            raise ValueError("cover_url must use an allow-listed HTTPS hostname without query or fragment")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return
        raise ValueError("cover_url must not use an IP-literal host")

    def validate_source_host(self, allowed_hosts: frozenset[str]) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
            raise ValueError("audio url must be an absolute HTTPS URL on the default port")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("audio url must not contain credentials or a fragment")
        host = parsed.hostname.lower().rstrip(".")
        if "." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("audio host must be a public DNS hostname")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP-literal audio hosts are not allowed; use an allow-listed DNS hostname")
        if host not in allowed_hosts:
            raise ValueError(f"audio host '{host}' is not in AUDIO_ALLOWED_HOSTS")


class CatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[Category] = Field(default_factory=list)
    tracks: list[Track] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_and_referenced(self) -> "CatalogDocument":
        category_ids = [item.id for item in self.categories]
        track_ids = [item.id for item in self.tracks]
        if len(category_ids) != len(set(category_ids)):
            raise ValueError("category ids must be unique")
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("track ids must be unique")
        known_categories = set(category_ids)
        for track in self.tracks:
            if track.category_id not in known_categories:
                raise ValueError(f"track '{track.id}' references unknown category '{track.category_id}'")
        return self


def _load_yaml_document(path: Path) -> CatalogDocument:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return CatalogDocument.model_validate(raw)


class Catalog:
    """SQLite-backed catalog. WAL journaling + a process-wide write lock."""

    def __init__(self, db_path: Path, allowed_hosts: frozenset[str], yaml_seed: Path | None = None):
        self.db_path = db_path
        self.allowed_hosts = allowed_hosts
        self._lock = threading.RLock()
        # 令牌存在性短缓存:secondary 会话每请求校验时避免持锁查库;
        # 删除令牌会主动失效,保证"删即吊销"的语义不因缓存打折。
        self._token_exists_cache: dict[str, tuple[float, bool]] = {}
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate_yaml_seed(yaml_seed)

    # ── row mapping ──
    @staticmethod
    def _track_from_row(row: sqlite3.Row) -> Track:
        return Track(
            id=row["id"], title=row["title"], artist=row["artist"],
            category_id=row["category_id"], description=row["description"],
            cover_url=row["cover_url"], url=row["url"], mime_type=row["mime_type"],
            enabled=bool(row["enabled"]), order=row["order_index"],
        )

    @staticmethod
    def _category_from_row(row: sqlite3.Row) -> Category:
        return Category(
            id=row["id"], name=row["name"],
            description=row["description"], order=row["order_index"],
        )

    # ── legacy migration ──
    def _migrate_yaml_seed(self, yaml_seed: Path | None) -> None:
        if yaml_seed is None or not yaml_seed.is_file():
            return
        with self._lock:
            counts = self._conn.execute(
                "SELECT (SELECT COUNT(*) FROM categories) AS c, (SELECT COUNT(*) FROM tracks) AS t"
            ).fetchone()
            if counts["c"] or counts["t"]:
                return
            try:
                document = _load_yaml_document(yaml_seed)
            except (OSError, yaml.YAMLError, ValidationError) as exc:
                logger.warning("YAML seed %s could not be parsed; starting with an empty catalog (%s)", yaml_seed, exc.__class__.__name__)
                return
            try:
                self._write(document.categories, document.tracks)
            except ValueError as exc:
                logger.warning("YAML seed rejected by host validation; starting empty: %s", exc)
                return
            logger.info(
                "Migrated catalog from %s: %d categories, %d tracks",
                yaml_seed, len(document.categories), len(document.tracks),
            )

    # ── reads ──
    def snapshot(self) -> CatalogDocument:
        with self._lock:
            return self._read_document()

    def update(self, mutator):
        """Atomic read-modify-write: mutator runs under the catalog lock and may
        raise; nothing is written unless it returns a valid document."""
        with self._lock:
            current = self._read_document()
            replacement = mutator(current)
            self._write(replacement.categories, replacement.tracks)
        return self._read_document()

    def _read_document(self) -> CatalogDocument:
        categories = [
            self._category_from_row(row)
            for row in self._conn.execute("SELECT * FROM categories ORDER BY order_index, id")
        ]
        tracks = [
            self._track_from_row(row)
            for row in self._conn.execute("SELECT * FROM tracks ORDER BY order_index, title COLLATE NOCASE")
        ]
        return CatalogDocument(categories=categories, tracks=tracks)

    def get_track(self, track_id: str) -> Track | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE id = ? AND enabled = 1", (track_id,)
            ).fetchone()
        return self._track_from_row(row) if row else None

    # ── 访客访问令牌(#两级 Token):只存 SHA-256 哈希,明文仅在生成时返回一次 ──
    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_access_token(self, name: str) -> tuple[str, dict[str, Any]]:
        """生成新访客令牌;返回 (明文一次, 元数据)。"""
        token_id = "tok-" + secrets.token_hex(8)
        plaintext = "ck_" + secrets.token_hex(24)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO access_tokens (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                (token_id, name, self._hash_token(plaintext), now),
            )
            self._conn.commit()
        return plaintext, {"id": token_id, "name": name, "created_at": now, "last_used_at": None}

    def list_access_tokens(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, last_used_at FROM access_tokens ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_access_token(self, token_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, created_at, last_used_at FROM access_tokens WHERE id = ?",
                (token_id,),
            ).fetchone()
        return dict(row) if row else None

    def access_token_exists(self, token_id: str, ttl_seconds: float = 5.0) -> bool:
        """会话热路径专用:带 TTL 的存在性检查(缓存未命中才查库)。"""
        now = time.time()
        with self._lock:
            cached = self._token_exists_cache.get(token_id)
            if cached is not None and cached[0] > now:
                return cached[1]
        exists = self.get_access_token(token_id) is not None
        with self._lock:
            if len(self._token_exists_cache) > 512:
                self._token_exists_cache.clear()
            self._token_exists_cache[token_id] = (now + ttl_seconds, exists)
        return exists

    def find_access_token_by_secret(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, created_at, last_used_at FROM access_tokens WHERE token_hash = ?",
                (self._hash_token(token),),
            ).fetchone()
        return dict(row) if row else None

    def touch_access_token(self, token_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE access_tokens SET last_used_at = ? WHERE id = ?", (time.time(), token_id)
            )
            self._conn.commit()

    def delete_access_token(self, token_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM access_tokens WHERE id = ?", (token_id,))
            self._conn.commit()
            # 立即失效:被吊销的会话不能因缓存继续通过校验
            self._token_exists_cache[token_id] = (0.0, False)
        return cursor.rowcount > 0

    def get_category(self, category_id: str) -> Category | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM categories WHERE id = ?", (category_id,)
            ).fetchone()
        return self._category_from_row(row) if row else None

    # ── writes ──
    def _write(self, categories: list[Category], tracks: list[Track]) -> None:
        document = CatalogDocument(categories=list(categories), tracks=list(tracks))
        for track in document.tracks:
            track.validate_source_host(self.allowed_hosts)
            track.validate_cover_host(self.allowed_hosts)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("DELETE FROM tracks")
                self._conn.execute("DELETE FROM categories")
                self._conn.executemany(
                    "INSERT INTO categories (id, name, description, order_index) VALUES (?, ?, ?, ?)",
                    [(cat.id, cat.name, cat.description, cat.order) for cat in document.categories],
                )
                self._conn.executemany(
                    "INSERT INTO tracks (id, title, artist, category_id, description, cover_url, url, mime_type, enabled, order_index) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            track.id, track.title, track.artist, track.category_id,
                            track.description, track.cover_url, track.url, track.mime_type,
                            int(track.enabled), track.order,
                        )
                        for track in document.tracks
                    ],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.info("Catalog written: %d categories, %d tracks", len(document.categories), len(document.tracks))

    def replace_document(self, categories: list[Category], tracks: list[Track]) -> None:
        """Validate and atomically replace the whole catalog."""
        self._write(categories, tracks)
