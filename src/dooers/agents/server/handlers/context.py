from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dooers.agents.server.dooers_threads.context import DooersThreadsExecutionContext

import copy
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any

from dooers.agents.server.protocol.models import (
    ChatContext,
    User,
    WorkspaceQueueCatalog,
    WorkspaceQueueMember,
)


@dataclass
class AgentContext:
    """Contextual metadata for the incoming message."""

    thread_id: str
    agent_id: str
    event_id: str
    organization_id: str = ""
    workspace_id: str = ""
    channel: str = "dooers-platform"
    channel_meta: dict | None = None
    user: User = field(default_factory=lambda: User(user_id=""))
    thread_title: str | None = field(default=None)
    thread_created_at: datetime | None = field(default=None)
    queue: str | None = None
    #: Workspace queue catalog from the connect handshake (snapshot).
    queues: list[WorkspaceQueueCatalog] = field(default_factory=list)
    chat_context: ChatContext | None = None
    #: Why this turn runs when no person typed it, e.g. ``{"type": "task", "task_id": ..., "run_id": ...}``.
    trigger: dict[str, Any] | None = None
    #: People taking part in the thread (from the thread row; the agent itself excluded).
    participants: list[User] = field(default_factory=list)
    #: Set by the pipeline; ``cancelled`` reads it. Not part of the public constructor contract.
    _active_run: Any = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        """True once someone pressed Stop (``run.cancel``). Long loops should check it."""
        return bool(getattr(self._active_run, "cancel_requested", False))

    @property
    def is_shared(self) -> bool:
        """True when someone besides the current user takes part in the thread.

        Personal memory must stay out of such threads: whatever the agent recalls about one person
        can end up in an answer the others read.
        """
        me = (self.user.user_id or "").strip()
        return any((p.user_id or "").strip() not in ("", me) for p in self.participants)

    @property
    def timezone(self) -> str | None:
        """IANA time zone reported by the client for this turn (unvalidated hint)."""
        return self.chat_context.timezone if self.chat_context else None

    # Not a dataclass field: in legacy mode AgentContext keeps production's fields, __init__,
    # repr, asdict, equality, copy and pickle. Dooers Threads mode moves the instance to a subclass
    # with the same fields that carries the metadata (``_dooers_threads_context_class``).
    @property
    def dooers_threads(self) -> "DooersThreadsExecutionContext | None":
        """Dooers Threads execution metadata; ``None`` in legacy mode."""
        return getattr(type(self), "_dooers_threads_metadata", None)

    @dooers_threads.setter
    def dooers_threads(self, value: "DooersThreadsExecutionContext | None") -> None:
        base = _production_class(type(self))
        self.__class__ = base if value is None else _dooers_threads_context_class(base, value)

    def members_for_queue(self, slug: str | None = None) -> list[WorkspaceQueueMember]:
        """Members of ``slug``, or of the current thread queue when omitted."""
        target = self.queue if slug is None else slug
        if not target:
            return []
        for queue in self.queues:
            if queue.slug == target:
                return list(queue.members)
        return []


def _production_class(cls: type) -> type:
    """The class before Dooers Threads metadata was attached (``AgentContext`` or a subclass of it)."""
    while "_dooers_threads_metadata" in vars(cls):
        cls = cls.__base__
    return cls


def _production_instance(cls: type, state: dict) -> "AgentContext":
    """Unpickle a Dooers Threads context as the production class, with the same field values."""
    instance = cls.__new__(cls)
    instance.__dict__.update(state)
    return instance


def _dooers_threads_context_class(base: type, metadata: "DooersThreadsExecutionContext") -> type:
    """A subclass of ``base`` for one execution, with the metadata on the class, not on the instance.

    The instance keeps only the production fields (``vars``, ``asdict``, ``repr`` and the
    name ``AgentContext`` stay). ``dataclasses.replace``, ``copy`` and ``deepcopy`` build the
    same subclass, so they keep the metadata (shared, not copied: it holds the live
    cancellation signal). Equality compares the fields, as production does. Pickle stores
    the production object without the metadata, which only exists during the execution.
    """

    def __copy__(self):
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        return clone

    def __deepcopy__(self, memo):
        clone = object.__new__(type(self))
        memo[id(self)] = clone
        clone.__dict__.update(copy.deepcopy(self.__dict__, memo))
        return clone

    def __reduce_ex__(self, protocol):
        return _production_instance, (base, dict(self.__dict__))

    def __eq__(self, other):
        if not isinstance(other, AgentContext) or _production_class(type(other)) is not base:
            return NotImplemented
        names = [f.name for f in fields(base)]
        return tuple(getattr(self, name) for name in names) == tuple(getattr(other, name) for name in names)

    return type(
        base.__name__,
        (base,),
        {
            "__module__": base.__module__,
            "__qualname__": base.__qualname__,
            "__doc__": base.__doc__,
            "_dooers_threads_metadata": metadata,
            "__copy__": __copy__,
            "__deepcopy__": __deepcopy__,
            "__reduce_ex__": __reduce_ex__,
            "__eq__": __eq__,
            "__hash__": None,
        },
    )
