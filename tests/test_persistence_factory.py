"""AgentServer selects the persistence backend from config.database_type."""

from unittest.mock import patch

from dooers.agents.server import server as srv
from dooers.agents.server.config import AgentConfig
from dooers.agents.server.persistence.postgres import PostgresPersistence


def test_build_persistence_selects_dooers(monkeypatch):
    monkeypatch.setenv("AGENT_DATABASE_INSTANCE", "inst-uri")
    cfg = AgentConfig(
        database_type="dooers", database_user="tenant-x@p.iam", database_name="agent_x"
    )
    with patch("dooers.agents.server.persistence.dooers.DooersPersistence") as DP:
        DP.return_value = "DOOERS_PERSISTENCE"
        p = srv._build_persistence(cfg)
    assert p == "DOOERS_PERSISTENCE"
    kwargs = DP.call_args.kwargs
    assert kwargs["instance_uri"] == "inst-uri"
    assert kwargs["user"] == "tenant-x@p.iam"
    assert kwargs["database"] == "agent_x"


def test_build_persistence_defaults_to_postgres():
    cfg = AgentConfig(database_type="postgres", database_name="agent_x")
    assert isinstance(srv._build_persistence(cfg), PostgresPersistence)
