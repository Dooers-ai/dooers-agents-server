# Dispatch: Multiple Entry Points for WorkerServer

**Date**: 2026-02-12
**Status**: Implemented

## Problem

The `WorkerServer` currently has a single entry point — `handle(websocket, handler)` — which ties handler execution to a WebSocket connection. In practice, agent systems receive input from many sources: WhatsApp webhooks, Stripe webhooks, REST upload endpoints, third-party API callbacks, scheduled jobs, etc.

These external sources need to:

- Send events back to the frontend through the **existing WebSocket connections**
- Access the same **memory, settings, analytics** infrastructure
- Either inject events into **existing threads** (by ID) or **create new threads**
- Run their own handler logic (same or different agent function)

There is no clean way to do this today. Previous versions exposed `worker_server.persistence` and `worker_server.broadcast` directly, forcing consumers to wire into internals.

## Design

### 1. `worker_server.dispatch()` — Programmatic Entry Point

A symmetric counterpart to `handle()`. Where `handle()` wires a WebSocket as the input source, `dispatch()` is a programmatic input source. The handler's output is persisted and broadcast to WS subscribers automatically.

**Signature:**

```python
stream = await worker_server.dispatch(
    handler: Handler,               # async generator handler to run
    worker_id: str,                 # which worker
    organization_id: str,           # tenant context
    workspace_id: str,              # workspace context
    message: str,                   # text message (convenience)
    user_id: str,                   # who sent this
    user_name: str | None = None,   # display name
    user_email: str | None = None,  # email
    user_role: str | None = None,   # role
    thread_id: str | None = None,   # existing thread or None for new
    thread_title: str | None = None,# title for new threads
    content: list[dict] | None = None, # full ContentPart list (alternative to message)
) -> DispatchStream
```

**Two-phase lifecycle:**

1. **Setup phase** (`await`): ensures persistence is initialized, creates or validates the thread, creates the incoming user event, persists and broadcasts it. After this resolves, `stream.thread_id` is available.

2. **Handler phase** (async iteration): runs the handler, yields `WorkerEvent` objects. Each event is persisted and broadcast to WS subscribers **before** being yielded to the caller.

### 2. `DispatchStream` — Return Object

```python
class DispatchStream:
    thread_id: str          # available immediately after await
    event_id: str           # the user event that was created
    is_new_thread: bool     # whether a new thread was created

    async def __aiter__(self):
        """Yields WorkerEvent objects as the handler produces them.
        Each event is already persisted + broadcast before being yielded."""
        ...

    async def collect(self) -> list[WorkerEvent]:
        """Run the handler to completion, return all events."""
        ...
```

**Stream events** — the full set of `WorkerEvent` types the handler can yield:

| `event.type` | Source | Description |
|---|---|---|
| `run_start` | `send.run_start()` | Agent run began |
| `text` | `send.text(...)` | Text message from agent |
| `image` | `send.image(...)` | Image attachment |
| `document` | `send.document(...)` | Document attachment |
| `tool_call` | `send.tool_call(...)` | Agent invoked a tool |
| `tool_result` | `send.tool_result(...)` | Tool returned a result |
| `tool_transaction` | `send.tool_transaction(...)` | Call + result combined |
| `thread_update` | `send.update_thread(...)` | Thread metadata changed |
| `run_end` | `send.run_end(...)` | Agent run finished |

The stream yields everything unfiltered. Callers match on what they need.

### 3. `worker_server.repository()` — Data Access Utility

A clean CRUD + query interface over all database tables, replacing direct persistence access.

```python
repo = await worker_server.repository()
```

Each table gets a consistent interface — `list`, `get`, `create`, `update`, `remove` — with `filter` and `order` arguments:

```python
# Threads
threads = await repo.list_threads(
    filter={"worker_id": "worker-123", "organization_id": "org-456"},
    order={"field": "last_event_at", "direction": "desc"},
    limit=50,
)
thread = await repo.get_thread(thread_id="thread-abc")
thread = await repo.create_thread(worker_id="...", organization_id="...", ...)
await repo.update_thread(thread_id="...", title="New title")
await repo.remove_thread(thread_id="thread-abc")

# Events
events = await repo.list_events(
    filter={"thread_id": "thread-abc", "actor": "assistant"},
    order={"field": "created_at", "direction": "asc"},
    limit=50,
)
event = await repo.get_event(event_id="evt-123")
event = await repo.create_event(thread_id="...", type="message", actor="system", ...)
await repo.remove_event(event_id="evt-123")

# Runs
runs = await repo.list_runs(
    filter={"thread_id": "thread-abc", "status": "succeeded"},
    order={"field": "started_at", "direction": "desc"},
)
run = await repo.get_run(run_id="run-123")

# Settings
settings = await repo.get_settings(worker_id="worker-123")
await repo.update_settings(worker_id="worker-123", values={"model": "gpt-4o"})
```

Note: a `worker_metadata` table is planned as a future addition (separate design). The repository will be extended with metadata methods at that point.

### 4. Utility Accessors — `memory()`, `settings()`, `analytics()`

These expose the same objects handlers receive, for use outside handlers (in webhook/REST code):

```python
# LLM-formatted conversation history
memory = await worker_server.memory(thread_id="thread-abc")
history = await memory.get_history(limit=20, format="openai")

# Worker settings with broadcast behavior
settings = await worker_server.settings(worker_id="worker-123")
enabled = await settings.get_field("whatsapp_enabled")

# Analytics event tracking
analytics = await worker_server.analytics(
    worker_id="worker-123",
    thread_id="thread-abc",
)
await analytics.track("webhook.received", data={"source": "whatsapp"})
```

These are kept separate from `repository()` because they have behavior beyond raw data access — settings broadcasts changes to WS subscribers, analytics batches and flushes to webhooks.

### 5. Error Handling

**Setup errors** (during `await worker_server.dispatch(...)`):

Infrastructure failures — database down, invalid arguments. Raised directly to the caller. No thread created, no events broadcast.

```python
from dooers import DispatchError

try:
    stream = await worker_server.dispatch(...)
except DispatchError as e:
    logger.error("dispatch setup failed: %s", e)
```

**Handler errors** (during async iteration):

Agent failures — LLM timeout, tool crash, unhandled exception. The pipeline handles cleanup internally before re-raising:

1. Logs the error
2. Marks the current run as `failed`
3. Creates a system error event in the thread
4. Broadcasts both to WS subscribers
5. Re-raises to the caller

```python
from dooers import HandlerError

try:
    async for event in stream:
        ...
except HandlerError as e:
    # Thread is already in a clean state (error event persisted, run marked failed)
    # WS subscribers already see the error
    # e.original contains the original exception
    await whatsapp_api.send(phone, "Sorry, something went wrong.")
```

Principle: the pipeline always cleans up internally before the exception reaches the caller. The caller never has to worry about inconsistent thread state.

### 6. Internal Architecture — HandlerPipeline

The handler execution logic is extracted from `Router._handle_event_create()` into a shared `HandlerPipeline` component.

```
┌─────────────────────────────────────────────────────────────┐
│                      HandlerPipeline                        │
│                                                             │
│  1. Get or create thread -> persist + broadcast             │
│  2. Create user event -> persist + broadcast                │
│  3. Build context (incoming, send, memory, analytics, settings) │
│  4. Iterate handler -> for each yielded event:              │
│     - persist to DB                                         │
│     - broadcast to WS subscribers                           │
│     - track analytics                                       │
│  5. Error handling -> error event + failed run              │
│                                                             │
│  Dependencies: persistence, registry, subscriptions,        │
│  analytics_collector, settings_schema, config               │
└──────────────┬──────────────────────────┬───────────────────┘
               │                          │
       ┌───────▼────────┐       ┌─────────▼──────────┐
       │ Router (WS)     │       │ dispatch()          │
       │                 │       │                     │
       │ + validate conn │       │ + build context     │
       │ + send ACK      │       │   from arguments    │
       │ + extract context│       │ + return            │
       │   from frame    │       │   DispatchStream    │
       └─────────────────┘       └─────────────────────┘
```

Both paths become thin wrappers. Router adds WS-specific behavior (ACK, connection validation, context extraction from frame). Dispatch adds the DispatchStream return type. Neither duplicates persist/broadcast logic.

## Full Usage Example

```python
from fastapi import FastAPI, WebSocket
from dooers import WorkerServer, WorkerConfig

app = FastAPI()

worker_server = WorkerServer(
    config=WorkerConfig(
        database_type="postgres",
        assistant_name="Support Agent",
    )
)

# ─── Handler definitions ───

async def run_main_agent(incoming, send, memory, analytics, settings):
    yield send.run_start()
    history = await memory.get_history(limit=20, format="openai")
    response = await openai_chat(history, incoming.message)
    yield send.text(response)
    yield send.run_end()

async def run_whatsapp_agent(incoming, send, memory, analytics, settings):
    yield send.run_start()
    history = await memory.get_history(limit=20, format="anthropic")
    response = await anthropic_chat(history, incoming.message)
    yield send.text(response)
    yield send.run_end()

async def run_payment_notification(incoming, send, memory, analytics, settings):
    yield send.run_start()
    yield send.text(incoming.message)
    yield send.run_end()

# ─── WebSocket entry point (existing) ───

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, run_main_agent)

# ─── REST/Webhook entry points (new) ───

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(payload: dict):
    repo = await worker_server.repository()

    # Find existing thread for this phone
    thread = await repo.find_thread(
        filter={"worker_id": "worker-123"},
        metadata={"phone": payload["phone"]},
    )

    stream = await worker_server.dispatch(
        handler=run_whatsapp_agent,
        worker_id="worker-123",
        organization_id="org-456",
        workspace_id="ws-789",
        message=payload["text"],
        user_id=f"wa:{payload['phone']}",
        user_name=payload.get("name"),
        thread_id=thread.id if thread else None,
        thread_title=f"WhatsApp: {payload['phone']}",
    )

    # Tag new threads for future lookups
    if stream.is_new_thread:
        await repo.set_metadata(
            thread_id=stream.thread_id,
            key="phone",
            value=payload["phone"],
        )

    # Stream handler events, forward text replies to WhatsApp
    async for event in stream:
        if event.send_type == "text":
            await whatsapp_api.send(payload["phone"], event.data["text"])

@app.post("/webhook/stripe")
async def stripe_webhook(payload: dict):
    stream = await worker_server.dispatch(
        handler=run_payment_notification,
        worker_id="worker-123",
        organization_id="org-456",
        workspace_id="ws-789",
        message=f"Payment {payload['status']}: ${payload['amount']}",
        user_id="system",
        thread_id=payload["metadata"]["thread_id"],
    )
    await stream.collect()

@app.post("/upload")
async def upload_endpoint(file: UploadFile, thread_id: str):
    url = await storage.upload(file)

    stream = await worker_server.dispatch(
        handler=run_main_agent,
        worker_id="worker-123",
        organization_id="org-456",
        workspace_id="ws-789",
        message=f"Process this document: {file.filename}",
        content=[{"type": "document", "url": url, "filename": file.filename}],
        user_id="user-from-auth",
        thread_id=thread_id,
    )
    await stream.collect()
    return {"thread_id": stream.thread_id}
```

## Summary

| Component | Purpose |
|---|---|
| `worker_server.dispatch()` | Programmatic entry point — run a handler from any source |
| `DispatchStream` | Return object with `thread_id`, async iteration, `collect()` |
| `HandlerPipeline` | Extracted handler execution engine shared by WS and dispatch |
| `worker_server.repository()` | CRUD + query interface for all tables |
| `worker_server.memory()` | LLM-formatted history outside handlers |
| `worker_server.settings()` | Worker settings with broadcast behavior |
| `worker_server.analytics()` | Event tracking outside handlers |
