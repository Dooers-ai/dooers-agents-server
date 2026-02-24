"""Multiple entry points: WebSocket for real-time chat, REST for dispatch.

- WebSocket /ws         — real-time chat via handler
- POST /api/summarize   — dispatch a summarization handler on an existing thread
- POST /api/notify      — inject a system notification into a thread via dispatch
"""

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from dooers import WorkerConfig, WorkerServer

app = FastAPI()
worker_server = WorkerServer(
    WorkerConfig(
        database_type="sqlite",
        database_name="worker.db",
        assistant_name="Multi Bot",
    )
)

WORKER_ID = "multi-worker"
ORG_ID = "org_demo"
WS_ID = "ws_demo"


# --- Handlers (identical API for WebSocket and dispatch) ---


async def chat_handler(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="chat")
    yield send.text(f"You said: {incoming.message}")
    yield send.update_thread(title=incoming.message[:60])
    yield send.run_end()


async def summarize_handler(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="summarizer")

    history = await memory.get_history(limit=50)
    message_count = len(history)

    summary = f"Thread summary: {message_count} messages. Latest: {incoming.message[:80]}"

    yield send.text(summary)
    yield send.update_thread(title="Summary")
    yield send.run_end()


async def notify_handler(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="notifier")
    yield send.text(incoming.message, author="System")
    yield send.run_end()


# --- WebSocket entry point (real-time) ---


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, chat_handler)


# --- REST entry points (dispatch) ---


@app.post("/api/summarize")
async def summarize(request: Request):
    body = await request.json()
    thread_id = body.get("thread_id")
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)

    stream = await worker_server.dispatch(
        handler=summarize_handler,
        worker_id=WORKER_ID,
        organization_id=ORG_ID,
        workspace_id=WS_ID,
        message="Summarize this conversation",
        user_id="api",
        thread_id=thread_id,
    )

    texts = []
    async for event in stream:
        if event.send_type == "text":
            texts.append(event.data["text"])

    return {"thread_id": stream.thread_id, "summary": " ".join(texts)}


@app.post("/api/notify")
async def notify(request: Request):
    body = await request.json()
    thread_id = body.get("thread_id")
    message = body.get("message", "")
    if not thread_id or not message:
        return JSONResponse({"error": "thread_id and message required"}, status_code=400)

    stream = await worker_server.dispatch(
        handler=notify_handler,
        worker_id=WORKER_ID,
        organization_id=ORG_ID,
        workspace_id=WS_ID,
        message=message,
        user_id="system",
        thread_id=thread_id,
    )

    await stream.collect()

    return {"status": "ok", "thread_id": stream.thread_id}


@app.on_event("startup")
async def startup():
    await worker_server.ensure_initialized()


@app.on_event("shutdown")
async def shutdown():
    await worker_server.close()
