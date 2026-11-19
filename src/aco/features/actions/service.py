"""Transactional domain commands, fenced execution and conservative recovery."""

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, ServiceContext, TenantContext
from aco.core.types import ActionResult, ActionStatus, ProviderFailure, StrictModel
from aco.features.accounting.models import AccountingEvent, InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ActionSource, OutboxEvent, ScheduledAction
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion


class ScheduledCommand(StrictModel):
    kind: Literal["accounting_recheck", "internal_task", "approval_request"]
    provider_connection_id: UUID
    invoice_id: UUID
    policy_version_id: UUID
    source_event_id: UUID
    due_at: AwareDatetime
    payload: dict[str, object] = Field(default_factory=dict)
    reservation_id: UUID | None = None


class ScheduledActionRef(StrictModel):
    action_id: UUID
    version: int


class ActionLease(StrictModel):
    action_id: UUID
    version: int
    token: UUID
    attempt: int


class ReconciliationState(StrictModel):
    action_id: UUID
    status: str
    automatic_resend: bool
    operation_key: str


class ActionFailure(Exception):
    def __init__(self, failure: ProviderFailure):
        self.failure = failure
        super().__init__(failure.code.value)


def worker_context(action: ScheduledAction, token: UUID | None = None) -> ServiceContext:
    return ServiceContext(
        tenant_id=action.tenant_id,
        actor_id=action.worker_actor_id,
        correlation_id=action.correlation_id,
        action_id=action.id,
        action_version=action.version,
        job_id=action.job_id,
        lease_token=token,
    )


class ActionService:
    def __init__(self, db: Session, at: datetime | None = None):
        self.db, self.at = db, at or now()

    def enqueue_action(self, ctx: ActorContext, command: ScheduledCommand) -> ScheduledActionRef:
        # Worker follow-ups require a separately validated owning feature. T07 never
        # promotes a service action into unbounded permission to mint more grants.
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_INITIATION_REQUIRED")
        identity = IdentityService(self.db)
        permission = (
            Permission.DEMO_PAYMENT if command.kind == "accounting_recheck" else Permission.TASK_WRITE
        )
        tenant = identity.authorize(ctx, permission)
        invoice = self.db.get(InvoiceSnapshot, command.invoice_id, populate_existing=True)
        connection = self.db.get(ProviderConnection, command.provider_connection_id, populate_existing=True)
        source = self.db.get(AccountingEvent, command.source_event_id)
        version = self.db.get(CollectionPolicyVersion, command.policy_version_id)
        policy = self.db.get(CollectionPolicy, version.policy_id, populate_existing=True) if version else None
        if (
            invoice is None
            or connection is None
            or source is None
            or policy is None
            or invoice.tenant_id != ctx.tenant_id
            or connection.tenant_id != ctx.tenant_id
            or source.tenant_id != ctx.tenant_id
            or policy.tenant_id != ctx.tenant_id
            or invoice.provider_connection_id != connection.id
            or source.provider_connection_id != connection.id
            or source.invoice_id != invoice.id
            or source.source_revision != invoice.source_revision
            or connection.status != "READY"
            or connection.provider not in {"demo", "mock"}
        ):
            raise HTTPException(403, "INVALID_ACTION_SCOPE")
        if command.kind == "accounting_recheck" and command.payload:
            raise HTTPException(422, "INVALID_COMMAND")
        if command.kind != "accounting_recheck":
            from aco.features.review.internal_work import ApprovalProposal, InternalTaskCommand

            model = InternalTaskCommand if command.kind == "internal_task" else ApprovalProposal
            model.model_validate_json(json.dumps(command.payload))
        if command.reservation_id is not None:
            from aco.features.usage.models import BudgetReservation

            reservation = self.db.get(BudgetReservation, command.reservation_id, populate_existing=True)
            if (
                reservation is None
                or reservation.tenant_id != ctx.tenant_id
                or reservation.provider_connection_id != connection.id
                or reservation.status != "RESERVED"
            ):
                raise HTTPException(403, "INVALID_RESERVATION")
        member = self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
            )
        )
        assert member is not None
        key = hashlib.sha256(
            json.dumps(
                [
                    str(ctx.tenant_id),
                    str(connection.id),
                    str(invoice.id),
                    invoice.source_revision,
                    command.kind,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if command.reservation_id is not None:
            assert reservation is not None
            if reservation.operation_key != key:
                raise HTTPException(403, "RESERVATION_OPERATION_MISMATCH")
        existing = self.db.scalar(
            select(ScheduledAction).where(
                ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.operation_key == key
            )
        )
        material = command.model_dump(mode="json", exclude={"due_at", "source_event_id"})
        if existing:
            if existing.command != material:
                raise HTTPException(409, "OPERATION_CONFLICT")
            self._source(existing, source.id)
            return ScheduledActionRef(action_id=existing.id, version=existing.version)
        actor = self.db.scalar(
            select(Actor).where(Actor.tenant_id == ctx.tenant_id, Actor.purpose == "durable_worker")
        )
        if actor is None:
            actor = Actor(kind="service", tenant_id=ctx.tenant_id, purpose="durable_worker")
            self.db.add(actor)
            self.db.flush()
        action = ScheduledAction(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection.id,
            invoice_id=invoice.id,
            source_revision=invoice.source_revision,
            source_event_id=source.id,
            policy_version_id=command.policy_version_id,
            policy_generation=policy.version,
            initiator_id=ctx.actor_id,
            membership_version=member.version,
            tenant_version=tenant.version,
            outbound_paused=tenant.outbound_paused,
            automation_enabled=tenant.automation_enabled,
            worker_actor_id=actor.id,
            actor_version=actor.version,
            connection_version=connection.version,
            correlation_id=ctx.correlation_id,
            kind=command.kind,
            operation_key=key,
            command=material,
            due_at=command.due_at,
            reservation_id=command.reservation_id,
        )
        self.db.add(action)
        self.db.flush()
        self._source(action, source.id)
        self._outbox(action)
        identity.audit(ctx, "action.queued", action.id)
        return ScheduledActionRef(action_id=action.id, version=action.version)

    def _source(self, action: ScheduledAction, source_id: UUID) -> None:
        if (
            self.db.scalar(
                select(ActionSource).where(
                    ActionSource.action_id == action.id, ActionSource.source_event_id == source_id
                )
            )
            is None
        ):
            self.db.add(
                ActionSource(tenant_id=action.tenant_id, action_id=action.id, source_event_id=source_id)
            )

    def _outbox(self, action: ScheduledAction) -> None:
        self.db.add(
            OutboxEvent(
                tenant_id=action.tenant_id,
                action_id=action.id,
                action_version=action.version,
                due_at=action.due_at,
            )
        )

    def locked(self, ctx: ActorContext, action_id: UUID) -> ScheduledAction:
        from aco.features.accounting.reconcile import SourceReadContext

        if isinstance(ctx, SourceReadContext):
            raise HTTPException(403, "SOURCE_CONTINUATION_ACTION_MUTATION_DENIED")
        self.db.scalar(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update(key_share=True))
        action = self.db.scalar(
            select(ScheduledAction)
            .where(ScheduledAction.id == action_id, ScheduledAction.tenant_id == ctx.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if action is None:
            raise HTTPException(404, "ACTION_NOT_FOUND")
        return action

    def claim_action(self, ctx: ServiceContext, action_id: UUID, expected_version: int) -> ActionLease:
        from aco.features.identity.actors import require_job_binding

        if ctx.action_id != action_id or ctx.action_version != expected_version:
            raise HTTPException(403, "ACTION_CONTEXT_MISMATCH")
        action = self.locked(ctx, action_id)
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        if action.status != "queued" or action.version != expected_version or action.due_at > self.at:
            raise HTTPException(409, "ACTION_NOT_CLAIMABLE")
        require_job_binding(self.db, action)
        action.lease_token = uuid4()
        action.lease_until = self.at + timedelta(seconds=30)
        action.status = "executing"
        action.attempts += 1
        self.db.add(
            ActionExecution(
                tenant_id=ctx.tenant_id,
                action_id=action.id,
                attempt=action.attempts,
                lease_token=action.lease_token,
                started_at=self.at,
            )
        )
        IdentityService(self.db).audit(ctx, "action.claimed", action.id)
        return ActionLease(
            action_id=action.id, version=action.version, token=action.lease_token, attempt=action.attempts
        )

    def complete_action(self, ctx: ServiceContext, lease: ActionLease, result: ActionResult) -> None:
        action = self.locked(ctx, lease.action_id)
        if (
            ctx.action_id != action.id
            or ctx.actor_id != action.worker_actor_id
            or ctx.action_version != lease.version
            or ctx.job_id != action.job_id
            or result.action_id != action.id
            or action.version != lease.version
            or action.lease_token != lease.token
            or action.status != "executing"
        ):
            raise HTTPException(409, "STALE_EXECUTION_LEASE")
        execution = self.db.scalar(select(ActionExecution).where(ActionExecution.lease_token == lease.token))
        assert execution is not None
        if result.provider_ref is not None:
            result_connection_id = action.provider_connection_id
            if action.kind in {"policy_due", "agent_email"}:
                from aco.features.communications.models import EmailDispatchBinding, EmailSubmission

                binding = self.db.scalar(
                    select(EmailDispatchBinding).where(
                        EmailDispatchBinding.tenant_id == action.tenant_id,
                        EmailDispatchBinding.action_id == action.id,
                    )
                )
                submission = (
                    self.db.scalar(
                        select(EmailSubmission).where(
                            EmailSubmission.tenant_id == action.tenant_id,
                            EmailSubmission.binding_id == binding.id,
                        )
                    )
                    if binding
                    else None
                )
                if (
                    binding is None
                    or binding.operation_key != action.operation_key
                    or submission is None
                    or submission.state != "accepted"
                    or submission.provider_message_id != result.provider_ref.external_id
                ):
                    raise HTTPException(409, "UNTRUSTED_PROVIDER_RESULT")
                result_connection_id = binding.email_connection_id
            connection = self.db.get(ProviderConnection, result_connection_id, populate_existing=True)
            if (
                connection is None
                or result.provider_ref.connection_id != connection.id
                or result.provider_ref.provider != connection.provider
            ):
                # The effect may already exist. Reject untrusted linkage without
                # converting an accepted but unidentifiable effect into a retry.
                raise HTTPException(409, "UNTRUSTED_PROVIDER_RESULT")
        if result.status == ActionStatus.UNKNOWN_OUTCOME and action.kind in {"policy_due", "agent_email"}:
            from aco.features.communications.uncertainty import project_email_uncertainty

            project_email_uncertainty(self.db, action)
        action.status = result.status.value
        action.result = result.model_dump(mode="json")
        action.last_error = result.error_code
        action.automatic_resend = False
        execution.status, execution.finished_at = action.status, self.at
        execution.result = action.result
        execution.provider_operation_id = result.provider_ref.external_id if result.provider_ref else None
        IdentityService(self.db).audit(ctx, "action." + action.status, action.id)
        if action.kind == "agent_email":
            from aco.features.tools.email import wake_email_run

            wake_email_run(self.db, action)

    def fail_action(self, ctx: ServiceContext, lease: ActionLease, failure: ProviderFailure) -> None:
        action = self.locked(ctx, lease.action_id)
        unknown = failure.may_have_side_effect
        retry = not unknown and failure.retryable and action.attempts < 3
        status = (
            ActionStatus.UNKNOWN_OUTCOME
            if unknown
            else ActionStatus.QUEUED
            if retry
            else (ActionStatus.DEAD_LETTER if failure.retryable else ActionStatus.FAILED)
        )
        self.complete_action(
            ctx, lease, ActionResult(action_id=action.id, status=status, error_code=failure.code.value)
        )
        execution = self.db.scalar(select(ActionExecution).where(ActionExecution.lease_token == lease.token))
        assert execution
        execution.error = failure.model_dump(mode="json")
        if retry:
            delay = max(
                (60, 300)[action.attempts - 1],
                math.ceil((failure.retry_after or timedelta()).total_seconds()),
            )
            execution.retry_delay_seconds = delay
            action.due_at = self.at + timedelta(seconds=delay)
            action.version += 1
            action.lease_token = None
            action.lease_until = None
            action.job_id = None
            action.automatic_resend = True
            self._outbox(action)

    def cancel_action(self, ctx: TenantContext, action_id: UUID, expected_version: int) -> None:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        IdentityService(self.db).authorize(ctx, Permission.TASK_WRITE)
        action = self.locked(ctx, action_id)
        if action.version != expected_version or action.status not in {"queued", "awaiting_approval"}:
            raise HTTPException(409, "ACTION_NOT_CANCELLABLE")
        action.status = "cancelled"
        action.version += 1
        action.automatic_resend = False
        IdentityService(self.db).audit(ctx, "action.cancelled", action.id)

    def reconcile_unknown(self, ctx: TenantContext, action_id: UUID) -> ReconciliationState:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        action = self.locked(ctx, action_id)
        if action.status != "unknown_outcome":
            raise HTTPException(409, "NOT_UNKNOWN")
        # No adapter currently proves confirmed non-submission. Vendor key expiry is
        # not such proof. Keep identity/exposure and expose the unresolved operation.
        return ReconciliationState(
            action_id=action.id,
            status=action.status,
            automatic_resend=False,
            operation_key=action.operation_key,
        )
