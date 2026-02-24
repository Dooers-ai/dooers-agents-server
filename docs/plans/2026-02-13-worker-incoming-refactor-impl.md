# WorkerIncoming Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rename WorkerOn → WorkerIncoming, reorganize context metadata, unify dispatch/handler APIs, and promote all utilities to first-class status.

**Architecture:** Introduce WorkerContext to group metadata (thread_id, user_id, organization_id, etc.), rename WorkerOn to WorkerIncoming, update dispatch() to accept context parameters directly, and ensure all utilities (memory, analytics, settings, repository) work identically in handler and dispatch flows.

**Tech Stack:** Python 3.10+, Pydantic v2, dataclasses, async/await, pytest

---

## Task 1: Create WorkerContext Class

**Files:**
- Create: `src/dooers/handlers/context.py` (new file)
- Modify: `src/dooers/handlers/__init__.py` (add export)

**Step 1: Create context.py with WorkerContext class**

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class WorkerContext:
    """Contextual metadata for the incoming message.

    Provides "where and who" information:
    - Where: thread_id, organization_id, workspace_id
    - Who: user_id, user_name, user_email, user_role
    - When: thread_created_at

    Always present in handlers and dispatch flows.
    """
    thread_id: str
    event_id: str
    organization_id: str
    workspace_id: str
    user_id: str
    user_name: str
    user_email: str
    user_role: str
    thread_title: str | None = field(default=None)
    thread_created_at: datetime | None = field(default=None)
```

**Step 2: Add WorkerContext to handlers/__init__.py exports**

In `src/dooers/handlers/__init__.py`, add:
```python
from dooers.handlers.context import WorkerContext

__all__ = [
    "WorkerContext",
    # ... existing exports
]
```

**Step 3: Run tests to verify no breakage**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test
```

Expected: All tests pass (imports are new, no behavior change yet)

**Step 4: Commit**

```bash
git add src/dooers/handlers/context.py src/dooers/handlers/__init__.py
git commit -m "feat: create WorkerContext class for metadata grouping"
```

---

## Task 2: Create WorkerIncoming Class

**Files:**
- Create: `src/dooers/handlers/incoming.py` (new file)
- Modify: `src/dooers/handlers/__init__.py` (add export)

**Step 1: Create incoming.py with WorkerIncoming class**

```python
from dataclasses import dataclass

from dooers.handlers.context import WorkerContext
from dooers.protocol.models import ContentPart


@dataclass
class WorkerIncoming:
    """Represents an incoming message with its complete context.

    Attributes:
      message: Extracted text from content parts
      content: Full content parts from the message
      context: WorkerContext with metadata (thread, org, user info)
    """
    message: str
    content: list[ContentPart]
    context: WorkerContext
```

**Step 2: Update handlers/__init__.py to export WorkerIncoming**

```python
from dooers.handlers.context import WorkerContext
from dooers.handlers.incoming import WorkerIncoming

__all__ = [
    "WorkerContext",
    "WorkerIncoming",
    # ... existing exports
]
```

**Step 3: Keep WorkerOn as deprecated alias in on.py**

Modify `src/dooers/handlers/on.py`:

```python
import warnings
from dataclasses import dataclass, field
from datetime import datetime

from dooers.handlers.context import WorkerContext
from dooers.handlers.incoming import WorkerIncoming
from dooers.protocol.models import ContentPart


# Deprecated: Use WorkerIncoming instead
@dataclass
class WorkerOn:
    message: str
    content: list[ContentPart]
    thread_id: str
    event_id: str
    organization_id: str
    workspace_id: str
    user_id: str
    user_name: str
    user_email: str
    user_role: str
    thread_title: str | None = field(default=None)
    thread_created_at: datetime | None = field(default=None)

    def __post_init__(self):
        warnings.warn(
            "WorkerOn is deprecated, use WorkerIncoming instead",
            DeprecationWarning,
            stacklevel=2
        )


# Convenience function to create WorkerIncoming from old WorkerOn parameters
def _convert_on_to_incoming(on: WorkerOn) -> WorkerIncoming:
    """Convert deprecated WorkerOn to new WorkerIncoming structure."""
    context = WorkerContext(
        thread_id=on.thread_id,
        event_id=on.event_id,
        organization_id=on.organization_id,
        workspace_id=on.workspace_id,
        user_id=on.user_id,
        user_name=on.user_name,
        user_email=on.user_email,
        user_role=on.user_role,
        thread_title=on.thread_title,
        thread_created_at=on.thread_created_at,
    )
    return WorkerIncoming(message=on.message, content=on.content, context=context)
```

**Step 4: Run tests to verify**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test
```

Expected: All tests pass, deprecation warnings may appear but shouldn't break tests

**Step 5: Commit**

```bash
git add src/dooers/handlers/incoming.py src/dooers/handlers/on.py src/dooers/handlers/__init__.py
git commit -m "feat: create WorkerIncoming and deprecate WorkerOn"
```

---

## Task 3: Update Handler Type Signature

**Files:**
- Modify: `src/dooers/handlers/pipeline.py` (Handler type and HandlerPipeline)

**Step 1: Update Handler type in pipeline.py**

Find the Handler type definition around line 42 and replace:

```python
# OLD
Handler = Callable[
    [WorkerOn, WorkerSend, WorkerMemory, WorkerAnalytics, WorkerSettings],
    AsyncGenerator[WorkerEvent, None],
]

# NEW
Handler = Callable[
    [WorkerIncoming, WorkerSend, WorkerMemory, WorkerAnalytics, WorkerSettings],
    AsyncGenerator[WorkerEvent, None],
]
```

Also update the imports in pipeline.py to include WorkerIncoming:

```python
from dooers.handlers.incoming import WorkerIncoming
from dooers.handlers.on import WorkerOn  # Keep for now, will remove in deprecation phase
```

**Step 2: Run tests to check for compilation errors**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test 2>&1 | head -50
```

Expected: May show type errors in handler invocations (we'll fix in next tasks)

**Step 3: Note errors for reference**

Document which tests/files reference the handler signature, as we'll update them next.

**Step 4: Commit (partial - acknowledge type breakage for now)**

```bash
git add src/dooers/handlers/pipeline.py
git commit -m "refactor: update Handler type to use WorkerIncoming"
```

---

## Task 4: Update HandlerPipeline.setup() to Create WorkerIncoming

**Files:**
- Modify: `src/dooers/handlers/pipeline.py` (HandlerPipeline.setup() method)

**Step 1: Update HandlerPipeline.setup() method**

In `src/dooers/handlers/pipeline.py`, find the setup() method (around line 89) and update the part where WorkerOn is created. Around line 100-120, replace:

```python
# OLD - somewhere in setup where WorkerOn is created
worker_on = WorkerOn(
    message=context.message,
    content=context.content or [],
    thread_id=thread.id,
    event_id=user_event.id,
    organization_id=context.organization_id,
    workspace_id=context.workspace_id,
    user_id=context.user_id,
    user_name=context.user_name or "",
    user_email=context.user_email or "",
    user_role=context.user_role or "",
    thread_title=thread.title,
    thread_created_at=thread.created_at,
)

# NEW - create WorkerContext and WorkerIncoming
worker_context = WorkerContext(
    thread_id=thread.id,
    event_id=user_event.id,
    organization_id=context.organization_id,
    workspace_id=context.workspace_id,
    user_id=context.user_id,
    user_name=context.user_name or "",
    user_email=context.user_email or "",
    user_role=context.user_role or "",
    thread_title=thread.title,
    thread_created_at=thread.created_at,
)

worker_incoming = WorkerIncoming(
    message=context.message,
    content=context.content or [],
    context=worker_context,
)
```

Update the return statement and PipelineResult to store worker_incoming instead of worker_on:

```python
# If HandlerPipeline.setup stores the on/incoming object, update storage
# Return PipelineResult with worker_incoming available
```

**Step 2: Update HandlerPipeline.execute() to use worker_incoming**

Find the execute() method and any place where it passes arguments to the handler. Replace WorkerOn parameters with WorkerIncoming:

```python
# OLD
async for event in self._handler(worker_on, worker_send, ...):

# NEW
async for event in self._handler(worker_incoming, worker_send, ...):
```

**Step 3: Write a test to verify setup creates WorkerIncoming correctly**

Create test file `tests/handlers/test_pipeline.py` (if not exists):

```python
import pytest
from datetime import datetime, UTC
from dooers.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.handlers.incoming import WorkerIncoming
from dooers.persistence.base import Persistence
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_pipeline_setup_creates_worker_incoming():
    """Test that setup() creates WorkerIncoming with proper context grouping."""
    # Mock persistence
    mock_persistence = AsyncMock(spec=Persistence)

    # Create a simple async generator handler
    async def dummy_handler(incoming, send, memory, analytics, settings):
        assert isinstance(incoming, WorkerIncoming)
        assert incoming.message == "test message"
        assert incoming.context.thread_id is not None
        assert incoming.context.organization_id == "org_123"
        assert incoming.context.workspace_id == "ws_456"
        yield send.text("response")

    pipeline = HandlerPipeline(persistence=mock_persistence)

    context = HandlerContext(
        handler=dummy_handler,
        worker_id="worker_1",
        organization_id="org_123",
        workspace_id="ws_456",
        message="test message",
        user_id="user_1",
        user_name="Test User",
    )

    result = await pipeline.setup(context)

    # Verify result contains valid thread and event
    assert result.thread is not None
    assert result.user_event is not None
    assert result.thread.organization_id == "org_123"
```

**Step 4: Run tests**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test tests/handlers/test_pipeline.py -v
```

Expected: Test passes, verifying WorkerIncoming is created properly

**Step 5: Commit**

```bash
git add src/dooers/handlers/pipeline.py tests/handlers/test_pipeline.py
git commit -m "refactor: update HandlerPipeline.setup/execute to use WorkerIncoming"
```

---

## Task 5: Update Router to Use WorkerIncoming

**Files:**
- Modify: `src/dooers/handlers/router.py` (WebSocket frame routing)

**Step 1: Check router.py for WorkerOn usage**

Look for where the router creates WorkerOn or calls handlers. Search for any handler invocations.

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
grep -n "WorkerOn\|worker_on" src/dooers/handlers/router.py
```

**Step 2: Update router.py imports and handler calls**

Replace WorkerOn with WorkerIncoming in imports and handler invocations in router.py.

**Step 3: Run tests**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test
```

Expected: Tests pass for router functionality

**Step 4: Commit**

```bash
git add src/dooers/handlers/router.py
git commit -m "refactor: update router to use WorkerIncoming"
```

---

## Task 6: Update Dispatch to Accept Context Parameters

**Files:**
- Modify: `src/dooers/server.py` (dispatch() method)
- Modify: `src/dooers/dispatch.py` (if needed)

**Step 1: Update WorkerServer.dispatch() signature**

In `src/dooers/server.py`, find the dispatch() method and update its signature to accept context parameters directly:

```python
# OLD signature (example)
async def dispatch(self, handler_id: str, thread_id: str, message: str):
    pass

# NEW signature
async def dispatch(
    self,
    handler_id: str,
    organization_id: str,  # Required
    workspace_id: str,     # Required
    message: str,
    thread_id: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    user_email: str | None = None,
    user_role: str | None = None,
    thread_title: str | None = None,
    thread_created_at: datetime | None = None,
    content: list[ContentPart] | None = None,
    data: dict | None = None,
):
    """Execute a handler with explicit context (programmatic entry point).

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

    Returns: DispatchStream async generator of response events
    """
    # Implementation:
    # 1. Create HandlerContext with provided parameters
    # 2. Call pipeline.setup(context)
    # 3. Return DispatchStream
    pass
```

**Step 2: Implement dispatch() to validate required fields**

```python
if not organization_id:
    raise ValueError("organization_id is required")
if not workspace_id:
    raise ValueError("workspace_id is required")

# Create HandlerContext
context = HandlerContext(
    handler=handler,
    worker_id=self._worker_id,
    organization_id=organization_id,
    workspace_id=workspace_id,
    message=message,
    thread_id=thread_id,
    user_id=user_id,
    user_name=user_name,
    user_email=user_email,
    user_role=user_role,
    thread_title=thread_title,
    content=content,
    data=data,
)

result = await self._pipeline.setup(context)
return DispatchStream(self._pipeline, context, result)
```

**Step 3: Write test for new dispatch signature**

In `tests/test_dispatch.py` (or create it):

```python
@pytest.mark.asyncio
async def test_dispatch_with_context_parameters():
    """Test dispatch() accepts context parameters and creates WorkerIncoming."""
    # Setup worker server with test handler
    async def test_handler(incoming, send, memory, analytics, settings):
        assert incoming.context.organization_id == "org_123"
        assert incoming.context.workspace_id == "ws_456"
        assert incoming.message == "test"
        yield send.text("response")

    # Register handler
    worker_server.register_handler("test_handler", test_handler)

    # Dispatch with full context
    stream = await worker_server.dispatch(
        handler_id="test_handler",
        organization_id="org_123",
        workspace_id="ws_456",
        thread_id="thread_789",
        user_id="user_101",
        user_name="John Doe",
        message="test"
    )

    # Collect events
    events = await stream.collect()
    assert len(events) > 0
    assert stream.thread_id == "thread_789"
```

**Step 4: Run dispatch tests**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test tests/test_dispatch.py -v
```

Expected: Tests pass

**Step 5: Commit**

```bash
git add src/dooers/server.py tests/test_dispatch.py
git commit -m "feat: update dispatch() to accept context parameters"
```

---

## Task 7: Update Utilities to Use incoming.context

**Files:**
- Modify: `src/dooers/handlers/memory.py`
- Modify: `src/dooers/features/analytics/worker_analytics.py`
- Modify: `src/dooers/features/settings/worker_settings.py`
- Modify: `src/dooers/repository.py`

**Step 1: Update WorkerMemory**

In `src/dooers/handlers/memory.py`, find where it receives `on` parameter and change to `incoming`:

```python
# OLD
async def get_history(self, limit: int = 20, format: str = "openai") -> list:
    # self._thread_id is set from on.thread_id
    pass

# NEW - thread_id now from incoming.context
async def get_history(self, limit: int = 20, format: str = "openai") -> list:
    # self._thread_id is set from incoming.context.thread_id
    pass
```

Update initialization signature:
```python
# In handler context, memory is initialized with incoming parameter
memory = WorkerMemory(incoming, persistence)
# Now it accesses: incoming.context.thread_id
```

**Step 2: Update WorkerAnalytics**

In `src/dooers/features/analytics/worker_analytics.py`:

```python
# Update to use incoming.context instead of top-level fields
class WorkerAnalytics:
    def __init__(self, incoming: WorkerIncoming, collector):
        self._thread_id = incoming.context.thread_id
        self._organization_id = incoming.context.organization_id
        self._workspace_id = incoming.context.workspace_id
        self._collector = collector
```

**Step 3: Update WorkerSettings**

In `src/dooers/features/settings/worker_settings.py`:

```python
# Update to use incoming.context
class WorkerSettings:
    def __init__(self, incoming: WorkerIncoming, broadcaster, schema):
        self._workspace_id = incoming.context.workspace_id
        self._broadcaster = broadcaster
        self._schema = schema
```

**Step 4: Update Repository**

In `src/dooers/repository.py`, ensure it handles context properly when passed directly:

```python
# Repository can be initialized with just worker_id for standalone use
# Then thread_id is optional/passed per-call
class WorkerRepository:
    def __init__(self, worker_id: str, persistence: Persistence):
        self._worker_id = worker_id
        self._persistence = persistence
```

**Step 5: Write tests for utility context access**

Create `tests/handlers/test_utilities_context.py`:

```python
import pytest
from datetime import datetime, UTC
from dooers.handlers.incoming import WorkerIncoming
from dooers.handlers.context import WorkerContext
from dooers.handlers.memory import WorkerMemory
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_memory_uses_incoming_context():
    """Test WorkerMemory accesses thread_id from incoming.context."""
    # Create WorkerIncoming with context
    context = WorkerContext(
        thread_id="t_123",
        event_id="e_456",
        organization_id="org_789",
        workspace_id="ws_000",
        user_id="u_111",
        user_name="Test",
        user_email="test@example.com",
        user_role="user",
    )
    incoming = WorkerIncoming(message="test", content=[], context=context)

    # Mock persistence
    mock_persistence = AsyncMock()

    # Create memory with incoming
    memory = WorkerMemory(incoming, mock_persistence)

    # Verify it has thread_id from context
    assert memory._thread_id == "t_123"
```

**Step 6: Run utility tests**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test tests/handlers/test_utilities_context.py -v
```

Expected: Tests pass

**Step 7: Commit**

```bash
git add src/dooers/handlers/memory.py src/dooers/features/analytics/worker_analytics.py src/dooers/features/settings/worker_settings.py src/dooers/repository.py tests/handlers/test_utilities_context.py
git commit -m "refactor: update utilities to access context from incoming.context"
```

---

## Task 8: Update Handler and Example Files

**Files:**
- Modify: Example handlers and integration tests that use handlers

**Step 1: Find all handler examples**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
find . -name "*.py" -type f | xargs grep -l "async def.*on.*send" | grep -E "(example|test)" | head -10
```

**Step 2: Update example handlers to use incoming.context**

For each handler example, update from:
```python
async def my_handler(on, send, memory, analytics, settings):
    user_id = on.user_id
    thread_id = on.thread_id
```

To:
```python
async def my_handler(incoming, send, memory, analytics, settings):
    user_id = incoming.context.user_id
    thread_id = incoming.context.thread_id
```

**Step 3: Update all handler parameter names in tests**

Search for test files that define handlers and update parameter names from `on` to `incoming`.

**Step 4: Run all tests**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test
```

Expected: All tests pass

**Step 5: Commit**

```bash
git add .
git commit -m "refactor: update examples and tests to use WorkerIncoming"
```

---

## Task 9: Update README Documentation

**Files:**
- Modify: `README.md`

**Step 1: Add "Execution Flows" section to README**

Find the main content section and add before the Handler examples:

```markdown
## Execution Flows

The workers-server SDK supports two ways to execute handlers:

### WebSocket Handlers (Real-Time)
When a client connects via WebSocket, incoming messages are automatically routed to registered handlers. The context (thread_id, organization_id, etc.) is automatically extracted from the message.

### Dispatch (Programmatic Entry Points)
For batch processing, scheduled tasks, or other programmatic use cases, use the `dispatch()` method. You explicitly provide the context, and the handler is executed with the same API as WebSocket handlers.

**Key insight:** Handler code is identical in both flows. Utilities work the same way whether context comes from a WebSocket message or dispatch parameters.
```

**Step 2: Add side-by-side handler/dispatch example**

```markdown
### Handler vs. Dispatch Example

**WebSocket Handler (Real-Time):**
```python
async def agent_handler(incoming, send, memory, analytics):
    # Context automatically available from incoming
    user_name = incoming.context.user_name
    thread_id = incoming.context.thread_id

    history = await memory.get_history(limit=20)
    yield send.text(f"Hello {user_name}")

    # Analytics automatically tagged with thread_id, workspace_id, organization_id
    analytics.track("greeting_sent", data={"user": user_name})
```

**Dispatch (Programmatic):**
```python
# You provide context explicitly - utilities work the same way
await worker_server.dispatch(
    handler_id="agent_handler",
    organization_id="org_123",
    workspace_id="ws_456",
    thread_id="scheduled_thread",
    user_name="John Doe",
    message="Process this chat"
)
# Handler code above runs identically
```

**Standalone Analytics:**
```python
# Option 1: Baked-in context
analytics = worker_server.analytics(
    worker_id="bot_1",
    thread_id="t_123"
)
analytics.track("custom_event", data={})

# Option 2: Flexible context
analytics = worker_server.analytics(worker_id="bot_1")
analytics.track("custom_event", data={}, thread_id="t_123")
```
```

**Step 3: Promote standalone utilities section**

Find the utilities documentation and ensure all four are equally documented:
- Memory
- Analytics
- Settings
- Repository

Add `repository()` documentation with examples if not present:

```markdown
### Repository

Direct database access for advanced use cases.

**In handlers:**
```python
async def handler(incoming, send, memory, analytics, settings, repository):
    thread = await repository.get_thread(incoming.context.thread_id)
    events = await repository.get_thread_events(incoming.context.thread_id, limit=50)
```

**Standalone:**
```python
repository = worker_server.repository(worker_id="bot_1")
thread = await repository.get_thread("thread_id")
events = await repository.get_thread_events("thread_id", limit=50)
```
```

**Step 4: Update API documentation**

Add docstrings section:

```markdown
## API Reference

### WorkerIncoming
Represents an incoming message with its complete context.

```python
@dataclass
class WorkerIncoming:
    message: str           # Extracted text from content parts
    content: list[ContentPart]  # Full content parts
    context: WorkerContext
```

### WorkerContext
Contextual metadata for the incoming message ("where and who").

```python
@dataclass
class WorkerContext:
    thread_id: str              # Message thread
    event_id: str               # Message event
    organization_id: str        # Organization scope
    workspace_id: str           # Workspace scope
    user_id: str                # Message user
    user_name: str              # User display name
    user_email: str             # User email
    user_role: str              # User role
    thread_title: str | None    # Optional thread title
    thread_created_at: datetime | None  # Optional creation time
```

### dispatch()
Execute a handler with explicit context.

```python
stream = await worker_server.dispatch(
    handler_id="handler_name",
    organization_id="org_123",      # Required
    workspace_id="ws_456",          # Required
    thread_id="thread_789",         # Optional
    message="Message text",
    user_id="user_101",             # Optional
    user_name="John Doe",           # Optional
    user_email="john@example.com",  # Optional
    user_role="admin",              # Optional
)
```
```

**Step 5: Run tests to verify nothing broke**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test
```

Expected: All tests pass

**Step 6: Commit**

```bash
git add README.md
git commit -m "docs: update README with execution flows and context examples"
```

---

## Task 10: Add Deprecation Warnings and Alias

**Files:**
- Modify: `src/dooers/handlers/__init__.py` (add WorkerOn alias export)
- Modify: `src/dooers/handlers/on.py` (ensure deprecation warnings work)

**Step 1: Create comprehensive deprecation test**

Create `tests/test_deprecation.py`:

```python
import pytest
import warnings
from dooers.handlers.on import WorkerOn


def test_worker_on_deprecation_warning():
    """Test that using WorkerOn triggers deprecation warning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        # Create WorkerOn instance
        on = WorkerOn(
            message="test",
            content=[],
            thread_id="t_123",
            event_id="e_456",
            organization_id="org_789",
            workspace_id="ws_000",
            user_id="u_111",
            user_name="Test",
            user_email="test@example.com",
            user_role="user",
        )

        # Check deprecation warning was issued
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "WorkerIncoming" in str(w[0].message)
```

**Step 2: Run deprecation test**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test tests/test_deprecation.py -v
```

Expected: Test passes, deprecation warning is properly caught

**Step 3: Export WorkerOn with deprecation note in __init__.py**

Ensure `src/dooers/handlers/__init__.py` exports both:

```python
from dooers.handlers.context import WorkerContext
from dooers.handlers.incoming import WorkerIncoming
from dooers.handlers.on import WorkerOn  # Deprecated, use WorkerIncoming

__all__ = [
    "WorkerContext",
    "WorkerIncoming",
    "WorkerOn",  # Deprecated
    # ... other exports
]
```

**Step 4: Add migration note to top-level __init__.py**

In `src/dooers/__init__.py`, add a note:

```python
# WorkerOn is deprecated, use WorkerIncoming instead
# Migration: on.thread_id -> incoming.context.thread_id
```

**Step 5: Run full test suite**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test
```

Expected: All tests pass, deprecation warnings appear for WorkerOn usage

**Step 6: Commit**

```bash
git add src/dooers/handlers/__init__.py src/dooers/handlers/on.py src/dooers/__init__.py tests/test_deprecation.py
git commit -m "docs: add deprecation warnings and migration notes for WorkerOn"
```

---

## Task 11: Create Migration Guide

**Files:**
- Create: `docs/MIGRATION_v0.7.0.md`

**Step 1: Create migration guide**

```markdown
# Migration Guide: v0.7.0 - WorkerIncoming Refactor

## Overview

Version 0.7.0 introduces a clearer API with better naming and organization:
- `WorkerOn` → `WorkerIncoming` (incoming request/context)
- Context metadata now grouped under `incoming.context`
- Dispatch API supports explicit context parameters
- All utilities (memory, analytics, settings, repository) are first-class citizens

## What's Changed

### 1. Rename on → incoming

**Before (v0.6.x):**
```python
async def my_handler(on, send, memory, analytics, settings):
    thread_id = on.thread_id
    user_name = on.user_name
```

**After (v0.7.0):**
```python
async def my_handler(incoming, send, memory, analytics, settings):
    thread_id = incoming.context.thread_id
    user_name = incoming.context.user_name
```

### 2. Access context via incoming.context

**Before (v0.6.x):**
```python
on.thread_id
on.organization_id
on.user_id
on.thread_title
```

**After (v0.7.0):**
```python
incoming.context.thread_id
incoming.context.organization_id
incoming.context.user_id
incoming.context.thread_title
```

### 3. Dispatch with explicit context

**Before (v0.6.x):**
```python
stream = await worker_server.dispatch(
    handler_id="my_handler",
    thread_id="t_123",
    message="test"
)
```

**After (v0.7.0):**
```python
stream = await worker_server.dispatch(
    handler_id="my_handler",
    organization_id="org_123",  # Required
    workspace_id="ws_456",      # Required
    thread_id="t_123",
    message="test"
)
```

### 4. Utilities work identically in all contexts

All utilities (memory, analytics, settings, repository) have consistent APIs:

**Handler context:** Context is implicit
```python
async def handler(incoming, send, memory, analytics):
    history = await memory.get_history()  # Uses incoming.context.thread_id
    analytics.track("event", data={})     # Uses incoming.context for tagging
```

**Dispatch context:** Context is implicit (provided to dispatch)
**Standalone context:** Context explicit at init or per-call

## Migration Checklist

- [ ] Update all handler definitions: `on` → `incoming`
- [ ] Update all context access: `on.field` → `incoming.context.field`
- [ ] Update dispatch calls to include `organization_id` and `workspace_id`
- [ ] Remove/update any imports of `WorkerOn` (deprecated alias available)
- [ ] Test all handlers with new signature
- [ ] Update any utility initialization if standalone

## Backwards Compatibility

`WorkerOn` is still available but deprecated. It will emit deprecation warnings and will be removed in v1.0.0.

For testing purposes, you can temporarily suppress warnings:
```python
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="dooers")
```

## Questions?

See the updated README.md for detailed examples and API reference.
```

**Step 2: Commit migration guide**

```bash
git add docs/MIGRATION_v0.7.0.md
git commit -m "docs: add migration guide for v0.7.0 WorkerIncoming refactor"
```

---

## Task 12: Final Testing & Verification

**Files:**
- Run full test suite

**Step 1: Run full test suite with coverage**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe test:cov
```

Expected: All tests pass, coverage maintained or improved

**Step 2: Run code quality checks**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe check
poe format --check
```

Expected: No linting errors, code properly formatted

**Step 3: Build and validate**

```bash
cd /home/frndvrgs/software/dooers/dooers-workers
poe build
```

Expected: Build succeeds without errors

**Step 4: Final commit message**

Create a final summary commit:

```bash
git log --oneline -12
```

**Step 5: Create git tag for release**

```bash
git tag -a v0.7.0 -m "WorkerIncoming refactor: incoming/send naming, context grouping, unified dispatch API"
```

**Step 6: Final verification**

```bash
git log --oneline -5
git tag
```

---

## Summary

This implementation plan refactors the entire handler API in 12 bite-sized tasks:

1. ✅ Create WorkerContext class
2. ✅ Create WorkerIncoming class (deprecate WorkerOn)
3. ✅ Update Handler type signature
4. ✅ Update HandlerPipeline to create WorkerIncoming
5. ✅ Update router to use WorkerIncoming
6. ✅ Update dispatch() signature for context parameters
7. ✅ Update utilities to use incoming.context
8. ✅ Update examples and tests
9. ✅ Update README documentation
10. ✅ Add deprecation warnings
11. ✅ Create migration guide
12. ✅ Final testing and verification

**Result:** Clean, consistent API with clear mental models for both handler and dispatch flows.
