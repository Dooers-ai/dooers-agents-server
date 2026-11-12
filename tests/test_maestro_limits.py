"""Agent output size limit and callback response caps (Maestro raised the agent message limit to 1 MiB)."""

from __future__ import annotations

from copy import deepcopy

import pytest

from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.maestro import MaestroConfigurationError
from dooers.agents.server.maestro.callbacks import MaestroCallbackClient, MaestroCallbackError
from dooers.agents.server.maestro.sink import MaestroEventSink
from dooers.agents.server.protocol.maestro import MaestroContracts
from tests.test_maestro_executor import execute_fixture, setup

KIB = 1024
# The contract bundle pinned in this repository still caps message text at 64 Ki characters
# (``x-maestro-string-length``); Maestro raises it for agent text. Until the bundle is re-vendored,
# these tests relax the schema check of the size-bound messages and exercise only the SDK limits.
_RELAXED = {"Public.OutputRequest", "Public.PersistedResponse"}


@pytest.fixture(autouse=True)
def relaxed_message_size(monkeypatch):
    validate = MaestroContracts.validate
    monkeypatch.setattr(
        MaestroContracts, "validate", lambda self, schema, data: deepcopy(data) if schema in _RELAXED else validate(self, schema, data)
    )


@pytest.mark.xfail(strict=True, reason="vendored contract still caps agent text at 64 Ki characters; remove after re-vendoring")
def test_vendored_contract_accepts_large_agent_text(contracts):
    text = "a" * (900 * KIB)
    output = {
        "output_id": "output:0",
        "type": "message.create",
        "lease_fence": "1",
        "payload": {"content": [{"type": "text", "text": text}]},
    }
    assert contracts.is_valid("Public.OutputRequest", output)


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return MaestroContracts()


def test_default_output_limit_is_one_mib_and_configurable(private_key, contracts):
    from dataclasses import replace

    from tests.test_maestro_admission import config_for

    config = config_for(private_key)
    assert config.max_output_bytes == 1048576
    assert replace(config, max_output_bytes=1024).max_output_bytes == 1024
    for invalid in (0, 1048577):
        with pytest.raises(MaestroConfigurationError, match="MAESTRO_LIMIT_INVALID"):
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
    client = MaestroCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
    try:
        await MaestroEventSink(client).accept(event)
    finally:
        client.close()


async def test_1_1_mib_text_fails_locally_before_sending(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        with pytest.raises(MaestroCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, AgentSend().text("a" * int(1.1 * 1024 * KIB)))
        assert not any(action == "outputs" for action, *_ in fixture.requests)


async def test_limit_counts_utf8_bytes_not_characters(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        # 600k characters of 2 bytes each: 1.2 MB of UTF-8, 600k characters.
        with pytest.raises(MaestroCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, AgentSend().text("é" * 600_000))
        assert not fixture.requests or all(action != "outputs" for action, *_ in fixture.requests)


async def test_limit_is_the_configured_one(private_key, contracts):
    async with setup(private_key, contracts, config_change={"max_output_bytes": 2048}) as fixture:
        await _accept(fixture, contracts, AgentSend().text("a" * 2048))
        with pytest.raises(MaestroCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, AgentSend().text("a" * 2049))


async def test_contact_and_template_json_count_like_maestro(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        send = AgentSend()
        # Maestro counts the JSON of the contact part and of the template next to the text.
        big = "x" * int(1.1 * 1024 * KIB)
        with pytest.raises(MaestroCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, send.contact(display_name="Ana", vcard=big))
        with pytest.raises(MaestroCallbackError, match="PAYLOAD_TOO_LARGE"):
            await _accept(fixture, contracts, send.template("t", "pt_BR", template_components=[{"type": "body", "text": big}]))
        assert all(action != "outputs" for action, *_ in fixture.requests)


@pytest.mark.parametrize("shape", ["contact", "template"])
async def test_contact_and_template_below_the_limit_are_published(private_key, contracts, shape):
    async with setup(private_key, contracts) as fixture:
        small = "x" * (500 * KIB)
        send = AgentSend()
        event = (
            send.contact(display_name="Ana", vcard=small)
            if shape == "contact"
            else send.template("t", "pt_BR", template_components=[{"type": "body", "text": small}])
        )
        await _accept(fixture, contracts, event)
        assert len(fixture.outputs) == 1


async def test_text_just_over_the_limit_with_json_escapes_is_counted_in_bytes_not_json(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        # 900 KiB of quotes is 1.8 MiB of JSON, but only 900 KiB of text for Maestro: it is published.
        await _accept(fixture, contracts, AgentSend().text('"' * (900 * KIB)))
        assert len(fixture.outputs) == 1
