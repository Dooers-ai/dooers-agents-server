"""E09-06: real loopback network closure and execution isolation; no provider billing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from dooers.agents.server.maestro import MaestroCallbackError, MaestroTextExecutor
from tests.test_maestro_context import private
from tests.test_maestro_executor import setup


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    from dooers.agents.server.protocol.maestro import MaestroContracts

    return MaestroContracts()


@asynccontextmanager
async def hanging_provider():
    received, disconnected = asyncio.Event(), asyncio.Event()
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            received.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/model", received, disconnected
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


@pytest.mark.parametrize("failure", [403, 409])
async def test_authority_loss_cancels_actual_http_generation_and_keeps_committed_effect(private_key, contracts, failure):
    async with (
        setup(private_key, contracts, config_change={"heartbeat_seconds": 1}) as fixture,
        hanging_provider() as (url, received, disconnected),
    ):
        effects, contexts, ended = [], [], asyncio.Event()

        async def handler(on, send, *_):
            contexts.append(on.context.maestro)
            effects.append("already-committed-sandbox-effect")
            yield send.text("Already saved output")
            fixture.status["heartbeat"] = failure
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False) as network:
                    await network.get(url)
            finally:
                ended.set()

        task = asyncio.create_task(MaestroTextExecutor(fixture.config, handler, private).execute(fixture.admission, fixture.lease))
        await asyncio.wait_for(received.wait(), 5)
        with pytest.raises(MaestroCallbackError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(disconnected.wait(), 2)
        assert ended.is_set() and contexts[0].cancellation.cancelled
        assert effects == ["already-committed-sandbox-effect"] and len(fixture.outputs) == 1
        assert not fixture.completions
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "maestro-heartbeat"]


async def test_cancel_a_preserves_b_and_closing_http_provider_is_failure_not_cancel(private_key, contracts):
    async with (
        setup(private_key, contracts, config_change={"heartbeat_seconds": 1}) as a,
        setup(private_key, contracts, config_change={"heartbeat_seconds": 1}) as b,
    ):
        b_started, release_b, a_ended = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler_a(*_):
            a.status["heartbeat"] = 409
            try:
                await asyncio.sleep(10)
            finally:
                a_ended.set()

        async def handler_b(_on, send, *_):
            b_started.set()
            await release_b.wait()
            yield send.text("B completed")

        ta = asyncio.create_task(MaestroTextExecutor(a.config, handler_a, private).execute(a.admission, a.lease))
        tb = asyncio.create_task(MaestroTextExecutor(b.config, handler_b, private).execute(b.admission, b.lease))
        try:
            await asyncio.wait_for(b_started.wait(), 5)
            with pytest.raises(MaestroCallbackError):
                await asyncio.wait_for(ta, 5)
            assert a_ended.is_set() and not tb.done()
            release_b.set()
            result = await asyncio.wait_for(tb, 5)
            assert result.response["state"] == "responded" and len(b.outputs) == 1
        finally:
            for task in (ta, tb):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ta, tb, return_exceptions=True)
    async with setup(private_key, contracts) as fixture:

        async def disconnected_provider(*_):
            raise httpx.RemoteProtocolError("provider closed")

        result = await MaestroTextExecutor(fixture.config, disconnected_provider, private).execute(fixture.admission, fixture.lease)
        assert result.response["state"] == "failed"
        assert fixture.completions[-1]["outcome"] == "failed"
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "maestro-heartbeat"]
