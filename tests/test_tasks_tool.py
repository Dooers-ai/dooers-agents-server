"""dooers.tools.tasks and AgentServer.run_task: scheduled tasks on the Dooers Tasks service."""

from __future__ import annotations

import json

import httpx
import pytest

from dooers.agents.server import AgentConfig, AgentServer, TaskRunRejected, User
from dooers.agents.server.auth_validation import AuthValidationResult
from dooers.tools import tasks
from dooers.tools.connectors.runtime import TurnContext, bind_turn, reset_turn
from tests.fakes import MemoryPersistence


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.task: dict = {"id": "t1", "status": "running", "current_run_id": "1700000000", "title": "Água", "goal": "Lembre de beber água"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append({"url": str(request.url), "body": body, "auth": request.headers.get("authorization")})
        if request.url.path == "/api/v2/identity/service-token/agent":
            return httpx.Response(
                200, json={"success": True, "data": {"accessToken": f"tasks-for-{body['presenceToken']}", "expiresIn": 900}}
            )
        operation = body.get("operation")
        if operation == "get":
            return httpx.Response(200, json={"success": True, "task": self.task})
        if operation == "report_run":
            return httpx.Response(200, json={"success": True, "task": {**self.task, "status": "resolved"}})
        return httpx.Response(201, json={"success": True, "task": {"id": "t1", "status": "scheduled"}})


@pytest.fixture
def service(monkeypatch):
    recorder = Recorder()
    real = httpx.AsyncClient
    monkeypatch.setattr(tasks.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(recorder), **kw))

    async def runtime_key(persistence, agent_id):
        return "rk_" + "x" * 30

    monkeypatch.setattr(tasks, "resolve_runtime_api_key", runtime_key)
    monkeypatch.setattr(tasks, "core_base_url", lambda: "https://core.test")
    monkeypatch.setenv("DOOERS_TASKS_SERVICE_URL", "https://tasks.test")
    tasks._tokens.clear()
    return recorder


def _turn(presence: str | None = "chat-jwt", user_id: str | None = "ana"):
    return bind_turn(
        TurnContext(
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="",
            thread_id="thread-1",
            user_id=user_id,
            channel="dooers-platform",
            presence_token=presence,
            persistence=MemoryPersistence(),
            emit=None,
        )
    )


async def test_schedule_acts_for_the_person_of_the_turn(service):
    token = _turn()
    try:
        task = await tasks.schedule(title="Água", instruction="Lembre de beber água", due_at="2026-11-19T12:00:00Z")
    finally:
        reset_turn(token)
    assert task["status"] == "scheduled"
    mint, call = service.calls
    assert mint["body"]["presenceToken"] == "chat-jwt" and mint["body"]["personal"] is True
    assert mint["body"]["audience"] == "tasks-service-core"
    assert call["url"] == "https://tasks.test/api/v1/external/tools/scheduled-tasks"
    assert call["auth"] == "Bearer tasks-for-chat-jwt"
    assert call["body"] == {
        "operation": "schedule",
        "body": {"title": "Água", "instruction": "Lembre de beber água", "dueAt": "2026-11-19T12:00:00Z", "threadId": "thread-1"},
    }


async def test_scheduling_needs_a_person_and_the_service(service, monkeypatch):
    token = _turn(presence=None)
    try:
        assert tasks.available() is False
        with pytest.raises(tasks.TasksUnavailable):
            await tasks.list()
    finally:
        reset_turn(token)
    monkeypatch.delenv("DOOERS_TASKS_SERVICE_URL")
    token = _turn()
    try:
        assert tasks.available() is False
    finally:
        reset_turn(token)
    with pytest.raises(tasks.TasksUnavailable):
        await tasks.list()  # outside a turn


def _server(monkeypatch, *, purpose: str = "task") -> AgentServer:
    server = AgentServer(AgentConfig(database_type="postgres", analytics_enabled=False))
    server._persistence = MemoryPersistence()  # type: ignore[assignment]
    server._initialized = True

    class Validator:
        async def validate(self, **kwargs):
            return AuthValidationResult(
                valid=True,
                user=User(user_id="ana", user_name="Ana"),
                organization_id="org-1",
                workspace_id="",
                agent_id="agent-1",
                purpose=purpose,
            )

    server._auth_validator = Validator()  # type: ignore[assignment]
    return server


async def test_run_task_runs_as_the_person_and_reports(service, monkeypatch):
    server = _server(monkeypatch)
    seen: dict = {}

    async def handler(incoming, send, memory, analytics, settings):
        seen["trigger"] = incoming.context.trigger
        seen["user"] = incoming.context.user.user_id
        seen["message"] = incoming.message
        yield send.run_start()
        yield send.text("Hora de beber água!")
        yield send.run_end()

    body = {"task_id": "t1", "run_id": "1700000000", "title": "Água", "instruction": "Lembre de beber água", "thread_id": None}
    result = await server.run_task(handler, authorization="Bearer run-jwt", body=body)
    assert result["ok"] and result["outcome"] == "succeeded"
    assert seen["user"] == "ana" and seen["message"] == "Lembre de beber água"
    assert seen["trigger"]["type"] == "task" and seen["trigger"]["run_id"] == "1700000000"
    first = (await server._persistence.get_events(result["thread_id"]))[0]  # type: ignore[union-attr]
    assert first.actor == "system" and first.data["trigger"]["task_id"] == "t1"
    report = service.calls[-1]["body"]
    assert report["operation"] == "report_run"
    assert report["body"] == {"runId": "1700000000", "outcome": "succeeded", "summary": "Hora de beber água!"}
    # The run token (purpose task) is what proves the person to the core.
    assert service.calls[0]["body"]["presenceToken"] == "run-jwt"


async def test_run_task_skips_a_run_that_is_not_current(service, monkeypatch):
    server = _server(monkeypatch)
    service.task = {**service.task, "current_run_id": "1699999999"}

    async def handler(incoming, send, memory, analytics, settings):  # pragma: no cover - must not run
        raise AssertionError("ran a stale task")
        yield

    result = await server.run_task(handler, authorization="Bearer run-jwt", body={"task_id": "t1", "run_id": "1700000000"})
    assert result == {"ok": True, "skipped": True}


async def test_run_task_refuses_chat_tokens(service, monkeypatch):
    server = _server(monkeypatch, purpose="chat")

    async def handler(incoming, send, memory, analytics, settings):  # pragma: no cover
        yield

    with pytest.raises(TaskRunRejected) as error:
        await server.run_task(handler, authorization="Bearer chat-jwt", body={"task_id": "t1", "run_id": "1"})
    assert error.value.status == 401


async def test_runs_and_outcome_statuses(service):
    token = _turn()
    try:
        await tasks.schedule(
            title="Relatório",
            instruction="Monte o relatório",
            due_at="2026-11-23T12:00:00Z",
            recurrence={"freq": "weekly", "byWeekday": [0], "timezone": "America/Sao_Paulo"},
            personal=False,
            on_success_status="55555555-5555-4555-8555-555555555555",
            on_failure_status="66666666-6666-4666-8666-666666666666",
        )
        await tasks.runs("t1", limit=5)
    finally:
        reset_turn(token)
    schedule_call = [c for c in service.calls if c["body"].get("operation") == "schedule"][0]
    assert schedule_call["body"]["body"]["onSuccessStatus"].startswith("5555")
    assert schedule_call["body"]["body"]["onFailureStatus"].startswith("6666")
    runs_call = service.calls[-1]["body"]
    assert runs_call == {"operation": "runs", "taskId": "t1", "limit": 5}
    personal_flags = [c["body"]["personal"] for c in service.calls if "presenceToken" in c["body"]]
    assert personal_flags == [False, True]
