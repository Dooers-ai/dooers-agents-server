"""E09-03: SDK real + callbacks TCP sintéticos; ingresso Maestro é E09-02."""

import asyncio
import time

import pytest

from dooers.agents.server.maestro import MaestroCallbackClient, MaestroCallbackError, MaestroEventSink, MaestroSend
from tests import test_maestro_executor as fixtures
from tests import test_maestro_journal as journal_fixtures

private_journal = journal_fixtures.private_journal
private_key = fixtures.private_key
contracts = fixtures.contracts
setup = fixtures.setup
execute_fixture = fixtures.execute_fixture
ENABLED = {"product_capabilities": ("stream.delta",)}


async def test_stream_and_complete_messages_have_one_ordinal_space(private_key, contracts):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        f.drop_output = 1
        handles = []

        async def handler(on, send, *_):
            a = send.open_text()
            handles.append(a)
            await a.delta("Olá ")
            yield send.text("independente")
            await a.delta("🌍")
            result = await a.finish()
            assert await a.finish() == result
            with pytest.raises(MaestroCallbackError):
                await a.delta("late")

        response = await execute_fixture(f, handler)
        assert response.status_code == 200 and response.json()["state"] == "responded"
        assert len(f.outputs) == 2
        values = {v[0]["output_id"]: v[0]["payload"]["content"][0]["text"] for v in f.outputs.values()}
        assert values == {"output:0": "Olá 🌍", "output:1": "independente"}
        chunks = [r[2] for r in f.requests if r[0] == "stream"]
        assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
        assert "".join(c["delta"] for c in chunks) == "Olá 🌍"
        assert all(c["lease_fence"] == f.envelope["lease_fence"] for c in chunks)
        assert handles[0]._text == handles[0]._pending == ""


@pytest.mark.parametrize("capabilities", [(), ("stream.delta",)])
async def test_missing_capability_or_remote_unsupported_never_succeeds(private_key, contracts, capabilities):
    async with setup(private_key, contracts, config_change={"product_capabilities": capabilities}) as f:
        f.status["stream"] = 422

        async def handler(on, send, *_):
            stream = send.open_text()
            await stream.delta("no")
            await stream.finish()

        result = await execute_fixture(f, handler)
        assert result.json()["state"] == "failed"
        assert not f.outputs
        assert sum(r[0] == "stream" for r in f.requests) == bool(capabilities)


async def test_unicode_split_continuous_flush_and_shared_rate(private_key, contracts):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        client = MaestroCallbackClient(f.config, f.admission, f.lease, contracts=contracts)
        sink = MaestroEventSink(client)
        original = client.stream
        sent = []

        async def record(*args):
            sent.append(time.monotonic())
            return await original(*args)

        client.stream = record
        a, b = sink.open_text(), sink.open_text()
        await a.delta("🌍" * 5000)
        await b.delta("é" * 8500)
        await asyncio.sleep(0.055)
        await a.delta("x")
        assert len(sent) >= 3  # Progress before finish, while producing.
        await b.finish()
        await a.finish()
        chunks = [r[2] for r in f.requests if r[0] == "stream"]
        assert all(len(c["delta"].encode()) <= 16384 for c in chunks)
        assert all(len(c["delta"].encode("utf-16-le")) // 2 <= 16384 for c in chunks)
        assert all(y - x >= 0.049 for x, y in zip(sent, sent[1:]))
        assert sink.normal_outcome()[0] == "responded"
        sink.close()
        client.close()


@pytest.mark.parametrize("status", [401, 403, 409, 410])
async def test_authority_loss_never_manufactures_final(private_key, contracts, status):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        f.status["stream"] = status

        async def handler(on, send, *_):
            a = send.open_text()
            await a.delta("partial")
            await a.finish()

        response = await execute_fixture(f, handler)
        assert response.status_code != 200
        assert not f.outputs and not f.completions


async def test_unknown_chunk_discards_preview_but_final_is_integral(private_key, contracts):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        f.status["stream"] = 503

        async def handler(on, send, *_):
            a = send.open_text()
            await a.delta("lost")
            await a.delta(" but complete")
            await a.finish()

        assert (await execute_fixture(f, handler)).json()["state"] == "responded"
        assert sum(r[0] == "stream" for r in f.requests) == 1
        assert next(iter(f.outputs.values()))[0]["payload"]["content"][0]["text"] == "lost but complete"


async def test_finish_lost_ack_retries_exact_bytes_and_key(private_key, contracts):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        f.drop_output = 2
        client = MaestroCallbackClient(f.config, f.admission, f.lease, contracts=contracts)
        sink = MaestroEventSink(client)
        stream = sink.open_text()
        await stream.delta("once")
        with pytest.raises(MaestroCallbackError) as error:
            await stream.finish()
        assert error.value.unknown
        with pytest.raises(MaestroCallbackError):
            await stream.delta("cannot change frozen intent")
        await stream.finish()
        requests = [r for r in f.requests if r[0] == "outputs"]
        assert len(requests) == 3 and len(f.outputs) == 1
        assert len({r[1]["idempotency-key"] for r in requests}) == len({r[3] for r in requests}) == 1
        assert requests[0][1]["idempotency-key"] == f"output:{f.envelope['delivery_id']}:{f.envelope['phase_id']}:0"
        sink.close()
        client.close()


async def test_cancel_pending_flush_closes_all_resources(private_key, contracts):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        entered = asyncio.Event()
        streams = []

        async def handler(on, send, *_):
            a = send.open_text()
            streams.append(a)
            await a.delta("partial")
            entered.set()
            await asyncio.Future()

        task = asyncio.create_task(execute_fixture(f, handler))
        await asyncio.wait_for(entered.wait(), 20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(s.closed and not s._text and not s._pending for s in streams)
        assert not f.outputs and not f.completions
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "maestro-heartbeat"]


async def test_bounds_and_unfinished_output_fail_closed(private_key, contracts):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        client = MaestroCallbackClient(f.config, f.admission, f.lease, contracts=contracts)
        sink = MaestroEventSink(client)
        streams = [sink.open_text() for _ in range(16)]
        with pytest.raises(MaestroCallbackError):
            sink.open_text()
        for value in ("", "\ud800", "x" * 65537):
            with pytest.raises(MaestroCallbackError):
                await streams[0].delta(value)
        with pytest.raises(MaestroCallbackError):
            sink.normal_outcome()
        with pytest.raises(MaestroCallbackError):
            await streams[0].finish()
        with pytest.raises(MaestroCallbackError):
            await asyncio.create_task(streams[0].delta("wrong task"))
        sink.close()
        client.close()
        assert not f.outputs


async def test_send_default_stays_complete_and_standalone_stream_is_denied():
    send = MaestroSend()
    assert send.text("complete").data == {"text": "complete", "author": None}
    assert send.whatsapp is not None
    with pytest.raises(MaestroCallbackError):
        send.open_text()


async def test_chunk_budget_and_aggregate_memory_are_bounded(private_key, contracts, monkeypatch):
    from types import SimpleNamespace

    from dooers.agents.server.maestro import streaming

    async with setup(private_key, contracts, config_change=ENABLED) as f:
        client = MaestroCallbackClient(f.config, f.admission, f.lease, contracts=contracts)
        sink = MaestroEventSink(client)
        indices = []
        clock = [1.0]

        def monotonic():
            clock[0] += 0.1
            return clock[0]

        async def accepted(output_id, index, delta):
            indices.append(index)
            return {"status": "accepted", "ephemeral": True, "output_id": output_id, "chunk_index": index}

        client.stream = accepted
        monkeypatch.setattr(streaming, "time", SimpleNamespace(monotonic=monotonic))
        a = sink.open_text()
        for _ in range(260):
            await a.delta("x")
        assert indices == list(range(256))
        assert not a._preview and not a._pending
        await a.finish()
        assert next(iter(f.outputs.values()))[0]["payload"]["content"][0]["text"] == "x" * 260
        for _ in range(4):
            b = sink.open_text()
            b._preview = False
            await b.delta("y" * 65536)
        c = sink.open_text()
        with pytest.raises(MaestroCallbackError) as error:
            await c.delta("z")
        assert error.value.code == "QUOTA_EXCEEDED"
        assert sum(s._bytes for s in sink._streams) == 262144
        sink.close()
        assert sum(s._bytes for s in sink._streams) == 0
        client.close()


async def test_missing_respond_operation_cannot_open(private_key, contracts):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        client = MaestroCallbackClient(f.config, f.admission, f.lease, contracts=contracts)
        client._claims = {**client._claims, "operations": ["delivery.heartbeat"]}
        sink = MaestroEventSink(client)
        with pytest.raises(MaestroCallbackError) as error:
            sink.open_text()
        assert error.value.code == "UNSUPPORTED_CAPABILITY"
        sink.close()
        client.close()


@pytest.mark.parametrize("private_journal", ["sdk"], indirect=True)
async def test_incremental_reuses_private_effect_journal(private_journal, private_key, contracts):
    from uuid import uuid4

    from dooers.agents.server.maestro import create_durable_text_delivery_adapter
    from dooers.agents.server.maestro.journal_runtime import MaestroEffectTool, run_maestro_effect
    from dooers.agents.server.maestro.journal_store import canonical
    from dooers.agents.server.protocol.maestro.journal import PrivateEffectResult
    from tests.test_maestro_admission import json_bytes, request
    from tests.test_maestro_context import private

    calls = []

    async def provider(arguments, key):
        calls.append(key)
        return PrivateEffectResult(canonical({"receipt": "once"}), canonical({"id": str(uuid4())}))

    tool = MaestroEffectTool("sandbox", "1", "synthetic", "non_reconcilable", lambda value: value, provider)

    async def handler(on, send, *_):
        await run_maestro_effect(tool, ordinal=0, arguments={"units": 1})
        output = send.open_text()
        await output.delta("journal ")
        await output.delta("once")
        await output.finish()

    async with setup(private_key, contracts, config_change=ENABLED) as f:
        f.drop_output = 1
        adapter = create_durable_text_delivery_adapter(
            f.config, handler, private, journal=private_journal["store"], retry_policy="journal_only"
        )
        assert (await request(adapter, json_bytes(f.envelope), f.headers)).status_code == 200
        assert len(calls) == len(f.outputs) == 1
        assert (await private_journal["db"].fetchrow("SELECT state FROM maestro_journal.phases"))["state"] == "confirmed"
        recreated = create_durable_text_delivery_adapter(
            f.config, handler, private, journal=private_journal["store"], retry_policy="journal_only"
        )
        assert (await request(recreated, json_bytes(f.envelope), f.headers)).status_code == 503
        assert len(calls) == len(f.outputs) == 1


@pytest.mark.parametrize("behavior", ["abandoned", "catch_unsupported", "catch_stale"])
async def test_handler_cannot_turn_unfinished_or_rejected_stream_into_success(private_key, contracts, behavior):
    async with setup(private_key, contracts, config_change=ENABLED) as f:
        if behavior != "abandoned":
            f.status["stream"] = 422 if behavior == "catch_unsupported" else 409

        async def handler(on, send, *_):
            stream = send.open_text()
            try:
                await stream.delta("partial")
            except MaestroCallbackError:
                with pytest.raises(MaestroCallbackError):
                    await stream.finish()
            # Return normally after partial or caught rejection.

        result = await execute_fixture(f, handler)
        assert not f.outputs
        if behavior == "catch_stale":
            assert result.status_code != 200 and not f.completions
        else:
            assert result.status_code == 200 and result.json()["state"] == "failed"
            assert f.completions[-1]["reason_code"] == "handler_contract_error"
