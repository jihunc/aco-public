"""Stopped-action inspection: real failed attempts, four roles, two current tenants."""

from typing import cast

from accounting_support import context, seed
from action_support import World, action_world, enqueue, run_attempts
from fastapi import FastAPI
from identity_support import add_actor
from sqlalchemy import func, select

from aco.features.accounting.models import AccountingEvent, InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.identity.models import Membership, ProviderConnection, Tenant
from aco.features.policies.models import CollectionPolicyVersion


def test_stopped_action_reads_keep_two_tenants_and_four_current_roles_separate() -> None:
    with action_world() as world:
        other = Tenant(name="Other stopped-action tenant", timezone="UTC")
        world.store.add(other)
        world.store.flush()
        world.store.add(Membership(tenant_id=other.id, user_id=world.ctx.actor_id, role="owner"))
        world.store.add(ProviderConnection(tenant_id=other.id, provider="demo", provider_type="accounting"))
        world.store.commit()
        seed(world.client, other.id)
        invoice = world.store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == other.id))
        policy = world.store.scalar(
            select(CollectionPolicyVersion).where(CollectionPolicyVersion.tenant_id == other.id)
        )
        assert invoice is not None and policy is not None
        event = world.store.scalar(select(AccountingEvent).where(AccountingEvent.invoice_id == invoice.id))
        assert event is not None
        foreign = World(
            world.client,
            world.store,
            context(other.id, world.ctx.actor_id),
            invoice.provider_connection_id,
            invoice.id,
            policy.id,
            event.id,
        )
        own_action = enqueue(world)
        foreign_action = enqueue(foreign)
        run_attempts(world, own_action, "TEMPORARILY_UNAVAILABLE")
        run_attempts(foreign, foreign_action, "AUTH_EXPIRED")
        assert world.action(own_action.action_id).status == "dead_letter"
        assert world.action(foreign_action.action_id).status == "dead_letter"
        totals = {
            model: world.store.scalar(select(func.count()).select_from(model))
            for model in (ScheduledAction, ActionExecution, AccountingEvent)
        }
        for role in ("owner", "finance_manager", "operator", "viewer"):
            actor, client = add_actor(
                world.store, cast(FastAPI, world.client.app), f"{role}@synthetic.invalid"
            )
            current = Membership(tenant_id=world.ctx.tenant_id, user_id=actor.id, role=role)
            membership = Membership(tenant_id=other.id, user_id=actor.id, role=role)
            world.store.add_all([current, membership])
            world.store.commit()
            own_path = f"/api/workspaces/{world.ctx.tenant_id}/scheduled-actions"
            foreign_path = f"/api/workspaces/{other.id}/scheduled-actions"
            for path, key, status, attempts, error, wrong_key in (
                (
                    own_path,
                    own_action.action_id,
                    "dead_letter",
                    3,
                    "TEMPORARILY_UNAVAILABLE",
                    foreign_action.action_id,
                ),
                (
                    foreign_path,
                    foreign_action.action_id,
                    "dead_letter",
                    3,
                    "AUTH_EXPIRED",
                    own_action.action_id,
                ),
            ):
                response = client.get(path + f"?status={status}")
                assert response.status_code == 200, response.text
                assert [row["id"] for row in response.json()] == [str(key)]
                detail = client.get(path + f"/{key}")
                assert detail.status_code == 200
                record = detail.json()
                assert record["status"] == status and record["last_error"] == error
                assert record["attempts"] == len(record["executions"]) == attempts
                assert record["automatic_resend"] is False
                assert client.get(path + f"/{wrong_key}").status_code == 404
            membership.active = False
            membership.version += 1
            world.store.commit()
            forged = {"x-tenant-id": str(world.ctx.tenant_id), "x-role": "owner"}
            assert client.get(foreign_path, headers=forged).status_code == 403
            assert (
                client.get(foreign_path + f"/{foreign_action.action_id}", headers=forged).status_code == 403
            )
            assert client.get(own_path + f"/{own_action.action_id}").status_code == 200
            client.close()
        world.store.rollback()
        assert totals == {
            model: world.store.scalar(select(func.count()).select_from(model)) for model in totals
        }, "Inspection must not enqueue actions, execute attempts or change source history"
