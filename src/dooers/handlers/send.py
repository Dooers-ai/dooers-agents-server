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

    def audio(
        self,
        url: str,
        mime_type: str,
        duration: float | None = None,
        author: str | None = None,
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="audio",
            data={"url": url, "mime_type": mime_type, "duration": duration, "author": author},
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

    def update_user_event(self, event_id: str, content: list[dict]) -> WorkerEvent:
        return WorkerEvent(
            send_type="event_update",
            data={"event_id": event_id, "content": content},
        )

    def update_thread(self, *, title: str | None = None) -> WorkerEvent:
        return WorkerEvent(
            send_type="thread_update",
            data={"title": title},
        )

    # --- Form elements ---

    @staticmethod
    def form_text(
        name: str,
        *,
        label: str = "",
        order: int = 0,
        required: bool = False,
        disabled: bool = False,
        placeholder: str | None = None,
        default: str | None = None,
        input_type: Literal["text", "password", "email", "number"] = "text",
    ) -> dict:
        return {
            "type": "text_input",
            "name": name,
            "label": label,
            "order": order,
            "required": required,
            "disabled": disabled,
            "placeholder": placeholder,
            "default": default,
            "input_type": input_type,
        }

    @staticmethod
    def form_textarea(
        name: str,
        *,
        label: str = "",
        order: int = 0,
        required: bool = False,
        disabled: bool = False,
        placeholder: str | None = None,
        default: str | None = None,
        rows: int | None = None,
    ) -> dict:
        return {
            "type": "textarea_input",
            "name": name,
            "label": label,
            "order": order,
            "required": required,
            "disabled": disabled,
            "placeholder": placeholder,
            "default": default,
            "rows": rows,
        }

    @staticmethod
    def form_select(
        name: str,
        *,
        label: str = "",
        options: list[dict] | None = None,
        order: int = 0,
        required: bool = False,
        disabled: bool = False,
        default: str | None = None,
        placeholder: str | None = None,
    ) -> dict:
        return {
            "type": "select_input",
            "name": name,
            "label": label,
            "options": options or [],
            "order": order,
            "required": required,
            "disabled": disabled,
            "default": default,
            "placeholder": placeholder,
        }

    @staticmethod
    def form_radio(
        name: str,
        *,
        label: str = "",
        options: list[dict] | None = None,
        order: int = 0,
        required: bool = False,
        disabled: bool = False,
        default: str | None = None,
        variant: Literal["native", "button"] = "native",
    ) -> dict:
        return {
            "type": "radio_input",
            "name": name,
            "label": label,
            "options": options or [],
            "order": order,
            "required": required,
            "disabled": disabled,
            "default": default,
            "variant": variant,
        }

    @staticmethod
    def form_checkbox(
        name: str,
        *,
        label: str = "",
        options: list[dict] | None = None,
        order: int = 0,
        required: bool = False,
        disabled: bool = False,
        default: list[str] | None = None,
        variant: Literal["native", "button"] = "native",
    ) -> dict:
        return {
            "type": "checkbox_input",
            "name": name,
            "label": label,
            "options": options or [],
            "order": order,
            "required": required,
            "disabled": disabled,
            "default": default,
            "variant": variant,
        }

    @staticmethod
    def form_file(
        name: str,
        *,
        label: str = "",
        upload_url: str = "",
        order: int = 0,
        required: bool = False,
        disabled: bool = False,
        accept: str | None = None,
        multiple: bool = False,
    ) -> dict:
        return {
            "type": "file_input",
            "name": name,
            "label": label,
            "upload_url": upload_url,
            "order": order,
            "required": required,
            "disabled": disabled,
            "accept": accept,
            "multiple": multiple,
        }

    def form(
        self,
        message: str,
        elements: list[dict],
        submit_label: str = "Send",
        cancel_label: str = "Cancel",
        size: Literal["small", "medium", "large"] = "medium",
    ) -> WorkerEvent:
        return WorkerEvent(
            send_type="form",
            data={
                "message": message,
                "elements": elements,
                "submit_label": submit_label,
                "cancel_label": cancel_label,
                "size": size,
            },
        )
