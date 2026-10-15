from __future__ import annotations

import hashlib
import json
import shutil
import stat
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from dooers.agents.server.protocol.maestro import (
    MaestroContractError,
    MaestroContracts,
    callback_idempotency_key,
    stable_identity,
    verify_contract_bundle,
)
from dooers.agents.server.protocol.maestro import contracts as contract_module
from dooers.agents.server.protocol.maestro.journal import PrivateEffectResult

ROOT = Path(contract_module.__file__).parent
CONTENTS = verify_contract_bundle()
BASELINE = json.loads(CONTENTS["fixtures.json"])["cases"]
EXTRA_BYTES = (Path(__file__).parent / "fixtures/maestro-server-consumer.json").read_bytes()
EXTRAS = json.loads(EXTRA_BYTES)["cases"]
CASES = BASELINE + EXTRAS
DELIVERY = "00000003-0000-4000-8000-000000000000"
PHASE = "000000ab-0000-4000-8000-000000000000"


@pytest.fixture(scope="module")
def contracts():
    return MaestroContracts()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_same_corpus_as_typescript(contracts, case):
    assert contracts.is_valid(case["schema"], case["data"]) is case["valid"]


def test_consumer_classification_and_manifest(contracts):
    results = [(case["id"], case["schema"], contracts.is_valid(case["schema"], case["data"])) for case in CASES]
    assert len(contracts.schema_names) == 142
    assert len(results) == len({case["id"] for case in CASES}) == 622
    assert sum(result[2] for result in results) == 252
    checksum = hashlib.sha256(json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert checksum == "5e7ecace305b24806660dc63b9eebd64c61a0ba943c800dea2eae53e2b08e9f3"
    pin = json.loads((ROOT / "consumer-manifest.json").read_bytes())
    assert pin["classification"]["sha256"] == checksum
    assert hashlib.sha256(EXTRA_BYTES).hexdigest() == pin["consumer_fixture_sha256"]
    assert hashlib.sha256((ROOT / "bundle.zip").read_bytes()).hexdigest() == pin["bundle_zip_sha256"]
    assert pin["source_manifest_sha256"] == contract_module.SOURCE_MANIFEST_SHA256
    assert pin["source_revision"] == contract_module.SOURCE_REVISION
    assert pin["bundle_manifest_sha256"] == contract_module.BUNDLE_MANIFEST_SHA256
    assert len(contracts.fixture_results()) == 598


def test_isolated_loader_does_not_change_process_or_write_bytecode(contracts):
    sentinel = object()
    before = (sys.dont_write_bytecode, list(sys.path), sys.modules.get("semantics", sentinel))
    MaestroContracts()
    assert before == (sys.dont_write_bytecode, list(sys.path), sys.modules.get("semantics", sentinel))
    with ZipFile(ROOT / "bundle.zip") as archive:
        assert not any("__pycache__" in name for name in archive.namelist())


def test_validation_errors_do_not_include_private_content(contracts):
    with pytest.raises(MaestroContractError, match="^CONTRACT_INVALID$"):
        contracts.validate("Delivery.DispatchEnvelope", {"password": "secret-synthetic"})
    with pytest.raises(MaestroContractError, match="^CONTRACT_SCHEMA_UNKNOWN$"):
        contracts.validate("private-data-as-name", {})
    private = PrivateEffectResult(b"secret-result", b"private-provider-receipt")
    assert "secret-result" not in repr(private)
    assert "private-provider-receipt" not in repr(private)


def test_validation_returns_independent_copy(contracts):
    original = next(case["data"] for case in CASES if case["schema"] == "Delivery.DispatchEnvelope" and case["valid"])
    validated = contracts.validate("Delivery.DispatchEnvelope", original)
    validated["context_cursor"] = "mutated-after-validation"
    assert original["context_cursor"] != validated["context_cursor"]


def test_stable_keys_survive_retry_and_change_only_with_phase(contracts):
    first = stable_identity(contracts, DELIVERY, "initial", 7)
    retried = stable_identity(contracts, DELIVERY, "initial", 7)
    resumed = stable_identity(contracts, DELIVERY, PHASE, 7)
    assert first == retried
    assert first.output_id == resumed.output_id == "output:7"
    assert first.operation_id == resumed.operation_id == "effect:7"
    assert first.effect_key == f"maestro:v1:effect:{DELIVERY}:initial:7"
    assert resumed.effect_key == f"maestro:v1:effect:{DELIVERY}:{PHASE}:7"
    assert first.effect_key != resumed.effect_key
    with pytest.raises(FrozenInstanceError):
        first.ordinal = 8


@pytest.mark.parametrize("ordinal", [-1, True, 0.5, "0", 9007199254740992])
def test_rejects_invalid_semantic_ordinals(contracts, ordinal):
    with pytest.raises(MaestroContractError, match="^CONTRACT_INVALID$"):
        stable_identity(contracts, DELIVERY, "initial", ordinal)


@pytest.mark.parametrize("operation,ordinal", [("output", 0), ("suspend", 5), ("complete", None)])
def test_callback_keys(contracts, operation, ordinal):
    expected = f"{operation}:{DELIVERY}:initial" + (f":{ordinal}" if ordinal is not None else "")
    assert callback_idempotency_key(contracts, operation, DELIVERY, "initial", ordinal) == expected


@pytest.mark.parametrize("operation,ordinal", [("output", None), ("suspend", None), ("complete", 0), ("unknown", 0)])
def test_invalid_callback_keys(contracts, operation, ordinal):
    with pytest.raises(MaestroContractError, match="^CONTRACT_INVALID$"):
        callback_idempotency_key(contracts, operation, DELIVERY, "initial", ordinal)


@pytest.fixture
def candidate(tmp_path):
    shutil.copy2(ROOT / "source-manifest.json", tmp_path)
    shutil.copy2(ROOT / "bundle.zip", tmp_path)
    return tmp_path


def rewrite_archive(path, mutation):
    with ZipFile(path / "bundle.zip") as archive:
        items = {name: archive.read(name) for name in archive.namelist()}
    mutation(items)
    with ZipFile(path / "bundle.zip", "w", compression=ZIP_DEFLATED) as archive:
        for name, data in items.items():
            archive.writestr(name, data)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda items: items.pop("schemas.json"), "CONTRACT_INVENTORY_DRIFT"),
        (lambda items: items.__setitem__("extra.json", b"{}"), "CONTRACT_INVENTORY_DRIFT"),
        (lambda items: items.__setitem__("../../outside", b"{}"), "CONTRACT_INVENTORY_DRIFT"),
        (lambda items: items.__setitem__("schemas.json", b"{}"), "CONTRACT_CHECKSUM_MISMATCH"),
        (lambda items: items.__setitem__("bundle-manifest.json", b"{}"), "CONTRACT_BUNDLE_PIN_MISMATCH"),
        (lambda items: items.__setitem__("python/semantics.py", b"raise RuntimeError('unsafe-code-ran')"), "CONTRACT_CHECKSUM_MISMATCH"),
    ],
)
def test_rejects_bundle_before_executing_candidate(candidate, mutation, code):
    rewrite_archive(candidate, mutation)
    with pytest.raises(MaestroContractError, match=f"^{code}$"):
        MaestroContracts(candidate)


def test_cannot_self_sign_changed_manifest(candidate):
    (candidate / "source-manifest.json").write_text('{"source_revision":"changed"}')
    with pytest.raises(MaestroContractError, match="^CONTRACT_SOURCE_PIN_MISMATCH$"):
        MaestroContracts(candidate)


def test_symlink_bundle_is_rejected(candidate):
    (candidate / "bundle.zip").unlink()
    (candidate / "bundle.zip").symlink_to(ROOT / "bundle.zip")
    with pytest.raises(MaestroContractError, match="^CONTRACT_SYMLINK_FORBIDDEN$"):
        MaestroContracts(candidate)


def test_archive_symlink_is_rejected(candidate):
    with ZipFile(candidate / "bundle.zip", "a") as archive:
        info = ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "schemas.json")
    with pytest.raises(MaestroContractError, match="^CONTRACT_SYMLINK_FORBIDDEN$"):
        MaestroContracts(candidate)


def test_duplicate_archive_entry_is_rejected(candidate):
    with ZipFile(candidate / "bundle.zip", "a") as archive, pytest.warns(UserWarning):
        archive.writestr("schemas.json", b"{}")
    with pytest.raises(MaestroContractError, match="^CONTRACT_INVENTORY_DRIFT$"):
        MaestroContracts(candidate)


def test_empty_bundle_is_rejected(candidate):
    (candidate / "bundle.zip").write_bytes(b"")
    with pytest.raises(MaestroContractError, match="^CONTRACT_BUNDLE_UNREADABLE$"):
        MaestroContracts(candidate)


def test_missing_bundle_is_rejected(candidate):
    (candidate / "bundle.zip").unlink()
    with pytest.raises(MaestroContractError, match="^CONTRACT_BUNDLE_UNREADABLE$"):
        MaestroContracts(candidate)


def test_unsupported_zip_compression_returns_closed_error(candidate):
    # Corrompe só o método de compressão nos headers ZIP local/central.
    data = bytearray((candidate / "bundle.zip").read_bytes())
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        start = 0
        while (index := data.find(signature, start)) >= 0:
            data[index + offset : index + offset + 2] = (99).to_bytes(2, "little")
            start = index + 4
    (candidate / "bundle.zip").write_bytes(data)
    with pytest.raises(MaestroContractError, match="^CONTRACT_BUNDLE_UNREADABLE$"):
        MaestroContracts(candidate)


def test_unpinned_dependency_is_rejected(monkeypatch):
    monkeypatch.setattr(contract_module, "version", lambda _: "0.0.0")
    with pytest.raises(MaestroContractError, match="^CONTRACT_DEPENDENCY_VERSION_MISMATCH$"):
        MaestroContracts()
