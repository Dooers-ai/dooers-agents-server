"""Public connector API: ``connectors.tools()`` → ``ToolSet``, ``connectors.call()`` and ``connectors.list()``."""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest

from dooers.tools import connectors
from dooers.tools.connectors import (
    ApprovalDeclined,
    ConnectionRequired,
    ConnectorError,
    InteractionTimeout,
    InvalidArguments,
    PermissionDenied,
)

AGENDA = {"id": "c-1", "slug": "agenda", "name": "Agenda Recepção", "icon": "google-calendar", "type": "google_calendar"}
GITHUB = {"id": "c-2", "slug": "github", "name": "GitHub", "icon": "github", "type": "github"}

CREATE_EVENT = {
    "name": "agenda__create_event",
    "connector": AGENDA,
    "tool": "create_event",
    "title": "Criar evento",
    "description": "Agenda Recepção: cria um evento",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "duration_minutes": {"type": "integer", "default": 30},
        },
        "required": ["summary", "duration_minutes"],
    },
    "read_only": False,
    "destructive": False,
    "state": "needs_approval",
    "status": "ready",
}
LIST_EVENTS = {
    **CREATE_EVENT,
    "name": "agenda__list_events",
    "tool": "list_events",
    "title": "Listar eventos",
    "description": "Agenda Recepção: lista eventos",
    "input_schema": {"type": "object", "properties": {"day": {"type": "string"}}, "required": []},
    "read_only": True,
    "state": "always_allow",
}

OK = {"status": "ok", "content": {"id": "e1"}, "truncated": False, "message": "Feito.", "interaction": None, "connect_url": None}
# What the LLM gets for OK: the service body without the interaction and without null fields.
OK_FOR_LLM = {"status": "ok", "content": {"id": "e1"}, "truncated": False, "message": "Feito."}


def interaction(interaction_id: str, kind: str) -> dict:
    return {
        "status": "interaction_required",
        "message": "Aguardando a pessoa.",
        "interaction": {
            "id": interaction_id,
            "kind": kind,
            "title": "Criar evento",
            "summary": "Criar 'x'",
            "connector": {"id": "c-1", "name": "Agenda Recepção", "icon": "google-calendar"},
            "expires_at": "2026-11-16T10:02:00Z",
        },
    }


def openai_call(arguments: str, call_id: str = "c1", name: str = "agenda__create_event") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


async def test_tools_outside_turn_raises_clear_error(monkeypatch):  # Review Focus
    monkeypatch.delenv("DOOERS_CONNECTOR_SERVICE_URL", raising=False)

    with pytest.raises(ConnectorError, match="connectors precisa de um turno ativo do handler"):
        await connectors.tools()


async def test_tools_lists_and_offers_each_provider_format(svc, turn):
    unavailable = {"name": "agenda__delete_event", "status": "unavailable", "reason": "needs_approval_without_presence"}
    svc.list.return_value = {"tools": [CREATE_EVENT, LIST_EVENTS, unavailable]}

    ts = await connectors.tools(only=["agenda"], read_only=True)

    body = svc.list.calls[0].json()
    assert (body["only"], body["read_only"], body["include_unavailable"]) == (["agenda"], True, False)
    assert body["context"] == {"agent_id": "agent-1", "workspace_id": "ws-1", "thread_id": "th-1", "channel": "dooers-platform"}
    # an unavailable tool never reaches the LLM
    assert ts.names == ["agenda__create_event", "agenda__list_events"]
    schema = CREATE_EVENT["input_schema"]
    assert ts.openai()[0] == {
        "type": "function",
        "function": {"name": "agenda__create_event", "description": "Agenda Recepção: cria um evento", "parameters": schema},
    }
    assert ts.openai_responses()[0] == {
        "type": "function",
        "name": "agenda__create_event",
        "description": "Agenda Recepção: cria um evento",
        "parameters": schema,
    }
    assert ts.anthropic()[1] == {
        "name": "agenda__list_events",
        "description": "Agenda Recepção: lista eventos",
        "input_schema": LIST_EVENTS["input_schema"],
    }
    assert "default" not in ts.gemini()[0]["parameters"]["properties"]["duration_minutes"]


async def test_bind_removes_arg_from_schema_and_injects(svc, turn):
    svc.list.return_value = {"tools": [CREATE_EVENT, LIST_EVENTS]}
    svc.execute.return_value = OK
    original = await connectors.tools()

    ts = original.bind("agenda__create_event", duration_minutes=45)

    parameters = ts.openai()[0]["function"]["parameters"]
    assert "duration_minutes" not in parameters["properties"]
    assert parameters["required"] == ["summary"]
    assert original.openai()[0]["function"]["parameters"] == CREATE_EVENT["input_schema"]  # bind returns a new ToolSet
    assert ts.anthropic()[1]["input_schema"] == LIST_EVENTS["input_schema"]  # other tools untouched

    await ts.run(openai_call('{"summary":"x","duration_minutes":10}'))

    assert svc.execute.calls[0].json()["arguments"] == {"summary": "x", "duration_minutes": 45}


@pytest.mark.parametrize(
    ("call", "call_id", "expected"),
    [
        (openai_call('{"summary":"x"}', "c1"), "c1", lambda body: {"role": "tool", "tool_call_id": "c1", "content": body}),
        (
            {"type": "function_call", "id": "fc_1", "call_id": "c2", "name": "agenda__create_event", "arguments": '{"summary":"x"}'},
            "c2",
            lambda body: {"type": "function_call_output", "call_id": "c2", "output": body},
        ),
        (
            {"type": "tool_use", "id": "t3", "name": "agenda__create_event", "input": {"summary": "x"}},
            "t3",
            lambda body: {"type": "tool_result", "tool_use_id": "t3", "content": body},
        ),
        (
            {"function_call": {"id": "g4", "name": "agenda__create_event", "args": {"summary": "x"}}},
            "g4",
            lambda body: {"function_response": {"id": "g4", "name": "agenda__create_event", "response": OK_FOR_LLM}},
        ),
        (("agenda__create_event", {"summary": "x"}), None, lambda body: OK_FOR_LLM),
    ],
    ids=["openai", "openai_responses", "anthropic", "gemini", "generic"],
)
async def test_run_answers_in_the_callers_provider_format(svc, turn, call, call_id, expected):
    svc.list.return_value = {"tools": [CREATE_EVENT]}
    svc.execute.return_value = OK
    ts = await connectors.tools()

    message = await ts.run(call)

    assert message == expected(json.dumps(OK_FOR_LLM, ensure_ascii=False))
    sent = svc.execute.calls[0].json()
    assert (sent["name"], sent["arguments"]) == ("agenda__create_event", {"summary": "x"})
    assert sent["call_id"] == call_id if call_id else sent["call_id"].startswith("call_")
    assert [e.send_type for e in turn.emitted] == ["tool_call", "tool_result"]


async def test_approval_inside_a_real_turn(pipeline_harness, svc, fake_clock):
    """End to end through the pipeline: the card and both tool events land in the open run, in order."""
    svc.list.return_value = {"tools": [CREATE_EVENT]}
    svc.execute.side_effect = [interaction("i1", "approval"), OK]
    svc.wait.side_effect = [{"status": "pending"}, {"status": "approved"}]
    replies: list[dict] = []

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        ts = await connectors.tools()
        replies.append(await ts.run(openai_call('{"summary":"x"}')))
        yield send.text("Agendado.")
        yield send.run_end()

    await pipeline_harness.run(handler, presence_token="presence-1")

    assert json.loads(replies[0]["content"]) == OK_FOR_LLM
    (run_id,) = pipeline_harness.persistence.runs
    writes = pipeline_harness.persistence.writes
    kinds = ("tool.call", "connector.interaction", "tool.result")
    created = [(i, obj) for i, (op, obj) in enumerate(writes) if op == "create_event" and obj.type in kinds]
    assert [obj.type for _, obj in created] == list(kinds)
    assert {obj.run_id for _, obj in created} == {run_id}
    run_writes = [(i, obj.status) for i, (op, obj) in enumerate(writes) if op in ("create_run", "update_run")]
    opened = next(i for i, status in run_writes if status == "running")
    closed = next(i for i, status in run_writes if status != "running")
    assert opened < created[0][0] and created[-1][0] < closed  # inside the open run
    call, card, result = (obj for _, obj in created)
    assert (call.data["display_name"], result.data["display_name"]) == ("Criar evento", "Criar evento")
    assert card.data["interaction_id"] == "i1" and card.actor == "system"
    assert result.data["result"]["status"] == "ok"
    first = svc.execute.calls[0].json()
    assert first["context"]["thread_id"] == call.thread_id
    assert first["connector_session"] == "sess-1"  # the turn's presence went through the exchange


async def test_run_with_malformed_json_returns_invalid_arguments(svc, turn):  # Review Focus (F11)
    svc.list.return_value = {"tools": [CREATE_EVENT]}
    ts = await connectors.tools()

    message = await ts.run(openai_call("{oops"))

    assert (message["role"], message["tool_call_id"]) == ("tool", "c1")
    payload = json.loads(message["content"])
    assert payload["status"] == "invalid_arguments" and "JSON" in payload["message"]
    assert svc.execute.calls == []


@pytest.mark.parametrize(
    ("answer", "name", "status", "executions"),
    [
        (httpx.Response(503, text="down"), "agenda__create_event", "provider_error", 1),
        (httpx.ConnectError("down"), "agenda__create_event", "provider_error", 1),
        (OK, "agenda__delete_everything", "denied", 0),  # not in this ToolSet: never sent to the service
    ],
    ids=["service_5xx", "network", "tool_not_in_set"],
)
async def test_run_never_raises(svc, turn, answer, name, status, executions):
    svc.list.return_value = {"tools": [CREATE_EVENT]}
    svc.execute.return_value = answer
    ts = await connectors.tools(read_only=True)

    message = await ts.run(openai_call('{"summary":"x"}', name=name))

    payload = json.loads(message["content"])
    assert payload["status"] == status and payload["message"]
    assert len(svc.execute.calls) == executions


async def test_call_returns_the_service_body(svc, turn):
    svc.execute.return_value = OK

    out = await connectors.call("agenda", "create_event", {"summary": "x"})

    assert out == OK
    assert svc.execute.calls[0].json()["name"] == "agenda__create_event"
    assert turn.emitted == []  # API mode: no tool events in the chat


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("connection_required", ConnectionRequired),
        ("declined", ApprovalDeclined),
        ("interaction_timeout", InteractionTimeout),
        ("denied", PermissionDenied),
        ("invalid_arguments", InvalidArguments),
        ("provider_error", ConnectorError),
    ],
)
async def test_call_raises_typed_exceptions(svc, turn, status, error):
    connect_url = "https://app.test/connectors/c-1/connect"
    svc.execute.return_value = {"status": status, "content": None, "message": "frase curta", "connect_url": connect_url}

    with pytest.raises(ConnectorError) as raised:
        await connectors.call("agenda", "create_event", {"summary": "x"})

    assert type(raised.value) is error
    assert (raised.value.status, raised.value.message) == (status, "frase curta")
    if error is ConnectionRequired:
        assert raised.value.connect_url == connect_url


async def test_call_outside_turn_raises():
    with pytest.raises(ConnectorError, match="turno ativo"):
        await connectors.call("agenda", "create_event", {"summary": "x"})


async def test_on_behalf_of_only_when_passed(svc, turn):
    svc.list.return_value = {"tools": [CREATE_EVENT]}
    svc.execute.return_value = OK
    call = ("agenda__create_event", {"summary": "x"})

    await (await connectors.tools()).run(call)
    await connectors.call("agenda", "create_event", {"summary": "x"})
    await connectors.list()
    implicit = [c.json()["on_behalf_of"] for c in svc.list.calls + svc.execute.calls]
    svc.list.calls.clear()
    svc.execute.calls.clear()
    await (await connectors.tools(on_behalf_of="user-9")).run(call)
    await connectors.call("agenda", "create_event", {"summary": "x"}, on_behalf_of="user-9")
    await connectors.list(on_behalf_of="user-9")
    explicit = [c.json()["on_behalf_of"] for c in svc.list.calls + svc.execute.calls]

    # the turn has a person (u1), but the SDK never sends it on its own
    assert implicit == [None, None, None, None]
    assert explicit == ["user-9", "user-9", "user-9", "user-9"]


@pytest.mark.parametrize(("entry", "kind"), [("tools", "approval"), ("call", "connect")])
async def test_interaction_timeout_overrides_both_deadlines(svc, turn, fake_clock, entry, kind):
    svc.list.return_value = {"tools": [CREATE_EVENT]}
    svc.execute.return_value = interaction("i1", kind)

    def held_for_the_whole_slice(call):
        fake_clock.advance(call.json()["timeout_seconds"])
        return {"status": "pending"}

    svc.wait.side_effect = held_for_the_whole_slice

    if entry == "tools":
        ts = await connectors.tools(interaction_timeout=40)
        assert (await ts.run(("agenda__create_event", {"summary": "x"})))["status"] == "interaction_timeout"
    else:
        with pytest.raises(InteractionTimeout):
            await connectors.call("agenda", "create_event", {"summary": "x"}, interaction_timeout=40)

    assert len(svc.wait.calls) == 2  # 40 s in 20 s slices; the defaults would give 6 (approval) or 9 (connect)


async def test_list_explains_unavailable_tools(svc, turn):  # Review Focus
    svc.list.return_value = {
        "tools": [
            LIST_EVENTS,
            # the service may send only name/status/reason for an unavailable tool: slug and tool come from the name
            {"name": "agenda__create_event", "status": "unavailable", "reason": "needs_approval_without_presence"},
            {**CREATE_EVENT, "name": "github__create_issue", "connector": GITHUB, "tool": "create_issue", "status": "connection_required"},
            {"name": "github__delete_repo", "connector": GITHUB, "tool": "delete_repo", "status": "unavailable", "reason": "blocked"},
        ]
    }

    out = await connectors.list()

    body = svc.list.calls[0].json()
    assert (body["only"], body["read_only"], body["include_unavailable"]) == (None, False, True)
    assert out == [
        {
            "slug": "agenda",
            "name": "Agenda Recepção",
            "type": "google_calendar",
            "tools": ["list_events"],
            "unavailable": [{"tool": "create_event", "reason": "needs_approval_without_presence"}],
        },
        {
            "slug": "github",
            "name": "GitHub",
            "type": "github",
            "tools": ["create_issue"],
            "unavailable": [{"tool": "delete_repo", "reason": "blocked"}],
        },
    ]


async def test_list_with_only_unavailable_tool_falls_back_to_the_slug(svc, turn):
    svc.list.return_value = {"tools": [{"name": "agenda__create_event", "status": "unavailable", "reason": "person_required"}]}

    out = await connectors.list()

    assert out == [
        {
            "slug": "agenda",
            "name": "agenda",
            "type": None,
            "tools": [],
            "unavailable": [{"tool": "create_event", "reason": "person_required"}],
        }
    ]


@pytest.mark.filterwarnings("ignore:Core Pydantic V1 functionality:UserWarning")  # langchain_core on Python 3.14
async def test_langchain_adapter(svc, turn):
    pytest.importorskip("langchain_core")
    from langchain_core.tools import StructuredTool

    svc.list.return_value = {"tools": [CREATE_EVENT]}
    svc.execute.return_value = OK
    ts = (await connectors.tools()).bind("agenda__create_event", duration_minutes=45)

    [tool] = ts.langchain()
    out = await tool.ainvoke({"summary": "x"})

    assert isinstance(tool, StructuredTool)
    assert (tool.name, tool.description) == ("agenda__create_event", "Agenda Recepção: cria um evento")
    assert tool.args_schema == ts.specs[0]["input_schema"]
    assert json.loads(out) == OK_FOR_LLM
    sent = svc.execute.calls[0].json()
    assert sent["arguments"] == {"summary": "x", "duration_minutes": 45}
    assert sent["call_id"].startswith("call_")
    assert [e.send_type for e in turn.emitted] == ["tool_call", "tool_result"]


async def test_openai_agents_adapter(svc, turn):
    pytest.importorskip("agents")
    from agents import FunctionTool
    from agents.tool_context import ToolContext

    svc.list.return_value = {"tools": [CREATE_EVENT]}
    svc.execute.return_value = OK
    ts = await connectors.tools()

    [tool] = ts.openai_agents()
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="oa1", tool_arguments='{"summary":"x"}')
    out = await tool.on_invoke_tool(ctx, '{"summary":"x"}')
    bad = await tool.on_invoke_tool(ctx, "{oops")

    assert isinstance(tool, FunctionTool)
    assert (tool.name, tool.params_json_schema, tool.strict_json_schema) == ("agenda__create_event", CREATE_EVENT["input_schema"], False)
    assert json.loads(out) == OK_FOR_LLM
    assert svc.execute.calls[0].json()["call_id"] == "oa1"
    assert json.loads(bad)["status"] == "invalid_arguments"
    assert len(svc.execute.calls) == 1


@pytest.mark.parametrize(
    ("method", "module", "package"),
    [("langchain", "langchain_core.tools", "langchain-core"), ("openai_agents", "agents", "openai-agents")],
)
async def test_missing_framework_gives_clear_message(svc, turn, monkeypatch, method, module, package):
    svc.list.return_value = {"tools": [CREATE_EVENT]}
    ts = await connectors.tools()
    monkeypatch.setitem(sys.modules, module, None)  # makes the import fail as if the package were not installed

    with pytest.raises(ImportError, match=f"pip install {package}"):
        getattr(ts, method)()


@pytest.mark.parametrize("server_first", [False, True], ids=["connectors_first", "server_first"])
def test_public_api_imports_in_any_order(server_first):  # F8
    lines = [
        "import builtins",
        "from dooers.tools.connectors import ToolSet, call, tools",
        "from dooers.tools.connectors import list as list_connectors",
        "from dooers.tools import connectors",
        "assert list_connectors is connectors.list and list_connectors.__name__ == 'list_connectors'",
        "assert callable(tools) and callable(call) and ToolSet.__name__ == 'ToolSet'",
        "assert builtins.list is list",
    ]
    if server_first:
        lines.insert(0, "import dooers.agents.server")
    proc = subprocess.run([sys.executable, "-c", "\n".join(lines)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
