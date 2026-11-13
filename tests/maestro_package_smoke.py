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
assert len(contracts.schema_names) == 191
assert len(results) == 1065
assert sum(row[2] for row in results) == 451
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "ec9fd8f3a71f36fa128c67af691a50741521d0d64d7eb081c3bc4a933ab7732a"
)
assert len(sys.argv) == 2, "Pass the reviewed consumer fixture JSON path"
fixture_bytes = Path(sys.argv[1]).read_bytes()
pin = json.loads((Path(module.__file__).parent / "consumer-manifest.json").read_bytes())
assert hashlib.sha256(fixture_bytes).hexdigest() == pin["consumer_fixture_sha256"]
for case in json.loads(fixture_bytes)["cases"]:
    valid = contracts.is_valid(case["schema"], case["data"])
    assert valid == case["valid"]
    results.append((case["id"], case["schema"], valid))
assert len(results) == 1099
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "5222b3e3586a1d467691a703223b4e2c84039f4e7e9d40e409bd114c98d08dea"
)
assert not any(Path(module.__file__).parent.glob("bundle/**/__pycache__"))
print(json.dumps({"installed_version": version("dooers-agents-server"), "schemas": 191, "fixtures": 1099, "legacy_imports": True}))
