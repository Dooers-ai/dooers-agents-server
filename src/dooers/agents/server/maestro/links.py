"""Temporary artifact links for one execution: one cached link per artifact.

A link is a bearer URL (no login): whoever has it reads the file until it expires. It is
never logged and never leaves this module except as the return value of ``url_for``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

#: A link with less than this left is replaced before it is handed out.
REFRESH_MARGIN = timedelta(seconds=60)


def utcnow() -> datetime:
    return datetime.now(UTC)


class LinkResolver:
    """Gives the current link of an artifact; asks for a new one when less than 60 s remain.

    ``issue(artifact_id)`` asks Maestro for a link and returns ``{"url", "expires_at"}``
    (``Public.ArtifactLinkResponse``). Its errors reach the caller unchanged. Concurrent
    calls for the same artifact share one request.
    """

    def __init__(self, issue: Callable[[str], Awaitable[dict[str, Any]]], *, now: Callable[[], datetime] = utcnow) -> None:
        self._issue = issue
        self._now = now
        self._links: dict[str, tuple[str, datetime]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def url_for(self, artifact_id: str) -> str:
        lock = self._locks.setdefault(artifact_id, asyncio.Lock())
        async with lock:
            cached = self._links.get(artifact_id)
            if cached is not None and cached[1] - self._now() >= REFRESH_MARGIN:
                return cached[0]
            link = await self._issue(artifact_id)
            url, expires_at = link["url"], datetime.fromisoformat(link["expires_at"].replace("Z", "+00:00"))
            if not isinstance(url, str) or not url or expires_at.tzinfo is None:
                raise ValueError("invalid artifact link")
            self._links[artifact_id] = (url, expires_at)
            return url

    def __repr__(self) -> str:
        # Never show the URLs: they are bearer links.
        return f"LinkResolver(links={len(self._links)})"
