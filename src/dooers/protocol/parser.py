import json
from typing import TypeVar

from pydantic import TypeAdapter

from dooers.protocol.frames import ClientToServer, ServerToClient

T = TypeVar("T")

_client_adapter = TypeAdapter(ClientToServer)


def parse_frame(data: str) -> ClientToServer:
    raw = json.loads(data)
    return _client_adapter.validate_python(raw)


def serialize_frame(frame: ServerToClient) -> str:
    return frame.model_dump_json(exclude_none=True)
