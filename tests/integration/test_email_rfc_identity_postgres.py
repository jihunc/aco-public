"""Actual PostgreSQL r26 + HTTPX fixture observation; no live email/acceptance proof.

The accepted submission is explicitly a synthetic prerequisite fixture. The
observed RFC ID comes from the real adapter's GET parser, not a model/HTTP setter.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from action_support import action_world, enqueue
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aco.core.context import ActorContext
from aco.core.models import register_models
from aco.core.types import ProviderReference
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.communications.linking import invoice_evidence, resolve_thread
from aco.features.communications.models import (
    Communication,
    CommunicationRfcIdentity,
    CommunicationThread,
    EmailDispatchBinding,
    EmailExecutionActivation,
    EmailSubmission,
)
from aco.features.communications.rfc_identity import READ_OPERATION, preserve_observed_rfc_identity
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.policies.models import ContactReservation
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.models import UsageAttempt
from aco.features.usage.service import UsageService
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.resend.adapter import ResendEmailAdapter
from aco.integrations.resend.settings import ResendBinding


def test_observed_rfc_identity_actual_parser_postgres_scope_and_immutable_history() -> None:
    register_models()
    with action_world() as world:
        db, ctx = world.store, world.ctx
        action = enqueue(world)
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        membership = db.scalar(select(Membership).where(Membership.tenant_id == ctx.tenant_id))
        assert invoice is not None and membership is not None
        email = ProviderConnection(
            tenant_id=ctx.tenant_id, provider="resend", provider_type="email", status="READY"
        )
        storage = ProviderConnection(
            tenant_id=ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
        )
        db.add_all([email, storage])
        db.flush()
        activation = EmailExecutionActivation(
            tenant_id=ctx.tenant_id,
            email_connection_id=email.id,
            email_connection_version=email.version,
            storage_connection_id=storage.id,
            storage_connection_version=storage.version,
            reviewer_id=ctx.actor_id,
            membership_version=membership.version,
            sender="sender@synthetic.invalid",
            destinations=["customer@synthetic.invalid"],
            request_cap=3,
            capture_policy="synthetic_30_days",
            evidence="Explicit synthetic accepted-submission prerequisite, not a live authorization",
            expires_at=now() + timedelta(minutes=5),
        )
        thread = CommunicationThread(
            tenant_id=ctx.tenant_id,
            provider_connection_id=email.id,
            origin_invoice_id=invoice.id,
            customer_id=invoice.customer_id,
        )
        db.add_all([activation, thread])
        db.flush()
        native = uuid4()
        message = Communication(
            tenant_id=ctx.tenant_id,
            provider_connection_id=email.id,
            thread_id=thread.id,
            origin_invoice_id=invoice.id,
            operation_id=uuid4(),
            native_message_id="sent:" + str(native),
            content={"message_id": "<composed@aco.example.test>"},
            event_at=now(),
            synthetic=True,
            direction="outgoing",
            delivery_state="accepted",
        )
        db.add(message)
        db.flush()
        contact = ContactReservation(
            tenant_id=ctx.tenant_id,
            invoice_id=invoice.id,
            customer_id=invoice.customer_id,
            destination="customer@synthetic.invalid",
            channel="email",
            operation_id=message.operation_id,
            action_id=action.action_id,
            reserved_at=now(),
            status="accepted",
        )
        db.add(contact)
        db.flush()
        binding = EmailDispatchBinding(
            tenant_id=ctx.tenant_id,
            action_id=action.action_id,
            communication_id=message.id,
            activation_id=activation.id,
            email_connection_id=email.id,
            storage_connection_id=storage.id,
            operation_id=message.operation_id,
            operation_key="explicit-synthetic-fixture",
            payload_hash="a" * 64,
            source_revision=invoice.source_revision,
            command={},
            mime_boundary="fixture",
            mime_sha256="b" * 64,
            contact_reservation_id=contact.id,
        )
        db.add(binding)
        db.flush()
        submission = EmailSubmission(
            tenant_id=ctx.tenant_id,
            binding_id=binding.id,
            operation_id=message.operation_id,
            state="accepted",
            provider_message_id=message.native_message_id,
        )
        db.add(submission)
        db.commit()
        expected = ProviderReference(
            provider="resend", connection_id=email.id, external_id="sent:" + str(native)
        )
        calls: list[str] = []

        async def authority(
            actual: ActorContext | EmailIngressContext | EmailWebhookContext,
            operation: str,
            material: object | None,
            /,
        ) -> None:
            assert actual == ctx and operation == "get_message" and material == expected

        def fixture(request: httpx.Request) -> httpx.Response:
            calls.append(request.method + " " + request.url.path)
            assert request.method == "GET" and request.url.path == "/emails/" + str(native)
            return httpx.Response(
                200,
                json={
                    "object": "email",
                    "id": str(native),
                    "message_id": "<provider-delivered@synthetic.invalid>",
                    "from": "sender@synthetic.invalid",
                    "to": ["customer@synthetic.invalid"],
                    "subject": "Explicit synthetic fixture",
                    "created_at": now().isoformat(),
                    "text": "Synthetic metadata only",
                    "html": None,
                },
            )

        adapter = ResendEmailAdapter(
            ResendBinding(
                tenant_id=ctx.tenant_id,
                connection_id=email.id,
                api_key=SecretStr("offline-not-a-key"),
                inbox_addresses=("sender@synthetic.invalid",),
                download_hosts=frozenset(),
                evidence_mode="MOCKED_HTTP",
            ),
            authority=authority,
            enabled=True,
            transport=httpx.MockTransport(fixture),
        )
        observed = asyncio.run(adapter.get_message(ctx, expected))
        event = UsageService(db).record_verified(
            ctx,
            ProviderUsageInput(
                provider_connection_id=email.id,
                operation_key=str(uuid4()),
                component="email_http_request",
                operation=READ_OPERATION,
                provider_kind="email",
                provider="resend",
                synthetic=True,
                cost=observed.cost,
                native_operation_id=observed.provider_request_id,
                native_subject=expected.external_id,
                outcome="SUCCEEDED",
            ),
        )
        db.add(
            UsageAttempt(
                tenant_id=ctx.tenant_id,
                provider_connection_id=email.id,
                correlation_id=ctx.correlation_id,
                actor_id=ctx.actor_id,
                operation=READ_OPERATION,
                outcome="SUCCEEDED",
                business_outcome="OBSERVED",
                event_id=event.id,
            )
        )
        db.flush()
        alias = preserve_observed_rfc_identity(
            db, submission_id=submission.id, result=observed, usage_event_id=event.id
        )
        assert alias is not None
        alias_id = alias.id
        db.commit()
        assert calls == ["GET /emails/" + str(native)]
        assert message.content["message_id"] == "<composed@aco.example.test>"
        repeated = preserve_observed_rfc_identity(
            db, submission_id=submission.id, result=observed, usage_event_id=event.id
        )
        assert repeated is not None and repeated.id == alias_id
        reply = observed.value.model_copy(
            update={
                "direction": "incoming",
                "in_reply_to": alias.rfc_message_id,
                "references": (alias.rfc_message_id,),
                "text_body": "No invoice mention.",
            }
        )
        resolved = resolve_thread(db, reply).thread
        assert resolved is not None and resolved.id == thread.id
        assert [link.invoice_id for link in invoice_evidence(db, reply)] == [invoice.id]
        db.commit()
        for sql in (
            "UPDATE communication_rfc_identities SET rfc_message_id='<rewritten@test.invalid>' WHERE id=:id",
            "DELETE FROM communication_rfc_identities WHERE id=:id",
        ):
            with pytest.raises(DBAPIError, match="RFC identity history is immutable"):
                with db.begin_nested():
                    db.execute(text(sql), {"id": alias_id})
        retained = db.get(CommunicationRfcIdentity, alias_id)
        assert retained is not None and retained.rfc_message_id == "<provider-delivered@synthetic.invalid>"
