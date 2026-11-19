"""T08 actual DB/service boundary consumers; external execution remains unavailable."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from boundary_evidence import BoundaryObserver
from fastapi import HTTPException
from policy_support import AT, activate, configure_recipients, definition, policy_world, save
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import CollectionRecord
from aco.features.identity.models import Tenant
from aco.features.policies.models import CollectionPolicyVersion

CASES = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]


def cases(domain: str) -> list[dict[str, Any]]:
    return [case for case in CASES if case["domain"] == domain]


def observe(case: dict[str, Any], values: dict[str, Any], recorder: BoundaryObserver) -> None:
    for key, value in case["expected"].items():
        if key.endswith("_max"):
            assert values[key] <= value, (case["id"], key, values)
        else:
            assert values[key] == value, (case["id"], key, values)
    recorder.assert_observed(case["id"], {key: values[key] for key in case["expected"]})


def test_owner_can_persist_policy_and_read_it_back() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        loaded = client.get(f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}")
        assert loaded.status_code == 200
        assert loaded.json()["definition"]["rules"][0]["offset_days"] == 3
        rows = list(
            store.scalars(
                select(CollectionPolicyVersion).where(
                    CollectionPolicyVersion.policy_id == UUID(saved["policy_id"])
                )
            )
        )
        assert (
            len(rows) == 1 and cast(list[dict[str, Any]], rows[0].definition["rules"])[0]["offset_days"] == 3
        )


def test_native_reminder_attestation_required() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        response = client.put(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}",
            json={
                "name": saved["name"],
                "definition": saved["definition"],
                "expected_version": 1,
                "activate": True,
                "configuration_reviewed": True,
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "NATIVE_REMINDER_ATTESTATION_REQUIRED"
        tenant = store.get(Tenant, ctx.tenant_id, populate_existing=True)
        assert tenant and not tenant.automation_enabled


@pytest.mark.parametrize("old_offset,new_offset,old_utc_days,new_utc_days", [(3, 5, 2, 6), (5, 7, 6, 6)])
def test_policy_save_recomputes_future_not_history(
    old_offset: int,
    new_offset: int,
    old_utc_days: int,
    new_utc_days: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aco.features.accounting import seed as seed_module
    from aco.features.actions.models import ScheduledAction
    from aco.features.policies import service as policy_module

    stamp = datetime(2026, 11, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(seed_module, "now", lambda: stamp)
    monkeypatch.setattr(policy_module, "now", lambda: stamp)
    with policy_world() as (client, store, ctx, invoices, saved):
        configure_recipients(client, store, ctx)
        if old_offset != 3:
            initial = json.loads(json.dumps(saved["definition"]))
            initial["rules"][0]["offset_days"] = old_offset
            saved = save(
                client,
                ctx.tenant_id,
                policy_id=saved["policy_id"],
                name=saved["name"],
                expected_version=saved["version"],
                definition=initial,
            )
        saved = activate(client, ctx, saved)
        old = list(store.scalars(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id)))
        old_values = {a.id: (a.invoice_id, a.policy_version_id, a.due_at) for a in old}
        source_dates = {i.id: i.due_date for i in invoices}
        assert len(old_values) == 10
        assert {source_dates[a.invoice_id] for a in old} == {stamp.date(), (stamp + timedelta(days=7)).date()}
        # Independent oracle: Monday anchors at Melbourne UTC+11. Thursday09:00
        # is anchor+2days22Z; Saturday and Monday both normalize to +6days22Z.
        for action in old:
            due = source_dates[action.invoice_id]
            assert due.weekday() == 0
            assert action.due_at == datetime.combine(due + timedelta(days=old_utc_days), time(22), tzinfo=UTC)
        store.rollback()
        revised = json.loads(json.dumps(saved["definition"]))
        revised["rules"][0]["offset_days"] = new_offset
        changed = save(
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
        rows = list(store.scalars(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id)))
        assert len(rows) == 20
        historical = [a for a in rows if a.id in old_values]
        future = [a for a in rows if a.id not in old_values]
        assert len(historical) == len(future) == 10
        assert {a.invoice_id for a in future} == {v[0] for v in old_values.values()}
        for action in historical:
            assert action.status == "cancelled"
            assert (action.invoice_id, action.policy_version_id, action.due_at) == old_values[action.id]
        for action in future:
            due = source_dates[action.invoice_id]
            assert action.status == "queued" and str(action.policy_version_id) == changed["id"]
            assert action.due_at == datetime.combine(due + timedelta(days=new_utc_days), time(22), tzinfo=UTC)
            previous = next(v for v in old_values.values() if v[0] == action.invoice_id)
            assert (action.due_at == previous[2]) == (old_utc_days == new_utc_days)
        history = client.get(f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/versions")
        assert history.status_code == 200
        versions = {v["id"]: v for v in history.json()}
        assert versions[saved["id"]]["definition"]["rules"][0]["offset_days"] == old_offset
        assert versions[changed["id"]]["definition"]["rules"][0]["offset_days"] == new_offset
        print(
            dict(
                clock=stamp.isoformat(),
                old_offset=old_offset,
                new_offset=new_offset,
                old_actions=len(historical),
                new_actions=len(future),
                same_due=old_utc_days == new_utc_days,
                independent_expected_utc_days=[old_utc_days, new_utc_days],
            )
        )


@pytest.mark.parametrize("case", cases("contact"), ids=lambda case: case["id"])
def test_boundary_customer_contact_caps(case: dict[str, Any], boundary_observer: BoundaryObserver) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        from aco.features.policies.guards import PolicyGuards
        from aco.features.policies.models import ContactReservation

        inputs = case["input"]
        customer = invoices[0].customer_id
        other = next(i.customer_id for i in invoices if i.customer_id != customer)
        destination = inputs.get("normalized_destination", "recipient@example.test")
        if "concurrent_proposals" in inputs:
            gate = Barrier(2)
            ids = [i.id for i in invoices if i.customer_id == customer][:2]
            store.rollback()

            def reserve(invoice_id: UUID) -> bool:
                with Session(store.get_bind()) as session:
                    gate.wait()
                    try:
                        PolicyGuards(session, AT).reserve_contact(
                            ctx,
                            invoice_id,
                            destination,
                            "email" if invoice_id == ids[0] else "voice",
                            uuid4(),
                        )
                        session.commit()
                        return True
                    except HTTPException as exc:
                        session.rollback()
                        assert exc.detail in {"customer_contact_cap", "recipient_contact_cap"}
                        return False

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(reserve, ids))
            reserved = list(
                store.scalars(select(ContactReservation).where(ContactReservation.tenant_id == ctx.tenant_id))
            )
            # T08 has no submission implementation: independently observed database ledger is empty.
            assert all(row.submitted_at is None for row in reserved)
            observe(
                case,
                {
                    "accepted_reservations": sum(outcomes),
                    "rejected_reservations": outcomes.count(False),
                    "provider_submissions_max": sum(row.submitted_at is not None for row in reserved),
                },
                boundary_observer,
            )
            return
        for entry in inputs["attempts"]:
            at = __import__("datetime").datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
            historical_customer = other if entry.get("customer") == "C1" else customer
            historical_invoice = next(i for i in invoices if i.customer_id == historical_customer)
            store.add(
                ContactReservation(
                    tenant_id=ctx.tenant_id,
                    invoice_id=historical_invoice.id,
                    customer_id=historical_customer,
                    destination=destination,
                    channel=entry["channel"],
                    operation_id=uuid4(),
                    status=entry["outcome"],
                    reserved_at=at,
                    submitted_at=at,
                    evidence={"origin": "synthetic historical fixture"},
                )
            )
        store.commit()
        result = PolicyGuards(store, AT).contact_status(ctx, customer, destination)
        observe(case, result.model_dump(), boundary_observer)


def test_customer_caps_are_atomic_across_invoices_channels() -> None:
    # Shared destination across different customer records must serialize too.
    with policy_world() as (client, store, ctx, invoices, saved):
        from aco.features.policies.guards import PolicyGuards

        first = invoices[0]
        second = next(i for i in invoices if i.customer_id != first.customer_id)
        barrier = Barrier(2)
        store.rollback()

        def reserve(args: tuple[UUID, str]) -> bool:
            with Session(store.get_bind()) as session:
                barrier.wait()
                try:
                    PolicyGuards(session, AT).reserve_contact(ctx, args[0], args[1], "email", uuid4())
                    session.commit()
                    return True
                except HTTPException as exc:
                    assert exc.detail == "recipient_contact_cap"
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    reserve, [(first.id, " Recipient@EXAMPLE.test "), (second.id, "recipient@example.test")]
                )
            )
        assert sorted(outcomes) == [False, True]


@pytest.mark.parametrize("case", cases("freshness"), ids=lambda case: case["id"])
def test_boundary_source_reverification(case: dict[str, Any], boundary_observer: BoundaryObserver) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        from aco.core.clock import FrozenClock
        from aco.core.types import ProviderErrorCode, ProviderFailure
        from aco.features.policies.guards import PolicyGuards, read_dispatch_source
        from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
        from aco.integrations.ports.accounting import AccountingFailure

        data = case["input"]
        from aco.features.identity.models import now

        read_at = now() + timedelta(seconds=1000)
        invoice = invoices[0]
        attempt_id = uuid4()
        incarnation = uuid4()
        store.rollback()

        class Source(DemoAccountingAdapter):
            async def get_invoice(self, context: Any, key: str) -> Any:
                if not data["source_read_succeeded"]:
                    raise AccountingFailure(
                        ProviderFailure(
                            code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                            retryable=True,
                            may_have_side_effect=False,
                        )
                    )
                result = await super().get_invoice(context, key)
                observed = read_at - timedelta(seconds=data["source_age_seconds"])
                value = result.value.model_copy(
                    update={"stamp": result.value.stamp.model_copy(update={"observed_at": observed})}
                )
                return result.model_copy(update={"observed_at": observed, "value": value})

            async def list_payments(self, context: Any, key: str) -> Any:
                result = await super().list_payments(context, key)
                return result.model_copy(update={"observed_at": read_at})

        async def read() -> UUID:
            async with client.app.state.business_sessions.begin() as db:
                return await read_dispatch_source(
                    db,
                    ctx,
                    invoice.id,
                    attempt_id,
                    incarnation,
                    Source(db, invoice.provider_connection_id),
                    clock=FrozenClock(read_at),
                )

        read_id = asyncio.run(read())
        result = PolicyGuards(store, read_at, incarnation).source_status(
            ctx, invoice.id, attempt_id if data["verified_in_current_dispatch_attempt"] else uuid4(), read_id
        )
        observe(case, result.model_dump(), boundary_observer)


@pytest.mark.parametrize("case", cases("policy_archive"), ids=lambda case: case["id"])
def test_boundary_policy_archive(case: dict[str, Any], boundary_observer: BoundaryObserver) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        from aco.features.actions.models import ScheduledAction

        configure_recipients(client, store, ctx)
        saved = activate(client, ctx, saved)
        old_version_ids = set(
            store.scalars(
                select(CollectionPolicyVersion.id).where(CollectionPolicyVersion.tenant_id == ctx.tenant_id)
            )
        )
        old_actions = list(
            store.scalars(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        )
        assert old_actions
        historical = old_actions[0]
        historical.status = "succeeded"
        historical.result = {"synthetic_fixture": "historical no-contact eligibility"}
        store.commit()
        original = (historical.id, historical.policy_version_id, historical.result, historical.due_at)
        replacement = None
        if case["input"]["replacement_validated"]:
            replacement = save(client, ctx.tenant_id, name="Replacement", definition=definition())
            for _ in range(3):
                replacement = save(
                    client,
                    ctx.tenant_id,
                    policy_id=replacement["policy_id"],
                    name=replacement["name"],
                    definition=replacement["definition"],
                    expected_version=replacement["version"],
                )
            assert replacement["version"] == 4
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/archive",
            json={
                "expected_version": saved["version"],
                "suspend_automation": case["input"]["suspend_automation"],
                "replacement_policy_id": replacement["policy_id"] if replacement else None,
                "replacement_expected_version": replacement["version"] if replacement else None,
                "native_reminders_disabled": True,
                "configuration_reviewed": True,
            },
        )
        assert response.status_code == (200 if case["expected"]["archived"] else 409), response.text
        current = set(
            store.scalars(
                select(CollectionPolicyVersion.id).where(CollectionPolicyVersion.tenant_id == ctx.tenant_id)
            )
        )
        values = {
            "archived": response.status_code == 200,
            "historical_versions_deleted": len(old_version_ids - current),
        }
        tenant = store.get(Tenant, ctx.tenant_id, populate_existing=True)
        assert tenant
        rows = list(
            store.scalars(
                select(ScheduledAction)
                .where(ScheduledAction.tenant_id == ctx.tenant_id)
                .execution_options(populate_existing=True)
            )
        )
        if case["input"]["suspend_automation"]:
            values.update(
                automation_enabled=tenant.automation_enabled,
                unsent_actions="cancelled"
                if all(a.status == "cancelled" for a in rows if a.id != historical.id)
                else "not_cancelled",
            )
        if replacement:
            future = [a for a in rows if a.status == "queued"]
            assert future
            versions = {
                v.version
                for a in future
                if (v := store.get(CollectionPolicyVersion, a.policy_version_id)) is not None
            }
            values.update(
                future_policy_version=next(iter(versions)) if len(versions) == 1 else None,
                future_actions="recomputed"
                if all(a.id not in {o.id for o in old_actions} for a in future)
                else "old",
                historical_sends="unchanged"
                if (historical.id, historical.policy_version_id, historical.result, historical.due_at)
                == original
                else "changed",
            )
        observe(case, values, boundary_observer)


def test_archiving_policy_preserves_history_and_recomputes_actions() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/archive",
            json={"expected_version": saved["version"]},
        )
        assert response.status_code == 200, response.text
        history = client.get(f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/versions")
        assert history.status_code == 200 and len(history.json()) == 1
        assert history.json()[0]["id"] == saved["id"]


@pytest.mark.parametrize("case", cases("holds"), ids=lambda case: case["id"])
def test_boundary_composed_hold_release(case: dict[str, Any], boundary_observer: BoundaryObserver) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        from aco.features.policies.guards import PolicyGuards
        from aco.features.policies.service import PolicyService

        service = PolicyService(store, AT)
        invoice = invoices[0]
        for reason in case["input"]["hold_reasons"]:
            service.place_hold(
                ctx,
                invoice.id,
                reason,
                "Synthetic explicit manual evidence",
                expires_at=AT if reason in case["input"]["expired_reasons"] else None,
            )
        store.commit()
        service.release_expired_holds(ctx, invoice.id, AT)
        store.commit()
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice.id))
        assert collection
        decision = PolicyGuards(store, AT).operational_status(ctx, invoice.id)
        observe(
            case,
            {
                "hold_reasons": collection.hold_reasons,
                "outbound_allowed": decision.allowed,
                "backlog_sends": 0 if not list(service.submitted_contacts(ctx, invoice.id)) else 1,
            },
            boundary_observer,
        )


def test_hold_reasons_are_composed() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        from aco.features.accounting.ingest import AccountingService
        from aco.features.policies.service import PolicyService
        from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

        service = PolicyService(store)
        invoice = invoices[0]
        service.place_hold(ctx, invoice.id, "active_dispute", "Synthetic documented dispute")
        service.place_hold(ctx, invoice.id, "active_promise", "Synthetic documented commitment")
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice.id))
        assert collection
        collection.hold_reasons = [*collection.hold_reasons, "stale_accounting"]
        store.commit()

        async def read() -> None:
            async with client.app.state.business_sessions.begin() as db:
                await AccountingService(
                    db, DemoAccountingAdapter(db, invoice.provider_connection_id)
                ).recheck_invoice(ctx, invoice.external_invoice_id)

        asyncio.run(read())
        collection = store.scalar(
            select(CollectionRecord)
            .where(CollectionRecord.invoice_id == invoice.id)
            .execution_options(populate_existing=True)
        )
        assert collection
        assert set(collection.hold_reasons) == {"active_dispute", "active_promise"}


@pytest.mark.parametrize("legacy", [False, True])
def test_policy_history_retains_name_and_configuration_after_rename(legacy: bool) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        original = saved
        if legacy:
            rows = client.get(f"/api/workspaces/{ctx.tenant_id}/policies").json()
            original = next(row for row in rows if row["policy_id"] != saved["policy_id"])
        save(
            client,
            ctx.tenant_id,
            policy_id=original["policy_id"],
            name="Renamed current policy",
            expected_version=original["version"],
            definition=saved["definition"],
        )
        response = client.get(f"/api/workspaces/{ctx.tenant_id}/policies/{original['policy_id']}/versions")
        assert response.status_code == 200
        assert [v["name"] for v in response.json()] == [original["name"], "Renamed current policy"]
        assert response.json()[0]["definition"] == original["definition"]


def test_voice_conditions_are_independent_of_missing_executor() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        rules = [{**saved["definition"]["rules"][0], "channel": "voice"}]
        changed = save(
            client,
            ctx.tenant_id,
            policy_id=saved["policy_id"],
            name=saved["name"],
            expected_version=saved["version"],
            definition={
                **saved["definition"],
                "rules": rules,
                "voice_enabled": False,
                "voice_after_days": 365,
            },
        )
        response = client.get(f"/api/workspaces/{ctx.tenant_id}/policies/{changed['policy_id']}/preview")
        assert response.status_code == 200
        for row in response.json()["items"]:
            assert "voice_disabled" in row["reasons"]
            assert "voice_overdue_threshold" in row["reasons"]
            assert "voice_permission_unavailable" in row["reasons"]


@pytest.mark.parametrize("proof", ["missing", "stale"])
def test_archive_replacement_requires_exact_reviewed_version_atomically(proof: str) -> None:
    from integration.test_policy_authority import setup_active

    from aco.features.actions.models import ActionExecution, OutboxEvent, ScheduledAction
    from aco.features.identity.models import AuditEvent
    from aco.features.policies.models import CollectionPolicy, PolicyActivation, PolicyEvent, PolicyOccurrence

    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved)
        replacement = save(client, ctx.tenant_id, name="Reviewed replacement", definition=saved["definition"])
        reviewed_version = replacement["version"]
        # A separate committed edit occurs after the archive caller reviewed version1.
        revised = {
            **replacement["definition"],
            "rules": [{**replacement["definition"]["rules"][0], "offset_days": 121}],
        }
        replacement = save(
            client,
            ctx.tenant_id,
            policy_id=replacement["policy_id"],
            name=replacement["name"],
            expected_version=reviewed_version,
            definition=revised,
        )
        assert replacement["version"] == reviewed_version + 1

        def snapshot() -> str:
            result = {}
            for model in (
                CollectionPolicy,
                CollectionPolicyVersion,
                PolicyActivation,
                PolicyEvent,
                PolicyOccurrence,
                ScheduledAction,
                OutboxEvent,
                ActionExecution,
                AuditEvent,
            ):
                table = model.__table__
                result[model.__tablename__] = [
                    dict(row)
                    for row in store.execute(
                        select(table).where(table.c.tenant_id == ctx.tenant_id).order_by(table.c.id)
                    ).mappings()
                ]
            result["tenant"] = [
                dict(row)
                for row in store.execute(
                    select(Tenant.__table__).where(Tenant.id == ctx.tenant_id)
                ).mappings()
            ]
            store.rollback()
            return json.dumps(result, default=str, sort_keys=True)

        before = snapshot()
        body = {
            "expected_version": saved["version"],
            "replacement_policy_id": replacement["policy_id"],
            "native_reminders_disabled": True,
            "configuration_reviewed": True,
        }
        if proof == "stale":
            body["replacement_expected_version"] = reviewed_version
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/archive", json=body
        )
        after = snapshot()
        print(
            {
                "replacement_proof": proof,
                "reviewed_version": reviewed_version,
                "current_version": replacement["version"],
                "status": response.status_code,
                "detail": response.json(),
                "rows_unchanged": before == after,
            }
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == (
            "REPLACEMENT_VERSION_REQUIRED" if proof == "missing" else "REPLACEMENT_VERSION_CONFLICT"
        )
        assert before == after
