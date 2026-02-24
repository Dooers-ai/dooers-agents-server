import os

from fastapi import FastAPI, WebSocket
from google import genai
from google.genai import types

from dooers import WorkerConfig, WorkerServer

app = FastAPI()
worker_server = WorkerServer(
    WorkerConfig(
        database_type="sqlite",
        database_name="worker.db",
    )
)
client = genai.Client(
    vertexai=True,
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
)


async def vertex_agent(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="vertex-gemini")

    # get_history() returns formatted dicts — use google format for Vertex
    history = await memory.get_history(limit=20, format="google")

    contents = []
    for msg in history:
        contents.append(types.Content(role=msg["role"], parts=[types.Part.from_text(msg["parts"][0]["text"])]))

    contents.append(types.Content(role="user", parts=[types.Part.from_text(incoming.message)]))

    result = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction="You are a helpful assistant.",
        ),
    )

    yield send.text(result.text)
    yield send.update_thread(title=incoming.message[:60])
    yield send.run_end()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, vertex_agent)


@app.on_event("shutdown")
async def shutdown():
    await worker_server.close()
