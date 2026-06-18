"""WhatsApp-style webhook using ``dispatch()`` with tools-shaped ``User`` and ``thread_id``.

- Bridge/tools POST ``message`` + ``content`` + tenant fields; you derive
  ``thread_id = whatsapp_thread_id(phone)`` and ``User(user_id=e164, …)`` as in
  :mod:`dooers.features.channels.whatsapp`.
- For assistant replies to the phone, use ``send.whatsapp.*``; for UI-only, use
  ``send.text`` and friends.
- Configure a real :class:`dooers.AgentConfig` with ``database_type=postgres`` (or
  ``cosmos``) for production; set ``AGENT_DATABASE_*`` env vars locally.
"""

import logging
import os

from fastapi import FastAPI, Request, WebSocket

from dooers.agents.server import AgentConfig, AgentServer, User, normalize_e164, whatsapp_thread_id

logger = logging.getLogger(__name__)

app = FastAPI()
agent_server = AgentServer(
    AgentConfig(
        database_type="postgres",
        database_host=os.environ.get("AGENT_DATABASE_HOST", "localhost"),
        database_port=int(os.environ.get("AGENT_DATABASE_PORT", "5432")),
        database_user=os.environ.get("AGENT_DATABASE_USER", "postgres"),
        database_name=os.environ.get("AGENT_DATABASE_NAME", "dooers_agent"),
        database_password=os.environ.get("AGENT_DATABASE_PASSWORD", ""),
        assistant_name="WhatsApp Bot",
    )
)

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


async def whatsapp_handler(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="whatsapp-echo")
    instance_id = os.environ.get("DEMO_WA_INSTANCE_ID", "demo-instance")
    to_phone = incoming.context.user.user_id
    try:
        e164 = normalize_e164(to_phone)
    except ValueError:
        e164 = to_phone
    # Set ``AgentConfig(dooers_whatsapp_service=True)`` and ``DOOERS_WHATSAPP_SERVICE_SECRET`` (optional ``DOOERS_WHATSAPP_TOOLS_BASE``). Here: ``send.whatsapp`` vs ``send.text``.
    yield send.whatsapp.text(
        f"Echo (WA): {incoming.message}",
        to_e164=e164,
        instance_id=instance_id,
    )
    yield send.text(f"Echo (UI only): {incoming.message}")
    yield send.update_thread(title=(incoming.message or "")[:60])
    yield send.run_end()


@app.post("/webhook/{agent_id}")
async def webhook(agent_id: str, request: Request):
    body = await request.json()
    phone = (body.get("user_id") or body.get("phone") or "").strip()
    if not phone:
        return {"status": "ignored", "reason": "missing user_id"}
    try:
        e164 = normalize_e164(phone)
    except ValueError:
        return {"status": "ignored", "reason": "invalid_e164"}
    try:
        text = (body.get("message") or body.get("text") or "").strip()
    except Exception:
        text = ""
    if not text and not body.get("content"):
        return {"status": "ignored", "reason": "no message or content"}
    content = body.get("content")
    if content is not None and not isinstance(content, list):
        return {"status": "ignored", "reason": "invalid content"}

    customer = CUSTOMER_DB.get(e164)
    if not customer:
        return {"status": "unknown_customer"}

    organization_id = body.get("organization_id") or customer["organization_id"]
    workspace_id = body.get("workspace_id") or customer["workspace_id"]
    display_name = body.get("user_name") or customer.get("name") or e164
    user = User(user_id=e164, user_name=display_name)
    thread_id = whatsapp_thread_id(e164)

    stream = await agent_server.dispatch(
        whatsapp_handler,
        agent_id=agent_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        message=text,
        user=user,
        thread_id=thread_id,
        thread_title=f"WhatsApp: {display_name}",
        content=content,
    )

    async for _ in stream:
        pass

    return {
        "status": "ok",
        "thread_id": stream.thread_id,
        "is_new": stream.is_new_thread,
    }


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    await agent_server.handle(websocket, whatsapp_handler)


@app.on_event("startup")
async def startup():
    await agent_server.ensure_initialized()


@app.on_event("shutdown")
async def shutdown():
    await agent_server.close()
