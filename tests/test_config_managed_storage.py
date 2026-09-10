"""AgentConfig + chat-artifact support for the Dooers-managed (GCS) storage mode.

`chat_storage_service="dooers"` rides the GCS backend but writes every object under
`DOOERS_STORAGE_PREFIX` (agents/<org_id>/), so the tenant SA's path-conditioned IAM
grant allows it and one org can never reach another org's objects.
"""

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.storage.chat_artifact_keys import build_chat_artifact_object_key
from dooers.agents.server.storage.chat_artifacts import (
    _managed_prefix,
    resolve_chat_artifact_backend,
    sign_chat_artifact_read_url,
)

ORG_PREFIX = "agents/e7d3594c-f61f-49c7-a4a0-ca288dd0619e/"


def _cfg(**kw) -> AgentConfig:
    return AgentConfig(database_type="postgres", **kw)


def test_dooers_is_an_accepted_backend_value(monkeypatch):
    monkeypatch.setenv("CHAT_STORAGE_SERVICE", "dooers")
    assert _cfg().chat_storage_service == "dooers"


def test_dooers_storage_prefix_reads_env(monkeypatch):
    monkeypatch.setenv("DOOERS_STORAGE_PREFIX", ORG_PREFIX)
    assert _cfg().dooers_storage_prefix == ORG_PREFIX


def test_dooers_backend_resolves_to_gcp():
    cfg = _cfg(chat_storage_service="dooers")
    assert resolve_chat_artifact_backend(cfg) == "gcp"


def test_managed_prefix_only_applies_to_dooers():
    assert _managed_prefix(_cfg(chat_storage_service="gcp", dooers_storage_prefix=ORG_PREFIX)) == ""
    assert _managed_prefix(_cfg(chat_storage_service="none")) == ""


def test_managed_prefix_is_normalized():
    # leading/trailing slashes normalized to exactly one trailing slash, none leading.
    cfg = _cfg(chat_storage_service="dooers", dooers_storage_prefix="/agents/org-1")
    assert _managed_prefix(cfg) == "agents/org-1/"
    assert _managed_prefix(_cfg(chat_storage_service="dooers", dooers_storage_prefix="")) == ""


def test_object_keys_land_under_org_prefix_in_dooers_mode():
    # The security-critical property: every managed object key starts with the org's
    # prefix, so it falls inside the tenant SA's conditioned grant.
    cfg = _cfg(chat_storage_service="dooers", dooers_storage_prefix=ORG_PREFIX)
    key = build_chat_artifact_object_key(
        agent_id="a1", thread_id="t1", ref_id="r1", filename="doc.pdf",
        prefix=_managed_prefix(cfg),
    )
    assert key.startswith(ORG_PREFIX)
    assert key == f"{ORG_PREFIX}chat-artifacts/v1/a1/t1/r1/doc.pdf"


def test_plain_gcp_mode_keys_are_unprefixed():
    # gcp/azure/none backends keep their existing keys (empty prefix) — no behavior change.
    cfg = _cfg(chat_storage_service="gcp", gcp_storage_bucket="b")
    key = build_chat_artifact_object_key(
        agent_id="a1", thread_id="t1", ref_id="r1", filename="doc.pdf",
        prefix=_managed_prefix(cfg),
    )
    assert key == "chat-artifacts/v1/a1/t1/r1/doc.pdf"


def test_signed_url_skipped_in_dooers_mode():
    # Phase 1: no signBlob/tokenCreator on the tenant SA — signing would fail, so skip.
    cfg = _cfg(chat_storage_service="dooers", dooers_storage_prefix=ORG_PREFIX, gcp_storage_bucket="b")
    assert sign_chat_artifact_read_url(cfg, f"{ORG_PREFIX}chat-artifacts/v1/a1/t1/r1/doc.pdf") is None


def test_storage_type_defaults_none():
    assert AgentConfig(database_type="postgres").storage_type == "none"


def test_storage_type_dooers_accepted():
    assert AgentConfig(database_type="postgres", storage_type="dooers").storage_type == "dooers"


def test_chat_follows_storage_type_when_unset(monkeypatch):
    monkeypatch.delenv("CHAT_STORAGE_SERVICE", raising=False)
    cfg = AgentConfig(database_type="postgres", storage_type="dooers")
    assert cfg.chat_storage_service == "dooers"  # followed


def test_explicit_chat_env_wins_over_follow(monkeypatch):
    monkeypatch.setenv("CHAT_STORAGE_SERVICE", "gcp")
    cfg = AgentConfig(database_type="postgres", storage_type="dooers")
    assert cfg.chat_storage_service == "gcp"  # env set → not overridden


def test_warns_when_managed_storage_provisioned_but_storage_type_none(monkeypatch, caplog):
    monkeypatch.setenv("GCP_BUCKET_NAME", "bkt")
    monkeypatch.setenv("DOOERS_STORAGE_PREFIX", ORG_PREFIX)
    with caplog.at_level("WARNING", logger="dooers.agents.server.config"):
        AgentConfig(database_type="postgres", storage_type="none")
    assert any("storage_type='none'" in r.message for r in caplog.records)


def test_no_warning_when_storage_type_dooers(monkeypatch, caplog):
    monkeypatch.setenv("GCP_BUCKET_NAME", "bkt")
    monkeypatch.setenv("DOOERS_STORAGE_PREFIX", ORG_PREFIX)
    with caplog.at_level("WARNING", logger="dooers.agents.server.config"):
        AgentConfig(database_type="postgres", storage_type="dooers")
    assert not any("managed storage is provisioned" in r.message for r in caplog.records)


def test_no_warning_when_managed_storage_env_incomplete(monkeypatch, caplog):
    monkeypatch.setenv("GCP_BUCKET_NAME", "bkt")
    monkeypatch.delenv("DOOERS_STORAGE_PREFIX", raising=False)
    with caplog.at_level("WARNING", logger="dooers.agents.server.config"):
        AgentConfig(database_type="postgres", storage_type="none")
    assert not any("managed storage is provisioned" in r.message for r in caplog.records)
