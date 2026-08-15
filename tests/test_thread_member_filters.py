import asyncio

from dooers.agents.server.persistence.cosmos import CosmosPersistence
from dooers.agents.server.persistence.postgres import PostgresPersistence
from dooers.agents.server.repository import Repository


class _PersistenceSpy:
    def __init__(self):
        self.kwargs = None

    async def list_threads(self, **kwargs):
        self.kwargs = kwargs
        return []


def test_repository_forwards_all_member_identity_filters():
    persistence = _PersistenceSpy()

    asyncio.run(
        Repository(persistence).list_threads(
            filter={
                "agent_id": "agent-1",
                "organization_id": "org-1",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
                "user_email": "user@example.com",
                "identity_ids": ["aad-object-id"],
            }
        )
    )

    assert persistence.kwargs["user_id"] == "user-1"
    assert persistence.kwargs["user_email"] == "user@example.com"
    assert persistence.kwargs["identity_ids"] == ["aad-object-id"]


def test_cosmos_member_scope_combines_all_identity_types():
    persistence = CosmosPersistence.__new__(CosmosPersistence)
    conditions = []
    params = []

    persistence._build_scope_conditions(
        "member",
        "org-1",
        "workspace-1",
        "user-1",
        "user@example.com",
        ["aad-object-id"],
        conditions,
        params,
    )

    member_condition = conditions[-1]
    assert "u.user_id = @user_id" in member_condition
    assert "u.user_email = @user_email" in member_condition
    assert "@identity_ids" in member_condition
    assert {param["name"] for param in params} >= {"@user_id", "@user_email", "@identity_ids"}


def test_postgres_member_scope_combines_all_identity_types():
    persistence = PostgresPersistence.__new__(PostgresPersistence)
    conditions = []
    params = []

    persistence._build_scope_conditions(
        "member",
        "org-1",
        "workspace-1",
        "user-1",
        "user@example.com",
        ["aad-object-id"],
        conditions,
        params,
        1,
    )

    member_condition = conditions[-1]
    assert " OR " in member_condition
    assert any('{"user_id": "user-1"}' in value for value in params)
    assert any('{"user_email": "user@example.com"}' in value for value in params)
