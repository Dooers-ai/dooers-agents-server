"""What a failing handler, a refused edit and unsafe text do in Dooers Threads mode, as production does (plan 3 final review).

Real HTTP loopback (``CallbackFixture``), like ``tests/test_dooers_threads_sink_parity.py``.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.dooers_threads import create_text_delivery_adapter
from dooers.agents.server.dooers_threads import sink as sink_module
from dooers.agents.server.dooers_threads.client import DooersThreadsContextError
from dooers.agents.server.dooers_threads.context import DooersThreadsPrivateResources
from dooers.agents.server.dooers_threads.window import DooersThreadsHistoryLimitError
from dooers.agents.server.protocol.dooers_threads import default_contracts
from tests.test_dooers_threads_admission import json_bytes, request
from tests.test_dooers_threads_executor import execute_fixture, setup


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return default_contracts()


def published(fixture):
    return [semantic for semantic, _result in fixture.outputs.values()]


class RecordingAnalytics:
    """The handler's ``analytics``: records ``track`` like ``AgentAnalytics`` does (event, data)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.fail = fail

    async def track(self, event, data=None):
        if self.fail:
            raise RuntimeError("analytics down")
        self.calls.append((event, data))


async def run_with(fixture, handler, analytics, *, config=None):
    """The delivery of ``fixture`` with a private resolver that gives the handler ``analytics``."""

    async def resolver(scope):
        return DooersThreadsPrivateResources(scope, {}, {}, analytics)

    adapter = create_text_delivery_adapter(config or fixture.config, handler, resolver)
    return await request(adapter, json_bytes(fixture.envelope), fixture.headers)


# -- I1: a handler exception follows the production error path (pipeline.py) --


async def test_handler_exception_fails_the_run_and_writes_the_error_message(private_key, contracts, caplog):
    analytics = RecordingAnalytics()
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.run_start()
            yield send.text("parcial")
            raise RuntimeError("falha do pedido 42")

        with caplog.at_level(logging.ERROR):
            response = await run_with(fixture, handler, analytics)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_error"
        start, partial, error_message, run_failed = published(fixture)
        run_id = start["payload"]["run_id"]
        assert (start["type"], start["payload"]["status"]) == ("run.update", "running")
        assert partial["payload"]["content"] == [{"type": "text", "text": "parcial"}]
        # The production system message: the exception text, in the failed run.
        assert error_message["type"] == "message.create"
        assert error_message["payload"]["content"] == [{"type": "text", "text": "falha do pedido 42"}]
        assert error_message["payload"]["run_id"] == run_id
        assert run_failed["type"] == "run.update"
        assert run_failed["payload"] == {"run_id": run_id, "status": "failed", "error": "falha do pedido 42"}
        # The traceback is logged, as production does.
        (record,) = [r for r in caplog.records if "handler error" in r.getMessage()]
        assert record.levelno == logging.ERROR and record.exc_info and record.exc_info[0] is RuntimeError
        # Production tracks ERROR_OCCURRED with the error text and the exception type.
        assert analytics.calls == [("error.occurred", {"error": "falha do pedido 42", "type": "RuntimeError"})]


async def test_timeout_error_raised_by_the_handler_is_a_handler_failure(private_key, contracts):
    """A TimeoutError the handler raises (e.g. its own asyncio.wait_for) is not the delivery budget expiring."""
    analytics = RecordingAnalytics()
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.run_start()
            await asyncio.wait_for(asyncio.sleep(1), timeout=0.01)

        response = await run_with(fixture, handler, analytics)
        assert response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_error"
        start, error_message, run_failed = published(fixture)
        assert error_message["type"] == "message.create"
        assert run_failed["type"] == "run.update" and run_failed["payload"]["status"] == "failed"
        assert run_failed["payload"]["run_id"] == start["payload"]["run_id"]
        assert [event for event, _data in analytics.calls] == ["error.occurred"]


async def test_handler_exception_without_a_run_writes_only_the_message(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, _send, *_args):
            raise ValueError("sem run")
            yield

        response = await run_with(fixture, handler, RecordingAnalytics())
        assert response.json()["state"] == "failed"
        (message,) = published(fixture)
        assert message["type"] == "message.create" and "run_id" not in message["payload"]
        assert message["payload"]["content"] == [{"type": "text", "text": "sem run"}]


async def test_handler_exception_after_the_run_ended_does_not_fail_the_closed_run(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.run_start()
            yield send.run_end()
            raise ValueError("depois do run")

        response = await run_with(fixture, handler, RecordingAnalytics())
        assert response.json()["state"] == "failed"
        types = [(output["type"], output["payload"].get("status")) for output in published(fixture)]
        assert types == [("run.update", "running"), ("run.update", "succeeded"), ("message.create", None)]


async def test_failure_report_is_best_effort_and_the_phase_still_fails(private_key, contracts, caplog):
    """A message Dooers Threads cannot take (too large) and an analytics port that fails do not hide the failure."""
    analytics = RecordingAnalytics(fail=True)
    async with setup(private_key, contracts, config_change={"max_output_bytes": 16}) as fixture:

        async def handler(_on, send, *_args):
            yield send.run_start()
            raise RuntimeError("x" * 100)

        with caplog.at_level(logging.WARNING):
            response = await run_with(fixture, handler, analytics)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "failed" and fixture.completions[-1]["reason_code"] == "handler_error"
        # The message is over the output limit; the run is still closed as failed.
        assert [(o["type"], o["payload"].get("status")) for o in published(fixture)] == [
            ("run.update", "running"),
            ("run.update", "failed"),
        ]


async def test_handler_without_a_trackable_analytics_port_still_fails_cleanly(private_key, contracts):
    """The default private resolver of the tests gives a plain dict: nothing to track, nothing breaks."""
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, _send, *_args):
            raise RuntimeError("boom")
            yield

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "failed"
        assert len(published(fixture)) == 1


# -- I6: one terminal state per cause --


@pytest.mark.parametrize("code", ["CONTEXT_UNAVAILABLE", "CONTEXT_INVALID"])
async def test_context_read_failure_in_the_handler_is_a_handler_failure(private_key, contracts, code):
    """Not a lost lease: no LEASE_STALE, so Dooers Threads does not start a retry after published outputs."""
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.text("antes")
            raise DooersThreadsContextError(code)

        response = await run_with(fixture, handler, RecordingAnalytics())
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_error"
        assert [o["payload"]["content"][0]["text"] for o in published(fixture)] == ["antes", code]


async def test_history_limit_in_the_handler_is_the_same_handler_failure(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, _send, *_args):
            raise DooersThreadsHistoryLimitError(20)
            yield

        response = await run_with(fixture, handler, RecordingAnalytics())
        assert response.status_code == 200 and response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_error"


@pytest.mark.parametrize("code", ["ACCESS_REVOKED", "CONTEXT_EXPIRED"])
async def test_lost_authority_in_the_handler_stays_lease_stale(private_key, contracts, code):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, _send, *_args):
            raise DooersThreadsContextError(code)
            yield

        response = await run_with(fixture, handler, RecordingAnalytics())
        assert response.status_code == 409 and response.json()["error"]["code"] == "LEASE_STALE"
        assert not fixture.completions


# -- I2: a refused send.update_user_event is a warning --


@pytest.mark.parametrize(
    "refusal",
    [(404, "NOT_FOUND", None), (403, "FORBIDDEN", "multi_agent_thread")],
    ids=["missing_event", "multi_agent_thread"],
)
async def test_refused_update_user_event_keeps_the_handler_running(private_key, contracts, caplog, refusal):
    async with setup(private_key, contracts) as fixture:
        fixture.refuse_output["message.update"] = refusal

        async def handler(_on, send, *_args):
            yield send.update_user_event("00000000-0000-4000-8000-0000000000ff", [{"type": "text", "text": "editado"}])
            yield send.text("depois")

        with caplog.at_level(logging.WARNING, logger=sink_module.__name__):
            response = await execute_fixture(fixture, handler)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "responded"
        # Nothing was recorded for the refused edit and its ordinal is free for the next output.
        (output,) = published(fixture)
        assert (output["output_id"], output["type"]) == ("output:0", "message.create")
        assert any("message.update" in r.getMessage() for r in caplog.records if r.name == sink_module.__name__)


async def test_update_user_event_422_still_fails_the_run_as_a_contract_error(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        fixture.refuse_output["message.update"] = (422, "VALIDATION_FAILED", None)

        async def handler(_on, send, *_args):
            yield send.update_user_event("00000000-0000-4000-8000-0000000000ff", [{"type": "text", "text": "editado"}])

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_contract_error"


# -- I3: production sanitizes NUL and lone surrogates before it stores --


async def test_text_with_nul_and_lone_surrogate_is_published_clean(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(_on, send, *_args):
            yield send.text("a\x00b\ud800c")
            yield send.reasoning("pen\x00sando")
            yield send.tool_call("buscar\x00", {"q": "x\x00y", "k\x00": ["\ud83d"]})
            yield send.tool_result("buscar", {"saida": "bin\x00ario\udfff"}, args={"q": "z\x00"})

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "responded"
        text, reasoning, call, result = published(fixture)
        assert text["payload"]["content"] == [{"type": "text", "text": "ab�c"}]
        assert reasoning["payload"]["text"] == "pensando"
        assert call["payload"]["name"] == "buscar"
        assert call["payload"]["args"] == {"q": "xy", "k": ["�"]}
        assert result["payload"]["result"] == {"saida": "binario�"}
        assert result["payload"]["args"] == {"q": "z"}
        for _action, _headers, _body, raw in fixture.requests:
            assert b"\\u0000" not in raw and b"\\ud800" not in raw.lower()


async def test_dooers_threads_422_on_an_output_is_a_contract_error_not_an_unsupported_capability(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        fixture.refuse_output["message.create"] = (422, "VALIDATION_FAILED", None)

        async def handler(_on, send, *_args):
            yield send.text("Olá")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_contract_error"


async def test_dooers_threads_unsupported_capability_on_an_output_keeps_its_reason(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        fixture.refuse_output["message.create"] = (422, "UNSUPPORTED_CAPABILITY", None)

        async def handler(_on, send, *_args):
            yield send.text("Olá")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "unsupported_capability"


# -- I5: AgentConfig.allowed_content_types and content_policy_denial_message --


def agent_config(fixture, **values):
    return AgentConfig(
        database_type="postgres", thread_management="dooers_threads", dooers_threads=fixture.config, assistant_name="Assistente", **values
    )


async def deliver(fixture, handler, config):
    called = []

    async def tracked(on, send, *args):
        called.append(on.message)
        async for event in handler(on, send, *args):
            yield event

    adapter = create_text_delivery_adapter(
        config, tracked, lambda scope: asyncio.sleep(0, DooersThreadsPrivateResources(scope, {}, {}, {}))
    )
    return await request(adapter, json_bytes(fixture.envelope), fixture.headers), called


async def handler_reply(_on, send, *_args):
    yield send.text("resposta do agente")


async def test_disallowed_content_type_is_denied_before_the_handler(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        config = agent_config(fixture, allowed_content_types=("image",))
        response, called = await deliver(fixture, handler_reply, config)
        assert response.status_code == 200, response.text
        assert called == []
        # pipeline.py: run_start, the denial text (author = assistant name), run_end failed "content_policy".
        start, denial, end = published(fixture)
        run_id = start["payload"]["run_id"]
        assert (start["type"], start["payload"]["status"]) == ("run.update", "running")
        assert denial["payload"]["content"] == [
            {"type": "text", "text": "This agent does not accept this attachment type in chat (text). Allowed: image."}
        ]
        assert denial["payload"]["author"] == "Assistente" and denial["payload"]["run_id"] == run_id
        assert end["payload"] == {"run_id": run_id, "status": "failed", "error": "content_policy"}


async def test_denial_message_template_is_used(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        config = agent_config(
            fixture, allowed_content_types="image, document", content_policy_denial_message="Só {allowed}; chegou {offenders}."
        )
        _response, called = await deliver(fixture, handler_reply, config)
        assert called == []
        assert published(fixture)[1]["payload"]["content"] == [{"type": "text", "text": "Só document, image; chegou text."}]


async def test_allowed_content_type_runs_the_handler(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        config = agent_config(fixture, allowed_content_types=("text",))
        response, called = await deliver(fixture, handler_reply, config)
        assert response.json()["state"] == "responded" and len(called) == 1
        assert [o["payload"]["content"][0]["text"] for o in published(fixture)] == ["resposta do agente"]


async def test_no_allowlist_runs_the_handler(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        _response, called = await deliver(fixture, handler_reply, agent_config(fixture))
        assert len(called) == 1


# -- the actor of the execution context (e-mail, identity ids) stays out of repr and logs --


async def test_execution_context_repr_does_not_show_the_actor(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        seen = {}

        async def handler(on, send, *_args):
            seen["actor"] = on.context.dooers_threads.actor
            seen["repr"] = repr(on.context.dooers_threads)
            yield send.text("ok")

        response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        assert seen["actor"]  # the handler still reads it
        for value in seen["actor"].values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in seen["repr"], value
        assert "actor=" not in seen["repr"]
