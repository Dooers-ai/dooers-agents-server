import pytest

from dooers.agents.server.thread_queue import (
    QueueValidationError,
    UNQUEUED_LIST_FILTER,
    default_thread_queue,
    is_unqueued_list_filter,
    queue_for_create,
    resolve_thread_queue,
)


def test_default_queue_follows_workspace():
    assert default_thread_queue("ws-1") == "agent"
    assert default_thread_queue("") is None
    assert default_thread_queue(None) is None


def test_resolve_rejects_personal_queue():
    with pytest.raises(ValueError, match="personal"):
        resolve_thread_queue("human", workspace_id="")


def test_resolve_requires_queue_on_workspace():
    with pytest.raises(ValueError, match="require"):
        resolve_thread_queue(None, workspace_id="ws-1")


def test_resolve_validates_catalog():
    assert resolve_thread_queue("human", workspace_id="ws-1", allowed=["agent", "human"]) == "human"
    with pytest.raises(ValueError, match="unknown"):
        resolve_thread_queue("n1", workspace_id="ws-1", allowed=["agent", "human"])


def test_resolve_allows_any_slug_without_catalog():
    assert resolve_thread_queue("n1", workspace_id="ws-1", allowed=[]) == "n1"
    assert resolve_thread_queue("n1", workspace_id="ws-1", allowed=None) == "n1"


def test_queue_for_create_defaults_workspace_to_agent():
    assert queue_for_create(None, workspace_id="ws-1") == "agent"
    assert queue_for_create(None, workspace_id="") is None
    assert queue_for_create("human", workspace_id="ws-1") == "human"


def test_queue_for_create_rejects_unknown_catalog_slug():
    with pytest.raises(QueueValidationError, match="unknown"):
        queue_for_create("n1", workspace_id="ws-1", allowed=["agent", "human"])


def test_unqueued_list_filter_sentinel():
    assert is_unqueued_list_filter(UNQUEUED_LIST_FILTER)
    assert not is_unqueued_list_filter("agent")
    assert not is_unqueued_list_filter(None)
