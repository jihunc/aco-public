"""Removing the producer bridge must lose observed attempts and fail these tests."""

import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

from accounting_support import context, seed
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select, text

from aco.features.identity.models import IdentityProviderEvent
from aco.features.usage.models import ProviderUsageEvent, UsageRevision


def test_identity_attempts_same_subject_and_pending_finalization() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        port = cast(FastAPI, client.app).state.identity
        one, two, correlation = uuid4(), uuid4(), uuid4()
        port.operation_event(store, "rotate", one, correlation, "PENDING", "same-subject")
        store.commit()
        port.operation_event(store, "rotate", one, correlation, "VERIFIED", "same-subject")
        port.operation_event(store, "rotate", two, uuid4(), "VERIFIED", "same-subject")
        store.commit()
        saved = store.get(IdentityProviderEvent, one)
        assert saved is not None and saved.outcome == "VERIFIED"
        events = list(store.scalars(select(ProviderUsageEvent).where(ProviderUsageEvent.id.in_([one, two]))))
        assert len(events) == 2
        assert all(row.tenant_id is None and row.native_operation_id is None for row in events)
        revisions = list(
            store.scalars(
                select(UsageRevision).where(UsageRevision.event_id == one).order_by(UsageRevision.version)
            )
        )
        assert [row.outcome for row in revisions] == ["PENDING", "VERIFIED"]
        assert all(row.currency is None and row.cost_amount is None for row in revisions)
        assert client.get(f"/api/workspaces/{tenant}/usage").json()["currencies"] == []


def test_accounting_all_operations_survive_business_rollback() -> None:
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        ctx = context(tenant, user.id)

        async def exercise() -> Any:
            try:
                async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                    adapter = DemoAccountingAdapter(db, connection)
                    await adapter.list_customers(ctx)
                    await adapter.list_invoices(ctx)
                    await adapter.get_change_feed(ctx)
                    await adapter.get_invoice(ctx, "INV-101")
                    await adapter.list_payments(ctx, "INV-101")
                    raise ValueError("failed business transaction")
            except ValueError:
                pass

        asyncio.run(exercise())
        events = list(
            store.scalars(
                select(ProviderUsageEvent).where(
                    ProviderUsageEvent.tenant_id == tenant,
                    ProviderUsageEvent.correlation_id == ctx.correlation_id,
                )
            )
        )
        assert {e.operation for e in events} == {
            "list_customers",
            "list_invoices",
            "get_change_feed",
            "get_invoice",
            "list_payments",
        }
        assert len(events) == 5
        for event in events:
            revision = store.scalar(select(UsageRevision).where(UsageRevision.event_id == event.id))
            assert revision is not None
            assert revision.cost_amount is None and revision.cost_quality == "UNKNOWN"


def test_tenant_lock_preserves_serialization_and_permits_independent_fk() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.dialects import postgresql

    from aco.features.identity.models import Tenant
    from aco.features.identity.service import IdentityService, Permission

    with actor_client("owner") as (client, store, user, tenant):
        ctx = context(tenant, user.id)
        statement = select(Tenant).with_for_update(key_share=True)
        assert "FOR NO KEY UPDATE" in str(statement.compile(dialect=cast(Any, postgresql.dialect)()))
        IdentityService(store).authorize(ctx, Permission.SETTINGS)

        def other_authorization() -> Any:
            with cast(FastAPI, client.app).state.sessions.begin() as other:
                other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                IdentityService(other).authorize(ctx, Permission.SETTINGS)

        def independent_reference() -> Any:
            from aco.features.identity.models import ProviderConnection

            with cast(FastAPI, client.app).state.sessions.begin() as other:
                other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                other.add(ProviderConnection(tenant_id=tenant, provider="mock", provider_type="llm"))

        with ThreadPoolExecutor(1) as executor:
            import pytest
            from sqlalchemy.exc import OperationalError

            with pytest.raises(OperationalError):
                executor.submit(other_authorization).result(timeout=3)
            executor.submit(independent_reference).result(timeout=3)
        store.rollback()


def test_failed_identity_discovery_and_validation_preserve_attempt(monkeypatch: Any) -> None:
    import pytest
    from starlette.requests import Request

    from aco.core.types import ProviderErrorCode
    from aco.integrations.ports.identity import IdentityFailure

    with actor_client("owner") as (client, store, user, tenant):
        port = cast(FastAPI, client.app).state.identity

        def unavailable() -> Any:
            raise port.failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)

        monkeypatch.setattr(port, "metadata", unavailable)
        for operation in ("begin_login", "validate_connection"):
            with pytest.raises(IdentityFailure):
                asyncio.run(
                    getattr(port, operation)(Request({"type": "http", "method": "GET", "headers": []}))
                    if operation == "begin_login"
                    else getattr(port, operation)()
                )
            store.expire_all()
            rows = list(
                store.scalars(
                    select(IdentityProviderEvent).where(IdentityProviderEvent.operation == operation)
                )
            )
            assert len(rows) == 1
            revisions = list(
                store.scalars(
                    select(UsageRevision)
                    .where(UsageRevision.event_id == rows[0].id)
                    .order_by(UsageRevision.version)
                )
            )
            assert [r.outcome for r in revisions] == ["PENDING", "TEMPORARILY_UNAVAILABLE"]
            assert rows[0].native_id is None


def test_uncommitted_connection_denies_before_source_call() -> None:
    from fastapi import HTTPException

    from aco.features.identity.models import ProviderConnection
    from aco.features.usage.accounting import ObservedAccounting

    with actor_client("owner") as (client, store, user, tenant):
        ctx = context(tenant, user.id)
        calls = []

        class Source:
            async def list_customers(self, ctx: Any, cursor: Any) -> Any:
                calls.append("called")
                raise AssertionError("must not call")

        async def run() -> Any:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                connection = ProviderConnection(tenant_id=tenant, provider="mock", provider_type="accounting")
                db.add(connection)
                await db.flush()
                import pytest

                with pytest.raises(HTTPException, match="COMMITTED_PROVIDER_SETUP_REQUIRED"):
                    await ObservedAccounting(db, cast(Any, Source()), connection.id).list_customers(ctx)

        asyncio.run(run())
        assert calls == []


def test_local_cookie_failure_retains_identity_audit_without_provider_charge() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        client.cookies.clear()
        client.cookies.set("aco_session", "invalid-cookie")
        response = client.get("/api/me")
        assert response.status_code == 401
        events = list(store.scalars(select(IdentityProviderEvent)))
        assert len(events) == 1 and events[0].outcome == "AUTH_EXPIRED"
        assert store.scalar(select(ProviderUsageEvent.id)) is None


def test_refresh_and_logout_discovery_failure_keeps_durable_intent(monkeypatch: Any) -> None:
    from aco.core.types import ProviderErrorCode
    from aco.features.identity.models import LoginSession

    for route, operation in (("refresh", "rotate"), ("logout", "logout")):
        with actor_client("owner") as (client, store, user, tenant):
            port = cast(FastAPI, client.app).state.identity

            def unavailable(port: Any = port, operation: str = operation) -> Any:
                with port.sessions() as evidence:
                    intent = evidence.scalar(
                        select(IdentityProviderEvent).where(IdentityProviderEvent.operation == operation)
                    )
                    assert (
                        intent is not None
                        and intent.provider_attempted is True
                        and intent.outcome == "PENDING"
                    )
                raise port.failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)

            monkeypatch.setattr(port, "metadata", unavailable)
            response = client.post(f"/api/auth/{route}")
            assert response.status_code == (200 if route == "logout" else 503)
            event = store.scalar(
                select(IdentityProviderEvent).where(IdentityProviderEvent.operation == operation)
            )
            assert event is not None and event.provider_attempted is True
            revisions = list(
                store.scalars(
                    select(UsageRevision)
                    .where(UsageRevision.event_id == event.id)
                    .order_by(UsageRevision.version)
                )
            )
            assert len(revisions) == 2 and revisions[0].outcome == "PENDING"
            assert all(r.cost_amount is None for r in revisions)
            session = store.scalar(select(LoginSession).where(LoginSession.user_id == user.id))
            assert session is not None and session.revoked
            assert client.get("/api/me").status_code == 401
