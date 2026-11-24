"""Agent output size limit and callback response caps (Dooers Threads raised the agent message limit to 1 MiB)."""

from __future__ import annotations

import json

import pytest

from dooers.agents.server.dooers_threads import DooersThreadsConfigurationError
from dooers.agents.server.dooers_threads.callbacks import DooersThreadsCallbackClient, DooersThreadsCallbackError
from dooers.agents.server.dooers_threads.sink import DooersThreadsEventSink, DooersThreadsSend
from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.protocol.dooers_threads import default_contracts
from tests.test_dooers_threads_executor import execute_fixture, setup

KIB = 1024


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return default_contracts()


def test_default_output_limit_is_one_mib_and_configurable(private_key, contracts):
    from dataclasses import replace

    from tests.test_dooers_threads_admission import config_for

    config = config_for(private_key)
    assert config.max_output_bytes == 1048576
    assert replace(config, max_output_bytes=1024).max_output_bytes == 1024
    for invalid in (0, 1048577):
        with pytest.raises(DooersThreadsConfigurationError, match="DOOERS_THREADS_LIMIT_INVALID"):
            replace(config, max_output_bytes=invalid)


async def test_900_kib_text_passes_the_local_check_and_is_published(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        text = "a" * (900 * KIB)

        async def handler(_on, send, *_args):
            yield send.text(text)

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "responded", response.text
        (output,) = [semantic for semantic, _ in fixture.outputs.values()]
        assert output["payload"]["content"] == [{"type": "text", "text": text}]


async def _accept(fixture, contracts, event):
    """One event through a fresh sink (a fresh sink restarts at ``output:0``)."""
    client = DooersThreadsCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
    try:
        await DooersThreadsEventSink(client).accept(event)
    finally:
        client.close()


async def test_1_1_mib_text_fails_locally_before_sending(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        with pytest.raises(DooersThreadsCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, AgentSend().text("a" * int(1.1 * 1024 * KIB)))
        assert not any(action == "outputs" for action, *_ in fixture.requests)


async def test_limit_counts_utf8_bytes_not_characters(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        # 600k characters of 2 bytes each: 1.2 MB of UTF-8, 600k characters.
        with pytest.raises(DooersThreadsCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, AgentSend().text("é" * 600_000))
        assert not fixture.requests or all(action != "outputs" for action, *_ in fixture.requests)


async def test_limit_is_the_configured_one(private_key, contracts):
    async with setup(private_key, contracts, config_change={"max_output_bytes": 2048}) as fixture:
        await _accept(fixture, contracts, AgentSend().text("a" * 2048))
        with pytest.raises(DooersThreadsCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, AgentSend().text("a" * 2049))


async def test_contact_and_template_json_count_like_dooers_threads(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        send = AgentSend()
        # Dooers Threads counts the JSON of the contact part and of the template next to the text.
        big = "x" * int(1.1 * 1024 * KIB)
        with pytest.raises(DooersThreadsCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, send.contact(display_name="Ana", vcard=big))
        with pytest.raises(DooersThreadsCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, send.template("t", "pt_BR", template_components=[{"type": "body", "text": big}]))
        assert all(action != "outputs" for action, *_ in fixture.requests)


@pytest.mark.parametrize("shape", ["contact", "template"])
async def test_contact_and_template_below_the_limit_are_published(private_key, contracts, shape):
    async with setup(private_key, contracts) as fixture:
        small = "x" * (500 * KIB)
        send = AgentSend()
        # The contract caps a vcard at 64 Ki characters (only agent text part is larger), so the contact stays below it.
        event = (
            send.contact(display_name="Ana", vcard="x" * (60 * KIB))
            if shape == "contact"
            else send.template("t", "pt_BR", template_components=[{"type": "body", "text": small}])
        )
        await _accept(fixture, contracts, event)
        assert len(fixture.outputs) == 1


async def test_text_just_over_the_limit_with_json_escapes_is_counted_in_bytes_not_json(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        # 900 KiB of quotes is 1.8 MiB of JSON, but only 900 KiB of text for Dooers Threads: it is published.
        await _accept(fixture, contracts, AgentSend().text('"' * (900 * KIB)))
        assert len(fixture.outputs) == 1


@pytest.mark.parametrize(
    "event",
    [
        AgentSend().text(5),  # not a string
    ],
    ids=["text_not_str"],
)
async def test_malformed_message_is_a_contract_error_not_a_handler_error(private_key, contracts, event):
    async with setup(private_key, contracts) as fixture:
        with pytest.raises(DooersThreadsCallbackError) as error:
            await _accept(fixture, contracts, event)
        assert error.value.code == "INVALID_REQUEST"
        assert not fixture.outputs

        async def handler(_on, _send, *_args):
            yield event

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_contract_error"


@pytest.mark.parametrize("shape", ["contact", "template"])
async def test_lone_surrogate_in_a_message_is_replaced_like_production(private_key, contracts, shape):
    async with setup(private_key, contracts) as fixture:
        event = (
            AgentSend().contact(display_name="Ana", vcard="a\ud800b")
            if shape == "contact"
            else AgentSend().template("t", "pt_BR", template_components=[{"type": "body", "text": "a\ud800b"}])
        )
        await _accept(fixture, contracts, event)
        (output,) = [semantic for semantic, _result in fixture.outputs.values()]
        assert "a\ufffdb" in json.dumps(output, ensure_ascii=False)


async def test_message_update_is_checked_locally_like_a_message(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        content = [{"type": "text", "text": "a" * int(1.1 * 1024 * KIB)}]
        with pytest.raises(DooersThreadsCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, AgentSend().update_user_event(fixture.envelope["trigger_event_id"], content))
        assert not fixture.outputs


async def test_silence_while_a_stream_is_open_is_refused(private_key, contracts):
    async with setup(private_key, contracts, config_change={"product_capabilities": ("stream.delta",)}) as fixture:
        client = DooersThreadsCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
        sink = DooersThreadsEventSink(client)
        stream = sink.open_text()  # an open stream will publish a message
        with pytest.raises(DooersThreadsCallbackError) as error:
            await sink.accept(DooersThreadsSend().silence("not_relevant"))
        assert error.value.code == "INVALID_REQUEST"
        stream.close()
        client.close()


async def test_a_new_sink_starts_without_outputs_of_other_phases(private_key, contracts):
    """A resumed phase gets a new sink (``executor.execute``) and Dooers Threads counts outputs per delivery and phase."""
    async with setup(private_key, contracts) as fixture:
        client = DooersThreadsCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
        earlier = DooersThreadsEventSink(client)
        await earlier.accept(AgentSend().text("resposta da fase anterior"))
        assert earlier.normal_outcome() == ("responded", "responded")
        resumed = DooersThreadsEventSink(client)
        assert resumed.intents == () and resumed.event_ids == ()
        assert resumed.normal_outcome() == ("silent", "implicit_empty")
        client.close()
