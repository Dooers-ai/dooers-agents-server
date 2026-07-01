"""Dooers-managed AlloyDB persistence (IAM auth, no passwords).

Same wire protocol as PostgreSQL, so this reuses every query + the ``migrate``
DDL from :class:`PostgresPersistence` and only overrides ``connect`` to build
the asyncpg pool through the AlloyDB Python connector. The agent runs as the
per-org tenant service account; the connector mints a short-lived IAM token
from the metadata server, so no password is ever configured.
"""

import logging

import asyncpg

from dooers.agents.server.config import OnSettingsUpdated
from dooers.agents.server.persistence.postgres import PostgresPersistence

logger = logging.getLogger(__name__)

try:
    from google.cloud.alloydb.connector import AsyncConnector, IPTypes

    ALLOYDB_AVAILABLE = True
except ImportError:
    ALLOYDB_AVAILABLE = False
    AsyncConnector = None  # type: ignore[assignment,misc]
    IPTypes = None  # type: ignore[assignment,misc]


class DooersPersistence(PostgresPersistence):
    def __init__(
        self,
        *,
        instance_uri: str,
        user: str,
        database: str,
        ip_type: str = "PRIVATE",
        table_prefix: str = "agent_",
        on_settings_updated: OnSettingsUpdated | None = None,
    ):
        if not ALLOYDB_AVAILABLE:
            raise ImportError(
                "AlloyDB connector not installed. Install with: "
                "pip install dooers-agents-server[dooers]"
            )
        if not instance_uri or not user or not database:
            raise ValueError(
                "Dooers-managed DB requires an instance URI, IAM user, and database name "
                "(AGENT_DATABASE_INSTANCE, AGENT_DATABASE_USER, AGENT_DATABASE_NAME). "
                "These are injected automatically by dooers-push for database.type=dooers."
            )
        super().__init__(
            host="",
            port=5432,
            user=user,
            database=database,
            password="",
            ssl=False,
            table_prefix=table_prefix,
            on_settings_updated=on_settings_updated,
        )
        self._instance_uri = instance_uri
        self._ip_type = ip_type
        self._connector = None

    async def connect(self) -> None:
        logger.info(
            "[agents] connecting to Dooers-managed AlloyDB %s/%s (user=%s, iam)",
            self._instance_uri,
            self._database,
            self._user,
        )
        self._connector = AsyncConnector()
        ip = IPTypes.PUBLIC if self._ip_type == "PUBLIC" else IPTypes.PRIVATE

        async def _getconn() -> asyncpg.Connection:
            return await self._connector.connect(
                self._instance_uri,
                "asyncpg",
                user=self._user,
                db=self._database,
                enable_iam_auth=True,
                ip_type=ip,
            )

        self._pool = await asyncpg.create_pool(
            connect=_getconn,
            min_size=1,
            max_size=10,
            timeout=30,
            command_timeout=30,
        )
        logger.info("[agents] successfully connected to AlloyDB")

    async def disconnect(self) -> None:
        await super().disconnect()
        if self._connector is not None:
            await self._connector.close()
            self._connector = None
