ANALYTICS_WEBHOOK_URL = "https://api.dooers.ai/api/webhooks/analytics"
ANALYTICS_BATCH_SIZE = 10
ANALYTICS_FLUSH_INTERVAL = 5.0  # seconds

# Connection validation webhook. When set, anonymous WebSocket connections
# (empty user_id in the connect frame) are delegated to this URL for a
# yes/no decision. When unset, anonymous connections are refused.
AUTH_VALIDATION_URL: str | None = None
AUTH_VALIDATION_TIMEOUT = 5.0  # seconds
