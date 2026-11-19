"""Explicit Owner publication of frozen observed populations and retained restatements."""

import hashlib
import json
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.context import TenantContext
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import AccountingObservation, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.identity.models import ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.impact.cohort_dto import CohortResultView
from aco.features.impact.cohort_source import materialize
from aco.features.impact.cohorts import CohortHistory, calculate_member, percent
from aco.features.impact.models import (
    CohortPair,
    CohortRefresh,
    CohortResult,
    InvoiceCohort,
    InvoiceCohortMembership,
)
from aco.features.impact.observed_dto import (
    ChangeObserved,
    FreezeObserved,
    ObservedCohortView,
    ObservedList,
    ObservedMember,
    ObservedPreview,
    ObservedSelection,
    ObservedSource,
    ObservedVersion,
    PreviewObservedChange,
)
from aco.integrations.demo_accounting.observed_history import DemoObservedHistoryAdapter
from aco.integrations.ports.accounting_history import CohortSourceHistory


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def retained_source_key(history: CohortSourceHistory) -> str:
    """Only immutable source facts known at the selected as-of can restate that result.

    The broader adapter watermark still protects review against any concurrent arrival.
    Fresh read timestamps are not revisions of these retained native identities.
    """
    return digest(
        {
            "manifest_id": str(history.manifest_id),
            "timezone": history.timezone,
            "synthetic": history.synthetic,
            "credit_terms_days": history.credit_terms_days,
            "business_segment": history.business_segment,
            "invoices": [
                {
                    "external_id": row.external_invoice_id,
                    "due": str(row.original_due_date),
                    "issue": str(row.issue_date),
                    "complete": row.history_complete,
                    "segment": row.business_segment,
                    "side": row.side,
                    "revisions": sorted(str(item.source_id) for item in row.revisions),
                    "allocations": sorted(str(item.source_id) for item in row.allocations),
                }
                for row in sorted(history.invoices, key=lambda item: item.external_invoice_id)
            ],
        }
    )


def summarize(
    histories: list[CohortHistory],
    history: CohortSourceHistory,
    observations: list[UUID],
    *,
    prior: CohortResult | None = None,
    restated: bool = False,
    reason: str = "Explicit source preview; not yet published.",
) -> CohortResultView:
    values = [calculate_member(row) for row in histories]
    eligible = [row for row in values if row.eligible]
    complete = bool(values) and all(
        row.status in {"COMPLETE", "nonpositive_opening_exposure", "void_or_cancelled"} for row in values
    )
    opening = sum(row.opening_minor or 0 for row in eligible)
    complete = complete and bool(eligible) and opening > 0
    numerator = sum(row.numerator_minor or 0 for row in eligible) if complete else None
    paid = (
        sum(row.paid_in_full is True for row in eligible)
        if complete and all(row.paid_in_full is not None for row in eligible)
        else None
    )
    collect = [row.days_to_collect for row in eligible if row.days_to_collect is not None]
    beyond = [row.days_beyond_terms for row in eligible if row.days_beyond_terms is not None]
    return CohortResultView(
        id=uuid4(),
        version=prior.version + 1 if prior else 1,
        prior_result_id=prior.id if prior else None,
        label="RESTATED" if complete and (prior or restated) else "OBSERVED" if complete else "INCOMPLETE",
        status="COMPLETE" if complete else "INCOMPLETE",
        as_of_at=history.complete_through,
        last_synced_at=history.complete_through,
        source_watermark=history.source_watermark,
        reason=reason,
        eligible_count=len(eligible),
        excluded_count=len(values) - len(eligible),
        opening_minor=str(opening)
        if histories and all(row.opening_minor is not None for row in histories)
        else None,
        raw_net_minor=str(sum(row.raw_net_minor or 0 for row in eligible)) if complete else None,
        recovery_numerator_minor=str(numerator) if numerator is not None else None,
        refunds_minor=str(sum(row.refunds_minor for row in values)),
        reversals_minor=str(sum(row.reversals_minor for row in values)),
        recovery_percent=percent(numerator, opening) if numerator is not None else None,
        paid_in_full_count=paid,
        paid_in_full_percent=percent(paid, len(eligible)) if paid is not None else None,
        days_to_collect=sum(collect, Decimal(0)) / len(collect) if complete and collect else None,
        days_beyond_terms=sum(beyond, Decimal(0)) / len(beyond) if complete and beyond else None,
        issues=[f"{row.invoice_id}:{row.status}" for row in values if row.status != "COMPLETE"],
        adjustment_ids=sorted({UUID(key) for row in values for key in row.adjustment_ids}, key=str),
        observation_ids=observations,
    )


class ObservedCohortService:
    def __init__(self, db: AsyncSession, app_env: str, configured: bool = True):
        self.db, self.app_env, self.configured = db, app_env, configured

    async def authority(self, ctx: TenantContext, write: bool = False) -> Tenant:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        tenant = await self.db.run_sync(
            lambda store: IdentityService(store).authorize(
                ctx, Permission.SETTINGS if write else Permission.INVOICE_READ
            )
        )
        if write:
            if not self.configured or self.app_env not in {"local", "demo"} or not tenant.is_demo:
                raise HTTPException(409, "OBSERVED_HISTORY_ADAPTER_UNAVAILABLE")
            await self.db.scalar(
                select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update(key_share=True)
            )
        return tenant

    async def read(self, ctx: TenantContext) -> ObservedList:
        tenant = await self.authority(ctx)
        connections = list(
            await self.db.scalars(
                select(ProviderConnection).where(
                    ProviderConnection.tenant_id == ctx.tenant_id,
                    ProviderConnection.provider_type == "accounting",
                    ProviderConnection.provider == "demo",
                    ProviderConnection.status == "READY",
                )
            )
        )
        can_build = False
        try:
            await self.db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.SETTINGS))
            can_build = bool(
                self.configured and self.app_env in {"local", "demo"} and tenant.is_demo and connections
            )
        except HTTPException:
            pass
        pairs = list(
            await self.db.scalars(
                select(CohortPair)
                .where(CohortPair.tenant_id == ctx.tenant_id, CohortPair.purpose == "OBSERVED")
                .order_by(CohortPair.created_at)
            )
        )
        return ObservedList(
            tenant_id=ctx.tenant_id,
            can_build=can_build,
            sources=[
                ObservedSource(connection_id=row.id, provider=row.provider, synthetic=True)
                for row in connections
            ],
            cohorts=[await self.view(row) for row in pairs],
            as_of_at=now(),
        )

    async def evidence(
        self, ctx: TenantContext, selection: ObservedSelection, frozen: InvoiceCohort | None = None
    ) -> tuple[ObservedPreview, list[CohortHistory], CohortSourceHistory]:
        await self.authority(ctx, write=True)
        connection = await self.db.scalar(
            select(ProviderConnection).where(
                ProviderConnection.id == selection.connection_id,
                ProviderConnection.tenant_id == ctx.tenant_id,
                ProviderConnection.status == "READY",
                ProviderConnection.provider == "demo",
                ProviderConnection.provider_type == "accounting",
            )
        )
        if connection is None:
            raise HTTPException(404, "COHORT_CONNECTION_NOT_FOUND")
        adapter = DemoObservedHistoryAdapter(self.db, connection.id)
        accounting = AccountingService(self.db, adapter)
        first = await adapter.read_observed_history(ctx, selection.as_of_at)
        await accounting.validate_result(ctx, first)
        accounting.require_current(first)
        sources = [
            row
            for row in first.value.invoices
            if selection.period_start <= row.original_due_date < selection.period_end
            and (row.original_due_date - row.issue_date).days == selection.credit_terms_days
            and row.revisions[0].invoice.original.currency == selection.currency
        ]
        prior_members = (
            list(
                await self.db.scalars(
                    select(InvoiceCohortMembership).where(
                        InvoiceCohortMembership.tenant_id == ctx.tenant_id,
                        InvoiceCohortMembership.cohort_id == frozen.id,
                    )
                )
            )
            if frozen
            else []
        )
        prior_by_external: dict[str, InvoiceCohortMembership] = {}
        for member in prior_members:
            prior_by_external[str(member.basis["external_invoice_id"])] = member
        if frozen:
            sources = [row for row in first.value.invoices if row.external_invoice_id in prior_by_external]
        observations = []
        for source in sources:
            await accounting.recheck_invoice(ctx, source.external_invoice_id)
            assert accounting.last_observation_id is not None
            observations.append(accounting.last_observation_id)
        second = await adapter.read_observed_history(ctx, selection.as_of_at)
        await accounting.validate_result(ctx, second)
        accounting.require_current(second)
        if first.value.source_watermark != second.value.source_watermark:
            raise HTTPException(409, "COHORT_SOURCE_CHANGED_DURING_READ")
        history = second.value
        sources_by_id = {row.external_invoice_id: row for row in history.invoices}
        effective = selection.model_copy(update={"as_of_at": history.complete_through})
        members, histories, warnings = [], [], []
        for source in sources:
            source = sources_by_id[source.external_invoice_id]
            invoice = await self.db.scalar(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.tenant_id == ctx.tenant_id,
                    InvoiceSnapshot.provider_connection_id == connection.id,
                    InvoiceSnapshot.external_invoice_id == source.external_invoice_id,
                )
            )
            assert invoice is not None
            allocations = list(
                await self.db.scalars(
                    select(VerifiedPaymentAllocation).where(
                        VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                        VerifiedPaymentAllocation.provider_connection_id == connection.id,
                        VerifiedPaymentAllocation.invoice_id == invoice.id,
                        VerifiedPaymentAllocation.created_at <= history.complete_through,
                        VerifiedPaymentAllocation.observed_at <= history.complete_through,
                        VerifiedPaymentAllocation.effective_at <= history.complete_through,
                    )
                )
            )
            prior_member = prior_by_external.get(source.external_invoice_id)
            old = (
                CohortHistory.model_validate_json(json.dumps(prior_member.basis["history"]))
                if prior_member
                else None
            )
            latest_revision = max(source.revisions, key=lambda row: int(row.invoice.stamp.revision or "0"))
            historical_observation = None
            if latest_revision.invoice.stamp.revision != invoice.source_revision:
                historical_observation = await self.db.scalar(
                    select(AccountingObservation)
                    .where(
                        AccountingObservation.tenant_id == ctx.tenant_id,
                        AccountingObservation.provider_connection_id == connection.id,
                        AccountingObservation.invoice_id == invoice.id,
                        AccountingObservation.observed_at <= history.complete_through,
                        AccountingObservation.created_at <= history.complete_through,
                        AccountingObservation.invoice_data["stamp"]["revision"].as_string()
                        == latest_revision.invoice.stamp.revision,
                    )
                    .order_by(AccountingObservation.observed_at.desc())
                    .limit(1)
                )
                if historical_observation is not None:
                    observations.append(historical_observation.id)
            basis = materialize(
                history,
                source,
                invoice,
                allocations,
                frozen=old,
                historical_observation=historical_observation,
            )
            histories.append(basis)
            start = calculate_member(basis).start_at
            before = [row for row in source.revisions if row.effective_at < start]
            opening_id = (
                max(
                    before, key=lambda row: (row.effective_at, int(row.invoice.stamp.revision or "0"))
                ).source_id
                if before and source.history_complete
                else None
            )
            current = max(source.revisions, key=lambda row: int(row.invoice.stamp.revision or "0"))
            change = (
                "Current due date differs; original frozen due date retained."
                if current.invoice.due_date != basis.original_due_date
                else None
            )
            members.append(
                ObservedMember(
                    invoice_id=invoice.id,
                    original_due_date=basis.original_due_date,
                    issue_date=basis.issue_date,
                    opening_minor=str(basis.opening_minor) if basis.opening_minor is not None else None,
                    original_minor=str(basis.original_minor),
                    source_watermark=basis.source_watermark,
                    opening_source_id=opening_id,
                    eligibility=calculate_member(basis).status,
                    source_revision_ids=[row.source_id for row in source.revisions],
                    current_due_date=current.invoice.due_date,
                    business_segment=source.business_segment,
                    source_change=change,
                )
            )
        found = {row.external_invoice_id for row in sources}
        for key, member in prior_by_external.items():
            if key in found:
                continue
            old = CohortHistory.model_validate_json(json.dumps(member.basis["history"]))
            histories.append(
                old.model_copy(
                    update={
                        "complete": False,
                        "source_consistent": False,
                        "as_of_at": history.complete_through,
                        "source_watermark": history.source_watermark,
                    }
                )
            )
            retained = ObservedMember.model_validate_json(json.dumps(member.basis["view"]))
            members.append(
                retained.model_copy(
                    update={
                        "eligibility": "INCOMPLETE",
                        "source_change": "Original source member missing; retained, result incomplete.",
                    }
                )
            )
            warnings.append("Missing original member retained; no silent deletion.")
        if frozen:
            candidates = {
                row.external_invoice_id
                for row in history.invoices
                if selection.period_start <= row.original_due_date < selection.period_end
                and (row.original_due_date - row.issue_date).days == selection.credit_terms_days
                and row.revisions[0].invoice.original.currency == selection.currency
            }
            if candidates - set(prior_by_external):
                warnings.append(
                    "New source members require explicit restatement; frozen denominator unchanged."
                )
        if any(not row.history_complete for row in sources):
            warnings.append(
                "Opening history unavailable; current balance/original amount is not opening exposure."
            )
        members.sort(key=lambda row: str(row.invoice_id))
        histories.sort(key=lambda row: str(row.invoice_id))
        reviewed = digest(
            {
                "selection": effective.model_dump(mode="json"),
                "members": [row.model_dump(mode="json") for row in members],
                "source_watermark": history.source_watermark,
                "frozen_definition": str(frozen.id) if frozen else None,
            }
        )
        return (
            ObservedPreview(
                selection=effective,
                reviewed_hash=reviewed,
                synthetic=history.synthetic,
                timezone=history.timezone,
                business_segments=sorted({row.business_segment for row in members}, key=str),
                members=members,
                result=summarize(histories, history, observations),
                warnings=warnings,
            ),
            histories,
            history,
        )

    async def preview(self, ctx: TenantContext, selection: ObservedSelection) -> ObservedPreview:
        return (await self.evidence(ctx, selection))[0]

    async def store_result(
        self,
        ctx: TenantContext,
        cohort: InvoiceCohort,
        preview: ObservedPreview,
        histories: list[CohortHistory],
        history: CohortSourceHistory,
        reason: str,
        prior: CohortResult | None = None,
    ) -> None:
        if (
            prior
            and retained_source_key(
                CohortSourceHistory.model_validate_json(json.dumps(prior.evidence["source"]))
            )
            == retained_source_key(history)
            and CohortResultView.model_validate_json(json.dumps(prior.result)).as_of_at
            == history.complete_through
        ):
            return
        result = summarize(
            histories,
            history,
            preview.result.observation_ids,
            prior=prior,
            restated=cohort.prior_cohort_id is not None,
            reason=reason,
        )
        self.db.add(
            CohortResult(
                id=result.id,
                tenant_id=ctx.tenant_id,
                cohort_id=cohort.id,
                version=result.version,
                prior_result_id=result.prior_result_id,
                result=result.model_dump(mode="json"),
                evidence={
                    "source": history.model_dump(mode="json"),
                    "members": [row.model_dump(mode="json") for row in histories],
                    "source_views": [row.model_dump(mode="json") for row in preview.members],
                    "warnings": preview.warnings,
                },
            )
        )
        await self.db.flush()

    async def store_definition(
        self,
        ctx: TenantContext,
        pair: CohortPair,
        command: FreezeObserved,
        preview: ObservedPreview,
        histories: list[CohortHistory],
        history: CohortSourceHistory,
        prior: InvoiceCohort | None = None,
    ) -> None:
        cohort = InvoiceCohort(
            tenant_id=ctx.tenant_id,
            pair_id=pair.id,
            side="OBSERVED",
            version=prior.version + 1 if prior else 1,
            prior_cohort_id=prior.id if prior else None,
            definition={
                "selection": preview.selection.model_dump(mode="json"),
                "timezone": preview.timezone,
                "synthetic": preview.synthetic,
                "reason": command.evidence,
            },
        )
        self.db.add(cohort)
        await self.db.flush()
        invoices = {row.invoice_id: row for row in histories}
        for member in preview.members:
            invoice = await self.db.get(InvoiceSnapshot, member.invoice_id)
            assert invoice is not None
            self.db.add(
                InvoiceCohortMembership(
                    tenant_id=ctx.tenant_id,
                    cohort_id=cohort.id,
                    invoice_id=member.invoice_id,
                    basis={
                        "external_invoice_id": invoice.external_invoice_id,
                        "history": invoices[member.invoice_id].model_dump(mode="json"),
                        "view": member.model_dump(mode="json"),
                    },
                )
            )
        await self.db.flush()
        await self.store_result(ctx, cohort, preview, histories, history, command.evidence)

    async def freeze(self, ctx: TenantContext, command: FreezeObserved) -> ObservedCohortView:
        await self.authority(ctx, write=True)
        old = await self.db.scalar(
            select(CohortPair).where(
                CohortPair.tenant_id == ctx.tenant_id, CohortPair.request_id == command.request_id
            )
        )
        if old:
            if (
                old.purpose != "OBSERVED"
                or old.actor_id != ctx.actor_id
                or old.command != command.model_dump(mode="json")
            ):
                raise HTTPException(409, "COHORT_COMMAND_CHANGED")
            return await self.view(old)
        if command.selection.as_of_at is None:
            raise HTTPException(422, "COHORT_CAPTURED_AS_OF_REQUIRED")
        preview, histories, history = await self.evidence(ctx, command.selection)
        if preview.reviewed_hash != command.reviewed_hash:
            raise HTTPException(409, "COHORT_REVIEWED_SOURCE_CHANGED")
        pair = CohortPair(
            tenant_id=ctx.tenant_id,
            provider_connection_id=command.selection.connection_id,
            actor_id=ctx.actor_id,
            request_id=command.request_id,
            command=command.model_dump(mode="json"),
            manifest_id=None,
            purpose="OBSERVED",
        )
        self.db.add(pair)
        await self.db.flush()
        await self.store_definition(ctx, pair, command, preview, histories, history)
        await self.db.run_sync(
            lambda store: IdentityService(store).audit(
                ctx,
                "impact.observed_cohort_frozen",
                pair.id,
                {"synthetic": history.synthetic, "reviewed_hash": command.reviewed_hash},
            )
        )
        return await self.view(pair)

    async def view(self, pair: CohortPair) -> ObservedCohortView:
        definitions = list(
            await self.db.scalars(
                select(InvoiceCohort)
                .where(InvoiceCohort.tenant_id == pair.tenant_id, InvoiceCohort.pair_id == pair.id)
                .order_by(InvoiceCohort.version)
            )
        )
        versions = []
        for row in definitions:
            members = list(
                await self.db.scalars(
                    select(InvoiceCohortMembership)
                    .where(
                        InvoiceCohortMembership.tenant_id == pair.tenant_id,
                        InvoiceCohortMembership.cohort_id == row.id,
                    )
                    .order_by(InvoiceCohortMembership.invoice_id)
                )
            )
            results = list(
                await self.db.scalars(
                    select(CohortResult)
                    .where(CohortResult.tenant_id == pair.tenant_id, CohortResult.cohort_id == row.id)
                    .order_by(CohortResult.version)
                )
            )
            versions.append(
                ObservedVersion(
                    id=row.id,
                    version=row.version,
                    prior_definition_id=row.prior_cohort_id,
                    reason=str(row.definition["reason"]),
                    selection=ObservedSelection.model_validate_json(json.dumps(row.definition["selection"])),
                    timezone=str(row.definition["timezone"]),
                    members=[
                        ObservedMember.model_validate_json(json.dumps(member.basis["view"]))
                        for member in members
                    ],
                    current_source_members=TypeAdapter(list[ObservedMember]).validate_json(
                        json.dumps(results[-1].evidence["source_views"])
                    )
                    if results
                    else [],
                    warnings=TypeAdapter(list[str]).validate_python(results[-1].evidence["warnings"])
                    if results
                    else [],
                    results=[
                        CohortResultView.model_validate_json(json.dumps(result.result)) for result in results
                    ],
                )
            )
        synthetic = bool(definitions[0].definition["synthetic"])
        return ObservedCohortView(
            id=pair.id,
            source_connection_id=pair.provider_connection_id,
            synthetic=synthetic,
            label="Observed execution — Synthetic accounting source"
            if synthetic
            else "Observed — Accounting-sourced",
            versions=versions,
        )

    async def current(
        self, ctx: TenantContext, pair_id: UUID
    ) -> tuple[CohortPair, InvoiceCohort, CohortResult]:
        pair = await self.db.scalar(
            select(CohortPair).where(
                CohortPair.tenant_id == ctx.tenant_id,
                CohortPair.id == pair_id,
                CohortPair.purpose == "OBSERVED",
            )
        )
        if pair is None:
            raise HTTPException(404, "COHORT_NOT_FOUND")
        cohort = await self.db.scalar(
            select(InvoiceCohort)
            .where(InvoiceCohort.tenant_id == ctx.tenant_id, InvoiceCohort.pair_id == pair.id)
            .order_by(InvoiceCohort.version.desc())
            .limit(1)
        )
        assert cohort is not None
        result = await self.db.scalar(
            select(CohortResult)
            .where(CohortResult.tenant_id == ctx.tenant_id, CohortResult.cohort_id == cohort.id)
            .order_by(CohortResult.version.desc())
            .limit(1)
        )
        assert result is not None
        return pair, cohort, result

    def reviewed_current(
        self,
        pair: CohortPair,
        cohort: InvoiceCohort,
        result: CohortResult,
        command: PreviewObservedChange | ChangeObserved,
        restate: bool,
    ) -> None:
        if command.expected_definition_id != cohort.id or command.expected_result_id != result.id:
            raise HTTPException(409, "COHORT_REVIEWED_RESULT_CHANGED")
        if command.selection.connection_id != pair.provider_connection_id:
            raise HTTPException(403, "COHORT_CONNECTION_CHANGED")
        previous = ObservedSelection.model_validate_json(json.dumps(cohort.definition["selection"]))
        if not restate and command.selection.model_dump(exclude={"as_of_at"}) != previous.model_dump(
            exclude={"as_of_at"}
        ):
            raise HTTPException(409, "COHORT_MEMBERSHIP_RESTATEMENT_REQUIRED")
        if command.selection.as_of_at is not None:
            prior_as_of = CohortResultView.model_validate_json(json.dumps(result.result)).as_of_at
            if command.selection.as_of_at < prior_as_of:
                raise HTTPException(409, "COHORT_AS_OF_REGRESSION")

    async def preview_change(
        self, ctx: TenantContext, pair_id: UUID, command: PreviewObservedChange
    ) -> ObservedPreview:
        await self.authority(ctx, write=True)
        pair, cohort, result = await self.current(ctx, pair_id)
        restate = command.mode == "RESTATE"
        self.reviewed_current(pair, cohort, result, command, restate)
        return (await self.evidence(ctx, command.selection, None if restate else cohort))[0]

    async def change(
        self, ctx: TenantContext, pair_id: UUID, command: ChangeObserved, *, restate: bool
    ) -> ObservedCohortView:
        await self.authority(ctx, write=True)
        pair, cohort, result = await self.current(ctx, pair_id)
        payload = {"mode": "RESTATE" if restate else "REFRESH", **command.model_dump(mode="json")}
        old = await self.db.scalar(
            select(CohortRefresh).where(
                CohortRefresh.tenant_id == ctx.tenant_id, CohortRefresh.request_id == command.request_id
            )
        )
        if old:
            if old.pair_id != pair.id or old.actor_id != ctx.actor_id or old.command != payload:
                raise HTTPException(409, "COHORT_COMMAND_CHANGED")
            return await self.view(pair)
        self.reviewed_current(pair, cohort, result, command, restate)
        if command.selection.as_of_at is None:
            raise HTTPException(422, "COHORT_CAPTURED_AS_OF_REQUIRED")
        preview, histories, history = await self.evidence(ctx, command.selection, None if restate else cohort)
        if preview.reviewed_hash != command.reviewed_hash:
            raise HTTPException(409, "COHORT_REVIEWED_SOURCE_CHANGED")
        if restate:
            await self.store_definition(ctx, pair, command, preview, histories, history, cohort)
        else:
            await self.store_result(ctx, cohort, preview, histories, history, command.evidence, result)
        self.db.add(
            CohortRefresh(
                tenant_id=ctx.tenant_id,
                pair_id=pair.id,
                request_id=command.request_id,
                actor_id=ctx.actor_id,
                command=payload,
            )
        )
        await self.db.flush()
        await self.db.run_sync(
            lambda store: IdentityService(store).audit(
                ctx,
                "impact.observed_membership_restated" if restate else "impact.observed_result_refreshed",
                pair.id,
                {"synthetic": history.synthetic, "reviewed_hash": command.reviewed_hash},
            )
        )
        return await self.view(pair)
