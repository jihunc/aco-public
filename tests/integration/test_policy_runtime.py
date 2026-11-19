"""Actual durable eligibility execution and fixture-seeded ticket rejection boundaries."""

from datetime import timedelta
from uuid import uuid4

import pytest
from action_support import World, wait_action, workers
from fastapi import HTTPException
from integration.test_policy_authority import setup_active
from policy_support import policy_world
from sqlalchemy import select, text

from aco.features.accounting.models import AccountingObservation, CollectionRecord
from aco.features.actions.models import ActionExecution, OutboxEvent, ScheduledAction
from aco.features.actions.service import worker_context
from aco.features.identity.models import Membership, now
from aco.features.policies.dto import GuardDecision, OutboundProposal
from aco.features.policies.guards import PolicyGuards
from aco.features.policies.models import (
    ContactReservation,
    DispatchRead,
    DispatchTicketRecord,
    PolicyOccurrence,
)
from aco.features.policies.service import PolicyService


def test_real_policy_worker_reads_source_fails_truthfully_and_continues_same_occurrence() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved)
        action = store.scalar(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        assert action
        action.due_at = now()
        outbox = store.scalar(select(OutboxEvent).where(OutboxEvent.action_id == action.id))
        assert outbox
        outbox.due_at = action.due_at
        store.execute(text("CREATE TABLE synthetic_submissions (action_id uuid NOT NULL)"))
        store.execute(text("CREATE TABLE synthetic_effects (action_id uuid NOT NULL)"))
        store.commit()
        assert action.policy_version_id is not None
        world = World(
            client,
            store,
            ctx,
            action.provider_connection_id,
            action.invoice_id,
            action.policy_version_id,
            uuid4(),
        )
        key, occurrence_id = action.operation_key, action.occurrence_id
        with workers(world, crash="domain"):
            completed = wait_action(world, action.id, "failed")
            assert completed.last_error == "OUTBOUND_EXECUTOR_UNAVAILABLE"
        store.rollback()
        completed = world.action(action.id)
        assert completed.result["status"] == "failed" and not completed.automatic_resend
        assert completed.operation_key == key
        occurrence = store.get(PolicyOccurrence, occurrence_id)
        assert occurrence and occurrence.state == "pending_recompute"
        assert occurrence.reconsider_after is not None and occurrence.reconsider_after > now()
        read = store.scalar(select(DispatchRead).where(DispatchRead.action_id == action.id))
        assert read and read.observation_id
        observation = store.get(AccountingObservation, read.observation_id)
        assert observation and observation.actor_id == action.worker_actor_id
        execution = store.get(ActionExecution, read.attempt_id)
        assert execution and execution.action_id == action.id and execution.status == "failed"
        assert world.submission_count(action.id) == 0 and world.effect_count(action.id) == 0
        assert not list(
            store.scalars(select(ContactReservation).where(ContactReservation.tenant_id == ctx.tenant_id))
        )
        # Explicit recompute retains the occurrence with a new immutable grant.
        store.rollback()
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/recompute",
            json={"expected_version": saved["version"]},
        )
        assert response.status_code == 200, response.text
        store.expire_all()
        future = list(
            store.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.occurrence_id == occurrence_id, ScheduledAction.status == "queued"
                )
            )
        )
        assert len(future) == 1 and future[0].id != action.id and future[0].due_at > now()
        assert future[0].operation_key != key
        print(
            {
                "policy_due": str(action.id),
                "observation": str(read.observation_id),
                "attempt": str(read.attempt_id),
                "continuation": str(future[0].id),
                "occurrence": str(occurrence_id),
                "outbound_submissions": 0,
            }
        )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("expired", "TICKET_EXPIRED"),
        ("future", "TICKET_EXPIRED"),
        ("consumed", "TICKET_ALREADY_CONSUMED"),
        ("restart", "TICKET_WORKER_RESTARTED"),
        ("attempt", "TICKET_ATTEMPT_MISMATCH"),
        ("hold", "TICKET_VERSION_CHANGED"),
        ("observation", "STALE_ACCOUNTING_DATA"),
    ],
)
def test_fixture_seeded_tickets_reject_specific_changed_proof(change: str, reason: str) -> None:
    # These are synthetic persisted tickets, not successful authorized production issuance.
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        action = store.scalar(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        assert action
        worker = worker_context(action)
        at, incarnation, attempt = now(), uuid4(), uuid4()
        observation = store.scalar(
            select(AccountingObservation).where(AccountingObservation.invoice_id == action.invoice_id)
        )
        assert observation and observation.actor_id != worker.actor_id
        read = DispatchRead(
            tenant_id=ctx.tenant_id,
            invoice_id=action.invoice_id,
            actor_id=worker.actor_id,
            action_id=action.id,
            action_version=action.version,
            lease_token=None,
            attempt_id=attempt,
            incarnation=incarnation,
            observation_id=observation.id,
            started_at=at,
        )
        store.add(read)
        store.flush()
        proposal = OutboundProposal(
            action_id=action.id,
            attempt_id=attempt,
            read_id=read.id,
            destination=str(action.command["destination"]),
        )
        guard = PolicyGuards(store, at, incarnation)
        ticket = DispatchTicketRecord(
            tenant_id=ctx.tenant_id,
            action_id=action.id,
            attempt_id=attempt,
            read_id=read.id,
            incarnation=incarnation,
            issued_at=at,
            bindings=guard._bindings(worker, proposal),
        )
        if change == "expired":
            ticket.issued_at = at - timedelta(seconds=60)
        if change == "future":
            ticket.issued_at = at + timedelta(seconds=1)
        if change == "consumed":
            ticket.consumed_at = at
        if change == "restart":
            ticket.incarnation = uuid4()
        if change == "attempt":
            ticket.attempt_id = uuid4()
        store.add(ticket)
        store.flush()
        if change == "hold":
            collection = store.scalar(
                select(CollectionRecord).where(CollectionRecord.invoice_id == action.invoice_id)
            )
            assert collection
            collection.hold_reasons = ["active_dispute"]
            collection.version += 1
            store.flush()
        with pytest.raises(HTTPException) as error:
            guard.consume_dispatch(worker, ticket.id, proposal)
        assert error.value.detail == reason
        assert reason != "OUTBOUND_EXECUTOR_UNAVAILABLE"
        assert ticket.consumed_at == (at if change == "consumed" else None)
        print({"ticket_fixture": change, "rejection": error.value.detail, "production_issuance": False})


def test_caller_decision_cannot_issue_ticket_or_reserve_outreach() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        action = store.scalar(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        assert action
        proposal = OutboundProposal(
            action_id=action.id, attempt_id=uuid4(), destination=str(action.command["destination"])
        )
        guard = PolicyGuards(store, now())
        decision = guard.evaluate_outbound(ctx, proposal, now())
        assert not decision.allowed and "OUTBOUND_EXECUTOR_UNAVAILABLE" in decision.reasons
        with pytest.raises(HTTPException):
            guard.reserve_dispatch(ctx, GuardDecision(allowed=True), proposal)
        assert list(store.scalars(select(DispatchTicketRecord))) == []
        assert list(store.scalars(select(ContactReservation))) == []


@pytest.mark.parametrize(
    ("role", "pause", "status"),
    [
        ("operator", True, 200),
        ("operator", False, 403),
        ("finance_manager", False, 200),
        ("viewer", True, 403),
    ],
)
def test_pause_resume_roles_do_not_grant_policy_settings(role: str, pause: bool, status: int) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        member = store.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
            )
        )
        assert member
        # Add a second genuine user as the acting Finance/Operator; preserve enabling Owner authority.
        from aco.core.types import Role
        from aco.features.identity.models import User
        from aco.features.identity.service import IdentityService, Permission

        user = User(issuer="synthetic-policy-roles", subject=str(uuid4()), email=f"{uuid4()}@example.test")
        store.add(user)
        store.flush()
        store.add(Membership(tenant_id=ctx.tenant_id, user_id=user.id, role=role))
        store.commit()
        actor = ctx.model_copy(update={"actor_id": user.id, "role": Role(role)})
        service = PolicyService(store)
        if status == 200:
            result = service.set_outbound_pause(actor, "tenant", pause)
            assert result.paused == pause
        else:
            with pytest.raises(HTTPException) as error:
                service.set_outbound_pause(actor, "tenant", pause)
            assert error.value.status_code == status
        with pytest.raises(HTTPException):
            IdentityService(store).authorize(actor, Permission.SETTINGS)
