from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from dooers.agents.server.maestro.artifacts import MaestroArtifactsClient
from dooers.agents.server.maestro.client import MaestroContextError
from dooers.agents.server.maestro.context import maestro_handler_context
from dooers.agents.server.maestro.memory import MaestroArtifactReference
from tests.test_maestro_context import contracts as contracts
from tests.test_maestro_context import fixture_server, prepare, private
from tests.test_maestro_context import private_key as private_key


def media(memory, *, maximum=1024, timeout=1):
    memory._client.config = replace(
        memory._client.config, artifact_mime_types=("text/plain",), max_artifact_bytes=maximum, artifact_timeout_seconds=timeout
    )
    # Authenticated signed fixture already contains the implemented artifact operations.
    return MaestroArtifactsClient(memory._client)


def prepare_media(key, contracts, server):
    return prepare(key, contracts, server)


def reference(memory, **extra):
    e = memory._envelope
    return MaestroArtifactReference(
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
            assert result.data == b"hello" and result.content_part().url is None
        assert len(requests) == 2 and all("artifact" not in req.scopes for req in requests)
        for first, headers in server.requests:
            assert first == f"GET /api/v1/artifacts/{ref.artifact_id}/download HTTP/1.1"
            assert headers["Authorization"] == f"Bearer {token}"
            assert headers["Maestro-Execution-Capability"] == memory._client._capability


@pytest.mark.parametrize("status", [401, 403, 404, 409, 410])
async def test_revoked_download_closes_shared_client_no_new_request(private_key, contracts, status):
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.status = status
        server.response = b"private"
        for _ in range(2):
            with pytest.raises(MaestroContextError):
                await client.hydrate(reference(memory))
        assert len(server.requests) == 1


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
        with pytest.raises(MaestroContextError):
            await client.hydrate(reference(memory))


async def test_timeout_cancel_and_finished_client_never_expose_bytes(private_key, contracts):
    async with fixture_server() as server:
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        server.delay = 2
        server.response = b"hello"
        with pytest.raises(MaestroContextError):
            await client.hydrate(reference(memory))
        assert memory._client._closed
        memory, *_ = prepare_media(private_key, contracts, server)
        client = media(memory)
        task = asyncio.create_task(client.hydrate(reference(memory)))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        memory._client.close()
        count = len(server.requests)
        with pytest.raises(MaestroContextError):
            await client.hydrate(reference(memory))
        assert len(server.requests) == count


async def test_handler_hydrates_attachment_without_rewriting_snapshot(private_key, contracts):
    async with fixture_server() as server:
        memory, page, *_ = prepare_media(private_key, contracts, server)
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

        from dooers.agents.server.maestro.artifacts import MaestroHydratedArtifact

        async def hydrate(self, reference, **kwargs):
            return MaestroHydratedArtifact(reference.artifact_id, "fixture.txt", "text/plain", b"hello")

        server.responses = []
        with patch.object(MaestroArtifactsClient, "hydrate", hydrate):
            async with maestro_handler_context(memory, private) as context:
                assert context.on.message == "Original"
                assert context.on.content[1].data == b"hello"
                assert context.on.content[1].url is None
                assert page["trigger_event"]["payload"]["content"] == [{"type": "text", "text": "Original"}, part]


async def test_repeated_identical_attachment_parts_remain_hydratable(private_key, contracts):
    from unittest.mock import patch

    from dooers.agents.server.maestro.artifacts import MaestroHydratedArtifact

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
            return MaestroHydratedArtifact(current.artifact_id, "fixture.txt", "text/plain", b"hello")

        with patch.object(MaestroArtifactsClient, "hydrate", hydrate):
            result = await memory.hydrate_artifact(ref)
            assert result.data == b"hello"
            for field in ("filename", "mime_type", "size_bytes"):
                page["history"]["items"][0]["payload"]["content"][0].pop(field)
                result = await memory.hydrate_artifact(ref)
                assert result.data == b"hello"
            page["history"]["items"][0]["payload"]["content"][0]["filename"] = "divergent.txt"
            with pytest.raises(MaestroContextError, match="CONTEXT_INVALID"):
                await memory.hydrate_artifact(ref)
