"""JWT RS256 fechado: somente keyset local, sem URLs/algoritmos controlados pelo JWT."""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .config import MaestroConfigurationError


def load_public_keys(pems: Mapping[str, bytes]) -> dict[str, Any]:
    try:
        if version("cryptography") != "48.0.0":
            raise MaestroConfigurationError("MAESTRO_CRYPTO_VERSION_MISMATCH")
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except (ImportError, PackageNotFoundError):
        raise MaestroConfigurationError("MAESTRO_EXTRA_REQUIRED") from None
    try:
        keys = {kid: load_pem_public_key(pem) for kid, pem in pems.items()}
        if any(not isinstance(key, RSAPublicKey) or not 2048 <= key.key_size <= 8192 for key in keys.values()):
            raise ValueError
        return keys
    except (ValueError, TypeError):
        raise MaestroConfigurationError("MAESTRO_KEYSET_INVALID") from None


def strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("constant")

    # UTF-8 estrito, sem BOM, duplicate members, NaN ou Infinity.
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)


def _decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise ValueError
    return raw


def verify_jwt(token: str, *, typ: str, keys: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    if not isinstance(token, str) or not 1 <= len(token) <= 16384:
        raise ValueError
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError
    header = strict_json(_decode(parts[0]))
    if (
        not isinstance(header, dict)
        or set(header) != {"alg", "typ", "kid"}
        or header["alg"] != "RS256"
        or header["typ"] != typ
        or not isinstance(header["kid"], str)
        or header["kid"] not in keys
    ):
        raise ValueError
    # Claims não são tratados como confiáveis antes desta verificação.
    keys[header["kid"]].verify(_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    claims = strict_json(_decode(parts[1]))
    if not isinstance(claims, dict):
        raise ValueError
    return header, claims


def renewed_capability(token: str, previous: dict[str, Any], *, keys: Mapping[str, Any], contracts: Any) -> dict[str, Any]:
    """Verifica renovação assinada; DTO interno jamais concede autoridade sozinho."""
    _, claims = verify_jwt(token, typ="maestro-execution-capability+jwt", keys=keys)
    contracts.validate("Delivery.ExecutionCapabilityClaims", claims)
    stable = {key: value for key, value in previous.items() if key not in {"jti", "iat", "exp"}}
    if (
        any(claims.get(key) != value for key, value in stable.items())
        or claims["jti"] == previous["jti"]
        or claims["iat"] < previous["iat"]
        or claims["exp"] <= previous["exp"]
        or not claims["iat"] <= time.time() < claims["exp"]
    ):
        raise ValueError("CAPABILITY_RENEWAL_INVALID")
    return claims
