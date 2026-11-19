"""Rejected source references remain unverified evidence, never financial facts."""

import asyncio
from typing import cast

import pytest
from accounting_support import context
from case_promise_support import invoices
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import func, select

from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import AccountingObservation, DemoSourceRecord, VerifiedPaymentAllocation
from aco.features.identity.models import now
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
from aco.integrations.ports.accounting import AccountingFailure


@pytest.mark.parametrize("invalid", ["parentless", "currency", "parent_unknown", "foreign_org"])
def test_actual_rejected_candidate_has_bounded_unverified_refs_only(invalid: str) -> None:
    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        app = cast(FastAPI, client.app)
        ctx = context(tenant, user.id)
        connection_id, invoice_key = invoice.provider_connection_id, invoice.external_invoice_id
        before = db.scalar(select(func.count()).select_from(AccountingObservation))
        facts = db.scalar(select(func.count()).select_from(VerifiedPaymentAllocation))
        db.add(
            DemoSourceRecord(
                tenant_id=tenant,
                provider_connection_id=connection_id,
                organisation_id="OTHER-ORG" if invalid == "foreign_org" else invoice.organisation_id,
                kind="payment",
                external_id="rejected-reversal-native",
                revision=1,
                effective_at=now(),
                observed_at=now(),
                payload={
                    "external_payment_id": "REJECTED-PAYMENT",
                    "external_allocation_id": "REJECTED-ALLOCATION",
                    "external_invoice_id": invoice_key,
                    "kind": "reversal",
                    "amount": {
                        "minor_units": -1000,
                        "currency": "USD" if invalid == "currency" else "AUD",
                        "exponent": 2,
                    },
                    "parent_payment_id": None if invalid == "parentless" else "MISSING-PARENT",
                    "parent_allocation_id": None if invalid == "parentless" else "MISSING-ALLOCATION",
                },
            )
        )
        if invalid == "currency":
            db.add(
                DemoSourceRecord(
                    tenant_id=tenant,
                    provider_connection_id=connection_id,
                    organisation_id=invoice.organisation_id,
                    kind="payment",
                    external_id="related-reversal-native",
                    revision=1,
                    effective_at=now(),
                    observed_at=now(),
                    payload={
                        "external_payment_id": "R1",
                        "external_allocation_id": "R1-ALLOCATION",
                        "external_invoice_id": invoice_key,
                        "kind": "reversal",
                        "amount": {"minor_units": -100, "currency": "AUD", "exponent": 2},
                        "parent_payment_id": "P1",
                        "parent_allocation_id": "P1-ALLOCATION",
                    },
                )
            )
        db.commit()

        async def read() -> None:
            async with app.state.business_sessions.begin() as source_db:
                await AccountingService(
                    source_db, DemoAccountingAdapter(source_db, connection_id)
                ).recheck_invoice(ctx, invoice_key)

        with pytest.raises(AccountingFailure) as error:
            asyncio.run(read())
        rejection = getattr(error.value, "rejection_evidence", None)
        if invalid == "foreign_org":
            assert rejection is None
        else:
            assert rejection is not None
            assert rejection.authenticity == "UNVERIFIED"
            assert (rejection.tenant_id, rejection.provider_connection_id, rejection.external_invoice_id) == (
                tenant,
                connection_id,
                invoice_key,
            )
            assert (
                rejection.reason
                == {
                    "parentless": "payment_shape_invalid",
                    "currency": "payment_currency_mismatch",
                    "parent_unknown": "payment_parent_unresolved",
                }[invalid]
            )
            assert rejection.candidate.external_payment_id == "REJECTED-PAYMENT"
            assert rejection.candidate.external_allocation_id == "REJECTED-ALLOCATION"
            assert rejection.candidate.kind == "reversal"
            if invalid == "currency":
                assert [ref.external_payment_id for ref in rejection.related_reversal_refs] == ["R1"]
                assert not rejection.related_reversal_refs_truncated
            assert "amount" not in rejection.model_dump_json() and "-1000" not in rejection.model_dump_json()
        db.expire_all()
        assert db.scalar(select(func.count()).select_from(AccountingObservation)) == before
        assert db.scalar(select(func.count()).select_from(VerifiedPaymentAllocation)) == facts
        assert invoice.balance_minor == 10000 and invoice.financial_status == "open"
