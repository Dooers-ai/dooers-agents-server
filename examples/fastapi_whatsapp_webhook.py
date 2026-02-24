"""WhatsApp webhook using dispatch().

External webhook flow:
- Phone → customer lookup → resolve org/workspace context
- Find or create thread per phone/worker
- Dispatch handler with explicit context
- Stream events back, forward text replies to WhatsApp
"""

import logging

from fastapi import FastAPI, Request, WebSocket

from dooers import WorkerConfig, WorkerServer

logger = logging.getLogger(__name__)

app = FastAPI()
worker_server = WorkerServer(
    WorkerConfig(
        database_type="sqlite",
        database_name="worker.db",
        assistant_name="WhatsApp Bot",
    )
)

# --- Simulated external services ---

CUSTOMER_DB: dict[str, dict] = {
    "+5511999990001": {
        "organization_id": "org_acme",
        "workspace_id": "ws_support",
        "name": "Alice",
    },
    "+5511999990002": {
        "organization_id": "org_acme",
        "workspace_id": "ws_support",
        "name": "Bob",
    },
}


def get_customer(phone: str) -> dict | None:
    return CUSTOMER_DB.get(phone)


async def send_whatsapp_message(phone: str, text: str) -> None:
    logger.info("[whatsapp] -> %s: %s", phone, text[:80])


# --- Handler (same API for both WebSocket and dispatch) ---


async def whatsapp_handler(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="whatsapp-echo")
    yield send.text(f"Echo: {incoming.message}")
    yield send.update_thread(title=incoming.message[:60])
    yield send.run_end()


# --- Webhook endpoint (dispatch) ---


@app.post("/webhook/{worker_id}")
async def webhook(worker_id: str, request: Request):
    body = await request.json()
    phone = body.get("phone", "")
    text = body.get("text", "")

    if not phone or not text:
        return {"status": "ignored"}

    customer = get_customer(phone)
    if not customer:
        return {"status": "unknown_customer"}

    organization_id = customer["organization_id"]
    workspace_id = customer["workspace_id"]
    customer_name = customer.get("name", phone)

    # Find existing thread for this phone/worker
    repo = await worker_server.repository()
    threads = await repo.list_threads(
        filter={
            "worker_id": worker_id,
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "user_id": phone,
        },
        limit=1,
    )
    thread_id = threads[0].id if threads else None

    # Dispatch handler with explicit context
    stream = await worker_server.dispatch(
        handler=whatsapp_handler,
        worker_id=worker_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        message=text,
        user_id=phone,
        user_name=customer_name,
        thread_id=thread_id,
        thread_title=f"WhatsApp: {customer_name}" if not thread_id else None,
    )

    # Stream events, forward text replies to WhatsApp
    async for event in stream:
        if event.send_type == "text":
            await send_whatsapp_message(phone, event.data["text"])

    return {"status": "ok", "thread_id": stream.thread_id, "is_new": stream.is_new_thread}


# --- WebSocket for dashboard monitoring ---


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, whatsapp_handler)


@app.on_event("startup")
async def startup():
    await worker_server.ensure_initialized()


@app.on_event("shutdown")
async def shutdown():
    await worker_server.close()
