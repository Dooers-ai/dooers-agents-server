from __future__ import annotations

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from functools import partial

import pytest

from dooers.agents.server.maestro import media_fetch as module
from dooers.agents.server.maestro.media_fetch import FetchedMedia, MediaFetchError

LIMIT = 26214400

# Loopback fixtures: only tests may open the guard, and only for this literal host.
fetch_media = partial(module.fetch_media, _allow_hosts={"127.0.0.1"})


class Server:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[str, dict[str, str], bytes]] = {}
        self.base = ""
        self.hosts: list[str] = []
        self.no_length: set[str] = set()

    def add(self, path: str, body: bytes = b"", *, status: str = "200 OK", headers: dict[str, str] | None = None) -> None:
        self.routes[path] = (status, headers or {}, body)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            text = raw.decode()
            path = text.split(" ", 2)[1].split("?", 1)[0]
            self.hosts.append(next((ln.split(": ", 1)[1] for ln in text.split("\r\n") if ln.lower().startswith("host: ")), ""))
            status, headers, body = self.routes.get(path, ("404 Not Found", {}, b"missing"))
            head = {"Connection": "close", **headers}
            if path not in self.no_length:
                head["Content-Length"] = str(len(body))
            writer.write(f"HTTP/1.1 {status}\r\n".encode())
            writer.write("".join(f"{k}: {v}\r\n" for k, v in head.items()).encode() + b"\r\n")
            writer.write(body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


@asynccontextmanager
async def serve():
    fixture = Server()
    server = await asyncio.start_server(fixture.handle, "127.0.0.1", 0)
    fixture.base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield fixture
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def server():
    async with serve() as fixture:
        yield fixture


async def test_fetch_ok_uses_content_type_without_params(server):
    server.add("/a/photo.png", b"PNG", headers={"Content-Type": "image/png; charset=x"})
    media = await fetch_media(f"{server.base}/a/photo.png")
    assert media == FetchedMedia(data=b"PNG", mime_type="image/png", filename="photo.png")


async def test_fetch_hints_win_over_header_and_url(server):
    server.add("/a/photo.png", b"x", headers={"Content-Type": "image/png"})
    media = await fetch_media(f"{server.base}/a/photo.png", hint_mime="image/jpeg", hint_filename="custom.jpg")
    assert (media.mime_type, media.filename) == ("image/jpeg", "custom.jpg")


async def test_fetch_mime_falls_back_to_filename(server):
    server.add("/doc.pdf", b"%PDF", headers={"Content-Type": ""})
    media = await fetch_media(f"{server.base}/doc.pdf?x=1")
    assert (media.mime_type, media.filename) == ("application/pdf", "doc.pdf")


async def test_fetch_follows_redirect(server):
    server.add("/r", status="302 Found", headers={"Location": "/final.txt"})
    server.add("/final.txt", b"ok", headers={"Content-Type": "text/plain"})
    media = await fetch_media(f"{server.base}/r")
    assert media.data == b"ok"


async def test_fetch_too_many_redirects_raises_network(server):
    server.add("/loop", status="302 Found", headers={"Location": "/loop"})
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/loop")
    assert exc.value.reason == "network"


async def test_fetch_404_raises_status(server):
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/nope")
    assert exc.value.reason == "status"


async def test_fetch_over_limit_raises_too_large(server):
    server.add("/big", b"x" * (LIMIT + 1), headers={"Content-Type": "application/octet-stream"})
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/big")
    assert exc.value.reason == "too_large"


async def test_fetch_exactly_at_limit_ok(server):
    server.add("/edge", b"x" * 100, headers={"Content-Type": "application/octet-stream"})
    media = await fetch_media(f"{server.base}/edge", max_bytes=100)
    assert len(media.data) == 100


async def test_fetch_timeout_raises_timeout():
    release = asyncio.Event()

    async def slow(reader, writer):
        await release.wait()
        writer.close()

    srv = await asyncio.start_server(slow, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        with pytest.raises(MediaFetchError) as exc:
            await fetch_media(f"http://127.0.0.1:{port}/x", timeout_s=0.2)
        assert exc.value.reason == "timeout"
    finally:
        release.set()
        srv.close()
        await srv.wait_closed()


async def test_fetch_connection_refused_raises_network():
    srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    srv.close()
    await srv.wait_closed()
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"http://127.0.0.1:{port}/x")
    assert exc.value.reason == "network"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a", "data:text/plain,hi", "/relative"])
async def test_fetch_rejects_non_http_scheme(url):
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(url)
    assert exc.value.reason == "scheme"


async def test_fetch_stops_streaming_at_limit_without_content_length(server):
    server.add("/chunked", b"x" * 5000, headers={"Content-Type": "application/octet-stream"})
    server.no_length.add("/chunked")
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/chunked", max_bytes=1000)
    assert exc.value.reason == "too_large"


async def test_fetch_redirect_without_location_raises_status(server):
    server.add("/r", status="302 Found")
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/r")
    assert exc.value.reason == "status"


async def test_fetch_three_redirects_ok_four_fail(server):
    server.add("/a", status="302 Found", headers={"Location": "/b"})
    server.add("/b", status="302 Found", headers={"Location": "/c"})
    server.add("/c", status="302 Found", headers={"Location": "/d.txt"})
    server.add("/d.txt", b"ok", headers={"Content-Type": "text/plain"})
    assert (await fetch_media(f"{server.base}/a")).data == b"ok"
    server.add("/c", status="302 Found", headers={"Location": "/c2"})
    server.add("/c2", status="302 Found", headers={"Location": "/d.txt"})
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/a")
    assert exc.value.reason == "network"


async def test_fetch_filename_comes_from_final_url_after_redirect(server):
    server.add("/short", status="302 Found", headers={"Location": "/files/report.pdf"})
    server.add("/files/report.pdf", b"%PDF", headers={"Content-Type": "application/pdf"})
    media = await fetch_media(f"{server.base}/short")
    assert media.filename == "report.pdf"


async def test_fetch_redirect_to_file_scheme_raises_scheme(server):
    server.add("/r", status="302 Found", headers={"Location": "file:///etc/passwd"})
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/r")
    assert exc.value.reason == "scheme"


# --- SSRF guard -------------------------------------------------------------------------------


@pytest.fixture
def no_connect(monkeypatch):
    """Fails the test if the guard lets any request reach httpx."""
    calls: list[str] = []
    real = module.httpx.AsyncClient.stream

    def spy(self, method, url, **kwargs):
        calls.append(str(url))
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(module.httpx.AsyncClient, "stream", spy)
    return calls


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "http://017700000001/",
        "http://127.1/",
        "http://127.0.0.1:9/",
        "http://localhost-as-ip.invalid@10.0.0.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://[::1]/",
        "http://[::]/",
        "http://[::7f00:1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
async def test_fetch_blocks_non_public_literals_without_connecting(url, no_connect):
    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media(url)
    assert exc.value.reason == "blocked"
    assert no_connect == []


async def test_fetch_blocks_hostname_resolving_to_private_ip(no_connect):
    async def resolver(host, port):
        return ["10.0.0.1"]

    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media("http://media.example.com/a.png", _resolver=resolver)
    assert exc.value.reason == "blocked"
    assert no_connect == []


async def test_fetch_blocks_when_any_resolved_address_is_private(no_connect):
    async def resolver(host, port):
        return ["93.184.216.34", "10.0.0.1"]

    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media("http://media.example.com/a.png", _resolver=resolver)
    assert exc.value.reason == "blocked"
    assert no_connect == []


async def test_fetch_blocks_redirect_to_metadata_ip(server, no_connect):
    server.add("/r", status="302 Found", headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/r")
    assert exc.value.reason == "blocked"
    assert len(no_connect) == 1  # only the allowed first hop was requested


async def test_fetch_blocks_redirect_to_hostname_resolving_private(server, no_connect):
    server.add("/r", status="302 Found", headers={"Location": "http://internal.example.com/x"})

    async def resolver(host, port):
        return ["127.0.0.1"] if host == "127.0.0.1" else ["192.168.0.5"]

    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"{server.base}/r", _resolver=resolver)
    assert exc.value.reason == "blocked"
    assert len(no_connect) == 1


async def test_fetch_connects_to_validated_ip_and_keeps_host_header(server):
    """The fake resolver is the only source of the address: the request must go to that IP with the original Host."""
    resolved: list[str] = []

    async def resolver(host, port):
        resolved.append(host)
        return ["127.0.0.1"]

    server.add("/p.png", b"PNG", headers={"Content-Type": "image/png"})
    port = server.base.rsplit(":", 1)[1]
    media = await module.fetch_media(f"http://pinned.example:{port}/p.png", _resolver=resolver, _allow_hosts={"pinned.example"})
    assert media.data == b"PNG"
    assert server.hosts == [f"pinned.example:{port}"]
    assert resolved == ["pinned.example"]  # resolved once; httpx never re-resolves the name


async def test_fetch_guard_is_closed_by_default(server):
    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media(f"{server.base}/x")
    assert exc.value.reason == "blocked"


def test_public_addresses_pass_the_guard_without_any_network():
    for address in ("93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"):
        assert module._is_public(ipaddress.ip_address(address))
    for address in ("fec0::1", "fec0::ffff:1", "10.0.0.1", "169.254.169.254", "::ffff:10.0.0.1"):
        assert not module._is_public(ipaddress.ip_address(address))


async def test_fetch_blocks_site_local_ipv6(no_connect):
    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media("http://[fec0::1]/")
    assert exc.value.reason == "blocked"
    assert no_connect == []


async def test_fetch_idn_host_is_sent_as_idna_host_header(server):
    resolved: list[str] = []

    async def resolver(host, port):
        resolved.append(host)
        return ["127.0.0.1"]

    server.add("/a.png", b"PNG", headers={"Content-Type": "image/png"})
    port = server.base.rsplit(":", 1)[1]
    media = await module.fetch_media(f"http://ex\u00e4mple.com:{port}/a.png", _resolver=resolver, _allow_hosts={"ex\u00e4mple.com"})
    assert media.data == b"PNG"
    assert resolved == ["xn--exmple-cua.com"]
    assert server.hosts == [f"xn--exmple-cua.com:{port}"]


async def test_fetch_idn_host_resolving_to_private_ip_is_blocked(no_connect):
    async def resolver(host, port):
        return ["10.0.0.1"]

    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media("http://ex\u00e4mple.com/a.png", _resolver=resolver)
    assert exc.value.reason == "blocked"
    assert no_connect == []


@pytest.mark.parametrize(
    "url",
    [
        "http://exa\x00mple.com/a.png",
        "http://exa mple.com/a.png",
        "http://" + "a" * 64 + ".example.com/a.png",
        "http://" + "\u00e4" * 64 + ".example.com/a.png",
        "http://a..b/a.png",
        "http://[::1/a.png",
    ],
)
async def test_fetch_malformed_hosts_raise_scheme_not_other_exceptions(url, no_connect):
    async def resolver(host, port):  # a real resolver would also reject these
        raise UnicodeError("label empty or too long")

    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media(url, _resolver=resolver)
    assert exc.value.reason in {"scheme", "network"}
    assert no_connect == []


@pytest.mark.parametrize("url", ["http://exa\x00mple.com/a.png", "http://" + "a" * 64 + ".example.com/a.png"])
async def test_fetch_nul_byte_and_long_label_are_scheme(url):
    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media(url)
    assert exc.value.reason == "scheme"


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), KeyError("k"), UnicodeEncodeError("ascii", "\u00e4", 0, 1, "bad"), ValueError("v")]
)
async def test_fetch_resolver_exceptions_become_media_fetch_error(error):
    async def resolver(host, port):
        raise error

    with pytest.raises(MediaFetchError):
        await module.fetch_media("http://media.example.com/a.png", _resolver=resolver)


async def test_fetch_unexpected_internal_error_becomes_media_fetch_error(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(module, "_download", boom)
    with pytest.raises(MediaFetchError) as exc:
        await module.fetch_media("http://media.example.com/a.png")
    assert exc.value.reason == "network"


async def test_fetch_closed_loopback_port_raises_network(server):
    """A port nobody listens on: network error against loopback only (no external traffic)."""
    srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    srv.close()
    await srv.wait_closed()
    with pytest.raises(MediaFetchError) as exc:
        await fetch_media(f"http://127.0.0.1:{port}/x")
    assert exc.value.reason == "network"


# --- data: URLs (decoded locally, no network) ---------------------------------


def test_data_url_base64_keeps_bytes_and_mime():
    import base64

    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    media = module.decode_data_url("data:image/png;base64," + base64.b64encode(png).decode(), hint_mime="image/jpeg")
    assert media == FetchedMedia(data=png, mime_type="image/png", filename="file.png")


def test_data_url_percent_encoded_and_defaults():
    assert module.decode_data_url("data:text/plain;charset=utf-8,Ol%C3%A1%2C%20mundo", hint_filename="nota.txt") == FetchedMedia(
        data="Olá, mundo".encode(), mime_type="text/plain", filename="nota.txt"
    )
    # No MIME in the URL: the declared one, then text/plain (RFC 2397).
    assert module.decode_data_url("data:;base64,AAE=", hint_mime="audio/wav").mime_type == "audio/wav"
    assert module.decode_data_url("data:,x").mime_type == "text/plain"


@pytest.mark.parametrize(
    "url,reason",
    [
        ("data:image/png;base64,@@@", "scheme"),
        ("data:image/png;base64,a", "scheme"),  # length % 4 == 1 cannot be base64
        ("data:image/png;base64,AA=A", "scheme"),  # "=" inside the data
        ("data:image/png;base64", "scheme"),  # no comma
        ("https://example.com/x.png", "scheme"),
        ("data:image/png;base64,AAECAwQF", "too_large"),  # 6 bytes
        ("data:text/plain,123456", "too_large"),
    ],
)
def test_data_url_errors(url, reason):
    with pytest.raises(MediaFetchError) as error:
        module.decode_data_url(url, max_bytes=5)
    assert error.value.reason == reason


def test_data_url_base64_is_forgiving_like_browsers():
    png = b"\x89PNG\r\n\x1a\n"  # iVBORw0KGgo=
    spaced = "data:image/png;base64,iVBO Rw0K\n\tGgo"  # ASCII whitespace and no padding
    assert module.decode_data_url(spaced).data == png
    assert module.decode_data_url("data:;base64,AAE").data == b"\x00\x01"
    assert module.decode_data_url("data:;base64,AAE%3D").data == b"\x00\x01"  # percent-encoded padding


def _traced_peak(function):
    import tracemalloc

    tracemalloc.start()
    try:
        function()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_too_long_data_url_is_refused_before_any_copy():
    url = "data:image/png;base64," + " ".join(["AAAA"] * 2_000_000)  # 10 MB of spaced base64

    def decode():
        with pytest.raises(MediaFetchError) as error:
            module.decode_data_url(url, max_bytes=1000)
        assert error.value.reason == "too_large"

    assert _traced_peak(decode) < 64 * 1024


def test_spaced_base64_within_the_limit_has_bounded_memory():
    import base64

    data = bytes(range(256)) * 8192  # 2 MiB
    encoded = base64.b64encode(data).decode()
    url = "data:application/pdf;base64," + "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
    result = []
    peak = _traced_peak(lambda: result.append(module.decode_data_url(url, max_bytes=3 * 1048576)))
    assert result[0].data == data
    assert peak < 4 * len(url)  # a few linear copies, never a list of chunks
