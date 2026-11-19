"""Read retained native history without creating any invoice, cash or completeness claim."""

import hashlib
import json
from datetime import datetime

from fastapi import HTTPException

from aco.core.context import TenantContext
from aco.features.accounting.dto import AccountingResult, InvoiceData, PaymentData
from aco.features.identity.models import Tenant, now
from aco.integrations.demo_accounting.history import DemoCohortAdapter
from aco.integrations.ports.accounting_history import (
    CohortSourceHistory,
    HistoricalAllocation,
    HistoricalInvoice,
    HistoricalInvoiceRevision,
)


class DemoObservedHistoryAdapter(DemoCohortAdapter):
    async def read_observed_history(
        self, ctx: TenantContext, as_of_at: datetime | None = None
    ) -> AccountingResult[CohortSourceHistory]:
        return await self.invoke(ctx, "read_observed_history", lambda: self._observed(ctx, as_of_at))

    async def _observed(
        self, ctx: TenantContext, as_of_at: datetime | None
    ) -> AccountingResult[CohortSourceHistory]:
        connection, rows = await self.demo_source.rows(ctx)
        at = as_of_at or now()
        if at > now():
            raise HTTPException(422, "COHORT_FUTURE_AS_OF")
        tenant = await self.db.get(Tenant, ctx.tenant_id)
        assert tenant is not None
        keys = sorted({row.external_id for row in rows if row.kind == "invoice" and row.observed_at <= at})
        if len(keys) > 2000:
            raise HTTPException(409, "COHORT_POPULATION_TOO_LARGE")
        complete: dict[str, tuple[str, str]] = {}
        for row in rows:
            if row.kind != "impact_manifest" or row.observed_at > at:
                continue
            data = row.payload
            population = data.get("population")
            if (
                data.get("history_contract") != "complete_from_issue_v1"
                or data.get("synthetic") is not True
                or not isinstance(population, list)
            ):
                continue
            for entry in population:
                if isinstance(entry, dict) and isinstance(entry.get("external_invoice_id"), str):
                    complete[entry["external_invoice_id"]] = (
                        str(data["timezone"]),
                        str(data["business_segment"]),
                    )
        invoices = []
        zones = {value[0] for key, value in complete.items() if key in keys}
        # Unknown source timezone cannot silently merge incompatible historical calendars.
        if zones and zones != {tenant.timezone}:
            raise HTTPException(409, "COHORT_SOURCE_TIMEZONE_MISMATCH")
        for key in keys:
            revisions, allocations = [], []
            for row in rows:
                if row.observed_at > at or row.effective_at > at:
                    continue
                if row.kind == "invoice" and row.external_id == key:
                    invoice = InvoiceData.model_validate_json(
                        json.dumps(
                            {
                                **row.payload,
                                "stamp": self.demo_source.stamp(row).model_dump(mode="json"),
                                "organisation_id": row.organisation_id,
                            }
                        )
                    )
                    revisions.append(
                        HistoricalInvoiceRevision(
                            source_id=row.id,
                            effective_at=row.effective_at,
                            observed_at=row.observed_at,
                            invoice=invoice,
                        )
                    )
                elif row.kind == "payment" and row.payload.get("external_invoice_id") == key:
                    payment = PaymentData.model_validate_json(
                        json.dumps(
                            {
                                **row.payload,
                                "stamp": self.demo_source.stamp(row).model_dump(mode="json"),
                                "organisation_id": row.organisation_id,
                                "verified": True,
                            }
                        )
                    )
                    allocations.append(
                        HistoricalAllocation(
                            source_id=row.id,
                            effective_at=row.effective_at,
                            observed_at=row.observed_at,
                            payment=payment,
                        )
                    )
            if not revisions:
                continue
            first = min(revisions, key=lambda row: int(row.invoice.stamp.revision or "0"))
            invoices.append(
                HistoricalInvoice(
                    external_invoice_id=key,
                    side="OBSERVED",
                    original_due_date=first.invoice.due_date,
                    issue_date=first.invoice.issue_date,
                    revisions=revisions,
                    allocations=allocations,
                    history_complete=key in complete,
                    business_segment=complete[key][1] if key in complete else None,
                )
            )
        # The immutable IDs also include current records after a selected historical as-of;
        # captured review therefore detects any newly arrived source evidence before publication.
        watermark = hashlib.sha256(
            json.dumps([str(row.id) for row in rows], separators=(",", ":")).encode()
        ).hexdigest()
        return self.demo_source.result(
            ctx,
            connection,
            CohortSourceHistory(
                manifest_id=None,
                timezone=tenant.timezone,
                credit_terms_days=0,
                business_segment="Observed source strata; unavailable when absent",
                complete_through=at,
                source_watermark=watermark,
                synthetic=True,
                invoices=invoices,
            ),
        )
