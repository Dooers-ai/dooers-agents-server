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
assert len(results) == 1091
assert sum(row[2] for row in results) == 477
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "d85fe15bb32ff5ab70887de16889ebe19b386151a2fe5322296afecb9c7ae866"
)
assert len(sys.argv) == 2, "Pass the reviewed consumer fixture JSON path"
fixture_bytes = Path(sys.argv[1]).read_bytes()
pin = json.loads((Path(module.__file__).parent / "consumer-manifest.json").read_bytes())
assert hashlib.sha256(fixture_bytes).hexdigest() == pin["consumer_fixture_sha256"]
for case in json.loads(fixture_bytes)["cases"]:
    valid = contracts.is_valid(case["schema"], case["data"])
    assert valid == case["valid"]
    results.append((case["id"], case["schema"], valid))
assert len(results) == 1125
assert hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == (
    "9983228fa13e62c382ce95ed501849c46bcced8c1d45e1a468ad731e1efbbc5b"
)
assert not any(Path(module.__file__).parent.glob("bundle/**/__pycache__"))
print(json.dumps({"installed_version": version("dooers-agents-server"), "schemas": 191, "fixtures": 1125, "legacy_imports": True}))
