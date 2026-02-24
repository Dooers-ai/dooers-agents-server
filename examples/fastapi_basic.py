from fastapi import FastAPI, WebSocket

from dooers import WorkerConfig, WorkerServer

app = FastAPI()
worker_server = WorkerServer(
    WorkerConfig(
        database_type="sqlite",
        database_name="worker.db",
        assistant_name="Echo Bot",
    )
)


async def echo_agent(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="echo")
    yield send.text(f"You said: {incoming.message}")
    yield send.update_thread(title=incoming.message[:60])
    yield send.run_end()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, echo_agent)


@app.on_event("shutdown")
async def shutdown():
    await worker_server.close()
