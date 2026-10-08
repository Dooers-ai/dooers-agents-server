from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.protocol.models import ConnectionWorkspace, WorkspaceQueueCatalog


def test_connection_workspace_keeps_slug_list():
    workspace = ConnectionWorkspace(id="ws-1", queues=["human", "n1"])
    assert workspace.queues == ["human", "n1"]
    assert workspace.queue_catalog == []
    assert [queue.slug for queue in workspace.catalog()] == ["human", "n1"]


def test_connection_workspace_extracts_slugs_from_objects():
    workspace = ConnectionWorkspace(
        id="ws-1",
        queues=[{"slug": "human", "name": "Recepção", "members": [{"user_id": "u1"}]}],
    )
    assert workspace.queues == ["human"]


def test_connection_workspace_uses_queue_catalog_roster():
    workspace = ConnectionWorkspace(
        id="ws-1",
        queues=["human"],
        queue_catalog=[
            {
                "slug": "human",
                "name": "Recepção",
                "members": [
                    {
                        "user_id": "u1",
                        "user_name": "Ana",
                        "user_email": "ana@example.com",
                        "identity_ids": ["aad-1"],
                        "claims": {"department": "CX"},
                    }
                ],
            }
        ],
    )
    member = workspace.catalog()[0].members[0]
    assert member.user_id == "u1"
    assert member.user_name == "Ana"
    assert member.claims["department"] == "CX"


def test_members_for_queue_uses_current_thread_slug():
    catalog = [
        WorkspaceQueueCatalog.model_validate(
            {
                "slug": "human",
                "name": "Human",
                "members": [{"user_id": "u1", "user_name": "Ana"}],
            }
        )
    ]
    context = AgentContext(
        thread_id="t1",
        agent_id="a1",
        event_id="e1",
        queue="human",
        queues=catalog,
    )
    assert [member.user_id for member in context.members_for_queue()] == ["u1"]
    assert context.members_for_queue("missing") == []
    context.queue = None
    assert context.members_for_queue() == []
