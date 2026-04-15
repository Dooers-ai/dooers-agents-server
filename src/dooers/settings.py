ANALYTICS_WEBHOOK_URL = "https://api.dooers.ai/api/webhooks/analytics"
ANALYTICS_BATCH_SIZE = 10
ANALYTICS_FLUSH_INTERVAL = 5.0  # seconds

# Connection validation webhook. When set, anonymous WebSocket connections
# (empty user_id in the connect frame) are delegated to this URL for a
# yes/no decision. When unset, anonymous connections are refused.
AUTH_VALIDATION_URL: str | None = None
AUTH_VALIDATION_TIMEOUT = 5.0  # seconds

# Idle guest thread cleanup. Threads whose owner.user_id starts with "guest:"
# are deleted by a periodic background task when their most recent event is
# older than GUEST_THREAD_TTL_SECONDS. The task runs every
# GUEST_THREAD_CLEANUP_INTERVAL_SECONDS. Set the interval to 0 to disable.
GUEST_THREAD_TTL_SECONDS = 24 * 60 * 60  # 24 hours
GUEST_THREAD_CLEANUP_INTERVAL_SECONDS = 60 * 60  # 1 hour
