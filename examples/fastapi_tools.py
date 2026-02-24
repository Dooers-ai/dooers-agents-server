import uuid

import httpx
from fastapi import FastAPI, WebSocket

from dooers import WorkerConfig, WorkerServer

app = FastAPI()
worker_server = WorkerServer(
    WorkerConfig(
        database_type="sqlite",
        database_name="worker.db",
        assistant_name="Search Agent",
    )
)


async def search_web(query: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json"},
        )
        return resp.json()


async def tool_agent(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="tool-agent")

    if incoming.message.lower().startswith("search "):
        query = incoming.message[7:]
        args = {"query": query}
        call_id = str(uuid.uuid4())

        yield send.tool_call(
            "web_search",
            args,
            display_name="Searching the web...",
            id=call_id,
        )

        results = await search_web(query)

        yield send.tool_result(
            "web_search",
            {"results": results},
            args=args,
            id=call_id,
        )

        if results.get("Abstract"):
            yield send.text(results["Abstract"])
        else:
            yield send.text(f"No results found for: {query}")
    else:
        yield send.text("Try: search <query>")

    yield send.update_thread(title=incoming.message[:60])
    yield send.run_end()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, tool_agent)


@app.on_event("shutdown")
async def shutdown():
    await worker_server.close()
