# SDK Improvements Design

**Date:** 2026-02-10
**Version:** 0.3.0
**Status:** Approved

## Overview

Four improvements to the workers-server SDK:

1. Configurable assistant name in WorkerConfig
2. Author field for display name override on events
3. Tool call correlation with display names
4. Azure Cosmos DB NoSQL persistence adapter

## 1. Configuration & Author Model

### WorkerConfig additions

```python
@dataclass
class WorkerConfig:
    # ... existing fields ...

    # Display name for assistant responses (default: "Assistant")
    assistant_name: str = "Assistant"
```

### ThreadEvent model changes

```python
class ThreadEvent(BaseModel):
    # ... existing fields ...

    # Optional display name override (takes precedence over defaults)
    author: str | None = None
```

### Resolution logic (frontend/consumer)

```
if event.author:
    display_name = event.author
elif event.actor == "assistant":
    display_name = config.assistant_name
elif event.actor == "user":
    display_name = event.user_name or event.user_email or "User"
elif event.actor == "tool":
    display_name = event.data.get("display_name") or event.data.get("name")
else:
    display_name = event.actor.capitalize()
```

### WorkerResponse changes

All message response methods gain optional `author` parameter:

```python
def text(self, text: str, author: str | None = None) -> WorkerEvent:
    ...

def image(self, url: str, ..., author: str | None = None) -> WorkerEvent:
    ...

def document(self, url: str, ..., author: str | None = None) -> WorkerEvent:
    ...
```

## 2. Tool Call Correlation & Display

### Event data structures

**tool.call event data:**

```python
{
    "id": str,                   # UUID to correlate with result
    "name": str,                 # Function name (e.g., "search_inventory")
    "display_name": str | None,  # Optional UI name (e.g., "Searching inventory...")
    "args": dict,                # Input arguments
}
```

**tool.result event data:**

```python
{
    "id": str,                   # Matches the call
    "name": str,                 # Function name (echoed)
    "display_name": str | None,  # Optional UI name (echoed)
    "args": dict | None,         # Input arguments (optional, for self-contained rendering)
    "result": dict,              # Output from the tool
}
```

### WorkerResponse changes

```python
def tool_call(
    self,
    name: str,
    args: dict,
    display_name: str | None = None,
    id: str | None = None,  # Auto-generated if not provided
) -> WorkerEvent:
    ...

def tool_result(
    self,
    name: str,
    result: dict,
    args: dict | None = None,       # Optional
    display_name: str | None = None,
    id: str | None = None,
) -> WorkerEvent:
    ...
```

### Usage example

```python
call_id = str(uuid.uuid4())
yield response.tool_call(
    "search_products",
    {"query": "laptop"},
    display_name="Searching products...",
    id=call_id
)
# ... execute tool ...
yield response.tool_result(
    "search_products",
    {"items": [...]},
    args={"query": "laptop"},
    id=call_id
)
```

## 3. Azure Cosmos DB NoSQL Adapter

### Container structure

Single database, partitioned by `worker_id`:

| Container | Partition Key | Documents |
|-----------|---------------|-----------|
| `threads` | `/worker_id` | Thread records |
| `events` | `/worker_id` | ThreadEvent records (with `thread_id` for filtering) |
| `runs` | `/worker_id` | Run records |
| `settings` | `/worker_id` | Settings documents |

### WorkerConfig additions

```python
@dataclass
class WorkerConfig:
    database_type: Literal["postgres", "sqlite", "cosmos"]

    # Cosmos DB specific (defaults from WORKER_COSMOS_* env vars)
    cosmos_endpoint: str | None = None
    cosmos_key: str | None = None
    cosmos_database: str | None = None
```

### Dependencies

```toml
[project.optional-dependencies]
cosmos = ["azure-cosmos>=4.7.0"]
```

### Query patterns

- `list_threads(worker_id)` → Query `threads` container where `worker_id = X`, order by `last_event_at`
- `get_events(thread_id)` → Query `events` container where `thread_id = X`, order by `created_at`
- Cross-partition queries avoided since `worker_id` is always in context

## 4. Database Schema & Migration

### ThreadEvent schema

```python
class ThreadEvent(BaseModel):
    # ... existing fields ...
    author: str | None = None  # New field
```

### SQLite/PostgreSQL migration

```sql
ALTER TABLE worker_events ADD COLUMN author TEXT;
```

### Cosmos DB

No schema migration needed - documents naturally accept the new `author` field.

### Tool call data

No schema changes - `data` is already a JSON/dict field. New fields (`id`, `display_name`, `args`) stored within `data`.

### Backward compatibility

All new fields are optional:
- `author` defaults to `None`
- `id` auto-generated if not provided
- `display_name` optional
- `args` on `tool_result` optional

No breaking changes.

## Files to Modify

| File | Changes |
|------|---------|
| `config.py` | Add `assistant_name`, `cosmos_endpoint`, `cosmos_key`, `cosmos_database` |
| `protocol/models.py` | Add `author` field to `ThreadEvent` |
| `handlers/response.py` | Add `author` param to `text`, `image`, `document`; update `tool_call` and `tool_result` |
| `handlers/router.py` | Pass `author` through to events, generate `id` for tool calls |
| `persistence/sqlite.py` | Add `author` column in migration, handle in CRUD |
| `persistence/postgres.py` | Add `author` column in migration, handle in CRUD |
| `migrations/schemas.py` | Update schema version |

## New Files

| File | Purpose |
|------|---------|
| `persistence/cosmos.py` | Azure Cosmos DB NoSQL adapter |

## pyproject.toml

```toml
[project.optional-dependencies]
cosmos = ["azure-cosmos>=4.7.0"]
dev = [...]
```
