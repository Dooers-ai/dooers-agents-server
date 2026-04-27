# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.2] — 2026-04-27

Security patch hardening JWT-driven validation URL handling. The SDK
extracts `validation_url` from the JWT payload without verifying the
token signature, so an honest client could be tricked into pointing the
SDK at an attacker-controlled host. This release adds a strict allowlist
on the host in that claim. A full fix (signature verification or a
trusted authoritative resolver) is coming with the upcoming auth rework.

### Security

- **`AuthValidationClient.validate()` now rejects any `validation_url`
  whose host is not `dooers.ai` or a `*.dooers.ai` subdomain**, requires
  `https`, and disallows URLs containing userinfo. Rejected URLs are
  logged and surface as `reason="validation_url_not_allowed"` without
  any outbound HTTP call. The operator-configured legacy validation URL
  is unaffected.

### Compatibility

Fully backward compatible for legitimate dashboard JWTs minted by
`dooers-service-core`, which always point at `*.dooers.ai`.

## [0.9.1] — 2026-04-23

Patch release fixing a production bug in the Postgres worker-seed
persistence path and improving observability around settings frames.

### Fixed

- **`agent_settings` seed-hash SQL referenced a non-existent `worker_id`
  column.** `get_worker_seed_hash_bytes()` and
  `set_worker_seed_hash_bytes()` in `persistence/postgres.py` now query
  and upsert `seed_secret_hash` by `agent_id` (the table's primary key);
  the worker UUID remains the stored value. Before this fix,
  `settings.seed` frames failed after the WebSocket accepted them,
  surfacing as 500s on seed rotation.

### Changed

- **`settings.seed` handler logs at INFO** on start, forbidden, and
  completion so the rotation path is visible in production logs.
- **WebSocket `handle` loop logs parse failures and unexpected errors at
  WARNING with `exc_info=True`**, replacing the previous silent
  swallow — unexpected frame errors now carry a full traceback.

### Compatibility

Fully backward compatible. No schema or wire changes.

## [0.9.0] — 2026-04-17

Additive release focused on authentication and guest-visitor UX. The SDK
now auto-discovers its validation URL from the JWT carried in the connect
frame, so agents no longer need `AUTH_VALIDATION_URL` configured for
dashboard sessions (the config stays for public-chat opaque session tokens
only). The validation webhook returns a richer `ConnectionContext` with
nested `user` / `organization` / `workspace` / `agent` / `policies`
objects, and threads can now carry opaque `metadata` supplied on creation.

### Added

- **Self-describing JWT validation.** `AuthValidationClient.validate()`
  base64-decodes the JWT payload, reads the `validation_url` claim, and
  POSTs the token to that URL. No env var, no config coupling — the
  backend tells the SDK where to verify. Opaque session tokens still fall
  back to the configured `auth_validation_url`.
- **`ConnectionContext` response parsing.** New Pydantic models
  (`ConnectionUser`, `ConnectionOrganization`, `ConnectionWorkspace`,
  `ConnectionAgent`, `ConnectionPolicies`, `ConnectionContext`) mirror
  the nested validation response. `AuthValidationResult` exposes
  `connection_type`, `agent_id`, `agent_owner_user_id`, and
  `organization_plan`.
- **`User.connection_type`** (str, default `"dashboard"`) — distinguishes
  `"dashboard"` / `"public_authenticated"` / `"guest"` sessions. Handlers
  can read it off `context.user` to branch UX (e.g., title prefixes,
  feature gating).
- **`Thread.metadata`** (`dict[str, Any] | None`) — opaque key/value
  store attached on thread creation. `EventCreatePayload` accepts
  `metadata?: dict` which the pipeline persists onto the thread row and
  ignores on subsequent events. Intended for per-thread context the
  agent needs (pre-chat form values, routing hints, external IDs).
- **`AgentMemory.get_thread()`** — handlers can read the current
  `Thread` (including `metadata`) on any invocation.
- **`metadata JSONB` column** on the threads table, auto-migrated via
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` on startup.
- **Guest display-field merge** on the anonymous connect path. When
  `connection_type == "guest"` and the webhook returns empty
  `user_name` / `user_email`, the router fills them in from the connect
  frame so stored events carry the visitor's label instead of rendering
  as `guest:<hex>`. Identity, roles, and scope remain webhook-authoritative.

### Changed

- **`AuthValidationClient` is always constructed** now, even without a
  configured URL. The JWT path doesn't need one — it reads from the
  token. The configured URL is only used as a legacy fallback for
  opaque tokens.
- **Authenticated connect path trusts only the webhook response** for
  identity and roles. The previous `result.field or incoming_user.field`
  fallback chain was removed — a frontend that tampers with the frame
  can no longer inject a fake role because the merge never happens.
  Connections are rejected outright if validation fails, rather than
  silently falling back to frame-supplied data.
- **`_user_author_display()` no longer falls back to `user_id`.** Guest
  IDs are opaque (`guest:<hex>`) and not meaningful to managers; the
  field is left null when name/email are both unavailable.
- **`SettingsPublicSchemaResultPayload.schema`** renamed to `schema_`
  (with `Field(alias="schema")`) to silence the Pydantic BaseModel
  shadowing warning. Wire shape unchanged.

### Compatibility

Fully additive. Existing handlers continue to work — new fields default
to safe values. Agents that still rely on the legacy flat validation
response (flat `user.*` + `organizationId` fields) are still supported
via the `_validate_legacy` fallback path, though new backends should
emit the nested `ConnectionContext` shape.

## [0.8.0] — 2026-04-14

Additive release on top of 0.7.0 — no new breaking changes. Bundles the
upload-URL processing + settings-update callback work alongside the public
chat webhook delegation and guest-thread cleanup shipped in 0.7.0.

### Added

- **Upload URL propagation on content parts.** `AudioPart`, `ImagePart`, and
  `DocumentPart` (both the wire C2S types and the handler-facing types) gain
  an optional `url: str | None` field. When the frontend upload endpoint
  persists the file (e.g., to an object store) and returns a `public_url`
  in the upload response, the client echoes it into subsequent `event.create`
  frames. The SDK's `HandlerPipeline` plumbs the `url` through the wire →
  handler format → storage path unchanged. Handlers can read
  `message.content[i].url` to access the public URL when available.
- **`AgentConfig.on_settings_updated` callback hook.** New optional callback
  fired after every `Persistence.update_setting()` / `set_settings()`
  invocation. Receives `(agent_id, field_id, old_value, new_value)` and is
  awaited. Errors inside the callback are caught and logged at warning level,
  never block the settings write. Useful for invalidating external caches,
  emitting audit events, or triggering configuration-change webhooks.
- **`OnSettingsUpdated` type alias** exported from `dooers` for type-hinting
  the callback: `Callable[[str, str, Any, Any], Awaitable[None]]`.
- **New `AgentMemory.get_history()` formats.** Three additional output
  formats alongside the existing `openai`/`anthropic`/`google`/`cohere`/`voyage`:
  - `"langchain"` — renders as LangChain-compatible message dicts.
  - `"openai_completions"` — classic OpenAI Chat Completions shape.
  - `"openai_responses"` — the newer OpenAI Responses API shape.
  All three render multimodal content (images, audio, documents) using the
  new `url` field when present, falling back to inline text references when
  a URL is not available.
- **`bcrypt>=4.2.0`** added as a runtime dependency (used by forthcoming
  settings field encryption).
- **New handler API reference doc** at `docs/sdk-handler-reference.md`
  (Portuguese) — 442 lines covering the handler contract, `AgentOn`,
  `AgentSend`, `AgentMemory`, `AgentSettings`, and `AgentAnalytics` surfaces.

### Changed

- **`handlers/pipeline.py` internal reformat.** The file was reflowed by the
  formatter as part of the upload-URL work. The line count jumped from ~2000
  to ~2100, but the semantic delta is ~140 lines that plumb `url` through the
  pipeline. Handler lifecycle (setup → execute → teardown), thread/event
  upsert ordering, analytics integration, settings broadcaster integration,
  and upload store integration are all preserved. No public API change.
- **`handlers/send.py` formatter-only reshuffle.** ~600 lines of diff, zero
  semantic changes. `AgentSend` public methods unchanged.

### Not changed from 0.7.0

- Anonymous connect rejection behavior: still fail-closed unless
  `AgentConfig.auth_validation_url` is set.
- `Persistence.delete_idle_guest_threads` still required (Postgres implements
  natively, Cosmos raises `NotImplementedError` with guidance to use Cosmos
  container TTL).
- Sliding-window rate limiter on `event.create` for validated sessions —
  still a no-op for authenticated direct connects.

### Migration from 0.7.0 → 0.8.0

**No breaking changes.** If you're on 0.7.0, upgrading to 0.8.0 is purely
additive:

- All new fields are optional.
- `AgentConfig.on_settings_updated` defaults to `None` (no callback).
- The `url` field on content parts defaults to `None`; existing handlers
  that ignore the field continue to work unchanged.
- New `get_history()` formats are opt-in; existing format strings still work.
- `handlers/pipeline.py` and `handlers/send.py` have no public API changes.

### Migration from 0.6.0 or earlier → 0.8.0

If you're skipping 0.7.0, you inherit **all the breaking changes documented
in the 0.7.0 section below** in addition to the 0.8.0 additions:

- Anonymous WebSocket connects now require `auth_validation_url` to be set.
- `Persistence` protocol has a new required method `delete_idle_guest_threads`.

See the 0.7.0 migration notes below.

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
