"""Fixed synthetic-only browser fixture source producer; never registered as an API."""

import asyncio
import sys
from uuid import UUID

from accounting_support import context, publish_invoice
from identity_support import database_url
from sqlalchemy import create_engine, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import CollectionRecord, InvoiceSnapshot
from aco.features.identity.models import Membership, ProviderConnection, Tenant
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter


def main() -> None:
    tenant_id, connection_id, user_id = (UUID(value) for value in sys.argv[1:])
    engine = create_engine(database_url())
    with Session(engine) as store:
        tenant = store.get(Tenant, tenant_id)
        member = store.scalar(
            select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
        )
        connection = store.get(ProviderConnection, connection_id)
        assert tenant and tenant.is_demo and tenant.name.startswith("T05 browser ")
        assert member and member.active and member.role == "owner"
        assert connection and connection.tenant_id == tenant_id and connection.provider == "demo"
        amount = {"minor_units": 9007199254740993, "currency": "AUD", "exponent": 2}
        event = publish_invoice(
            store, tenant_id, connection_id, key="INV-101", original=amount, outstanding=amount
        )
    engine.dispose()

    async def import_source() -> None:
        async_engine = create_async_engine(database_url())
        try:
            async with async_sessionmaker(async_engine, expire_on_commit=False).begin() as db:
                await AccountingService(db, DemoAccountingAdapter(db, connection_id)).apply_accounting_change(
                    context(tenant_id, user_id), event
                )
        finally:
            await async_engine.dispose()

    asyncio.run(import_source())
    engine = create_engine(database_url())
    with Session(engine) as store:
        invoice_id = store.scalar(
            select(InvoiceSnapshot.id).where(
                InvoiceSnapshot.tenant_id == tenant_id, InvoiceSnapshot.external_invoice_id == "INV-101"
            )
        )
        assert invoice_id is not None
        store.execute(delete(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id))
        store.commit()
    engine.dispose()


if __name__ == "__main__":
    main()
