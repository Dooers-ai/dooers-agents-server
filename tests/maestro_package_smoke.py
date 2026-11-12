"""Executar com Python do wheel instalado, em cwd fora de ambos os checkouts."""

import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

import dooers.agents.server.protocol.maestro.contracts as module
from dooers.agents.server import AgentConfig, AgentServer
from dooers.agents.server.protocol.maestro import MaestroContracts

assert AgentConfig and AgentServer
assert "/site-packages/" in module.__file__
assert "/src/" not in module.__file__
assert not any("dooers-service-maestro" in path or "/.worktrees/" in path for path in sys.path)
contracts = MaestroContracts()
results = contracts.fixture_results()
assert len(contracts.schema_names) == 161
assert len(results) == 746
assert sum(row[2] for row in results) == 329
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "69055310d13646739f421132c8ca0875d8b22b89e5cb79979a75dbc5f7b9a9ed"
)
assert len(sys.argv) == 2, "Pass the reviewed consumer fixture JSON path"
fixture_bytes = Path(sys.argv[1]).read_bytes()
pin = json.loads((Path(module.__file__).parent / "consumer-manifest.json").read_bytes())
assert hashlib.sha256(fixture_bytes).hexdigest() == pin["consumer_fixture_sha256"]
for case in json.loads(fixture_bytes)["cases"]:
    valid = contracts.is_valid(case["schema"], case["data"])
    assert valid == case["valid"]
    results.append((case["id"], case["schema"], valid))
assert len(results) == 780
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "6c3b3988304bbaf0f81e2dc56a33a1991684818f562697925ba3c863dacb67f3"
)
assert not any(Path(module.__file__).parent.glob("bundle/**/__pycache__"))
print(json.dumps({"installed_version": version("dooers-agents-server"), "schemas": 161, "fixtures": 780, "legacy_imports": True}))
