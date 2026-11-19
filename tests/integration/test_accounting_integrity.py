"""T04: real migrated PostgreSQL and authenticated API; all source data synthetic."""

from typing import Any, cast
from uuid import uuid4

from accounting_support import seed
from alembic.script import ScriptDirectory
from boundary_evidence import BoundaryObserver
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy.orm import Session

from aco.core.context import ActorContext


def test_demo_payment_reuses_canonical_handler() -> None:
    # Missing real source mutation/import, event, or idempotency must break this test.
    with actor_client("owner") as (client, store, user, tenant):
        seeded = seed(client, tenant)
        command = {
            "provider_connection_id": seeded["provider_connection_id"],
            "external_invoice_id": "INV-101",
            "amount": {"minor_units": 10000, "currency": "AUD", "exponent": 2},
            "operation_id": str(uuid4()),
        }
        response = client.post(f"/api/workspaces/{tenant}/demo/accounting/simulate-payment", json=command)
        assert response.status_code == 200, response.text
        assert response.json()["financial_status"] == "paid"
        assert response.json()["collection_lifecycle"] == "closed"
        assert response.json()["source_revision"] == "2"
        assert response.json()["synthetic"] is True
        replay = client.post(f"/api/workspaces/{tenant}/demo/accounting/simulate-payment", json=command)
        assert replay.json() == response.json()


def test_financial_fields_have_no_user_or_tool_write_surface() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        # T05 adds GET-only receivables; its write denial is now exact405, with no write schema.
        for path, denied_status in (("invoices", 404), ("payments", 404), ("receivables", 405)):
            assert (
                client.post(f"/api/workspaces/{tenant}/{path}", json={"status": "paid"}).status_code
                == denied_status
            )
        financial_write_routes = [
            path
            for path, methods in cast(FastAPI, client.app).openapi()["paths"].items()
            if any(part in path for part in ("/invoices", "/payments", "/receivables"))
            and set(methods) & {"post", "patch", "put", "delete"}
        ]
        assert financial_write_routes == []


def test_boundary_connection_scoped_payment_identity(boundary_observer: BoundaryObserver) -> None:
    import asyncio
    import json
    from pathlib import Path
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    cases = [
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["domain"] == "identity"
    ]
    assert {c["id"] for c in cases} == {"source_same_payment_id_different_connection"}
    for case in cases:
        with actor_client("owner") as (client, store, user, tenant):
            seeded = seed(client, tenant)  # RED: no source/producer exists yet.
            from aco.core.context import TenantContext
            from aco.core.types import Role
            from aco.features.accounting.ingest import AccountingService
            from aco.features.accounting.models import (
                DemoSourceRecord,
                InvoiceSnapshot,
                VerifiedPaymentAllocation,
            )
            from aco.features.accounting.seed import seed_connection
            from aco.features.identity.models import ProviderConnection
            from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

            conn_ids = {"C1": UUID(seeded["provider_connection_id"]), "C2": uuid4()}
            store.add(
                ProviderConnection(
                    id=conn_ids["C2"],
                    tenant_id=tenant,
                    provider_type="accounting",
                    provider="demo",
                    status="NOT_CONFIGURED",
                )
            )
            store.commit()
            ctx = TenantContext(tenant_id=tenant, actor_id=user.id, role=Role.OWNER, correlation_id=uuid4())

            async def consume() -> None:
                async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                    await seed_connection(db, ctx, conn_ids["C2"], "local", variant="B")  # noqa: B023 - awaited immediately in this iteration
                    for item in case["input"]["payments"]:  # noqa: B023 - awaited immediately in this iteration
                        conn = conn_ids[item["connection"]]  # noqa: B023 - awaited immediately in this iteration
                        adapter = DemoAccountingAdapter(db, conn)
                        invoice = (await adapter.get_invoice(ctx, "INV-101")).value  # noqa: B023 - awaited immediately in this iteration
                        payload = {
                            "external_payment_id": item["external_payment"],
                            "external_allocation_id": item["allocation"],
                            "external_invoice_id": "INV-101",
                            "kind": "cash",
                            "amount": {"minor_units": 100, "currency": "AUD", "exponent": 2},
                            "parent_payment_id": None,
                            "parent_allocation_id": None,
                        }
                        db.add(
                            DemoSourceRecord(
                                tenant_id=tenant,
                                provider_connection_id=conn,
                                organisation_id=invoice.organisation_id,
                                kind="payment",
                                external_id="P1/A1",
                                revision=1,
                                payload=payload,
                                effective_at=invoice.stamp.source_at,
                                observed_at=invoice.stamp.observed_at,
                            )
                        )
                        await db.flush()
                        await AccountingService(db, adapter).sync_accounting(ctx, None)  # noqa: B023 - awaited immediately in this iteration

            asyncio.run(consume())
            store.expire_all()
            allocations = list(
                store.scalars(
                    select(VerifiedPaymentAllocation).where(
                        VerifiedPaymentAllocation.tenant_id == tenant,
                        VerifiedPaymentAllocation.external_payment_id == "P1",
                    )
                )
            )
            assert len(allocations) == case["expected"]["distinct_allocation_count"]
            assert {r.external_allocation_id for r in allocations} == {"A1"}
            invoices = list(
                store.scalars(
                    select(InvoiceSnapshot).where(
                        InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.external_invoice_id == "INV-101"
                    )
                )
            )
            by_conn = {r.provider_connection_id: r.id for r in invoices}
            bad = VerifiedPaymentAllocation(
                tenant_id=tenant,
                provider_connection_id=conn_ids["C1"],
                invoice_id=by_conn[conn_ids["C2"]],
                external_payment_id="illegal",
                external_allocation_id="A2",
                kind="cash",
                minor_units=100,
                currency="AUD",
                exponent=2,
                source_revision="1",
                effective_at=allocations[0].effective_at,
                observed_at=allocations[0].observed_at,
                organisation_id=allocations[0].organisation_id,
                synthetic=True,
            )
            cross_link_allowed = True
            try:
                with store.begin_nested():
                    store.add(bad)
                    store.flush()
            except IntegrityError:
                cross_link_allowed = False
            assert cross_link_allowed is case["expected"]["cross_link_allowed"]
            boundary_observer.assert_observed(
                case["id"],
                {"distinct_allocation_count": len(allocations), "cross_link_allowed": cross_link_allowed},
            )
            import pytest

            from aco.features.accounting.models import AccountingEvent

            with pytest.raises(IntegrityError), store.begin_nested():
                store.add(
                    AccountingEvent(
                        tenant_id=tenant,
                        provider_connection_id=conn_ids["C1"],
                        invoice_id=by_conn[conn_ids["C2"]],
                        source_revision="1",
                        origin="AUTHORITATIVE_READ",
                        external_event_id=None,
                        envelope={},
                        provenance={},
                        result={},
                    )
                )
                store.flush()
            store.rollback()


def test_source_events_persist_read_provenance_and_unknown_cost() -> None:
    from sqlalchemy import select

    from aco.features.accounting.models import AccountingEvent

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        event = store.scalar(select(AccountingEvent).where(AccountingEvent.tenant_id == tenant))
        assert event is not None
        provenance = cast(dict[str, Any], event.provenance)
        assert provenance["invoice"]["provider"] == "demo"
        assert provenance["invoice"]["native_operation_id"]
        assert provenance["invoice"]["authenticity"] == "VERIFIED"
        assert provenance["payments"]["cost"]["quality"] == "UNKNOWN"
        assert provenance["payments"]["cost"]["amount"] is None


def test_snapshot_correction_preserves_history() -> None:
    from uuid import UUID

    import pytest
    from accounting_support import apply, context, publish_invoice
    from sqlalchemy import select, update
    from sqlalchemy.exc import DBAPIError

    from aco.features.accounting.models import DemoSourceRecord, InvoiceSnapshot, InvoiceSourceRevision

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        event = publish_invoice(store, tenant, connection, due_date="2026-12-01", number="CORRECTED-101")
        result = apply(cast(FastAPI, client.app), context(tenant, user.id), event)
        store.expire_all()
        invoice = store.get(InvoiceSnapshot, result.invoice_id)
        assert invoice is not None and str(invoice.due_date) == "2026-12-01"
        versions = list(
            store.scalars(select(InvoiceSourceRevision).where(InvoiceSourceRevision.invoice_id == invoice.id))
        )
        assert len(versions) == 2
        assert {v.payload["number"] for v in versions} == {"INV-101", "CORRECTED-101"}
        for table in (InvoiceSourceRevision, DemoSourceRecord):
            with pytest.raises(DBAPIError), store.begin_nested():
                store.execute(update(table).values(version=99))
        store.rollback()


def test_unknown_stale_future_paid_cannot_close_collection() -> None:
    from datetime import timedelta

    from accounting_support import context, document_apply, document_source
    from sqlalchemy import select

    from aco.features.accounting.models import CollectionRecord, InvoiceSnapshot
    from aco.features.identity.models import now

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        first = document_apply(client.app, context(tenant, user.id), source)
        trusted_observed = source.observed_at
        for index, (delta, verified) in enumerate(((-901, True), (1, True), (0, False))):
            source.documents[0].update(etag=f"unverified-{index}", state="PAID", unpaid=0)
            source.observed_at, source.verified = now() + timedelta(seconds=delta), verified
            source.payment_observed_at = now()  # Independent receipt fetch remains verifiable.
            result = document_apply(client.app, context(tenant, user.id), source)
            assert result.financial_status == "unknown" and result.collection_lifecycle == "active"
            store.expire_all()
            record = store.scalar(
                select(CollectionRecord).where(CollectionRecord.invoice_id == result.invoice_id)
            )
            assert record is not None and "stale_accounting" in record.hold_reasons
            snapshot = store.get(InvoiceSnapshot, first.invoice_id)
            assert snapshot is not None and snapshot.observed_at == trusted_observed
            assert snapshot.balance_minor == 7500 and snapshot.source_revision == "v1"


def test_demo_payment_independent_guards_and_rollback() -> None:
    from uuid import UUID

    from sqlalchemy import func, select

    from aco.features.accounting.models import DemoSourceRecord
    from aco.features.identity.models import Membership, Tenant

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        command = {
            "provider_connection_id": str(connection),
            "external_invoice_id": "INV-101",
            "amount": {"minor_units": 100, "currency": "AUD", "exponent": 2},
            "operation_id": str(uuid4()),
        }
        count = store.scalar(select(func.count()).select_from(DemoSourceRecord))
        path = f"/api/workspaces/{tenant}/demo/accounting/simulate-payment"
        settings = cast(FastAPI, client.app).state.settings
        for env in ("production",):
            cast(FastAPI, client.app).state.settings = settings.model_copy(update={"app_env": env})
            assert client.post(path, json=command).status_code in (403, 404)
        cast(FastAPI, client.app).state.settings = settings
        tenant_row = store.get(Tenant, tenant)
        assert tenant_row is not None
        tenant_row.is_demo = False
        store.commit()
        assert client.post(path, json=command).status_code == 403
        tenant_row.is_demo = True
        member = store.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert member is not None
        for role in ("operator", "viewer"):
            member.role = role
            store.commit()
            assert client.post(path, json=command).status_code == 403
        member.role = "owner"
        store.commit()
        csrf = client.headers.pop("x-csrf-token")
        assert client.post(path, json=command).status_code == 403
        client.headers["x-csrf-token"] = csrf
        client.headers["origin"] = "https://foreign.invalid"
        assert client.post(path, json=command).status_code == 403
        client.headers["origin"] = "http://127.0.0.1:53000"
        command["amount"] = {"minor_units": 100, "currency": "USD", "exponent": 2}
        assert client.post(path, json=command).status_code == 422
        assert store.scalar(select(func.count()).select_from(DemoSourceRecord)) == count


def test_currency_zero_negative_credit_contract_through_source_import() -> None:
    import json
    from pathlib import Path
    from uuid import UUID

    import pytest
    from accounting_support import apply, context, publish_invoice
    from sqlalchemy import select

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.integrations.ports.accounting import AccountingFailure

    cases = json.loads(Path("tests/fixtures/accounting/source_events.json").read_text())["cases"]
    for case in cases:
        with actor_client("owner") as (client, store, user, tenant):
            connection = UUID(seed(client, tenant)["provider_connection_id"])
            money = {"minor_units": case["minor"], "currency": case["currency"], "exponent": case["exponent"]}
            event = publish_invoice(
                store,
                tenant,
                connection,
                original=money,
                outstanding=money,
                status=case["status"],
                source_status=case["status"],
            )
            if case["accepted"]:
                result = apply(cast(FastAPI, client.app), context(tenant, user.id), event)
                store.expire_all()
                row = store.get(InvoiceSnapshot, result.invoice_id)
                assert row is not None
                assert (row.balance_minor, row.currency, row.exponent, row.financial_status) == (
                    case["minor"],
                    case["currency"],
                    case["exponent"],
                    case["status"],
                )
            else:
                with pytest.raises(AccountingFailure):
                    apply(cast(FastAPI, client.app), context(tenant, user.id), event)
                row = store.scalar(
                    select(InvoiceSnapshot).where(
                        InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.external_invoice_id == "INV-101"
                    )
                )
                assert row is not None and row.balance_minor == 10000 and row.source_revision == "1"


def test_verified_batch_routes_each_event() -> None:
    import asyncio
    from uuid import UUID

    import pytest
    from accounting_support import context, publish_invoice
    from fastapi import HTTPException
    from sqlalchemy import func, select

    from aco.features.accounting.ingest import reconcile_batch
    from aco.features.accounting.models import AccountingEvent, InvoiceSnapshot
    from aco.features.accounting.seed import seed_connection
    from aco.features.identity.models import LoginSession, Membership, ProviderConnection, Tenant, now
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
    from aco.integrations.ports.accounting import AccountingFailure
    from aco.integrations.ports.identity import AuthenticatedSubject

    with actor_client("owner") as (client, store, user, tenant):
        a = UUID(seed(client, tenant)["provider_connection_id"])
        btenant = Tenant(name="Synthetic B", timezone="UTC")
        store.add(btenant)
        store.flush()
        membership = Membership(tenant_id=btenant.id, user_id=user.id, role="owner")
        b = ProviderConnection(tenant_id=btenant.id, provider_type="accounting", provider="demo")
        store.add_all([membership, b])
        store.commit()

        async def seed_b() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                await seed_connection(db, context(btenant.id, user.id), b.id, "local", variant="B")

        asyncio.run(seed_b())
        session = store.scalar(select(LoginSession).where(LoginSession.user_id == user.id))
        assert session is not None
        subject = AuthenticatedSubject(user.id, user.issuer, user.subject, session.id, now(), uuid4())

        async def run(events: Any) -> Any:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                return await reconcile_batch(db, subject, events, DemoAccountingAdapter)

        events = [
            publish_invoice(store, tenant, a, number="A-corrected"),
            publish_invoice(store, btenant.id, b.id, number="B-corrected"),
        ]
        results = asyncio.run(run(events))
        assert len(results) == 2 and results[0].invoice_id != results[1].invoice_id
        store.expire_all()
        rows = [store.get(InvoiceSnapshot, r.invoice_id) for r in results]
        assert all(r is not None for r in rows)
        assert [r.number for r in rows if r is not None] == [
            "A-corrected",
            "B-corrected",
        ]
        for rejected in ("revoked", "nonmember", "wrong_organisation"):
            first = publish_invoice(store, tenant, a, number="must-rollback")
            second = publish_invoice(store, btenant.id, b.id, number="must-rollback-B")
            if rejected == "revoked":
                membership.active = False
            if rejected == "nonmember":
                other_id = uuid4()
            if rejected == "nonmember":
                # A real different actor, not a dangling FK.
                from aco.features.identity.models import User

                other = User(
                    id=other_id, issuer="synthetic", subject=str(uuid4()), email="other@example.test"
                )
                store.add(other)
                store.flush()
                membership.user_id = other_id
            if rejected == "wrong_organisation":
                second = second.model_copy(update={"organisation_id": "foreign"})
            store.commit()
            before = store.scalar(select(func.count()).select_from(AccountingEvent))
            with pytest.raises((HTTPException, AccountingFailure)):
                asyncio.run(run([first, second]))
            store.expire_all()
            first_row = store.get(InvoiceSnapshot, results[0].invoice_id)
            assert first_row is not None and first_row.number == "A-corrected"
            assert store.scalar(select(func.count()).select_from(AccountingEvent)) == before
            membership.active = True
            membership.user_id = user.id
            store.commit()


def test_verified_reversal_preserves_cash_fact_and_reopens_collection() -> None:
    from uuid import UUID

    import pytest
    from accounting_support import apply, context, publish_invoice
    from sqlalchemy import select, update
    from sqlalchemy.exc import DBAPIError

    from aco.features.accounting.models import DemoSourceRecord, VerifiedPaymentAllocation
    from aco.features.identity.models import now

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        operation = uuid4()
        response = client.post(
            f"/api/workspaces/{tenant}/demo/accounting/simulate-payment",
            json={
                "provider_connection_id": str(connection),
                "external_invoice_id": "INV-101",
                "amount": {"minor_units": 10000, "currency": "AUD", "exponent": 2},
                "operation_id": str(operation),
            },
        )
        assert response.status_code == 200 and response.json()["collection_lifecycle"] == "closed"
        event = publish_invoice(
            store,
            tenant,
            connection,
            status="open",
            source_status="REOPENED",
            outstanding={"minor_units": 10000, "currency": "AUD", "exponent": 2},
        )
        stamp = now()
        store.add(
            DemoSourceRecord(
                tenant_id=tenant,
                provider_connection_id=connection,
                organisation_id=event.organisation_id,
                kind="payment",
                external_id="REV-1/1",
                revision=1,
                effective_at=stamp,
                observed_at=stamp,
                payload={
                    "external_payment_id": "REV-1",
                    "external_allocation_id": "1",
                    "external_invoice_id": "INV-101",
                    "kind": "reversal",
                    "amount": {"minor_units": -10000, "currency": "AUD", "exponent": 2},
                    "parent_payment_id": str(operation),
                    "parent_allocation_id": "1",
                },
            )
        )
        store.commit()
        result = apply(client.app, context(tenant, user.id), event)
        assert result.financial_status == "open" and result.collection_lifecycle == "active"
        rows = list(
            store.scalars(
                select(VerifiedPaymentAllocation).where(
                    VerifiedPaymentAllocation.invoice_id == result.invoice_id
                )
            )
        )
        assert sorted(r.minor_units for r in rows) == [-10000, 10000]
        original = next(r for r in rows if r.kind == "cash")
        reversal = next(r for r in rows if r.kind == "reversal")
        assert reversal.parent_id == original.id
        with pytest.raises(DBAPIError), store.begin_nested():
            store.execute(
                update(VerifiedPaymentAllocation)
                .where(VerifiedPaymentAllocation.id == original.id)
                .values(minor_units=0)
            )
        store.rollback()


def test_connection_scoped_invoice_and_payment_ids() -> None:
    from uuid import UUID

    import pytest
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
    from aco.features.identity.models import Tenant

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        invoice = store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant))
        assert invoice is not None
        other = Tenant(name="Foreign", timezone="UTC")
        store.add(other)
        store.commit()
        for target_tenant, customer in ((other.id, invoice.customer_id), (tenant, uuid4())):
            with pytest.raises(IntegrityError), store.begin_nested():
                store.add(
                    CustomerContact(
                        tenant_id=target_tenant,
                        provider_connection_id=connection,
                        customer_id=customer,
                        email="bad@example.test",
                        source_revision="1",
                    )
                )
                store.flush()
        store.rollback()


def test_financial_migration_rollback_preserves_identity_and_reupgrades() -> None:
    from alembic.config import Config
    from sqlalchemy import inspect

    from aco.features.identity.models import User
    from alembic import command

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        user_id = user.id
        store.rollback()
        engine = store.get_bind()
        from sqlalchemy import Engine

        assert isinstance(engine, Engine)
        config = Config("alembic.ini")
        config.set_main_option(
            "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
        )
        from legacy_usage_support import legacy_snapshot

        with legacy_snapshot(store) as (legacy, legacy_config):
            legacy_engine = legacy.get_bind()
            command.downgrade(legacy_config, "r0002a_identity_cost")
            assert "invoice_snapshots" not in inspect(legacy_engine).get_table_names()
            assert legacy.get(User, user_id) is not None
            legacy.rollback()
            command.upgrade(legacy_config, "head")
            assert "invoice_snapshots" in inspect(legacy_engine).get_table_names()
        # At the actual current head the newest non-destructive restore guard
        # must refuse first, before any older identity/financial/usage DDL runs.
        import pytest
        from sqlalchemy import text

        tables = (
            "identity_users",
            "tenants",
            "memberships",
            "identity_provider_events",
            "invoice_snapshots",
            "accounting_events",
            "accounting_observations",
            "verified_payment_allocations",
            "provider_usage_events",
            "usage_revisions",
            "usage_sources",
        )
        before = _migration_retained_rows(store, tables)
        assert before["identity_users"] and before["invoice_snapshots"] and before["provider_usage_events"]
        revision = store.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == ScriptDirectory.from_config(config).get_current_head()
        store.rollback()
        with pytest.raises(
            RuntimeError,
            match="Retained Voice grant, request and join authority requires non-destructive restore",
        ):
            command.downgrade(config, "r0002a_identity_cost")
        assert store.scalar(text("SELECT version_num FROM alembic_version")) == revision
        assert _migration_retained_rows(store, tables) == before


def _migration_retained_rows(store: Session, tables: tuple[str, ...]) -> dict[str, list[str]]:
    from sqlalchemy import text

    result: dict[str, list[str]] = {}
    for table in tables:
        rows = list(store.scalars(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id")))
        assert all(isinstance(row, str) for row in rows)
        result[table] = rows
    return result


def test_historical_usage_revision_refuses_identity_classification_loss_without_row_changes() -> None:
    import pytest
    from legacy_usage_support import legacy_snapshot
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from alembic import command

    with actor_client("owner") as (client, store, _user, tenant):
        seed(client, tenant)
        # Reapply the actual current fixture's retained classification at the
        # exact historical migration which introduced that column. Do not drop,
        # stamp, downgrade or disable any guard in the current-head schema.
        classified = list(
            store.execute(
                text(
                    "SELECT id, provider_attempted FROM identity_provider_events "
                    "WHERE provider_attempted IS NOT NULL ORDER BY id"
                )
            )
        )
        assert classified
        store.rollback()
        with legacy_snapshot(store) as (legacy, config):
            command.upgrade(config, "r0003a_usage_invariants")
            for key, attempted in classified:
                changed = legacy.execute(
                    text(
                        "UPDATE identity_provider_events SET provider_attempted=:attempted "
                        "WHERE id=:id RETURNING id"
                    ),
                    {"id": key, "attempted": attempted},
                )
                assert changed.scalar_one() == key
            legacy.commit()
            tables = (
                "identity_users",
                "identity_provider_events",
                "invoice_snapshots",
                "accounting_events",
                "accounting_observations",
                "provider_usage_events",
                "usage_revisions",
                "usage_sources",
            )
            before = _migration_retained_rows(legacy, tables)
            assert legacy.scalar(text("SELECT version_num FROM alembic_version")) == "r0003a_usage_invariants"
            legacy.rollback()
            with pytest.raises(DBAPIError, match="identity activity classification would be lost"):
                command.downgrade(config, "r0002a_identity_cost")
            assert legacy.scalar(text("SELECT version_num FROM alembic_version")) == "r0003a_usage_invariants"
            assert _migration_retained_rows(legacy, tables) == before


def test_two_tenant_seed_persists_distinct_inactive_policies_and_roles() -> None:
    import asyncio

    from accounting_support import context
    from sqlalchemy import func, select

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.accounting.seed import prepare_demo_tenants, seed_demo_tenants
    from aco.features.identity.models import Membership, Tenant, User
    from aco.features.identity.service import IdentityService, Permission
    from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion

    with actor_client("owner") as (client, store, user, tenant):
        operator = User(
            issuer=user.issuer, subject=str(uuid4()), email="operator@example.test", email_verified=True
        )
        store.add(operator)
        store.commit()

        async def run() -> Any:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                await prepare_demo_tenants(db, user.id, operator.id, "local")
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                return await seed_demo_tenants(db, user.id, operator.id, "local")

        ids = asyncio.run(run())
        assert asyncio.run(run()) == ids
        store.expire_all()
        assert len(ids) == 2
        policies = list(store.scalars(select(CollectionPolicy).where(CollectionPolicy.tenant_id.in_(ids))))
        versions = list(
            store.scalars(select(CollectionPolicyVersion).where(CollectionPolicyVersion.tenant_id.in_(ids)))
        )
        assert len(policies) == 2 and all(not p.active for p in policies)
        assert {v.state for v in versions} == {"DRAFT"}
        assert {tuple(cast(list[int], v.definition["reminder_offsets_days"])) for v in versions} == {
            (-3, 3, 10),
            (-1, 5, 14),
        }
        count = store.scalar(
            select(func.count()).select_from(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == ids[0])
        )
        assert count is not None and 20 <= count <= 30
        roles = list(store.scalars(select(Membership.role).where(Membership.tenant_id == ids[0])))
        assert set(roles) == {"owner", "operator"}
        for key in ids:
            row = store.get(Tenant, key)
            assert row and row.is_demo and row.outbound_paused and not row.automation_enabled
        # Fixture actors use actual persisted membership permission guard; no real-login claim.
        IdentityService(store).authorize(context(ids[0], operator.id), Permission.TASK_WRITE)


def test_unselected_accounting_runtime_has_no_mutation_capability() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        app = cast(FastAPI, client.app)
        app.state.settings = app.state.settings.model_copy(update={"accounting_adapter": "fake"})
        response = client.post(f"/api/workspaces/{tenant}/demo/accounting/seed")
        assert response.status_code == 503
        assert response.json()["detail"] == "ACCOUNTING_NOT_CONFIGURED"


def test_fresh_observation_preserves_immutable_invoice_and_payment_facts() -> None:
    from accounting_support import context, document_apply, document_source
    from sqlalchemy import select

    from aco.features.accounting.models import (
        InvoiceSnapshot,
        InvoiceSourceRevision,
        VerifiedPaymentAllocation,
    )
    from aco.features.identity.models import now

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        first_observed = source.observed_at
        first = document_apply(client.app, context(tenant, user.id), source)
        original = store.scalar(
            select(InvoiceSourceRevision).where(InvoiceSourceRevision.invoice_id == first.invoice_id)
        )
        payment = store.scalar(
            select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.invoice_id == first.invoice_id)
        )
        assert original is not None and payment is not None
        original_payload, original_payment_time = original.payload, payment.observed_at
        source.observed_at = now()
        second = document_apply(client.app, context(tenant, user.id), source)
        assert second.invoice_id == first.invoice_id
        store.expire_all()
        snapshot = store.get(InvoiceSnapshot, first.invoice_id)
        assert snapshot is not None and snapshot.verified and snapshot.observed_at == source.observed_at
        assert (
            original.payload == original_payload
            and payment.observed_at == original_payment_time == first_observed
        )
        assert len(list(store.scalars(select(InvoiceSourceRevision)))) == 1
        assert len(list(store.scalars(select(VerifiedPaymentAllocation)))) == 1
        from aco.features.accounting.models import AccountingObservation

        observations = list(
            store.scalars(select(AccountingObservation).order_by(AccountingObservation.created_at))
        )
        assert len(observations) == 2
        assert all(observation.actor_id == user.id for observation in observations)
        assert [cast(dict[str, str], o.invoice_data["stamp"])["observed_at"] for o in observations] == [
            first_observed.isoformat().replace("+00:00", "Z"),
            source.observed_at.isoformat().replace("+00:00", "Z"),
        ]
        assert observations[0].payment_data != observations[1].payment_data


def test_invoice_correction_accepts_reobserved_payment_but_conflict_rolls_back() -> None:
    import pytest
    from accounting_support import context, document_apply, document_source
    from sqlalchemy import func, select

    from aco.features.accounting.models import (
        InvoiceSnapshot,
        InvoiceSourceRevision,
        VerifiedPaymentAllocation,
    )
    from aco.features.identity.models import now
    from aco.integrations.ports.accounting import AccountingFailure

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        first = document_apply(client.app, context(tenant, user.id), source)
        source.documents[0].update(etag="opaque-new", due="2026-12-05")
        source.observed_at = now()
        document_apply(client.app, context(tenant, user.id), source)
        store.expire_all()
        snapshot = store.get(InvoiceSnapshot, first.invoice_id)
        assert (
            snapshot is not None
            and str(snapshot.due_date) == "2026-12-05"
            and snapshot.source_revision == "opaque-new"
        )
        assert store.scalar(select(func.count()).select_from(VerifiedPaymentAllocation)) == 1
        from aco.features.accounting.models import AccountingObservation

        before = store.scalar(select(func.count()).select_from(AccountingObservation))
        source.documents[0].update(etag="another", due="2026-12-06")
        source.receipts = [("RCPT-1", "split-9", "DOC-1", 9999)]
        source.observed_at = now()
        with pytest.raises(AccountingFailure):
            document_apply(client.app, context(tenant, user.id), source)
        store.expire_all()
        assert str(snapshot.due_date) == "2026-12-05" and snapshot.source_revision == "opaque-new"
        assert store.scalar(select(func.count()).select_from(InvoiceSourceRevision)) == 2
        assert store.scalar(select(func.count()).select_from(AccountingObservation)) == before
        assert store.scalar(select(VerifiedPaymentAllocation.minor_units)) == 2500


def test_current_backdated_correction_preserves_history_and_delayed_hint_refetches() -> None:
    from copy import deepcopy
    from datetime import timedelta
    from uuid import UUID

    from accounting_support import apply, context, publish_invoice
    from sqlalchemy import select

    from aco.features.accounting.dto import AccountingDataChanged
    from aco.features.accounting.models import DemoSourceRecord, InvoiceSnapshot, InvoiceSourceRevision
    from aco.features.identity.models import now

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        prior = store.scalar(
            select(DemoSourceRecord).where(
                DemoSourceRecord.provider_connection_id == connection,
                DemoSourceRecord.kind == "invoice",
                DemoSourceRecord.external_id == "INV-101",
            )
        )
        assert prior is not None
        payload = deepcopy(prior.payload)
        payload["number"] = "BACKDATED-CURRENT"
        effective = prior.effective_at - timedelta(days=1)
        store.add(
            DemoSourceRecord(
                tenant_id=tenant,
                provider_connection_id=connection,
                organisation_id=prior.organisation_id,
                kind="invoice",
                external_id="INV-101",
                revision=2,
                payload=payload,
                effective_at=effective,
                observed_at=now(),
            )
        )
        store.commit()
        hint = AccountingDataChanged(
            provider_connection_id=connection,
            organisation_id=prior.organisation_id,
            external_invoice_id="INV-101",
            external_event_id="old-source-hint",
        )
        result = apply(client.app, context(tenant, user.id), hint)
        store.expire_all()
        snapshot = store.get(InvoiceSnapshot, result.invoice_id)
        assert (
            snapshot is not None
            and snapshot.number == "BACKDATED-CURRENT"
            and snapshot.effective_at == effective
        )
        versions = list(
            store.scalars(
                select(InvoiceSourceRevision).where(InvoiceSourceRevision.invoice_id == snapshot.id)
            )
        )
        assert {v.source_revision for v in versions} == {"1", "2"}
        assert {cast(dict[str, str], v.payload["stamp"])["source_at"] for v in versions} == {
            prior.effective_at.isoformat().replace("+00:00", "Z"),
            effective.isoformat().replace("+00:00", "Z"),
        }
        publish_invoice(store, tenant, connection, number="LATEST")
        replay = apply(client.app, context(tenant, user.id), hint)
        assert replay == result  # Immutable original event/result, current projection refreshed separately.
        store.expire_all()
        assert snapshot.number == "LATEST" and snapshot.source_revision == "3"
        assert (
            len(
                list(
                    store.scalars(
                        select(InvoiceSourceRevision).where(InvoiceSourceRevision.invoice_id == snapshot.id)
                    )
                )
            )
            == 3
        )


def test_aged_demo_ledger_is_freshly_verified_without_rewriting_source_observation() -> None:
    from datetime import timedelta
    from uuid import UUID

    from accounting_support import apply, context, publish_invoice
    from sqlalchemy import func, select

    from aco.features.accounting.models import (
        AccountingEvent,
        DemoSourceRecord,
        InvoiceSnapshot,
        InvoiceSourceRevision,
    )
    from aco.features.identity.models import now

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        original_observed = now() - timedelta(days=2)
        hint = publish_invoice(
            store,
            tenant,
            connection,
            observed_at=original_observed,
            status="paid",
            source_status="PAID",
            outstanding={"minor_units": 0, "currency": "AUD", "exponent": 2},
        )
        result = apply(client.app, context(tenant, user.id), hint)
        assert result.financial_status == "paid" and result.collection_lifecycle == "closed"
        store.expire_all()
        snapshot = store.get(InvoiceSnapshot, result.invoice_id)
        assert snapshot is not None and snapshot.observed_at > original_observed
        source = store.scalar(
            select(DemoSourceRecord).where(
                DemoSourceRecord.provider_connection_id == connection,
                DemoSourceRecord.kind == "invoice",
                DemoSourceRecord.external_id == "INV-101",
                DemoSourceRecord.revision == 2,
            )
        )
        assert source is not None and source.observed_at == original_observed
        first_read = snapshot.observed_at
        counts = [
            store.scalar(select(func.count()).select_from(m))
            for m in (InvoiceSourceRevision, AccountingEvent)
        ]
        assert apply(client.app, context(tenant, user.id), hint) == result
        store.expire_all()
        assert snapshot.observed_at > first_read
        assert [
            store.scalar(select(func.count()).select_from(m))
            for m in (InvoiceSourceRevision, AccountingEvent)
        ] == counts


def test_currentness_rejects_stale_and_unverified_response_atomically() -> None:
    from datetime import timedelta

    import pytest
    from accounting_support import context, document_apply, document_source
    from sqlalchemy import func, select

    from aco.features.accounting.models import InvoiceSnapshot, InvoiceSourceRevision
    from aco.features.identity.models import now
    from aco.integrations.ports.accounting import AccountingFailure

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        first = document_apply(client.app, context(tenant, user.id), source)
        for native_current, read_at in (
            (False, None),
            (None, None),
            (True, now() - timedelta(days=1)),
            (True, now() + timedelta(days=1)),
        ):
            source.native_current, source.read_at = native_current, read_at
            source.documents[0].update(etag="apparently-new", unpaid=0)
            source.observed_at = now()
            with pytest.raises(AccountingFailure):
                document_apply(client.app, context(tenant, user.id), source)
            store.expire_all()
            row = store.get(InvoiceSnapshot, first.invoice_id)
            assert row is not None and row.source_revision == "v1" and row.balance_minor == 7500
            assert store.scalar(select(func.count()).select_from(InvoiceSourceRevision)) == 1


def test_opaque_revision_backdated_document_correction_and_old_read_rejection() -> None:
    from datetime import timedelta

    import pytest
    from accounting_support import context, document_apply, document_source
    from sqlalchemy import select

    from aco.features.accounting.models import InvoiceSnapshot, InvoiceSourceRevision
    from aco.features.identity.models import now
    from aco.integrations.ports.accounting import AccountingFailure

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        source.documents[0]["etag"] = "z-last-lexically"
        source.read_at = now() - timedelta(seconds=2)
        first = document_apply(client.app, context(tenant, user.id), source)
        source.documents[0].update(etag="a-backdated-current", due="2026-12-09")
        source.invoice_effective_at = source.effective_at - timedelta(days=20)
        source.observed_at = now()
        source.read_at = now() - timedelta(seconds=1)
        document_apply(client.app, context(tenant, user.id), source)
        store.expire_all()
        row = store.get(InvoiceSnapshot, first.invoice_id)
        assert row is not None and row.source_revision == "a-backdated-current"
        assert row.effective_at == source.invoice_effective_at
        assert len(list(store.scalars(select(InvoiceSourceRevision)))) == 2
        source.documents[0].update(etag="z-last-lexically", due="2026-11-01")
        source.invoice_effective_at = source.effective_at
        source.read_at = now() - timedelta(seconds=10)
        with pytest.raises(AccountingFailure):
            document_apply(client.app, context(tenant, user.id), source)
        store.expire_all()
        assert row.source_revision == "a-backdated-current" and str(row.due_date) == "2026-12-09"


def test_observation_history_is_immutable_and_sync_refresh_does_not_create_source_events() -> None:
    from uuid import UUID

    import pytest
    from sqlalchemy import delete, func, select, update
    from sqlalchemy.exc import DBAPIError

    from aco.features.accounting.models import (
        AccountingEvent,
        AccountingObservation,
        DemoSourceRecord,
        InvoiceSnapshot,
        VerifiedPaymentAllocation,
    )

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        before = [
            store.scalar(select(func.count()).select_from(m))
            for m in (AccountingEvent, DemoSourceRecord, VerifiedPaymentAllocation)
        ]
        snapshots = {r.id: r.observed_at for r in store.scalars(select(InvoiceSnapshot))}
        first = client.post(
            f"/api/workspaces/{tenant}/accounting/sync", json={"provider_connection_id": str(connection)}
        )
        assert first.status_code == 200
        observation_count = store.scalar(select(func.count()).select_from(AccountingObservation))
        assert observation_count is not None
        second = client.post(
            f"/api/workspaces/{tenant}/accounting/sync",
            json={"provider_connection_id": str(connection), "cursor": first.json()["source_watermark"]},
        )
        assert second.status_code == 200 and second.json()["invoice_count"] == 24
        store.expire_all()
        assert all(r.observed_at > snapshots[r.id] for r in store.scalars(select(InvoiceSnapshot)))
        assert [
            store.scalar(select(func.count()).select_from(m))
            for m in (AccountingEvent, DemoSourceRecord, VerifiedPaymentAllocation)
        ] == before
        assert store.scalar(select(func.count()).select_from(AccountingObservation)) == observation_count + 24
        assert (
            store.scalar(
                select(func.count())
                .select_from(AccountingObservation)
                .where(AccountingObservation.external_event_id.is_(None))
            )
            == 24
        )
        for statement in (update(AccountingObservation).values(version=99), delete(AccountingObservation)):
            with pytest.raises(DBAPIError), store.begin_nested():
                store.execute(statement)
        store.rollback()


def test_invoice_same_revision_conflict_keeps_fact_and_observation_history() -> None:
    import pytest
    from accounting_support import context, document_apply, document_source
    from sqlalchemy import func, select

    from aco.features.accounting.models import AccountingObservation, InvoiceSnapshot, InvoiceSourceRevision
    from aco.features.identity.models import now
    from aco.integrations.ports.accounting import AccountingFailure

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        first = document_apply(client.app, context(tenant, user.id), source)
        source.observed_at = now()
        source.documents[0]["unpaid"] = 1
        with pytest.raises(AccountingFailure):
            document_apply(client.app, context(tenant, user.id), source)
        store.expire_all()
        row = store.get(InvoiceSnapshot, first.invoice_id)
        assert row is not None and row.balance_minor == 7500
        assert store.scalar(select(func.count()).select_from(InvoiceSourceRevision)) == 1
        assert store.scalar(select(func.count()).select_from(AccountingObservation)) == 1


def test_observation_migration_preserves_existing_financial_and_identity_rows() -> None:
    from uuid import UUID

    from alembic.config import Config
    from sqlalchemy import Engine, func, inspect, select

    from aco.features.accounting.models import (
        DemoSourceRecord,
        InvoiceSourceRevision,
        VerifiedPaymentAllocation,
    )
    from aco.features.identity.models import User
    from alembic import command

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        user_id = user.id
        before = [
            store.scalar(select(func.count()).select_from(m))
            for m in (DemoSourceRecord, InvoiceSourceRevision, VerifiedPaymentAllocation)
        ]
        store.rollback()
        engine = store.get_bind()
        assert isinstance(engine, Engine)
        config = Config("alembic.ini")
        config.set_main_option(
            "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
        )
        from legacy_usage_support import legacy_snapshot

        with legacy_snapshot(store) as (legacy, legacy_config):
            legacy_engine = legacy.get_bind()
            command.downgrade(legacy_config, "r0002_financials")
            assert "accounting_observations" not in inspect(legacy_engine).get_table_names()
            assert [
                legacy.scalar(select(func.count()).select_from(m))
                for m in (DemoSourceRecord, InvoiceSourceRevision, VerifiedPaymentAllocation)
            ] == before
            assert legacy.get(User, user_id) is not None
            legacy.rollback()
            command.upgrade(legacy_config, "head")
            assert [
                legacy.scalar(select(func.count()).select_from(m))
                for m in (DemoSourceRecord, InvoiceSourceRevision, VerifiedPaymentAllocation)
            ] == before
        response = client.post(
            f"/api/workspaces/{tenant}/accounting/sync", json={"provider_connection_id": str(connection)}
        )
        assert response.status_code == 200


def test_future_demo_evidence_never_refreshes_trusted_invoice_or_payment() -> None:
    from datetime import timedelta
    from uuid import UUID

    import pytest
    from accounting_support import apply, context, publish_invoice
    from sqlalchemy import func, select

    from aco.features.accounting.models import (
        AccountingObservation,
        DemoSourceRecord,
        InvoiceSnapshot,
        VerifiedPaymentAllocation,
    )
    from aco.features.identity.models import now
    from aco.integrations.ports.accounting import AccountingFailure

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        event = publish_invoice(
            store,
            tenant,
            connection,
            observed_at=now() + timedelta(days=1),
            status="paid",
            source_status="PAID",
            outstanding={"minor_units": 0, "currency": "AUD", "exponent": 2},
        )
        result = apply(client.app, context(tenant, user.id), event)
        assert result.financial_status == "unknown" and result.collection_lifecycle == "active"
        store.expire_all()
        snapshot = store.get(InvoiceSnapshot, result.invoice_id)
        assert snapshot is not None and snapshot.source_revision == "1" and snapshot.balance_minor == 10000
        event = publish_invoice(
            store,
            tenant,
            connection,
            observed_at=now(),
            status="open",
            source_status="OPEN",
            outstanding={"minor_units": 10000, "currency": "AUD", "exponent": 2},
        )
        apply(client.app, context(tenant, user.id), event)
        store.add(
            DemoSourceRecord(
                tenant_id=tenant,
                provider_connection_id=connection,
                organisation_id=event.organisation_id,
                kind="payment",
                external_id="future-cash/future-allocation",
                revision=1,
                effective_at=now(),
                observed_at=now() + timedelta(days=1),
                payload={
                    "external_payment_id": "future-cash",
                    "external_allocation_id": "future-allocation",
                    "external_invoice_id": "INV-101",
                    "kind": "cash",
                    "amount": {"minor_units": 100, "currency": "AUD", "exponent": 2},
                    "parent_payment_id": None,
                    "parent_allocation_id": None,
                },
            )
        )
        store.commit()
        before = store.scalar(select(func.count()).select_from(AccountingObservation))
        with pytest.raises(AccountingFailure):
            apply(
                client.app,
                context(tenant, user.id),
                event.model_copy(update={"external_event_id": "future-receipt"}),
            )
        assert (
            store.scalar(
                select(func.count())
                .select_from(VerifiedPaymentAllocation)
                .where(VerifiedPaymentAllocation.external_payment_id == "future-cash")
            )
            == 0
        )
        assert store.scalar(select(func.count()).select_from(AccountingObservation)) == before


def test_concurrent_reads_serialize_before_fetch_and_older_response_cannot_commit_last() -> None:
    import asyncio

    from accounting_support import context, document_apply, document_source
    from sqlalchemy import text
    from synthetic_accounting import DocumentAccounting

    from aco.features.accounting.dto import AccountingDataChanged, AccountingResult, InvoiceData
    from aco.features.accounting.ingest import AccountingService
    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.identity.models import now
    from aco.integrations.ports.accounting import AccountingFailure

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        ctx = context(tenant, user.id)
        original = document_apply(client.app, ctx, source)
        app = cast(FastAPI, client.app)

        async def run() -> None:
            fetched, release, older_called, second_ready = (asyncio.Event() for _ in range(4))
            second_pid = 0

            class PausedCurrent(DocumentAccounting):
                async def get_invoice(
                    self, ctx: ActorContext, invoice_key: str
                ) -> AccountingResult[InvoiceData]:
                    response = await super().get_invoice(ctx, invoice_key)
                    fetched.set()
                    await release.wait()
                    return response

            class EarlierRead(DocumentAccounting):
                async def get_invoice(
                    self, ctx: ActorContext, invoice_key: str
                ) -> AccountingResult[InvoiceData]:
                    older_called.set()
                    return await super().get_invoice(ctx, invoice_key)

            earlier_captured_at = now()
            newer = PausedCurrent(tenant, source.connection_id, source.organisation, now())
            newer.documents[0].update(etag="opaque-current", due="2026-12-10")
            newer.effective_at = newer.invoice_effective_at = source.effective_at
            older = EarlierRead(tenant, source.connection_id, source.organisation, source.observed_at)
            older.read_at = earlier_captured_at
            # Earlier captured transport read is still fresh in age, but precedes newer's read.
            # Payment effective/source facts remain unchanged across both provider responses.
            older.effective_at = older.invoice_effective_at = source.effective_at

            async def ingest(adapter: DocumentAccounting, second: bool = False) -> object:
                nonlocal second_pid
                async with app.state.business_sessions.begin() as db:
                    if second:
                        second_pid = int(await db.scalar(text("SELECT pg_backend_pid()")))
                        second_ready.set()
                    return await AccountingService(db, adapter).apply_accounting_change(
                        ctx,
                        AccountingDataChanged(
                            provider_connection_id=source.connection_id,
                            organisation_id=source.organisation,
                            external_invoice_id="DOC-1",
                            external_event_id=str(uuid4()),
                        ),
                    )

            first_task = asyncio.create_task(ingest(newer))
            await asyncio.wait_for(fetched.wait(), timeout=10)
            second_task = asyncio.create_task(ingest(older, True))
            try:
                await asyncio.wait_for(second_ready.wait(), timeout=10)
                async with app.state.business_sessions.begin() as observer:
                    async with asyncio.timeout(10):
                        while True:
                            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                            waiting = await observer.scalar(
                                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                                {"pid": second_pid},
                            )
                            if waiting == "Lock":
                                break
                            await asyncio.sleep(0.01)
                assert not older_called.is_set()  # Actual PostgreSQL lock precedes source fetch.
            finally:
                release.set()
                outcomes = await asyncio.gather(first_task, second_task, return_exceptions=True)
            assert not isinstance(outcomes[0], BaseException)
            assert isinstance(outcomes[1], AccountingFailure)
            assert older_called.is_set()

        asyncio.run(run())
        store.expire_all()
        snapshot = store.get(InvoiceSnapshot, original.invoice_id)
        assert snapshot is not None and snapshot.source_revision == "opaque-current"
        assert str(snapshot.due_date) == "2026-12-10"


def test_missed_hint_recovers_one_normalized_change_and_audit_without_vendor_event_id() -> None:
    import asyncio
    from uuid import UUID

    from accounting_support import context, document_source
    from sqlalchemy import func, select

    from aco.features.accounting.ingest import AccountingService
    from aco.features.accounting.models import AccountingEvent, AccountingObservation, InvoiceSnapshot
    from aco.features.identity.models import AuditEvent, now

    with actor_client("owner") as (client, store, user, tenant):
        source = document_source(store, tenant)
        app = cast(FastAPI, client.app)
        ctx = context(tenant, user.id)

        from aco.features.accounting.dto import SyncCursor, SyncResult

        async def sync(cursor: SyncCursor | None = None) -> SyncResult:
            async with app.state.business_sessions.begin() as db:
                return await AccountingService(db, source).sync_accounting(ctx, cursor)

        first = asyncio.run(sync())
        initial = store.scalar(select(AccountingEvent))
        assert initial is not None
        initial_result = dict(initial.result)
        source.documents[0].update(etag="recovered-opaque", due="2026-12-15")
        source.observed_at = now()
        asyncio.run(sync(first.source_watermark))  # Native feed returns no hints; list/get has changed truth.
        store.expire_all()
        assert store.scalar(select(func.count()).select_from(AccountingEvent)) == 2
        recovered = store.scalar(select(AccountingEvent).where(AccountingEvent.external_event_id.is_(None)))
        assert recovered is not None and recovered.origin == "AUTHORITATIVE_READ"
        snapshot = store.scalar(select(InvoiceSnapshot))
        assert snapshot is not None and snapshot.source_revision == "recovered-opaque"
        assert recovered.invoice_id == snapshot.id and recovered.source_revision == "recovered-opaque"
        assert recovered.envelope["kind"] == "AccountingDataChanged"
        payload = cast(dict[str, object], recovered.envelope["payload"])
        assert payload["provider_event_id"] is None and payload["origin"] == "AUTHORITATIVE_READ"
        assert store.get(AccountingObservation, UUID(str(payload["observation_id"]))) is not None
        audit = list(store.scalars(select(AuditEvent).where(AuditEvent.action == "accounting.reconciled")))
        assert len(audit) == 2 and {a.actor_id for a in audit} == {user.id}
        assert str(recovered.id) in {a.details["source_event_id"] for a in audit}
        observation_count = store.scalar(select(func.count()).select_from(AccountingObservation))
        assert observation_count is not None
        asyncio.run(sync(first.source_watermark))
        store.expire_all()
        assert initial.result == initial_result
        assert store.scalar(select(func.count()).select_from(AccountingEvent)) == 2
        assert (
            store.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "accounting.reconciled")
            )
            == 2
        )
        after_observations = store.scalar(select(func.count()).select_from(AccountingObservation))
        assert after_observations is not None and after_observations > observation_count

        import pytest
        from sqlalchemy.exc import DBAPIError, IntegrityError

        for origin, native_id in (
            ("AUTHORITATIVE_READ", "invented"),
            ("SOURCE_HINT", None),
            ("UNKNOWN", None),
            ("AUTHORITATIVE_READ", None),
        ):
            with pytest.raises(IntegrityError), store.begin_nested():
                store.add(
                    AccountingEvent(
                        tenant_id=tenant,
                        provider_connection_id=source.connection_id,
                        invoice_id=snapshot.id,
                        source_revision="recovered-opaque",
                        origin=origin,
                        external_event_id=native_id,
                        envelope={},
                        provenance={},
                        result={},
                    )
                )
                store.flush()
        # An older schema cannot represent the absent native ID: downgrade must fail atomically,
        # not erase immutable recovered evidence or invent a vendor identity.
        from alembic.config import Config
        from sqlalchemy import Engine, text

        from alembic import command

        store.rollback()
        engine = store.get_bind()
        assert isinstance(engine, Engine)
        config = Config("alembic.ini")
        config.set_main_option(
            "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
        )
        from legacy_usage_support import legacy_snapshot

        with legacy_snapshot(store) as (legacy, legacy_config):
            with pytest.raises(DBAPIError, match="authoritative-read history"):
                command.downgrade(legacy_config, "r0002_financials")
            assert (
                legacy.scalar(text("SELECT version_num FROM alembic_version"))
                == "r0002b_accounting_observations"
            )
            assert legacy.scalar(select(func.count()).select_from(AccountingEvent)) == 2
        assert (
            store.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        assert store.scalar(select(func.count()).select_from(AccountingEvent)) == 2
