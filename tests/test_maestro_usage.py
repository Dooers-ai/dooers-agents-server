"""Corpus E10-01 aceito contra o novo adapter instalado; sem provider pago."""

import copy
import json
from pathlib import Path

import pytest

from dooers.agents.server.maestro.journal_store import JournalError
from dooers.agents.server.maestro.usage import (
    JournalUsage,
    LocalFinancialClient,
    LocalUsageSink,
    UsageOperation,
    UsageResult,
    parse_usage,
    usage_hash,
)

CASES = json.loads((Path(__file__).parent / "fixtures" / "maestro_usage_contract.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_accepted_e1001_usage_corpus(case):
    if case["valid"]:
        fact = parse_usage(case["data"])
        assert fact["content_sha256"] == usage_hash(fact)
    else:
        with pytest.raises((JournalError, ValueError, TypeError)):
            parse_usage(case["data"])


@pytest.mark.parametrize("field", ["prompt", "response", "token", "url", "arguments", "retry_count", "worker_id"])
def test_private_body_and_transport_fields_never_enter_fact(field):
    fact = copy.deepcopy(CASES[0]["data"])
    fact[field] = "private sentinel"
    fact["content_sha256"] = usage_hash(fact)
    with pytest.raises(JournalError):
        parse_usage(fact)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:12/api/v2/maestro/usage-sandbox/",
        "http://localhost:12/api/v2/maestro/usage-sandbox/",
        "http://127.0.0.1:12/api/v2/maestro/usage-sandbox/?secret=x",
        "http://a:b@127.0.0.1:12/api/v2/maestro/usage-sandbox/",
        "http://127.0.0.1:12/redirect/",
        "http://127.0.0.1:12/api/v2/maestro/usage-sandbox/#x",
    ],
)
def test_financial_transport_only_accepts_exact_trusted_local_endpoint(url):
    with pytest.raises(JournalError):
        LocalFinancialClient(url, lambda _: "x" * 43)


def test_private_repr_and_no_ram_fallback():
    assert "private sentinel" not in repr(UsageOperation(CASES[0]["data"]["provider_operation_id"], {"x": "private sentinel"}))
    assert "private sentinel" not in repr(UsageResult(CASES[0]["data"]["provider_operation_id"], "private sentinel"))
    with pytest.raises(JournalError):
        JournalUsage(object())
    with pytest.raises(JournalError):
        LocalUsageSink("http://127.0.0.1:12/internal/journal", lambda: "x" * 43)
