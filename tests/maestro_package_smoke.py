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
assert len(results) == 1067
assert sum(row[2] for row in results) == 452
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "2e42144357dbd44ab92813a39b0df55de3c8a5731606149a72cdf35e71e13a71"
)
assert len(sys.argv) == 2, "Pass the reviewed consumer fixture JSON path"
fixture_bytes = Path(sys.argv[1]).read_bytes()
pin = json.loads((Path(module.__file__).parent / "consumer-manifest.json").read_bytes())
assert hashlib.sha256(fixture_bytes).hexdigest() == pin["consumer_fixture_sha256"]
for case in json.loads(fixture_bytes)["cases"]:
    valid = contracts.is_valid(case["schema"], case["data"])
    assert valid == case["valid"]
    results.append((case["id"], case["schema"], valid))
assert len(results) == 1101
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "c547407c6c772f7da4fdaec60929152de14b06b7c30ba71783617cc0246b9e39"
)
assert not any(Path(module.__file__).parent.glob("bundle/**/__pycache__"))
print(json.dumps({"installed_version": version("dooers-agents-server"), "schemas": 191, "fixtures": 1101, "legacy_imports": True}))
