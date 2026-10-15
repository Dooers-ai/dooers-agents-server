"""Negativas locais; sem PG/provider pago ou cálculo de preço."""

import json

import httpx
import pytest

from dooers.agents.server.maestro.commercial import REASONS, MaestroCommercialBlocked, commercial_denial
from dooers.agents.server.maestro.journal_store import JournalError
from dooers.agents.server.maestro.usage import JournalUsage, LocalFinancialClient
from tests.test_maestro_executor import execute_fixture, setup


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    from dooers.agents.server.protocol.maestro import MaestroContracts

    return MaestroContracts()


def denial(reason="insufficient_balance"):
    return {"allowed": False, "reason": reason, "retryable": False, "policy_version": "sandbox-v1", "retry_after": None, "sandbox": True}


@pytest.mark.parametrize("reason", sorted(REASONS))
def test_allowlisted_terminal_denial_without_secret_or_price(reason):
    block = commercial_denial(denial(reason))
    assert block.reason_code == reason and str(block) == reason


@pytest.mark.parametrize(
    "patch",
    [
        {"reason": "Bearer-secret-token"},
        {"reason": "constructor"},
        {"allowed": 0},
        {"retryable": True},
        {"retry_after": 5},
        {"sandbox": False},
        {"policy_version": "production"},
        {"price": "999"},
        {"decision_id": "secret"},
    ],
)
def test_invalid_denial_is_not_a_balance_error(patch):
    with pytest.raises(JournalError) as error:
        commercial_denial({**denial(), **patch})
    assert not isinstance(error.value, MaestroCommercialBlocked)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("reason", sorted(REASONS))
async def test_usage_denial_is_terminal_before_any_journal_or_provider(reason):
    # decision performs its authenticated-result validation before opening SQL.
    with pytest.raises(MaestroCommercialBlocked) as error:
        await JournalUsage.decision(None, None, denial(reason))
    assert error.value.reason_code == reason


@pytest.mark.parametrize(
    "reason,partial", [("insufficient_balance", False), ("org_limit_exceeded", True), ("billing_policy_unavailable", False)]
)
async def test_executor_completes_failed_once_and_preserves_partial_output(private_key, contracts, reason, partial):
    async with setup(private_key, contracts) as fixture:
        calls = 0

        async def handler(on, send, *_args):
            nonlocal calls
            calls += 1
            if partial:
                yield send.text("Resposta já persistida")
            raise commercial_denial(denial(reason))

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "failed"
        assert calls == 1 and len(fixture.completions) == 1
        assert fixture.completions[0]["reason_code"] == reason
        assert len(fixture.outputs) == int(partial)
        assert "price" not in response.text and "secret" not in response.text


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (409, {"error": "decision_not_executable", "commercial_block": denial("billing_policy_unavailable")}, "billing_policy_unavailable"),
        (409, {"error": "reconciliation_required", "commercial_block": denial("billing_policy_unavailable")}, "billing_policy_unavailable"),
        (409, {"error": "secret-token"}, "USAGE_UNAVAILABLE"),
        (503, {"error": "insufficient_balance"}, "USAGE_UNAVAILABLE"),
        (403, {"error": "insufficient_balance"}, "USAGE_UNAVAILABLE"),
    ],
)
async def test_authenticated_start_envelope_distinguishes_commercial_from_transport(monkeypatch, status, payload, expected):
    original = httpx.AsyncClient

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(payload).encode()

    def create(**kwargs):
        return original(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, headers={"content-type": "application/json"}, stream=Stream())
            ),
        )

    monkeypatch.setattr(httpx, "AsyncClient", create)
    finance = LocalFinancialClient("http://127.0.0.1:12345/api/v2/maestro/usage-sandbox/", lambda _: "x" * 43)
    with pytest.raises(JournalError) as error:
        await finance.call("start", {"decision_id": "00000000-0000-4000-8000-000000000001"})
    assert str(error.value) == expected
    assert isinstance(error.value, MaestroCommercialBlocked) == (status == 409 and "commercial_block" in payload)
