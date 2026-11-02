"""Shared Origin checks for cookie-authenticated write routes."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def ensure_same_origin(request: Request) -> None:
    """Reject cross-origin and malformed Origin headers with 403, never 500."""
    origin = request.headers.get("origin")
    host = request.headers.get("host", "").lower()
    if not origin:
        return
    try:
        parsed = urlsplit(origin)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid request origin") from exc
    if parsed.netloc.lower() != host or parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
