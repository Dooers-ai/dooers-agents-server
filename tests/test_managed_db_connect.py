"""DooersPersistence connects to AlloyDB via IAM (no password)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dooers.agents.server.persistence.dooers import DooersPersistence

INSTANCE = "projects/dooers-agents/locations/southamerica-east1/clusters/c/instances/i"
DOOERS = "dooers.agents.server.persistence.dooers"


@pytest.mark.asyncio
async def test_connect_uses_alloydb_iam_no_password():
    fake_connector = MagicMock()
    fake_connector.connect = AsyncMock(return_value=object())
    fake_connector.close = AsyncMock()
    created: dict = {}

    async def fake_create_pool(*args, **kwargs):
        created["kwargs"] = kwargs
        # asyncpg invokes the `connect` factory with its own kwargs (e.g. loop=...);
        # the factory must tolerate them.
        await kwargs["connect"](loop=object())
        return MagicMock()

    with (
        patch(f"{DOOERS}.ALLOYDB_AVAILABLE", True),
        patch(f"{DOOERS}.AsyncConnector", return_value=fake_connector),
        patch(f"{DOOERS}.IPTypes", MagicMock()),
        patch(f"{DOOERS}.asyncpg.create_pool", side_effect=fake_create_pool),
    ):
        p = DooersPersistence(
            instance_uri=INSTANCE, user="tenant-84601f39ecd0@dooers-agents.iam", database="agent_x"
        )
        await p.connect()

    call = fake_connector.connect.await_args
    assert call.args[0] == INSTANCE
    assert call.args[1] == "asyncpg"
    assert call.kwargs["user"] == "tenant-84601f39ecd0@dooers-agents.iam"
    assert call.kwargs["db"] == "agent_x"
    assert call.kwargs["enable_iam_auth"] is True
    # No password anywhere in the pool creation.
    assert "password" not in created["kwargs"]


def test_requires_alloydb_connector_installed():
    with patch(f"{DOOERS}.ALLOYDB_AVAILABLE", False):
        with pytest.raises(ImportError):
            DooersPersistence(instance_uri=INSTANCE, user="u", database="agent_x")


def test_requires_instance_user_and_db():
    with patch(f"{DOOERS}.ALLOYDB_AVAILABLE", True):
        with pytest.raises(ValueError):
            DooersPersistence(instance_uri="", user="u", database="agent_x")
