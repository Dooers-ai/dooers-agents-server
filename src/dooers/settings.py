ANALYTICS_WEBHOOK_URL = "https://api.dooers.ai/api/webhooks/analytics"
ANALYTICS_BATCH_SIZE = 10
ANALYTICS_FLUSH_INTERVAL = 5.0  # seconds

# Legacy fallback URL. All tokens (dashboard + public-chat) now carry their
# own validation_url as a signed JWT claim, so this setting is no longer
# required. The SDK auto-detects JWT tokens and calls the embedded URL.
# Only set this if you need to support non-JWT opaque tokens from an
# external system.
AUTH_VALIDATION_URL: str | None = None
AUTH_VALIDATION_TIMEOUT = 5.0  # seconds

# Idle guest thread cleanup. Threads whose owner.user_id starts with "guest:"
# are deleted by a periodic background task when their most recent event is
# older than GUEST_THREAD_TTL_SECONDS. The task runs every
# GUEST_THREAD_CLEANUP_INTERVAL_SECONDS. Set the interval to 0 to disable.
GUEST_THREAD_TTL_SECONDS = 24 * 60 * 60  # 24 hours
GUEST_THREAD_CLEANUP_INTERVAL_SECONDS = 60 * 60  # 1 hour
