"""Independent usage intent/results survive caller rollback, commit failure and producer outage."""

import asyncio
from typing import Any, cast
from uuid import UUID

import pytest
from accounting_support import context, seed
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from aco.features.usage.accounting import ObservedAccounting
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageRevision
from aco.integrations.demo_accounting.adapter import DemoAccountingSource


def test_result_outage_keeps_durable_pending_visible_after_restart(monkeypatch: Any) -> None:
    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        ctx = context(tenant, user.id)
        calls = []

        async def unavailable(*args: Any) -> Any:
            raise RuntimeError("synthetic result persistence outage")

        async def run() -> Any:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                source = DemoAccountingSource(db, connection)
                original = source.list_customers

                async def counted(ctx: Any, cursor: Any = None) -> Any:
                    calls.append("provider-request")
                    return await original(ctx, cursor)

                cast(Any, source).list_customers = counted
                adapter = ObservedAccounting(db, source, connection)
                monkeypatch.setattr(adapter, "finish", unavailable)
                await adapter.list_customers(ctx)

        with pytest.raises(RuntimeError, match="result persistence outage"):
            asyncio.run(run())
        with Session(store.get_bind()) as restarted:
            attempt = restarted.scalar(
                select(UsageAttempt).where(UsageAttempt.correlation_id == ctx.correlation_id)
            )
            assert attempt is not None
            assert attempt.outcome == "PENDING" and attempt.event_id is None
            assert attempt.business_outcome == "ROLLED_BACK"
        assert calls == ["provider-request"]
        summary = client.get(f"/api/workspaces/{tenant}/usage").json()
        assert any("Unresolved provider attempt" in notice for notice in summary["notices"])


def test_intent_failure_prevents_request_and_caller_commit_failure_keeps_result() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        ctx = context(tenant, user.id)
        calls = []

        async def run(fail_commit: Any = False) -> Any:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                db.info["test_reject_commit"] = fail_commit
                source = DemoAccountingSource(db, connection)
                original = source.list_customers

                async def counted(ctx: Any, cursor: Any = None) -> Any:
                    calls.append("request")
                    return await original(ctx, cursor)

                cast(Any, source).list_customers = counted
                await ObservedAccounting(db, source, connection).list_customers(ctx)

        def fail_intent(*args: Any) -> Any:
            raise RuntimeError("synthetic intent failure")

        event.listen(UsageAttempt, "before_insert", fail_intent)
        try:
            with pytest.raises(RuntimeError, match="intent failure"):
                asyncio.run(run())
        finally:
            event.remove(UsageAttempt, "before_insert", fail_intent)
        assert calls == []

        def fail_commit(db: Any) -> Any:
            if db.info.pop("test_reject_commit", False):
                raise RuntimeError("synthetic caller commit failure")

        event.listen(Session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="caller commit failure"):
                asyncio.run(run(True))
        finally:
            event.remove(Session, "before_commit", fail_commit)
        assert calls == ["request"]
        store.expire_all()
        attempt = store.scalar(select(UsageAttempt).where(UsageAttempt.correlation_id == ctx.correlation_id))
        assert attempt is not None
        assert attempt.outcome == "VERIFIED" and attempt.business_outcome == "ROLLED_BACK"
        revision = store.scalar(select(UsageRevision).where(UsageRevision.event_id == attempt.event_id))
        assert revision is not None
        assert revision.cost_amount is None and revision.cost_quality == "UNKNOWN"


@pytest.mark.parametrize(
    "operation", ["list_customers", "list_invoices", "get_invoice", "list_payments", "get_change_feed"]
)
def test_failed_request_id_is_not_vendor_operation_identity(operation: str) -> None:
    from aco.core.types import ProviderErrorCode, ProviderFailure
    from aco.integrations.ports.accounting import AccountingFailure

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        ctx = context(tenant, user.id)

        class FailedSource:
            pricing_currency = "AUD"

            async def unavailable(self, ctx: Any, cursor: Any) -> Any:
                raise AccountingFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                        retryable=True,
                        may_have_side_effect=False,
                        request_id="local-reference",
                    )
                )

        async def run() -> Any:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                source = FailedSource()
                setattr(source, operation, source.unavailable)
                adapter = ObservedAccounting(db, cast(Any, source), connection)
                await getattr(adapter, operation)(
                    ctx, "INV-101" if operation in {"get_invoice", "list_payments"} else None
                )

        with pytest.raises(AccountingFailure):
            asyncio.run(run())
        attempt = store.scalar(select(UsageAttempt).where(UsageAttempt.correlation_id == ctx.correlation_id))
        assert attempt is not None
        usage = store.get(ProviderUsageEvent, attempt.event_id)
        assert usage is not None
        assert attempt.error_reference == "local-reference" and usage.native_operation_id is None
        assert usage.operation_key == "attempt:" + str(attempt.id)
        assert usage.operation == operation
        revision = store.scalar(select(UsageRevision).where(UsageRevision.event_id == usage.id))
        assert revision is not None and revision.cost_amount is None and revision.currency == "AUD"


def test_unconfigured_provider_price_currency_denies_before_invocation() -> None:
    from fastapi import HTTPException

    from aco.features.identity.models import ProviderConnection

    with actor_client("owner") as (client, store, user, tenant):
        connection = ProviderConnection(
            tenant_id=tenant, provider="future-provider", provider_type="accounting"
        )
        store.add(connection)
        store.commit()
        ctx = context(tenant, user.id)
        calls: list[str] = []

        class Unpriced:
            async def list_customers(self, ctx: Any, cursor: Any) -> Any:
                calls.append("request")
                raise RuntimeError("must not invoke unconfigured provider")

        async def run() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                await ObservedAccounting(db, cast(Any, Unpriced()), connection.id).list_customers(ctx)

        with pytest.raises(HTTPException, match="PRICING_CURRENCY_REQUIRED"):
            asyncio.run(run())
        assert calls == []


def test_secondary_outcome_writer_failure_does_not_report_committed_business_as_failed(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import aco.features.usage.accounting as observed
    from aco.features.identity.models import AuditEvent
    from aco.features.identity.service import IdentityService

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        ctx = context(tenant, user.id)
        original = observed._business_outcome

        def unavailable(db: Session, outcome: str) -> None:
            if outcome == "COMMITTED" and db.info.get("usage_attempt_ids"):
                raise RuntimeError("synthetic secondary outcome outage")
            original(db, outcome)

        monkeypatch.setattr(observed, "_business_outcome", unavailable)

        async def run() -> None:
            async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                await ObservedAccounting(db, DemoAccountingSource(db, connection), connection).list_customers(
                    ctx
                )
                await db.run_sync(lambda s: IdentityService(s).audit(ctx, "test.commit_marker", tenant, {}))

        asyncio.run(run())
        store.expire_all()
        assert store.scalar(select(AuditEvent).where(AuditEvent.action == "test.commit_marker")) is not None
        attempt = store.scalar(select(UsageAttempt).where(UsageAttempt.correlation_id == ctx.correlation_id))
        assert attempt is not None and attempt.outcome == "VERIFIED" and attempt.business_outcome == "PENDING"
        summary = client.get(f"/api/workspaces/{tenant}/usage").json()
        assert any("Business commit outcome unresolved" in n for n in summary["notices"])
        warnings = [record.getMessage() for record in caplog.records if record.name == observed.__name__]
        assert warnings == ["Business transaction outcome unresolved in usage evidence"]


def test_bounded_pool_fails_before_request_when_independent_writer_cannot_acquire() -> None:
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        ctx = context(tenant, user.id)
        calls: list[str] = []

        async def run(size: int) -> None:
            bind = store.get_bind()
            assert isinstance(bind, Engine)
            engine = create_async_engine(bind.url, pool_size=size, max_overflow=0, pool_timeout=0.1)
            try:
                async with async_sessionmaker(engine, expire_on_commit=False).begin() as db:
                    source = DemoAccountingSource(db, connection)
                    original = source.list_customers

                    async def counted(ctx: Any, cursor: Any = None) -> Any:
                        calls.append("request")
                        return await original(ctx, cursor)

                    cast(Any, source).list_customers = counted
                    await ObservedAccounting(db, source, connection).list_customers(ctx)
            finally:
                await engine.dispose()

        with pytest.raises(PoolTimeout):
            asyncio.run(run(1))
        assert calls == []
        asyncio.run(run(2))
        assert calls == ["request"]


@pytest.mark.parametrize(
    "nested_rollback,outer_rollback", [(False, True), (True, False), (False, False), (True, True)]
)
def test_attempt_outcomes_follow_their_savepoint_ancestry(
    nested_rollback: bool, outer_rollback: bool
) -> None:
    from aco.features.identity.models import AuditEvent
    from aco.features.identity.service import IdentityService

    with actor_client("owner") as (client, store, user, tenant):
        connection = UUID(seed(client, tenant)["provider_connection_id"])
        outer_ctx, nested_ctx = context(tenant, user.id), context(tenant, user.id)

        async def run() -> None:
            try:
                async with cast(FastAPI, client.app).state.business_sessions.begin() as db:
                    adapter = ObservedAccounting(db, DemoAccountingSource(db, connection), connection)
                    await adapter.list_customers(outer_ctx)
                    await db.run_sync(
                        lambda s: IdentityService(s).audit(outer_ctx, "repair.outer", tenant, {})
                    )
                    try:
                        async with db.begin_nested():
                            await adapter.list_customers(nested_ctx)
                            await db.run_sync(
                                lambda s: IdentityService(s).audit(nested_ctx, "repair.nested", tenant, {})
                            )
                            if nested_rollback:
                                raise ValueError("synthetic savepoint rollback")
                    except ValueError as error:
                        assert str(error) == "synthetic savepoint rollback"
                    if outer_rollback:
                        raise RuntimeError("synthetic root rollback")
            except RuntimeError as error:
                assert str(error) == "synthetic root rollback"

        asyncio.run(run())
        with Session(store.get_bind()) as verify:
            for ctx, marker, rolled_back in (
                (outer_ctx, "repair.outer", outer_rollback),
                (nested_ctx, "repair.nested", nested_rollback or outer_rollback),
            ):
                attempt = verify.scalar(
                    select(UsageAttempt).where(UsageAttempt.correlation_id == ctx.correlation_id)
                )
                audit = verify.scalar(select(AuditEvent.id).where(AuditEvent.action == marker))
                assert (audit is None) == rolled_back
                assert attempt is not None and attempt.outcome == "VERIFIED" and attempt.event_id is not None
                expected = "ROLLED_BACK" if rolled_back else "COMMITTED"
                print("F5 persisted", tenant, attempt.id, marker, audit, attempt.business_outcome, expected)
                assert attempt.business_outcome == expected
