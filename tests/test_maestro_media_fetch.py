from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from dooers.agents.server.maestro.media_fetch import FetchedMedia, MediaFetchError, fetch_media

LIMIT = 26214400


class Server:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[str, dict[str, str], bytes]] = {}
        self.base = ""

    def add(self, path: str, body: bytes = b"", *, status: str = "200 OK", headers: dict[str, str] | None = None) -> None:
        self.routes[path] = (status, headers or {}, body)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            path = raw.decode().split(" ", 2)[1].split("?", 1)[0]
            status, headers, body = self.routes.get(path, ("404 Not Found", {}, b"missing"))
            head = {"Content-Length": str(len(body)), "Connection": "close", **headers}
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
    async def slow(reader, writer):
        await asyncio.sleep(5)
        writer.close()

    srv = await asyncio.start_server(slow, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        with pytest.raises(MediaFetchError) as exc:
            await fetch_media(f"http://127.0.0.1:{port}/x", timeout_s=0.2)
        assert exc.value.reason == "timeout"
    finally:
        srv.close()


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
