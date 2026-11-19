"""Actual persisted membership edits must use a current reviewed version."""

from fastapi import FastAPI
from identity_support import actor_client, add_actor
from sqlalchemy import select

from aco.features.identity.models import AuditEvent, Membership


def test_reviewed_member_change_stale_denial_reactivation_and_current_authority() -> None:
    with actor_client("owner") as (client, store, owner, workspace):
        app = client.app
        assert isinstance(app, FastAPI)
        user, member_client = add_actor(store, app, "member@example.test")
        invitation = client.post(
            f"/api/workspaces/{workspace}/invitations", json={"email": user.email, "role": "viewer"}
        )
        assert invitation.status_code == 201
        assert (
            member_client.post(
                "/api/invitations/accept", json={"invite_code": invitation.json()["invite_code"]}
            ).status_code
            == 200
        )
        path = f"/api/workspaces/{workspace}/members"
        members = client.get(path)
        assert members.status_code == 200
        reviewed = next(row for row in members.json() if row["user_id"] == str(user.id))
        assert reviewed["version"] == 1
        command = {"role": "operator", "active": False, "expected_version": reviewed["version"]}
        saved = client.patch(f"{path}/{user.id}", json=command)
        assert saved.status_code == 200
        assert saved.json()["member"]["version"] == 2
        assert saved.json()["audit"]["actor_id"] == str(owner.id)
        assert member_client.get(f"/api/workspaces/{workspace}").status_code == 403
        stale = client.patch(f"{path}/{user.id}", json={**command, "role": "owner"})
        assert stale.status_code == 409 and stale.json()["detail"] == "MEMBERSHIP_VERSION_CHANGED"
        assert client.patch(f"{path}/{user.id}", json={"role": "viewer", "active": True}).status_code == 422
        assert (
            client.patch(
                f"{path}/{user.id}", json={"role": "finance_manager", "active": True, "expected_version": 2}
            ).status_code
            == 200
        )
        assert member_client.get(f"/api/workspaces/{workspace}").json()["role"] == "finance_manager"
        assert member_client.get(path).status_code == 403
        assert (
            member_client.patch(
                f"{path}/{owner.id}", json={"role": "viewer", "active": True, "expected_version": 1}
            ).status_code
            == 403
        )
        store.expire_all()
        member = store.scalar(
            select(Membership).where(Membership.tenant_id == workspace, Membership.user_id == user.id)
        )
        assert member is not None and member.version == 3 and member.active
        audits = store.scalars(select(AuditEvent).where(AuditEvent.action == "membership.changed")).all()
        assert len(audits) == 2
        csrf = client.headers.pop("x-csrf-token")
        assert client.patch(f"{path}/{user.id}", json={**command, "expected_version": 3}).status_code == 403
        client.headers["x-csrf-token"] = csrf
        assert (
            client.patch(
                f"{path}/{owner.id}", json={"role": "viewer", "active": True, "expected_version": 1}
            ).json()["detail"]
            == "LAST_OWNER"
        )
