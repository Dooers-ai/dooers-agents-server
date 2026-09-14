ANALYTICS_WEBHOOK_URL = "https://api.dooers.ai/api/webhooks/analytics"
ANALYTICS_BATCH_SIZE = 10
ANALYTICS_FLUSH_INTERVAL = 5.0  # seconds

# Platform defaults for OpenTelemetry (same abstraction style as analytics / WhatsApp).
# Creators should not need to set these; operators may override via env / AgentConfig.
AGENT_CORE_BASE_URL = "https://api.dooers.ai"
AGENT_OTEL_SERVICE_URL = "https://observability.dooers.ai"
OTEL_SERVICE_NAME = "dooers-agent"

# Legacy fallback URL for non-core opaque tokens only. Core-issued JWTs
# (dashboard + public-chat) are always verified against AGENT_CORE_BASE_URL;
# the validation_url claim inside a token is ignored for routing.
AUTH_VALIDATION_URL: str | None = None
AUTH_VALIDATION_TIMEOUT = 5.0  # seconds

# Idle guest thread cleanup. Threads whose owner.user_id starts with "guest:"
# are deleted by a periodic background task when their most recent event is
# older than GUEST_THREAD_TTL_SECONDS. The task runs every
# GUEST_THREAD_CLEANUP_INTERVAL_SECONDS. Set the interval to 0 to disable.
GUEST_THREAD_TTL_SECONDS = 24 * 60 * 60  # 24 hours
GUEST_THREAD_CLEANUP_INTERVAL_SECONDS = 60 * 60  # 1 hour
