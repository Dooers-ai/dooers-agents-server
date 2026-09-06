"""WhatsApp peer_message config defaults."""

from dooers.agents.server.config import AgentConfig


def test_whatsapp_peer_message_default_register():
    cfg = AgentConfig(database_type="postgres", database_name="t")
    assert cfg.whatsapp_peer_message == "register"
    assert cfg.dooers_whatsapp_service is False
