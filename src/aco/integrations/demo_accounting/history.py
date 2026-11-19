"""Bounded, explicitly synthetic historical ledger. Only the adapter reads source storage."""

import hashlib
import json
from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from aco.core.context import TenantContext
from aco.features.accounting.dto import AccountingResult, InvoiceData, PaymentData
from aco.features.accounting.models import DemoSourceRecord
from aco.features.accounting.seed import demo_guard
from aco.features.identity.models import Membership, now
from aco.features.identity.service import Permission
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
from aco.integrations.ports.accounting import failure
from aco.integrations.ports.accounting_history import (
    CohortSourceHistory,
    HistoricalAllocation,
    HistoricalInvoice,
    HistoricalInvoiceRevision,
)

MANIFEST = "aco-illustrative-cohorts-v1"


class DemoCohortAdapter(DemoAccountingAdapter):
    async def publish_fixed_history(self, ctx: TenantContext, timezone: str, app_env: str) -> UUID:
        await demo_guard(self.db, ctx, app_env, Permission.SETTINGS)
        member = await self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == ctx.actor_id,
                Membership.active.is_(True),
            )
        )
        if member is None or member.role != "owner":
            raise HTTPException(403, "OWNER_REQUIRED")
        connection, rows = await self.demo_source.rows(ctx)
        old = [row for row in rows if row.kind == "impact_manifest" and row.external_id == MANIFEST]
        if old:
            if len(old) != 1:
                raise failure()
            return old[0].id
        if any(row.external_id.startswith("COHORT-") for row in rows):
            raise HTTPException(409, "COHORT_SOURCE_IDENTITY_CONFLICT")
        zone, stamp = ZoneInfo(timezone), now()
        today = stamp.astimezone(zone).date()
        organisation = connection.external_account_id
        assert organisation is not None

        def append(
            kind: str, key: str, revision: int, payload: dict[str, object], at: datetime
        ) -> DemoSourceRecord:
            row = DemoSourceRecord(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                organisation_id=organisation,
                kind=kind,
                external_id=key,
                revision=revision,
                payload=payload,
                effective_at=at,
                observed_at=at,
            )
            self.db.add(row)
            return row

        append(
            "customer",
            "COHORT-CUSTOMER",
            1,
            {"external_id": "COHORT-CUSTOMER", "name": "Synthetic cohort illustration", "email": None},
            stamp,
        )
        population: list[dict[str, object]] = []
        for side, age, amounts, paid_day in (
            ("BASELINE_SYNTHETIC", 100, [10000, 10000, 4000, 0], 20),
            ("AFTER_SYNTHETIC", 60, [10000, 10000, 10000, 0], 10),
        ):
            due = today - timedelta(days=age)
            issue = due - timedelta(days=30)
            for index, amount in enumerate(amounts):
                key = f"COHORT-{side}-{index + 1}"
                money: dict[str, object] = {"minor_units": 10000, "currency": "AUD", "exponent": 2}
                payload: dict[str, object] = {
                    "external_id": key,
                    "external_customer_id": "COHORT-CUSTOMER",
                    "number": key,
                    "issue_date": str(issue),
                    "due_date": str(due),
                    "original": money,
                    "outstanding": money,
                    "status": "open",
                    "source_status": "OPEN",
                    "verified": True,
                }
                append("invoice", key, 1, payload, datetime.combine(issue, time.min, zone))
                if amount:
                    at = datetime.combine(due + timedelta(days=paid_day), time.min, zone)
                    append(
                        "payment",
                        f"{key}-cash/1",
                        1,
                        {
                            "external_payment_id": f"{key}-cash",
                            "external_allocation_id": "1",
                            "external_invoice_id": key,
                            "kind": "cash",
                            "amount": {**money, "minor_units": amount},
                            "parent_payment_id": None,
                            "parent_allocation_id": None,
                        },
                        at,
                    )
                    status = "paid" if amount == 10000 else "partially_paid"
                    append(
                        "invoice",
                        key,
                        2,
                        {
                            **payload,
                            "outstanding": {**money, "minor_units": 10000 - amount},
                            "status": status,
                            "source_status": status.upper(),
                        },
                        at,
                    )
                population.append(
                    {
                        "external_invoice_id": key,
                        "side": side,
                        "original_due_date": str(due),
                        "issue_date": str(issue),
                    }
                )
        manifest = append(
            "impact_manifest",
            MANIFEST,
            1,
            {
                "synthetic": True,
                "timezone": timezone,
                "credit_terms_days": 30,
                "business_segment": "fixed-synthetic-wholesaler",
                "population": population,
                "history_contract": "complete_from_issue_v1",
            },
            stamp,
        )
        await self.db.flush()
        return manifest.id

    async def read_cohort_history(self, ctx: TenantContext) -> AccountingResult[CohortSourceHistory]:
        return await self.invoke(ctx, "read_cohort_history", lambda: self._read_history(ctx))

    async def _read_history(self, ctx: TenantContext) -> AccountingResult[CohortSourceHistory]:
        connection, rows = await self.demo_source.rows(ctx)
        manifests = [row for row in rows if row.kind == "impact_manifest" and row.external_id == MANIFEST]
        if len(manifests) != 1:
            raise HTTPException(409, "COMPLETE_HISTORY_UNAVAILABLE")
        manifest = manifests[0]
        data = manifest.payload
        if data.get("history_contract") != "complete_from_issue_v1" or data.get("synthetic") is not True:
            raise failure()
        population = data.get("population")
        if not isinstance(population, list) or len(population) != 8:
            raise failure()
        at = now()
        selected = [manifest]
        invoices: list[HistoricalInvoice] = []
        for entry in population:
            if not isinstance(entry, dict):
                raise failure()
            key = str(entry["external_invoice_id"])
            revisions, allocations = [], []
            for row in rows:
                if row.observed_at > at or row.effective_at > at:
                    continue
                if row.kind == "invoice" and row.external_id == key:
                    selected.append(row)
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
                    selected.append(row)
                    payment = PaymentData.model_validate(
                        {
                            **row.payload,
                            "stamp": self.demo_source.stamp(row),
                            "organisation_id": row.organisation_id,
                            "verified": True,
                        }
                    )
                    allocations.append(
                        HistoricalAllocation(
                            source_id=row.id,
                            effective_at=row.effective_at,
                            observed_at=row.observed_at,
                            payment=payment,
                        )
                    )
            invoices.append(
                HistoricalInvoice.model_validate_json(
                    json.dumps(
                        {
                            **entry,
                            "revisions": [row.model_dump(mode="json") for row in revisions],
                            "allocations": [row.model_dump(mode="json") for row in allocations],
                        }
                    )
                )
            )
        canonical = [
            {
                "id": str(row.id),
                "revision": row.revision,
                "payload": row.payload,
                "effective_at": row.effective_at.isoformat(),
                "observed_at": row.observed_at.isoformat(),
            }
            for row in sorted(selected, key=lambda row: row.sequence)
        ]
        watermark = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result = CohortSourceHistory(
            manifest_id=manifest.id,
            timezone=str(data["timezone"]),
            credit_terms_days=int(str(data["credit_terms_days"])),
            business_segment=str(data["business_segment"]),
            complete_through=at,
            source_watermark=watermark,
            invoices=invoices,
        )
        return self.demo_source.result(ctx, connection, result)
