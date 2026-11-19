"""Actual routes, source adapter/imports and PostgreSQL; no real-provider calls or impact claims."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from accounting_support import apply, context, publish_invoice, seed
from case_promise_support import communication
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from identity_support import actor_client
from sqlalchemy import func, select

from aco.core.clock import FrozenClock
from aco.core.types import CostQuality, ProviderCost
from aco.features.accounting.models import (
    AccountingEvent,
    AccountingObservation,
    DemoSourceRecord,
    InvoiceSnapshot,
    VerifiedPaymentAllocation,
)
from aco.features.agent.schemas import AgentTrigger
from aco.features.agent.service import AgentService
from aco.features.cases.dto import CommunicationEvidence, UpsertCase
from aco.features.cases.service import CaseService
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.impact.api import router
from aco.features.impact.dto import ImpactQuery
from aco.features.impact.service import ImpactQueries
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.models import ProviderUsageEvent, UsageRevision
from aco.features.usage.service import UsageService


def read(client: TestClient, tenant: UUID, since: str, until: str) -> dict[str, Any]:
    response = client.get(f"/api/workspaces/{tenant}/impact", params={"since": since, "until": until})
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def test_actual_source_payment_readonly_report_and_genuine_activity_records() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        app = cast(FastAPI, client.app)
        app.include_router(router)
        seed(client, tenant)
        invoice = store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.number == "INV-101"
            )
        )
        assert invoice is not None
        start = now()
        command = {
            "provider_connection_id": str(invoice.provider_connection_id),
            "external_invoice_id": invoice.external_invoice_id,
            "operation_id": str(uuid4()),
            "amount": {"minor_units": 2500, "currency": "AUD", "exponent": 2},
        }
        assert (
            client.post(
                f"/api/workspaces/{tenant}/demo/accounting/simulate-payment", json=command
            ).status_code
            == 200
        )
        ctx = context(tenant, user.id)
        message = communication(
            store, tenant, [invoice], "Synthetic report fixture: rate dispute remains open."
        )
        case = CaseService(store).upsert_case(
            ctx,
            UpsertCase(
                customer_id=invoice.customer_id,
                issue_type="wrong_amount",
                claim_key="impact-case-fixture",
                substance="Synthetic report fixture dispute",
                invoice_ids=[invoice.id],
                evidence=CommunicationEvidence(
                    communication_id=message.id,
                    excerpt="Synthetic report fixture: rate dispute remains open.",
                    offset=0,
                ),
            ),
        )
        run = AgentService(store).start(
            ctx,
            AgentTrigger(
                invoice_ids=[invoice.id],
                objective="Synthetic reporting fixture: inspect source.",
                request_id=uuid4(),
            ),
        )
        store.commit()
        end = now()
        models = (
            AccountingEvent,
            AccountingObservation,
            DemoSourceRecord,
            VerifiedPaymentAllocation,
            ProviderUsageEvent,
            UsageRevision,
        )
        before = [store.scalar(select(func.count()).select_from(model)) for model in models]
        result = read(client, tenant, start.isoformat(), end.isoformat())
        assert result["tenant_id"] == str(tenant)
        assert result["cash"][0]["net_cash"]["minor_units"] == "2500"
        assert result["cash"][0]["completeness"] == "partial"
        assert result["operations"] == client.get(f"/api/workspaces/{tenant}/overview").json() | {
            "as_of_at": result["operations"]["as_of_at"]
        }
        assert any(
            row["id"] == str(run.id) and row["status"] == "queued" and row["synthetic"]
            for row in result["recent_activity"]
        )
        assert any(
            row["id"] == str(case.case.id) and row["status"] == "OPEN" for row in result["recent_activity"]
        )
        assert result["usage"]["currencies"][0]["unknown_count"] > 0
        assert all("reason" in metric for metric in result["unavailable"])
        observed = next(metric for metric in result["unavailable"] if "observed-cohort" in metric["metric"])
        assert "Frozen observed-cohort results" in observed["reason"]
        assert "fixed illustrative synthetic cohorts" in observed["reason"]
        assert "causal impact" in observed["reason"]
        assert "matched cohorts are unavailable" not in observed["reason"]
        handling = next(
            metric for metric in result["unavailable"] if "unbound approvals" in metric["metric"]
        )
        assert "separate case-history projection" in handling["reason"]
        assert "do not establish measured handling" in handling["reason"]
        assert "exact immutable case/action links" in handling["reason"]
        assert "Unbound historical approvals remain unattributed" in handling["reason"]
        economics = next(metric for metric in result["unavailable"] if "ROI" in metric["metric"])
        assert "Owner-reviewed versioned assumptions" in economics["reason"]
        assert "missing provider billing" in economics["reason"]
        assert "measured handling" in economics["reason"]
        assert "same-currency costs" in economics["reason"]
        after_http = [store.scalar(select(func.count()).select_from(model)) for model in models]
        assert after_http[:4] == before[:4]

        # Authenticated HTTP introspection itself records genuine identity usage. The
        # report service adds no provider invocation or usage/source mutation.
        async def direct_read() -> None:
            async with app.state.business_sessions.begin() as db:
                await ImpactQueries(db).read(ctx, ImpactQuery(since=start, until=end))

        asyncio.run(direct_read())
        assert [store.scalar(select(func.count()).select_from(model)) for model in models] == after_http


def test_actual_source_linked_refund_uses_own_window_and_excludes_noncash() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        app = cast(FastAPI, client.app)
        app.include_router(router)
        seed(client, tenant)
        invoice = store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.number == "INV-101"
            )
        )
        assert invoice is not None
        operation = uuid4()
        assert (
            client.post(
                f"/api/workspaces/{tenant}/demo/accounting/simulate-payment",
                json={
                    "provider_connection_id": str(invoice.provider_connection_id),
                    "external_invoice_id": invoice.external_invoice_id,
                    "operation_id": str(operation),
                    "amount": {"minor_units": 2500, "currency": "AUD", "exponent": 2},
                },
            ).status_code
            == 200
        )
        start = now()
        for kind, amount in (("refund", -1000), ("credit", 500), ("write_off", 200)):
            at = now()
            store.add(
                DemoSourceRecord(
                    tenant_id=tenant,
                    provider_connection_id=invoice.provider_connection_id,
                    organisation_id=invoice.organisation_id,
                    kind="payment",
                    external_id=f"impact-{kind}/1",
                    revision=1,
                    payload={
                        "external_payment_id": f"impact-{kind}",
                        "external_allocation_id": "1",
                        "external_invoice_id": invoice.external_invoice_id,
                        "kind": kind,
                        "amount": {"minor_units": amount, "currency": "AUD", "exponent": 2},
                        "parent_payment_id": str(operation) if kind == "refund" else None,
                        "parent_allocation_id": "1" if kind == "refund" else None,
                    },
                    effective_at=at,
                    observed_at=at,
                )
            )
        store.commit()
        event = publish_invoice(
            store,
            tenant,
            invoice.provider_connection_id,
            outstanding={"minor_units": 7800, "currency": "AUD", "exponent": 2},
            status="partially_paid",
        )
        apply(app, context(tenant, user.id), event)
        result = read(client, tenant, start.isoformat(), now().isoformat())
        cash = result["cash"][0]
        assert cash["net_cash"]["minor_units"] == "-1000"
        assert cash["non_cash_count"] == 2 and cash["allocation_count"] == 1
        assert cash["cash"]["minor_units"] == "0"
        assert cash["outflows"]["minor_units"] == "1000"
        assert cash["evidence"][0]["parent_id"] is not None


def test_current_membership_strict_window_and_stale_basis() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        app = cast(FastAPI, client.app)
        app.include_router(router)
        seed(client, tenant)
        start, end = now() - timedelta(days=1), now()
        path = f"/api/workspaces/{tenant}/impact"
        query = {"since": start.isoformat(), "until": end.isoformat()}
        for changes in (
            {"since": "2026-11-01T00:00:00"},
            {"until": start.isoformat()},
            {"until": (end + timedelta(days=1)).isoformat()},
            {"as_of": "2020-01-01"},
            {"role": "owner"},
        ):
            assert client.get(path, params=query | changes).status_code == 422
        assert client.get(f"/api/workspaces/{uuid4()}/impact", params=query).status_code == 403
        member = store.scalar(select(Membership).where(Membership.tenant_id == tenant))
        assert member is not None
        member.role = "viewer"
        store.commit()
        assert client.get(path, params=query).status_code == 200

        async def stale() -> Any:
            async with app.state.business_sessions.begin() as db:
                report = await ImpactQueries(db, FrozenClock(end + timedelta(hours=1))).read(
                    context(tenant, user.id), ImpactQuery(since=start, until=end)
                )
                assert report.cash and report.cash[0].freshness == "stale"
                assert report.operations.currencies[0].freshness == "stale"
                with pytest.raises(HTTPException, match="HUMAN_CONTEXT_REQUIRED"):
                    await ImpactQueries(db).read(cast(Any, object()), ImpactQuery(since=start, until=end))

        asyncio.run(stale())
        connection = store.scalar(select(ProviderConnection).where(ProviderConnection.tenant_id == tenant))
        assert connection is not None
        connection.status = "DISCONNECTED"
        store.commit()
        disconnected = read(client, tenant, start.isoformat(), end.isoformat())
        assert disconnected["source_warnings"] and disconnected["cash"][0]["freshness"] == "unknown"
        member.active = False
        store.commit()
        assert client.get(path, params=query, headers={"x-role": "owner"}).status_code == 403


def test_usage_window_selects_identity_creation_and_preserves_latest_exact_reconciliation() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        cast(FastAPI, client.app).include_router(router)
        connection = ProviderConnection(
            tenant_id=tenant, provider="mock", provider_type="llm", status="READY"
        )
        store.add(connection)
        store.commit()
        ctx = context(tenant, user.id)
        start = now()
        command = ProviderUsageInput(
            provider_connection_id=connection.id,
            operation_key="impact-window-cost",
            component="provider_total",
            operation="investigate",
            provider_kind="llm",
            provider="mock",
            synthetic=True,
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
        )
        event = UsageService(store).record_usage(ctx, command)
        store.commit()
        end = now()
        reported = command.model_copy(
            update={
                "reconciliation_key": "later-provider-reconciliation",
                "cost": ProviderCost(
                    amount=Decimal("0.000000000003"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        UsageService(store).record_usage(ctx, reported)
        store.commit()
        latest = store.scalar(
            select(UsageRevision)
            .where(UsageRevision.event_id == event.id)
            .order_by(UsageRevision.version.desc())
        )
        assert latest is not None and latest.created_at >= end
        report = read(client, tenant, start.isoformat(), end.isoformat())
        usd = next(row for row in report["usage"]["currencies"] if row["currency"] == "USD")
        assert usd["reported_amount"] == "0.000000000003" and usd["estimated_amount"] == "0"
        assert usd["synthetic"] and usd["unknown_count"] == 0
        assert (
            "identities created" in report["usage_basis"]
            and "current latest revisions" in report["usage_basis"]
        )
