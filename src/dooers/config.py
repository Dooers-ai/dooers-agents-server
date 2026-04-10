import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from dooers.features.settings.models import SettingsSchema


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

    auth_validation_url: str | None = None
    auth_validation_timeout: float = 5.0

    settings_schema: "SettingsSchema | None" = None
    # If set, settings.seed WebSocket frames are accepted (e.g. core copies template on hire).
    agent_seed_secret: str = field(default_factory=lambda: os.environ.get("AGENT_SEED_SECRET", "").strip())

    upload_max_size_bytes: int = 25 * 1024 * 1024  # 25MB
    upload_ttl_seconds: int = 300  # 5 minutes
