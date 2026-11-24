"""``DooersThreadsAgentApi``: the SDK's HTTP client of the Dooers Threads agent API (``/agent/*``).

It runs against a real loopback server (``tests/parity/fake_agent_api.py``) that answers the routes of
``agent-api/routes.ts`` and checks every request and response against the vendored contract.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from dooers.agents.server.dooers_threads import DooersThreadsConfigurationError
from dooers.agents.server.dooers_threads.agent_api import (
    DooersThreadsAgentApi,
    DooersThreadsAgentApiError,
    DooersThreadsAgentMissing,
    DooersThreadsAgentNotFound,
    DooersThreadsAgentScopeError,
    DooersThreadsAgentThreadDeleting,
)
from tests.parity.fake_agent_api import PREFIX, WORKSPACE_ID, FakeAgentApi

PHONE = "+5511999990000"
EXTERNAL_KEY = f"wa:inst:i1:{PHONE}"
CONTACT = {"kind": "external", "channel": "whatsapp", "external_id": PHONE, "display_name": "Maria"}


@pytest.fixture
async def fake():
    server = await FakeAgentApi().start()
    try:
        yield server
        # A response of the fake that is not contract-valid is a bug of the fake: fail loudly.
        assert server.failures == []
    finally:
        await server.stop()


@pytest.fixture
def api(fake):
    return DooersThreadsAgentApi(fake.config())


async def _thread(api, **overrides):
    return await api.create_thread(
        external_key=overrides.pop("external_key", EXTERNAL_KEY),
        title="WhatsApp",
        channel="whatsapp",
        channel_meta={"instance_id": "i1"},
        queue="suporte",
        participants=[CONTACT],
        **overrides,
    )


async def _message(api, thread_id, text="Oi", *, event_id=None, actor=None, trigger=False):
    return await api.publish_event(
        thread_id,
        actor=actor or {"kind": "self"},
        payload={"content": [{"type": "text", "text": text}]},
        trigger=trigger,
        idempotency_key=event_id or str(uuid4()),
    )


# -- threads --


async def test_create_find_get_list_update_delete_thread(api, fake):
    thread = await _thread(api)
    assert thread["external_key"] == EXTERNAL_KEY
    assert thread["owner_instance_agent_id"] == fake.agent_id
    assert thread["channel"] == "whatsapp"
    assert any(p["kind"] == "external" and p["external_id"] == PHONE for p in thread["participants"])

    assert (await api.find_thread(EXTERNAL_KEY))["thread_id"] == thread["thread_id"]
    assert await api.find_thread("wa:inst:i1:+0000") is None
    # get_thread takes the caller's key or the Dooers Threads id.
    assert (await api.get_thread(EXTERNAL_KEY))["thread_id"] == thread["thread_id"]
    assert (await api.get_thread(thread["thread_id"]))["external_key"] == EXTERNAL_KEY

    page = await api.list_threads(limit=10)
    assert [t["thread_id"] for t in page["items"]] == [thread["thread_id"]]
    assert page["has_more"] is False
    assert (await api.list_threads(queue="suporte"))["items"]
    assert (await api.list_threads(queue="outra"))["items"] == []
    assert (await api.list_threads(external_key=EXTERNAL_KEY))["items"]

    renamed = await api.update_thread(EXTERNAL_KEY, title="Novo", queue="vendas")
    assert (renamed["title"], renamed["queue"]) == ("Novo", "vendas")
    only_title = await api.update_thread(thread["thread_id"], title="So titulo")
    assert (only_title["title"], only_title["queue"]) == ("So titulo", "vendas")
    cleared = await api.update_thread(EXTERNAL_KEY, clear_queue=True)
    assert cleared["queue"] is None

    deleted = await api.delete_thread(EXTERNAL_KEY)
    assert deleted["state"] == "deleting"


async def test_a_key_too_long_for_a_url_is_found_by_its_sha256_and_the_path_carries_the_dooers_threads_id(api, fake):
    """Production takes any thread_id: a key longer than a URL can carry goes by ``external_key_sha256`` and the Dooers Threads id."""
    key = "wa:inst:i1:" + "k" * 5000 + "😀"
    thread = await _thread(api, external_key=key)
    fake.requests.clear()
    assert (await api.find_thread(key))["thread_id"] == thread["thread_id"]
    assert (await api.get_thread(key))["external_key"] == key
    renamed = await api.update_thread(key, title="Novo")
    assert renamed["title"] == "Novo"
    await _message(api, key)
    # No request carries the key: the lookups use its SHA-256 and the paths the Dooers Threads id.
    assert all(key not in request.path and key not in str(request.query) for request in fake.requests)
    assert {request.path for request in fake.requests} >= {f"/api/v1/agent/threads/{thread['thread_id']}"}
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.get_thread(key + "x")


async def test_list_threads_pages_with_cursor(api):
    for number in range(3):
        await _thread(api, external_key=f"k{number}")
    first = await api.list_threads(limit=2)
    assert len(first["items"]) == 2 and first["has_more"] is True
    second = await api.list_threads(limit=2, cursor=first["next_cursor"])
    assert len(second["items"]) == 1 and second["has_more"] is False


async def test_update_thread_needs_a_field(api, fake):
    thread = await _thread(api)
    before = len(fake.requests)
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.update_thread(thread["thread_id"])
    assert error.value.code == "REQUEST_INVALID"
    assert len(fake.requests) == before


# -- events --


async def test_publish_list_get_remove_event(api):
    thread = await _thread(api)
    client_event_id = str(uuid4())
    published = await _message(api, thread["thread_id"], "Ola", event_id=client_event_id, trigger=True)
    event = published["event"]
    assert event["type"] == "message.created"
    assert event["payload"]["client_event_id"] == client_event_id
    assert event["published_by_instance_agent_id"]
    assert "delivery_id" in published

    contact = await _message(
        api, EXTERNAL_KEY, "Cliente", actor={"kind": "external", "channel": "whatsapp", "external_id": PHONE}, trigger=True
    )
    assert contact["event"]["actor"]["kind"] == "external"

    page = await api.list_events(thread["thread_id"])
    assert [e["payload"]["client_event_id"] for e in page["items"]][0] == client_event_id
    assert len(page["items"]) == 2
    newest = await api.list_events(EXTERNAL_KEY, order="desc", limit=1)
    assert newest["items"][0]["event_id"] == contact["event"]["event_id"]
    assert newest["has_more"] is True
    older = await api.list_events(EXTERNAL_KEY, order="desc", limit=1, cursor=newest["next_cursor"])
    assert older["items"][0]["event_id"] == event["event_id"]

    assert (await api.get_event(event["event_id"]))["event_id"] == event["event_id"]

    removed = await api.remove_event(thread["thread_id"], event["event_id"])
    assert removed["type"] == "message.removed"
    assert removed["payload"] == {"event_id": event["event_id"]}
    # The removed message is hidden; the ``message.removed`` event itself stays in the thread.
    remaining = await api.list_events(thread["thread_id"])
    assert [e["type"] for e in remaining["items"]] == ["message.created", "message.removed"]
    assert remaining["items"][0]["event_id"] == contact["event"]["event_id"]
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.get_event(event["event_id"])


async def test_get_event_by_client_event_id(api):
    thread = await _thread(api)
    client_event_id = f"sdk-event-{uuid4()}"
    published = await _message(api, thread["thread_id"], event_id=client_event_id)
    found = await api.get_event(client_event_id)
    assert found["event_id"] == published["event"]["event_id"]
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.get_event("never-published")


async def test_list_events_server_filters(api):
    thread = await _thread(api)
    run_id = str(uuid4())
    await api.publish_event(
        thread["thread_id"],
        actor={"kind": "self"},
        payload={"content": [{"type": "text", "text": "do run"}], "run_id": run_id},
        trigger=False,
        idempotency_key=str(uuid4()),
    )
    await _message(api, thread["thread_id"], "contato", actor={"kind": "external", "channel": "whatsapp", "external_id": PHONE})
    await _message(api, thread["thread_id"], "agente")

    assert len((await api.list_events(thread["thread_id"], types=["message.created"]))["items"]) == 3
    assert (await api.list_events(thread["thread_id"], types=["message.removed"]))["items"] == []
    assert len((await api.list_events(thread["thread_id"], actor_kind="external"))["items"]) == 1
    assert len((await api.list_events(thread["thread_id"], actor_kind="agent"))["items"]) == 2
    assert len((await api.list_events(thread["thread_id"], actor_id=PHONE))["items"]) == 1
    assert len((await api.list_events(thread["thread_id"], run_id=run_id))["items"]) == 1


async def test_publish_uses_the_sdk_event_id_as_idempotency_key_and_replays(api, fake):
    thread = await _thread(api)
    event_id = str(uuid4())
    first = await _message(api, thread["thread_id"], "uma vez", event_id=event_id)
    again = await _message(api, thread["thread_id"], "uma vez", event_id=event_id)
    assert again == first
    assert len((await api.list_events(thread["thread_id"]))["items"]) == 1
    sent = [r for r in fake.requests if r.method == "POST" and r.path.endswith("/events")]
    assert [r.body["idempotency_key"] for r in sent] == [event_id, event_id]
    assert [r.status for r in sent] == [201, 200]
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await _message(api, thread["thread_id"], "outro texto", event_id=event_id)
    assert error.value.code == "IDEMPOTENCY_CONFLICT" and error.value.retryable is False


async def test_add_participant(api):
    thread = await _thread(api)
    event = await api.add_participant(thread["thread_id"], {"kind": "human", "principal_id": "auth0|maria"})
    assert event["type"] == "participant.added"
    assert event["payload"]["participant"]["principal_id"] == "auth0|maria"
    assert any(p["kind"] == "human" for p in (await api.get_thread(thread["thread_id"]))["participants"])


# -- files --


async def test_upload_artifact_and_issue_link(api, fake):
    thread = await _thread(api)
    data = b"conteudo do arquivo"
    artifact = await api.upload_artifact(thread["thread_id"], data, "nota.txt", "text/plain")
    assert artifact == {"artifact_id": artifact["artifact_id"], "filename": "nota.txt", "mime_type": "text/plain", "size_bytes": len(data)}
    upload = fake.requests[-1]
    assert upload.body["metadata"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert upload.headers["idempotency-key"]
    assert fake.artifacts[artifact["artifact_id"]]["data"] == data

    link = await api.issue_artifact_link(thread["thread_id"], artifact["artifact_id"])
    assert link["url"].startswith("https://") and link["expires_at"].endswith("Z")
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.issue_artifact_link(thread["thread_id"], str(uuid4()))


async def test_upload_artifact_checks_the_file_locally(api, fake):
    thread = await _thread(api)
    before = len(fake.requests)
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.upload_artifact(thread["thread_id"], b"x", "a.txt", "TEXT")
    assert error.value.code == "REQUEST_INVALID"
    assert len(fake.requests) == before


async def test_upload_artifact_accepts_an_empty_file_as_production(api, fake):
    """Production's ``POST /uploads`` stores a 0-byte file (``UploadStore.store`` checks only the maximum)."""
    thread = await _thread(api)
    artifact = await api.upload_artifact(thread["thread_id"], b"", "vazio.txt", "text/plain")
    assert artifact["size_bytes"] == 0
    assert fake.requests[-1].body["metadata"]["sha256"] == hashlib.sha256(b"").hexdigest()


async def test_upload_artifact_accepts_any_type_as_production(api):
    """Attachments sweep (R1): ``AgentServer.upload`` stages any file; so does the agent API upload."""
    thread = await _thread(api)
    artifact = await api.upload_artifact(thread["thread_id"], b"MZ", "a.exe", "application/x-msdownload")
    assert artifact["filename"] == "a.exe" and artifact["size_bytes"] == 2


async def test_link_route_off_is_not_retryable(api, fake):
    thread = await _thread(api)
    artifact = await api.upload_artifact(thread["thread_id"], b"abc", "a.txt", "text/plain")
    fake.link_route_enabled = False
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.issue_artifact_link(thread["thread_id"], artifact["artifact_id"])
    assert (error.value.code, error.value.retryable) == ("UNSUPPORTED_CAPABILITY", False)


async def test_link_resolver_returns_none_for_a_missing_file(api):
    thread = await _thread(api)
    artifact = await api.upload_artifact(thread["thread_id"], b"abc", "a.txt", "text/plain")
    resolver = api.link_resolver(thread["thread_id"])
    assert (await resolver.url_for(artifact["artifact_id"])).startswith("https://")
    assert await resolver.url_for(str(uuid4())) is None


# -- runs and channel deliveries --


async def test_list_runs_and_get_run(api, fake):
    thread = await _thread(api)
    run = fake.add_run(thread["thread_id"])
    page = await api.list_runs(thread["thread_id"])
    assert [r["run_id"] for r in page["items"]] == [run["run_id"]]
    assert (await api.get_run(run["run_id"]))["status"] == "succeeded"
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.get_run(str(uuid4()))


async def test_record_and_find_channel_delivery(api):
    thread = await _thread(api)
    message = (await _message(api, thread["thread_id"], "enviada"))["event"]
    first = await api.record_channel_delivery(
        thread_id=EXTERNAL_KEY, event_id=message["event_id"], status="sent", provider_message_id="wamid.1"
    )
    assert first["applied"] is True and first["event"]["type"] == "channel_delivery.updated"
    delivered = await api.record_channel_delivery(
        thread_id=EXTERNAL_KEY, event_id=message["event_id"], status="delivered", provider_message_id="wamid.1"
    )
    assert delivered["applied"] is True
    backwards = await api.record_channel_delivery(thread_id=EXTERNAL_KEY, event_id=message["event_id"], status="sent")
    assert backwards == {"applied": False}
    found = await api.find_channel_delivery("wamid.1")
    assert found == {"thread_id": thread["thread_id"], "event_id": message["event_id"], "status": "delivered"}
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.find_channel_delivery("wamid.unknown")
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.record_channel_delivery(thread_id=EXTERNAL_KEY, event_id=str(uuid4()), status="read")


# -- errors: NOT_FOUND vs THREAD_DELETING --


async def test_not_found_and_thread_deleting_are_distinct(api, fake):
    with pytest.raises(DooersThreadsAgentNotFound) as missing:
        await api.get_thread("wa:inst:i1:+0000")
    assert missing.value.code == "NOT_FOUND" and missing.value.status == 404 and missing.value.retryable is False
    assert isinstance(missing.value, DooersThreadsAgentMissing)
    assert not isinstance(missing.value, DooersThreadsAgentThreadDeleting)

    thread = await _thread(api)
    fake.delete_thread_state(thread["thread_id"])
    for call in (
        lambda: api.get_thread(EXTERNAL_KEY),
        lambda: api.list_events(thread["thread_id"]),
        lambda: _message(api, thread["thread_id"]),
        lambda: api.update_thread(thread["thread_id"], title="x"),
        lambda: api.list_runs(thread["thread_id"]),
    ):
        with pytest.raises(DooersThreadsAgentThreadDeleting) as deleting:
            await call()
        assert deleting.value.code == "THREAD_DELETING" and deleting.value.status == 409
        assert isinstance(deleting.value, DooersThreadsAgentMissing)
        assert not isinstance(deleting.value, DooersThreadsAgentNotFound)


async def test_retryable_follows_the_dooers_threads_error(api, fake):
    fake.fail_next[f"{PREFIX}/agent/threads"] = "DEPENDENCY_UNAVAILABLE"
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.list_threads()
    assert (error.value.code, error.value.status, error.value.retryable) == ("DEPENDENCY_UNAVAILABLE", 503, True)
    fake.fail_next[f"{PREFIX}/agent/threads"] = "RATE_LIMITED"
    with pytest.raises(DooersThreadsAgentApiError) as limited:
        await api.list_threads()
    assert limited.value.retryable is True
    fake.fail_next[f"{PREFIX}/agent/threads"] = "FORBIDDEN"
    with pytest.raises(DooersThreadsAgentApiError) as forbidden:
        await api.list_threads()
    assert (forbidden.value.code, forbidden.value.retryable) == ("FORBIDDEN", False)
    assert (await api.list_threads())["items"] == []


async def test_errors_never_carry_url_token_or_payload(api, fake):
    fake.fail_next[f"{PREFIX}/agent/threads"] = "FORBIDDEN"
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.create_thread(external_key=EXTERNAL_KEY, participants=[CONTACT])
    text = f"{error.value!s} {error.value!r} {error.value.args}"
    assert PHONE not in text and "127.0.0.1" not in text and "Bearer" not in text


async def test_a_server_that_is_down_is_a_retryable_transport_error(fake):
    api = DooersThreadsAgentApi(fake.config())
    await fake.stop()
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.list_threads()
    assert (error.value.code, error.value.retryable, error.value.status) == ("TRANSPORT_UNAVAILABLE", True, None)


# -- contract validation --


async def test_a_response_outside_the_contract_is_refused(api, fake):
    thread = await _thread(api)
    for path, call in (
        (f"{PREFIX}/agent/threads", lambda: api.list_threads()),
        (f"{PREFIX}/agent/threads/{thread['thread_id']}", lambda: api.get_thread(thread["thread_id"])),
        (f"{PREFIX}/agent/threads/{thread['thread_id']}/events", lambda: api.list_events(thread["thread_id"])),
        (f"{PREFIX}/agent/threads/{thread['thread_id']}/events", lambda: _message(api, thread["thread_id"])),
    ):
        fake.corrupt_next.add(path)
        with pytest.raises(DooersThreadsAgentApiError) as error:
            await call()
        assert error.value.code == "INVALID_RESPONSE" and error.value.retryable is False


async def test_a_response_of_another_scope_is_refused(api, fake):
    thread = await _thread(api)
    fake.view_organization_id = "00000000-0000-4000-8000-0000000000ff"
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.get_thread(thread["thread_id"])
    assert error.value.code == "INVALID_RESPONSE"


async def test_a_request_outside_the_contract_is_not_sent(api, fake):
    thread = await _thread(api)
    before = len(fake.requests)
    bad_calls = (
        lambda: api.publish_event(
            thread["thread_id"], actor={"kind": "self"}, payload={"content": "x"}, trigger=False, idempotency_key="k"
        ),
        lambda: api.publish_event(
            thread["thread_id"],
            actor={"kind": "self"},
            payload={"content": [{"type": "text", "text": "x"}]},
            trigger="yes",
            idempotency_key="k",
        ),
        lambda: api.publish_event(
            thread["thread_id"],
            actor={"kind": "self"},
            payload={"content": [{"type": "text", "text": "x"}]},
            trigger=False,
            idempotency_key="with space",
        ),
        lambda: api.list_events(thread["thread_id"], order="sideways"),
        lambda: api.list_threads(limit=0),
        lambda: api.create_thread(external_key="", participants=[]),
        lambda: api.add_participant(thread["thread_id"], {"kind": "robot"}),
        lambda: api.record_channel_delivery(thread_id=EXTERNAL_KEY, event_id=str(uuid4()), status="read", channel="sms"),
    )
    for call in bad_calls:
        with pytest.raises(DooersThreadsAgentApiError) as error:
            await call()
        assert error.value.code == "REQUEST_INVALID"
    assert len(fake.requests) == before


# -- idempotency --


async def test_create_thread_replays_with_the_same_key(api, fake):
    key = f"thread.create:{uuid4()}"
    first = await api.create_thread(participants=[], idempotency_key=key)
    again = await api.create_thread(participants=[], idempotency_key=key)
    assert again["thread_id"] == first["thread_id"]
    other = await api.create_thread(participants=[], idempotency_key=f"thread.create:{uuid4()}")
    assert other["thread_id"] != first["thread_id"]
    posts = [r for r in fake.requests if r.method == "POST"]
    assert [r.status for r in posts] == [201, 200, 201]
    with pytest.raises(DooersThreadsAgentApiError) as conflict:
        await api.create_thread(participants=[], title="Outro pedido", idempotency_key=key)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


async def test_the_same_external_key_gives_the_same_thread_with_other_keys(api):
    first = await _thread(api)
    again = await _thread(api)
    assert again["thread_id"] == first["thread_id"]


async def test_the_create_key_carries_neither_the_external_key_nor_the_phone(api, fake):
    await _thread(api)
    await _thread(api)
    await api.create_thread(external_key=f"{uuid4()}", participants=[CONTACT])
    keys = [r.headers["idempotency-key"] for r in fake.requests if r.method == "POST" and r.path.endswith("/agent/threads")]
    assert len(keys) == 3 and len(set(keys)) == 3
    for key in keys:
        assert PHONE not in key and "5511999990000" not in key and EXTERNAL_KEY not in key and "wa:" not in key
    # Nor does any other key the client makes.
    for request in fake.requests:
        assert "5511999990000" not in request.headers.get("idempotency-key", "")


async def test_a_caller_key_that_carries_the_external_key_or_the_phone_is_refused(api, fake):
    before = len(fake.requests)
    for key in (f"create:{EXTERNAL_KEY}", f"create:{PHONE}", "create:5511999990000"):
        with pytest.raises(ValueError):
            await api.create_thread(external_key=EXTERNAL_KEY, participants=[CONTACT], idempotency_key=key)
    assert len(fake.requests) == before


async def test_a_sdk_thread_id_makes_a_stable_non_identifying_key(api, fake):
    thread_id = str(uuid4())
    key = DooersThreadsAgentApi.thread_idempotency_key(thread_id)
    assert thread_id in key and key == DooersThreadsAgentApi.thread_idempotency_key(thread_id)
    first = await api.create_thread(external_key=thread_id, participants=[], idempotency_key=key)
    again = await api.create_thread(external_key=thread_id, participants=[], idempotency_key=key)
    assert again["thread_id"] == first["thread_id"]
    # A key that is not a UUID (an external key, a phone) is never used: a random one is made.
    random_one = DooersThreadsAgentApi.thread_idempotency_key(EXTERNAL_KEY)
    assert EXTERNAL_KEY not in random_one and PHONE not in random_one
    assert random_one != DooersThreadsAgentApi.thread_idempotency_key(EXTERNAL_KEY)


async def test_mutations_send_a_fresh_idempotency_key_by_default(api, fake):
    thread = await _thread(api)
    await api.update_thread(thread["thread_id"], title="a")
    await api.update_thread(thread["thread_id"], title="b")
    patches = [r.headers["idempotency-key"] for r in fake.requests if r.method == "PATCH"]
    assert len(set(patches)) == 2
    assert [r.body for r in fake.requests if r.method == "PATCH"] == [{"title": "a"}, {"title": "b"}]


async def test_remove_event_replays_with_the_same_key(api):
    thread = await _thread(api)
    event = (await _message(api, thread["thread_id"]))["event"]
    key = f"remove:{uuid4()}"
    first = await api.remove_event(thread["thread_id"], event["event_id"], idempotency_key=key)
    again = await api.remove_event(thread["thread_id"], event["event_id"], idempotency_key=key)
    assert again == first


# -- credentials and scope --


async def test_it_asks_for_the_threads_manage_scope_and_sends_the_bearer(fake):
    seen = []
    resolver = fake.credential

    async def spy(request):
        seen.append(request)
        return await resolver(request)

    api = DooersThreadsAgentApi(fake.config(credential_resolver=spy))
    await api.list_threads()
    await api.list_threads()
    assert [r.scopes for r in seen] == [("threads:manage",), ("threads:manage",)]
    assert (seen[0].organization_id, seen[0].instance_agent_id) == (fake.organization_id, fake.agent_id)
    request = fake.requests[0]
    assert request.headers["authorization"].startswith("Bearer ")
    assert request.headers["accept-encoding"] == "identity"
    assert "cookie" not in request.headers


async def test_a_token_without_the_scope_fails_before_any_request(fake):
    fake.token_scopes = ["deliveries:read"]
    api = DooersThreadsAgentApi(fake.config())
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.list_threads()
    assert (error.value.code, error.value.retryable) == ("CREDENTIAL_UNAVAILABLE", False)
    assert fake.requests == []


async def test_workspace_scope_is_the_token_scope_and_empty_means_personal(fake):
    api = DooersThreadsAgentApi(fake.config())
    # Personal token: "" (and None) is the personal scope; the organization may be omitted or the same.
    api.check_scope()
    api.check_scope(organization_id=fake.organization_id, workspace_id="")
    api.check_scope(workspace_id=None)
    for organization_id, workspace_id in ((None, WORKSPACE_ID), (str(uuid4()), ""), (fake.organization_id, "ws-other")):
        with pytest.raises(DooersThreadsAgentScopeError) as error:
            api.check_scope(organization_id=organization_id, workspace_id=workspace_id)
        assert isinstance(error.value, DooersThreadsConfigurationError)
    thread = await api.create_thread(workspace_id="", participants=[])
    assert thread["scope_kind"] == "personal" and thread["workspace_id"] is None
    before = len(fake.requests)
    with pytest.raises(DooersThreadsAgentScopeError):
        await api.create_thread(workspace_id=WORKSPACE_ID, participants=[])
    assert len(fake.requests) == before


async def test_a_workspace_token_creates_workspace_threads():
    server = await FakeAgentApi(workspace_id=WORKSPACE_ID).start()
    try:
        api = DooersThreadsAgentApi(server.config())
        thread = await api.create_thread(external_key="k", participants=[])
        assert (thread["scope_kind"], thread["workspace_id"]) == ("workspace", WORKSPACE_ID)
        assert json.loads(json.dumps(server.requests[-1].body)) == {
            "external_key": "k",
            "participants": [],
            "scope_kind": "workspace",
            "workspace_id": WORKSPACE_ID,
        }
        api.check_scope(workspace_id=WORKSPACE_ID.upper())
        for wrong in ("", None):
            with pytest.raises(DooersThreadsAgentScopeError):
                api.check_scope(workspace_id=wrong)
        with pytest.raises(DooersThreadsAgentScopeError):
            await api.create_thread(workspace_id="", participants=[])
    finally:
        await server.stop()


async def test_a_thread_ref_with_slash_and_plus_goes_in_the_path_encoded(api, fake):
    key = "a/b+c d:é"
    thread = await api.create_thread(external_key=key, participants=[])
    assert (await api.get_thread(key))["thread_id"] == thread["thread_id"]
    path = fake.requests[-1].path
    assert path.endswith("/agent/threads/a%2Fb%2Bc%20d%3A%C3%A9")


async def test_an_injected_transport_is_used_and_never_closed(fake):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(502, text="bad gateway")

    transport = httpx.MockTransport(handler)
    api = DooersThreadsAgentApi(fake.config(), transport=transport)
    for _ in range(2):
        with pytest.raises(DooersThreadsAgentApiError) as error:
            await api.list_threads()
        # No Public.Error body: the code is closed, and a 502 is worth repeating.
        assert (error.value.code, error.value.status, error.value.retryable) == ("INVALID_RESPONSE", 502, True)
    assert len(seen) == 2 and fake.requests == []
    assert seen[0].url.path == f"{PREFIX}/agent/threads"


async def test_an_answer_that_is_not_json_is_refused(fake):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>", headers={"content-type": "text/html"}))
    api = DooersThreadsAgentApi(fake.config(), transport=transport)
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.list_threads()
    assert (error.value.code, error.value.retryable) == ("INVALID_RESPONSE", False)


# -- slice 5c carries (5b review: Minor 1, 4, 6) --


async def test_a_generated_create_key_is_never_checked_for_identifying_text(api, fake):
    # Minor 1: a short external key (or a phone digit) appears in a random UUID. Only a key the caller chose is checked.
    for key in ("1", "a", "0", "-", "e"):
        thread = await api.create_thread(external_key=key, participants=[{"kind": "external", "channel": "whatsapp", "external_id": "5"}])
        assert thread["external_key"] == key
    with pytest.raises(ValueError):
        await api.create_thread(external_key="abc", idempotency_key="thread.create:abc")


async def test_get_event_finds_any_event_type_by_event_id_only(api, fake):
    thread = await _thread(api)
    published = await _message(api, thread["thread_id"], event_id="sdk-1")
    reasoning = fake.inject_event(thread["thread_id"], "reasoning.created", {"text": "Pensando"})
    run = fake.inject_event(
        thread["thread_id"],
        "run.updated",
        {"run_id": str(uuid4()), "agent_id": fake.agent_id, "status": "running", "trigger_event_id": published["event"]["event_id"]},
    )
    # The Dooers Threads id finds every type; the client id only an agent publication (a handler output has none).
    assert (await api.get_event(reasoning["event_id"]))["type"] == "reasoning.created"
    assert (await api.get_event(run["event_id"]))["type"] == "run.updated"
    assert (await api.get_event("sdk-1"))["event_id"] == published["event"]["event_id"]
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.get_event("sdk-2")
    # remove: content events only (``removableTypes``); a control event is VALIDATION_FAILED event_not_removable.
    assert (await api.remove_event(thread["thread_id"], reasoning["event_id"]))["type"] == "message.removed"
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.remove_event(thread["thread_id"], run["event_id"])
    assert (error.value.code, error.value.reason_code) == ("VALIDATION_FAILED", "event_not_removable")
    with pytest.raises(DooersThreadsAgentNotFound):
        await api.get_event(reasoning["event_id"])  # removed


async def test_the_fake_refuses_a_query_key_a_route_does_not_know(api, fake):
    thread = await _thread(api)
    token = (await fake.credential(None)).token
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as http:
        known = await http.get(f"{fake.base_url}/agent/threads/{thread['thread_id']}", headers=headers)
        unknown = await http.get(f"{fake.base_url}/agent/threads/{thread['thread_id']}", params={"foo": "1"}, headers=headers)
        listed = await http.get(f"{fake.base_url}/agent/threads", params={"foo": "1"}, headers=headers)
    assert (known.status_code, unknown.status_code, listed.status_code) == (200, 400, 400)


async def test_retry_after_travels_with_a_rate_limit(api, fake):
    thread = await _thread(api)
    fake.fail_next[f"{PREFIX}/agent/threads/{thread['thread_id']}"] = "RATE_LIMITED"
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await api.get_thread(thread["thread_id"])
    assert (error.value.code, error.value.retryable, error.value.retry_after_seconds) == ("RATE_LIMITED", True, 3)
    assert fake.requests[-1].status == 429


async def test_credential_unavailable_is_retryable_only_when_the_exchange_failed(fake):
    # Minor 6: the core did not answer (the resolver raised) is worth repeating; a credential that does not hold is not.
    async def down(request):
        raise ConnectionError("core down")

    with pytest.raises(DooersThreadsAgentApiError) as error:
        await DooersThreadsAgentApi(fake.config(credential_resolver=down)).list_threads()
    assert (error.value.code, error.value.retryable) == ("CREDENTIAL_UNAVAILABLE", True)

    fake.token_scopes = []  # the credential comes back without the scope: refused for good
    with pytest.raises(DooersThreadsAgentApiError) as error:
        await DooersThreadsAgentApi(fake.config()).list_threads()
    assert (error.value.code, error.value.retryable) == ("CREDENTIAL_UNAVAILABLE", False)
    assert fake.requests == []


async def test_check_scope_agent_is_this_agent_in_any_case_or_empty(api, fake):
    api.check_scope(agent_id="")
    api.check_scope(agent_id=None)
    api.check_scope(agent_id=fake.agent_id)
    api.check_scope(agent_id=fake.agent_id.upper())
    for other in ("w1", str(uuid4())):
        with pytest.raises(DooersThreadsAgentScopeError):
            api.check_scope(agent_id=other)
