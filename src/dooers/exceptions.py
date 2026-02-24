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
