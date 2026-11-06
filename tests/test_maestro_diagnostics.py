import asyncio
import json
from uuid import uuid4

import pytest

from dooers.agents.server.maestro.diagnostics import diagnostic_stage


def test_failure_logs_type_and_ids_without_sensitive_data(capsys):
    execution = str(uuid4())
    with pytest.raises(TimeoutError):
        with diagnostic_stage("context.snapshot", {"execution_id": execution, "delivery_id": "secret-token", "token": "secret-token"}):
            raise TimeoutError("Authorization: Bearer secret-token")
    output = capsys.readouterr().err
    record = json.loads(output)
    assert record["execution_id"] == execution
    assert record["exception_type"] == "TimeoutError"
    assert record["outcome"] == "error"
    assert record["duration_ms"] >= 0
    assert "secret-token" not in output
    assert "Authorization" not in output
    assert "delivery_id" not in record


def test_logging_failure_does_not_change_execution(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("log unavailable")

    monkeypatch.setattr("builtins.print", broken)
    with diagnostic_stage("journal.reserve", {}):
        pass
    with pytest.raises(asyncio.CancelledError):
        with diagnostic_stage("journal.reserve", {}):
            raise asyncio.CancelledError()
