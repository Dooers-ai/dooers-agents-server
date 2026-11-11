from __future__ import annotations

import asyncio
import mimetypes
import posixpath
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx

MediaFetchReason = Literal["status", "too_large", "timeout", "scheme", "network"]

DEFAULT_MAX_BYTES = 26214400
MAX_REDIRECTS = 3
FALLBACK_MIME = "application/octet-stream"
FALLBACK_FILENAME = "file"


@dataclass(frozen=True)
class FetchedMedia:
    data: bytes
    mime_type: str
    filename: str


class MediaFetchError(Exception):
    def __init__(self, reason: MediaFetchReason, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason: MediaFetchReason = reason


def _filename_from_url(url: str) -> str:
    name = posixpath.basename(unquote(urlsplit(url).path))
    return name or FALLBACK_FILENAME


def _clean_mime(value: str | None) -> str | None:
    if not value:
        return None
    mime = value.split(";", 1)[0].strip().lower()
    return mime or None


async def fetch_media(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_s: float = 30.0,
    hint_mime: str | None = None,
    hint_filename: str | None = None,
) -> FetchedMedia:
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise MediaFetchError("scheme", f"unsupported URL scheme: {scheme or 'none'}")
    try:
        async with asyncio.timeout(timeout_s):
            data, content_type = await _download(url, max_bytes, timeout_s)
    except TimeoutError as exc:
        raise MediaFetchError("timeout") from exc
    filename = hint_filename or _filename_from_url(url)
    mime = _clean_mime(hint_mime) or _clean_mime(content_type) or mimetypes.guess_type(filename)[0] or FALLBACK_MIME
    return FetchedMedia(data=data, mime_type=mime, filename=filename)


async def _download(url: str, max_bytes: int, timeout_s: float) -> tuple[bytes, str | None]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, max_redirects=MAX_REDIRECTS, trust_env=False, timeout=timeout_s) as client:
            async with client.stream("GET", url) as response:
                if not response.is_success:
                    raise MediaFetchError("status", f"HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise MediaFetchError("too_large")
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise MediaFetchError("too_large")
                return bytes(buffer), response.headers.get("content-type")
    except httpx.TimeoutException as exc:
        raise MediaFetchError("timeout") from exc
    except httpx.InvalidURL as exc:
        raise MediaFetchError("scheme", str(exc)) from exc
    except httpx.HTTPError as exc:
        raise MediaFetchError("network", f"{type(exc).__name__}: {exc}") from exc
