"""Rodar com python -I após instalar wheel sem nenhum extra."""

import json
import sys
from importlib.util import find_spec
from pathlib import Path

from dooers.agents.server import AgentConfig, AgentServer, MaestroConfig, MaestroDeliveryAdapter

assert "site-packages" in str(Path(sys.modules[AgentConfig.__module__].__file__))
for package in ("cryptography", "jsonschema", "ada_url", "pytest", "uvicorn"):
    assert find_spec(package) is None, package
config = AgentConfig(database_type="postgres")
assert config.thread_management == "agent" and config.maestro is None
server = AgentServer(config)
assert server._persistence is None
assert MaestroConfig and MaestroDeliveryAdapter
print(json.dumps({"installed_legacy_imports": True, "maestro_extras_absent": True, "no_database_connection": True}))
