"""Both transaction orders of policy edit versus real persisted worker claim."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from integration.test_policy_authority import setup_active
from policy_support import AT, policy_world
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import (
    AccountingEvent,
    AccountingObservation,
    CollectionRecord,
    Customer,
    CustomerContact,
    DemoSourceRecord,
    InvoiceSnapshot,
    InvoiceSourceRevision,
    VerifiedPaymentAllocation,
)
from aco.features.actions.models import ActionExecution, OutboxEvent, ScheduledAction
from aco.features.actions.service import ActionService, worker_context
from aco.features.identity.models import AuditEvent, Tenant, now
from aco.features.policies.dto import PolicySave, RecipientSave
from aco.features.policies.guards import PolicyGuards
from aco.features.policies.models import (
    CollectionHold,
    CollectionPolicy,
    CollectionPolicyVersion,
    ContactReservation,
    PolicyActivation,
    PolicyEvent,
    PolicyOccurrence,
    RecipientControl,
)
from aco.features.policies.service import PolicyService
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
from aco.worker.app import WorkerRuntime


@pytest.mark.parametrize("first", ["claim", "edit"])
def test_policy_edit_and_claim_serialize_without_replacing_executing_occurrence(first: str) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved)
        action = store.scalar(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        assert action
        action.due_at = now()
        outbox = store.scalar(select(OutboxEvent).where(OutboxEvent.action_id == action.id))
        assert outbox
        outbox.due_at = action.due_at
        store.commit()
        runtime = WorkerRuntime(client.app.state.settings)

        async def dispatch() -> None:
            try:
                async with runtime.app.open_async():
                    await runtime.dispatcher.dispatch_outbox(100)
            finally:
                await runtime.engine.dispose()

        asyncio.run(dispatch())
        store.refresh(action)
        assert action.job_id is not None
        worker, key, occurrence = worker_context(action), action.id, action.occurrence_id
        command = PolicySave(
            name=saved["name"],
            definition=saved["definition"],
            expected_version=saved["version"],
            activate=True,
            native_reminders_disabled=True,
            configuration_reviewed=True,
        )
        store.rollback()
        locked, contender = Event(), Event()

        def run(kind: str) -> str:
            with Session(store.get_bind()) as session:
                if kind != first:
                    assert locked.wait(10)
                    contender.set()
                try:
                    if kind == "claim":
                        ActionService(session).claim_action(worker, key, worker.action_version)
                    else:
                        PolicyService(session).save_policy(
                            ctx,
                            command,
                            command.expected_version,
                            UUID(saved["policy_id"]),
                        )
                    if kind == first:
                        locked.set()
                        assert contender.wait(10)
                    session.commit()
                    return "committed"
                except HTTPException as exc:
                    session.rollback()
                    return str(exc.detail)

        with ThreadPoolExecutor(2) as pool:
            claim = pool.submit(run, "claim")
            edit = pool.submit(run, "edit")
            results = claim.result(timeout=20), edit.result(timeout=20)
        store.expire_all()
        current = store.get(ScheduledAction, key)
        assert current
        actions = list(
            store.scalars(select(ScheduledAction).where(ScheduledAction.occurrence_id == occurrence))
        )
        attempts = list(store.scalars(select(ActionExecution).where(ActionExecution.action_id == key)))
        if first == "claim":
            assert results == ("committed", "committed")
            assert current.status == "executing" and len(attempts) == 1 and len(actions) == 1
        else:
            assert results[0] == "STALE_SERVICE_AUTHORITY"
            assert results[1] == "committed"
            assert current.status == "cancelled" and attempts == []
            assert len([a for a in actions if a.status == "queued"]) == 1
        print(
            {
                "first": first,
                "claim": results[0],
                "edit": results[1],
                "historical_job": current.job_id,
                "action_count": len(actions),
                "attempts": len(attempts),
            }
        )


def stored_row(db: Session, model: Any, key: UUID) -> dict[str, Any]:
    table = model.__table__
    return dict(db.execute(select(table).where(table.c.id == key)).mappings().one())


def test_cached_capacity_release_preserves_complete_committed_unknown() -> None:
    """A cached reserved state must never overwrite subsequently committed uncertain exposure."""
    with policy_world() as (_, store, ctx, invoices, _saved):
        invoice_id = invoices[0].id
        row = PolicyGuards(store, AT).reserve_contact(
            ctx, invoice_id, "unknown@example.test", "email", uuid4()
        )
        key = row.id
        store.commit()
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached = stale.get(ContactReservation, key)
            assert cached and cached.status == "reserved" and cached.submitted_at is None
            stale.commit()
            with Session(store.get_bind()) as writer:
                current = writer.get(ContactReservation, key)
                assert current
                current.status, current.submitted_at = "unknown_outcome", AT
                current.evidence = {"origin": "Synthetic independently committed uncertain submission"}
                current.version += 1
                writer.commit()
                before = stored_row(writer, ContactReservation, key)
                audit_before = list(writer.execute(select(AuditEvent.__table__)).mappings())
            assert cached.status == "reserved" and cached.submitted_at is None
            refusal = None
            try:
                PolicyGuards(stale, AT + timedelta(seconds=1)).release_unused_capacity(ctx, key)
                stale.commit()
            except HTTPException as exc:
                refusal = exc.detail
                stale.rollback()
            with Session(store.get_bind()) as check:
                after = stored_row(check, ContactReservation, key)
                audit_after = list(check.execute(select(AuditEvent.__table__)).mappings())
                print({"cached_release_refusal": refusal, "complete_row_preserved": before == after})
                assert refusal == "VERIFIED_NO_EFFECT_REQUIRED"
                assert after == before
                assert audit_after == audit_before
                decision = PolicyGuards(check, AT).contact_status(
                    ctx, invoices[0].customer_id, "unknown@example.test"
                )
                assert not decision.allowed and decision.count_24h == decision.count_7d == 1


def test_cached_old_reservation_counts_recent_unknown_and_denies_extra_slot() -> None:
    """Counting and reservation use committed submitted_at, not an old cached reserved_at."""
    with policy_world() as (_, store, ctx, invoices, _saved):
        invoice_id, customer_id = invoices[0].id, invoices[0].customer_id
        row = PolicyGuards(store, AT - timedelta(days=8)).reserve_contact(
            ctx, invoice_id, "count@example.test", "email", uuid4()
        )
        key = row.id
        store.commit()
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached = stale.get(ContactReservation, key)
            assert cached and cached.submitted_at is None
            stale.commit()
            with Session(store.get_bind()) as writer:
                current = writer.get(ContactReservation, key)
                assert current
                current.status, current.submitted_at = "unknown_outcome", AT
                current.evidence = {"origin": "Synthetic current uncertain exposure"}
                current.version += 1
                writer.commit()
                before = stored_row(writer, ContactReservation, key)
            assert cached.submitted_at is None
            decision = PolicyGuards(stale, AT).contact_status(ctx, customer_id, "count@example.test")
            refusal, new_id = None, None
            try:
                new_id = (
                    PolicyGuards(stale, AT)
                    .reserve_contact(ctx, invoice_id, "count@example.test", "email", uuid4())
                    .id
                )
                stale.commit()
            except HTTPException as exc:
                refusal = exc.detail
                stale.rollback()
            with Session(store.get_bind()) as check:
                rows = list(check.scalars(select(ContactReservation)))
                print(
                    {
                        "cached_counts": [decision.count_24h, decision.count_7d],
                        "refusal": refusal,
                        "rows": len(rows),
                    }
                )
                assert not decision.allowed and decision.count_24h == decision.count_7d == 1
                assert decision.reason == refusal == "customer_contact_cap" and new_id is None
                assert len(rows) == 1 and stored_row(check, ContactReservation, key) == before


def test_cached_collection_composes_independent_dispute_and_promise() -> None:
    """A later hold adds to the latest committed projection and increments its current version."""
    with policy_world() as (_, store, ctx, invoices, _saved):
        invoice_id = invoices[0].id
        store.rollback()
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached = stale.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id))
            assert cached and cached.hold_reasons == []
            prior_version = cached.version
            stale.commit()
            with Session(store.get_bind()) as writer:
                dispute = PolicyService(writer, AT).place_hold(
                    ctx, invoice_id, "active_dispute", "Independent dispute evidence"
                )
                writer.commit()
                dispute_before = stored_row(writer, CollectionHold, dispute)
                committed_version = writer.scalar(
                    select(CollectionRecord.version).where(CollectionRecord.invoice_id == invoice_id)
                )
            assert cached.hold_reasons == [] and cached.version == prior_version
            promise = PolicyService(stale, AT).place_hold(
                ctx, invoice_id, "active_promise", "Independent promise evidence", AT + timedelta(days=1)
            )
            stale.commit()
            with Session(store.get_bind()) as check:
                actual = check.scalar(
                    select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id)
                )
                assert actual
                print(
                    {
                        "cached_hold_reasons": actual.hold_reasons,
                        "version": actual.version,
                        "committed_version": committed_version,
                    }
                )
                assert actual.hold_reasons == ["active_dispute", "active_promise"]
                assert committed_version is not None and actual.version == committed_version + 1
                assert stored_row(check, CollectionHold, dispute) == dispute_before
                assert (
                    stored_row(check, CollectionHold, promise)["evidence"] == "Independent promise evidence"
                )


@pytest.mark.parametrize("stale_accounting", [True, False], ids=["removes-stale", "no-change-control"])
def test_cached_accounting_reconciliation_preserves_independent_dispute(stale_accounting: bool) -> None:
    """Only a real fresh source read may remove stale_accounting; unrelated holds survive it."""
    with policy_world() as (client, store, ctx, invoices, _saved):
        invoice = invoices[0]
        invoice_id, connection, source_key = (
            invoice.id,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
        )
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id))
        assert collection
        if stale_accounting:
            collection.hold_reasons = ["stale_accounting"]
            collection.version += 1
        store.commit()

        async def reconcile() -> None:
            async with client.app.state.business_sessions.begin() as db:
                cached = await db.scalar(
                    select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id)
                )
                assert cached
                before_reasons = list(cached.hold_reasons)
                with Session(store.get_bind()) as writer:
                    dispute = PolicyService(writer).place_hold(
                        ctx, invoice_id, "active_dispute", "Independent accounting dispute evidence"
                    )
                    writer.commit()
                    dispute_before = stored_row(writer, CollectionHold, dispute)
                    committed_version = writer.scalar(
                        select(CollectionRecord.version).where(CollectionRecord.invoice_id == invoice_id)
                    )
                assert cached.hold_reasons == before_reasons
                service = AccountingService(db, DemoAccountingAdapter(db, connection))
                await service.recheck_invoice(ctx, source_key)
                observation_id = service.last_observation_id
                assert observation_id
            with Session(store.get_bind()) as check:
                actual = check.scalar(
                    select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id)
                )
                observation = check.get(AccountingObservation, observation_id)
                assert actual and observation
                provenance = observation.provenance["invoice"]
                assert isinstance(provenance, dict)
                print(
                    {
                        "cached_accounting_before": before_reasons,
                        "after": actual.hold_reasons,
                        "currentness": provenance["currentness"],
                        "version": actual.version,
                        "committed_version": committed_version,
                    }
                )
                assert provenance["currentness"] == "CURRENT"
                assert actual.hold_reasons == ["active_dispute"]
                assert committed_version is not None and actual.version == committed_version + int(
                    stale_accounting
                )
                assert stored_row(check, CollectionHold, dispute) == dispute_before

        asyncio.run(reconcile())


@pytest.mark.parametrize("first", ["hold", "accounting"])
def test_hold_and_accounting_current_state_writers_serialize_in_both_orders(first: str) -> None:
    """Real contending transactions retain both fresh financial evidence and an independent hold."""
    with policy_world() as (client, store, ctx, invoices, _saved):
        invoice = invoices[0]
        key, connection, source_key = invoice.id, invoice.provider_connection_id, invoice.external_invoice_id
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == key))
        assert collection
        collection.hold_reasons = ["stale_accounting"]
        collection.version += 1
        base_version = collection.version
        store.commit()
        locked, contender = Event(), Event()

        def before_operation(kind: str) -> None:
            if kind != first:
                assert locked.wait(10)
                contender.set()

        def before_commit(kind: str) -> None:
            if kind == first:
                locked.set()
                assert contender.wait(10)

        def hold() -> UUID:
            with Session(store.get_bind()) as db:
                before_operation("hold")
                hold_id = PolicyService(db).place_hold(ctx, key, "active_dispute", "Concurrent dispute")
                before_commit("hold")
                db.commit()
                return hold_id

        def accounting() -> UUID:
            async def run() -> UUID:
                async with client.app.state.business_sessions.begin() as db:
                    before_operation("accounting")
                    service = AccountingService(db, DemoAccountingAdapter(db, connection))
                    await service.recheck_invoice(ctx, source_key)
                    before_commit("accounting")
                    assert service.last_observation_id
                    return service.last_observation_id

            return asyncio.run(run())

        with ThreadPoolExecutor(2) as pool:
            held, read = pool.submit(hold), pool.submit(accounting)
            hold_id, observation_id = held.result(timeout=30), read.result(timeout=30)
        store.expire_all()
        actual = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == key))
        evidence = store.get(CollectionHold, hold_id)
        observation = store.get(AccountingObservation, observation_id)
        assert actual and actual.hold_reasons == ["active_dispute"] and actual.version == base_version + 2
        assert evidence and evidence.released_at is None and evidence.evidence == "Concurrent dispute"
        assert observation
        provenance = observation.provenance["invoice"]
        assert isinstance(provenance, dict) and provenance["currentness"] == "CURRENT"
        print(
            {"writer_first": first, "current_holds": actual.hold_reasons, "current_version": actual.version}
        )


def test_cached_contact_replay_returns_current_committed_unknown_without_mutation() -> None:
    with policy_world() as (_, store, ctx, invoices, _saved):
        key, operation = invoices[0].id, uuid4()
        row = PolicyGuards(store, AT).reserve_contact(ctx, key, "replay@example.test", "email", operation)
        reservation = row.id
        store.commit()
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached = stale.get(ContactReservation, reservation)
            assert cached and cached.status == "reserved"
            stale.commit()
            with Session(store.get_bind()) as writer:
                current = writer.get(ContactReservation, reservation)
                assert current
                current.status, current.submitted_at = "unknown_outcome", AT
                current.evidence = {"origin": "Synthetic uncertain exposure for idempotent replay"}
                current.version += 1
                writer.commit()
                before = stored_row(writer, ContactReservation, reservation)
            assert cached.status == "reserved"
            replay = PolicyGuards(stale, AT).reserve_contact(
                ctx, key, "replay@example.test", "email", operation
            )
            assert replay is cached and replay.status == "unknown_outcome" and replay.submitted_at == AT
            assert replay.version == before["version"] and replay.evidence == before["evidence"]
            stale.commit()
            assert stored_row(stale, ContactReservation, reservation) == before


def test_cached_promise_expiry_uses_current_hold_and_projection() -> None:
    with policy_world() as (_, store, ctx, invoices, _saved):
        key = invoices[0].id
        promise = PolicyService(store, AT).place_hold(ctx, key, "active_promise", "Original promise", AT)
        store.commit()
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached_hold = stale.get(CollectionHold, promise)
            cached_projection = stale.scalar(
                select(CollectionRecord).where(CollectionRecord.invoice_id == key)
            )
            assert cached_hold and cached_hold.expires_at == AT
            assert cached_projection and cached_projection.hold_reasons == ["active_promise"]
            projection_id = cached_projection.id
            stale.commit()
            with Session(store.get_bind()) as writer:
                current = writer.get(CollectionHold, promise)
                assert current
                # Synthetic later authoritative promise amendment; T13 owns the actual capture workflow.
                current.expires_at = AT + timedelta(days=1)
                current.version += 1
                dispute = PolicyService(writer, AT).place_hold(ctx, key, "active_dispute", "New dispute")
                writer.commit()
                before_hold = stored_row(writer, CollectionHold, promise)
                before_dispute = stored_row(writer, CollectionHold, dispute)
                before_projection = stored_row(writer, CollectionRecord, projection_id)
            assert cached_hold.expires_at == AT and cached_projection.hold_reasons == ["active_promise"]
            PolicyService(stale, AT).release_expired_holds(ctx, key, AT)
            stale.commit()
            assert stored_row(stale, CollectionHold, promise) == before_hold
            assert stored_row(stale, CollectionHold, dispute) == before_dispute
            assert stored_row(stale, CollectionRecord, projection_id) == before_projection


@pytest.mark.parametrize("expected_version", [1, 2])
def test_retained_recipient_review_uses_current_committed_version(expected_version: int) -> None:
    """Stale review cannot restore revoked channels or create any policy/audit work."""
    from policy_support import activate

    with policy_world() as (client, store, ctx, invoices, saved):
        contact = store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoices[0].customer_id)
        )
        assert contact
        key = contact.id
        command = RecipientSave(
            expected_version=0,
            expected_contact_revision=contact.source_revision,
            expected_destination=contact.email,
            timezone="Australia/Melbourne",
            timezone_verified=True,
            destination_verified=True,
            allowed_channels=["email", "voice"],
            opt_out=False,
            evidence="Synthetic initial recipient review",
        )
        initial = PolicyService(store).save_recipient(ctx, key, command)
        store.commit()
        assert initial.version == 1 and initial.allowed_channels == ["email", "voice"]
        saved = activate(client, ctx, saved)
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached = stale.scalar(select(RecipientControl).where(RecipientControl.contact_id == key))
            assert cached and cached.version == 1
            row_id = cached.id
            stale.commit()
            with Session(store.get_bind()) as writer:
                revoked = PolicyService(writer).save_recipient(
                    ctx,
                    key,
                    command.model_copy(
                        update={
                            "expected_version": 1,
                            "allowed_channels": [],
                            "evidence": "Independent committed revocation of all channels",
                        }
                    ),
                )
                writer.commit()
                assert revoked.version == 2 and revoked.allowed_channels == []
                before = recipient_review_state(writer, ctx.tenant_id)
            assert cached.version == 1 and cached.allowed_channels == ["email", "voice"]
            refusal = None
            result = None
            try:
                result = PolicyService(stale).save_recipient(
                    ctx,
                    key,
                    command.model_copy(
                        update={
                            "expected_version": expected_version,
                            "allowed_channels": ["email"],
                            "evidence": "Synthetic explicitly reviewed recipient update",
                        }
                    ),
                )
                stale.commit()
            except HTTPException as exc:
                refusal = (exc.status_code, exc.detail)
                stale.rollback()
            with Session(store.get_bind()) as check:
                after = recipient_review_state(check, ctx.tenant_id)
                current = stored_row(check, RecipientControl, row_id)
                print(
                    {
                        "recipient_expected_version": expected_version,
                        "refusal": refusal,
                        "current_version": current["version"],
                        "channels": current["allowed_channels"],
                        "complete_related_state_preserved": before == after,
                    }
                )
                if expected_version == 1:
                    assert refusal == (409, "VERSION_CONFLICT")
                    assert result is None and after == before
                else:
                    assert refusal is None and result and result.version == 3
                    assert current["version"] == 3 and current["allowed_channels"] == ["email"]
                    assert current["evidence"] == "Synthetic explicitly reviewed recipient update"
                    assert current["reviewed_destination"] == contact.email
                    assert current["reviewed_contact_revision"] == contact.source_revision
                    assert current["reviewer_id"] == ctx.actor_id
                    assert len(after["identity_audit_events"]) > len(before["identity_audit_events"])


def recipient_review_state(db: Session, tenant_id: UUID) -> dict[str, list[dict[str, Any]]]:
    """Read actual columns, bypassing ORM cache, including every related mutation target."""
    result = {}
    for model in (
        Tenant,
        RecipientControl,
        Customer,
        CustomerContact,
        InvoiceSnapshot,
        CollectionRecord,
        DemoSourceRecord,
        AccountingObservation,
        AccountingEvent,
        InvoiceSourceRevision,
        VerifiedPaymentAllocation,
        CollectionPolicy,
        CollectionPolicyVersion,
        PolicyActivation,
        PolicyEvent,
        PolicyOccurrence,
        ScheduledAction,
        OutboxEvent,
        AuditEvent,
    ):
        table = model.__table__
        tenant_column = table.c.id if model is Tenant else table.c.tenant_id
        result[model.__tablename__] = [
            dict(row)
            for row in db.execute(
                select(table).where(tenant_column == tenant_id).order_by(table.c.id)
            ).mappings()
        ]
    return result


@pytest.mark.parametrize("path", ["view", "preview"])
def test_retained_recipient_reads_observe_revocation(path: str) -> None:
    with policy_world() as (_, store, ctx, invoices, saved):
        contact = store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoices[0].customer_id)
        )
        assert contact
        key = contact.id
        command = RecipientSave(
            expected_version=0,
            expected_contact_revision=contact.source_revision,
            expected_destination=contact.email,
            timezone="Australia/Melbourne",
            timezone_verified=True,
            destination_verified=True,
            allowed_channels=["email"],
            opt_out=False,
            evidence="Synthetic initial review",
        )
        PolicyService(store).save_recipient(ctx, key, command)
        store.commit()
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached = stale.scalar(select(RecipientControl).where(RecipientControl.contact_id == key))
            assert cached and cached.version == 1
            stale.commit()
            with Session(store.get_bind()) as writer:
                result = PolicyService(writer).save_recipient(
                    ctx,
                    key,
                    command.model_copy(
                        update={
                            "expected_version": 1,
                            "allowed_channels": [],
                            "evidence": "Synthetic independent revocation",
                        }
                    ),
                )
                writer.commit()
                assert result.version == 2
                before = recipient_review_state(writer, ctx.tenant_id)
            assert cached and cached.version == 1
            service = PolicyService(stale)
            if path == "view":
                actual = next(r for r in service.recipients(ctx) if r.contact_id == key)
                print({"read": path, "version": actual.version, "channels": actual.allowed_channels})
                assert actual.version == 2 and actual.allowed_channels == []
            else:
                policy = service.policy(ctx, UUID(saved["policy_id"]))
                preview = service.preview_policy(ctx, service.view(policy, service.latest(policy)), AT)
                items = [i for i in preview.items if i.invoice_id == invoices[0].id]
                assert items
                print({"read": path, "reasons": [i.reasons for i in items]})
                assert all("recipient_channel_excluded" in i.reasons for i in items)
            stale.commit()
            with Session(store.get_bind()) as check:
                assert recipient_review_state(check, ctx.tenant_id) == before


@pytest.mark.parametrize("path", ["contact_view", "contact_preview", "due_preview"])
def test_retained_source_reads_use_committed_import(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from accounting_support import apply, publish_invoice

    monkeypatch.setattr("aco.features.accounting.seed.now", lambda: datetime(2026, 11, 16, tzinfo=UTC))
    with policy_world() as (client, store, ctx, invoices, saved):
        invoice = next(i for i in invoices if i.due_date == date(2026, 11, 16))
        contact = store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
        )
        assert contact
        command = RecipientSave(
            expected_version=0,
            expected_contact_revision=contact.source_revision,
            expected_destination=contact.email,
            timezone="Australia/Melbourne",
            timezone_verified=True,
            destination_verified=True,
            allowed_channels=["email"],
            opt_out=False,
            evidence="Synthetic reviewed source",
        )
        PolicyService(store).save_recipient(ctx, contact.id, command)
        store.commit()
        key, invoice_id, connection, external_key = (
            contact.id,
            invoice.id,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
        )
        with Session(store.get_bind(), expire_on_commit=False) as stale:
            cached_contact = stale.get(CustomerContact, key)
            cached_invoice = stale.get(InvoiceSnapshot, invoice_id)
            assert cached_contact and cached_invoice
            old_email, old_revision = cached_contact.email, cached_contact.source_revision
            assert cached_invoice.due_date == date(2026, 11, 16)
            stale.commit()
            with Session(store.get_bind()) as writer:
                if path.startswith("contact"):
                    customer = writer.get(Customer, invoice.customer_id)
                    assert customer
                    source = writer.scalar(
                        select(DemoSourceRecord)
                        .where(
                            DemoSourceRecord.tenant_id == ctx.tenant_id,
                            DemoSourceRecord.provider_connection_id == connection,
                            DemoSourceRecord.kind == "customer",
                            DemoSourceRecord.external_id == customer.external_customer_id,
                        )
                        .order_by(DemoSourceRecord.revision.desc())
                    )
                    assert source
                    writer.add(
                        DemoSourceRecord(
                            tenant_id=ctx.tenant_id,
                            provider_connection_id=connection,
                            organisation_id=source.organisation_id,
                            kind="customer",
                            external_id=source.external_id,
                            revision=source.revision + 1,
                            payload={**source.payload, "email": "changed-recipient@example.test"},
                            effective_at=now(),
                            observed_at=now(),
                        )
                    )
                    writer.commit()
                event = publish_invoice(
                    writer,
                    ctx.tenant_id,
                    connection,
                    external_key,
                    **({"due_date": "2026-11-23"} if path == "due_preview" else {}),
                )
            apply(client.app, ctx, event)
            with Session(store.get_bind()) as check:
                current_contact = check.get(CustomerContact, key)
                current_invoice = check.get(InvoiceSnapshot, invoice_id)
                assert current_contact and current_invoice
                before = recipient_review_state(check, ctx.tenant_id)
                if path.startswith("contact"):
                    assert current_contact.email == "changed-recipient@example.test"
                    assert current_contact.source_revision != old_revision
                else:
                    assert current_invoice.due_date == date(2026, 11, 23)
            assert cached_contact.email == old_email and cached_invoice.due_date == date(2026, 11, 16)
            service = PolicyService(stale)
            if path == "contact_view":
                result = next(r for r in service.recipients(ctx) if r.contact_id == key)
                print({"path": path, "destination": result.destination})
                assert result.destination == "changed-recipient@example.test"
            else:
                policy = service.policy(ctx, UUID(saved["policy_id"]))
                preview = service.preview_policy(ctx, service.view(policy, service.latest(policy)), AT)
                item = next(i for i in preview.items if i.invoice_id == invoice_id)
                print(
                    {
                        "path": path,
                        "destination": item.destination,
                        "due_date": str(item.due_date),
                        "next_at": str(item.next_at),
                        "reasons": item.reasons,
                    }
                )
                if path == "contact_preview":
                    assert (
                        item.destination == "changed-recipient@example.test"
                        and "recipient_unverified" in item.reasons
                    )
                else:
                    assert item.due_date == date(2026, 11, 23)
                    assert item.next_at == datetime(2026, 11, 25, 22, tzinfo=UTC)
            stale.commit()
            with Session(store.get_bind()) as check:
                assert recipient_review_state(check, ctx.tenant_id) == before


def import_contact_destination(
    client: Any, store: Session, ctx: Any, invoice: InvoiceSnapshot, destination: str | None
) -> UUID:
    """Append synthetic source evidence and import through the actual accounting service."""
    from accounting_support import apply, publish_invoice

    customer_id, connection, external = (
        invoice.customer_id,
        invoice.provider_connection_id,
        invoice.external_invoice_id,
    )
    store.rollback()
    with Session(store.get_bind()) as writer:
        contact = writer.scalar(select(CustomerContact).where(CustomerContact.customer_id == customer_id))
        customer = writer.get(Customer, customer_id)
        assert contact and customer
        key = contact.id
        source = writer.scalar(
            select(DemoSourceRecord)
            .where(
                DemoSourceRecord.tenant_id == ctx.tenant_id,
                DemoSourceRecord.provider_connection_id == connection,
                DemoSourceRecord.kind == "customer",
                DemoSourceRecord.external_id == customer.external_customer_id,
            )
            .order_by(DemoSourceRecord.revision.desc())
        )
        assert source
        writer.add(
            DemoSourceRecord(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection,
                organisation_id=source.organisation_id,
                kind="customer",
                external_id=source.external_id,
                revision=source.revision + 1,
                payload={**source.payload, "email": destination},
                effective_at=now(),
                observed_at=now(),
            )
        )
        writer.commit()
        event = publish_invoice(writer, ctx.tenant_id, connection, external)
    apply(client.app, ctx, event)
    return key


@pytest.mark.parametrize("existing_review", [True, False])
@pytest.mark.parametrize("source_change", ["destination", "revision_only"])
def test_http_recipient_review_rejects_source_changed_after_display(
    existing_review: bool, source_change: str
) -> None:
    """Captured form A cannot attest to destination B imported before its PUT."""
    from policy_support import activate

    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved) if existing_review else activate(client, ctx, saved)
        invoice = invoices[0]
        contact = store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
        )
        assert contact
        key, invoice_id = contact.id, invoice.id
        store.rollback()
        base = f"/api/workspaces/{ctx.tenant_id}"
        displayed = next(
            r for r in client.get(base + "/policy-recipients").json() if r["contact_id"] == str(key)
        )
        assert displayed["version"] == (1 if existing_review else 0)
        # Capture the source identity from the displayed GET, before accounting changes it.
        submitted = dict(
            expected_version=displayed["version"],
            expected_contact_revision=displayed["source_revision"],
            expected_destination=displayed["destination"],
            timezone="Australia/Melbourne",
            timezone_verified=True,
            destination_verified=True,
            allowed_channels=["email"],
            evidence=f"Reviewed only displayed destination {displayed['destination']}",
        )
        destination = (
            "replacement@example.test" if source_change == "destination" else displayed["destination"]
        )
        assert import_contact_destination(client, store, ctx, invoice, destination) == key
        preview_path = base + f"/policies/{saved['policy_id']}/preview"
        before_item = next(
            i for i in client.get(preview_path).json()["items"] if i["invoice_id"] == str(invoice_id)
        )
        assert before_item["destination"] == destination
        assert "recipient_unverified" in before_item["reasons"]
        assert "OUTBOUND_EXECUTOR_UNAVAILABLE" in before_item["reasons"]
        with Session(store.get_bind()) as check:
            before = recipient_review_state(check, ctx.tenant_id)
        response = client.put(base + f"/policy-recipients/{key}", json=submitted)
        with Session(store.get_bind()) as check:
            after = recipient_review_state(check, ctx.tenant_id)
        print(
            {
                "existing_review": existing_review,
                "http_status": response.status_code,
                "changed_tables": [name for name in before if before[name] != after[name]],
            }
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "RECIPIENT_SOURCE_CHANGED"
        assert after == before
        current = next(
            r for r in client.get(base + "/policy-recipients").json() if r["contact_id"] == str(key)
        )
        assert current["source_revision"] != displayed["source_revision"]
        assert not current["review_matches_source"]
        assert current["reviewed_destination"] == (displayed["destination"] if existing_review else None)
        if existing_review:
            assert current["reviewed_contact_revision"] == displayed["source_revision"]
            assert current["evidence"] == displayed["evidence"]
        fresh = {
            **submitted,
            "expected_contact_revision": current["source_revision"],
            "expected_destination": current["destination"],
            "evidence": "Explicit current source review",
        }
        accepted = client.put(base + f"/policy-recipients/{key}", json=fresh)
        assert accepted.status_code == 200, accepted.text
        result = accepted.json()
        assert result["version"] == displayed["version"] + 1 and result["review_matches_source"]
        assert result["reviewed_contact_revision"] == current["source_revision"]
        assert result["reviewed_destination"] == current["destination"]
        # Own returned version can support another legitimate unchanged-source edit.
        accepted_again = client.put(
            base + f"/policy-recipients/{key}",
            json={**fresh, "expected_version": result["version"], "evidence": "Second current source review"},
        )
        assert accepted_again.status_code == 200
        assert accepted_again.json()["version"] == result["version"] + 1
        after_item = next(
            i for i in client.get(preview_path).json()["items"] if i["invoice_id"] == str(invoice_id)
        )
        assert "recipient_unverified" not in after_item["reasons"]
        assert "OUTBOUND_EXECUTOR_UNAVAILABLE" in after_item["reasons"]


@pytest.mark.parametrize("fallback", [True, False])
def test_http_recipient_review_rejects_changed_workspace_timezone(fallback: bool) -> None:
    with policy_world() as (client, store, ctx, _invoices, _saved):
        base = f"/api/workspaces/{ctx.tenant_id}"
        workspace = client.get(base).json()
        displayed = client.get(base + "/policy-recipients").json()[0]
        body = dict(
            expected_version=displayed["version"],
            expected_contact_revision=displayed["source_revision"],
            expected_destination=displayed["destination"],
            expected_tenant_timezone=workspace["timezone"] if fallback else None,
            timezone=None if fallback else "Australia/Melbourne",
            timezone_verified=not fallback,
            owner_confirmed_tenant_timezone=fallback,
            destination_verified=True,
            evidence=f"Reviewed tenant timezone {workspace['timezone']}",
        )
        new_zone = "Pacific/Auckland" if workspace["timezone"] != "Pacific/Auckland" else "Australia/Perth"
        patched = client.patch(base, json=dict(name=workspace["name"], timezone=new_zone))
        assert patched.status_code == 200, patched.text
        assert patched.json()["timezone"] == new_zone
        with Session(store.get_bind()) as check:
            before = recipient_review_state(check, ctx.tenant_id)
        response = client.put(base + f"/policy-recipients/{displayed['contact_id']}", json=body)
        with Session(store.get_bind()) as check:
            after = recipient_review_state(check, ctx.tenant_id)
            control = check.scalar(
                select(RecipientControl).where(RecipientControl.tenant_id == ctx.tenant_id)
            )
            print(
                dict(
                    displayed_timezone=workspace["timezone"],
                    current_timezone=new_zone,
                    status=response.status_code,
                    stored_confirmed_timezone=control.confirmed_tenant_timezone if control else None,
                    stored_evidence=control.evidence if control else None,
                    changed_tables=[name for name in before if before[name] != after[name]],
                )
            )
        if fallback:
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == "RECIPIENT_TIMEZONE_CHANGED"
            assert after == before
            current = next(
                r
                for r in client.get(base + "/policy-recipients").json()
                if r["contact_id"] == displayed["contact_id"]
            )
            assert current["tenant_timezone"] == new_zone and current["confirmed_tenant_timezone"] is None
            response = client.put(
                base + f"/policy-recipients/{displayed['contact_id']}",
                json={
                    **body,
                    "expected_tenant_timezone": current["tenant_timezone"],
                    "evidence": f"Explicitly reviewed current timezone {new_zone}",
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["version"] == displayed["version"] + 1
        assert response.json()["confirmed_tenant_timezone"] == (new_zone if fallback else None)
        assert response.json()["timezone"] == (None if fallback else "Australia/Melbourne")


def test_http_recipient_binding_validation_null_and_tenant_first() -> None:
    from aco.features.identity.models import Membership

    with policy_world() as (client, store, ctx, invoices, _saved):
        key = import_contact_destination(client, store, ctx, invoices[0], None)
        base = f"/api/workspaces/{ctx.tenant_id}"
        displayed = next(
            r for r in client.get(base + "/policy-recipients").json() if r["contact_id"] == str(key)
        )
        assert displayed["destination"] is None and displayed["version"] == 0
        body = dict(
            expected_version=0,
            expected_contact_revision=displayed["source_revision"],
            expected_destination=None,
            evidence="Synthetic null destination review",
            destination_verified=False,
            timezone="Australia/Melbourne",
            timezone_verified=True,
        )
        with Session(store.get_bind()) as check:
            before = recipient_review_state(check, ctx.tenant_id)
        for missing in ("expected_contact_revision", "expected_destination"):
            response = client.put(
                base + f"/policy-recipients/{key}", json={k: v for k, v in body.items() if k != missing}
            )
            assert response.status_code == 422
            assert any(
                e["loc"] == ["body", missing] and e["type"] == "missing" for e in response.json()["detail"]
            )
        for change, code in (
            ({"expected_contact_revision": "different-opaque-revision"}, "RECIPIENT_SOURCE_CHANGED"),
            ({"expected_destination": ""}, "RECIPIENT_SOURCE_CHANGED"),
            ({"expected_version": 3, "expected_contact_revision": "stale"}, "VERSION_CONFLICT"),
        ):
            response = client.put(base + f"/policy-recipients/{key}", json={**body, **change})
            assert response.status_code == 409 and response.json()["detail"] == code
        with Session(store.get_bind()) as check:
            assert recipient_review_state(check, ctx.tenant_id) == before
        accepted = client.put(base + f"/policy-recipients/{key}", json=body)
        assert accepted.status_code == 200 and accepted.json()["version"] == 1
        assert accepted.json()["reviewed_destination"] is None and accepted.json()["review_matches_source"]
        with Session(store.get_bind()) as writer:
            member = writer.scalar(
                select(Membership).where(
                    Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
                )
            )
            assert member
            member.role = "viewer"  # Persisted synthetic authority revocation fixture.
            writer.commit()
            before = recipient_review_state(writer, ctx.tenant_id)
        response = client.put(base + f"/policy-recipients/{key}", json={**body, "expected_version": 0})
        assert response.status_code == 403, response.text
        with Session(store.get_bind()) as check:
            assert recipient_review_state(check, ctx.tenant_id) == before
