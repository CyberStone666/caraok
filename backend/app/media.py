"""Local media path resolution.

Tracks downloaded by the admin pipeline are stored under ``media_dir`` and
referenced by a public URL (``PUBLIC_MEDIA_BASE`` + bare file name). Those
bytes must never be served as static files: the web container no longer
mounts ``./media`` and Caddy answers ``/media/*`` with 404, so the only way
to read them is the authenticated ``/api/v1/stream/{id}`` route, which
resolves the URL back to a file inside ``media_dir`` here.
"""

from __future__ import annotations

from pathlib import Path

from .settings import Settings


def resolve_local_media(settings: Settings, url: str) -> Path | None:
    """Return the local file backing ``url``, or None when it is not local.

    The name must be a bare file name: anything containing a separator or a
    dot-segment is rejected so a crafted catalog entry cannot escape
    ``media_dir``.
    """
    base = settings.public_media_base
    if not base:
        return None
    prefix = f"{base}/"
    if not url.startswith(prefix):
        return None
    name = url[len(prefix):]
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return None
    root = settings.media_dir.resolve()
    candidate = (root / name).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        return None
    return candidate
