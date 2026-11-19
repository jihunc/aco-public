"""Reject widened source evidence through real SDK/worker/domain command validation."""

import asyncio
from uuid import UUID

import pytest
from accounting_support import document_apply, document_source, seed
from case_promise_support import communication
from evaluations.test_collection_agent import dispute, proposal, run_sdk
from ingress_support import ingress_world
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.models import CollectionCase
from aco.features.communications.models import Communication, CommunicationInvoiceLink, EmailIngressReceipt
from aco.features.promises.models import PaymentPromise
from aco.features.review.models import CollectionTask


@pytest.mark.parametrize(
    "boundary",
    [
        "unselected",
        "unlinked",
        "other_customer",
        "other_connection",
        "other_tenant",
        "ambiguous",
        "forged_span",
        "unread",
    ],
)
def test_actual_sdk_rejects_widened_original_evidence(boundary: str) -> None:
    with ingress_world() as lane:
        world, db = lane.world, lane.world.store
        rows = {r.number: r for r in db.scalars(select(InvoiceSnapshot))}
        first, target = rows["INV-101"], rows["INV-107"]
        body = "Explicit synthetic invoice dispute: INV-101" + (
            " and INV-107." if boundary != "unlinked" else "."
        )
        message = lane.message(body=body)
        db.rollback()

        async def capture() -> UUID:
            key = await lane.deliver(message)
            # Capture the actual signed provider source without producing an automatic run.
            await lane.work(None)
            return UUID(str(key))

        receipt_id = asyncio.run(capture())
        db.rollback()
        receipt = db.get(EmailIngressReceipt, receipt_id)
        assert receipt and receipt.communication_id and receipt.agent_run_id is None
        original = db.get(Communication, receipt.communication_id)
        assert original and original.source_state == "stored"
        selected = [first, target]
        if boundary == "unselected":
            selected = [first]
        elif boundary == "other_customer":
            target = rows["INV-102"]
            selected = [first, target]
        elif boundary == "other_connection":
            source = document_source(db, world.ctx.tenant_id)
            db.rollback()
            document_apply(world.client.app, world.ctx, source)
            other_invoice = db.scalar(
                select(InvoiceSnapshot).where(InvoiceSnapshot.provider_connection_id == source.connection_id)
            )
            assert other_invoice is not None
            target = other_invoice
            selected = [first, target]
        elif boundary == "other_tenant":
            created = world.client.post(
                "/api/workspaces", json=dict(name="Foreign synthetic scope", timezone="UTC")
            )
            assert created.status_code == 201, created.text
            foreign_id = UUID(created.json()["id"])
            seed(world.client, foreign_id)
            foreign_invoice = db.scalar(
                select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == foreign_id)
            )
            assert foreign_invoice
            original = communication(db, foreign_id, [foreign_invoice], body)
            assert original.source_state == "synthetic_fixture"  # no forged private-provider provenance
        elif boundary == "ambiguous":
            db.add(
                CommunicationInvoiceLink(
                    tenant_id=world.ctx.tenant_id,
                    communication_id=original.id,
                    invoice_id=target.id,
                    claim_or_context_key="explicit-adversarial-ambiguous-link",
                    confidence="0.5",
                    provenance="explicit_reference",
                    evidence_excerpt=body,
                    evidence_offset=0,
                    classification="explicit_synthetic_negative_fixture",
                )
            )
            db.commit()
        command = dispute([target], original)
        if boundary == "forged_span":
            command["evidence"] = dict(
                communication_id=str(original.id), offset=0, excerpt=body + " Not read."
            )
        saved = run_sdk(
            world,
            selected,
            [proposal(target, "upsert_case", case_command=command)],
            extra_reads=[] if boundary == "unread" else [("get_communication_history", first.id)],
        )
        assert saved["status"] == "failed", saved
        assert saved["execution"] not in {"EXECUTED", "PARTIAL"}
        for model in (CollectionCase, PaymentPromise, CollectionTask):
            assert db.scalar(select(func.count()).select_from(model)) == 0
