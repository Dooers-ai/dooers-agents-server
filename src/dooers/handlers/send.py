import uuid
from dataclasses import dataclass
from typing import Literal


@dataclass
class WorkerEvent:
    send_type: str
    data: dict


class WorkerSend:
    def text(self, text: str, author: str | None = None) -> WorkerEvent:
        return WorkerEvent(
            send_type="text",
            data={"text": text, "author": author},
        )

    def image(
        self,
        url: str,
        mime_type: str | None = None,
        alt: str | None = None,
        author: str | None = None,
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="image",
            data={"url": url, "mime_type": mime_type, "alt": alt, "author": author},
        )

    def document(
        self,
        url: str,
        filename: str,
        mime_type: str,
        author: str | None = None,
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="document",
            data={"url": url, "filename": filename, "mime_type": mime_type, "author": author},
        )

    def tool_call(
        self,
        name: str,
        args: dict,
        display_name: str | None = None,
        id: str | None = None,
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="tool_call",
            data={
                "id": id or str(uuid.uuid4()),
                "name": name,
                "display_name": display_name,
                "args": args,
            },
        )

    def tool_result(
        self,
        name: str,
        result: dict,
        args: dict | None = None,
        display_name: str | None = None,
        id: str | None = None,
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="tool_result",
            data={
                "id": id or str(uuid.uuid4()),
                "name": name,
                "display_name": display_name,
                "args": args,
                "result": result,
            },
        )

    def tool_transaction(
        self,
        name: str,
        args: dict,
        result: dict,
        display_name: str | None = None,
        id: str | None = None,
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="tool_transaction",
            data={
                "id": id or str(uuid.uuid4()),
                "name": name,
                "display_name": display_name,
                "args": args,
                "result": result,
            },
        )

    def run_start(self, agent_id: str | None = None) -> WorkerEvent:
        return WorkerEvent(
            send_type="run_start",
            data={"agent_id": agent_id},
        )

    def run_end(
        self,
        status: Literal["succeeded", "failed"] = "succeeded",
        error: str | None = None,
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="run_end",
            data={"status": status, "error": error},
        )

    def update_thread(self, *, title: str | None = None) -> WorkerEvent:
        return WorkerEvent(
            send_type="thread_update",
            data={"title": title},
        )
