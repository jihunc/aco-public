"""Policy grants must be narrower than human action initiation."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from policy_support import activate, configure_recipients, policy_world, save
from sqlalchemy import select

from aco.features.actions.models import ScheduledAction
from aco.features.actions.service import ActionService, ScheduledCommand, worker_context
from aco.features.identity.models import Membership, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.models import PolicyActivation, PolicyEvent, PolicyOccurrence
from aco.features.policies.service import PolicyService


def setup_active(client: Any, store: Any, ctx: Any, saved: Any) -> dict[str, Any]:
    configure_recipients(client, store, ctx)
    definition = dict(saved["definition"])
    definition["rules"] = [{**definition["rules"][0], "offset_days": 120}]
    saved = save(
        client,
        ctx.tenant_id,
        policy_id=saved["policy_id"],
        name=saved["name"],
        expected_version=saved["version"],
        definition=definition,
    )
    return activate(client, ctx, saved)


def test_stored_policy_actor_can_read_only_its_action_scope() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved)
        action = store.scalar(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        assert action is not None
        assert action.source_event_id is None
        event = store.get(PolicyEvent, action.policy_event_id)
        activation = store.get(PolicyActivation, action.policy_activation_id)
        assert event and event.policy_version_id == action.policy_version_id
        assert activation and activation.enabling_human_id == ctx.actor_id
        proof = PolicyService(store)._proof(ctx)
        assert proof is not None
        from aco.features.accounting.models import AccountingEvent

        source = store.scalar(select(AccountingEvent).where(AccountingEvent.invoice_id == action.invoice_id))
        assert source
        command = ScheduledCommand(
            kind="accounting_recheck",
            provider_connection_id=action.provider_connection_id,
            invoice_id=action.invoice_id,
            policy_version_id=action.policy_version_id,
            source_event_id=source.id,
            due_at=now(),
        )
        for internal in (proof, worker_context(action)):
            with pytest.raises(HTTPException, match="HUMAN_INITIATION_REQUIRED"):
                ActionService(store).enqueue_action(cast(Any, internal), command)
            with pytest.raises(HTTPException, match="HUMAN_CONTEXT_REQUIRED"):
                ActionService(store).cancel_action(cast(Any, internal), action.id, action.version)
        worker = worker_context(action)
        assert IdentityService(store).authorize(worker, Permission.INVOICE_READ).id == ctx.tenant_id
        for permission in (Permission.SETTINGS, Permission.TASK_WRITE, Permission.APPROVE_EXTERNAL):
            with pytest.raises(HTTPException) as error:
                IdentityService(store).authorize(worker, permission)
            assert error.value.status_code == 403
        other = next(i for i in invoices if i.id != action.invoice_id)
        with pytest.raises(HTTPException, match="SCHEDULING_SCOPE_OUTSIDE_ACTION"):
            PolicyService(store).recompute_next_action(worker, other.id, now())
        with pytest.raises(HTTPException, match="CONTACT_SCOPE_OUTSIDE_ACTION"):
            PolicyService(store).submitted_contacts(worker, other.id)
        policy_service = PolicyService(store)
        for operation in (
            lambda: policy_service.list_policies(worker),
            lambda: policy_service.history(worker, UUID(saved["policy_id"])),
            lambda: policy_service.recipients(worker),
        ):
            with pytest.raises(HTTPException, match="TENANT_WIDE_POLICY_SCOPE_FORBIDDEN"):
                operation()
        policy = policy_service.policy(worker, UUID(saved["policy_id"]))
        preview = policy_service.preview_policy(
            worker, policy_service.view(policy, policy_service.latest(policy)), now()
        )
        assert {item.invoice_id for item in preview.items} == {action.invoice_id}
        member = store.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
            )
        )
        assert member
        member.version += 1
        store.flush()
        with pytest.raises(HTTPException) as error:
            IdentityService(store).authorize(worker, Permission.INVOICE_READ)
        assert error.value.detail in {"INITIATOR_REVOKED", "STALE_POLICY_AUTHORITY"}


def test_policy_rescheduling_deduplicates_stable_rules_and_fences_unknown() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved)
        actions = list(
            store.scalars(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        )
        assert actions
        service = PolicyService(store)
        for action in actions:
            service.recompute_next_action(ctx, action.invoice_id, now())
        assert len(
            list(store.scalars(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id)))
        ) == len(actions)
        fenced = actions[0]
        fenced.status = "unknown_outcome"
        original = (fenced.id, fenced.operation_key, fenced.policy_version_id, fenced.occurrence_id)
        store.commit()
        revised = dict(saved["definition"])
        revised["rules"] = [{**revised["rules"][0], "offset_days": 121}]
        save(
            client,
            ctx.tenant_id,
            policy_id=saved["policy_id"],
            name=saved["name"],
            expected_version=saved["version"],
            definition=revised,
            activate=True,
            native_reminders_disabled=True,
            configuration_reviewed=True,
        )
        store.expire_all()
        assert (fenced.id, fenced.operation_key, fenced.policy_version_id, fenced.occurrence_id) == original
        assert fenced.status == "unknown_outcome"
        assert (
            len(
                list(
                    store.scalars(
                        select(ScheduledAction).where(ScheduledAction.occurrence_id == fenced.occurrence_id)
                    )
                )
            )
            == 1
        )
        occurrence = store.get(PolicyOccurrence, fenced.occurrence_id)
        assert occurrence and occurrence.rule_id == UUID(revised["rules"][0]["id"])


def test_missing_operational_projection_blocks_without_deleting_financial_read() -> None:
    from aco.features.accounting.models import CollectionRecord
    from aco.features.policies.guards import PolicyGuards

    with policy_world() as (client, store, ctx, invoices, saved):
        invoice = invoices[0]
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice.id))
        store.delete(collection)
        store.commit()
        decision = PolicyGuards(store, now()).operational_status(ctx, invoice.id)
        assert not decision.allowed and decision.reason == "OPERATIONAL_PROJECTION_MISSING"
        persisted = store.get(type(invoice), invoice.id)
        assert persisted and persisted.balance_minor == invoice.balance_minor
        key = invoice.id
        store.rollback()
        response = client.get(f"/api/workspaces/{ctx.tenant_id}/receivables/{key}")
        assert response.status_code == 200


@pytest.mark.parametrize("changed_source", [False, True], ids=["same-source", "changed-source"])
def test_recheck_does_not_recreate_missing_projection_or_ignore_retained_dispute(
    changed_source: bool,
) -> None:
    import asyncio

    from accounting_support import publish_invoice

    from aco.features.accounting.ingest import AccountingService
    from aco.features.accounting.models import (
        AccountingEvent,
        AccountingObservation,
        CollectionRecord,
        InvoiceSnapshot,
        InvoiceSourceRevision,
    )
    from aco.features.policies.guards import PolicyGuards
    from aco.features.policies.models import CollectionHold
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

    with policy_world() as (client, store, ctx, invoices, _saved):
        invoice = invoices[0]
        key, connection, source_key = invoice.id, invoice.provider_connection_id, invoice.external_invoice_id
        dispute = PolicyService(store).place_hold(
            ctx, key, "active_dispute", "Retained dispute before projection loss"
        )
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == key))
        assert collection  # Genuine initial source import created this projection.
        hold_before = dict(
            store.execute(select(CollectionHold.__table__).where(CollectionHold.id == dispute))
            .mappings()
            .one()
        )
        prior_revision = invoice.source_revision
        store.delete(collection)
        store.commit()
        if changed_source:
            publish_invoice(store, ctx.tenant_id, connection, source_key, source_status="OPEN_REVIEWED")
        before = PolicyGuards(store, now()).operational_status(ctx, key)
        assert before.reason == "OPERATIONAL_PROJECTION_MISSING"
        store.rollback()

        async def recheck() -> UUID:
            async with client.app.state.business_sessions.begin() as db:
                service = AccountingService(db, DemoAccountingAdapter(db, connection))
                result = await service.recheck_invoice(ctx, source_key)
                assert result is None  # Recheck persists its event; it does not return source-hint results.
                assert service.last_observation_id
                return service.last_observation_id

        observation_id = asyncio.run(recheck())
        after = PolicyGuards(store, now()).operational_status(ctx, key)
        projection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == key))
        observation = store.get(AccountingObservation, observation_id)
        hold = store.get(CollectionHold, dispute)
        assert observation
        provenance = observation.provenance["invoice"]
        assert isinstance(provenance, dict) and provenance["currentness"] == "CURRENT"
        assert (
            hold and hold.released_at is None and hold.evidence == "Retained dispute before projection loss"
        )
        print(
            {
                "missing_projection_before": before.reason,
                "after": after.reason,
                "outbound_allowed": after.allowed,
                "recreated": projection is not None,
                "current_observation": str(observation_id),
            }
        )
        assert not after.allowed and after.reason == "OPERATIONAL_PROJECTION_MISSING"
        assert projection is None
        assert (
            dict(
                store.execute(select(CollectionHold.__table__).where(CollectionHold.id == dispute))
                .mappings()
                .one()
            )
            == hold_before
        )
        current = store.get(InvoiceSnapshot, key, populate_existing=True)
        assert current and current.verified and current.financial_status == "open"
        if changed_source:
            assert current.source_revision != prior_revision and current.source_status == "OPEN_REVIEWED"
            revision = store.scalar(
                select(InvoiceSourceRevision).where(
                    InvoiceSourceRevision.invoice_id == key,
                    InvoiceSourceRevision.source_revision == current.source_revision,
                )
            )
            event = store.scalar(
                select(AccountingEvent)
                .where(AccountingEvent.invoice_id == key)
                .order_by(AccountingEvent.created_at.desc())
            )
            assert revision and event and event.result["collection_lifecycle"] == "unavailable"
        else:
            assert current.source_revision == prior_revision


def test_genuine_first_source_import_bootstraps_operational_projection() -> None:
    from accounting_support import seed
    from identity_support import actor_client

    from aco.features.accounting.models import CollectionRecord, InvoiceSnapshot

    with actor_client("owner") as (client, store, _user, tenant):
        assert not list(store.scalars(select(InvoiceSnapshot)))
        assert not list(store.scalars(select(CollectionRecord)))
        store.rollback()
        seed(client, tenant)
        invoices = list(store.scalars(select(InvoiceSnapshot)))
        records = list(store.scalars(select(CollectionRecord)))
        assert len(invoices) == len(records) == 24
        assert {r.invoice_id for r in records} == {i.id for i in invoices}
        for invoice in invoices:
            projection = next(r for r in records if r.invoice_id == invoice.id)
            assert projection.hold_reasons == [] and projection.version == 1
            assert projection.lifecycle == ("closed" if invoice.financial_status == "paid" else "active")
        print({"first_import_invoices": len(invoices), "operational_projections": len(records)})


def test_policy_config_rejects_cross_tenant_and_stale_versions() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        response = client.put(
            f"/api/workspaces/{ctx.tenant_id}/policies/{uuid4()}",
            json={"name": "Missing", "definition": saved["definition"], "expected_version": 1},
        )
        assert response.status_code == 404
        response = client.put(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}",
            json={"name": saved["name"], "definition": saved["definition"], "expected_version": 0},
        )
        assert response.status_code == 409 and response.json()["detail"] == "VERSION_CONFLICT"


def test_database_rejects_contact_history_with_wrong_customer() -> None:
    from sqlalchemy.exc import IntegrityError

    from aco.features.policies.models import ContactReservation

    with policy_world() as (client, store, ctx, invoices, saved):
        invoice = invoices[0]
        other = next(i.customer_id for i in invoices if i.customer_id != invoice.customer_id)
        store.add(
            ContactReservation(
                tenant_id=ctx.tenant_id,
                invoice_id=invoice.id,
                customer_id=other,
                destination="wrong@example.test",
                channel="email",
                operation_id=uuid4(),
                reserved_at=now(),
                evidence={"origin": "invalid synthetic binding"},
            )
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            store.flush()
        store.rollback()


def test_owner_cannot_mint_global_pause_or_naive_preview_time() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/automation/pause", json={"scope": "global", "paused": False}
        )
        assert response.status_code == 403
        response = client.get(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/preview",
            params={"as_of": "2026-11-18T10:00:00"},
        )
        assert response.status_code == 422
