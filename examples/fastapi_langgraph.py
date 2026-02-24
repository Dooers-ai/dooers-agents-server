import os

from fastapi import FastAPI, WebSocket
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from dooers import WorkerConfig, WorkerServer

app = FastAPI()
worker_server = WorkerServer(
    WorkerConfig(
        database_type="sqlite",
        database_name="worker.db",
    )
)

llm = ChatOpenAI(
    model="gpt-4o-mini",
    api_key=os.getenv("OPENAI_API_KEY"),
)


@tool
def multiply(a: int, b: int) -> int:
    return a * b


@tool
def add(a: int, b: int) -> int:
    return a + b


agent = create_react_agent(llm, tools=[multiply, add])


async def langgraph_agent(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="langgraph")

    # get_history_raw() returns ThreadEvent objects for manual conversion
    events = await memory.get_history_raw(limit=20, order="asc")

    messages = [SystemMessage(content="You are a helpful assistant with math tools.")]
    for event in events:
        if event.type == "message" and event.content:
            text = " ".join(part.text for part in event.content if hasattr(part, "text"))
            if text:
                if event.actor == "user":
                    messages.append(HumanMessage(content=text))
                else:
                    messages.append(AIMessage(content=text))

    messages.append(HumanMessage(content=incoming.message))

    result = await agent.ainvoke({"messages": messages})

    for msg in result["messages"]:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                yield send.tool_call(tc["name"], tc["args"])
        if msg.type == "tool":
            yield send.tool_result(msg.name, {"result": msg.content})

    final = result["messages"][-1]
    if final.content:
        yield send.text(final.content)

    yield send.update_thread(title=incoming.message[:60])
    yield send.run_end()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await worker_server.handle(websocket, langgraph_agent)


@app.on_event("shutdown")
async def shutdown():
    await worker_server.close()
