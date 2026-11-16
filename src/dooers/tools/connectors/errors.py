"""Connector errors. ``status`` uses the ``/v1/tools/execute`` vocabulary; ``message`` is a short sentence."""

from __future__ import annotations


class ConnectorError(Exception):
    def __init__(self, message: str, status: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ConnectionRequired(ConnectorError):
    def __init__(self, message: str, status: str = "connection_required", *, connect_url: str | None = None) -> None:
        super().__init__(message, status)
        self.connect_url = connect_url


class ApprovalDeclined(ConnectorError):
    def __init__(self, message: str, status: str = "declined") -> None:
        super().__init__(message, status)


class InteractionTimeout(ConnectorError):
    def __init__(self, message: str, status: str = "interaction_timeout") -> None:
        super().__init__(message, status)


class PermissionDenied(ConnectorError):
    def __init__(self, message: str, status: str = "denied") -> None:
        super().__init__(message, status)


class InvalidArguments(ConnectorError):
    def __init__(self, message: str, status: str = "invalid_arguments") -> None:
        super().__init__(message, status)
