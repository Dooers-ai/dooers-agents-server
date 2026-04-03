# dooers-agents-server

Python agents server SDK for agents client SDK.

## Install

```bash
pip install dooers-agents-server
```

## How It Works

The SDK has two entry points for executing handlers:

```
WebSocket (real-time chat)          Everything else (REST, webhooks, cron)
─────────────────────────           ────────────────────────────────────────
Client connects via WS              Your code calls dispatch()
  ↓                                   ↓
agent_server.handle()              agent_server.dispatch()
  ↓                                   ↓
Context from WS frame               Context from your parameters
  ↓                                   ↓
  └──────────── handler(incoming, send, memory, analytics, settings) ──┘
                                   ↓
                          Same handler, same API
```

**The handler is always the same.** Whether the message came from a WebSocket or a REST endpoint, the handler receives the same parameters and yields the same events.

## Quick Start — WebSocket

The primary entry point. A WebSocket connection sends messages, and the handler responds in real-time.

```python
from fastapi import FastAPI, WebSocket
from openai import AsyncOpenAI
from dooers import AgentServer, AgentConfig

app = FastAPI()
openai = AsyncOpenAI()

agent_server = AgentServer(AgentConfig(
    database_type="sqlite",
    database_name="agent.db",
    assistant_name="My Assistant",
))


async def agent_handler(incoming, send, memory, analytics, settings):
    yield send.run_start()

    history = await memory.get_history(limit=20, format="openai")

    completion = await openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            *history,
        ],
    )

    yield send.text(completion.choices[0].message.content)
    yield send.update_thread(title=incoming.message[:60])
    yield send.run_end()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    await agent_server.handle(websocket, agent_handler)
```

## Quick Start — Dispatch

For REST endpoints, webhooks, background jobs — anything outside a WebSocket connection. You provide the context explicitly and iterate over the handler's events.

```python
from dooers import User

@app.post("/api/webhook")
async def webhook(request: Request):
    body = await request.json()

    stream = await agent_server.dispatch(
        handler=agent_handler,       # Same handler as WebSocket
        agent_id="agent-1",
        organization_id="org-1",
        workspace_id="ws-1",
        message=body["text"],
        user=User(user_id=body["user_id"], user_name="Alice"),
        thread_id=body.get("thread_id"),   # None → creates new thread
        thread_title="Webhook conversation",
    )

    # Stream events from the handler
    async for event in stream:
        if event.send_type == "text":
            print(event.data["text"])

    return {"thread_id": stream.thread_id, "is_new": stream.is_new_thread}
```

## Handler

Handlers are async generators that receive context and yield response events.

```python
async def agent_handler(incoming, send, memory, analytics, settings):
    ...
```

### incoming

The incoming message and its context.

Messages can contain text, images, and documents. `incoming.message` is a convenience shortcut that joins all text parts into a single string. `incoming.content` is the full list of content parts — use it when you need to handle images or documents.

```python
incoming.message                     # str — all text parts joined with spaces
incoming.content                     # list[ContentPart] — full content parts

# ContentPart types:
# TextPart:     { type: "text", text: str }
# ImagePart:    { type: "image", url: str, mime_type?, alt? }
# DocumentPart: { type: "document", url: str, filename: str, mime_type: str }

incoming.context.thread_id           # str
incoming.context.event_id            # str
incoming.context.organization_id     # str
incoming.context.workspace_id        # str
incoming.context.thread_title        # str | None
incoming.context.thread_created_at   # datetime | None

# User (incoming.context.user)
incoming.context.user.user_id             # str
incoming.context.user.user_name           # str | None
incoming.context.user.user_email          # str | None
incoming.context.user.system_role         # str — "admin" | "user"
incoming.context.user.organization_role   # str — "owner" | "manager" | "member"
incoming.context.user.workspace_role      # str — "manager" | "member"
```

### send

Yield events back to the client.

```python
# Messages
yield send.text("Hello!")
yield send.text("Hello!", author="Support Bot")   # Override assistant_name
yield send.image(url, mime_type?, alt?, author?)
yield send.document(url, filename, mime_type, author?)

# Tool calls
yield send.tool_call(name, args, display_name?, id?)
yield send.tool_result(name, result, args?, display_name?, id?)

# Thread metadata
yield send.update_thread(title="New title")

# Run lifecycle
yield send.run_start(agent_id?)
yield send.run_end(status?, error?)
```

### memory

Conversation history in multiple formats.

```python
# LLM-formatted dicts (ready to pass to your model)
messages = await memory.get_history(limit=20, format="openai")
messages = await memory.get_history(format="anthropic")
messages = await memory.get_history(format="google")
messages = await memory.get_history(format="cohere")

# Chronological order
messages = await memory.get_history(limit=50, order="asc")

# Filter by fields
messages = await memory.get_history(filters={"actor": "user"})

# Raw ThreadEvent objects (for manual conversion)
events = await memory.get_history_raw(limit=50)
events = await memory.get_history_raw(limit=50, order="asc", filters={"type": "message"})
```

### analytics

```python
await analytics.track("event.name", data={"key": "value"})
await analytics.like("event", target_id, reason?)
await analytics.dislike("event", target_id, reason?)
```

### settings

```python
value = await settings.get("field_id")
all_values = await settings.get_all()
all_values = await settings.get_all(exclude=["avatar_base64"])
await settings.set("field_id", new_value)
```

## Dispatch API

Full signature for programmatic handler execution.

```python
from dooers import User

stream = await agent_server.dispatch(
    handler=my_handler,
    agent_id="agent-1",
    organization_id="org-1",
    workspace_id="ws-1",
    message="Hello from webhook",
    user=User(                      # optional — defaults to User(user_id="")
        user_id="user-1",
        user_name="Alice",
        user_email="alice@example.com",
        system_role="user",         # "admin" | "user"
        organization_role="member", # "owner" | "manager" | "member"
        workspace_role="member",    # "manager" | "member"
    ),
    thread_id=None,                 # None → creates new thread
    thread_title="Webhook",         # title for new threads
    content=None,                   # optional list of ContentPart
)

# Available immediately (before iterating)
stream.thread_id       # str
stream.event_id        # str
stream.is_new_thread   # bool

# Iterate to run the handler
async for event in stream:
    if event.send_type == "text":
        print(event.data["text"])

# Or collect all events at once
events = await stream.collect()
```

### Error Handling

```python
from dooers import DispatchError, HandlerError

try:
    stream = await agent_server.dispatch(...)
except DispatchError:
    # Setup failed (bad parameters, DB error)
    pass

try:
    async for event in stream:
        ...
except HandlerError as e:
    # Handler raised during execution
    # Pipeline already cleaned up (logged, run marked failed, error event created)
    print(e.original)  # The original exception
```

## Repository

Direct database access for threads, events, runs, and settings.

```python
repo = await agent_server.repository()

# Threads
threads = await repo.list_threads(filter={"agent_id": "w1", "organization_id": "org1"})
thread = await repo.get_thread(thread_id)
thread = await repo.create_thread(agent_id, organization_id, workspace_id, user_id, title?)
thread = await repo.update_thread(thread_id, title="New title")
await repo.remove_thread(thread_id)

# Events
events = await repo.list_events(filter={"thread_id": "t1"}, order={"direction": "asc"})
event = await repo.get_event(event_id)
event = await repo.create_event(thread_id, type="message", actor="user", content=[...])
await repo.remove_event(event_id)

# Runs
runs = await repo.list_runs(filter={"thread_id": "t1"})
run = await repo.get_run(run_id)

# Settings
values = await repo.get_settings(agent_id)
await repo.update_settings(agent_id, {"key": "value"})
```

## Standalone Utilities

Access memory, settings, and analytics outside of handlers.

```python
memory = await agent_server.memory(thread_id)
history = await memory.get_history(limit=20, format="openai")

settings = await agent_server.settings(agent_id)
value = await settings.get("model")

analytics = await agent_server.analytics(agent_id, thread_id="t1")
await analytics.track("custom.event")
```

## Settings Schema

**Full reference:** [docs/agent-settings.md](docs/agent-settings.md) (field parameters, internal vs public fields, frontend rendering, WebSocket frames).

Define configurable settings for your agent.

```python
from dooers import (
    AgentConfig,
    AgentServer,
    SettingsSchema,
    SettingsField,
    SettingsFieldGroup,
    SettingsFieldType,
    SettingsFieldVisibility,
    SettingsSelectOption,
)

schema = SettingsSchema(
    fields=[
        SettingsField(
            id="model",
            type=SettingsFieldType.SELECT,
            label="Model",
            value="gpt-4o-mini",
            options=[
                SettingsSelectOption(value="gpt-4o-mini", label="GPT-4o Mini"),
                SettingsSelectOption(value="gpt-4o", label="GPT-4o"),
            ],
        ),
        SettingsFieldGroup(
            id="advanced",
            label="Advanced Settings",
            collapsible="closed",
            visibility=SettingsFieldVisibility.USER,
            fields=[
                SettingsField(
                    id="temperature",
                    type=SettingsFieldType.NUMBER,
                    label="Temperature",
                    value=0.7,
                    min=0,
                    max=2,
                ),
            ],
        ),
        SettingsField(
            id="api_key",
            type=SettingsFieldType.PASSWORD,
            label="API Key",
            visibility=SettingsFieldVisibility.INTERNAL,  # handler-only; not sent over WebSocket
        ),
    ]
)

agent_server = AgentServer(AgentConfig(
    database_type="sqlite",
    database_name="agent.db",
    settings_schema=schema,
))
```

### Field Types

- `TEXT` — Single-line text input
- `NUMBER` — Numeric input with optional min/max
- `SELECT` — Dropdown selection
- `CHECKBOX` — Boolean toggle
- `TEXTAREA` — Multi-line text input
- `PASSWORD` — Password input (hidden)
- `EMAIL` — Email input
- `DATE` — Date picker
- `IMAGE` — Display-only image (e.g., QR codes)

### Field Groups

Groups organize related fields with an optional collapsible UI:

```python
SettingsFieldGroup(
    id="group_id",
    label="Group Label",
    collapsible="open",    # "open" | "closed" | None (not collapsible)
    visibility=SettingsFieldVisibility.USER,
    fields=[...],
)
```

### Field visibility (`SettingsFieldVisibility`)

- **`INTERNAL`** — Not sent over WebSocket; only via `settings.get()` / `get_all()` / `set()` in the handler.
- **`CREATOR`** — Studio / builder UI: subscribe with `audience=creator` (org admin/manager or agent owner). Patches require that subscription.
- **`USER`** — Default runtime config: subscribe with `audience=user` (default). WebSocket `settings.subscribe` payload supports `audience` and `agent_owner_user_id` (for creator access).

### Internal Fields

Fields with `visibility=INTERNAL` are:
- Hidden from frontend settings snapshots
- Rejected if a WebSocket client attempts to patch them
- Suppressed from broadcast patch notifications
- Accessible normally via `settings.get()` and `settings.get_all()` in handlers

## Database Configuration

Three database backends: PostgreSQL, SQLite, and Azure Cosmos DB.

### PostgreSQL (default)

```python
agent_server = AgentServer(AgentConfig(
    database_type="postgres",
    database_host="localhost",
    database_port=5432,
    database_user="postgres",
    database_name="mydb",
    database_password="secret",
    database_ssl=False,
    database_table_prefix="agent_",
    database_auto_migrate=True,
))
```

### SQLite

```python
agent_server = AgentServer(AgentConfig(
    database_type="sqlite",
    database_name="agent.db",
    database_table_prefix="agent_",
    database_auto_migrate=True,
))
```

### Azure Cosmos DB

Requires the cosmos extra: `pip install dooers-agents-server[cosmos]`

```python
agent_server = AgentServer(AgentConfig(
    database_type="cosmos",
    database_host="https://your-account.documents.azure.com:443/",
    database_name="your-database",
    database_key="your-cosmos-key",
    database_table_prefix="agent_",
    database_auto_migrate=True,
))
```

### Environment Variables

| Field | Environment Variable |
|-------|---------------------|
| `database_host` | `AGENT_DATABASE_HOST` |
| `database_port` | `AGENT_DATABASE_PORT` |
| `database_user` | `AGENT_DATABASE_USER` |
| `database_name` | `AGENT_DATABASE_NAME` |
| `database_password` | `AGENT_DATABASE_PASSWORD` |
| `database_key` | `AGENT_DATABASE_KEY` |
| `database_ssl` | `AGENT_DATABASE_SSL` |

## User Roles and Thread Scoping

Users have three role levels that determine thread visibility:

```python
from dooers import User

user = User(
    user_id="user-1",
    user_name="Alice",
    user_email="alice@example.com",
    system_role="user",         # "admin" | "user"
    organization_role="member", # "owner" | "manager" | "member"
    workspace_role="member",    # "manager" | "member"
)
```

When listing threads, the SDK resolves the user's highest scope and filters accordingly:

| Scope | Condition | Sees |
|-------|-----------|------|
| `admin` | `system_role == "admin"` | All threads |
| `organization` | `organization_role` is `"owner"` or `"manager"` | All threads in the organization |
| `workspace` | `workspace_role == "manager"` | All threads in the workspace |
| `member` | Default | Only threads where they are a participant |

Threads track participants automatically — each user who sends a message is added to the thread's `users` list.

## Thread Privacy

Enable `private_threads` to restrict users to only their own threads:

```python
agent_server = AgentServer(AgentConfig(
    database_type="postgres",
    database_name="mydb",
    private_threads=True,
))
```

When enabled:
- Thread listing filters by the connected user's `user_id`
- Each user only sees threads they created
- Useful for multi-tenant or personal assistant scenarios

## Examples

See [`examples/`](examples/) for complete working examples:

| Example | Entry Point | Description |
|---------|-------------|-------------|
| [`fastapi_basic.py`](examples/fastapi_basic.py) | WebSocket | Minimal echo handler |
| [`fastapi_openai.py`](examples/fastapi_openai.py) | WebSocket | OpenAI chat completions |
| [`fastapi_anthropic.py`](examples/fastapi_anthropic.py) | WebSocket | Anthropic Claude |
| [`fastapi_vertex.py`](examples/fastapi_vertex.py) | WebSocket | Google Vertex AI |
| [`fastapi_tools.py`](examples/fastapi_tools.py) | WebSocket | Tool calls with OpenAI |
| [`fastapi_langchain.py`](examples/fastapi_langchain.py) | WebSocket | LangChain integration |
| [`fastapi_langgraph.py`](examples/fastapi_langgraph.py) | WebSocket | LangGraph ReAct agent |
| [`fastapi_openai_agents.py`](examples/fastapi_openai_agents.py) | WebSocket | OpenAI Agents SDK |
| [`fastapi_multiple_endpoints.py`](examples/fastapi_multiple_endpoints.py) | Both | WebSocket + REST dispatch |
| [`fastapi_whatsapp_webhook.py`](examples/fastapi_whatsapp_webhook.py) | Dispatch | WhatsApp webhook with customer lookup |

## See Also

- [dooers-agents-client](https://github.com/Dooers-ai/dooers-agents-client) — React SDK that connects to agents built with this server
