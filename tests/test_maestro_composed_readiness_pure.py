"""Pure composition checks: no ASGI, services, installed package or database."""

from dataclasses import replace
from itertools import permutations
from uuid import uuid4

import pytest

from dooers.agents.server.maestro.journal_runtime import invocation_fingerprint
from dooers.agents.server.maestro.journal_store import JournalError
from dooers.agents.server.maestro.journal_transport import JournalServiceKey, canonical, sign_request, verify_request
from dooers.agents.server.protocol.maestro.journal import ExecutionAuthority, JournalScope
from tests.test_maestro_terminal_replay import private_key as private_key
from tests.test_maestro_terminal_replay import setup

OPERATIONS = ("projection", "recovery", "approval_outcome", "forms_readiness", "approval_readiness")


def key(operation):
    return JournalServiceKey("same-key", b"x" * 32, "http://127.0.0.1:9000/internal/readiness", operation, str(uuid4()), None, str(uuid4()))


@pytest.mark.parametrize("operation", OPERATIONS)
def test_closed_direction_accepts_its_own_actual_hmac(operation):
    bound = key(operation)
    body = canonical({"safe": True})
    verify_request(bound, body, sign_request(bound, body))


@pytest.mark.parametrize("source,target", list(permutations(OPERATIONS, 2)))
def test_actual_hmac_never_crosses_private_operation_direction(source, target):
    bound = key(source)
    body = canonical({"safe": True})
    with pytest.raises(JournalError, match="JOURNAL_UNAUTHENTICATED"):
        verify_request(replace(bound, operation=target), body, sign_request(bound, body))


@pytest.mark.parametrize("change", ["generation", "phase_unknown", "receipt_missing", "fence", "fingerprint"])
async def test_composed_current_journal_refuses_stale_terminal_replay_without_remote_io(private_key, monkeypatch, change):
    adapter, envelope, _headers, db, result, calls, credentials = setup(private_key, monkeypatch)
    journal = adapter._lease_gate.journal
    scope = JournalScope(**{k: envelope[k] for k in (
        "organization_id", "workspace_id", "thread_id", "recipient_participant_id", "instance_agent_id"
    )})
    authority = ExecutionAuthority(envelope["execution_id"], envelope["runtime_id"], envelope["lease_fence"], envelope["deadline_at"])
    args = (scope, envelope["delivery_id"], envelope["phase_id"], authority)
    kwargs = {"context_fingerprint": invocation_fingerprint(envelope), "retry_policy": "journal_only"}
    confirmed = await journal.read_confirmed_invocation(*args, **kwargs)
    assert confirmed.confirmed and confirmed.response == result
    if change == "generation":
        db.invalid_generation_after_remote = True
    elif change == "phase_unknown":
        db.phase_state = "unknown"
    elif change == "receipt_missing":
        db.receipt_exists = False
    elif change == "fence":
        db.change["maestro_fence"] = int(envelope["lease_fence"]) + 1
    else:
        db.change["context_fingerprint"] = "0" * 64
    with pytest.raises(JournalError):
        await journal.read_confirmed_invocation(*args, **kwargs)
    assert calls == [] and credentials == []
    assert all("SELECT" in statement for statement in db.statements)
