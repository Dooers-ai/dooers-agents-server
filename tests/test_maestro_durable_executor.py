"""Executor instalado em adapters distintos + autoridade privada PostgreSQL real.

Callbacks usam servidor HTTP canônico de fixtures E05-08; core real é a camada C2.
"""

import asyncio
from uuid import uuid4

import pytest

from dooers.agents.server.maestro import create_durable_text_delivery_adapter
from dooers.agents.server.maestro.journal_runtime import MaestroEffectTool, run_maestro_effect
from dooers.agents.server.maestro.journal_store import canonical
from dooers.agents.server.protocol.maestro.journal import PrivateEffectResult
from tests import test_maestro_executor as executor_fixtures
from tests import test_maestro_journal as journal_fixtures
from tests.test_maestro_admission import json_bytes, request
from tests.test_maestro_context import private

contracts = executor_fixtures.contracts
private_key = executor_fixtures.private_key
private_journal = journal_fixtures.private_journal
setup = executor_fixtures.setup


@pytest.mark.parametrize("private_journal", ["sdk"], indirect=True)
async def test_two_adapters_share_one_handler_and_one_provider_call(private_journal, private_key, contracts):
    h = private_journal
    handler_calls = 0
    provider_calls = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(args, key):
        nonlocal provider_calls
        provider_calls += 1
        return PrivateEffectResult(canonical({"receipt": "private-success"}), canonical({"id": str(uuid4())}))

    tool = MaestroEffectTool("sandbox", "1", "synthetic", "non_reconcilable", lambda value: value, provider)

    async def handler(on, send, memory, analytics, settings):
        nonlocal handler_calls
        handler_calls += 1
        value = await run_maestro_effect(tool, ordinal=0, arguments={"units": 1})
        assert value == {"receipt": "private-success"}
        entered.set()
        await release.wait()
        yield send.text("Resposta persistida uma vez")

    async with setup(private_key, contracts) as fixture:
        a = create_durable_text_delivery_adapter(fixture.config, handler, private, journal=h["store"], retry_policy="journal_only")
        b = create_durable_text_delivery_adapter(fixture.config, handler, private, journal=h["store"], retry_policy="journal_only")
        first = asyncio.create_task(request(a, json_bytes(fixture.envelope), fixture.headers))
        await asyncio.wait_for(entered.wait(), 10)
        duplicate = await request(b, json_bytes(fixture.envelope), fixture.headers)
        assert duplicate.status_code == 503
        release.set()
        assert (await first).status_code == 200
        assert handler_calls == provider_calls == 1
        assert len(fixture.outputs) == len(fixture.completions) == 1
        assert (await h["db"].fetchrow("SELECT state FROM maestro_journal.phases"))["state"] == "confirmed"
        # A third adapter after process-equivalent recreation does not repeat either action.
        c = create_durable_text_delivery_adapter(fixture.config, handler, private, journal=h["store"], retry_policy="journal_only")
        assert (await request(c, json_bytes(fixture.envelope), fixture.headers)).status_code == 503
        assert handler_calls == provider_calls == 1


@pytest.mark.parametrize("private_journal", ["sdk"], indirect=True)
async def test_journal_unavailable_or_unverified_never_calls_handler(private_journal, private_key, contracts):
    h = private_journal

    async def forbidden(*args):
        raise AssertionError("handler must not execute")

    async with setup(private_key, contracts) as fixture:
        h["store"].integrity_verified = False
        adapter = create_durable_text_delivery_adapter(fixture.config, forbidden, private, journal=h["store"])
        assert (await request(adapter, json_bytes(fixture.envelope), fixture.headers)).status_code == 503
        assert fixture.outputs == {} and fixture.completions == []


@pytest.mark.parametrize("private_journal", ["sdk"], indirect=True)
async def test_unknown_provider_completes_manual_review_without_repeating(private_journal, private_key, contracts):
    calls = 0

    async def provider(args, key):
        nonlocal calls
        calls += 1
        raise ConnectionError("sandbox applied but its response was lost")

    tool = MaestroEffectTool("sandbox", "1", "synthetic", "non_reconcilable", lambda value: value, provider)

    async def handler(on, send, memory, analytics, settings):
        await run_maestro_effect(tool, ordinal=0, arguments={"units": 1})
        yield send.text("Must not publish")

    async with setup(private_key, contracts) as fixture:
        adapter = create_durable_text_delivery_adapter(
            fixture.config, handler, private, journal=private_journal["store"], retry_policy="journal_only"
        )
        response = await request(adapter, json_bytes(fixture.envelope), fixture.headers)
        assert response.status_code == 200 and response.json()["state"] == "failed"
        assert calls == 1 and len(fixture.completions) == 1
        assert fixture.completions[0]["reason_code"] == "manual_review"
        assert fixture.outputs == {}
