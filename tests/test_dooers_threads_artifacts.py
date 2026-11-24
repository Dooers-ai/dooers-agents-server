from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from dooers.agents.server.dooers_threads.artifacts import DOWNLOAD_ATTEMPTS, DooersThreadsArtifactsClient
from dooers.agents.server.dooers_threads.client import DooersThreadsContextError
from dooers.agents.server.dooers_threads.context import dooers_threads_handler_context
from dooers.agents.server.dooers_threads.memory import DooersThreadsArtifactReference
from tests.test_dooers_threads_context import contracts as contracts
from tests.test_dooers_threads_context import fixture_server, prepare, private
from tests.test_dooers_threads_context import private_key as private_key


def media(memory, *, maximum=1024, timeout=1):
    memory._client.config = replace(
        memory._client.config, artifact_mime_types=("text/plain",), max_artifact_bytes=maximum, artifact_timeout_seconds=timeout
    )
    # Authenticated signed fixture already contains the implemented artifact operations.
    return DooersThreadsArtifactsClient(memory._client)


def prepare_media(key, contracts, server):
    return prepare(key, contracts, server)


def reference(memory, **extra):
    e = memory._envelope
    return DooersThreadsArtifactReference(
        str(uuid4()), e["trigger_event_id"], e["trigger_sequence"], {}, filename="fixture.txt", mime_type="text/plain", **extra
    )


async def test_proxy_download_typed_bytes_and_repeated_authorization(private_key, contracts):
    async with fixture_server() as server:
        memory, page, requests, token = prepare_media(private_key, contracts, server)
        client = media(memory)
        ref = reference(memory, size_bytes=5)
        server.response = b"hello"
        server.extra_headers = {"Content-Type": "text/plain"}
        for _ in range(2):
            result = await client.hydrate(ref)
            part = result.content_part("https://dooers-threads.example/v1/artifact-links/a.b")
            assert part.data == b"hello" and part.url == "https://dooers-threads.example/v1/artifact-links/a.b"
        assert len(requests) == 2 and all("artifact" not in req.scopes for req in requests)
        for first, headers in server.requests:
            assert first == f"GET /api/v1/artifacts/{ref.artifact_id}/download HTTP/1.1"
            assert headers["Authorization"] == f"Bearer {token}"
            assert headers["Dooers-Threads-Execution-Capability"] == memory._client._capability


async def test_empty_file_reaches_the_handler_as_production_hands_it(private_key, contracts):
    """A 0-byte upload: production hands ``DocumentPart(data=b"", size_bytes=0)`` (``UploadStore`` entry)."""
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.response = b""
        server.extra_headers = {"Content-Type": "text/plain"}
        part = (await client.hydrate(reference(memory, size_bytes=0))).content_part(None)
        assert type(part).__name__ == "DocumentPart"
        assert (part.data, part.size_bytes, part.mime_type) == (b"", 0, "text/plain")


@pytest.mark.parametrize("status", [401, 403, 409])
async def test_revoked_download_closes_shared_client_no_new_request(private_key, contracts, status):
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.status = status
        server.response = b"private"
        for _ in range(2):
            with pytest.raises(DooersThreadsContextError):
                await client.hydrate(reference(memory))
        assert len(server.requests) == 1


@pytest.mark.parametrize("status", [404, 410])
async def test_missing_file_is_shown_and_keeps_the_client_open(private_key, contracts, status):
    """M2: a file that is gone is production's ``UploadReferenceError``: visible text, never a silent close."""
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.status = status
        server.response = {"error": {"code": "NOT_FOUND", "message": "x", "retryable": False, "request_id": str(uuid4())}}
        for _ in range(2):
            with pytest.raises(DooersThreadsContextError) as raised:
                await client.hydrate(reference(memory))
            assert raised.value.code == "CONTEXT_UNAVAILABLE"
        assert not memory._client._closed
        assert len(server.requests) == 2


@pytest.mark.parametrize("transient", [429, 500, 502, 503, 504])
async def test_transient_download_failure_is_tried_again_within_the_execution(private_key, contracts, transient):
    """M2: 429 (16 downloads at the same time) or 5xx waits ``Retry-After`` and reads again."""
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        # The fixture appends the request before answering: the third one is served.
        served = lambda: len(server.requests) >= 3  # noqa: E731
        server.status = lambda _first: 200 if served() else transient
        server.response = lambda _first: b"hello" if served() else b'{"error":{}}'
        server.extra_headers = lambda _first: {"Content-Type": "text/plain"} if served() else {"Retry-After": "0"}
        started = asyncio.get_running_loop().time()
        assert (await client.hydrate(reference(memory, size_bytes=5))).data == b"hello"
        assert len(server.requests) == 3
        # Retry-After 0 waits the 0.25 s floor, twice.
        assert asyncio.get_running_loop().time() - started >= 0.5
        assert not memory._client._closed


async def test_transient_download_failure_stops_after_the_attempts_and_shows_the_failure(private_key, contracts):
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.status = 503
        server.response = b'{"error":{}}'
        server.extra_headers = {"Retry-After": "0"}
        with pytest.raises(DooersThreadsContextError) as raised:
            await client.hydrate(reference(memory))
        assert raised.value.code == "CONTEXT_UNAVAILABLE"
        assert len(server.requests) == DOWNLOAD_ATTEMPTS == 5
        assert not memory._client._closed


async def test_transient_download_failure_never_waits_past_the_execution_budget(private_key, contracts):
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.status = 429
        server.response = b'{"error":{}}'
        server.extra_headers = {"Retry-After": "5"}
        memory._client._check_current = lambda **_: 1.0
        started = asyncio.get_running_loop().time()
        with pytest.raises(DooersThreadsContextError) as raised:
            await client.hydrate(reference(memory))
        assert raised.value.code == "CONTEXT_UNAVAILABLE"
        assert len(server.requests) == 1
        assert asyncio.get_running_loop().time() - started < 1


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"Content-Type": "text/html"}, b"private"),
        ({"Content-Type": "text/plain", "Content-Encoding": "gzip"}, b"private"),
        ({"Content-Type": "text/plain", "Content-Length": "2048"}, b"private"),
    ],
)
async def test_limits_type_encoding_before_exposing_bytes(private_key, contracts, headers, body):
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.extra_headers = headers
        server.response = body
        with pytest.raises(DooersThreadsContextError):
            await client.hydrate(reference(memory))


async def test_timeout_cancel_and_finished_client_never_expose_bytes(private_key, contracts):
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        # F5a: ``artifact_timeout_seconds`` (1 s here) no longer cuts a download; the execution budget does.
        server.delay = 2
        server.response = b"hello"
        server.extra_headers = {"Content-Type": "text/plain"}
        assert (await client.hydrate(reference(memory))).data == b"hello"
        memory._client._check_current = lambda **_: 0.5
        with pytest.raises(DooersThreadsContextError) as raised:
            await client.hydrate(reference(memory))
        # The execution keeps its authority: the failure can still be shown in the thread (F5c).
        assert raised.value.code == "CONTEXT_UNAVAILABLE"
        assert not memory._client._closed
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        task = asyncio.create_task(client.hydrate(reference(memory)))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        memory._client.close()
        count = len(server.requests)
        with pytest.raises(DooersThreadsContextError):
            await client.hydrate(reference(memory))
        assert len(server.requests) == count


LINK = {"url": "https://dooers-threads.example/v1/artifact-links/token.signature", "expires_at": "2099-01-01T00:00:00Z"}


def with_links(data):
    data["capability_claims"]["operations"].append("artifact.link.issue")


async def test_handler_hydrates_attachment_without_rewriting_snapshot(private_key, contracts):
    async with fixture_server() as server:
        memory, page, *_ = prepare(private_key, contracts, server, change=with_links)
        media(memory)
        ref = reference(memory, size_bytes=5)
        part = {"type": "attachment", "artifact_id": ref.artifact_id, "filename": ref.filename, "mime_type": ref.mime_type, "size_bytes": 5}
        page["trigger_event"]["payload"]["content"] = [{"type": "text", "text": "Original"}, part]
        page["history"]["items"][0]["payload"]["content"] = page["trigger_event"]["payload"]["content"].copy()
        server.responses = [page, b"hello", page]
        server.extra_headers = {}
        # One endpoint-specific response header; a real fixture transport, no production auth claims.
        server.response = page
        # Test download in isolation above; incoming path uses patched typed port only to verify immutable mapping.
        from unittest.mock import patch

        from dooers.agents.server.dooers_threads.artifacts import DooersThreadsHydratedArtifact

        async def hydrate(self, reference, **kwargs):
            return DooersThreadsHydratedArtifact(reference.artifact_id, "fixture.txt", "text/plain", b"hello")

        server.responses = []
        server.response = lambda first: LINK if "/links " in first else page
        with patch.object(DooersThreadsArtifactsClient, "hydrate", hydrate):
            async with dooers_threads_handler_context(memory, private) as context:
                assert context.on.message == "Original"
                assert context.on.content[1].data == b"hello"
                # The part carries the temporary Dooers Threads link, as production carries its signed URL.
                assert context.on.content[1].url == LINK["url"]
                assert page["trigger_event"]["payload"]["content"] == [{"type": "text", "text": "Original"}, part]


async def test_repeated_identical_attachment_parts_remain_hydratable(private_key, contracts):
    from unittest.mock import patch

    from dooers.agents.server.dooers_threads.artifacts import DooersThreadsHydratedArtifact

    async with fixture_server() as server:
        memory, page, *_ = prepare_media(private_key, contracts, server)
        media(memory)
        ref = reference(memory, size_bytes=5)
        part = {"type": "attachment", "artifact_id": ref.artifact_id, "filename": ref.filename, "mime_type": ref.mime_type, "size_bytes": 5}
        page["trigger_event"]["payload"]["content"] = [part.copy(), part.copy()]
        page["history"]["items"][0]["payload"]["content"] = [part.copy(), part.copy()]
        server.response = page

        async def hydrate(self, current):
            assert current.artifact_id == ref.artifact_id
            return DooersThreadsHydratedArtifact(current.artifact_id, "fixture.txt", "text/plain", b"hello")

        with patch.object(DooersThreadsArtifactsClient, "hydrate", hydrate):
            result = await memory.hydrate_artifact(ref)
            assert result.data == b"hello"
            for field in ("filename", "mime_type", "size_bytes"):
                page["history"]["items"][0]["payload"]["content"][0].pop(field)
                result = await memory.hydrate_artifact(ref)
                assert result.data == b"hello"
            page["history"]["items"][0]["payload"]["content"][0]["filename"] = "divergent.txt"
            with pytest.raises(DooersThreadsContextError, match="CONTEXT_INVALID"):
                await memory.hydrate_artifact(ref)
