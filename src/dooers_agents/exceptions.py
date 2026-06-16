class UnsupportedContentTypeError(Exception):
    """Raised when ingest cannot decode a ``ContentPart`` (e.g. video or unknown ``type``).

    The router maps this to a failed ``event.create`` ack **before** the user message exists.
    For chat allowlist mismatches (:class:`~dooers.config.AgentConfig.allowed_content_types`),
    prefer the pipeline assistant policy reply (persisted assistant text) rather than raising this error.
    """


class DispatchError(Exception):
    """Raised during dispatch setup phase.

    Indicates infrastructure failures (database down, invalid arguments)
    that prevent handler execution from starting. No thread is created
    and no events are broadcast.
    """


class HandlerError(Exception):
    """Raised when a handler fails during execution.

    Before this exception reaches the caller, the pipeline has already:
    1. Logged the error
    2. Marked the current run as failed
    3. Created a system error event in the thread
    4. Broadcast both to WebSocket subscribers

    The thread is always in a clean state when this exception is raised.
    """

    def __init__(self, message: str, original: Exception | None = None):
        super().__init__(message)
        self.original = original
