"""Explicit synthetic source commands, separate from the read-only AccountingPort."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.context import TenantContext
from aco.features.accounting.dto import (
    AccountingDataChanged,
    DemoPaymentCommand,
    EventRef,
    ReconciliationResult,
)
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import DemoPaymentOperation, DemoSourceRecord
from aco.features.identity.models import ProviderConnection, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion
from aco.integrations.demo_accounting.adapter import FEATURES, DemoAccountingAdapter
from aco.integrations.ports.accounting import failure


def seed_customer_payload(index: int) -> dict[str, object]:
    return {
        "external_id": f"CUSTOMER-{index + 1}",
        "name": f"Synthetic Example Trading {index + 1}",
        "email": f"accounts{index + 1}@example.test",
    }


def seed_invoice_payload(index: int, stamp: datetime) -> dict[str, object]:
    balance = 0 if index and index % 6 == 0 else 5000 if index % 6 == 2 else 10000
    status = "paid" if balance == 0 else "partially_paid" if balance < 10000 else "open"
    money = {"minor_units": 10000, "currency": "AUD", "exponent": 2}
    return {
        "external_id": f"INV-{101 + index}",
        "external_customer_id": f"CUSTOMER-{index % 6 + 1}",
        "number": f"INV-{101 + index}",
        "issue_date": str(stamp.date() - timedelta(days=35)),
        "due_date": str(stamp.date() + timedelta(days=(index % 4 - 2) * 7)),
        "original": money,
        "outstanding": {**money, "minor_units": balance},
        "status": status,
        "source_status": status.upper(),
        "verified": True,
    }


async def demo_guard(db: AsyncSession, ctx: TenantContext, app_env: str, permission: Permission) -> None:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
    tenant = await db.run_sync(lambda store: IdentityService(store).authorize(ctx, permission))
    if app_env not in {"local", "demo"} or not tenant.is_demo:
        raise HTTPException(403, "DEMO_ONLY")


async def seed_connection(
    db: AsyncSession, ctx: TenantContext, connection_id: UUID, app_env: str, variant: str = "A"
) -> UUID:
    await demo_guard(db, ctx, app_env, Permission.SETTINGS)
    connection = await db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.id == connection_id, ProviderConnection.tenant_id == ctx.tenant_id
        )
    )
    if connection is None or connection.provider != "demo" or connection.provider_type != "accounting":
        raise failure()
    existing = await db.scalar(
        select(DemoSourceRecord.id).where(
            DemoSourceRecord.tenant_id == ctx.tenant_id,
            DemoSourceRecord.provider_connection_id == connection_id,
        )
    )
    if existing is not None:
        return connection_id  # Never reset/overwrite an existing workspace.
    connection.external_account_id = f"synthetic:{connection_id}"
    connection.capabilities, connection.status = sorted(FEATURES), "READY"
    stamp = now()
    for index in range(6):
        key = f"CUSTOMER-{index + 1}"
        db.add(
            DemoSourceRecord(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection_id,
                organisation_id=connection.external_account_id,
                kind="customer",
                external_id=key,
                revision=1,
                payload=seed_customer_payload(index),
                effective_at=stamp,
                observed_at=stamp,
            )
        )
    for index in range(24):
        key = f"INV-{101 + index}"
        balance = 0 if index and index % 6 == 0 else 5000 if index % 6 == 2 else 10000
        money = {"minor_units": 10000, "currency": "AUD", "exponent": 2}
        db.add(
            DemoSourceRecord(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection_id,
                organisation_id=connection.external_account_id,
                kind="invoice",
                external_id=key,
                revision=1,
                payload=seed_invoice_payload(index, stamp),
                effective_at=stamp,
                observed_at=stamp,
            )
        )
        if balance < 10000:
            db.add(
                DemoSourceRecord(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=connection_id,
                    organisation_id=connection.external_account_id,
                    kind="payment",
                    external_id=f"seed-{key}/1",
                    revision=1,
                    payload={
                        "external_payment_id": f"seed-{key}",
                        "external_allocation_id": "1",
                        "external_invoice_id": key,
                        "kind": "cash",
                        "amount": {**money, "minor_units": 10000 - balance},
                        "parent_payment_id": None,
                        "parent_allocation_id": None,
                    },
                    effective_at=stamp,
                    observed_at=stamp,
                )
            )
    db.add(
        DemoSourceRecord(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection_id,
            organisation_id=connection.external_account_id,
            kind="scenario_input",
            external_id="future-stories",
            revision=1,
            payload={
                "synthetic": True,
                "dispute_invoice": "INV-102",
                "promise_invoice": "INV-101",
                "operational_cases_or_promises_created": False,
            },
            effective_at=stamp,
            observed_at=stamp,
        )
    )
    policy = await db.scalar(select(CollectionPolicy).where(CollectionPolicy.tenant_id == ctx.tenant_id))
    if policy is None:
        policy = CollectionPolicy(tenant_id=ctx.tenant_id, name="Synthetic draft", active=False)
        db.add(policy)
        await db.flush()
        db.add(
            CollectionPolicyVersion(
                tenant_id=ctx.tenant_id,
                policy_id=policy.id,
                state="DRAFT",
                synthetic=True,
                definition={
                    "reminder_offsets_days": [-3, 3, 10] if variant == "A" else [-1, 5, 14],
                    "customer_24h_cap": 1,
                    "customer_7d_cap": 3,
                    "timezone": "Australia/Melbourne",
                    "approval_required": True,
                    "synthetic_variant": variant,
                },
            )
        )
    await db.flush()
    await AccountingService(db, DemoAccountingAdapter(db, connection_id)).sync_accounting(ctx, None)
    await db.run_sync(
        lambda store: IdentityService(store).audit(
            ctx, "accounting.synthetic_seeded", connection_id, {"synthetic": True}
        )
    )
    return connection_id


async def simulate_payment(
    db: AsyncSession, ctx: TenantContext, cmd: DemoPaymentCommand, app_env: str
) -> EventRef:
    await demo_guard(db, ctx, app_env, Permission.DEMO_PAYMENT)
    adapter = DemoAccountingAdapter(db, cmd.provider_connection_id)
    connection = await adapter.connection(ctx)
    operation = await db.scalar(
        select(DemoPaymentOperation).where(
            DemoPaymentOperation.tenant_id == ctx.tenant_id,
            DemoPaymentOperation.provider_connection_id == connection.id,
            DemoPaymentOperation.operation_id == cmd.operation_id,
        )
    )
    if operation is not None:
        if operation.command != cmd.model_dump(mode="json"):
            raise failure()
        return ReconciliationResult.model_validate_json(__import__("json").dumps(operation.result))
    invoice = (await adapter.get_invoice(ctx, cmd.external_invoice_id)).value
    if (
        not invoice.stamp.synthetic
        or not invoice.verified
        or invoice.status not in {"open", "partially_paid"}
        or cmd.amount.currency != invoice.outstanding.currency
        or cmd.amount.exponent != invoice.outstanding.exponent
        or cmd.amount.minor_units > invoice.outstanding.minor_units
    ):
        raise failure()
    stamp = now()
    balance = invoice.outstanding.minor_units - cmd.amount.minor_units
    payload = invoice.model_dump(mode="json", exclude={"stamp", "organisation_id"})
    payload["outstanding"] = cmd.amount.model_copy(update={"minor_units": balance}).model_dump(mode="json")
    payload["status"] = "paid" if balance == 0 else "partially_paid"
    payload["source_status"] = str(payload["status"]).upper()
    db.add(
        DemoSourceRecord(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection.id,
            organisation_id=invoice.organisation_id,
            kind="invoice",
            external_id=invoice.external_id,
            revision=int(invoice.stamp.revision or "0") + 1,
            payload=payload,
            effective_at=stamp,
            observed_at=stamp,
        )
    )
    db.add(
        DemoSourceRecord(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection.id,
            organisation_id=invoice.organisation_id,
            kind="payment",
            external_id=f"{cmd.operation_id}/1",
            revision=1,
            payload={
                "external_payment_id": str(cmd.operation_id),
                "external_allocation_id": "1",
                "external_invoice_id": invoice.external_id,
                "kind": "cash",
                "amount": cmd.amount.model_dump(mode="json"),
                "parent_payment_id": None,
                "parent_allocation_id": None,
            },
            effective_at=stamp,
            observed_at=stamp,
        )
    )
    await db.flush()
    event = AccountingDataChanged(
        provider_connection_id=connection.id,
        organisation_id=invoice.organisation_id,
        external_invoice_id=invoice.external_id,
        external_event_id=f"demo-payment:{cmd.operation_id}",
    )
    result = await AccountingService(db, adapter).apply_accounting_change(ctx, event)
    db.add(
        DemoPaymentOperation(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection.id,
            operation_id=cmd.operation_id,
            command=cmd.model_dump(mode="json"),
            result=result.model_dump(mode="json"),
        )
    )
    await db.flush()
    return result


async def prepare_demo_tenants(
    db: AsyncSession, owner_id: UUID, operator_id: UUID, app_env: str
) -> list[UUID]:
    """Trusted local setup only; IDs must come from actual verified identity-provider users.

    This is not an HTTP command or authentication fallback. It never creates sessions,
    changes an existing membership or grants outbound/activation authority.
    """
    from uuid import uuid5

    from aco.core.types import Role
    from aco.features.identity.models import Membership, Tenant, User

    owner, operator = await db.get(User, owner_id), await db.get(User, operator_id)
    if (
        app_env not in {"local", "demo"}
        or owner is None
        or operator is None
        or not (owner.email_verified and operator.email_verified and owner.issuer == operator.issuer)
    ):
        raise HTTPException(403, "VERIFIED_DEMO_IDENTITIES_REQUIRED")
    result = []
    for variant in ("A", "B"):
        tenant_id = uuid5(owner_id, f"aco-synthetic-{variant}")
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            tenant = Tenant(
                id=tenant_id,
                name=f"Synthetic Example Wholesaler {variant}",
                timezone="Australia/Melbourne",
                is_demo=True,
                automation_enabled=False,
                outbound_paused=True,
            )
            db.add(tenant)
            await db.flush()
            db.add_all(
                [
                    Membership(tenant_id=tenant_id, user_id=owner_id, role="owner"),
                    Membership(
                        tenant_id=tenant_id,
                        user_id=operator_id,
                        role="operator" if variant == "A" else "viewer",
                    ),
                ]
            )
            await db.flush()
        ctx = TenantContext(tenant_id=tenant_id, actor_id=owner_id, role=Role.OWNER, correlation_id=uuid4())
        await demo_guard(db, ctx, app_env, Permission.SETTINGS)
        connection_id = uuid5(tenant_id, "primary-accounting")
        connection = await db.get(ProviderConnection, connection_id)
        if connection is None:
            db.add(
                ProviderConnection(
                    id=connection_id, tenant_id=tenant_id, provider="demo", provider_type="accounting"
                )
            )
            await db.flush()
        result.append(tenant_id)
    return result


async def seed_demo_tenants(db: AsyncSession, owner_id: UUID, operator_id: UUID, app_env: str) -> list[UUID]:
    """Atomic financial/source/policy import, after explicit committed setup scaffolding."""
    from uuid import uuid5

    from aco.core.types import Role
    from aco.features.identity.models import User

    owner, operator = await db.get(User, owner_id), await db.get(User, operator_id)
    if (
        owner is None
        or operator is None
        or not (owner.email_verified and operator.email_verified and owner.issuer == operator.issuer)
    ):
        raise HTTPException(403, "VERIFIED_DEMO_IDENTITIES_REQUIRED")
    result = []
    for variant in ("A", "B"):
        tenant_id = uuid5(owner_id, f"aco-synthetic-{variant}")
        ctx = TenantContext(tenant_id=tenant_id, actor_id=owner_id, role=Role.OWNER, correlation_id=uuid4())
        await seed_connection(db, ctx, uuid5(tenant_id, "primary-accounting"), app_env, variant)
        result.append(tenant_id)
    return result
