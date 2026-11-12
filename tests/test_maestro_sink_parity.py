"""Every production ``send.*`` call is published inside a Maestro execution (plan 3, task 3).

Real HTTP loopback: ``CallbackFixture`` records outputs and artifact uploads; a small media
server stands in for the URLs the agent passes to ``send.image``/``audio``/``document``.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import logging
from contextlib import asynccontextmanager

import pytest

from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.maestro import media_fetch
from dooers.agents.server.maestro import outputs as outputs_module
from dooers.agents.server.maestro import sink as sink_module
from dooers.agents.server.maestro.sink import MaestroSend
from dooers.agents.server.protocol.maestro import MaestroContracts
from tests.test_maestro_executor import execute_fixture, setup

OTHER_AGENT = "00000000-0000-4000-8000-000000000012"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\r\n\n\r-- end"
PDF = b"%PDF-1.4 fixture"
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt fixture"


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return MaestroContracts()


MESSAGE_SENDS = ("text", "image", "document", "audio", "contact", "template")
ALL_PRODUCTION_SEND_TYPES_EXCEPT_WHATSAPP = (
    *MESSAGE_SENDS,
    "reasoning",
    "tool_call",
    "tool_result",
    "tool_transaction",
    "form",
    "chart",
    "run_start",
    "run_end",
    "event_update",
    "thread_update",
    "participant_add",
)
EXPECTED_OUTPUT = {
    **dict.fromkeys(MESSAGE_SENDS, "message.create"),
    "reasoning": "reasoning.create",
    "tool_call": "tool_call.create",
    "tool_result": "tool_result.create",
    "tool_transaction": "tool_transaction.create",
    "form": "form.create",
    "chart": "chart.create",
    "run_start": "run.update",
    "run_end": "run.update",
    "event_update": "message.update",
    "thread_update": "thread.update",
    "participant_add": "participant.add",
}


@asynccontextmanager
async def media_server(routes):
    """GET path -> (status, headers, body). Unknown paths answer 404."""
    seen = []

    async def handle(reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            path = raw.split(b" ", 2)[1].decode()
            seen.append(path)
            status, headers, body = routes.get(path, (404, {"Content-Type": "text/plain"}, b"missing"))
            head = {"Content-Length": str(len(body)), "Connection": "close", **headers}
            writer.write(
                f"HTTP/1.1 {status} Fixture\r\n".encode() + "".join(f"{k}: {v}\r\n" for k, v in head.items()).encode() + b"\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", seen
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def loopback_media(monkeypatch):
    """The loopback media server is not a public host; allow it only in these tests."""
    monkeypatch.setattr(sink_module, "fetch_media", functools.partial(media_fetch.fetch_media, _allow_hosts=("127.0.0.1",)))


MEDIA_ROUTES = {
    "/logo.png": (200, {"Content-Type": "image/png"}, PNG),
    "/nota.pdf": (200, {"Content-Type": "application/pdf"}, PDF),
    "/voz.wav": (200, {"Content-Type": "audio/wav"}, WAV),
}


def published(fixture):
    """Outputs in request order, without ``lease_fence``."""
    return [semantic for semantic, _result in fixture.outputs.values()]


def production_calls(send_type, send, on, media):
    return {
        "text": lambda: [send.text("Olá")],
        "image": lambda: [send.image(f"{media}/logo.png")],
        "document": lambda: [send.document(f"{media}/nota.pdf", filename="nota.pdf", mime_type="application/pdf")],
        "audio": lambda: [send.audio(f"{media}/voz.wav", mime_type="audio/wav", duration=1.5)],
        "contact": lambda: [send.contact(display_name="Ana", phones=[{"phone": "+5511999990000", "type": "CELL"}])],
        "template": lambda: [send.template("boas_vindas", "pt_BR")],
        "reasoning": lambda: [send.reasoning("pensando")],
        "tool_call": lambda: [send.tool_call("buscar", {"q": "x"})],
        "tool_result": lambda: [send.tool_result("buscar", {"ok": True})],
        "tool_transaction": lambda: [send.tool_transaction("buscar", {"q": "x"}, {"ok": True})],
        "form": lambda: [send.form("Preencha", [send.form_text("nome", label="Nome")])],
        "chart": lambda: [send.chart(chart_type="bar", data=[{"x": "a", "y": 1}], x_key="x", y_keys=["y"])],
        "run_start": lambda: [send.run_start()],
        "run_end": lambda: [send.run_start(), send.run_end()],
        "event_update": lambda: [send.update_user_event(on.context.event_id, [{"type": "text", "text": "editado"}])],
        "thread_update": lambda: [send.update_thread(title="Atendimento")],
        "participant_add": lambda: [send.add_participant("user-2", user_name="Bia")],
    }[send_type]()


def test_parametrization_covers_every_production_send_type():
    builders = set(outputs_module._BUILDERS)
    assert set(ALL_PRODUCTION_SEND_TYPES_EXCEPT_WHATSAPP) == builders | {"image", "audio", "document"}


@pytest.mark.parametrize("send_type", ALL_PRODUCTION_SEND_TYPES_EXCEPT_WHATSAPP)
async def test_production_send_is_published(private_key, contracts, loopback_media, send_type):
    async with setup(private_key, contracts) as fixture, media_server(MEDIA_ROUTES) as (media, _seen):

        async def handler(on, send, *_args):
            for event in production_calls(send_type, send, on, media):
                yield event

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "responded"
        outputs = published(fixture)
        assert outputs and outputs[-1]["type"] == EXPECTED_OUTPUT[send_type]
        assert [output["output_id"] for output in outputs] == [f"output:{n}" for n in range(len(outputs))]
        if send_type in {"image", "document", "audio"}:
            (part,) = outputs[-1]["payload"]["content"]
            assert part["type"] == "attachment"
            assert fixture.artifacts[part["artifact_id"]]["data"] == MEDIA_ROUTES["/" + part["filename"]][2]
        if send_type == "run_end":
            assert [o["payload"]["status"] for o in outputs] == ["running", "succeeded"]


async def test_image_url_is_uploaded_with_alt(private_key, contracts, loopback_media):
    async with setup(private_key, contracts) as fixture, media_server(MEDIA_ROUTES) as (media, seen):

        async def handler(_on, send, *_args):
            yield send.image(f"{media}/logo.png", alt="Logo da loja")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "responded"
        (output,) = published(fixture)
        (part,) = output["payload"]["content"]
        assert part == {
            "type": "attachment",
            "artifact_id": part["artifact_id"],
            "filename": "logo.png",
            "mime_type": "image/png",
            "size_bytes": len(PNG),
            "alt": "Logo da loja",
        }
        artifact = fixture.artifacts[part["artifact_id"]]
        assert artifact["data"] == PNG and artifact["mime_type"] == "image/png"
        assert artifact["idempotency_key"].endswith(":0")
        assert seen == ["/logo.png"]


async def test_audio_attachment_keeps_duration_and_declared_mime(private_key, contracts, loopback_media):
    routes = {"/voz": (200, {"Content-Type": "application/octet-stream"}, WAV)}
    async with setup(private_key, contracts) as fixture, media_server(routes) as (media, _seen):

        async def handler(_on, send, *_args):
            yield send.audio(f"{media}/voz", mime_type="audio/wav", duration=2.5, author="Bot")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output["payload"]["author"] == "Bot"
        (part,) = output["payload"]["content"]
        assert (part["type"], part["mime_type"], part["duration"], part["size_bytes"]) == ("attachment", "audio/wav", 2.5, len(WAV))


async def test_image_url_404_becomes_media_link(private_key, contracts, loopback_media, caplog):
    async with setup(private_key, contracts) as fixture, media_server({}) as (media, seen):
        url = f"{media}/sumiu.png?token=segredo"

        async def handler(_on, send, *_args):
            yield send.image(url, alt="Logo")

        with caplog.at_level(logging.WARNING, logger=sink_module.__name__):
            response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output["payload"]["content"] == [{"type": "media_link", "kind": "image", "url": url, "alt": "Logo"}]
        assert not fixture.artifacts and seen == ["/sumiu.png?token=segredo"]
        (record,) = [r for r in caplog.records if r.name == sink_module.__name__]
        assert record.levelno == logging.WARNING and "status" in record.getMessage()
        assert "segredo" not in record.getMessage()


@pytest.mark.parametrize("reason", ["blocked", "too_large", "network"])
async def test_media_fetch_failure_becomes_media_link(private_key, contracts, monkeypatch, caplog, reason):
    config_change = {"max_artifact_bytes": 4} if reason == "too_large" else None
    if reason != "blocked":
        monkeypatch.setattr(sink_module, "fetch_media", functools.partial(media_fetch.fetch_media, _allow_hosts=("127.0.0.1",)))
    async with setup(private_key, contracts, config_change=config_change) as fixture, media_server(MEDIA_ROUTES) as (media, _seen):
        url = f"{media}/nota.pdf"
        if reason == "network":
            closed = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
            url = f"http://127.0.0.1:{closed.sockets[0].getsockname()[1]}/nota.pdf"
            closed.close()
            await closed.wait_closed()

        async def handler(_on, send, *_args):
            yield send.document(url, filename="nota.pdf", mime_type="application/pdf")

        with caplog.at_level(logging.WARNING, logger=sink_module.__name__):
            response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output["payload"]["content"] == [
            {"type": "media_link", "kind": "document", "url": url, "filename": "nota.pdf", "mime_type": "application/pdf"}
        ]
        assert not fixture.artifacts
        assert any(reason in r.getMessage() for r in caplog.records if r.name == sink_module.__name__)


async def test_media_type_outside_production_policy_becomes_media_link(private_key, contracts, loopback_media):
    routes = {"/dados.bin": (200, {"Content-Type": "application/octet-stream"}, b"\x00\x01")}
    async with setup(private_key, contracts) as fixture, media_server(routes) as (media, _seen):

        async def handler(_on, send, *_args):
            yield send.image(f"{media}/dados.bin")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output["payload"]["content"] == [{"type": "media_link", "kind": "image", "url": f"{media}/dados.bin"}]
        assert not fixture.artifacts


async def test_upload_rejected_by_maestro_becomes_media_link(private_key, contracts, loopback_media):
    async with setup(private_key, contracts) as fixture, media_server(MEDIA_ROUTES) as (media, _seen):
        fixture.status["artifacts"] = 422

        async def handler(_on, send, *_args):
            yield send.image(f"{media}/logo.png", mime_type="image/png")
            yield send.text("depois")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        first, second = published(fixture)
        assert first["payload"]["content"] == [
            {"type": "media_link", "kind": "image", "url": f"{media}/logo.png", "mime_type": "image/png"}
        ]
        assert second["payload"]["content"] == [{"type": "text", "text": "depois"}]


async def test_part_uses_the_mime_type_maestro_stored(private_key, contracts, loopback_media):
    async with setup(private_key, contracts) as fixture, media_server(MEDIA_ROUTES) as (media, _seen):
        fixture.stored_mime["audio/x-wav"] = "audio/wav"

        async def handler(_on, send, *_args):
            yield send.audio(f"{media}/voz.wav", mime_type="audio/x-wav")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        (part,) = output["payload"]["content"]
        assert part["mime_type"] == "audio/wav"
        assert fixture.artifacts[part["artifact_id"]]["mime_type"] == "audio/wav"


async def test_png_data_url_is_uploaded_as_attachment(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()

        async def handler(_on, send, *_args):
            yield send.image(url, alt="Gráfico")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        (part,) = output["payload"]["content"]
        assert (part["type"], part["filename"], part["mime_type"], part["size_bytes"], part["alt"]) == (
            "attachment",
            "file.png",
            "image/png",
            len(PNG),
            "Gráfico",
        )
        assert fixture.artifacts[part["artifact_id"]]["data"] == PNG


@pytest.mark.parametrize(
    "url,config_change",
    [
        ("data:image/png;base64," + base64.b64encode(PNG).decode(), {"max_artifact_bytes": 16}),  # oversized
        ("data:image/png;base64,@@não-é-base64@@", None),  # malformed
        ("data:application/x-msdownload;base64,TVo=", None),  # outside the production file policy
        ("ftp://example.com/logo.png", None),  # other non-http(s) scheme
    ],
    ids=["data_oversized", "data_malformed", "data_policy", "ftp"],
)
async def test_refused_non_http_media_url_fails_the_run(private_key, contracts, caplog, url, config_change):
    async with setup(private_key, contracts, config_change=config_change) as fixture:

        async def handler(_on, send, *_args):
            yield send.image(url)

        with caplog.at_level(logging.WARNING, logger=sink_module.__name__):
            response = await execute_fixture(fixture, handler)
        assert response.status_code == 200
        assert (response.json()["state"], fixture.completions[-1]["reason_code"]) == ("failed", "handler_contract_error")
        assert not fixture.outputs and not fixture.artifacts
        (record,) = [r for r in caplog.records if r.name == sink_module.__name__]
        assert "base64" not in record.getMessage() and "TVo" not in record.getMessage()


RULE_REFUSALS = {
    "already_participant": (422, "VALIDATION_FAILED"),
    "agent_not_participant": (403, "FORBIDDEN"),
    "multi_agent_thread": (403, "FORBIDDEN"),
}


TOLERATED_REFUSALS = [
    *((send_type, reason) for send_type in ("participant_add", "thread_update") for reason in sorted(RULE_REFUSALS)),
    ("run_start", "multi_agent_thread"),
]


@pytest.mark.parametrize("send_type,reason", TOLERATED_REFUSALS, ids=[f"{s}-{r}" for s, r in TOLERATED_REFUSALS])
async def test_thread_rule_refusal_keeps_the_handler_running(private_key, contracts, caplog, send_type, reason):
    output_type = EXPECTED_OUTPUT[send_type]
    async with setup(private_key, contracts) as fixture:
        fixture.refuse_output[output_type] = (*RULE_REFUSALS[reason], reason)

        async def handler(_on, send, *_args):
            if send_type == "participant_add":
                yield send.add_participant("user-2", user_name="Bia")
            elif send_type == "thread_update":
                yield send.update_thread(title="Atendimento", queue="suporte")
            else:
                yield send.run_start()
            yield send.text("depois")

        with caplog.at_level(logging.WARNING, logger=sink_module.__name__):
            response = await execute_fixture(fixture, handler)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "responded"
        # The refused output records nothing and does not consume its ordinal.
        (output,) = published(fixture)
        assert (output["output_id"], output["type"]) == ("output:0", "message.create")
        refused = [r for r in fixture.requests if r[0] == "outputs" and r[2]["type"] == output_type]
        assert len(refused) == 1 and refused[0][1]["idempotency-key"].endswith(":0")
        assert any(reason in r.getMessage() for r in caplog.records if r.name == sink_module.__name__)


@pytest.mark.parametrize(
    "send_type,refusal,expected",
    [
        ("participant_add", (403, "FORBIDDEN", None), 403),  # no rule reason: lost authority
        ("text", (403, "FORBIDDEN", "multi_agent_thread"), 403),  # message.create tolerates no refusal
        # run.update tolerates only multi_agent_thread; a 422 completes the phase as failed.
        ("run_start", (422, "VALIDATION_FAILED", "already_participant"), "failed"),
    ],
    ids=["participant_add-no_reason", "text-multi_agent_thread", "run_start-already_participant"],
)
async def test_other_refusals_still_stop_the_run(private_key, contracts, send_type, refusal, expected):
    async with setup(private_key, contracts) as fixture:
        fixture.refuse_output[EXPECTED_OUTPUT[send_type]] = refusal

        async def handler(_on, send, *_args):
            calls = {"participant_add": lambda: send.add_participant("user-2"), "run_start": send.run_start}
            yield calls.get(send_type, lambda: send.text("Olá"))()
            yield send.text("depois")

        response = await execute_fixture(fixture, handler)
        assert not fixture.outputs
        if expected == 403:
            assert response.status_code == 403 and not fixture.completions
        else:
            assert response.status_code == 200 and response.json()["state"] == "failed"


@pytest.mark.parametrize("user", [{"user_email": "bia@example.com"}, {}], ids=["email_only", "empty"])
async def test_add_participant_without_user_id_is_skipped(private_key, contracts, caplog, user):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.add_participant("", **user)
            yield send.text("depois")

        with caplog.at_level(logging.WARNING, logger=sink_module.__name__):
            response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert (output["output_id"], output["type"]) == ("output:0", "message.create")
        assert not [r for r in fixture.requests if r[0] == "outputs" and r[2]["type"] == "participant.add"]
        assert any("user_id" in r.getMessage() for r in caplog.records if r.name == sink_module.__name__)


async def test_two_hundred_outputs_are_published_in_order(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            for number in range(200):
                yield send.text(f"mensagem {number}")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "responded"
        outputs = published(fixture)
        assert [o["output_id"] for o in outputs] == [f"output:{n}" for n in range(200)]
        assert [o["payload"]["content"][0]["text"] for o in outputs] == [f"mensagem {n}" for n in range(200)]


async def test_run_end_failed_does_not_fail_phase(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.run_start()
            yield send.text("parcial")
            yield send.run_end(status="failed", error="boom")
            yield send.run_end()  # production: no current run, nothing happens

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200
        assert response.json()["state"] == "responded"
        assert fixture.completions[-1]["reason_code"] == "responded"
        start, text, end = published(fixture)
        run_id = start["payload"]["run_id"]
        assert start["type"] == end["type"] == "run.update"
        assert start["payload"] == {"run_id": run_id, "status": "running"}
        assert text["payload"]["run_id"] == run_id
        assert end["payload"] == {"run_id": run_id, "status": "failed", "error": "boom"}
        assert [o["output_id"] for o in (start, text, end)] == ["output:0", "output:1", "output:2"]


async def test_update_thread_accepts_queue_keyword(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.update_thread(title="x", queue="suporte")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output == {"output_id": "output:0", "type": "thread.update", "payload": {"title": "x", "queue": "suporte"}}


async def test_text_author_is_recorded(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.text("Olá", author="Assistente")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output["payload"] == {"content": [{"type": "text", "text": "Olá"}], "mentions": [], "author": "Assistente"}


async def test_whatsapp_sends_stay_refused(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.whatsapp.text("oi", to_e164="+5511111111111", instance_id="inst")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200
        assert (response.json()["state"], fixture.completions[-1]["reason_code"]) == ("failed", "unsupported_capability")
        assert not fixture.outputs


def test_form_without_suspension_is_the_production_form():
    elements = [AgentSend.form_text("nome", label="Nome")]
    assert MaestroSend().form("Preencha", elements, submit_label="Ok", size="large") == AgentSend().form(
        "Preencha", elements, submit_label="Ok", size="large"
    )


def test_thread_calls_are_inherited_from_production():
    for name in ("update_user_event", "update_thread", "add_participant"):
        assert getattr(MaestroSend, name) is getattr(AgentSend, name)


@pytest.mark.xfail(strict=True, reason="plano 2, tarefa 5")
async def test_empty_text_is_published(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.text("")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output["payload"]["content"] == [{"type": "text", "text": ""}]


@pytest.mark.xfail(strict=True, reason="plano 2, tarefa 5")
async def test_run_start_other_agent_records_agent_id(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.run_start(agent_id=OTHER_AGENT)
            yield send.text("Olá")
            yield send.run_end()

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        start = published(fixture)[0]
        assert start["payload"]["agent_id"] == OTHER_AGENT and start["payload"]["status"] == "running"
