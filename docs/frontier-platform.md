# 2026-10-31 — Frontier agents on the SDK (0.31.0)

Part of the platform plan in `Dooers-ai/dooers-agent-template` → `docs/PLANO-FRONTIER.md`.

## Problem

General-purpose agents (Claude.ai / ChatGPT class) need: live token streaming, a Stop button,
memory that outlives a conversation, scheduled work, durable generated files and a safe place
to run code. The SDK had none of these, processed one frame at a time per socket (a running turn
blocked pings and everything else), and broadcast every thread event to every socket of the
agent — a privacy leak once one agent serves a whole organization.

## Design

| Concern | Decision |
| --- | --- |
| Streaming | Handler yields `text_delta`; pipeline announces a draft (`event.append`, `streaming`) then `event.delta` (coalesced), persists only the final event. Old clients still get the final text. |
| Stop | `RunRegistry` per process. Cancel = flag + `Task.cancel()` only while the pipeline awaits the handler, so cancellation never lands inside pipeline persistence. `uncancel()` after a requested stop. |
| Socket concurrency | One worker task per connection runs queued turns in order; the receive loop keeps serving frames. In-flight turn survives disconnect, queued ones are dropped. |
| Isolation | `PipelineBroadcaster`: thread frames go to subscribers + participants (`write` access). Supervisors must subscribe (with reason) to watch live. |
| Memory | SQL tables in the agent DB; scopes `user` (agent, org, workspace, user — private) and `workspace` (shared with the members; editable by managers and its author); author recorded on every memory; sharing a personal memory is an explicit action (`share()`); `AgentContext.is_shared` to keep personal memory out of multi-person threads; full-text + pinning/usage/recency ranking; preferences per (agent, org, user). |
| Scheduled tasks | On the Dooers Tasks service, fired by Cloud Tasks (a poller inside a scale-to-zero agent misses them). Explicit recurrence model (no cron from LLMs) on the local wall clock. The core re-checks the scheduler's access at fire time and calls `run_task` with a `purpose: task` token for that person. |
| Artifacts | `store_artifact` reuses chat storage keys so hydration re-signs URLs by `ref_id`. |
| Sandbox | `dooers.tools.sandbox` mirrors `dooers.tools.rag` (service token, runtime context). |

## Next

- Optional embeddings for memory once the gateway exposes `/v1/embeddings`.
- Push/e-mail notifications when a task runs or fails.
- Cross-replica `run.cancel` (pub/sub) when agents run with more than one instance.
