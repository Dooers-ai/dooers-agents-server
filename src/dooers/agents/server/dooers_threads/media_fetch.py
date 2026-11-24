from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import mimetypes
import posixpath
import socket
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlsplit

import httpx

MediaFetchReason = Literal["status", "too_large", "timeout", "scheme", "network", "blocked"]

DEFAULT_MAX_BYTES = 26214400
MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
FALLBACK_MIME = "application/octet-stream"
FALLBACK_FILENAME = "file"

NAT64_NETWORK = ipaddress.IPv6Network("64:ff9b::/96")
SITE_LOCAL_NETWORK = ipaddress.IPv6Network("fec0::/10")  # deprecated site-local; not flagged by is_global
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]


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


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv4 carried inside an IPv6 address: mapped, compatible, NAT64 and 6to4."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    value = int(ip)
    if 1 < value < 2**32:  # ::a.b.c.d (IPv4-compatible)
        return ipaddress.IPv4Address(value)
    if ip in NAT64_NETWORK:
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    if value >> 112 == 0x2002:  # 2002:a.b.c.d::/48 (6to4)
        return ipaddress.IPv4Address((value >> 80) & 0xFFFFFFFF)
    return None


def _is_public(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return _is_public(embedded)
    if isinstance(ip, ipaddress.IPv6Address) and ip in SITE_LOCAL_NETWORK:
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _literal_ip(host: str) -> IPAddress | None:
    """Parse IP literals, including decimal/octal/hex IPv4 forms such as 2130706433."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _idna_host(host: str) -> str:
    """Lowercase ASCII (IDNA) form of the host. Raises ValueError for hosts that cannot be encoded."""
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in host):
        raise ValueError("host contains control or space characters")
    if host.isascii():
        host = host.lower()
        if any(len(label) > 63 for label in host.split(".")):
            raise ValueError("host label is longer than 63 characters")
        return host
    return host.encode("idna").decode("ascii").lower()  # UnicodeError (a ValueError) on invalid labels


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


@dataclass(frozen=True)
class _Target:
    url: str
    host: str
    port: int
    ip: str


async def _resolve_target(url: str, resolver: Resolver, allow_hosts: Collection[str]) -> _Target:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise MediaFetchError("scheme", f"invalid URL: {exc}") from exc
    if scheme not in ("http", "https"):
        raise MediaFetchError("scheme", f"unsupported URL scheme: {scheme or 'none'}")
    if not host:
        raise MediaFetchError("scheme", "URL has no host")
    port = port or (443 if scheme == "https" else 80)
    try:
        host = _idna_host(host)
        literal = _literal_ip(host)
    except ValueError as exc:
        raise MediaFetchError("scheme", f"invalid host: {exc}") from exc
    try:
        addresses = [str(literal)] if literal is not None else await resolver(host, port)
    except OSError as exc:
        raise MediaFetchError("network", f"DNS resolution failed for {host}: {exc}") from exc
    except ValueError as exc:  # includes UnicodeError
        raise MediaFetchError("scheme", f"invalid host {host}: {exc}") from exc
    if not addresses:
        raise MediaFetchError("network", f"DNS resolution returned no address for {host}")
    parsed = [ipaddress.ip_address(address) for address in addresses]
    if host not in allow_hosts and not all(_is_public(ip) for ip in parsed):
        raise MediaFetchError("blocked", f"host {host} resolves to a non-public address")
    return _Target(url=url, host=host, port=port, ip=addresses[0])


def _pinned_request(target: _Target) -> tuple[str, dict[str, str], dict[str, str]]:
    """Request aimed at the validated IP, keeping the original Host header and SNI."""
    parts = urlsplit(target.url)
    ip = ipaddress.ip_address(target.ip)
    netloc_ip = f"[{ip}]" if ip.version == 6 else str(ip)
    path = parts.path or "/"
    pinned = f"{parts.scheme.lower()}://{netloc_ip}:{target.port}{path}" + (f"?{parts.query}" if parts.query else "")
    default_port = 443 if parts.scheme.lower() == "https" else 80
    host_header = f"[{target.host}]" if ":" in target.host else target.host
    if target.port != default_port:
        host_header = f"{host_header}:{target.port}"
    extensions = {"sni_hostname": target.host} if parts.scheme.lower() == "https" else {}
    return pinned, {"Host": host_header}, extensions


async def fetch_media(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_s: float = 30.0,
    hint_mime: str | None = None,
    hint_filename: str | None = None,
    _resolver: Resolver | None = None,
    _allow_hosts: Collection[str] = (),
) -> FetchedMedia:
    """Download a media URL with production limits.

    Every hop (the first request and each redirect) must use http/https and resolve only to
    globally routable addresses. The request connects to the validated IP, so DNS rebinding
    between check and connect cannot reach an internal address.

    `_resolver` and `_allow_hosts` exist only so tests can use loopback servers. Production
    callers must not pass them.
    """
    resolver = _resolver or _system_resolver
    allow_hosts = frozenset(_idna_host(host) for host in _allow_hosts)
    try:
        async with asyncio.timeout(timeout_s):
            data, content_type, final_url = await _download(url, max_bytes, timeout_s, resolver, allow_hosts)
    except MediaFetchError:
        raise
    except TimeoutError as exc:
        raise MediaFetchError("timeout") from exc
    except ValueError as exc:  # includes UnicodeError: malformed URL or host
        raise MediaFetchError("scheme", f"invalid URL: {exc}") from exc
    except Exception as exc:  # last guard: callers only ever see MediaFetchError
        raise MediaFetchError("network", f"{type(exc).__name__}: {exc}") from exc
    filename = hint_filename or _filename_from_url(final_url)
    mime = _clean_mime(hint_mime) or _clean_mime(content_type) or mimetypes.guess_type(filename)[0] or FALLBACK_MIME
    return FetchedMedia(data=data, mime_type=mime, filename=filename)


_DATA_URL_SLACK = 4096  # header and percent-encoding room above the base64 size
_ASCII_WHITESPACE = b" \t\n\x0c\r"  # WHATWG forgiving-base64 removes these


def is_data_url(url: object) -> bool:
    return isinstance(url, str) and url[:5].lower() == "data:"


def decode_data_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    hint_mime: str | None = None,
    hint_filename: str | None = None,
) -> FetchedMedia:
    """Decode a ``data:`` URL (RFC 2397) locally, with the same size limit as ``fetch_media``.

    ``data:[<mime>][;<param>=<value>]*[;base64],<data>``. The MIME comes from the URL;
    without one, ``hint_mime`` and then ``text/plain`` (RFC 2397 default). Base64 is
    forgiving, like browsers: ASCII whitespace is ignored and missing padding is completed.

    Memory stays linear in the URL: a URL longer than ``ceil(max_bytes * 4 / 3) + 4096`` is
    refused before any copy, so a percent-encoded file near the limit is refused too.
    Raises only ``MediaFetchError``: ``scheme`` for a malformed URL, ``too_large`` above the limit.
    """
    if not is_data_url(url):
        raise MediaFetchError("scheme", "not a data URL")
    if len(url) > -(-max_bytes * 4 // 3) + _DATA_URL_SLACK:
        raise MediaFetchError("too_large")
    comma = url.find(",", 5)
    if comma < 0:
        raise MediaFetchError("scheme", "data URL without ','")
    params = [part.strip() for part in url[5:comma].split(";")]
    encoded = len(params) > 1 and params[-1].lower() == "base64"
    if encoded:
        params.pop()
    mime = _clean_mime(params[0]) if params and "=" not in params[0] else None
    try:
        data = unquote_to_bytes(url[comma + 1 :])
        if encoded:
            data = data.translate(None, _ASCII_WHITESPACE)
            if len(data) % 4 == 0 and data.endswith(b"="):
                data = data[:-2] if data.endswith(b"==") else data[:-1]
            if len(data) % 4 == 1:
                raise ValueError("base64 length")
            data = base64.b64decode(data + b"=" * (-len(data) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaFetchError("scheme", f"invalid data URL payload: {type(exc).__name__}") from exc
    if len(data) > max_bytes:
        raise MediaFetchError("too_large")
    mime = mime or _clean_mime(hint_mime) or "text/plain"
    extension = mimetypes.guess_extension(mime) or ""
    return FetchedMedia(data=data, mime_type=mime, filename=hint_filename or f"{FALLBACK_FILENAME}{extension}")


async def _download(
    url: str, max_bytes: int, timeout_s: float, resolver: Resolver, allow_hosts: Collection[str]
) -> tuple[bytes, str | None, str]:
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout_s) as client:
            current = url
            for hop in range(MAX_REDIRECTS + 1):
                target = await _resolve_target(current, resolver, allow_hosts)
                pinned_url, headers, extensions = _pinned_request(target)
                async with client.stream("GET", pinned_url, headers=headers, extensions=extensions) as response:
                    location = response.headers.get("location")
                    if response.status_code in REDIRECT_STATUSES and location:
                        if hop == MAX_REDIRECTS:
                            raise MediaFetchError("network", f"more than {MAX_REDIRECTS} redirects")
                        current = urljoin(current, location)
                        continue
                    if not response.is_success:
                        raise MediaFetchError("status", f"HTTP {response.status_code}")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise MediaFetchError("too_large")
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk[: max_bytes + 1 - len(buffer)])
                        if len(buffer) > max_bytes:
                            raise MediaFetchError("too_large")
                    return bytes(buffer), response.headers.get("content-type"), current
            raise MediaFetchError("network", "redirect loop")  # pragma: no cover
    except httpx.TimeoutException as exc:
        raise MediaFetchError("timeout") from exc
    except httpx.InvalidURL as exc:
        raise MediaFetchError("scheme", str(exc)) from exc
    except httpx.HTTPError as exc:
        raise MediaFetchError("network", f"{type(exc).__name__}: {exc}") from exc
