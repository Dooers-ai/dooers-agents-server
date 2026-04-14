# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-04-14

### Breaking changes

- **Anonymous WebSocket connections are now rejected by default.** A connect frame with an empty `user_id` is NACK'd with `ANONYMOUS_NOT_ALLOWED` unless `AgentConfig.auth_validation_url` is set. Previously, such frames were silently accepted and the user ran with an empty identity. Set `auth_validation_url` to a validation endpoint (see `AuthValidationClient`) to re-enable anonymous connects via webhook delegation. This is fail-closed for security.
- **`Persistence` protocol has a new required method** `delete_idle_guest_threads(max_idle_seconds: int) -> int`. Custom `Persistence` subclasses must either implement this method or disable the background cleanup task by setting `AgentConfig.guest_thread_cleanup_interval_seconds = 0`. `PostgresPersistence` implements it natively using `SELECT ... FOR UPDATE SKIP LOCKED` inside a transaction. `CosmosPersistence` raises `NotImplementedError` with guidance to use Cosmos container TTL instead; the cleanup loop handles this by logging and retrying on the next interval.

### Added

- Generic `AUTH_VALIDATION_URL` webhook delegation for anonymous connect validation. When `AgentConfig.auth_validation_url` is set, the SDK posts `{authToken, agentId, guestUserId, organizationId, workspaceId}` to that URL on every anonymous connect and only accepts the connection when the webhook returns `valid: true` with a user payload.
- `AuthValidationClient` with long-lived reused `httpx.AsyncClient`, owned by `AgentServer`, closed in `AgentServer.close()`. Fail-closed on transport errors, 5xx responses, non-200 status codes, and malformed JSON.
- `AuthValidationResult` carries `organization_id` and `workspace_id` fields populated from the webhook response. The router prefers these over client-supplied frame values for anonymous connects, closing a spoofing gap where a client could claim arbitrary workspace membership.
- Per-session sliding-window rate limiter on `event.create`. Limits are per-session, configured via the `rateLimits.messagesPerMinute` field returned by the validation webhook. Authenticated direct connects (non-empty `user_id`) are unaffected — their `_rate_limits` stays empty and the limiter is a no-op.
- Periodic guest-thread TTL cleanup task in `AgentServer`. Deletes threads whose `owner.user_id` starts with `guest:` and whose `last_event_at` is older than the TTL. Default: 24-hour TTL, 1-hour interval. Disable by setting `AgentConfig.guest_thread_cleanup_interval_seconds = 0`.
- `Persistence.delete_idle_guest_threads(max_idle_seconds: int) -> int` — new protocol method with Postgres implementation (transactional with `SELECT ... FOR UPDATE SKIP LOCKED` to avoid races with concurrent writers). Returns the count of threads deleted.
- New `AgentConfig` fields (all additive, all with sensible defaults):
  - `auth_validation_url: str | None = None`
  - `auth_validation_timeout: float = 5.0`
  - `guest_thread_ttl_seconds: int = 86400`
  - `guest_thread_cleanup_interval_seconds: int = 3600`
- Guest role hardening: `AuthValidationClient.validate()` hardcodes `system_role="user"`, `organization_role="member"`, `workspace_role="member"` on the returned `User`, so a compromised or misconfigured validation webhook cannot escalate a guest's role.

### Fixed

- Per-connection rate-limit state (`_rate_limits`, `_event_timestamps`) is reset at the top of `_handle_connect` so a second `C2S_Connect` frame on the same WebSocket session starts clean.
- Guest cleanup loop uses `logger.exception` (not `logger.warning`) so tracebacks are captured in production logs when the cleanup query fails.
- `AgentServer.close()` narrows exception handling around the cleanup task's await so shutdown-time errors are logged rather than silently swallowed.

### Dependencies

- `httpx` — no change, already a prod dependency in 0.6.0.
- `respx` — new dev dependency used by `tests/test_auth_validation.py`. Not in `[project.dependencies]`, only in `[dependency-groups.dev]`. No impact on consumers.

### Migration

- **If you accept anonymous WebSocket connections (empty `user_id`):** set `AgentConfig.auth_validation_url` to an HTTP endpoint that validates the `auth_token` from the connect frame and returns a user payload. The expected response shape is:
  ```json
  {
    "valid": true,
    "user": { "user_id": "...", "user_email": "...", "user_name": "...", "identity_ids": [] },
    "rateLimits": { "messagesPerMinute": 20 },
    "organizationId": "...",
    "workspaceId": "...",
    "threadTtlHours": 24
  }
  ```
  Return `{ "valid": false, "reason": "..." }` to reject. Network errors, 5xx, non-200, and malformed JSON are all treated as `valid: false` by the client.

- **If you have a custom `Persistence` subclass:** implement `delete_idle_guest_threads(max_idle_seconds: int) -> int` OR set `AgentConfig.guest_thread_cleanup_interval_seconds = 0` to disable the background task. If your backend has no efficient query for this, you can raise `NotImplementedError` inside the method — the cleanup loop catches it, logs, and retries on the next interval.
