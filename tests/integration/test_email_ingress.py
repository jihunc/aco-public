"""Actual synthetic inbound receipts and originals; never verified live ingress."""

import pytest
from boundary_evidence import BoundaryObserver

from aco.features.communications.linking import sanitized_html


def test_hostile_html_has_no_active_content_or_remote_resources() -> None:
    result = sanitized_html(
        '<p>Hello<img src="https://tracker.invalid/p"><script>alert(1)</script>'
        '<a href="javascript:alert(2)" onclick="evil()">invoice</a>'
        "<style>body{background:url(https://tracker.invalid)}</style></p>"
    )
    assert result is not None
    assert "Hello" in result and "invoice" in result
    for forbidden in ("script", "onclick", "javascript", "img", "style", "tracker.invalid"):
        assert forbidden not in result


def test_signed_notification_actual_fetch_private_original_and_worker_receipt() -> None:
    import asyncio

    from ingress_support import ingress_world
    from sqlalchemy import select

    from aco.features.communications.models import (
        Communication,
        CommunicationInvoiceLink,
        CommunicationSource,
        EmailIngressReceipt,
    )

    with ingress_world() as lane:
        message = lane.message()
        lane.world.store.rollback()
        receipt_id = asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        lane.world.store.rollback()
        receipt = lane.world.store.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.state == "review_required", (
            receipt.error_code if receipt else None
        )
        communication = lane.world.store.get(Communication, receipt.communication_id)
        assert communication is not None and communication.source_state == "stored"
        assert communication.direction == "incoming" and communication.synthetic
        links = list(
            lane.world.store.scalars(
                select(CommunicationInvoiceLink).where(
                    CommunicationInvoiceLink.communication_id == communication.id
                )
            )
        )
        assert [link.invoice_id for link in links] == [lane.world.invoice_id]
        assert links[0].evidence_excerpt == "INV-101"
        source = lane.world.store.scalar(
            select(CommunicationSource).where(CommunicationSource.communication_id == communication.id)
        )
        assert source is not None and source.sha256 == message.source.integrity_sha256
        assert receipt.job_id is not None and receipt.agent_run_id is None


def test_one_original_links_three_invoices_and_deduplicates_event_and_message_separately() -> None:
    import asyncio

    from ingress_support import ingress_world
    from sqlalchemy import func, select

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.communications.models import (
        Communication,
        CommunicationInvoiceLink,
        CommunicationSource,
        EmailIngressReceipt,
    )

    with ingress_world() as lane:
        message = lane.message(body="Synthetic source mentions INV-101, INV-107 and INV-113.")
        raw, headers = lane.notification(message)
        lane.world.store.rollback()
        first = asyncio.run(lane.deliver(message, raw, headers))
        assert asyncio.run(lane.deliver(message, raw, headers)) == first
        second = asyncio.run(lane.deliver(message))
        assert second != first
        asyncio.run(lane.work())
        lane.world.store.rollback()
        db = lane.world.store
        assert db.scalar(select(func.count()).select_from(Communication)) == 1
        assert db.scalar(select(func.count()).select_from(EmailIngressReceipt)) == 2
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        expected = set(
            db.scalars(
                select(InvoiceSnapshot.id).where(
                    InvoiceSnapshot.number.in_(["INV-101", "INV-107", "INV-113"])
                )
            )
        )
        assert set(db.scalars(select(CommunicationInvoiceLink.invoice_id))) == expected
        assert all(row.state == "review_required" for row in db.scalars(select(EmailIngressReceipt)))


def test_second_native_thread_retains_new_original_and_echo_never_produces_agent_run() -> None:
    import asyncio

    from ingress_support import ingress_world
    from sqlalchemy import func, select

    from aco.features.communications.models import (
        Communication,
        CommunicationSource,
        CommunicationThread,
        EmailIngressReceipt,
    )

    with ingress_world() as lane:
        first, second, echo = lane.message(), lane.message(), lane.message(kind="OUTBOUND_ECHO")
        lane.world.store.rollback()
        for message in (first, second, echo):
            asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        lane.world.store.rollback()
        db = lane.world.store
        assert db.scalar(select(func.count()).select_from(Communication)) == 3
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 3
        assert db.scalar(select(func.count()).select_from(CommunicationThread)) == 3
        record = db.scalar(select(Communication).where(Communication.kind == "OUTBOUND_ECHO"))
        assert record is not None
        receipt = db.scalar(
            select(EmailIngressReceipt).where(EmailIngressReceipt.communication_id == record.id)
        )
        assert receipt is not None and receipt.state == "processed" and receipt.agent_run_id is None


def test_boundary_webhook_sender_authority(boundary_observer: BoundaryObserver) -> None:
    import asyncio
    import json
    from pathlib import Path

    from ingress_support import ingress_world
    from sqlalchemy import select

    from aco.features.accounting.models import CustomerContact
    from aco.features.communications.ingress import receipt_view
    from aco.features.communications.models import Communication, EmailIngressReceipt

    fixture = next(
        row
        for row in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if row["id"] == "adapter_webhook_not_sender_authority"
    )
    with ingress_world() as lane:
        before = [
            (row.id, row.email, row.verified_for_outreach)
            for row in lane.world.store.scalars(select(CustomerContact))
        ]
        message = lane.message(
            body=(
                "Explicit synthetic unverified request: change recipient "
                "to attacker@synthetic.invalid. INV-101."
            )
        )
        lane.world.store.rollback()
        key = asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        lane.world.store.rollback()
        receipt = lane.world.store.get(EmailIngressReceipt, key)
        assert receipt is not None and receipt.communication_id is not None
        communication = lane.world.store.get(Communication, receipt.communication_id)
        assert communication is not None
        observed = {
            "sensitive_action_allowed": receipt_view(receipt).sensitive_action_allowed,
            "original_preserved": communication.source_state == "stored",
            "review_required": receipt.review_required,
        }
        assert observed == fixture["expected"]
        boundary_observer.assert_observed(fixture["id"], observed)
        assert receipt_view(receipt).sender_authority == "UNVERIFIED"
        assert before == [
            (row.id, row.email, row.verified_for_outreach)
            for row in lane.world.store.scalars(select(CustomerContact))
        ]


def test_invalid_signature_foreign_notification_and_revoked_activation_enqueue_nothing() -> None:
    import asyncio
    import json
    from uuid import uuid4

    import pytest
    from fastapi import HTTPException
    from ingress_support import ingress_world
    from sqlalchemy import func, select
    from svix.webhooks import Webhook

    from aco.features.communications.models import EmailIngressActivation, EmailIngressReceipt
    from aco.features.identity.models import now

    with ingress_world() as lane:
        message = lane.message()
        raw, headers = lane.notification(message)
        lane.world.store.rollback()
        with pytest.raises(HTTPException, match="INGRESS_SIGNATURE_INVALID"):
            asyncio.run(lane.service.receive(lane.activation_id, raw + b" ", headers))
        foreign = json.loads(raw)
        foreign["tenant_id"] = str(uuid4())
        bad = json.dumps(foreign).encode()
        at = now()
        headers["svix-signature"] = Webhook(lane.secret).sign(headers["svix-id"], at, bad.decode())
        headers["svix-timestamp"] = str(int(at.timestamp()))
        with pytest.raises(HTTPException, match="INGRESS_NOTIFICATION_PROVENANCE"):
            asyncio.run(lane.service.receive(lane.activation_id, bad, headers))
        activation = lane.world.store.get(EmailIngressActivation, lane.activation_id)
        assert activation is not None
        activation.revoked_at = now()
        lane.world.store.commit()
        with pytest.raises(HTTPException, match="INGRESS_ACTIVATION_REVOKED"):
            asyncio.run(lane.service.receive(lane.activation_id, raw, headers))
        assert lane.world.store.scalar(select(func.count()).select_from(EmailIngressReceipt)) == 0


def test_unmatched_inbound_preserves_original_and_is_readable_with_nullable_scope() -> None:
    import asyncio

    from ingress_support import ingress_world
    from sqlalchemy import func, select

    from aco.features.communications.models import CommunicationInvoiceLink, EmailIngressReceipt
    from aco.features.communications.queries import CommunicationQueries

    with ingress_world() as lane:
        message = lane.message().model_copy(update={"sender": "unknown@synthetic.invalid"})
        lane.world.store.rollback()
        key = asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        lane.world.store.rollback()
        receipt = lane.world.store.get(EmailIngressReceipt, key)
        assert receipt is not None and receipt.communication_id is not None
        view = CommunicationQueries(lane.world.store, lane.world.ctx).get(receipt.communication_id)
        assert view.origin_invoice_id is None and view.source_state == "stored"
        assert view.sources[0].available
        assert lane.world.store.scalar(select(func.count()).select_from(CommunicationInvoiceLink)) == 0


def test_ingress_http_activation_requires_current_human_csrf_and_defaults_off() -> None:
    from typing import cast

    from fastapi import FastAPI
    from ingress_support import ingress_world

    from aco.features.communications.ingress_api import router

    with ingress_world() as lane:
        app = cast(FastAPI, lane.world.client.app)
        app.include_router(router)
        path = f"/api/workspaces/{lane.world.ctx.tenant_id}/email-ingress/activations"
        command = {
            "email_connection_id": str(lane.email_id),
            "storage_connection_id": str(lane.storage_id),
            "evidence": "Explicit synthetic API capture review, no live capture.",
        }
        response = lane.world.client.post(path, json=command)
        assert response.status_code == 403 and response.json()["detail"] == "SYNTHETIC_INGRESS_DISABLED"
        app.state.email_ingress = lane.service
        response = lane.world.client.post(path, json=command)
        assert response.status_code == 200, response.text
        view = response.json()
        revoked = lane.world.client.post(
            path + "/" + view["id"] + "/revoke", json={"expected_version": view["version"]}
        )
        assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
        lane.world.client.headers.pop("x-csrf-token", None)
        assert lane.world.client.post(path, json=command).status_code == 403


def test_capture_failure_retries_same_original_and_exact_capture_scope(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    import asyncio
    from uuid import uuid4

    import pytest
    from fastapi import HTTPException
    from ingress_support import ingress_world
    from sqlalchemy import func, select

    from aco.features.artifacts.dto import ArtifactRef, ArtifactWrite
    from aco.features.artifacts.service import ArtifactService
    from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
    from aco.integrations.ports.email_context import EmailIngressContext

    original = ArtifactService.store_inbound_artifact
    attempts = []

    async def archive(
        service: ArtifactService, ctx: EmailIngressContext, command: ArtifactWrite
    ) -> ArtifactRef:
        attempts.append(command.artifact_id)
        forged = command.model_copy(update={"artifact_id": uuid4()})
        with pytest.raises(HTTPException, match="INGRESS_CAPTURE_SCOPE"):
            await original(service, ctx, forged)
        if len(attempts) == 1:
            raise HTTPException(503, "INJECTED_ARCHIVE_OUTAGE")
        return await original(service, ctx, command)

    monkeypatch.setattr(ArtifactService, "store_inbound_artifact", archive)
    with ingress_world() as lane:
        message = lane.message()
        lane.world.store.rollback()
        key = asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        lane.world.store.rollback()
        row = lane.world.store.get(EmailIngressReceipt, key)
        assert row is not None and row.state == "failed"
        communication_id = row.communication_id
        assert communication_id is not None
        pending = lane.world.store.get(Communication, communication_id)
        assert pending is not None and pending.source_state == "pending"
        lane.service.retry(lane.world.store, lane.world.ctx, key, row.version)
        lane.world.store.commit()
        asyncio.run(lane.work())
        lane.world.store.rollback()
        completed = lane.world.store.get(EmailIngressReceipt, key, populate_existing=True)
        assert completed is not None and completed.state == "review_required"
        assert completed.communication_id == communication_id
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert lane.world.store.scalar(select(func.count()).select_from(Communication)) == 1
        assert lane.world.store.scalar(select(func.count()).select_from(CommunicationSource)) == 1


def test_mime_attachments_private_exact_original_and_unsafe_media_quarantined() -> None:
    import asyncio
    from email.message import EmailMessage as MimeMessage
    from hashlib import sha256

    from artifact_support import storage_configuration
    from ingress_support import ingress_world
    from sqlalchemy import select

    from aco.features.artifacts.models import Artifact
    from aco.features.artifacts.service import ArtifactService
    from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
    from aco.integrations.ports.email_dto import EmailSource
    from aco.integrations.s3.adapter import StorageRuntime

    with ingress_world() as lane:
        message = lane.message()
        mime = MimeMessage()
        mime["From"], mime["To"], mime["Subject"] = message.sender, message.to[0], message.subject
        mime.set_content(message.text_body)
        mime.add_attachment(
            b"%PDF-1.7 synthetic fixture", maintype="application", subtype="pdf", filename="synthetic.pdf"
        )
        mime.add_attachment(
            b"explicit synthetic unsafe bytes",
            maintype="application",
            subtype="x-executable",
            filename="unsafe.exe",
        )
        raw = mime.as_bytes()
        message = message.model_copy(
            update={
                "source": EmailSource(
                    kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
                )
            }
        )
        lane.world.store.rollback()
        key = asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        lane.world.store.rollback()
        row = lane.world.store.get(EmailIngressReceipt, key)
        assert row is not None and row.communication_id is not None
        communication = lane.world.store.get(Communication, row.communication_id)
        assert communication is not None and communication.source_state == "stored"
        sources = list(
            lane.world.store.scalars(
                select(CommunicationSource).where(CommunicationSource.communication_id == communication.id)
            )
        )
        assert len(sources) == 3
        artifacts = [lane.world.store.get(Artifact, source.artifact_id) for source in sources]
        assert all(artifact is not None for artifact in artifacts)
        assert sorted(artifact.storage_state for artifact in artifacts if artifact is not None) == [
            "quarantined",
            "stored",
            "stored",
        ]
        original = next(source for source in sources if source.representation == "provider_original")
        lane.world.store.rollback()
        service = ArtifactService(lane.service.sessions, StorageRuntime(**storage_configuration()), "local")
        download = asyncio.run(service.authorize_artifact_read(lane.world.ctx, original.artifact_id))
        assert download.body == raw and download.sha256 == sha256(raw).hexdigest()


def test_actual_worker_interruption_recovers_same_receipt_and_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import multiprocessing
    from datetime import timedelta

    from ingress_support import crash_after_original_identity, ingress_world
    from sqlalchemy import func, select

    from aco.features.communications import ingress
    from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
    from aco.features.identity.models import now

    with ingress_world() as lane:
        message = lane.message()
        lane.world.store.rollback()
        key = asyncio.run(lane.deliver(message))
        process = multiprocessing.get_context("spawn").Process(
            target=crash_after_original_identity, args=(lane.world.url, lane.secret)
        )
        process.start()
        process.join(timeout=30)
        if process.is_alive():
            process.kill()
            process.join()
            pytest.fail("Owned ingress worker did not reach its interruption checkpoint")
        assert process.exitcode == 87
        row = lane.world.store.get(EmailIngressReceipt, key, populate_existing=True)
        assert row is not None and row.state == "fetching" and row.communication_id is not None
        original_id, first_job = row.communication_id, row.job_id
        lane.world.store.rollback()
        # Advance only the recovery clock after a genuine process exit; no fabricated job or lease rows.
        future = now() + timedelta(seconds=120)
        with monkeypatch.context() as clock:
            clock.setattr(ingress, "now", lambda: future)
            assert asyncio.run(ingress.recover_email_ingress(lane.service.sessions)) == 1
        asyncio.run(lane.work())
        row = lane.world.store.get(EmailIngressReceipt, key, populate_existing=True)
        assert row is not None and row.state == "review_required"
        assert row.communication_id == original_id and row.job_id != first_job
        assert lane.world.store.scalar(select(func.count()).select_from(Communication)) == 1
        assert lane.world.store.scalar(select(func.count()).select_from(CommunicationSource)) == 1


def test_activation_revoked_after_notification_fails_before_fetch() -> None:
    import asyncio

    from ingress_support import ingress_world
    from sqlalchemy import func, select

    from aco.features.communications.models import Communication, EmailIngressActivation, EmailIngressReceipt
    from aco.features.identity.models import now

    with ingress_world() as lane:
        message = lane.message()
        lane.world.store.rollback()
        key = asyncio.run(lane.deliver(message))
        activation = lane.world.store.get(EmailIngressActivation, lane.activation_id)
        assert activation is not None
        activation.revoked_at = now()
        lane.world.store.commit()
        asyncio.run(lane.work())
        row = lane.world.store.get(EmailIngressReceipt, key, populate_existing=True)
        assert row is not None and row.state == "failed" and row.error_code == "INGRESS_ACTIVATION_REVOKED"
        assert lane.world.store.scalar(select(func.count()).select_from(Communication)) == 0


def test_unavailable_original_keeps_opaque_attachment_reference_without_fetching_urls() -> None:
    import asyncio

    from ingress_support import ingress_world

    from aco.core.types import ProviderReference
    from aco.features.communications.models import EmailIngressReceipt
    from aco.features.communications.queries import CommunicationQueries
    from aco.integrations.ports.email_dto import EmailAttachment, EmailObjectReference, EmailSource

    with ingress_world() as lane:
        opaque = EmailObjectReference(
            provider_ref=ProviderReference(
                provider="demo_email",
                connection_id=lane.email_id,
                external_id="https://untrusted.synthetic.invalid/not-a-fetch-authority",
            )
        )
        attachment = EmailAttachment(filename="unknown.pdf", content_type="application/pdf", reference=opaque)
        message = lane.message().model_copy(
            update={"source": EmailSource(kind="UNAVAILABLE"), "attachments": (attachment,)}
        )
        lane.world.store.rollback()
        key = asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        lane.world.store.rollback()
        receipt = lane.world.store.get(EmailIngressReceipt, key)
        assert receipt is not None and receipt.communication_id is not None and receipt.agent_run_id is None
        view = CommunicationQueries(lane.world.store, lane.world.ctx).get(receipt.communication_id)
        assert view.source_state == "unavailable" and view.sources == []
        assert view.content.attachments == (attachment,)


def test_native_reply_retains_origin_invoice_and_exact_other_invoice_mention() -> None:
    import asyncio

    from ingress_support import ingress_world
    from sqlalchemy import select

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.communications.models import (
        Communication,
        CommunicationInvoiceLink,
        EmailIngressReceipt,
    )

    with ingress_world() as lane:
        first = lane.message(thread="synthetic-origin-thread")
        lane.world.store.rollback()
        first_receipt_id = asyncio.run(lane.deliver(first))
        asyncio.run(lane.work())
        reply = lane.message(thread="synthetic-origin-thread", body="Synthetic reply also mentions INV-107.")
        lane.world.store.rollback()
        reply_receipt_id = asyncio.run(lane.deliver(reply))
        asyncio.run(lane.work())
        db = lane.world.store
        db.rollback()
        first_receipt = db.get(EmailIngressReceipt, first_receipt_id)
        reply_receipt = db.get(EmailIngressReceipt, reply_receipt_id)
        assert first_receipt is not None and reply_receipt is not None
        original = db.get(Communication, first_receipt.communication_id)
        received = db.get(Communication, reply_receipt.communication_id)
        assert original is not None and received is not None
        assert received.thread_id == original.thread_id
        assert received.origin_invoice_id == lane.world.invoice_id
        other = db.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.number == "INV-107"))
        assert other is not None
        links = {
            row.invoice_id: row
            for row in db.scalars(
                select(CommunicationInvoiceLink).where(
                    CommunicationInvoiceLink.communication_id == received.id
                )
            )
        }
        assert set(links) == {lane.world.invoice_id, other.id}
        assert links[lane.world.invoice_id].provenance == "origin_thread"
        assert links[other.id].provenance == "explicit_reference"
        assert links[other.id].evidence_excerpt == "INV-107"
        assert reply.text_body is not None
        assert links[other.id].evidence_offset == reply.text_body.index("INV-107")
