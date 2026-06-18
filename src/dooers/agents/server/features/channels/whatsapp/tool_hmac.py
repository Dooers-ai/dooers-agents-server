"""Dooers tools ↔ agent HMAC: service_secrets (``dooers_whatsapp_*``) for inbound verify and outbound sign."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from dooers.agents.server.persistence.base import Persistence

__all__ = [
    "dooers_whatsapp_hmac_key_fingerprint",
    "parse_dooers_whatsapp_instance_hmac_map",
    "primary_dooers_whatsapp_service_secret",
    "resolve_dooers_whatsapp_outbound_message_hmac",
    "verify_dooers_whatsapp_tool_inbound_signature",
    "verify_dooers_whatsapp_tool_inbound_with_persistence",
]

logger = logging.getLogger("dooers.whatsapp.tool_hmac")


def dooers_whatsapp_hmac_key_fingerprint(secret: str) -> str:
    """Short SHA-256 prefix for a secret (compare across logs; not reversible)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def parse_dooers_whatsapp_instance_hmac_map(raw: Any) -> dict[str, str] | None:
    """Value of ``dooers_whatsapp_instance_hmac_json`` may be a JSON string or a dict (e.g. JSONB + asyncpg)."""
    if isinstance(raw, dict):
        out: dict[str, str] = {}
        for k, v in raw.items():
            key = str(k).strip() if k is not None else ""
            if not key:
                continue
            if isinstance(v, str) and (sv := v.strip()):
                out[key] = sv
            elif v is not None:
                s = str(v).strip()
                if s:
                    out[key] = s
        return out if out else None
    if isinstance(raw, str) and raw.strip():
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parse_dooers_whatsapp_instance_hmac_map(m)
    return None


def primary_dooers_whatsapp_service_secret(raw: str) -> str:
    """First comma-separated segment of ``dooers_whatsapp_service_secret`` (rotation / default signing)."""
    s = (raw or "").strip()
    if not s:
        return ""
    return s.split(",")[0].strip()


def _inbound_verification_secrets(
    services_secrets: dict[str, Any], instance_id: str | None
) -> list[str]:
    iid = (instance_id or "").strip()
    candidates: list[str] = []
    ij_raw = services_secrets.get("dooers_whatsapp_instance_hmac_json")
    ij_map = parse_dooers_whatsapp_instance_hmac_map(ij_raw) if iid else None
    if iid and ij_raw is not None and ij_map is None:
        logger.debug(
            "dooers_whatsapp_instance_hmac_json present but unparsable type=%s",
            type(ij_raw).__name__,
        )
    if iid and ij_map:
        if iid in ij_map:
            inst_secret = ij_map[iid]
            if inst_secret:
                candidates.append(inst_secret)
        else:
            logger.warning(
                "whatsapp inbound HMAC: instance_id=%s missing from instance_hmac map (map size=%s)",
                iid,
                len(ij_map),
            )
    raw = services_secrets.get("dooers_whatsapp_service_secret")
    if isinstance(raw, str) and raw.strip():
        for p in raw.split(","):
            t = p.strip()
            if t and t not in candidates:
                candidates.append(t)
    return candidates


def verify_dooers_whatsapp_tool_inbound_signature(
    services_secrets: dict[str, Any],
    body: bytes,
    header: str | None,
    *,
    instance_id: str | None = None,
    agent_id: str = "",
    log: logging.Logger | None = None,
) -> bool:
    """Verify ``X-WhatsApp-Tool-Signature`` (sha256=...) against service_secrets only."""
    log = log or logger
    if not header or not (agent_id or "").strip():
        return False
    got = (header or "").replace("sha256=", "").strip()
    if not got:
        return False
    iid = (instance_id or "").strip()
    candidates = _inbound_verification_secrets(services_secrets, instance_id)
    seen: set[str] = set()
    ordered_unique: list[str] = []
    for secret in candidates:
        if not secret or secret in seen:
            continue
        seen.add(secret)
        ordered_unique.append(secret)
        want = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(want, got):
            return True

    body_fp = hashlib.sha256(body).hexdigest()[:16]
    sig_prefix = got[:16]
    aid = agent_id.strip()
    if not ordered_unique:
        log.warning(
            "whatsapp inbound HMAC rejected agent_id=%s instance_id=%s reason=no_whatsapp_hmac_candidates "
            "body_sha256_fp=%s sig_prefix=%s",
            aid,
            iid or "-",
            body_fp,
            sig_prefix,
        )
    else:
        fps = ",".join(dooers_whatsapp_hmac_key_fingerprint(s) for s in ordered_unique)
        log.warning(
            "whatsapp inbound HMAC rejected agent_id=%s instance_id=%s reason=secret_mismatch_or_wrong_body "
            "stored_hmac_key_fps=%s body_sha256_fp=%s sig_prefix=%s",
            aid,
            iid or "-",
            fps,
            body_fp,
            sig_prefix,
        )
    return False


async def verify_dooers_whatsapp_tool_inbound_with_persistence(
    persistence: Persistence,
    body: bytes,
    header: str | None,
    agent_id: str,
    instance_id: str | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Fetch ``get_service_secrets(agent_id)`` and verify the tools→agent HMAC (same as service-agent /whatsapp/inbound)."""
    if not header or not (agent_id or "").strip():
        return False
    services_secrets = await persistence.get_service_secrets(agent_id.strip())
    return verify_dooers_whatsapp_tool_inbound_signature(
        services_secrets,
        body,
        header,
        instance_id=instance_id,
        agent_id=agent_id,
        log=log,
    )


def resolve_dooers_whatsapp_outbound_message_hmac(services_secrets: dict[str, Any], instance_id: str) -> str:
    """HMAC for ``X-Message-Signature`` to tools, preferring per-instance map when present."""
    iid = (instance_id or "").strip()
    ij = services_secrets.get("dooers_whatsapp_instance_hmac_json")
    m = parse_dooers_whatsapp_instance_hmac_map(ij) if iid else None
    if iid and m:
        s = m.get(iid)
        if isinstance(s, str) and (t := s.strip()):
            return t
    raw = services_secrets.get("dooers_whatsapp_service_secret")
    raw_str = raw if isinstance(raw, str) else ""
    return primary_dooers_whatsapp_service_secret(raw_str)
