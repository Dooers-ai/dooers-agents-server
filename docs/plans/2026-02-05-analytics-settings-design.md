# Analytics & Dynamic Settings System Design

**Date:** 2026-02-05
**Status:** Approved
**Version:** 1.0

## Overview

Two new features for the workers-server:

1. **Analytics System** — Usage tracking, feedback (likes/dislikes), real-time dashboard via WebSocket
2. **Dynamic Settings System** — Real-time configurable agent settings with bidirectional sync

## Handler Signature Change

```python
# Before
async def handler(request, response, memory): ...

# After
async def handler(request, response, memory, analytics, settings): ...
```

---

## Feature 1: Analytics System

### Summary

| Aspect | Decision |
|--------|----------|
| Tracking | Hybrid: automatic core events + manual `analytics.track()` |
| Handler API | Fourth parameter: `analytics` |
| Automatic events | `thread.created`, `message.c2s`, `message.s2c`, `tool.called`, `error.occurred` |
| Feedback | `analytics.like()` / `analytics.dislike()` with flexible targeting |
| Webhook delivery | Batched (10 events or 5s interval) |
| Real-time | Analytics subscription per `worker_id`, immediate broadcast |
| Metadata | `event`, `timestamp`, `worker_id`, `thread_id`, `user_id`, `run_id`, `event_id` |
| Auth | None for now |

### Core Models

```python
from enum import Enum
from datetime import datetime
from pydantic import BaseModel
from typing import Literal, Any

class AnalyticsEvent(str, Enum):
    # Automatic events
    THREAD_CREATED = "thread.created"
    MESSAGE_C2S = "message.c2s"
    MESSAGE_S2C = "message.s2c"
    TOOL_CALLED = "tool.called"
    ERROR_OCCURRED = "error.occurred"

    # Feedback events
    FEEDBACK_LIKE = "feedback.like"
    FEEDBACK_DISLIKE = "feedback.dislike"

class AnalyticsEventPayload(BaseModel):
    event: str  # AnalyticsEvent value or custom string
    timestamp: datetime
    worker_id: str
    thread_id: str | None = None
    user_id: str | None = None
    run_id: str | None = None
    event_id: str | None = None
    data: dict | None = None  # Event-specific data

class FeedbackData(BaseModel):
    target_type: Literal["event", "run", "thread"]
    target_id: str
    reason: str | None = None

class AnalyticsBatch(BaseModel):
    batch_id: str
    worker_id: str
    events: list[AnalyticsEventPayload]
    sent_at: datetime
```

### WebSocket Protocol

**Client-to-Server:**

```python
class C2S_AnalyticsSubscribe(BaseModel):
    id: str
    type: Literal["analytics.subscribe"]
    payload: AnalyticsSubscribePayload

class AnalyticsSubscribePayload(BaseModel):
    worker_id: str

class C2S_AnalyticsUnsubscribe(BaseModel):
    id: str
    type: Literal["analytics.unsubscribe"]
    payload: AnalyticsUnsubscribePayload

class AnalyticsUnsubscribePayload(BaseModel):
    worker_id: str

class C2S_Feedback(BaseModel):
    id: str
    type: Literal["feedback"]
    payload: FeedbackPayload

class FeedbackPayload(BaseModel):
    target_type: Literal["event", "run", "thread"]
    target_id: str
    feedback: Literal["like", "dislike"]
    reason: str | None = None
```

**Server-to-Client:**

```python
class S2C_AnalyticsEvent(BaseModel):
    id: str
    type: Literal["analytics.event"] = "analytics.event"
    payload: AnalyticsEventPayload

class S2C_FeedbackAck(BaseModel):
    id: str
    type: Literal["feedback.ack"] = "feedback.ack"
    payload: FeedbackAckPayload

class FeedbackAckPayload(BaseModel):
    target_type: str
    target_id: str
    feedback: str
    ok: bool
```

### Handler API

```python
class WorkerAnalytics:
    def __init__(
        self,
        worker_id: str,
        thread_id: str,
        user_id: str,
        run_id: str | None,
        collector: AnalyticsCollector,
    ):
        self._worker_id = worker_id
        self._thread_id = thread_id
        self._user_id = user_id
        self._run_id = run_id
        self._collector = collector

    async def track(self, event: str, data: dict | None = None) -> None:
        """Track a custom analytics event."""
        await self._collector.track(
            event=event,
            worker_id=self._worker_id,
            thread_id=self._thread_id,
            user_id=self._user_id,
            run_id=self._run_id,
            data=data,
        )

    async def like(
        self,
        target_type: str,
        target_id: str,
        reason: str | None = None,
    ) -> None:
        """Record a like (for external sources like WhatsApp)."""
        await self._collector.feedback("like", target_type, target_id, reason)

    async def dislike(
        self,
        target_type: str,
        target_id: str,
        reason: str | None = None,
    ) -> None:
        """Record a dislike (for external sources like WhatsApp)."""
        await self._collector.feedback("dislike", target_type, target_id, reason)
```

### Collector & Batching

```python
class AnalyticsCollector:
    def __init__(
        self,
        webhook_url: str,
        registry: ConnectionRegistry,
        analytics_subscriptions: dict[str, set[str]],  # worker_id -> set of ws_ids
        batch_size: int = 10,
        flush_interval: float = 5.0,
    ):
        self._webhook_url = webhook_url
        self._registry = registry
        self._subscriptions = analytics_subscriptions
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: list[AnalyticsEventPayload] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background flush task."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop and flush remaining events."""
        if self._flush_task:
            self._flush_task.cancel()
        await self._flush()

    async def track(self, event: str, worker_id: str, **context) -> None:
        """Add event to buffer, broadcast to subscribers, flush if needed."""
        payload = AnalyticsEventPayload(
            event=event,
            timestamp=datetime.utcnow(),
            worker_id=worker_id,
            **context,
        )

        # Broadcast to analytics subscribers immediately
        await self._broadcast(worker_id, payload)

        # Add to batch buffer
        async with self._lock:
            self._buffer.append(payload)
            if len(self._buffer) >= self._batch_size:
                await self._flush()

    async def _flush(self) -> None:
        """Send batch to webhook."""
        async with self._lock:
            if not self._buffer:
                return
            batch = AnalyticsBatch(
                batch_id=str(uuid4()),
                worker_id=self._buffer[0].worker_id,
                events=self._buffer.copy(),
                sent_at=datetime.utcnow(),
            )
            self._buffer.clear()

        # HTTP POST to webhook (fire and forget, with retry logic)
        await self._send_to_webhook(batch)

    async def _flush_loop(self) -> None:
        """Background task that flushes every flush_interval seconds."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()
```

### Configuration

**settings.py (hardcoded values):**

```python
ANALYTICS_WEBHOOK_URL = "https://api.dooers.ai/webhooks/analytics"
ANALYTICS_BATCH_SIZE = 10
ANALYTICS_FLUSH_INTERVAL = 5.0  # seconds
```

**WorkerConfig extension:**

```python
@dataclass
class WorkerConfig:
    database_url: str
    database_type: Literal["sqlite", "postgres"] = "sqlite"
    table_prefix: str = "worker_"
    auto_migrate: bool = True

    # Analytics (optional, defaults from settings.py)
    analytics_enabled: bool = True
    analytics_batch_size: int | None = None  # Override default
    analytics_flush_interval: float | None = None  # Override default
```

### Automatic Event Tracking

Events are captured inside Router at these points:

- `thread.created` — When a new thread is created
- `message.c2s` — When `event.create` frame is received
- `message.s2c` — When handler yields `response.text()`, `response.image()`, etc.
- `tool.called` — When handler yields `response.tool_call()`
- `error.occurred` — When handler raises an exception (wrapped in try/except)

---

## Feature 2: Dynamic Settings System

### Summary

| Aspect | Decision |
|--------|----------|
| Scope | Worker-level (shared across all threads/users) |
| Schema definition | `WorkerConfig(settings_schema=...)` |
| Validation | Immediate at instantiation (fail fast) |
| Handler API | Fifth parameter: `settings.get()`, `get_all()`, `set()` |
| Field types | `text`, `number`, `select`, `checkbox`, `textarea`, `password`, `email`, `date`, `image` |
| Frontend payload | Inline values in schema fields |
| Persistence | `worker_settings` table with JSON values column |
| Protocol | Subscribe → snapshot + real-time patches (bidirectional) |

### Schema Models

```python
from enum import Enum
from typing import Any
from pydantic import BaseModel, model_validator

class SettingsFieldType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    SELECT = "select"
    CHECKBOX = "checkbox"
    TEXTAREA = "textarea"
    PASSWORD = "password"
    EMAIL = "email"
    DATE = "date"
    IMAGE = "image"  # Display-only (QR codes, etc.)

class SelectOption(BaseModel):
    value: str
    label: str

class SettingsField(BaseModel):
    id: str
    type: SettingsFieldType
    label: str
    required: bool = False
    readonly: bool = False
    value: Any = None  # Default value

    # Type-specific options
    placeholder: str | None = None
    options: list[SelectOption] | None = None  # For select
    min: int | float | None = None  # For number
    max: int | float | None = None  # For number
    rows: int | None = None  # For textarea
    src: str | None = None  # For image (display URL)
    width: int | None = None  # For image
    height: int | None = None  # For image

class SettingsSchema(BaseModel):
    version: str = "1.0"
    fields: list[SettingsField]

    @model_validator(mode="after")
    def validate_unique_ids(self):
        ids = [f.id for f in self.fields]
        if len(ids) != len(set(ids)):
            raise ValueError("Field IDs must be unique")
        return self
```

### WebSocket Protocol

**Client-to-Server:**

```python
class C2S_SettingsSubscribe(BaseModel):
    id: str
    type: Literal["settings.subscribe"]
    payload: SettingsSubscribePayload

class SettingsSubscribePayload(BaseModel):
    worker_id: str

class C2S_SettingsUnsubscribe(BaseModel):
    id: str
    type: Literal["settings.unsubscribe"]
    payload: SettingsUnsubscribePayload

class SettingsUnsubscribePayload(BaseModel):
    worker_id: str

class C2S_SettingsPatch(BaseModel):
    id: str
    type: Literal["settings.patch"]
    payload: SettingsPatchPayload

class SettingsPatchPayload(BaseModel):
    field_id: str
    value: Any
```

**Server-to-Client:**

```python
class S2C_SettingsSnapshot(BaseModel):
    id: str
    type: Literal["settings.snapshot"] = "settings.snapshot"
    payload: SettingsSnapshotPayload

class SettingsSnapshotPayload(BaseModel):
    worker_id: str
    fields: list[SettingsField]  # Schema with values populated
    updated_at: datetime

class S2C_SettingsPatch(BaseModel):
    id: str
    type: Literal["settings.patch"] = "settings.patch"
    payload: SettingsPatchBroadcastPayload

class SettingsPatchBroadcastPayload(BaseModel):
    worker_id: str
    field_id: str
    value: Any
    updated_at: datetime
```

### Handler API

```python
class WorkerSettings:
    def __init__(
        self,
        worker_id: str,
        schema: SettingsSchema,
        persistence: Persistence,
        broadcaster: SettingsBroadcaster,
    ):
        self._worker_id = worker_id
        self._schema = schema
        self._persistence = persistence
        self._broadcaster = broadcaster

    async def get(self, field_id: str) -> Any:
        """Get a single field value."""
        values = await self._persistence.get_settings(self._worker_id)
        return values.get(field_id, self._get_default(field_id))

    async def get_all(self) -> dict[str, Any]:
        """Get all field values as a dict."""
        defaults = {f.id: f.value for f in self._schema.fields}
        stored = await self._persistence.get_settings(self._worker_id)
        return {**defaults, **stored}

    async def set(self, field_id: str, value: Any) -> None:
        """Update a field value and broadcast to subscribers."""
        self._validate_field(field_id, value)
        await self._persistence.update_setting(self._worker_id, field_id, value)
        await self._broadcaster.broadcast_patch(self._worker_id, field_id, value)

    def _get_default(self, field_id: str) -> Any:
        for field in self._schema.fields:
            if field.id == field_id:
                return field.value
        raise KeyError(f"Unknown field: {field_id}")

    def _validate_field(self, field_id: str, value: Any) -> None:
        """Validate field exists and value is appropriate for type."""
        field = next((f for f in self._schema.fields if f.id == field_id), None)
        if not field:
            raise KeyError(f"Unknown field: {field_id}")
        if field.readonly:
            raise ValueError(f"Field '{field_id}' is readonly")
        # Additional type validation can be added here
```

### Persistence

**New table schema:**

```sql
CREATE TABLE IF NOT EXISTS {prefix}settings (
    worker_id TEXT PRIMARY KEY,
    values TEXT NOT NULL DEFAULT '{}',  -- JSON object
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_{prefix}settings_worker
    ON {prefix}settings(worker_id);
```

**New persistence methods:**

```python
class Persistence(Protocol):
    # ... existing methods ...

    async def get_settings(self, worker_id: str) -> dict[str, Any]:
        """Get all stored values for a worker. Returns empty dict if none."""
        ...

    async def update_setting(self, worker_id: str, field_id: str, value: Any) -> datetime:
        """Update a single field value. Returns updated_at timestamp."""
        ...

    async def set_settings(self, worker_id: str, values: dict[str, Any]) -> datetime:
        """Replace all settings values. Returns updated_at timestamp."""
        ...
```

### Configuration

**WorkerConfig extension:**

```python
@dataclass
class WorkerConfig:
    database_url: str
    database_type: Literal["sqlite", "postgres"] = "sqlite"
    table_prefix: str = "worker_"
    auto_migrate: bool = True

    # Analytics
    analytics_enabled: bool = True
    analytics_batch_size: int | None = None
    analytics_flush_interval: float | None = None

    # Settings
    settings_schema: SettingsSchema | None = None
```

---

## File Structure

**New/modified files:**

```
src/dooers/
├── settings.py                    # NEW - hardcoded webhook URL, defaults
├── analytics/                     # NEW - analytics module
│   ├── __init__.py
│   ├── models.py                  # AnalyticsEvent enum, payload models
│   ├── collector.py               # AnalyticsCollector (batching, webhook, broadcast)
│   └── worker_analytics.py        # WorkerAnalytics (handler API)
├── settings/                      # NEW - settings module
│   ├── __init__.py
│   ├── models.py                  # SettingsFieldType, SettingsField, SettingsSchema
│   ├── worker_settings.py         # WorkerSettings (handler API)
│   └── broadcaster.py             # SettingsBroadcaster (WebSocket broadcast)
├── protocol/
│   └── frames.py                  # MODIFIED - add C2S/S2C frames
├── persistence/
│   ├── base.py                    # MODIFIED - add settings methods
│   ├── sqlite.py                  # MODIFIED - implement settings methods
│   └── postgres.py                # MODIFIED - implement settings methods
├── migrations/
│   └── schemas.py                 # MODIFIED - add worker_settings table
├── handlers/
│   ├── router.py                  # MODIFIED - analytics/settings params, new frames
│   └── __init__.py                # MODIFIED - export new classes
├── server.py                      # MODIFIED - init collector, subscriptions
├── config.py                      # MODIFIED - new config options
└── __init__.py                    # MODIFIED - export new types
```

---

## Usage Example

```python
from dooers import (
    WorkerConfig, WorkerServer,
    SettingsSchema, SettingsField, SettingsFieldType,
)

schema = SettingsSchema(
    fields=[
        SettingsField(
            id="api_key",
            type=SettingsFieldType.PASSWORD,
            label="API Key",
            required=True,
            placeholder="sk-...",
        ),
        SettingsField(
            id="model",
            type=SettingsFieldType.SELECT,
            label="Model",
            value="gpt-4o-mini",
            options=[
                {"value": "gpt-4o-mini", "label": "GPT-4o Mini"},
                {"value": "gpt-4o", "label": "GPT-4o"},
            ],
        ),
        SettingsField(
            id="temperature",
            type=SettingsFieldType.NUMBER,
            label="Temperature",
            value=0.7,
            min=0,
            max=2,
        ),
        SettingsField(
            id="qr_code",
            type=SettingsFieldType.IMAGE,
            label="WhatsApp QR",
            readonly=True,
            width=200,
            height=200,
        ),
    ]
)

worker_server = WorkerServer(
    WorkerConfig(
        database_url="sqlite:///worker.db",
        settings_schema=schema,
    )
)

async def my_agent(request, response, memory, analytics, settings):
    yield response.run_start(agent_id="my-agent")

    # Read settings
    model = await settings.get("model")
    temperature = await settings.get("temperature")

    # Track custom event
    await analytics.track("llm.called", data={"model": model})

    # Call LLM...
    result = await call_llm(model, temperature, request.message)

    # Update QR code from server (e.g., WhatsApp reconnection)
    if needs_new_qr:
        await settings.set("qr_code", generate_qr_url())

    yield response.text(result)
    yield response.run_end()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, my_agent)
```

---

## Open Questions / Future Considerations

1. **Analytics auth** — Add API key or signature-based auth when needed
2. **Settings validation** — Add type-specific validation (email format, number ranges, etc.)
3. **Settings history** — Track settings changes over time for audit
4. **Analytics retention** — Define how long events are kept on backend
5. **Rate limiting** — Prevent abuse of analytics/settings endpoints
