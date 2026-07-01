"""AgentConfig support for the Dooers-managed (AlloyDB) database."""

from dooers.agents.server.config import AgentConfig

INSTANCE = "projects/dooers-agents/locations/southamerica-east1/clusters/c/instances/i"


def test_database_instance_uri_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_DATABASE_INSTANCE", INSTANCE)
    cfg = AgentConfig(database_type="dooers")
    assert cfg.database_instance_uri == INSTANCE


def test_database_instance_uri_defaults_empty(monkeypatch):
    monkeypatch.delenv("AGENT_DATABASE_INSTANCE", raising=False)
    cfg = AgentConfig(database_type="postgres")
    assert cfg.database_instance_uri == ""


def test_database_type_dooers_is_accepted():
    # `dooers` is a valid backend value (alongside postgres/cosmos).
    assert AgentConfig(database_type="dooers").database_type == "dooers"
