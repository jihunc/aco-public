"""Trusted fixed synthetic usage transitions; no HTTP scenario or spending authority."""

import sys
from decimal import Decimal
from uuid import UUID

from identity_support import database_url
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from usage_support import setup

from aco.core.types import CostQuality, ProviderCost
from aco.features.identity.models import Membership, Tenant, User, now
from aco.features.usage.budgets import BudgetService
from aco.features.usage.dto import ProviderUsageInput, UsageOutcome, UsageReservationRequest
from aco.features.usage.models import RateSnapshot
from aco.features.usage.service import UsageService


def main() -> None:
    tenant_id, user_id = (UUID(arg) for arg in sys.argv[1:])
    engine = create_engine(database_url())
    with Session(engine) as store:
        tenant = store.get(Tenant, tenant_id)
        user = store.get(User, user_id)
        membership = store.scalar(
            select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
        )
        assert tenant and tenant.is_demo and tenant.name.startswith("T06 browser ")
        assert user and membership and membership.active and membership.role == "owner"
        ctx, connection, authority, scopes = setup(store, tenant_id, user)
        for key, currency, amount in (
            ("subcent", "USD", Decimal("0.000000000003")),
            ("unknown", "USD", None),
            ("large", "AUD", Decimal("99999999999999999999999999.123456789012")),
        ):
            UsageService(store).record_usage(
                ctx,
                ProviderUsageInput(
                    provider_connection_id=connection,
                    operation_key=key,
                    component="model",
                    operation="synthetic",
                    provider_kind="llm",
                    provider="mock",
                    synthetic=True,
                    cost=ProviderCost(
                        currency=currency,
                        amount=amount,
                        quality=CostQuality.UNKNOWN if amount is None else CostQuality.PROVIDER_REPORTED,
                    ),
                ),
            )
        rate = RateSnapshot(
            tenant_id=tenant_id,
            provider="mock",
            currency="USD",
            unit="requests",
            amount=Decimal("0.000000000002"),
            price_source_at=now(),
            synthetic=True,
        )
        store.add(rate)
        store.flush()
        UsageService(store).record_usage(
            ctx,
            ProviderUsageInput(
                provider_connection_id=connection,
                operation_key="estimated",
                component="model",
                operation="synthetic",
                provider_kind="llm",
                provider="mock",
                synthetic=True,
                price_source_at=rate.price_source_at,
                cost=ProviderCost(
                    currency="USD",
                    amount=rate.amount,
                    quality=CostQuality.RATE_ESTIMATE,
                    rate_snapshot_id=rate.id,
                ),
            ),
        )
        parent_input = ProviderUsageInput(
            provider_connection_id=connection,
            operation_key="bundle-conflict",
            component="provider_total",
            operation="synthetic",
            provider_kind="llm",
            provider="mock",
            synthetic=True,
            bundle="voice",
            cost=ProviderCost(currency="EUR", amount=Decimal("0.20"), quality=CostQuality.PROVIDER_REPORTED),
        )
        usage = UsageService(store)
        parent = usage.record_usage(ctx, parent_input)
        child = usage.record_usage(
            ctx,
            parent_input.model_copy(
                update={
                    "component": "carrier",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        currency="EUR", amount=Decimal("0.08"), quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        tts = usage.record_usage(
            ctx,
            parent_input.model_copy(
                update={
                    "component": "tts",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        currency="EUR", amount=Decimal("0.08"), quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        usage.record_usage(
            ctx,
            parent_input.model_copy(
                update={
                    "reconciliation_key": "corrected",
                    "cost": ProviderCost(
                        currency="EUR", amount=Decimal("0.10"), quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        print(f"bundle parent={parent.id} carrier={child.id} tts={tts.id} corrected=0.10 each_child=0.08")
        budget = BudgetService(store)
        request = UsageReservationRequest(
            provider_connection_id=connection,
            scope_ids=scopes,
            operation_key="uncertain",
            currency="USD",
            amount=Decimal("0.06"),
        )
        reserved = budget.reserve_budget(ctx, request, authority=authority)
        budget.reconcile_reservation(
            ctx, reserved.id, UsageOutcome(key="unknown", status="UNKNOWN_SIDE_EFFECT")
        )
        blocked = budget.reserve_budget(
            ctx, request.model_copy(update={"operation_key": "blocked"}), authority=authority
        )
        assert blocked.status == "BLOCKED" and blocked.reason.startswith("CAP_REACHED:")
        store.commit()
        print(
            f"tenant={tenant_id} reservation={reserved.id} "
            f"retained={reserved.retained_amount} blocked={blocked.id}"
        )
    engine.dispose()


if __name__ == "__main__":
    main()
