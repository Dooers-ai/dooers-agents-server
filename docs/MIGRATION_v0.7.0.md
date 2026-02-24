# Migration Guide: v0.7.0 - WorkerIncoming Refactor

## Overview

Version 0.7.0 introduces a clearer API with better naming and organization:
- `WorkerOn` -> `WorkerIncoming` (incoming request/context)
- Context metadata now grouped under `incoming.context`
- Handler parameter renamed from `on` to `incoming`

## What's Changed

### 1. Rename on -> incoming

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
on.message
on.content
```

**After (v0.7.0):**
```python
incoming.context.thread_id
incoming.context.organization_id
incoming.context.user_id
incoming.context.thread_title
incoming.message          # message stays top-level
incoming.content          # content stays top-level
```

### 3. Import changes

**Before (v0.6.x):**
```python
from dooers import WorkerOn
```

**After (v0.7.0):**
```python
from dooers import WorkerIncoming, WorkerContext
```

## Migration Checklist

- [ ] Update all handler definitions: `on` -> `incoming`
- [ ] Update all context access: `on.field` -> `incoming.context.field`
- [ ] Keep `on.message` / `on.content` as `incoming.message` / `incoming.content` (top-level, not under context)
- [ ] Remove any imports of `WorkerOn` (removed in v0.7.0)
- [ ] Test all handlers with new signature
