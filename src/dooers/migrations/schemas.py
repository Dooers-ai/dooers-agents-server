def get_migration_sql(table_prefix: str = "agent_") -> str:
    threads_table = f"{table_prefix}threads"
    events_table = f"{table_prefix}events"
    runs_table = f"{table_prefix}runs"
    settings_table = f"{table_prefix}settings"
    analytics_table = f"{table_prefix}analytics_events"

    return f"""
        CREATE TABLE IF NOT EXISTS {threads_table} (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL DEFAULT '',
            owner JSONB NOT NULL DEFAULT '{{}}',
            users JSONB NOT NULL DEFAULT '[]',
            title TEXT,
            metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            last_event_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {events_table} (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES {threads_table}(id),
            run_id TEXT,
            type TEXT NOT NULL,
            actor TEXT NOT NULL,
            author TEXT,
            user_id TEXT,
            user_name TEXT,
            user_email TEXT,
            content JSONB,
            data JSONB,
            created_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {runs_table} (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES {threads_table}(id),
            agent_id TEXT,
            status TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS {settings_table} (
            agent_id TEXT PRIMARY KEY,
            values JSONB NOT NULL DEFAULT '{{}}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_{table_prefix}threads_agent_id
            ON {threads_table}(agent_id);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}threads_organization_id
            ON {threads_table}(organization_id);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}threads_workspace_id
            ON {threads_table}(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}threads_users
            ON {threads_table} USING GIN (users jsonb_path_ops);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}events_thread_id
            ON {events_table}(thread_id);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}events_user_id
            ON {events_table}(user_id);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}threads_agent_last_event
            ON {threads_table}(agent_id, last_event_at DESC);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}events_thread_created
            ON {events_table}(thread_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}runs_thread_id
            ON {runs_table}(thread_id);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}settings_agent
            ON {settings_table}(agent_id);

        CREATE TABLE IF NOT EXISTS {analytics_table} (
            id TEXT PRIMARY KEY,
            event TEXT NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            agent_id TEXT NOT NULL,
            thread_id TEXT,
            user_id TEXT,
            run_id TEXT,
            event_id TEXT,
            organization_id TEXT,
            workspace_id TEXT,
            data JSONB,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}analytics_events_agent_id
            ON {analytics_table}(agent_id);
        CREATE INDEX IF NOT EXISTS idx_{table_prefix}analytics_events_timestamp
            ON {analytics_table}(timestamp);
    """
