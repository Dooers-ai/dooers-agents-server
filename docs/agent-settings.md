# Agent settings (worker settings)

This document describes how **configurable settings** work in the agents stack: who defines them, what fields and parameters are available, how values are stored, and how a **frontend** built with **dooers-agents-client** can display and edit them.

## Roles

| Piece | Responsibility |
|-------|------------------|
| **dooers-agents-server** | Provides `SettingsSchema`, `SettingsField`, `SettingsFieldGroup`, `SettingsFieldType`, persistence per `worker_id`, WebSocket frames (`settings.*`), and the handler API `WorkerSettings`. |
| **Your worker (agent)** | Declares **one** `SettingsSchema` instance and passes it in `WorkerConfig(settings_schema=...)`. You choose field IDs, labels, types, defaults, and which fields are user-editable vs internal. |
| **dooers-agents-client** | Subscribes over WebSocket, receives a **snapshot** of schema + current values, and can **patch** individual fields. The UI maps each field `type` to a control. |

The **schema is not** global to the platform: each worker ships its own schema in code (for example `agente_dufrio` uses `schemas.py`).

## End-to-end flow

1. The client sends **`settings.subscribe`** with `worker_id`, optional **`audience`** (`"user"` default or `"creator"`), and for creator access optionally **`agent_owner_user_id`** (platform agent owner from `GET /agents/:id`).
2. The server responds with **`settings.snapshot`** filtered to that audience (`internal` fields are never included).
3. When a user edits a value, the client sends **`settings.patch`** with `{ field_id, value }`.
4. The server validates the field (exists, not `readonly`, `visibility` matches the subscription audience, `INTERNAL` rejected from WS), persists it, and broadcasts **`settings.patch`** to subscribers that should see that field.
5. In the **handler**, your code uses `await settings.get("field_id")` / `get_all()` / `set()` — same storage as the UI (including `INTERNAL` fields).

**Creator audience:** allowed only if the connected user’s `organization_role` is `owner` or `manager`, or their `user_id` matches `agent_owner_user_id` (trusted from the platform client).

## Defining a schema in Python

```python
from dooers import (
    SettingsSchema,
    SettingsField,
    SettingsFieldGroup,
    SettingsFieldType,
    SettingsFieldVisibility,
    SettingsSelectOption,
)

settings_schema = SettingsSchema(
    version="1.0",  # optional, default "1.0"
    fields=[
        SettingsField(
            id="support_email",
            type=SettingsFieldType.EMAIL,
            label="Support email",
            placeholder="team@example.com",
        ),
        SettingsFieldGroup(
            id="advanced",
            label="Advanced",
            collapsible="closed",  # "open" | "closed" | omit for non-collapsible
            fields=[
                SettingsField(
                    id="temperature",
                    type=SettingsFieldType.NUMBER,
                    label="Temperature",
                    value=0.7,
                    min=0.0,
                    max=2.0,
                ),
            ],
        ),
    ],
)
```

Pass it into the server:

```python
worker_server = WorkerServer(WorkerConfig(
    database_type="postgres",
    settings_schema=settings_schema,
    # ...
))
```

**Rules:**

- Every field **`id`** must be unique across the whole schema (including inside groups).
- **`value`** on a field is the **default** when nothing is stored yet; persisted values override defaults (`WorkerSettings.get_all()` merges defaults + DB).

## Field types (`SettingsFieldType`)

Wire / enum values are lowercase strings (e.g. `"text"`, `"email"`).

| Type | Typical use | Extra parameters |
|------|-------------|------------------|
| `text` | Single-line string | `placeholder` |
| `number` | Int or float | `min`, `max` |
| `select` | Enum-like choice | **`options`**: list of `SettingsSelectOption(value=..., label=...)` |
| `checkbox` | Boolean | — |
| `textarea` | Long text | `placeholder`, **`rows`** |
| `password` | Secret string (masked in UIs) | `placeholder` |
| `email` | Email | `placeholder` |
| `date` | Date | — |
| `image` | Display-only image (e.g. QR) | **`src`**, optional **`width`**, **`height`** |

## Parameters on `SettingsField`

Defined in `dooers/features/settings/models.py`:

| Parameter | Meaning |
|-----------|---------|
| `id` | Stable key used in DB, `settings.get(id)`, and WebSocket `field_id`. |
| `type` | One of `SettingsFieldType`. |
| `label` | Human-readable label for forms. |
| `required` | Default `False`. Semantics for validation are enforced by your worker if needed. |
| `readonly` | If `True`, users cannot patch via WebSocket; `settings.set()` in the handler also rejects updates. |
| `value` | Default when no value is stored. |
| `visibility` | `SettingsFieldVisibility`: **`INTERNAL`** (handler-only), **`CREATOR`** (studio snapshot when `audience=creator`), **`USER`** (runtime config when `audience=user`). Default `USER`. |
| `placeholder` | Optional hint text. |
| `options` | For `select` only: `SettingsSelectOption` list. |
| `min` / `max` | For `number` (optional bounds). |
| `rows` | For `textarea` (optional). |
| `src` / `width` / `height` | For `image` (display metadata). |

## Parameters on `SettingsFieldGroup`

| Parameter | Meaning |
|-----------|---------|
| `id` | Group identifier (must not collide with field IDs in the uniqueness rule for flattened fields). |
| `label` | Section title in the UI. |
| `fields` | Nested list of `SettingsField`. |
| `collapsible` | `"open"` \| `"closed"` \| `None` (not collapsible). |
| `visibility` | Same enum as fields; default `USER`. Use **`INTERNAL`** to hide the whole group from WebSocket snapshots. |

**Filtering:** `SettingsSchema.get_fields_for_audience("user" | "creator")` returns items for that surface. `get_public_fields()` is an alias for **`get_fields_for_audience("user")`** (backward compatible).

## Handler API

Inside `async def handler(on, send, memory, analytics, settings):`:

```python
temperature = await settings.get("temperature")
config = await settings.get_all()
await settings.set("temperature", 0.5)
redacted = await settings.get_all(exclude=["api_key"])
```

## Frontend (dooers-agents-client)

### Types

Import display types and a group guard:

```ts
import type { SettingsField, SettingsFieldGroup, SettingsItem } from 'dooers-agents-client'
import { isSettingsFieldGroup } from 'dooers-agents-client'
```

`SettingsItem` is `SettingsField | SettingsFieldGroup`. Nested groups are **not** recursive in the model: a group contains a flat list of fields.

### Subscribing and patching

Use **`useSettings()`** (React) or the underlying **`WorkerClient`**:

- **`subscribe(options?)`** / **`unsubscribe()`** — `subscribe({ audience: 'user' | 'creator', agentOwnerUserId })` for scoped snapshots; default `audience: 'user'`. For Studio/creator fields, pass `audience: 'creator'` and **`agentOwnerUserId`** from the platform agent (`ownerUserId` in the API).
- **`fields`** — `(SettingsField | SettingsFieldGroup)[]` from the latest snapshot, with **`value`** and **`visibility`**.
- **`patchField(fieldId, value)`** — sends `settings.patch`; the server rejects unknown, readonly, or wrong-audience fields.
- **`updatedAt`**, **`isLoading`**

Example pattern:

```tsx
const { fields, subscribe, unsubscribe, patchField } = useSettings()

useEffect(() => {
  subscribe({ audience: 'user' })
  return () => unsubscribe()
}, [subscribe, unsubscribe])
```

### Rendering

The client does **not** ship a full settings form component: you iterate `fields` and branch:

1. If **`isSettingsFieldGroup(item)`**, render a section (optionally collapsible using `item.collapsible`) and map **`item.fields`**.
2. Otherwise treat **`item` as `SettingsField`** and switch on **`item.type`**:
   - `text` → single-line input  
   - `textarea` → multiline (`rows` if set)  
   - `number` → number input (`min` / `max`)  
   - `select` → dropdown (`item.options`)  
   - `checkbox` → checkbox  
   - `password` → masked input  
   - `email` / `date` → appropriate inputs  
   - `image` → `<img src={item.src ?? ''} width={item.width} height={item.height} />` (often read-only)

Use **`item.label`**, **`item.placeholder`**, and **`item.readonly`** to disable editing where appropriate.

### Wire protocol (reference)

| Direction | Frame | Purpose |
|-----------|--------|---------|
| C2S | `settings.subscribe` | `{ worker_id, audience?, agent_owner_user_id? }` |
| C2S | `settings.unsubscribe` | Stop receiving settings broadcasts |
| C2S | `settings.patch` | `{ field_id, value }` |
| S2C | `settings.snapshot` | Schema + values + `updated_at` |
| S2C | `settings.patch` | Broadcast after a successful patch |

If no `settings_schema` is configured on the worker, subscribe/patch return **`NOT_CONFIGURED`**.

## Persistence

Values are stored per **`worker_id`** (same persistence layer as threads/events). Defaults come from the schema; any `settings.set` or UI patch overrides stored JSON for that field id.

## See also

- [README.md](../README.md) — “Settings Schema” quick example and handler `settings` usage  
- [dooers-agents-client README](https://github.com/Dooers-ai/dooers-agents-client) — `useSettings` and store shape  

Source of truth for Python models: `src/dooers/features/settings/models.py`.
