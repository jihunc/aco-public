"""Genuine confirmed commitments feed the canonical action outbox; never outreach authority."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import Role, StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.actions.service import ActionService, ScheduledActionRef
from aco.features.identity.models import Actor, Membership, ProviderConnection
from aco.features.identity.service import ROLE_PERMISSIONS, IdentityService, Permission
from aco.features.promises.models import PaymentPromise, PromiseEvent, PromiseInstallment, PromiseInvoiceLink


class PromiseCheckCommand(StrictModel):
    promise_id: UUID
    installment_id: UUID
    confirmation_event_id: UUID
    invoice_ids: list[UUID] = Field(min_length=1, max_length=100)


SchedulingStatus = Literal[
    "PENDING_ENGINE", "PENDING_CONFIRMATION", "QUEUED", "RUNNING", "COMPLETED", "REVIEW_REQUIRED"
]


def commitment_snapshot(db: Session, promise: PaymentPromise) -> dict[str, object]:
    links = list(
        db.scalars(
            select(PromiseInvoiceLink)
            .where(
                PromiseInvoiceLink.tenant_id == promise.tenant_id,
                PromiseInvoiceLink.promise_id == promise.id,
            )
            .order_by(PromiseInvoiceLink.invoice_id)
        )
    )
    installments = list(
        db.scalars(
            select(PromiseInstallment)
            .where(
                PromiseInstallment.tenant_id == promise.tenant_id,
                PromiseInstallment.promise_id == promise.id,
            )
            .order_by(PromiseInstallment.sequence, PromiseInstallment.id)
        )
    )
    return {
        "promise_id": str(promise.id),
        "customer_id": str(promise.customer_id),
        "provider_connection_id": str(promise.provider_connection_id),
        "source_communication_id": str(promise.source_communication_id),
        "confirmation_communication_id": str(promise.confirmation_communication_id),
        "confirmed_at": promise.confirmed_at.isoformat() if promise.confirmed_at else None,
        "amount": promise.amount,
        "timezone": promise.timezone,
        "invoice_links": [{"invoice_id": str(link.invoice_id), "amount": link.amount} for link in links],
        "installments": [
            {
                "id": str(item.id),
                "sequence": item.sequence,
                "cutoff_at": item.cutoff_at.isoformat(),
                "amount": item.amount,
                "allocations": item.allocations,
            }
            for item in installments
        ],
    }


def validate_promise_check(db: Session, action: ScheduledAction) -> PaymentPromise:
    """Domain scope validator; shared action authorization separately proves current worker/job/lease."""
    if action.kind != "promise_check" or action.reservation_id is not None:
        raise HTTPException(403, "INVALID_PROMISE_CHECK_TRIGGER")
    try:
        command = PromiseCheckCommand.model_validate_json(json.dumps(action.command))
    except ValidationError as error:
        raise HTTPException(403, "INVALID_PROMISE_CHECK_COMMAND") from error
    event = db.get(PromiseEvent, action.promise_event_id)
    installment = db.get(PromiseInstallment, action.promise_installment_id, populate_existing=True)
    promise = db.get(PaymentPromise, command.promise_id, populate_existing=True)
    if (
        event is None
        or installment is None
        or promise is None
        or event.tenant_id != action.tenant_id
        or installment.tenant_id != action.tenant_id
        or promise.tenant_id != action.tenant_id
        or event.id != command.confirmation_event_id
        or installment.id != command.installment_id
        or event.promise_id != promise.id
        or installment.promise_id != promise.id
        or event.event != "promise.confirmed"
        or event.actor_id != action.initiator_id
        or promise.status not in {"ACTIVE", "FULFILLED", "MISSED"}
        or promise.confirmed_at is None
        or action.due_at < installment.cutoff_at
        or event.details.get("commitment") != commitment_snapshot(db, promise)
    ):
        raise HTTPException(403, "PROMISE_CHECK_COMMITMENT_CHANGED")
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == action.tenant_id, Membership.user_id == action.initiator_id)
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, action.initiator_id, populate_existing=True)
    if (
        member is None
        or not member.active
        or member.version != action.membership_version
        or actor is None
        or not actor.active
        or actor.kind != "human"
        or Permission.RESOLVE not in ROLE_PERMISSIONS[Role(member.role)]
    ):
        raise HTTPException(403, "PROMISE_CONFIRMING_AUTHORITY_REVOKED")
    links = list(
        db.scalars(
            select(PromiseInvoiceLink)
            .where(
                PromiseInvoiceLink.tenant_id == action.tenant_id, PromiseInvoiceLink.promise_id == promise.id
            )
            .execution_options(populate_existing=True)
        )
    )
    actual = sorted((link.invoice_id for link in links), key=str)
    if (
        command.invoice_ids != actual
        or len(set(actual)) != len(actual)
        or action.invoice_id not in {UUID(str(part["invoice_id"])) for part in installment.allocations}
    ):
        raise HTTPException(403, "PROMISE_CHECK_SCOPE_CHANGED")
    connection = db.get(ProviderConnection, action.provider_connection_id, populate_existing=True)
    if (
        connection is None
        or connection.tenant_id != action.tenant_id
        or connection.id != promise.provider_connection_id
        or connection.provider_type != "accounting"
        or connection.status != "READY"
        or connection.version != action.connection_version
        or connection.provider not in {"demo", "mock"}
    ):
        raise HTTPException(403, "PROMISE_CHECK_CONNECTION_CHANGED")
    for key in actual:
        invoice = db.get(InvoiceSnapshot, key, populate_existing=True)
        if (
            invoice is None
            or invoice.tenant_id != action.tenant_id
            or invoice.customer_id != promise.customer_id
            or invoice.provider_connection_id != connection.id
        ):
            raise HTTPException(403, "PROMISE_CHECK_SCOPE_CHANGED")
    return promise


def schedule_promise_checks(
    db: Session, ctx: TenantContext, promise_id: UUID, expected_version: int
) -> list[ScheduledActionRef]:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_CONFIRMATION_REQUIRED")
    identity = IdentityService(db)
    tenant = identity.authorize(ctx, Permission.RESOLVE)
    promise = db.scalar(
        select(PaymentPromise)
        .where(PaymentPromise.tenant_id == ctx.tenant_id, PaymentPromise.id == promise_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        promise is None
        or promise.version != expected_version
        or promise.status not in {"ACTIVE", "FULFILLED", "MISSED"}
    ):
        raise HTTPException(409, "PROMISE_CHECK_VERSION_CHANGED")
    event = db.scalar(
        select(PromiseEvent)
        .where(
            PromiseEvent.tenant_id == ctx.tenant_id,
            PromiseEvent.promise_id == promise.id,
            PromiseEvent.event == "promise.confirmed",
        )
        .order_by(PromiseEvent.created_at.desc())
    )
    if (
        event is None
        or event.actor_id != ctx.actor_id
        or event.details.get("commitment") != commitment_snapshot(db, promise)
    ):
        raise HTTPException(409, "PROMISE_CONFIRMATION_SCOPE_UNAVAILABLE")
    member = db.scalar(
        select(Membership).where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id)
    )
    assert member is not None
    connection = db.get(ProviderConnection, promise.provider_connection_id, populate_existing=True)
    if connection is None or connection.status != "READY" or connection.provider not in {"demo", "mock"}:
        raise HTTPException(403, "PROMISE_CHECK_CONNECTION_UNAVAILABLE")
    worker = db.scalar(
        select(Actor).where(Actor.tenant_id == ctx.tenant_id, Actor.purpose == "durable_worker")
    )
    if worker is None:
        worker = Actor(kind="service", tenant_id=ctx.tenant_id, purpose="durable_worker")
        db.add(worker)
        db.flush()
    if not worker.active:
        raise HTTPException(403, "PROMISE_WORKER_DISABLED")
    invoice_ids = sorted(
        db.scalars(
            select(PromiseInvoiceLink.invoice_id).where(
                PromiseInvoiceLink.tenant_id == ctx.tenant_id, PromiseInvoiceLink.promise_id == promise.id
            )
        ),
        key=str,
    )
    result = []
    for item in db.scalars(
        select(PromiseInstallment)
        .where(PromiseInstallment.tenant_id == ctx.tenant_id, PromiseInstallment.promise_id == promise.id)
        .order_by(PromiseInstallment.sequence)
    ):
        command = PromiseCheckCommand(
            promise_id=promise.id,
            installment_id=item.id,
            confirmation_event_id=event.id,
            invoice_ids=invoice_ids,
        )
        for primary in sorted({UUID(str(part["invoice_id"])) for part in item.allocations}, key=str):
            invoice = db.get(InvoiceSnapshot, primary, populate_existing=True)
            if invoice is None:
                raise HTTPException(403, "PROMISE_CHECK_SCOPE_CHANGED")
            key = hashlib.sha256(
                f"promise_check:{ctx.tenant_id}:{event.id}:{item.id}:{primary}".encode()
            ).hexdigest()
            old = db.scalar(
                select(ScheduledAction).where(
                    ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.operation_key == key
                )
            )
            if old is not None:
                if old.command != command.model_dump(mode="json"):
                    raise HTTPException(409, "PROMISE_CHECK_IDENTITY_CONFLICT")
                validate_promise_check(db, old)
                result.append(ScheduledActionRef(action_id=old.id, version=old.version))
                continue
            action = ScheduledAction(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                invoice_id=primary,
                source_revision=invoice.source_revision,
                source_event_id=None,
                policy_version_id=None,
                policy_generation=0,
                initiator_id=ctx.actor_id,
                membership_version=member.version,
                tenant_version=tenant.version,
                outbound_paused=tenant.outbound_paused,
                automation_enabled=tenant.automation_enabled,
                worker_actor_id=worker.id,
                actor_version=worker.version,
                connection_version=connection.version,
                correlation_id=ctx.correlation_id,
                kind="promise_check",
                operation_key=key,
                command=command.model_dump(mode="json"),
                due_at=item.cutoff_at,
                promise_event_id=event.id,
                promise_installment_id=item.id,
            )
            db.add(action)
            db.flush()
            validate_promise_check(db, action)
            ActionService(db)._outbox(action)
            identity.audit(
                ctx,
                "promise.check_queued",
                action.id,
                {"promise_id": str(promise.id), "event_id": str(event.id), "installment_id": str(item.id)},
            )
            result.append(ScheduledActionRef(action_id=action.id, version=action.version))
    return result


def scheduling_status(db: Session, promise: PaymentPromise) -> SchedulingStatus:
    if promise.confirmed_at is None:
        return "PENDING_CONFIRMATION"
    actions = list(
        db.scalars(
            select(ScheduledAction).where(
                ScheduledAction.tenant_id == promise.tenant_id,
                ScheduledAction.kind == "promise_check",
                ScheduledAction.promise_event_id.in_(
                    select(PromiseEvent.id).where(
                        PromiseEvent.tenant_id == promise.tenant_id, PromiseEvent.promise_id == promise.id
                    )
                ),
            )
        )
    )
    if not actions:
        return "PENDING_ENGINE"
    statuses = {action.status for action in actions}
    if "executing" in statuses:
        return "RUNNING"
    if statuses & {"failed", "unknown_outcome", "cancelled", "dead_letter"}:
        return "REVIEW_REQUIRED"
    if statuses == {"succeeded"}:
        return "COMPLETED"
    return "QUEUED"


def cancel_promise_checks(db: Session, ctx: TenantContext, promise: PaymentPromise) -> None:
    for action in db.scalars(
        select(ScheduledAction)
        .where(
            ScheduledAction.tenant_id == ctx.tenant_id,
            ScheduledAction.kind == "promise_check",
            ScheduledAction.status.in_(["queued", "awaiting_approval"]),
            ScheduledAction.promise_event_id.in_(
                select(PromiseEvent.id).where(
                    PromiseEvent.tenant_id == ctx.tenant_id,
                    PromiseEvent.promise_id == promise.id,
                )
            ),
        )
        .order_by(ScheduledAction.id)
        .with_for_update()
    ):
        ActionService(db).cancel_action(ctx, action.id, action.version)
