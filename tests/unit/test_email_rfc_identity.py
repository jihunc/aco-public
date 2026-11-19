"""Provider-observed reply identity and scope; SQLite tests are not migration/live proof."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Table, create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from aco.core.models import register_models
from aco.core.types import CostQuality, ProviderCost, ProviderReference
from aco.features.accounting.models import Customer, CustomerContact, InvoiceSnapshot
from aco.features.communications.linking import invoice_evidence, resolve_thread
from aco.features.communications.models import (
    Communication,
    CommunicationInvoiceLink,
    CommunicationThread,
    EmailDispatchBinding,
    EmailSubmission,
)
from aco.features.identity.models import ProviderConnection, now
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt
from aco.integrations.ports.email_dto import EmailMessage, EmailResult, EmailSource


@dataclass
class Lane:
    db: Session
    connection: ProviderConnection
    message: Communication
    invoice: InvoiceSnapshot
    submission: EmailSubmission
    event: ProviderUsageEvent
    attempt: UsageAttempt
    result: EmailResult[EmailMessage]


@pytest.fixture
def lane() -> Iterator[Lane]:
    from aco.features.communications.models import CommunicationRfcIdentity

    register_models()
    engine = create_engine("sqlite://")
    tables = [
        ProviderConnection,
        CommunicationThread,
        Communication,
        EmailDispatchBinding,
        EmailSubmission,
        ProviderUsageEvent,
        UsageAttempt,
        CommunicationRfcIdentity,
        Customer,
        CustomerContact,
        InvoiceSnapshot,
        CommunicationInvoiceLink,
    ]
    # Exercise real mapped queries/guards without pretending SQLite proves PostgreSQL FKs/triggers.
    with engine.begin() as ddl_connection:
        for model in tables:
            table = model.__table__
            assert isinstance(table, Table)
            ddl_connection.execute(CreateTable(table, include_foreign_key_constraints=[]))
    with Session(engine) as db:
        tenant, customer, invoice = uuid4(), uuid4(), uuid4()
        connection = ProviderConnection(
            tenant_id=tenant, provider="resend", provider_type="email", status="READY"
        )
        db.add(connection)
        # Real current mapped source projection in the SQLite unit fixture. The
        # RFC alias alone cannot supply invoice/customer truth; PG adapter/import
        # and immutable source-trigger evidence remain separate integration tests.
        accounting = ProviderConnection(
            tenant_id=tenant, provider="demo", provider_type="accounting", status="READY"
        )
        db.add(accounting)
        db.flush()
        stamp = now()
        db.add(
            Customer(
                id=customer,
                tenant_id=tenant,
                provider_connection_id=accounting.id,
                external_customer_id="SYNTHETIC-RFC-CUSTOMER",
                name="Synthetic RFC source customer",
                source_revision="1",
                observed_at=stamp,
                synthetic=True,
            )
        )
        current_invoice = InvoiceSnapshot(
            id=invoice,
            tenant_id=tenant,
            provider_connection_id=accounting.id,
            customer_id=customer,
            external_invoice_id="SYNTHETIC-RFC-INVOICE",
            number="SYNTHETIC-RFC-INVOICE",
            issue_date=stamp.date(),
            due_date=(stamp + timedelta(days=14)).date(),
            original_minor=10000,
            balance_minor=10000,
            currency="AUD",
            exponent=2,
            financial_status="open",
            source_status="OPEN",
            source_revision="1",
            organisation_id="synthetic-rfc-organisation",
            effective_at=stamp,
            observed_at=stamp,
            synthetic=True,
            verified=True,
        )
        db.add(current_invoice)
        db.flush()
        thread = CommunicationThread(
            tenant_id=tenant,
            provider_connection_id=connection.id,
            customer_id=customer,
            origin_invoice_id=invoice,
        )
        db.add(thread)
        db.flush()
        message = Communication(
            tenant_id=tenant,
            provider_connection_id=connection.id,
            thread_id=thread.id,
            operation_id=uuid4(),
            native_message_id="sent:" + str(uuid4()),
            content={"message_id": "<composed@aco.example.test>"},
            event_at=now(),
            delivery_state="accepted",
            synthetic=True,
        )
        db.add(message)
        db.flush()
        binding = EmailDispatchBinding(
            tenant_id=tenant,
            communication_id=message.id,
            email_connection_id=connection.id,
            operation_id=message.operation_id,
            action_id=uuid4(),
            activation_id=uuid4(),
            storage_connection_id=uuid4(),
            operation_key="synthetic-operation",
            payload_hash="a" * 64,
            source_revision="fixture",
            command={},
            mime_boundary="fixture",
            mime_sha256="b" * 64,
            contact_reservation_id=uuid4(),
        )
        db.add(binding)
        db.flush()
        correlation = uuid4()
        event = ProviderUsageEvent(
            tenant_id=tenant,
            scope_kind="TENANT",
            scope_key=str(tenant),
            provider_connection_id=connection.id,
            operation_key="observed-read",
            component="email_read",
            operation="email_submission_get_message",
            provider_kind="email",
            provider="resend",
            correlation_id=correlation,
            native_operation_id=None,
            native_subject=message.native_message_id,
            synthetic=True,
        )
        db.add(event)
        db.flush()
        attempt = UsageAttempt(
            tenant_id=tenant,
            provider_connection_id=connection.id,
            correlation_id=correlation,
            actor_id=uuid4(),
            operation=event.operation,
            outcome="SUCCEEDED",
            business_outcome="OBSERVED",
            event_id=event.id,
        )
        submission = EmailSubmission(
            tenant_id=tenant,
            binding_id=binding.id,
            operation_id=message.operation_id,
            state="accepted",
            provider_message_id=message.native_message_id,
        )
        db.add_all([attempt, submission])
        db.flush()
        assert message.native_message_id is not None
        ref = ProviderReference(
            provider="resend", connection_id=connection.id, external_id=message.native_message_id
        )
        result = EmailResult[EmailMessage](
            tenant_id=tenant,
            provider_connection_id=connection.id,
            provider_ref=ProviderReference(
                provider="resend", connection_id=connection.id, external_id="aco-observation:" + str(uuid4())
            ),
            reference_origin="APPLICATION",
            correlation_id=correlation,
            observed_at=now(),
            revision=None,
            native_identity="SIMULATED",
            synthetic=True,
            authenticity="SIMULATED",
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
            value=EmailMessage(
                tenant_id=tenant,
                provider_connection_id=connection.id,
                provider_ref=ref,
                direction="outgoing",
                kind="OUTBOUND_ECHO",
                synthetic=True,
                subject="Synthetic retained source",
                source=EmailSource(kind="UNAVAILABLE"),
                sent_at=now(),
                native_identity="SIMULATED",
                authenticity="SIMULATED",
                message_id="<delivered@provider.test>",
            ),
        )
        yield Lane(db, connection, message, current_invoice, submission, event, attempt, result)
    engine.dispose()


def preserve(lane: Lane) -> object:
    from aco.features.communications.rfc_identity import preserve_observed_rfc_identity

    return preserve_observed_rfc_identity(
        lane.db, submission_id=lane.submission.id, result=lane.result, usage_event_id=lane.event.id
    )


def incoming(lane: Lane) -> EmailMessage:
    return lane.result.value.model_copy(
        update={
            "direction": "incoming",
            "in_reply_to": "<delivered@provider.test>",
            "references": ("<delivered@provider.test>",),
            "text_body": "No invoice number.",
        }
    )


def test_distinct_observed_rfc_alias_matches_reply_without_rewriting_composed_content(lane: Lane) -> None:
    from aco.features.communications.models import CommunicationRfcIdentity

    financial = (lane.invoice.balance_minor, lane.invoice.financial_status, lane.invoice.source_revision)
    identity = preserve(lane)
    assert isinstance(identity, CommunicationRfcIdentity)
    assert identity.rfc_message_id == "<delivered@provider.test>"
    assert identity.observation_origin == "APPLICATION" and identity.provider_request_id is None
    assert preserve(lane) is identity
    assert lane.db.scalar(select(func.count()).select_from(CommunicationRfcIdentity)) == 1
    assert lane.message.content == {"message_id": "<composed@aco.example.test>"}
    resolution = resolve_thread(lane.db, incoming(lane))
    assert resolution.thread is not None and resolution.thread.id == lane.message.thread_id
    assert not resolution.ambiguous
    assert [item.invoice_id for item in invoice_evidence(lane.db, incoming(lane))] == [
        resolution.thread.origin_invoice_id
    ]
    assert (
        lane.invoice.balance_minor,
        lane.invoice.financial_status,
        lane.invoice.source_revision,
    ) == financial


@pytest.mark.parametrize("current", ["missing", "different_customer"])
def test_observed_alias_retains_thread_but_never_attributes_absent_or_corrected_current_invoice(
    lane: Lane, current: str
) -> None:
    preserve(lane)
    if current == "missing":
        lane.db.delete(lane.invoice)
    else:
        customer = Customer(
            tenant_id=lane.invoice.tenant_id,
            provider_connection_id=lane.invoice.provider_connection_id,
            external_customer_id="SYNTHETIC-CORRECTED-CUSTOMER",
            name="Synthetic corrected current customer",
            source_revision="2",
            observed_at=now(),
            synthetic=True,
        )
        lane.db.add(customer)
        lane.db.flush()
        # SQLite fixture state only; the actual source-correction producer/FKs
        # are exercised in PostgreSQL accounting/thread integration tests.
        lane.invoice.customer_id = customer.id
        lane.invoice.source_revision = "2"
    lane.db.flush()
    retained = resolve_thread(lane.db, incoming(lane))
    assert retained.thread is not None and retained.thread.id == lane.message.thread_id
    assert invoice_evidence(lane.db, incoming(lane)) == []
    assert lane.message.content == {"message_id": "<composed@aco.example.test>"}


@pytest.mark.parametrize(
    "field",
    [
        "tenant",
        "connection",
        "native",
        "pending",
        "usage_scope",
        "usage_native",
        "usage_operation",
        "usage_failure",
        "correlation",
        "provenance",
        "incoming",
    ],
)
def test_observation_must_match_accepted_native_submission_and_successful_scoped_usage(
    lane: Lane, field: str
) -> None:
    if field == "tenant":
        lane.result = lane.result.model_copy(update={"tenant_id": uuid4()})
    elif field == "connection":
        lane.result = lane.result.model_copy(update={"provider_connection_id": uuid4()})
    elif field == "native":
        lane.result = lane.result.model_copy(
            update={
                "value": lane.result.value.model_copy(
                    update={
                        "provider_ref": ProviderReference(
                            provider="resend",
                            connection_id=lane.connection.id,
                            external_id="sent:" + str(uuid4()),
                        )
                    }
                )
            }
        )
    elif field == "pending":
        lane.submission.state = "unknown_outcome"
    elif field == "usage_scope":
        lane.event.tenant_id = uuid4()
    elif field == "usage_native":
        lane.event.native_subject = "sent:" + str(uuid4())
    elif field == "usage_operation":
        lane.event.operation = "send_email"
    elif field == "usage_failure":
        lane.attempt.outcome = "FAILED"
    elif field == "correlation":
        lane.event.correlation_id = uuid4()
    elif field == "provenance":
        lane.result = lane.result.model_copy(update={"synthetic": False})
    elif field == "incoming":
        lane.result = lane.result.model_copy(update={"value": incoming(lane)})
    lane.db.flush()
    with pytest.raises(HTTPException, match="RFC_IDENTITY_SOURCE_MISMATCH"):
        preserve(lane)


@pytest.mark.parametrize("scope", ["tenant", "connection"])
def test_observed_alias_never_contributes_context_outside_scope(lane: Lane, scope: str) -> None:
    preserve(lane)
    message = incoming(lane).model_copy(
        update={"tenant_id" if scope == "tenant" else "provider_connection_id": uuid4()}
    )
    assert resolve_thread(lane.db, message).thread is None
    assert invoice_evidence(lane.db, message) == []


@pytest.mark.parametrize("native", [False, True])
def test_alias_collision_or_native_conflict_stays_unmerged_and_has_no_inferred_links(
    lane: Lane, native: bool
) -> None:
    preserve(lane)
    thread = CommunicationThread(
        tenant_id=lane.message.tenant_id,
        provider_connection_id=lane.connection.id,
        purpose="fixture",
        native_thread_id="another-native" if native else None,
    )
    lane.db.add(thread)
    lane.db.flush()
    message = incoming(lane)
    if native:
        message = message.model_copy(
            update={
                "provider_thread_ref": ProviderReference(
                    provider="resend", connection_id=lane.connection.id, external_id="another-native"
                )
            }
        )
    else:
        lane.db.add(
            Communication(
                tenant_id=lane.message.tenant_id,
                provider_connection_id=lane.connection.id,
                thread_id=thread.id,
                operation_id=uuid4(),
                content={"message_id": "<delivered@provider.test>"},
                event_at=now(),
            )
        )
        lane.db.flush()
    resolution = resolve_thread(lane.db, message)
    assert resolution.thread is None and resolution.ambiguous
    assert invoice_evidence(lane.db, message) == []


def test_missing_rfc_metadata_is_incomplete_without_fabricated_alias(lane: Lane) -> None:
    lane.result = lane.result.model_copy(
        update={"value": lane.result.value.model_copy(update={"message_id": None})}
    )
    assert preserve(lane) is None


@pytest.mark.parametrize(
    "rfc_id", ["arbitrary", "<one@test> <two@test>", "<one@@test>", "<one@te st>", "<one@test>\x00"]
)
def test_malformed_observed_rfc_header_never_becomes_matching_authority(lane: Lane, rfc_id: str) -> None:
    lane.result = lane.result.model_copy(
        update={"value": lane.result.value.model_copy(update={"message_id": rfc_id})}
    )
    with pytest.raises(HTTPException, match="RFC_IDENTITY_INVALID_MESSAGE_ID"):
        preserve(lane)


def test_same_source_event_cannot_reassign_or_replace_observed_identity(lane: Lane) -> None:
    preserve(lane)
    lane.result = lane.result.model_copy(
        update={"value": lane.result.value.model_copy(update={"message_id": "<changed@provider.test>"})}
    )
    with pytest.raises(HTTPException, match="RFC_IDENTITY_OBSERVATION_CONFLICT"):
        preserve(lane)
