"""Synthetic source writers for adversarial tests; never part of runtime authority."""

import asyncio
from copy import deepcopy
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.accounting.dto import AccountingDataChanged, ReconciliationResult
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import DemoSourceRecord
from aco.features.identity.models import now
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter


def context(tenant: UUID, user: UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=user, role=Role.OWNER, correlation_id=uuid4())


def publish_invoice(
    store: Session, tenant: UUID, connection: UUID, key: str = "INV-101", **updates: Any
) -> AccountingDataChanged:
    latest = store.scalar(
        select(DemoSourceRecord)
        .where(
            DemoSourceRecord.tenant_id == tenant,
            DemoSourceRecord.provider_connection_id == connection,
            DemoSourceRecord.kind == "invoice",
            DemoSourceRecord.external_id == key,
        )
        .order_by(DemoSourceRecord.revision.desc())
    )
    assert latest is not None
    payload = deepcopy(latest.payload)
    observed = updates.pop("observed_at", now())
    payload.update(updates)
    row = DemoSourceRecord(
        tenant_id=tenant,
        provider_connection_id=connection,
        organisation_id=latest.organisation_id,
        kind="invoice",
        external_id=key,
        revision=latest.revision + 1,
        payload=payload,
        effective_at=now(),
        observed_at=observed,
    )
    store.add(row)
    store.commit()
    return AccountingDataChanged(
        provider_connection_id=connection,
        organisation_id=row.organisation_id,
        external_invoice_id=key,
        external_event_id=f"test-source:{row.sequence}",
    )


def apply(app: Any, ctx: TenantContext, event: AccountingDataChanged) -> ReconciliationResult:
    async def run() -> ReconciliationResult:
        async with app.state.business_sessions.begin() as db:
            return await AccountingService(
                db, DemoAccountingAdapter(db, event.provider_connection_id)
            ).apply_accounting_change(ctx, event)

    return asyncio.run(run())


def seed(client: Any, tenant: Any) -> dict[str, Any]:
    response = client.post(f"/api/workspaces/{tenant}/demo/accounting/seed")
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def document_source(store: Session, tenant: UUID) -> Any:
    from synthetic_accounting import DocumentAccounting

    from aco.features.identity.models import ProviderConnection

    row = ProviderConnection(
        tenant_id=tenant,
        provider_type="accounting",
        provider="document-test",
        status="READY",
        external_account_id="ORG-DOC",
    )
    store.add(row)
    store.commit()
    return DocumentAccounting(tenant, row.id, "ORG-DOC", now())


def document_apply(
    app: Any, ctx: TenantContext, source: Any, event: AccountingDataChanged | None = None
) -> ReconciliationResult:
    notification = event or AccountingDataChanged(
        provider_connection_id=source.connection_id,
        organisation_id=source.organisation,
        external_invoice_id="DOC-1",
        external_event_id=str(uuid4()),
    )

    async def run() -> ReconciliationResult:
        async with app.state.business_sessions.begin() as db:
            return await AccountingService(db, source).apply_accounting_change(ctx, notification)

    return asyncio.run(run())
