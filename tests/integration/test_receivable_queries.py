"""Read-only product consumers against real PostgreSQL and canonical source imports."""

from typing import Any, cast
from uuid import UUID, uuid4

from accounting_support import apply, context, publish_invoice, seed
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.identity.models import Membership, ProviderConnection, Tenant


def operations(client: Any, tenant: UUID) -> dict[str, Any]:
    response = client.get(f"/api/workspaces/{tenant}/overview")
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_every_read_route_rejects_unknown_queries_without_changing_authority_or_facts() -> None:
    # A silently ignored historical/authority parameter can make current detail look historical.
    from aco.features.accounting.models import (
        AccountingEvent,
        AccountingObservation,
        CollectionRecord,
        DemoSourceRecord,
        InvoiceSourceRevision,
        VerifiedPaymentAllocation,
    )

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        invoice = store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant))
        member = store.scalar(select(Membership).where(Membership.tenant_id == tenant))
        assert invoice is not None and member is not None
        other = Tenant(name="Other query-validation fixture", timezone="UTC")
        store.add(other)
        store.flush()
        other_member = Membership(tenant_id=other.id, user_id=user.id, role="owner")
        store.add(other_member)
        store.add(ProviderConnection(tenant_id=other.id, provider="demo", provider_type="accounting"))
        store.commit()
        seed(client, other.id)
        foreign = store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == other.id))
        assert foreign is not None
        paths = (
            "/overview",
            "/receivables",
            f"/receivables/{invoice.id}",
            "/customers",
            f"/customers/{invoice.customer_id}",
        )
        models = (
            DemoSourceRecord,
            InvoiceSnapshot,
            InvoiceSourceRevision,
            VerifiedPaymentAllocation,
            AccountingEvent,
            AccountingObservation,
            CollectionRecord,
        )

        def facts() -> dict[str, list[dict[str, Any]]]:
            return {
                model.__tablename__: [
                    dict(row) for row in store.execute(select(model.__table__).order_by(model.id)).mappings()
                ]
                for model in models
            }

        before = facts()
        base = f"/api/workspaces/{tenant}"
        for path in paths:
            assert client.get(base + path).status_code == 200, path
        member.role = "viewer"
        other_member.active = False
        store.commit()
        rejected = {}
        for path in paths:
            assert client.get(base + path).status_code == 200, path
            for key, value in (
                ("role", "owner"),
                ("tenant", str(other.id)),
                ("as_of", "2020-01-01"),
                ("unexpected", "ignored-must-not-mean-success"),
            ):
                response = client.get(base + path, params={key: value})
                rejected[f"{path}?{key}"] = response.status_code
                if response.status_code == 422:
                    assert response.json()["detail"][0]["loc"] == ["query", key]
            assert (
                client.get(
                    f"/api/workspaces/{other.id}" + path,
                    headers={"x-role": "owner", "x-tenant-id": str(tenant)},
                ).status_code
                == 403
            ), path
        assert client.get(base + f"/receivables/{foreign.id}").status_code == 404
        assert client.get(base + f"/customers/{foreign.customer_id}").status_code == 404
        member.active = False
        store.commit()
        for path in paths:
            assert client.get(base + path, headers={"x-role": "owner"}).status_code == 403, path
        store.expire_all()
        assert facts() == before  # Includes read observations: no implicit sync or financial mutation.
        assert rejected == dict.fromkeys(rejected, 422), rejected


def test_operations_metrics_reconcile_per_currency_and_as_of() -> None:
    # Catch hardcoded metrics, duplicated joins, implicit FX and UI-only payment changes.
    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        before = operations(client, tenant)
        aud = before["currencies"][0]
        assert aud["outstanding"] == {"minor_units": "190000", "currency": "AUD", "exponent": 2}
        assert aud["aging"]["total"] == "190000"
        assert int(aud["overdue"]["minor_units"]) > 0
        assert before["as_of_at"].endswith("Z")
        assert "mark_paid" not in before["allowed_actions"]
        event = publish_invoice(
            store,
            tenant,
            connection,
            outstanding={"minor_units": 10000, "currency": "USD", "exponent": 2},
            original={"minor_units": 10000, "currency": "USD", "exponent": 2},
        )
        apply(client.app, context(tenant, user.id), event)
        separated = operations(client, tenant)
        assert {
            c["outstanding"]["currency"]: c["outstanding"]["minor_units"] for c in separated["currencies"]
        } == {"AUD": "180000", "USD": "10000"}
        payment = client.post(
            f"/api/workspaces/{tenant}/demo/accounting/simulate-payment",
            json={
                "provider_connection_id": str(connection),
                "external_invoice_id": "INV-102",
                "operation_id": str(uuid4()),
                "amount": {"minor_units": 3000, "currency": "AUD", "exponent": 2},
            },
        )
        assert payment.status_code == 200, payment.text
        after = operations(client, tenant)["currencies"][0]
        assert after["outstanding"]["minor_units"] == "177000"
        assert int(after["overdue"]["minor_units"]) == int(aud["overdue"]["minor_units"]) - 13000
        assert after["aging"]["total"] == "177000"


def test_read_filters_pagination_customer_detail_precision_and_no_writes() -> None:
    # Catch missing server filters, unsafe numeric JSON, cross-customer detail or financial writes.
    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        event = publish_invoice(
            store,
            tenant,
            connection,
            original={"minor_units": 9007199254740993, "currency": "AUD", "exponent": 2},
            outstanding={"minor_units": 9007199254740993, "currency": "AUD", "exponent": 2},
        )
        apply(client.app, context(tenant, user.id), event)
        base = f"/api/workspaces/{tenant}"
        page = client.get(base + "/receivables?page_size=5").json()
        assert page["total"] == 24 and len(page["items"]) == 5
        page2 = client.get(base + "/receivables?page=2&page_size=5").json()
        assert {i["id"] for i in page["items"]}.isdisjoint(i["id"] for i in page2["items"])
        found = client.get(
            base + "/receivables?search=INV-101&currency=AUD&min_minor=9007199254740993"
        ).json()
        assert found["total"] == 1
        invoice = found["items"][0]
        assert invoice["outstanding"]["minor_units"] == "9007199254740993"
        assert invoice["financial_status"] == "open" and invoice["collection_lifecycle"] == "active"
        assert invoice["source"]["revision"] == "2" and invoice["last_synced_at"]
        assert client.get(base + "/receivables?financial_status=paid").json()["total"] == 3
        assert client.get(base + "/receivables?collection_state=closed").json()["total"] == 3
        assert client.get(base + "/receivables?stale=false").json()["total"] == 24
        assert client.get(base + "/receivables?stale=true").json()["total"] == 0
        assert client.get(base + "/receivables?max_minor=0").status_code == 422  # currency required
        assert client.get(base + "/receivables?currency=AUD&max_minor=0").json()["total"] == 3
        assert client.get(base + "/receivables?search=not-present").json()["items"] == []
        assert client.get(base + "/receivables?search=%25").json()["total"] == 0
        assert (
            client.get(base + "/receivables?currency=AUD&min_minor=999999999999999999999999999999").json()[
                "total"
            ]
            == 0
        )
        assert (
            client.get(base + "/receivables?currency=AUD&max_minor=-999999999999999999999999999999").json()[
                "total"
            ]
            == 0
        )
        assert (
            client.get(base + "/receivables?currency=AUD&min_minor=-999999999999999999999999999999").json()[
                "total"
            ]
            == 24
        )
        customers = client.get(base + "/customers?search=Trading%201").json()
        assert customers["total"] == 1
        customer = customers["items"][0]
        assert customer["source"]["external_id"] == "CUSTOMER-1"
        assert customer["source"]["observed_at"] and customer["source"]["synthetic"]
        assert client.get(base + f"/customers/{customer['id']}").json()["customer"]["id"] == customer["id"]
        assert client.get(base + f"/receivables?customer_id={customer['id']}").json()["total"] == 4
        detail = client.get(base + f"/receivables/{invoice['id']}").json()
        assert detail["invoice"]["outstanding"]["minor_units"] == "9007199254740993"
        assert detail["sections"]["communications"] == "available"
        assert (
            client.patch(
                base + f"/receivables/{invoice['id']}", json={"financial_status": "paid"}
            ).status_code
            == 405
        )
        schema = client.get("/openapi.json").json()
        money_schema = schema["components"]["schemas"]["MoneyJSON"]
        assert money_schema["properties"]["minor_units"]["type"] == "string"


def test_viewer_read_current_membership_and_foreign_ids_denied() -> None:
    # Catch coupling read to financial sync and trusting URL/header/body roles.
    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        row = store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant))
        assert row is not None
        member = store.scalar(select(Membership).where(Membership.tenant_id == tenant))
        assert member is not None
        member.role = "viewer"
        other = Tenant(name="Other", timezone="UTC")
        store.add(other)
        store.commit()
        base = f"/api/workspaces/{tenant}"
        assert client.get(base + "/receivables").status_code == 200
        assert client.get(base + "/overview").status_code == 200
        assert (
            client.post(
                base + "/accounting/sync", json={"provider_connection_id": str(row.provider_connection_id)}
            ).status_code
            == 403
        )
        assert (
            client.get(f"/api/workspaces/{other.id}/receivables", headers={"x-role": "owner"}).status_code
            == 403
        )
        assert client.get(base + f"/receivables/{uuid4()}").status_code == 404
        member.active = False
        store.commit()
        assert client.get(base + "/overview").status_code == 403


def test_calendar_aging_edges_excluded_signed_facts_and_oldest_freshness() -> None:
    # Catch UTC-date aging, off-by-one bins, netted credits and latest-read freshness.
    import asyncio
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from aco.core.clock import FrozenClock
    from aco.core.types import Money
    from aco.features.identity.models import now
    from aco.features.receivables.queries import MetricQuery, ReceivableFilter, ReceivableQueries

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        tenant_row = store.get(Tenant, tenant)
        assert tenant_row is not None
        tenant_row.timezone = "Australia/Melbourne"
        store.commit()
        # All these source invoices have no existing payment allocations.
        calendar = now().astimezone(ZoneInfo("Australia/Melbourne")).date()
        for key, days, amount, status in [
            ("INV-101", 0, 100, "open"),
            ("INV-102", 1, 200, "open"),
            ("INV-104", 30, 300, "open"),
            ("INV-105", 31, 400, "open"),
            ("INV-106", 60, 500, "open"),
            ("INV-108", 61, 600, "open"),
            ("INV-110", 90, 700, "open"),
            ("INV-112", 91, 800, "open"),
            ("INV-114", 10, -70, "credited"),
            ("INV-116", 10, 90, "voided"),
            ("INV-118", 10, 80, "unknown"),
            ("INV-120", 10, 0, "paid"),
        ]:
            event = publish_invoice(
                store,
                tenant,
                connection,
                key=key,
                due_date=str(calendar - timedelta(days=days)),
                status=status,
                original={"minor_units": 1000, "currency": "JPY", "exponent": 0},
                outstanding={"minor_units": amount, "currency": "JPY", "exponent": 0},
            )
            apply(client.app, context(tenant, user.id), event)
        at = now()
        store.expire_all()
        rows = list(
            store.scalars(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.currency == "JPY"
                )
            )
        )
        reads = [r.last_read_at for r in rows if r.last_read_at is not None]

        async def inspect() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                queries = ReceivableQueries(db, FrozenClock(at))
                view = await queries.get_operations(context(tenant, user.id), MetricQuery(currency="JPY"))
                group = view.currencies[0]
                assert group.outstanding is not None and group.overdue is not None and group.aging is not None
                assert group.outstanding.minor_units == 3600
                assert group.overdue.minor_units == 3500
                assert group.aging.model_dump() == {
                    "current": "100",
                    "days_1_30": "500",
                    "days_31_60": "900",
                    "days_61_90": "1300",
                    "days_91_plus": "800",
                    "total": "3600",
                }
                assert group.excluded_count == 4 and group.excluded_balance.minor_units == 100
                assert group.excluded_by_reason == {
                    "credited": 1,
                    "voided": 1,
                    "unverified_or_unknown": 1,
                    "zero_balance": 1,
                }
                assert group.last_synced_at == min(reads) and min(reads) < max(reads)
                assert group.completeness == "incomplete"
                listing = await queries.list_receivables(
                    context(tenant, user.id), ReceivableFilter(currency="JPY", overdue=True)
                )
                assert "INV-101" not in {r.number for r in listing.items}
                # Canonical Money serialization is still integer-valued, never the wire-view string.
                assert (
                    Money(minor_units=9007199254740993, currency="AUD", exponent=2).model_dump(mode="json")[
                        "minor_units"
                    ]
                    == 9007199254740993
                )
                late = await ReceivableQueries(db, FrozenClock(at + timedelta(minutes=16))).get_operations(
                    context(tenant, user.id), MetricQuery(currency="JPY")
                )
                assert late.currencies[0].freshness == "stale"

        asyncio.run(inspect())


def test_empty_population_known_zero_and_unknown_read_are_distinct() -> None:
    # Catch empty→zero fabrication, 0/0, latest invoice certifying the whole currency and history mutation.
    import asyncio
    from datetime import timedelta

    from aco.core.clock import FrozenClock
    from aco.features.accounting.models import AccountingEvent, AccountingObservation, InvoiceSourceRevision
    from aco.features.identity.models import now
    from aco.features.receivables.queries import MetricQuery, ReceivableQueries

    with actor_client("owner") as (client, store, user, tenant):
        assert operations(client, tenant)["availability"] == "unavailable"
        assert operations(client, tenant)["currencies"] == []
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        event = publish_invoice(
            store,
            tenant,
            connection,
            status="paid",
            original={"minor_units": 100, "currency": "JPY", "exponent": 0},
            outstanding={"minor_units": 0, "currency": "JPY", "exponent": 0},
        )
        apply(client.app, context(tenant, user.id), event)
        before = {
            table.__tablename__: [r.__dict__["id"] for r in store.scalars(select(table))]
            for table in (InvoiceSourceRevision, AccountingEvent, AccountingObservation)
        }

        async def inspect() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                group = (
                    await ReceivableQueries(db).get_operations(
                        context(tenant, user.id), MetricQuery(currency="JPY")
                    )
                ).currencies[0]
                assert group.outstanding is not None and group.aging is not None
                assert group.outstanding.minor_units == 0 and group.overdue_ratio is None
                assert group.completeness == "complete" and group.aging.total == "0"
                future_read = await ReceivableQueries(
                    db, FrozenClock(now() - timedelta(hours=1))
                ).get_operations(context(tenant, user.id), MetricQuery(currency="JPY"))
                assert future_read.currencies[0].freshness == "unknown"
                assert future_read.currencies[0].completeness == "incomplete"

        asyncio.run(inspect())
        assert before == {
            table.__tablename__: [r.__dict__["id"] for r in store.scalars(select(table))]
            for table in (InvoiceSourceRevision, AccountingEvent, AccountingObservation)
        }


def test_nonterminating_ratio_and_missing_read_never_claim_complete() -> None:
    # Catch float-derived ratios, 0/0, and missing read watermarks certified as fresh.
    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        from datetime import timedelta

        from aco.features.identity.models import now

        for key, amount, due in [("INV-101", 1, -1), ("INV-102", 2, 1)]:
            event = publish_invoice(
                store,
                tenant,
                connection,
                key=key,
                due_date=str(now().date() + timedelta(days=due)),
                original={"minor_units": amount, "currency": "JPY", "exponent": 0},
                outstanding={"minor_units": amount, "currency": "JPY", "exponent": 0},
            )
            apply(client.app, context(tenant, user.id), event)
        group = next(
            g for g in operations(client, tenant)["currencies"] if g["outstanding"]["currency"] == "JPY"
        )
        assert group["outstanding"]["minor_units"] == "3" and group["overdue"]["minor_units"] == "1"
        assert group["overdue_ratio"] == "0.3333333333333333333333333333333333333333"
        row = store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.external_invoice_id == "INV-101"
            )
        )
        assert row is not None
        row.last_read_at = None  # Deliberately incomplete persisted projection; never fabricate a read.
        store.commit()
        group = next(
            g for g in operations(client, tenant)["currencies"] if g["outstanding"]["currency"] == "JPY"
        )
        assert group["last_synced_at"] is None and group["freshness"] == "unknown"
        assert group["completeness"] == "incomplete"
        assert (
            client.get(f"/api/workspaces/{tenant}/receivables?stale=true&search=INV-101").json()["total"] == 1
        )
        assert client.get(f"/api/workspaces/{tenant}/overview?as_of=2020-01-01").status_code == 422
        assert (
            client.get(f"/api/workspaces/{tenant}/receivables?tenant_id={uuid4()}&role=owner").status_code
            == 422
        )


def test_foreign_resources_are_not_found_and_reads_do_not_change_source_history() -> None:
    # Catch filtering only the workspace route, and query code mutating source or audit evidence.
    from aco.features.accounting.models import AccountingEvent, AccountingObservation, InvoiceSourceRevision

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        other = Tenant(name="Other synthetic source", timezone="UTC")
        store.add(other)
        store.flush()
        membership = Membership(tenant_id=other.id, user_id=user.id, role="owner")
        store.add(membership)
        store.add(ProviderConnection(tenant_id=other.id, provider="demo", provider_type="accounting"))
        store.commit()
        seed(client, other.id)
        row = store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == other.id))
        assert row is not None
        base = f"/api/workspaces/{tenant}"
        assert client.get(base + f"/receivables/{row.id}").status_code == 404
        assert client.get(base + f"/customers/{row.customer_id}").status_code == 404
        assert client.get(base + f"/receivables?customer_id={row.customer_id}").json()["total"] == 0
        membership.active = False
        store.commit()
        assert (
            client.get(
                f"/api/workspaces/{other.id}/overview",
                headers={"x-tenant-id": str(tenant), "x-role": "owner"},
            ).status_code
            == 403
        )
        tables = (InvoiceSourceRevision, AccountingEvent, AccountingObservation)
        before = {
            table.__tablename__: [dict(r.__dict__) for r in store.scalars(select(table).order_by(table.id))]
            for table in tables
        }
        assert client.get(base + "/overview").status_code == 200
        assert client.get(base + "/receivables").status_code == 200
        store.expire_all()
        after = {
            table.__tablename__: [dict(r.__dict__) for r in store.scalars(select(table).order_by(table.id))]
            for table in tables
        }
        # SQLAlchemy's internal state object is not a financial field.
        for values in (before, after):
            for records in values.values():
                for record in records:
                    record.pop("_sa_instance_state")
        assert before == after


def test_all_unknown_currency_has_no_invented_financial_zero() -> None:
    # Catch an empty supported basis represented as a complete zero AR amount.
    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        event = publish_invoice(
            store,
            tenant,
            connection,
            status="unknown",
            original={"minor_units": 8000, "currency": "KWD", "exponent": 3},
            outstanding={"minor_units": 8000, "currency": "KWD", "exponent": 3},
        )
        apply(client.app, context(tenant, user.id), event)
        group = next(
            g for g in operations(client, tenant)["currencies"] if g["excluded_balance"]["currency"] == "KWD"
        )
        assert group["outstanding"] is None and group["overdue"] is None and group["aging"] is None
        assert group["excluded_balance"]["minor_units"] == "8000"
        assert group["completeness"] == "incomplete"


def test_query_response_latency_measured_at_fixture_volume() -> None:
    # Measure the real DB query and authenticated route without inventing an SLA threshold.
    import asyncio
    import json
    from statistics import median
    from time import perf_counter

    from aco.features.receivables.queries import MetricQuery, ReceivableQueries

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        route_ms = []
        for _ in range(5):
            start = perf_counter()
            response = client.get(f"/api/workspaces/{tenant}/overview")
            route_ms.append((perf_counter() - start) * 1000)
            assert response.status_code == 200
            assert response.json()["currencies"][0]["outstanding"]["minor_units"] == "190000"

        async def measure() -> list[float]:
            times = []
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                for _ in range(5):
                    start = perf_counter()
                    result = await ReceivableQueries(db).get_operations(
                        context(tenant, user.id), MetricQuery()
                    )
                    times.append((perf_counter() - start) * 1000)
                    assert result.currencies[0].invoice_count == 24
            return times

        query_ms = asyncio.run(measure())
        print(
            json.dumps(
                {
                    "environment": "local PostgreSQL; synthetic identity transport; fake providers",
                    "invoices": 24,
                    "currencies": 1,
                    "samples": 5,
                    "query_ms": query_ms,
                    "query_median_ms": median(query_ms),
                    "response_ms": route_ms,
                    "response_median_ms": median(route_ms),
                    "response_bytes": len(response.content),
                    "sla_claim": False,
                }
            )
        )


def test_tenant_calendar_day_boundary_differs_from_utc_date() -> None:
    # Catch UTC calendar comparison: at 13:00 UTC Melbourne is already the following day in November.
    import asyncio
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from aco.core.clock import FrozenClock
    from aco.features.identity.models import now
    from aco.features.receivables.queries import ReceivableFilter, ReceivableQueries

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        tenant_row = store.get(Tenant, tenant)
        assert tenant_row is not None
        tenant_row.timezone = "Australia/Melbourne"
        store.commit()
        # Pick a future November boundary, independent of test wall-clock day.
        year = now().year + 1
        at = datetime(year, 11, 17, 13, 0, tzinfo=UTC)
        local_date = at.astimezone(ZoneInfo("Australia/Melbourne")).date()
        assert str(local_date) == f"{year}-11-18" and str(at.date()) == f"{year}-11-17"
        for key, due in [("INV-101", local_date), ("INV-102", local_date - timedelta(days=1))]:
            event = publish_invoice(store, tenant, connection, key=key, due_date=str(due))
            apply(client.app, context(tenant, user.id), event)

        async def inspect() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                rows = await ReceivableQueries(db, FrozenClock(at)).list_receivables(
                    context(tenant, user.id), ReceivableFilter(search="INV-10", page_size=100)
                )
                selected = {r.number: r for r in rows.items}
                assert selected["INV-101"].days_past_due == 0 and not selected["INV-101"].overdue
                assert selected["INV-102"].days_past_due == 1 and selected["INV-102"].overdue

        asyncio.run(inspect())


def test_missing_collection_projection_never_hides_accounting_invoice() -> None:
    # Catch INNER JOIN dropping financial truth when the independent operational projection is absent.
    from sqlalchemy import delete, func

    from aco.features.accounting.models import (
        AccountingEvent,
        AccountingObservation,
        CollectionRecord,
        InvoiceSourceRevision,
        VerifiedPaymentAllocation,
    )

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        invoice = store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.external_invoice_id == "INV-101"
            )
        )
        assert invoice is not None
        store.execute(delete(CollectionRecord).where(CollectionRecord.invoice_id == invoice.id))
        store.commit()
        tables = (
            InvoiceSnapshot,
            InvoiceSourceRevision,
            AccountingEvent,
            AccountingObservation,
            VerifiedPaymentAllocation,
        )
        before = {
            table.__tablename__: store.scalar(
                select(func.count()).select_from(table).where(table.tenant_id == tenant)
            )
            for table in tables
        }
        base = f"/api/workspaces/{tenant}"
        response = client.get(base + "/receivables")
        assert response.status_code == 200
        assert response.json()["total"] == 24
        detail = client.get(base + f"/receivables/{invoice.id}")
        assert detail.status_code == 200
        view = detail.json()["invoice"]
        assert view["outstanding"]["minor_units"] == "10000"
        assert view["collection_lifecycle"] is None and view["hold_reasons"] is None
        assert operations(client, tenant)["currencies"][0]["outstanding"]["minor_units"] == "190000"
        after = {
            table.__tablename__: store.scalar(
                select(func.count()).select_from(table).where(table.tenant_id == tenant)
            )
            for table in tables
        }
        assert before == after
        store.refresh(invoice)
        assert invoice.balance_minor == invoice.original_minor == 10000 and invoice.financial_status == "open"
