"""Human-scoped synthetic cohort publication and immutable result restatements."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.context import TenantContext
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.identity.models import Membership, ProviderConnection, Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.features.impact.cohort_dto import (
    CohortListView,
    CohortMemberView,
    CohortPairView,
    CohortResultView,
    CohortView,
    RefreshCohorts,
    SeedCohorts,
)
from aco.features.impact.cohort_source import materialize
from aco.features.impact.cohorts import CohortHistory, calculate_member, percent
from aco.features.impact.models import (
    CohortPair,
    CohortRefresh,
    CohortResult,
    InvoiceCohort,
    InvoiceCohortMembership,
)
from aco.integrations.demo_accounting.history import DemoCohortAdapter
from aco.integrations.ports.accounting_history import CohortSourceHistory


class CohortService:
    def __init__(self, db: AsyncSession, app_env: str, configured: bool = True):
        self.db, self.app_env, self.configured = db, app_env, configured

    async def scope(
        self, ctx: TenantContext, *, write: bool = False
    ) -> tuple[Tenant, ProviderConnection | None, bool]:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        tenant = await self.db.run_sync(
            lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ)
        )
        member = await self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == ctx.actor_id,
                Membership.active.is_(True),
            )
        )
        connections = list(
            await self.db.scalars(
                select(ProviderConnection).where(
                    ProviderConnection.tenant_id == ctx.tenant_id,
                    ProviderConnection.provider == "demo",
                    ProviderConnection.provider_type == "accounting",
                    ProviderConnection.status == "READY",
                )
            )
        )
        connection = connections[0] if len(connections) == 1 else None
        allowed = bool(
            member
            and member.role == "owner"
            and tenant.is_demo
            and self.app_env in {"local", "demo"}
            and self.configured
            and connection
        )
        if write:
            await self.db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.SETTINGS))
            if not member or member.role != "owner":
                raise HTTPException(403, "OWNER_REQUIRED")
            if not allowed:
                raise HTTPException(409, "SYNTHETIC_COHORT_SOURCE_UNAVAILABLE")
        return tenant, connection, allowed

    async def read(self, ctx: TenantContext) -> CohortListView:
        _tenant, _connection, allowed = await self.scope(ctx)
        pairs = list(
            await self.db.scalars(
                select(CohortPair).where(
                    CohortPair.tenant_id == ctx.tenant_id, CohortPair.purpose == "ILLUSTRATION"
                )
            )
        )
        views = [await self.pair_view(row) for row in pairs]
        return CohortListView(
            tenant_id=ctx.tenant_id,
            can_seed=allowed and not pairs,
            can_refresh=allowed and bool(pairs),
            unavailable_reason=None
            if views
            else (
                "No frozen complete historical cohort. "
                "Current balances cannot substitute for opening history."
            ),
            pairs=views,
        )

    async def seed(self, ctx: TenantContext, command: SeedCohorts) -> CohortListView:
        tenant, connection, _allowed = await self.scope(ctx, write=True)
        assert connection is not None
        old = await self.db.scalar(
            select(CohortPair).where(
                CohortPair.tenant_id == ctx.tenant_id, CohortPair.purpose == "ILLUSTRATION"
            )
        )
        if old is not None:
            if old.request_id != command.request_id or old.command != command.model_dump(mode="json"):
                raise HTTPException(409, "COHORT_SEED_ALREADY_EXISTS_OR_COMMAND_CHANGED")
            return await self.read(ctx)
        adapter = DemoCohortAdapter(self.db, connection.id)
        manifest = await adapter.publish_fixed_history(ctx, tenant.timezone, self.app_env)
        pair = CohortPair(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection.id,
            actor_id=ctx.actor_id,
            request_id=command.request_id,
            command=command.model_dump(mode="json"),
            manifest_id=manifest,
        )
        self.db.add(pair)
        await self.db.flush()
        history, observations = await self.import_history(ctx, adapter)
        for side in ("BASELINE_SYNTHETIC", "AFTER_SYNTHETIC"):
            sources = [row for row in history.invoices if row.side == side]
            if not sources:
                raise HTTPException(409, "COHORT_POPULATION_INCOMPLETE")
            dates = [row.original_due_date for row in sources]
            definition: dict[str, object] = {
                "period_start": str(min(dates)),
                "period_end": str(max(dates) + timedelta(days=1)),
                "timezone": history.timezone,
                "currency": "AUD",
                "exponent": 2,
                "credit_terms_days": history.credit_terms_days,
                "business_segment": history.business_segment,
                "algorithm": "aco_cash_v1",
            }
            cohort = InvoiceCohort(tenant_id=ctx.tenant_id, pair_id=pair.id, side=side, definition=definition)
            self.db.add(cohort)
            await self.db.flush()
            for source in sources:
                basis = await self.basis(ctx, pair, history, source.external_invoice_id)
                source_opening = [
                    row for row in source.revisions if row.effective_at < calculate_member(basis).start_at
                ]
                opening_id = (
                    max(
                        source_opening,
                        key=lambda row: (row.effective_at, int(row.invoice.stamp.revision or "0")),
                    ).source_id
                    if source_opening
                    else None
                )
                self.db.add(
                    InvoiceCohortMembership(
                        tenant_id=ctx.tenant_id,
                        cohort_id=cohort.id,
                        invoice_id=basis.invoice_id,
                        basis={
                            "history": basis.model_dump(mode="json"),
                            "opening_source_id": str(opening_id) if opening_id else None,
                        },
                    )
                )
            await self.db.flush()
            await self.append_result(
                ctx, pair, cohort, history, observations, "Initial fixed synthetic cohort evidence."
            )
        await self.audit(ctx, "impact.synthetic_cohorts_created", pair.id)
        return await self.read(ctx)

    async def import_history(
        self, ctx: TenantContext, adapter: DemoCohortAdapter
    ) -> tuple[CohortSourceHistory, list[UUID]]:
        accounting = AccountingService(self.db, adapter)
        first = await adapter.read_cohort_history(ctx)
        await accounting.validate_result(ctx, first)
        accounting.require_current(first)
        history = first.value
        if len({row.external_invoice_id for row in history.invoices}) != len(history.invoices):
            raise HTTPException(409, "COHORT_DUPLICATE_SOURCE_IDENTITY")
        observations: list[UUID] = []
        for row in history.invoices:
            await accounting.recheck_invoice(ctx, row.external_invoice_id)
            assert accounting.last_observation_id is not None
            observations.append(accounting.last_observation_id)
        second = await adapter.read_cohort_history(ctx)
        await accounting.validate_result(ctx, second)
        accounting.require_current(second)
        if (
            first.value.source_watermark != second.value.source_watermark
            or first.value.manifest_id != second.value.manifest_id
        ):
            raise HTTPException(409, "COHORT_SOURCE_CHANGED_DURING_READ")
        return second.value, observations

    async def basis(
        self,
        ctx: TenantContext,
        pair: CohortPair,
        history: CohortSourceHistory,
        key: str,
        frozen: CohortHistory | None = None,
    ) -> CohortHistory:
        source = next((row for row in history.invoices if row.external_invoice_id == key), None)
        invoice = await self.db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == ctx.tenant_id,
                InvoiceSnapshot.provider_connection_id == pair.provider_connection_id,
                InvoiceSnapshot.external_invoice_id == key,
            )
        )
        if source is None or invoice is None:
            raise HTTPException(409, "COHORT_MEMBER_SOURCE_MISSING")
        facts = list(
            await self.db.scalars(
                select(VerifiedPaymentAllocation).where(
                    VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                    VerifiedPaymentAllocation.invoice_id == invoice.id,
                    VerifiedPaymentAllocation.provider_connection_id == pair.provider_connection_id,
                )
            )
        )
        return materialize(history, source, invoice, facts, frozen=frozen)

    async def append_result(
        self,
        ctx: TenantContext,
        pair: CohortPair,
        cohort: InvoiceCohort,
        history: CohortSourceHistory,
        observations: list[UUID],
        reason: str,
    ) -> None:
        previous = await self.db.scalar(
            select(CohortResult)
            .where(CohortResult.tenant_id == ctx.tenant_id, CohortResult.cohort_id == cohort.id)
            .order_by(CohortResult.version.desc())
        )
        if previous and previous.result["source_watermark"] == history.source_watermark:
            return
        members = list(
            await self.db.scalars(
                select(InvoiceCohortMembership).where(
                    InvoiceCohortMembership.tenant_id == ctx.tenant_id,
                    InvoiceCohortMembership.cohort_id == cohort.id,
                )
            )
        )
        evidence: list[CohortHistory] = []
        selected_observations: list[UUID] = []
        for member in members:
            frozen = CohortHistory.model_validate_json(json.dumps(member.basis["history"]))
            invoice = await self.db.get(InvoiceSnapshot, member.invoice_id)
            if invoice is None or invoice.tenant_id != ctx.tenant_id:
                raise HTTPException(409, "COHORT_MEMBER_SOURCE_MISSING")
            evidence.append(await self.basis(ctx, pair, history, invoice.external_invoice_id, frozen))
            position = next(
                index
                for index, row in enumerate(history.invoices)
                if row.external_invoice_id == invoice.external_invoice_id
            )
            selected_observations.append(observations[position])
        results = [calculate_member(row) for row in evidence]
        eligible = [row for row in results if row.eligible]
        complete = bool(results) and all(
            row.status in {"COMPLETE", "nonpositive_opening_exposure", "void_or_cancelled"} for row in results
        )
        opening = sum(row.opening_minor or 0 for row in eligible)
        complete = complete and opening > 0 and bool(eligible)
        numerator = sum(row.numerator_minor or 0 for row in eligible) if complete else None
        pif = (
            sum(row.paid_in_full is True for row in eligible)
            if complete and all(row.paid_in_full is not None for row in eligible)
            else None
        )
        collect = [row.days_to_collect for row in eligible if row.days_to_collect is not None]
        beyond = [row.days_beyond_terms for row in eligible if row.days_beyond_terms is not None]
        key = uuid4()
        label: Literal["BASELINE_SYNTHETIC", "AFTER_SYNTHETIC", "RESTATED", "INCOMPLETE"] = "INCOMPLETE"
        if complete:
            label = (
                "RESTATED"
                if previous
                else "BASELINE_SYNTHETIC"
                if cohort.side == "BASELINE_SYNTHETIC"
                else "AFTER_SYNTHETIC"
            )
        view = CohortResultView(
            id=key,
            version=previous.version + 1 if previous else 1,
            prior_result_id=previous.id if previous else None,
            label=label,
            status="COMPLETE" if complete else "INCOMPLETE",
            as_of_at=history.complete_through,
            last_synced_at=history.complete_through,
            source_watermark=history.source_watermark,
            reason=reason,
            eligible_count=len(eligible),
            excluded_count=len(results) - len(eligible),
            opening_minor=str(opening),
            raw_net_minor=str(sum(row.raw_net_minor or 0 for row in eligible)) if complete else None,
            recovery_numerator_minor=str(numerator) if numerator is not None else None,
            refunds_minor=str(sum(row.refunds_minor for row in results)),
            reversals_minor=str(sum(row.reversals_minor for row in results)),
            recovery_percent=percent(numerator, opening) if numerator is not None else None,
            paid_in_full_count=pif,
            paid_in_full_percent=percent(pif, len(eligible)) if pif is not None else None,
            days_to_collect=sum(collect, Decimal(0)) / len(collect) if complete and collect else None,
            days_beyond_terms=sum(beyond, Decimal(0)) / len(beyond) if complete and beyond else None,
            issues=[f"{row.invoice_id}:{row.status}" for row in results if row.status != "COMPLETE"],
            adjustment_ids=sorted({UUID(key) for row in results for key in row.adjustment_ids}, key=str),
            observation_ids=selected_observations,
        )
        self.db.add(
            CohortResult(
                id=key,
                tenant_id=ctx.tenant_id,
                cohort_id=cohort.id,
                version=view.version,
                prior_result_id=view.prior_result_id,
                evidence={
                    "source": history.model_dump(mode="json"),
                    "members": [row.model_dump(mode="json") for row in evidence],
                    "calculations": [row.model_dump(mode="json") for row in results],
                },
                result=view.model_dump(mode="json"),
            )
        )
        await self.db.flush()

    async def refresh(self, ctx: TenantContext, pair_id: UUID, command: RefreshCohorts) -> CohortListView:
        _tenant, connection, _allowed = await self.scope(ctx, write=True)
        pair = await self.db.scalar(
            select(CohortPair)
            .where(
                CohortPair.tenant_id == ctx.tenant_id,
                CohortPair.id == pair_id,
                CohortPair.purpose == "ILLUSTRATION",
            )
            .with_for_update()
        )
        if pair is None or connection is None or pair.provider_connection_id != connection.id:
            raise HTTPException(404, "COHORT_NOT_FOUND")
        old = await self.db.scalar(
            select(CohortRefresh).where(
                CohortRefresh.tenant_id == ctx.tenant_id, CohortRefresh.request_id == command.request_id
            )
        )
        if old:
            if old.pair_id != pair.id or old.command != command.model_dump(mode="json"):
                raise HTTPException(409, "COHORT_COMMAND_CHANGED")
            return await self.read(ctx)
        view = await self.pair_view(pair)
        if {row.results[-1].id for row in view.cohorts} != set(command.expected_result_ids):
            raise HTTPException(409, "COHORT_REVIEWED_RESULT_CHANGED")
        adapter = DemoCohortAdapter(self.db, pair.provider_connection_id)
        history, observations = await self.import_history(ctx, adapter)
        if history.manifest_id != pair.manifest_id:
            raise HTTPException(409, "COHORT_MANIFEST_CHANGED")
        cohorts = list(
            await self.db.scalars(
                select(InvoiceCohort).where(
                    InvoiceCohort.tenant_id == ctx.tenant_id, InvoiceCohort.pair_id == pair.id
                )
            )
        )
        for cohort in cohorts:
            await self.append_result(ctx, pair, cohort, history, observations, command.evidence)
        self.db.add(
            CohortRefresh(
                tenant_id=ctx.tenant_id,
                pair_id=pair.id,
                request_id=command.request_id,
                actor_id=ctx.actor_id,
                command=command.model_dump(mode="json"),
            )
        )
        await self.db.flush()
        await self.audit(ctx, "impact.cohorts_refreshed", pair.id)
        return await self.read(ctx)

    async def pair_view(self, pair: CohortPair) -> CohortPairView:
        cohorts = list(
            await self.db.scalars(
                select(InvoiceCohort)
                .where(InvoiceCohort.tenant_id == pair.tenant_id, InvoiceCohort.pair_id == pair.id)
                .order_by(InvoiceCohort.side.desc())
            )
        )
        views: list[CohortView] = []
        for cohort in cohorts:
            members = list(
                await self.db.scalars(
                    select(InvoiceCohortMembership)
                    .where(
                        InvoiceCohortMembership.tenant_id == pair.tenant_id,
                        InvoiceCohortMembership.cohort_id == cohort.id,
                    )
                    .order_by(InvoiceCohortMembership.invoice_id)
                )
            )
            member_views = []
            for row in members:
                frozen = CohortHistory.model_validate_json(json.dumps(row.basis["history"]))
                source_id = row.basis.get("opening_source_id")
                member_views.append(
                    CohortMemberView(
                        invoice_id=row.invoice_id,
                        original_due_date=frozen.original_due_date,
                        issue_date=frozen.issue_date,
                        opening_minor=str(frozen.opening_minor) if frozen.opening_minor is not None else None,
                        original_minor=str(frozen.original_minor),
                        source_watermark=frozen.source_watermark,
                        opening_source_id=UUID(str(source_id)) if source_id else None,
                        eligibility=calculate_member(frozen).status,
                    )
                )
            results = list(
                await self.db.scalars(
                    select(CohortResult)
                    .where(CohortResult.tenant_id == pair.tenant_id, CohortResult.cohort_id == cohort.id)
                    .order_by(CohortResult.version)
                )
            )
            views.append(
                CohortView.model_validate_json(
                    json.dumps(
                        {
                            **cohort.definition,
                            "id": str(cohort.id),
                            "side": cohort.side,
                            "members": [row.model_dump(mode="json") for row in member_views],
                            "results": [row.result for row in results],
                        }
                    )
                )
            )
        matched = len(views) == 2 and all(
            row.results and row.results[-1].status == "COMPLETE" for row in views
        )
        strata = {
            (
                row.currency,
                row.exponent,
                row.timezone,
                row.credit_terms_days,
                row.business_segment,
                row.algorithm,
            )
            for row in views
        }
        matched = matched and len(strata) == 1
        first, second = (
            (views[0].results[-1], views[1].results[-1])
            if len(views) == 2 and all(row.results for row in views)
            else (None, None)
        )
        difference = (
            second.recovery_percent - first.recovery_percent
            if matched
            and first
            and second
            and first.recovery_percent is not None
            and second.recovery_percent is not None
            else None
        )
        return CohortPairView(
            id=pair.id,
            source_connection_id=pair.provider_connection_id,
            comparison_allowed=matched,
            unmatched_strata=[] if len(strata) == 1 else [str(row) for row in sorted(strata)],
            recovery_change_pp=difference,
            recovery_relative_change_percent=difference * 100 / first.recovery_percent
            if difference is not None and first and first.recovery_percent
            else None,
            paid_in_full_change_pp=second.paid_in_full_percent - first.paid_in_full_percent
            if matched
            and first
            and second
            and first.paid_in_full_percent is not None
            and second.paid_in_full_percent is not None
            else None,
            cohorts=views,
        )

    async def audit(self, ctx: TenantContext, action: str, key: UUID) -> None:
        await self.db.run_sync(
            lambda store: IdentityService(store).audit(
                ctx, action, key, {"synthetic": True, "algorithm": "aco_cash_v1"}
            )
        )
