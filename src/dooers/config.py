import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from dooers.settings import (
    GUEST_THREAD_CLEANUP_INTERVAL_SECONDS,
    GUEST_THREAD_TTL_SECONDS,
)

if TYPE_CHECKING:
    from dooers.features.settings.models import SettingsSchema

# (agent_id, field_id, old_value, new_value). Called after each successful settings write.
OnSettingsUpdated = Callable[[str, str, Any, Any], Awaitable[None]]


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _default_chat_storage_service() -> str:
    """Chat blob backend: ``none`` | ``gcp`` | ``azure`` only. Env: ``CHAT_STORAGE_SERVICE`` (default ``none``)."""
    raw = (os.environ.get("CHAT_STORAGE_SERVICE") or "").strip().lower()
    if raw in {"none", "gcp", "azure"}:
        return raw
    return "none"


def _parse_ssl(value: str) -> bool | str:
    """Parse SSL config: accepts bool strings ('true'/'false') or PostgreSQL SSL modes."""
    lower = value.lower().strip()
    if lower in ("false", "0", "no", "off", ""):
        return False
    if lower in ("true", "1", "yes", "on"):
        return True
    if lower in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full"):
        return lower
    return False


@dataclass
class AgentConfig:
    database_type: Literal["postgres", "cosmos"]

    assistant_name: str = "Assistant"

    database_host: str = field(default_factory=lambda: os.environ.get("AGENT_DATABASE_HOST", "localhost"))
    database_port: int = field(default_factory=lambda: int(os.environ.get("AGENT_DATABASE_PORT", "5432")))
    database_user: str = field(default_factory=lambda: os.environ.get("AGENT_DATABASE_USER", "postgres"))
    database_name: str = field(default_factory=lambda: os.environ.get("AGENT_DATABASE_NAME", ""))
    database_password: str = field(default_factory=lambda: os.environ.get("AGENT_DATABASE_PASSWORD", ""))
    database_key: str = field(default_factory=lambda: os.environ.get("AGENT_DATABASE_KEY", ""))
    database_ssl: bool | str = field(default_factory=lambda: _parse_ssl(os.environ.get("AGENT_DATABASE_SSL", "false")))

    database_table_prefix: str = "agent_"
    database_auto_migrate: bool = True

    analytics_enabled: bool = True
    analytics_webhook_url: str | None = None
    analytics_batch_size: int | None = None
    analytics_flush_interval: float | None = None

    # Validation URL for public-chat opaque session tokens only. Dashboard (JWT)
    # tokens carry their own validation URL in the token payload and bypass this.
    auth_validation_url: str | None = None
    auth_validation_timeout: float = 5.0

    # Idle guest thread cleanup (threads whose owner.user_id starts with "guest:").
    # Set guest_thread_cleanup_interval_seconds to 0 to disable.
    guest_thread_ttl_seconds: int = GUEST_THREAD_TTL_SECONDS
    guest_thread_cleanup_interval_seconds: int = GUEST_THREAD_CLEANUP_INTERVAL_SECONDS

    settings_schema: "SettingsSchema | None" = None
    #: If normalized to a non-empty set, the pipeline rejects *after* persisting the user message: it skips the
    #: creator handler and streams ``run_start`` → assistant ``text`` (see ``content_policy_denial_message``) →
    #: ``run_end`` failed. Unknown / video attachment kinds are still rejected in ``setup``.
    #: ``None`` / empty parse → no allowlist here (creator may validate manually).
    #: Pass ``frozenset({...})``, list/tuple tokens, comma-separated string, or JSON-array string — ``normalize_allowed_content_types``.
    allowed_content_types: frozenset[str] | tuple[str, ...] | list[str] | str | None = None
    #: When ``allowed_content_types`` blocks a ``message`` event, assistant copy shown in the thread. Use
    #: ``{offenders}`` (present types in the payload) and ``{allowed}`` (configured allowlist). English default applies if omitted/blank.
    content_policy_denial_message: str | None = None
    # If set, called after each successful settings field change (also per key after set_settings bulk replace).
    on_settings_updated: OnSettingsUpdated | None = None
    # If set, settings.seed WebSocket frames are accepted (e.g. core copies template on hire).
    agent_seed_secret: str = field(default_factory=lambda: os.environ.get("AGENT_SEED_SECRET", "").strip())

    upload_max_size_bytes: int = 25 * 1024 * 1024  # 25MB
    upload_ttl_seconds: int = 300  # 5 minutes

    #: When True (env ``STORE_CHAT_UPLOADS``), durable blob writes may run after ``AgentServer.upload`` when the
    #: HTTP layer requests persistence and storage credentials are configured.
    store_chat_uploads: bool = field(default_factory=lambda: _env_bool("STORE_CHAT_UPLOADS", False))
    #: ``none`` | ``gcp`` | ``azure`` for **chat** blobs only (env ``CHAT_STORAGE_SERVICE``). Independent of RAG.
    chat_storage_service: str = field(default_factory=_default_chat_storage_service)
    #: GCS bucket for chat artifacts (typically ``GCP_BUCKET_NAME`` in .env).
    gcp_storage_bucket: str = field(default_factory=lambda: (os.environ.get("GCP_BUCKET_NAME") or "").strip())
    azure_storage_connection_string: str = field(default_factory=lambda: (os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or "").strip())
    azure_storage_container: str = field(default_factory=lambda: (os.environ.get("AZURE_STORAGE_CONTAINER") or "").strip())
    chat_artifact_signed_url_ttl_minutes: int = field(
        default_factory=lambda: max(1, int(os.environ.get("CHAT_ARTIFACT_SIGNED_URL_TTL_MINUTES", "60") or "60"))
    )
