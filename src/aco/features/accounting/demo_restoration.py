"""Compensating synthetic source facts; current snapshots are written only by AccountingService."""

import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.context import TenantContext
from aco.features.accounting.dto import InvoiceData, PaymentData


@dataclass(frozen=True)
class ResidualCash:
    external_payment_id: str
    external_allocation_id: str
    minor_units: int


def review_required() -> HTTPException:
    return HTTPException(409, "DEMO_RESET_SOURCE_REVIEW_REQUIRED")


def residual_reversals(
    seed: InvoiceData, current: InvoiceData, payments: list[PaymentData]
) -> tuple[ResidualCash, ...]:
    """Keep original seed cash; undo only verified residual additions with exact parents."""
    if (
        not seed.verified
        or not current.verified
        or not seed.stamp.synthetic
        or not current.stamp.synthetic
        or seed.stamp.connection_id != current.stamp.connection_id
        or seed.external_id != current.external_id
        or seed.organisation_id != current.organisation_id
        or seed.external_customer_id != current.external_customer_id
        or seed.original != current.original
        or not 0 <= seed.outstanding.minor_units <= seed.original.minor_units
        or not 0 <= current.outstanding.minor_units <= seed.outstanding.minor_units
        or current.status not in {"open", "partially_paid", "paid"}
    ):
        raise review_required()
    rows: dict[tuple[str, str], PaymentData] = {}
    for payment in payments:
        key = (payment.external_payment_id, payment.external_allocation_id)
        if (
            key in rows
            or not payment.verified
            or not payment.stamp.synthetic
            or payment.stamp.connection_id != seed.stamp.connection_id
            or payment.external_invoice_id != seed.external_id
            or payment.organisation_id != seed.organisation_id
            or (payment.amount.currency, payment.amount.exponent)
            != (seed.original.currency, seed.original.exponent)
            or payment.kind in {"credit", "write_off"}
        ):
            raise review_required()
        rows[key] = payment
    cash = {key: row for key, row in rows.items() if row.kind == "cash"}
    remaining = {key: row.amount.minor_units for key, row in cash.items()}
    for row in rows.values():
        if row.kind in {"refund", "reversal"}:
            key = (row.parent_payment_id or "", row.parent_allocation_id or "")
            if key not in cash:
                raise review_required()
            remaining[key] += row.amount.minor_units
    if any(value < 0 for value in remaining.values()):
        raise review_required()
    base_key = (f"seed-{seed.external_id}", "1")
    baseline_cash = seed.original.minor_units - seed.outstanding.minor_units
    base = cash.get(base_key)
    if baseline_cash:
        if (
            base is None
            or base.amount.minor_units != baseline_cash
            or remaining[base_key] != baseline_cash
            or base.stamp.revision != "1"
        ):
            raise review_required()
    elif base is not None:
        raise review_required()
    extras = []
    for key in sorted(cash):
        if key == base_key:
            continue
        row = cash[key]
        if (
            row.parent_payment_id is not None
            or row.stamp.source_at is None
            or seed.stamp.source_at is None
            or row.stamp.source_at < seed.stamp.source_at
        ):
            raise review_required()
        if remaining[key]:
            extras.append(ResidualCash(key[0], key[1], remaining[key]))
    if (
        seed.outstanding.minor_units - sum(row.minor_units for row in extras)
        != current.outstanding.minor_units
    ):
        raise review_required()
    return tuple(extras)


@dataclass(frozen=True)
class RestoredSource:
    invoice_ids: tuple[UUID, ...]
    customer_ids: tuple[UUID, ...]
    reversal_allocation_ids: tuple[UUID, ...]
    source_event_ids: tuple[UUID, ...]


async def restore_seed_source(db: AsyncSession, ctx: TenantContext, request_id: UUID) -> RestoredSource:
    """Retain original seed identities and cash, append compensations, then import normally."""
    from copy import deepcopy
    from uuid import uuid5

    from sqlalchemy import select

    from aco.features.accounting.dto import AccountingDataChanged
    from aco.features.accounting.ingest import AccountingService
    from aco.features.accounting.models import (
        Customer,
        DemoSourceRecord,
        VerifiedPaymentAllocation,
    )
    from aco.features.accounting.seed import seed_customer_payload, seed_invoice_payload
    from aco.features.identity.models import ProviderConnection, now
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

    namespace = UUID("f6fc0686-96bd-4ea2-8bcd-8c720ca63836")
    records = list(
        await db.scalars(
            select(DemoSourceRecord)
            .where(DemoSourceRecord.tenant_id == ctx.tenant_id)
            .order_by(DemoSourceRecord.sequence)
        )
    )
    markers = [
        row
        for row in records
        if row.kind == "scenario_input"
        and row.external_id == "future-stories"
        and row.revision == 1
        and row.payload.get("synthetic") is True
    ]
    if len(markers) != 1:
        raise review_required()
    connection_id = markers[0].provider_connection_id
    connection = await db.get(ProviderConnection, connection_id)
    if (
        connection is None
        or connection.tenant_id != ctx.tenant_id
        or connection.provider != "demo"
        or connection.status != "READY"
        or connection.external_account_id != f"synthetic:{connection.id}"
    ):
        raise review_required()
    originals = {
        (row.kind, row.external_id): row
        for row in records
        if row.provider_connection_id == connection_id and row.revision == 1
    }
    latest: dict[tuple[str, str], int] = {}
    for retained in records:
        if retained.provider_connection_id == connection_id:
            if retained.organisation_id != connection.external_account_id:
                raise review_required()
            record_key = (retained.kind, retained.external_id)
            latest[record_key] = max(latest.get(record_key, 0), retained.revision)
    customers = []
    for index in range(6):
        row = originals.get(("customer", f"CUSTOMER-{index + 1}"))
        if row is None or row.payload != seed_customer_payload(index):
            raise review_required()
        customers.append(row)
    adapter = DemoAccountingAdapter(db, connection_id)
    service = AccountingService(db, adapter)
    plans = []
    for index in range(24):
        row = originals.get(("invoice", f"INV-{101 + index}"))
        if row is None or row.payload != seed_invoice_payload(index, row.effective_at):
            raise review_required()
        baseline = InvoiceData.model_validate_json(
            json.dumps(
                {
                    **row.payload,
                    "organisation_id": row.organisation_id,
                    "stamp": adapter.demo_source.stamp(row).model_dump(mode="json"),
                }
            )
        )
        current = await adapter.get_invoice(ctx, row.external_id)
        await service.validate_result(ctx, current)
        service.require_current(current)
        payments = await adapter.list_payments(ctx, row.external_id)
        await service.validate_result(ctx, payments)
        service.require_current(payments)
        plans.append((row, residual_reversals(baseline, current.value, payments.value)))
    stamp = now()
    for original in customers + [plan[0] for plan in plans]:
        db.add(
            DemoSourceRecord(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection_id,
                organisation_id=connection.external_account_id,
                kind=original.kind,
                external_id=original.external_id,
                revision=latest[(original.kind, original.external_id)] + 1,
                payload=deepcopy(original.payload),
                effective_at=stamp,
                observed_at=stamp,
            )
        )
    reversal_ids = []
    for original, reversals in plans:
        for reversal in reversals:
            payment_key = str(
                uuid5(
                    namespace,
                    f"{ctx.tenant_id}:{request_id}:{original.external_id}:"
                    f"{reversal.external_payment_id}:{reversal.external_allocation_id}",
                )
            )
            reversal_ids.append(payment_key)
            db.add(
                DemoSourceRecord(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=connection_id,
                    organisation_id=connection.external_account_id,
                    kind="payment",
                    external_id=f"{payment_key}/1",
                    revision=1,
                    payload={
                        "external_payment_id": payment_key,
                        "external_allocation_id": "1",
                        "external_invoice_id": original.external_id,
                        "kind": "reversal",
                        "amount": {"minor_units": -reversal.minor_units, "currency": "AUD", "exponent": 2},
                        "parent_payment_id": reversal.external_payment_id,
                        "parent_allocation_id": reversal.external_allocation_id,
                    },
                    effective_at=stamp,
                    observed_at=stamp,
                )
            )
    await db.flush()
    results = []
    for original, _reversals in plans:
        results.append(
            await service.apply_accounting_change(
                ctx,
                AccountingDataChanged(
                    provider_connection_id=connection_id,
                    organisation_id=connection.external_account_id,
                    external_invoice_id=original.external_id,
                    external_event_id=f"demo-reset:{request_id}:{original.external_id}",
                ),
            )
        )
    customer_ids = tuple(
        await db.scalars(
            select(Customer.id)
            .where(
                Customer.tenant_id == ctx.tenant_id,
                Customer.provider_connection_id == connection_id,
                Customer.external_customer_id.in_([row.external_id for row in customers]),
            )
            .order_by(Customer.id)
        )
    )
    allocation_ids = tuple(
        await db.scalars(
            select(VerifiedPaymentAllocation.id)
            .where(
                VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                VerifiedPaymentAllocation.provider_connection_id == connection_id,
                VerifiedPaymentAllocation.external_payment_id.in_(reversal_ids),
            )
            .order_by(VerifiedPaymentAllocation.id)
        )
    )
    if len(customer_ids) != 6 or len(allocation_ids) != len(reversal_ids):
        raise review_required()
    return RestoredSource(
        tuple(result.invoice_id for result in results),
        customer_ids,
        allocation_ids,
        tuple(result.event_id for result in results),
    )
