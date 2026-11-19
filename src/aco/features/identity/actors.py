"""Narrow durable-worker authority, resolved from persisted actions, never copied roles."""

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, ServiceContext, TenantContext
from aco.core.types import Role
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant, now

if TYPE_CHECKING:
    from aco.features.actions.models import ScheduledAction


def require_job_binding(db: Session, action: "ScheduledAction") -> None:
    """A stored ID alone does not prove which operation the library job owns."""
    job = (
        db.execute(
            text("SELECT task_name, args FROM procrastinate_jobs WHERE id=:id"), {"id": action.job_id}
        ).first()
        if action.job_id is not None
        else None
    )
    if (
        job is None
        or job.task_name != "aco.execute"
        or job.args != {"action_id": str(action.id), "expected_version": action.version}
    ):
        raise HTTPException(409, "INVALID_ACTION_JOB_BINDING")


def authorize_service(db: Session, ctx: ServiceContext, permission: str) -> Tenant:
    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.accounting.reconcile import SourceReadContext, validate_source_read
    from aco.features.actions.models import ActionExecution, ScheduledAction
    from aco.features.identity.service import IdentityService, Permission
    from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion

    if isinstance(ctx, SourceReadContext) and permission != "invoice_read":
        raise HTTPException(403, "SOURCE_CONTINUATION_PERMISSION_DENIED")
    if permission not in {"invoice_read", "demo_payment", "task_write", "investigate"}:
        raise HTTPException(403, "SERVICE_PERMISSION_DENIED")
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == ctx.tenant_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    action = db.scalar(
        select(ScheduledAction)
        .where(ScheduledAction.id == ctx.action_id, ScheduledAction.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, ctx.actor_id, populate_existing=True)
    if (
        tenant is None
        or action is None
        or actor is None
        or actor.kind != "service"
        or actor.tenant_id != ctx.tenant_id
        or actor.purpose != "durable_worker"
        or not actor.active
        or action.worker_actor_id != actor.id
        or action.actor_version != actor.version
        or action.version != ctx.action_version
        or action.job_id != ctx.job_id
        or (
            action.kind != "promise_check"
            and (
                tenant.version != action.tenant_version
                or tenant.outbound_paused != action.outbound_paused
                or tenant.automation_enabled != action.automation_enabled
            )
        )
        or action.status not in {"queued", "executing"}
        or (ctx.lease_token is not None and ctx.lease_token != action.lease_token)
        or (
            action.status == "executing"
            and (ctx.lease_token is None or action.lease_until is None or action.lease_until <= now())
        )
    ):
        raise HTTPException(403, "STALE_SERVICE_AUTHORITY")
    allowed = {
        "accounting_recheck": {"invoice_read", "demo_payment"},
        "promise_check": {"invoice_read", "demo_payment"},
        "policy_due": {"invoice_read", "demo_payment"},
        "agent_email": {"invoice_read", "demo_payment"},
        "internal_task": {"invoice_read", "task_write"},
        "approval_request": {"invoice_read", "task_write", "investigate"},
    }
    if permission not in allowed.get(action.kind, set()):
        raise HTTPException(403, "ACTION_PERMISSION_DENIED")
    if permission != "invoice_read" and action.status != "executing":
        raise HTTPException(403, "ACTION_NOT_EXECUTING")
    if action.status == "executing":
        require_job_binding(db, action)
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == action.initiator_id)
        .execution_options(populate_existing=True)
    )
    if member is None or not member.active or member.version != action.membership_version:
        raise HTTPException(403, "INITIATOR_REVOKED")
    human = None
    if action.kind not in {"policy_due", "promise_check", "agent_email"}:
        human = TenantContext(
            tenant_id=ctx.tenant_id,
            actor_id=member.user_id,
            role=Role(member.role),
            correlation_id=ctx.correlation_id,
        )
        IdentityService(db).authorize(
            human, Permission.DEMO_PAYMENT if action.kind == "accounting_recheck" else Permission.TASK_WRITE
        )
    connection = db.get(ProviderConnection, action.provider_connection_id, populate_existing=True)
    invoice = db.get(InvoiceSnapshot, action.invoice_id, populate_existing=True)
    version = db.get(CollectionPolicyVersion, action.policy_version_id) if action.policy_version_id else None
    policy = db.get(CollectionPolicy, version.policy_id, populate_existing=True) if version else None
    if (
        connection is None
        or connection.tenant_id != ctx.tenant_id
        or connection.status != "READY"
        or connection.version != action.connection_version
        or connection.provider not in {"demo", "mock"}
        or invoice is None
        or invoice.tenant_id != ctx.tenant_id
        or invoice.provider_connection_id != connection.id
        or (
            action.kind != "promise_check"
            and not isinstance(ctx, SourceReadContext)
            and invoice.source_revision != action.source_revision
        )
        or (
            action.kind != "promise_check"
            and (
                policy is None
                or policy.tenant_id != ctx.tenant_id
                or policy.version != action.policy_generation
            )
        )
    ):
        raise HTTPException(403, "STALE_ACTION_SCOPE")
    if action.kind == "promise_check":
        from aco.features.promises.scheduling import validate_promise_check

        validate_promise_check(db, action)
        if action.status == "executing":
            attempt = db.scalar(select(ActionExecution).where(ActionExecution.lease_token == ctx.lease_token))
            job = db.execute(
                text(
                    "SELECT j.status::text, w.last_heartbeat FROM procrastinate_jobs j "
                    "LEFT JOIN procrastinate_workers w ON w.id=j.worker_id WHERE j.id=:id"
                ),
                {"id": ctx.job_id},
            ).first()
            if (
                attempt is None
                or attempt.tenant_id != ctx.tenant_id
                or attempt.action_id != action.id
                or attempt.attempt != action.attempts
                or attempt.status != "executing"
                or attempt.finished_at is not None
                or job is None
                or job.status != "doing"
                or job.last_heartbeat is None
                or job.last_heartbeat < now() - timedelta(seconds=90)
            ):
                raise HTTPException(403, "PROMISE_EXECUTION_NOT_CURRENT")
    if isinstance(ctx, SourceReadContext):
        validate_source_read(db, ctx, action, invoice)
    if action.kind == "agent_email" and not isinstance(ctx, SourceReadContext):
        from aco.features.review.email_approvals import validate_email_worker

        validate_email_worker(db, action, ctx.lease_token)
    if action.kind == "policy_due":
        from aco.features.policies.models import PolicyActivation
        from aco.features.policies.service import validate_activation

        activation = db.get(PolicyActivation, action.policy_activation_id)
        if (
            activation is None
            or version is None
            or policy is None
            or (
                activation.enabling_human_id != action.initiator_id
                or activation.event_id != action.policy_event_id
                or activation.worker_actor_id != action.worker_actor_id
            )
        ):
            raise HTTPException(403, "INVALID_POLICY_ACTION_AUTHORITY")
        validate_activation(db, tenant, policy, version, activation)
        if action.reservation_id is not None:
            raise HTTPException(403, "POLICY_SPEND_AUTHORITY_UNAVAILABLE")
    if action.reservation_id is not None:
        from aco.features.usage.budgets import BudgetService
        from aco.features.usage.dto import UsageReservationRequest
        from aco.features.usage.models import BudgetReservation

        reservation = db.get(BudgetReservation, action.reservation_id, populate_existing=True)
        if (
            reservation is None
            or reservation.status != "RESERVED"
            or reservation.tenant_id != ctx.tenant_id
            or reservation.provider_connection_id != connection.id
            or reservation.operation_key != action.operation_key
        ):
            raise HTTPException(403, "STALE_RESERVATION")
        # Revalidate the actual initiating human's existing synthetic reservation.
        # This cannot create a reservation, mint worker SETTINGS or broaden a cap.
        budgets = BudgetService(db)
        assert human is not None
        authority = budgets.synthetic_authority(human, connection.id, "local")
        budgets.reserve_budget(
            human,
            UsageReservationRequest.model_validate_json(json.dumps(reservation.request)),
            authority=authority,
        )
    return tenant


def accounting_scope(
    db: Session,
    ctx: ActorContext,
    connection_id: UUID,
    invoice_key: str | None = None,
    *,
    customer_read: bool = False,
) -> tuple[str, str] | None:
    """Validate the requested resource before any independent intent/provider call."""
    if not isinstance(ctx, ServiceContext):
        return None
    authorize_service(db, ctx, "invoice_read")
    from aco.features.accounting.models import Customer, InvoiceSnapshot
    from aco.features.actions.models import ScheduledAction

    action = db.get(ScheduledAction, ctx.action_id, populate_existing=True)
    assert action is not None
    invoice = db.get(InvoiceSnapshot, action.invoice_id, populate_existing=True)
    customer = db.get(Customer, invoice.customer_id, populate_existing=True) if invoice else None
    if (
        action.kind not in {"accounting_recheck", "policy_due", "promise_check", "agent_email"}
        or action.status != "executing"
        or ctx.job_id is None
        or action.provider_connection_id != connection_id
        or invoice is None
        or customer is None
        or (not customer_read and invoice_key != invoice.external_invoice_id)
    ):
        raise HTTPException(403, "ACCOUNTING_SCOPE_OUTSIDE_ACTION")
    return invoice.external_invoice_id, customer.external_customer_id
