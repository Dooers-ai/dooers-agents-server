"""E11-05: unit guards + actual private PostgreSQL; callbacks below are fixtures."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.maestro import (
    MaestroCheckpoint,
    MaestroCheckpointStore,
    MaestroFormSuspension,
    MaestroSend,
    create_durable_text_delivery_adapter,
)
from dooers.agents.server.maestro.adapter import MaestroConfirmedResult
from dooers.agents.server.maestro.callbacks import MaestroCallbackClient, MaestroCallbackError
from dooers.agents.server.maestro.journal_store import JournalError
from dooers.agents.server.protocol.maestro.journal import ExecutionAuthority
from tests import test_maestro_executor as ef
from tests import test_maestro_journal as jf
from tests.test_maestro_admission import json_bytes, request
from tests.test_maestro_context import private

private_journal = jf.private_journal
private_key = ef.private_key
contracts = ef.contracts


def step(value):
    if value != {"step": "await_name"}:
        raise ValueError
    return value


def checkpoint():
    return MaestroCheckpoint("demo.step", 1, {"step": "await_name"})


async def prepared(h):
    await h["db"].execute((Path(__file__).parent / "fixtures/maestro_checkpoint_extension.sql").read_text())
    claim = await h["invocation"]()
    scope = h["scope"]
    e = {
        "organization_id": scope.organization_id,
        "workspace_id": scope.workspace_id,
        "thread_id": scope.thread_id,
        "recipient_participant_id": scope.recipient_participant_id,
        "instance_agent_id": scope.instance_agent_id,
        "delivery_id": h["delivery"],
        "phase_id": "initial",
        "execution_id": h["authority"].execution_id,
        "lease_fence": "1",
        "delivery_expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
    }
    execution = SimpleNamespace(
        active=True,
        claim=claim,
        store=h["store"],
        scope=scope,
        delivery=h["delivery"],
        phase="initial",
        authority=h["authority"],
        admission=SimpleNamespace(envelope=e),
    )
    return MaestroCheckpointStore(h["store"], backend_id="local_journal", schemas={"demo.step": (1, step)}), execution


async def test_same_journal_checkpoint_is_immutable_stable_and_does_not_change_effect_counts(private_journal):
    h = private_journal
    store, execution = await prepared(h)
    first = await store.save(execution, checkpoint(), 0)
    assert await store.save(execution, checkpoint(), 0) == first
    assert (await h["db"].fetchrow("SELECT count(*) AS n FROM maestro_journal.checkpoints"))["n"] == 1
    assert (await h["db"].fetchrow("SELECT effect_count,invocation_count FROM maestro_journal.authority")) == {
        "effect_count": 0,
        "invocation_count": 1,
    }
    with pytest.raises(Exception):
        await h["db"].execute("UPDATE maestro_journal.checkpoints SET state='{}'")
    execution.claim = replace(execution.claim, maestro_fence="2")
    with pytest.raises(JournalError, match="JOURNAL_STALE_WRITER"):
        await store.save(execution, checkpoint(), 0)


@pytest.mark.parametrize(
    "bad",
    [
        MaestroCheckpoint("unknown", 1, {}),
        MaestroCheckpoint("demo.step", 2, {}),
        MaestroCheckpoint("demo.step", 1, {"step": "wrong"}),
        MaestroCheckpoint("demo.step", 1, {"step": "await_name", "secret": "x"}),
        MaestroCheckpoint("demo.step", 1, {"number": 1.0}),
    ],
)
async def test_schema_or_version_or_private_state_fails_before_checkpoint_write(private_journal, bad):
    h = private_journal
    store, execution = await prepared(h)
    with pytest.raises(JournalError):
        await store.save(execution, bad, 0)
    assert (await h["db"].fetchrow("SELECT count(*) AS n FROM maestro_journal.checkpoints"))["n"] == 0


@pytest.mark.parametrize("change", ["backend", "hash", "origin", "retention", "generation", "integrity", "stale_fence"])
async def test_resume_requires_exact_source_and_current_shared_backend(private_journal, change):
    h = private_journal
    store, execution = await prepared(h)
    d = await store.save(execution, checkpoint(), 0)
    e = deepcopy(execution.admission.envelope)
    phase = str(uuid4())
    authority = ExecutionAuthority(str(uuid4()), str(uuid4()), "2", h["authority"].deadline_at)
    claim = await h["store"].reserve_invocation(h["scope"], h["delivery"], phase, authority, context_fingerprint=h["fingerprint"])
    await h["store"].start_invocation(claim, authority)
    e.update(phase_id=phase, execution_id=authority.execution_id, lease_fence="2")
    e["resume"] = {
        "interaction_id": phase,
        "kind": "form",
        "resume_of_execution_id": d["source_execution_id"],
        "original_trigger_event_id": str(uuid4()),
        "response_event_id": str(uuid4()),
        "response_sequence": "2",
        "checkpoint_ref": d["checkpoint_ref"],
    }
    private_context = {"resume": e["resume"], "continuity": deepcopy(d), "response_data": {"name": "private", "number": 1.25}}

    class Client:
        async def interaction_context(self):
            return deepcopy(private_context)

        def _check_current(self):
            return None

    resumed = SimpleNamespace(
        active=True,
        claim=claim,
        store=h["store"],
        scope=h["scope"],
        delivery=h["delivery"],
        phase=phase,
        authority=authority,
        admission=SimpleNamespace(envelope=e),
        client=Client(),
    )
    assert await store.load(resumed, private_context) == {"step": "await_name"}
    if change == "backend":
        private_context["continuity"]["backend_id"] = "different"
    elif change == "hash":
        private_context["continuity"]["content_sha256"] = "0" * 64
    elif change == "origin":
        private_context["continuity"]["source_execution_id"] = str(uuid4())
    elif change == "retention":
        private_context["continuity"]["retain_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "generation":
        h["store"].generation = str(uuid4())
    elif change == "integrity":
        h["store"].integrity_verified = False
    else:
        resumed.claim = replace(claim, maestro_fence="3")
    with pytest.raises(JournalError):
        await store.load(resumed, private_context)


def test_legacy_form_constructor_stays_exact_and_maestro_requires_explicit_suspend():
    data = {
        "message": "Title",
        "elements": [{"type": "text", "name": "name"}],
        "submit_label": "Send",
        "cancel_label": "Cancel",
        "size": "medium",
    }
    event = AgentSend().form(data["message"], data["elements"])
    assert event.send_type == "form" and event.data == data
    with pytest.raises(MaestroCallbackError, match="UNSUPPORTED_CAPABILITY"):
        MaestroSend().form("Title", [])


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("private_journal", ["sdk"], indirect=True)
async def test_generator_stops_after_suspend_even_if_ack_unknown(private_journal, private_key, contracts, monkeypatch, unknown):
    h = private_journal
    await h["db"].execute((Path(__file__).parent / "fixtures/maestro_checkpoint_extension.sql").read_text())
    checkpoints = MaestroCheckpointStore(h["store"], backend_id="local_journal", schemas={"demo.step": (1, step)})
    after = []
    finally_blocked = []
    suspends = []

    async def suspend(client, interaction, ref, ordinal):
        suspends.append((interaction, ref, ordinal))
        if unknown:
            raise MaestroCallbackError(unknown=True)
        return MaestroConfirmedResult(
            {
                "delivery_id": client._envelope["delivery_id"],
                "execution_id": client._envelope["execution_id"],
                "state": "waiting_input",
                "result_event_ids": [str(uuid4())],
            },
            True,
        )

    monkeypatch.setattr(MaestroCallbackClient, "suspend", suspend)
    async with ef.setup(private_key, contracts) as fixture:

        async def handler(on, send, *args):
            try:
                yield send.form(
                    "Title",
                    [{"name": "name", "label": "Name", "type": "text", "required": True, "share_response": False, "max_length": 80}],
                    suspension=MaestroFormSuspension(
                        checkpoint(),
                        (datetime.now(UTC) + timedelta(minutes=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        (str(uuid4()),),
                    ),
                )
                after.append(True)
                yield send.text("FORBIDDEN")
            finally:
                import asyncio

                from dooers.agents.server.maestro.journal_runtime import _bound

                finally_blocked.append(not _bound[asyncio.current_task()].active)

        adapter = create_durable_text_delivery_adapter(fixture.config, handler, private, journal=h["store"], checkpoints=checkpoints)
        response = await request(adapter, json_bytes(fixture.envelope), fixture.headers)
        assert response.status_code == (503 if unknown else 200)
        assert after == [] and finally_blocked == [True] and len(suspends) == 1
        assert fixture.completions == [] and fixture.outputs == {}


@pytest.mark.parametrize("expiry", ["lease", "credential"])
async def test_private_context_rechecks_authority_after_slow_validation(private_key, contracts, monkeypatch, expiry):
    import time

    import httpx
    import respx

    from dooers.agents.server.maestro import interactions
    from dooers.agents.server.maestro.adapter import MaestroCurrentLease
    from dooers.agents.server.maestro.client import MaestroContextError

    async with ef.setup(private_key, contracts) as fixture:
        client = MaestroCallbackClient(fixture.config, fixture.admission, fixture.lease)
        e = client._envelope
        phase = str(uuid4())
        e["phase_id"] = phase
        e["context_cutoff_sequence"] = "2"
        resume = {
            "interaction_id": phase,
            "kind": "form",
            "resume_of_execution_id": str(uuid4()),
            "original_trigger_event_id": e["trigger_event_id"],
            "response_event_id": str(uuid4()),
            "response_sequence": "2",
            "checkpoint_ref": f"checkpoint:v1:{uuid4()}",
        }
        e["resume"] = resume
        descriptor = {
            "format_version": "maestro.continuity.local.v1",
            "checkpoint_ref": resume["checkpoint_ref"],
            "checkpoint_schema": "demo.step",
            "checkpoint_version": 1,
            "backend_id": "local_journal",
            "content_sha256": "0" * 64,
            **{key: e[key] for key in ["organization_id", "workspace_id", "thread_id", "delivery_id", "instance_agent_id"]},
            "source_phase_id": "initial",
            "source_execution_id": resume["resume_of_execution_id"],
            "agent_participant_id": e["recipient_participant_id"],
            "retain_until": e["delivery_expires_at"],
        }
        dto = {
            "format_version": "maestro.interaction-context.local.v1",
            "resume": resume,
            "continuity": descriptor,
            "form_event_id": str(uuid4()),
            "response_data": {"number": 1.25},
        }
        original = interactions.validate_private_context

        def slow(value, envelope):
            result = original(value, envelope)
            time.sleep(0.08)
            return result

        monkeypatch.setattr(interactions, "validate_private_context", slow)
        if expiry == "lease":
            # Millisecond precision deliberately avoids timestamp helper's integer rounding.
            until = (datetime.now(UTC) + timedelta(seconds=0.04)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            client._lease = MaestroCurrentLease(e["execution_id"], e["lease_fence"], until)
        else:
            original_credential = client._credential

            async def short(*args):
                return replace(await original_credential(*args), expires_at=time.time() + 0.04)

            monkeypatch.setattr(client, "_credential", short)
        with respx.mock:
            route = respx.get(f"{fixture.url}/deliveries/{e['delivery_id']}/interaction-context").mock(
                return_value=httpx.Response(200, json=dto)
            )
            with pytest.raises(MaestroContextError):
                await client.interaction_context()
            assert route.call_count == 1


def test_checkpoint_canonical_bytes_match_javascript_utf16_and_integer_key_enumeration():
    from dooers.agents.server.maestro.interactions import checkpoint_bytes

    value = {"\uffff": 1, "😀": 2, "10": 3, "2": 4, "nested": {"10": 1, "2": 2}}
    assert checkpoint_bytes(value).decode() == '{"2":4,"10":3,"nested":{"2":2,"10":1},"😀":2,"\uffff":1}'
