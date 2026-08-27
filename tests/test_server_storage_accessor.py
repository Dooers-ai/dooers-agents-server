from dooers.agents.server import AgentConfig, AgentServer
from dooers.agents.server.storage.object_store import ObjectStore


def test_agent_server_exposes_storage():
    srv = AgentServer(AgentConfig(database_type="postgres", storage_type="dooers",
                                  gcp_storage_bucket="b", dooers_storage_prefix="agents/o1/"))
    assert isinstance(srv.storage, ObjectStore)
