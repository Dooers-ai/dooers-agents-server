# WorkerIncoming Refactor & Dispatch/Analytics Unification Design

**Date:** 2026-02-13
**Status:** Design Phase
**Scope:** Rename `WorkerOn` to `WorkerIncoming`, restructure context metadata, unify dispatch and handler APIs, document standalone utilities

---

## Problem Statement

The current API has several clarity issues:

1. **Naming confusion:** `on` suggests event reactivity ("reacting to events") when it's actually the incoming request context. This creates mental friction.

2. **Scattered metadata:** Context fields like `thread_id`, `user_id`, `organization_id` are top-level attributes on `WorkerOn`, making it unclear that they're contextual metadata rather than primary data.

3. **Dispatch API inconsistency:** Dispatch flows lack clear context management patterns compared to WebSocket handlers, making it unclear how utilities should work outside handlers.

4. **Undocumented utilities:** `repository()` isn't documented as a first-class standalone utility like the others, appearing fundamentally different.

5. **Documentation gaps:** The README poorly explains dispatch with no practical examples showing the relationship between WebSocket handlers and dispatch entry points.

---

## Solution Overview

### 1. Naming & Core API Consistency

**Change:** Rename `WorkerOn` → `WorkerIncoming` and pair with `send` for directional clarity.

The `incoming` / `send` pair creates intuitive semantics:
- `incoming`: data flowing **IN** (request/context)
- `send`: data flowing **OUT** (response)

```python
async def handler(incoming: WorkerIncoming, send: WorkerSend,
                  memory, analytics, settings):
    # incoming: request data and context
    # send: response factory
    pass
```

### 2. Context Metadata Grouping

**Change:** Group all contextual metadata under `incoming.context` namespace.

```python
class WorkerIncoming:
    # Primary message content
    message: str                    # Extracted text
    content: list[ContentPart]      # Full content parts

    # All context metadata
    context: WorkerContext

class WorkerContext:
    # Identifiers and metadata
    thread_id: str
    event_id: str
    organization_id: str            # Required - organizational anchor
    workspace_id: str               # Required - workspace anchor

    # User information
    user_id: str
    user_name: str
    user_email: str
    user_role: str

    # Thread information
    thread_title: str | None
    thread_created_at: datetime | None
```

**Mental Model:** `incoming.context` contains "where and who" — all contextual information that directs the message to the right place and user.

**Migration:** Access changes from `on.thread_id` → `incoming.context.thread_id`

### 3. Dispatch API & Context-Aware Utilities

**Change:** Dispatch accepts context parameters directly, making context explicit and utilities work identically to handlers.

```python
# Dispatch with full context - utilities work the same as in handlers
await worker_server.dispatch(
    handler_id="agent_handler",

    # Required context (organizational anchor)
    organization_id="org_123",
    workspace_id="ws_456",

    # Optional context (depends on handler needs)
    thread_id="thread_789",
    user_id="user_101",
    user_name="John Doe",
    user_email="john@example.com",
    user_role="admin",
    thread_title="Support Request",

    # Message content
    message="Process this data",
    content=[ContentPart(type="text", text="Process this data")]
)
```

**Internal Behavior:** Dispatch internally constructs a `WorkerIncoming` object with the provided context, then passes it to the handler. The handler doesn't know (or care) whether the context came from WebSocket or dispatch — utilities work identically.

**Key Insight:** By requiring `organization_id` and `workspace_id` upfront, we eliminate the possibility of orphan workers. All dispatch calls are grounded in organizational context.

### 4. Standalone Utilities Consistency

**Change:** Document all standalone utilities (`memory`, `analytics`, `settings`, `repository`) as first-class citizens with consistent APIs.

**Three Usage Patterns:**

#### Pattern A: Inside Handlers/Dispatch (Context Implicit)
```python
async def handler(incoming, send, memory, analytics, settings, repository):
    # Context automatically available from incoming
    history = await memory.get_history(limit=20)
    analytics.track("event", data={})
    config = settings.get("feature_flag")
    thread = await repository.get_thread(incoming.context.thread_id)
```

Context flows automatically from `WorkerIncoming` to all utilities.

#### Pattern B: Standalone with Initialization Context
```python
# Initialize utilities with required context
analytics = worker_server.analytics(
    worker_id="worker_1",
    thread_id="t_123",
    organization_id="org_123"
)
analytics.track("custom_event", data={"value": 100})

memory = worker_server.memory(
    worker_id="worker_1",
    thread_id="t_123"
)
history = await memory.get_history()

repository = worker_server.repository(worker_id="worker_1")
thread = await repository.get_thread("t_123")
```

Context is baked in at initialization; utilities use it implicitly.

#### Pattern C: Flexible Context (For Reporting/Admin)
```python
# Initialize with worker only, provide context per-call
analytics = worker_server.analytics(worker_id="worker_1")
analytics.track(
    "event",
    data={},
    thread_id="t_123",
    organization_id="org_456"
)
```

Context provided on each call for maximum flexibility.

**Key Principle:** Utilities don't care where context comes from. Once initialized with required context, they work identically whether context came from a handler, dispatch, or explicit parameters.

---

## Side-by-Side: Handler vs. Dispatch Approaches

### Example 1: Basic Message Processing

**Handler (WebSocket):**
```python
async def chat_handler(incoming, send, memory, analytics):
    # Context extracted from WebSocket message
    user_name = incoming.context.user_name
    thread_id = incoming.context.thread_id

    history = await memory.get_history(limit=20)
    yield send.text(f"Hello {user_name}")

    # Analytics automatically has context
    analytics.track("greeting_sent", data={"user": user_name})
```

**Dispatch (Programmatic Entry Point):**
```python
# You provide context explicitly
await worker_server.dispatch(
    handler_id="chat_handler",
    organization_id="org_123",
    workspace_id="ws_456",
    thread_id="scheduled_thread",
    user_name="John Doe",
    message="Process this chat"
)

# Internally dispatches to SAME handler with SAME utilities
# Handler code is identical - utilities work the same way
```

**Key insight:** The handler code doesn't change. Only the context source changes: WebSocket extracts it from the message, dispatch requires you to provide it.

### Example 2: Analytics Tracking in Both Flows

**Handler:**
```python
async def support_handler(incoming, send, analytics):
    # Thread context is implicit in analytics
    analytics.track("support_request", data={"issue": "billing"})
    # Automatically tagged with: thread_id, workspace_id, organization_id
```

**Dispatch:**
```python
await worker_server.dispatch(
    handler_id="support_handler",
    organization_id="org_123",
    workspace_id="ws_456",
    thread_id="support_ticket_789",
    message="Customer issue: billing"
)
# Handler's analytics.track() works exactly the same - context is implicit
```

**Standalone Analytics:**
```python
# Option 1: Baked-in context
analytics = worker_server.analytics(
    worker_id="support_bot",
    thread_id="support_ticket_789"
)
analytics.track("support_request", data={"issue": "billing"})

# Option 2: Flexible context
analytics = worker_server.analytics(worker_id="support_bot")
analytics.track(
    "support_request",
    data={"issue": "billing"},
    thread_id="support_ticket_789"
)
```

**Mental model:**
- **Handler:** Context flows IN with the message → utilities use it implicitly
- **Dispatch:** Context flows IN as parameters → utilities use it implicitly
- **Standalone:** Context either baked in at init or provided per-call

---

## Documentation Changes

### 1. README Restructuring

**Add "Execution Flows" section:**
- Explain WebSocket handlers (real-time, context automatic)
- Explain dispatch (programmatic, context explicit)
- Show that handler code is identical in both flows

**Add "Handler vs. Dispatch" side-by-side examples:**
- Same handler, different entry points
- Show utilities work identically
- Demonstrate context availability in both flows

**Promote standalone utilities to equal status:**
- `memory`: accessing conversation history
- `analytics`: tracking events (with handler + standalone examples)
- `settings`: managing worker configuration
- `repository`: direct database access (currently undocumented, now first-class)

### 2. API Documentation Updates

**WorkerIncoming docstring:**
```
Represents an incoming message with its complete context.

Attributes:
  message: Extracted text from content parts
  content: Full content parts from the message
  context: WorkerContext with metadata (thread, org, user info)
```

**WorkerContext docstring:**
```
Contextual metadata for the incoming message.

Provides "where and who" information:
- Where: thread_id, organization_id, workspace_id
- Who: user_id, user_name, user_email, user_role
- When: thread_created_at

Always present in handlers and dispatch flows.
```

**dispatch() docstring:**
```
Execute a handler with explicit context (programmatic entry point).

Args:
  handler_id: ID of handler to execute
  organization_id: Required - organizational context
  workspace_id: Required - workspace context
  thread_id: Optional - thread context
  message: Message text to process
  content: Message content parts
  user_id: Optional - user context
  user_name: Optional - user context
  user_email: Optional - user context
  user_role: Optional - user context
  thread_title: Optional - thread context
  thread_created_at: Optional - thread context

Returns: Async generator of response events
```

---

## Migration Path

### Phase 1: Add New Interfaces
- Create `WorkerIncoming` and `WorkerContext` classes (keep `WorkerOn` as deprecated alias)
- Add new dispatch signature accepting context parameters
- Keep old dispatch signature working (with deprecation warning)

### Phase 2: Refactor Internally
- Update handler routing to use `WorkerIncoming`
- Update all utilities to use `context` namespace
- Update examples and tests

### Phase 3: Documentation
- Write new README sections
- Update API docs
- Create migration guide for users

### Phase 4: Deprecation
- Mark `WorkerOn` as deprecated
- Add migration warnings in release notes
- Plan removal for next major version

---

## Benefits

1. **Clearer naming:** `incoming` / `send` immediately communicates request/response direction
2. **Better organization:** Context grouped under `context` makes it clear what's metadata vs. message
3. **Consistent APIs:** Handler and dispatch flows use identical utility APIs - no special cases
4. **Reduced confusion:** WebSocket and programmatic entry points have parallel, understandable patterns
5. **First-class utilities:** All utilities documented equally, with handler + standalone examples
6. **Easier learning curve:** New users understand how utilities work in any context

---

## Open Questions

None at this time. Design is complete and ready for implementation planning.
