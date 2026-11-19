"""Accounting adapter substitution is proved through production reconciliation."""

import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from accounting_support import context, seed
from fastapi import FastAPI, HTTPException
from identity_support import actor_client
from sqlalchemy import func, select
from synthetic_accounting import DocumentAccounting

from aco.core.types import ProviderErrorCode
from aco.features.accounting.dto import SyncCursor
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import CollectionRecord, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.identity.models import ProviderConnection, now
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
from aco.integrations.ports.accounting import AccountingFailure, AccountingPort


def test_accounting_runtime_is_actual_database_adapter() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        seeded = seed(client, tenant)
        response = client.post(
            f"/api/workspaces/{tenant}/accounting/sync",
            json={"provider_connection_id": seeded["provider_connection_id"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["invoice_count"] == 24
        assert response.json()["source_watermark"] is not None


@pytest.mark.parametrize("shape", ["postgres-demo", "document-source"])
def test_shared_accounting_methods_provenance_tenancy_and_idempotency(shape: str) -> None:
    with actor_client("owner") as (client, store, user, tenant):
        if shape == "postgres-demo":
            connection = UUID(seed(client, tenant)["provider_connection_id"])
        else:
            row = ProviderConnection(
                tenant_id=tenant,
                provider_type="accounting",
                provider="document-test",
                status="READY",
                external_account_id="ORG-DOC",
            )
            store.add(row)
            store.commit()
            connection = row.id
        ctx = context(tenant, user.id)

        async def run() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                adapter: AccountingPort = (
                    DemoAccountingAdapter(db, connection)
                    if shape == "postgres-demo"
                    else DocumentAccounting(tenant, connection, "ORG-DOC", now())
                )
                capabilities = await adapter.describe_capabilities()
                assert {
                    "list_customers",
                    "list_invoices",
                    "get_invoice",
                    "list_payments",
                    "get_change_feed",
                } <= capabilities.features
                assert "webhook" not in capabilities.features
                with pytest.raises(AccountingFailure) as unsupported:
                    await adapter.verify_webhook(b"forged", {"tenant": "foreign"})
                assert unsupported.value.failure.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
                assert unsupported.value.failure.may_have_side_effect is False
                customers = await adapter.list_customers(ctx)
                invoices = await adapter.list_invoices(ctx)
                invoice = await adapter.get_invoice(ctx, invoices.value[0].external_id)
                payments = await adapter.list_payments(ctx, invoices.value[0].external_id)
                changes = await adapter.get_change_feed(ctx)
                for response in (customers, invoices, invoice, payments, changes):
                    assert response.tenant_id == tenant and response.provider_connection_id == connection
                    assert response.correlation_id == ctx.correlation_id and response.native_operation_id
                    assert response.authenticity == "VERIFIED" and response.attempt == 1
                    assert response.currentness == "CURRENT"
                    assert response.cost.amount is None and response.cost.quality.value == "UNKNOWN"
                assert invoice.value.stamp.synthetic and invoice.value.stamp.revision
                service = AccountingService(db, adapter)
                first = await service.sync_accounting(ctx, None)
                second = await service.sync_accounting(ctx, first.source_watermark)
                assert second.invoice_count == first.invoice_count
                assert first.invoice_count == (24 if shape == "postgres-demo" else 1)
                for method, args in (
                    (adapter.list_customers, ()),
                    (adapter.list_invoices, ()),
                    (adapter.get_invoice, (invoices.value[0].external_id,)),
                    (adapter.list_payments, (invoices.value[0].external_id,)),
                    (adapter.get_change_feed, ()),
                ):
                    with pytest.raises((HTTPException, AccountingFailure)):
                        await method(ctx.model_copy(update={"tenant_id": uuid4()}), *args)
                with pytest.raises(AccountingFailure):
                    await adapter.get_change_feed(ctx, SyncCursor(token="other-connection:0"))

        asyncio.run(run())
        count = store.scalar(
            select(func.count())
            .select_from(InvoiceSnapshot)
            .where(InvoiceSnapshot.provider_connection_id == connection)
        )
        allocations = store.scalar(
            select(func.count())
            .select_from(VerifiedPaymentAllocation)
            .where(VerifiedPaymentAllocation.provider_connection_id == connection)
        )
        collections = store.scalar(
            select(func.count()).select_from(CollectionRecord).where(CollectionRecord.tenant_id == tenant)
        )
        assert count == (24 if shape == "postgres-demo" else 1)
        assert allocations == (7 if shape == "postgres-demo" else 1)
        assert collections == count


@pytest.mark.parametrize(
    "native,code",
    [
        ("expired", "AUTH_EXPIRED"),
        ("busy", "RATE_LIMITED"),
        ("offline", "TEMPORARILY_UNAVAILABLE"),
        ("bad", "VALIDATION_FAILED"),
    ],
)
def test_document_adapter_error_normalization(native: str, code: str) -> None:
    ctx = context(uuid4(), uuid4())
    adapter = DocumentAccounting(ctx.tenant_id, uuid4(), "DOC", now())
    adapter.native_failure = native
    with pytest.raises(AccountingFailure) as raised:
        asyncio.run(adapter.get_invoice(ctx, "DOC-1"))
    assert raised.value.failure.code.value == code
    assert raised.value.failure.may_have_side_effect is False
    assert raised.value.failure.request_id == "document-request"
    if native == "busy":
        assert raised.value.failure.retry_after is not None
        assert raised.value.failure.retry_after.total_seconds() == 7


def test_import_rejects_provider_identity_mismatch() -> None:
    from aco.features.accounting.dto import AccountingResult, InvoiceData

    with actor_client("owner") as (client, store, user, tenant):
        row = ProviderConnection(
            tenant_id=tenant,
            provider_type="accounting",
            provider="document-test",
            status="READY",
            external_account_id="ORG-DOC",
        )
        store.add(row)
        store.commit()

        class WrongIdentity(DocumentAccounting):
            async def get_invoice(self, ctx: Any, invoice_key: str) -> AccountingResult[InvoiceData]:
                result = await super().get_invoice(ctx, invoice_key)
                return result.model_copy(update={"provider": "foreign-provider"})

        async def run() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                adapter = WrongIdentity(tenant, row.id, "ORG-DOC", now())
                await AccountingService(db, adapter).sync_accounting(context(tenant, user.id), None)

        with pytest.raises(AccountingFailure):
            asyncio.run(run())
        assert (
            store.scalar(
                select(func.count()).select_from(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant)
            )
            == 0
        )


def test_demo_adapter_unconfigured_and_database_failure_are_typed() -> None:
    from sqlalchemy import text

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        ctx = context(tenant, user.id)

        async def read(key: str = "INV-101") -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                await DemoAccountingAdapter(db, connection).get_invoice(ctx, key)

        with pytest.raises(AccountingFailure) as invalid:
            asyncio.run(read("missing"))
        assert invalid.value.failure.code == ProviderErrorCode.VALIDATION_FAILED
        row = store.get(ProviderConnection, connection)
        assert row is not None
        row.status = "NOT_CONFIGURED"
        store.commit()
        with pytest.raises(AccountingFailure) as unconfigured:
            asyncio.run(read())
        assert unconfigured.value.failure.code == ProviderErrorCode.AUTH_EXPIRED
        row.status = "READY"
        store.commit()
        store.execute(text("ALTER TABLE demo_source_records RENAME TO test_unavailable_source"))
        store.commit()
        with pytest.raises(AccountingFailure) as unavailable:
            asyncio.run(read())
        assert unavailable.value.failure.code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE
        assert unavailable.value.failure.retryable and not unavailable.value.failure.may_have_side_effect
        store.execute(text("ALTER TABLE test_unavailable_source RENAME TO demo_source_records"))
        store.commit()


def test_second_shape_normalizes_native_document_values() -> None:
    ctx = context(uuid4(), uuid4())
    adapter = DocumentAccounting(ctx.tenant_id, uuid4(), "DOC", now())
    adapter.documents[0].update({"units": 20000, "unpaid": 17500, "due": "2026-12-05", "etag": "v2"})
    value = asyncio.run(adapter.get_invoice(ctx, "DOC-1")).value
    assert value.original.minor_units == 20000 and value.outstanding.minor_units == 17500
    assert str(value.due_date) == "2026-12-05" and value.stamp.revision == "v2"
