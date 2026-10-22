"""Conformidade Maestro v1; não autentica JWT nem autoriza execução.

O bundle é copiado sem alterações da autoridade TS. O loader verifica todos
os bytes antes de interpretar seu validador, sem imports de repositório irmão.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import stat
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from zipfile import BadZipFile, ZipFile

SOURCE_MANIFEST_SHA256 = "753a1325e030e3222289f7a45c3a12b76461a803d61b07e12ff78d04154260c8"
SOURCE_REVISION = "sha256:ab2df747118075a26b571806bc0b9c7dd124413b22236fdf475f7bc6584bb144"
BUNDLE_MANIFEST_SHA256 = "2a5e8315e2614cad209c58d57f505ae34601093ec0004a5f8dc5932c5a0bac76"


class MaestroContractError(ValueError):
    """Erro técnico fechado, sem payload, tokens ou detalhes do validador."""


def _fail(code: str) -> None:
    raise MaestroContractError(code)


def _read_verified(directory: Path) -> dict[str, bytes]:
    if directory.is_symlink() or not directory.is_dir():
        _fail("CONTRACT_DIRECTORY_INVALID")
    source_path = directory / "source-manifest.json"
    if source_path.is_symlink():
        _fail("CONTRACT_SYMLINK_FORBIDDEN")
    source_bytes = source_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != SOURCE_MANIFEST_SHA256:
        _fail("CONTRACT_SOURCE_PIN_MISMATCH")
    source = json.loads(source_bytes)
    bundle = directory / "bundle.zip"
    if bundle.is_symlink():
        _fail("CONTRACT_SYMLINK_FORBIDDEN")
    if bundle.stat().st_size > 16 * 1024 * 1024:
        _fail("CONTRACT_BUNDLE_TOO_LARGE")
    # ZIP apenas como recurso de dados: nunca extrair/importar seu conteúdo.
    archive = ZipFile(BytesIO(bundle.read_bytes()))
    infos = archive.infolist()
    if len(infos) != len({info.filename for info in infos}):
        _fail("CONTRACT_INVENTORY_DRIFT")
    if sum(info.file_size for info in infos) > 16 * 1024 * 1024:
        _fail("CONTRACT_BUNDLE_TOO_LARGE")
    for info in infos:
        if stat.S_ISLNK(info.external_attr >> 16):
            _fail("CONTRACT_SYMLINK_FORBIDDEN")
    manifest_bytes = archive.read("bundle-manifest.json")
    if hashlib.sha256(manifest_bytes).hexdigest() != BUNDLE_MANIFEST_SHA256:
        _fail("CONTRACT_BUNDLE_PIN_MISMATCH")
    manifest = json.loads(manifest_bytes)
    if (
        source["source_revision"] != SOURCE_REVISION
        or manifest["source_revision"] != SOURCE_REVISION
        or source["bundle_manifest_sha256"] != BUNDLE_MANIFEST_SHA256
    ):
        _fail("CONTRACT_REVISION_MISMATCH")
    expected = set(manifest["artifacts"]) | {"bundle-manifest.json"}
    actual = set(archive.namelist())
    if actual != expected:
        _fail("CONTRACT_INVENTORY_DRIFT")
    contents = {}
    for name, checksum in manifest["artifacts"].items():
        if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
            _fail("CONTRACT_PATH_INVALID")
        data = archive.read(name)
        if hashlib.sha256(data).hexdigest() != checksum:
            _fail("CONTRACT_CHECKSUM_MISMATCH")
        contents[name] = data
    return contents


def verify_contract_bundle(directory: Path | None = None) -> dict[str, bytes]:
    """Retorna snapshot de bytes verificados; nenhum arquivo candidato é executado."""
    try:
        return _read_verified(directory or Path(__file__).parent)
    except MaestroContractError:
        raise
    except (OSError, ValueError, KeyError, TypeError, BadZipFile, NotImplementedError, RuntimeError):
        raise MaestroContractError("CONTRACT_BUNDLE_UNREADABLE") from None


def _dependency_check() -> None:
    for package, expected in (("jsonschema", "4.26.0"), ("ada-url", "4.0.0")):
        try:
            installed = version(package)
        except PackageNotFoundError:
            raise MaestroContractError("CONTRACT_EXTRA_REQUIRED") from None
        if installed != expected:
            _fail("CONTRACT_DEPENDENCY_VERSION_MISMATCH")


def _validator_module(contents: dict[str, bytes]) -> ModuleType:
    # Não se altera sys.path/sys.modules nem arquivos copiados. O import absoluto
    # do harness standalone recebe seu próprio módulo semantics verificado.
    semantics = ModuleType("_maestro_v1_semantics")
    exec(compile(contents["python/semantics.py"], "<maestro-v1-semantics>", "exec"), semantics.__dict__)
    validator = ModuleType("_maestro_v1_validator")

    def scoped_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "semantics":
            return semantics
        if name == "sys":
            # O CLI standalone desabilita pyc globalmente; o adapter não deve
            # mudar o processo hospedeiro. O bloco __main__ não é executado.
            return SimpleNamespace(dont_write_bytecode=True)
        return builtins.__import__(name, *args, **kwargs)

    validator.__dict__["__builtins__"] = {**vars(builtins), "__import__": scoped_import}
    exec(compile(contents["python/validate.py"], "<maestro-v1-validator>", "exec"), validator.__dict__)
    return validator


class MaestroContracts:
    """Validador estrito de contrato; só construir quando Maestro for opt-in.

    Binding/Transition/Decision são assertions para snapshots confiáveis, nunca
    uma substituição para assinatura, consulta ao core ou lock/fence no banco.
    """

    def __init__(self, directory: Path | None = None) -> None:
        contents = verify_contract_bundle(directory)
        _dependency_check()
        try:
            adapter = _validator_module(contents)
            rules = json.loads(contents["authorization-rules.json"])
            rules.update(json.loads(contents["delivery-rules.json"]))
            validator = adapter.build_validator(rules)
            self._compiled = {}
            for name, schema in json.loads(contents["schemas.json"])["schemas"].items():
                adapter.inspect_schema(schema)
                validator.check_schema(schema)
                self._compiled[name] = validator(schema, format_checker=adapter.format_checker)
            self._fixtures = json.loads(contents["fixtures.json"])["cases"]
        except Exception:
            raise MaestroContractError("CONTRACT_INITIALIZATION_FAILED") from None

    @property
    def schema_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._compiled))

    def is_valid(self, schema: str, data: Any) -> bool:
        if schema not in self._compiled:
            _fail("CONTRACT_SCHEMA_UNKNOWN")
        try:
            return self._compiled[schema].is_valid(data)
        except Exception:
            # Objetos Python que não vieram de JSON podem conter ciclos ou tipos
            # inválidos. Não expor repr/ValidationError com conteúdo do caller.
            return False

    def validate(self, schema: str, data: Any) -> Any:
        """Valida e devolve cópia; não normaliza escopo/fence/números silenciosamente."""
        if not self.is_valid(schema, data):
            _fail("CONTRACT_INVALID")
        return deepcopy(data)

    def fixture_results(self) -> list[tuple[str, str, bool]]:
        """Classificação pública sintética para confronto com o produtor TS."""
        results = []
        for case in self._fixtures:
            valid = self.is_valid(case["schema"], case["data"])
            if valid != case["valid"]:
                _fail("CONTRACT_FIXTURE_DIVERGENCE")
            results.append((case["id"], case["schema"], valid))
        return results
