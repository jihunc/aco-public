"""Server-side source, operational and concurrent contact guards."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from aco.core.clock import Clock
from aco.core.context import ActorContext, ServiceContext
from aco.core.settings import Settings
from aco.core.types import ActionResult
from aco.features.accounting.models import CollectionRecord, InvoiceSnapshot
from aco.features.accounting.reconcile import SourceReadContext
from aco.features.actions.models import ScheduledAction
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies import checks
from aco.features.policies.contact_stop import pending_contact_stops
from aco.features.policies.dto import DispatchTicket, GuardDecision, OutboundProposal
from aco.features.policies.models import ContactReservation
from aco.integrations.ports.accounting import AccountingPort


def normalized_destination(value: str) -> str:
    value = value.strip().casefold()
    if "@" in value:
        if value.count("@") != 1 or any(c.isspace() for c in value) or not all(value.split("@")):
            raise HTTPException(422, "INVALID_DESTINATION")
        local, domain = value.split("@")
        return local + "@" + domain.encode("idna").decode("ascii")
    phone = "".join(c for c in value if c not in " ()-.")
    if not phone.startswith("+") or not phone[1:].isdigit() or not 8 <= len(phone[1:]) <= 15:
        raise HTTPException(422, "E164_DESTINATION_REQUIRED")
    return phone


class PolicyGuards:
    def __init__(
        self,
        db: Session,
        at: datetime,
        incarnation: UUID | None = None,
        *,
        settings: Settings | None = None,
        email_runtime: object = None,
    ):
        self.db, self.at, self.incarnation = db, at, incarnation or PROCESS_INCARNATION
        self.settings, self.email_runtime = settings, email_runtime

    def operational_status(self, ctx: ActorContext, invoice_id: UUID) -> GuardDecision:
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        if isinstance(ctx, ServiceContext):
            scoped = self.db.get(ScheduledAction, ctx.action_id)
            if scoped is None or scoped.invoice_id != invoice_id:
                raise HTTPException(403, "INVOICE_SCOPE_OUTSIDE_ACTION")
        invoice = self.db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if invoice is None or invoice.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "INVOICE_NOT_FOUND")
        return checks.operational_status(self.db, invoice)

    def contact_status(
        self,
        ctx: ActorContext,
        customer_id: UUID,
        destination: str,
        *,
        cap_24h: int = 1,
        cap_7d: int = 3,
        exclude_operation_id: UUID | None = None,
    ) -> GuardDecision:
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        from aco.features.accounting.models import Customer

        if isinstance(ctx, ServiceContext):
            scoped = self.db.get(ScheduledAction, ctx.action_id)
            invoice = self.db.get(InvoiceSnapshot, scoped.invoice_id) if scoped else None
            if invoice is None or invoice.customer_id != customer_id:
                raise HTTPException(403, "CUSTOMER_SCOPE_OUTSIDE_ACTION")
        customer = self.db.get(Customer, customer_id)
        if customer is None or customer.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "CUSTOMER_NOT_FOUND")
        destination = normalized_destination(destination)
        return checks.contact_status(
            self.db,
            ctx.tenant_id,
            customer_id,
            destination,
            self.at,
            cap_24h=cap_24h,
            cap_7d=cap_7d,
            exclude_operation_id=exclude_operation_id,
        )

    def reserve_contact(
        self, ctx: ActorContext, invoice_id: UUID, destination: str, channel: str, operation_id: UUID
    ) -> ContactReservation:
        """Capacity reservation only. This record is never permission to submit outreach."""
        if isinstance(ctx, SourceReadContext):
            raise HTTPException(403, "SOURCE_CONTINUATION_OUTREACH_DENIED")
        IdentityService(self.db).authorize(
            ctx, Permission.INVOICE_READ if isinstance(ctx, ServiceContext) else Permission.TASK_WRITE
        )
        if isinstance(ctx, ServiceContext):
            action = self.db.get(ScheduledAction, ctx.action_id)
            if (
                action is None
                or action.kind not in {"policy_due", "agent_email"}
                or action.status != "executing"
                or action.invoice_id != invoice_id
                or channel != "email"
                or normalized_destination(str(action.command["destination"]))
                != normalized_destination(destination)
            ):
                raise HTTPException(403, "CONTACT_SCOPE_OUTSIDE_ACTION")
        invoice = self.db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if invoice is None or invoice.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "INVOICE_NOT_FOUND")
        destination = normalized_destination(destination)
        existing = self.db.scalar(
            select(ContactReservation)
            .where(
                ContactReservation.tenant_id == ctx.tenant_id, ContactReservation.operation_id == operation_id
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing:
            if (existing.invoice_id, existing.destination, existing.channel) != (
                invoice_id,
                destination,
                channel,
            ):
                raise HTTPException(409, "CONTACT_OPERATION_CONFLICT")
            return existing
        from aco.features.policies.dto import PolicyDefinition
        from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion

        active = self.db.scalar(
            select(CollectionPolicy).where(
                CollectionPolicy.tenant_id == ctx.tenant_id, CollectionPolicy.active.is_(True)
            )
        )
        cap24, cap7 = 1, 3
        if active:
            version = self.db.scalar(
                select(CollectionPolicyVersion)
                .where(CollectionPolicyVersion.policy_id == active.id)
                .order_by(CollectionPolicyVersion.version.desc())
            )
            if version:
                definition = PolicyDefinition.model_validate(version.definition)
                cap24, cap7 = definition.cap_24h, definition.cap_7d
        decision = self.contact_status(ctx, invoice.customer_id, destination, cap_24h=cap24, cap_7d=cap7)
        if not decision.allowed:
            raise HTTPException(409, decision.reason)
        row = ContactReservation(
            tenant_id=ctx.tenant_id,
            invoice_id=invoice.id,
            customer_id=invoice.customer_id,
            destination=destination,
            channel=channel,
            operation_id=operation_id,
            reserved_at=self.at,
            action_id=ctx.action_id if isinstance(ctx, ServiceContext) else None,
            occurrence_id=action.occurrence_id if isinstance(ctx, ServiceContext) and action else None,
            evidence={"origin": "capacity reservation only; no dispatch authorization"},
        )
        self.db.add(row)
        self.db.flush()
        IdentityService(self.db).audit(ctx, "policy.contact_capacity_reserved", row.id)
        return row

    def release_unused_capacity(self, ctx: ActorContext, reservation_id: UUID) -> None:
        IdentityService(self.db).authorize(ctx, Permission.TASK_WRITE)
        row = self.db.scalar(
            select(ContactReservation)
            .where(ContactReservation.tenant_id == ctx.tenant_id, ContactReservation.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, "CONTACT_RESERVATION_NOT_FOUND")
        # T08 unbound capacity cannot invoke a provider. Once bound/inflight/unknown, only T10's
        # authoritative provider reconciliation can establish no effect; caller assertions cannot.
        if row.status != "reserved" or row.action_id is not None or row.submitted_at is not None:
            raise HTTPException(409, "VERIFIED_NO_EFFECT_REQUIRED")
        row.status, row.released_at = "released", self.at
        row.evidence = {**row.evidence, "release": "Unbound unused capacity; no executor authority"}
        row.version += 1
        IdentityService(self.db).audit(ctx, "policy.contact_capacity_released", row.id)

    def source_status(
        self, ctx: ActorContext, invoice_id: UUID, attempt_id: UUID, read_id: UUID | None
    ) -> GuardDecision:
        from aco.features.accounting.models import AccountingObservation
        from aco.features.policies.models import DispatchRead

        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        read = self.db.get(DispatchRead, read_id) if read_id else None
        if (
            read is None
            or read.tenant_id != ctx.tenant_id
            or read.invoice_id != invoice_id
            or read.attempt_id != attempt_id
            or read.incarnation != self.incarnation
            or read.actor_id != ctx.actor_id
        ):
            return denied("source_not_reverified")
        observation = self.db.get(AccountingObservation, read.observation_id) if read.observation_id else None
        if (
            observation is None
            or read.error
            or observation.tenant_id != ctx.tenant_id
            or observation.invoice_id != invoice_id
            or observation.actor_id != ctx.actor_id
        ):
            return denied("STALE_ACCOUNTING_DATA")
        from aco.core.context import ServiceContext

        if isinstance(ctx, ServiceContext) and (
            read.action_id != ctx.action_id
            or read.action_version != ctx.action_version
            or read.lease_token != ctx.lease_token
        ):
            return denied("source_not_reverified")
        invoice = self.db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        stamp = observation.invoice_data.get("stamp", {})
        if (
            invoice is None
            or not isinstance(stamp, dict)
            or stamp.get("revision") != invoice.source_revision
            or observation.provider_connection_id != invoice.provider_connection_id
        ):
            return denied("SOURCE_VERSION_CHANGED")
        for key in ("invoice", "payments"):
            evidence = observation.provenance[key]
            if not isinstance(evidence, dict):
                return denied("STALE_ACCOUNTING_DATA")
            observed = datetime.fromisoformat(str(evidence["observed_at"]).replace("Z", "+00:00"))
            if (
                evidence.get("authenticity") != "VERIFIED"
                or evidence.get("currentness") != "CURRENT"
                or not (timedelta(0) <= self.at - observed <= timedelta(seconds=900))
            ):
                return denied("STALE_ACCOUNTING_DATA")
        return GuardDecision(allowed=True)

    def _bindings(self, ctx: ActorContext, proposal: OutboundProposal) -> dict[str, object]:
        from aco.features.accounting.models import CustomerContact
        from aco.features.actions.models import ScheduledAction
        from aco.features.identity.models import Actor, Membership, Tenant
        from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion, RecipientControl
        from aco.features.review.models import ApprovalRequest

        action = self.db.get(ScheduledAction, proposal.action_id, populate_existing=True)
        if action is None or action.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "ACTION_NOT_FOUND")
        invoice = self.db.get(InvoiceSnapshot, action.invoice_id, populate_existing=True)
        tenant = self.db.get(Tenant, ctx.tenant_id, populate_existing=True)
        actor = self.db.get(Actor, ctx.actor_id, populate_existing=True)
        version = self.db.get(CollectionPolicyVersion, action.policy_version_id)
        policy = self.db.get(CollectionPolicy, version.policy_id, populate_existing=True) if version else None
        member = self.db.scalar(
            select(Membership)
            .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == action.initiator_id)
            .execution_options(populate_existing=True)
        )
        collection = self.db.scalar(
            select(CollectionRecord)
            .where(
                CollectionRecord.tenant_id == ctx.tenant_id, CollectionRecord.invoice_id == action.invoice_id
            )
            .execution_options(populate_existing=True)
        )
        contact = (
            self.db.scalar(
                select(CustomerContact).where(
                    CustomerContact.tenant_id == ctx.tenant_id,
                    CustomerContact.customer_id == invoice.customer_id,
                )
            )
            if invoice
            else None
        )
        control = (
            self.db.scalar(
                select(RecipientControl)
                .where(RecipientControl.tenant_id == ctx.tenant_id, RecipientControl.contact_id == contact.id)
                .execution_options(populate_existing=True)
            )
            if contact
            else None
        )
        approval = (
            self.db.get(ApprovalRequest, proposal.approval_id, populate_existing=True)
            if proposal.approval_id
            else None
        )
        from aco.features.communications.models import EmailDispatchBinding, EmailExecutionActivation
        from aco.features.identity.models import ProviderConnection

        email_binding = self.db.scalar(
            select(EmailDispatchBinding).where(
                EmailDispatchBinding.tenant_id == ctx.tenant_id, EmailDispatchBinding.action_id == action.id
            )
        )
        activation = (
            self.db.get(EmailExecutionActivation, email_binding.activation_id) if email_binding else None
        )
        email_connection = (
            self.db.get(ProviderConnection, email_binding.email_connection_id, populate_existing=True)
            if email_binding
            else None
        )
        return {
            "email_binding": str(email_binding.id) if email_binding else None,
            "email_operation": str(email_binding.operation_id) if email_binding else None,
            "email_payload": email_binding.payload_hash if email_binding else None,
            "email_connection": str(email_connection.id) if email_connection else None,
            "email_connection_version": email_connection.version if email_connection else None,
            "email_activation": str(activation.id) if activation else None,
            "capture_storage": str(email_binding.storage_connection_id) if email_binding else None,
            "capture_original": email_binding.mime_sha256 if email_binding else None,
            "contact_reservation": str(email_binding.contact_reservation_id) if email_binding else None,
            "approval_requested": str(proposal.approval_id) if proposal.approval_id else None,
            "tenant": tenant.version if tenant else None,
            "pause": tenant.outbound_paused if tenant else None,
            "automation": tenant.automation_enabled if tenant else None,
            "actor": actor.version if actor else None,
            "actor_active": actor.active if actor else None,
            "action": action.version,
            "action_status": action.status,
            "lease": str(action.lease_token),
            "source_revision": invoice.source_revision if invoice else None,
            "source_observed": invoice.observed_at.isoformat() if invoice else None,
            "source_balance": invoice.balance_minor if invoice else None,
            "policy_version": str(action.policy_version_id),
            "policy_generation": policy.version if policy else None,
            "policy_active": policy.active if policy else None,
            "membership": member.version if member else None,
            "membership_active": member.active if member else None,
            "holds": collection.hold_reasons if collection else None,
            "hold_version": collection.version if collection else None,
            "customer_contact_stops": pending_contact_stops(self.db, invoice) if invoice else [],
            "lifecycle": collection.lifecycle if collection else None,
            "recipient": control.version if control else None,
            "recipient_source": contact.source_revision if contact else None,
            "recipient_destination": contact.email if contact else None,
            "destination": normalized_destination(proposal.destination),
            "channel": proposal.channel,
            "approval": str(approval.id) if approval else None,
            "approval_version": approval.version if approval else None,
            "approval_status": approval.status if approval else None,
            "approval_consumed": str(approval.consumed_at) if approval else None,
        }

    def evaluate_outbound(
        self, ctx: ActorContext, proposal: OutboundProposal, now: datetime
    ) -> GuardDecision:
        from aco.core.context import ServiceContext
        from aco.features.actions.models import ActionExecution, ScheduledAction
        from aco.features.policies.service import PolicyService

        self.at = now
        if isinstance(ctx, SourceReadContext):
            raise HTTPException(403, "SOURCE_CONTINUATION_OUTREACH_DENIED")
        tenant = IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        action = self.db.get(ScheduledAction, proposal.action_id, populate_existing=True)
        if action is None or action.tenant_id != ctx.tenant_id:
            return denied("ACTION_NOT_FOUND")
        reasons = []
        if not isinstance(ctx, ServiceContext) or ctx.action_id != action.id or action.status != "executing":
            reasons.append("ACTIVE_SERVICE_ATTEMPT_REQUIRED")
        attempt = self.db.get(ActionExecution, proposal.attempt_id)
        if (
            attempt is None
            or attempt.action_id != action.id
            or attempt.lease_token != action.lease_token
            or attempt.status != "executing"
        ):
            reasons.append("DISPATCH_ATTEMPT_MISMATCH")
        reasons.extend(
            self.source_status(ctx, action.invoice_id, proposal.attempt_id, proposal.read_id).reasons
        )
        reasons.extend(self.operational_status(ctx, action.invoice_id).reasons)
        service = PolicyService(self.db, now)
        from aco.features.policies.models import CollectionPolicyVersion

        version = self.db.get(CollectionPolicyVersion, action.policy_version_id)
        if version:
            policy = service.policy(ctx, version.policy_id)
            from aco.features.communications.models import EmailDispatchBinding

            binding = self.db.scalar(
                select(EmailDispatchBinding).where(
                    EmailDispatchBinding.tenant_id == ctx.tenant_id,
                    EmailDispatchBinding.action_id == action.id,
                )
            )
            preview = service.preview_policy(
                ctx,
                service.view(policy, version),
                now,
                exclude_operation_id=binding.operation_id if binding else None,
            )
            matching = next(
                (
                    i
                    for i in preview.items
                    if i.invoice_id == action.invoice_id and str(i.rule_id) == action.command.get("rule_id")
                ),
                None,
            )
            if matching is None:
                reasons.append("POLICY_RULE_MISSING")
            else:
                reasons.extend(
                    r
                    for r in matching.reasons
                    if r not in {"past_occurrence_no_backlog", "OUTBOUND_EXECUTOR_UNAVAILABLE"}
                )
                if (
                    matching.destination is None
                    or normalized_destination(matching.destination)
                    != normalized_destination(proposal.destination)
                    or matching.channel != proposal.channel
                ):
                    reasons.append("RECIPIENT_CHANGED")
                from aco.features.policies.dto import PolicyDefinition
                from aco.features.policies.schedule import recipient_calendar

                definition = PolicyDefinition.model_validate(version.definition)
                calendar = recipient_calendar(
                    now,
                    matching.recipient_timezone,
                    opens_at=definition.opens_at,
                    closes_at=definition.closes_at,
                    weekdays=tuple(definition.weekdays),
                )
                if not calendar.allowed:
                    reasons.append(calendar.reason or "quiet_hours")
        else:
            reasons.append("POLICY_VERSION_MISSING")
        if tenant.outbound_paused:
            reasons.append("TENANT_OUTBOUND_PAUSED")
        if not tenant.automation_enabled:
            reasons.append("AUTOMATION_DISABLED")
        from aco.features.communications.outbound import dispatch_prerequisites

        reasons.extend(dispatch_prerequisites(self.db, ctx, proposal, self.settings, self.email_runtime))
        reasons = list(dict.fromkeys(reasons))
        return GuardDecision(allowed=not reasons, reason=reasons[0] if reasons else None, reasons=reasons)

    def reserve_dispatch(
        self, ctx: ActorContext, decision: GuardDecision, proposal: OutboundProposal
    ) -> DispatchTicket:
        # Caller decisions are explanations, never authority. Re-evaluate stored state under the tenant lock.
        current = self.evaluate_outbound(ctx, proposal, self.at)
        if not current.allowed:
            raise HTTPException(409, current.reason or "OUTBOUND_EXECUTOR_UNAVAILABLE")
        if proposal.approval_id is not None:
            from aco.features.actions.models import ScheduledAction
            from aco.features.review.email_approvals import current_email_approval

            action = self.db.get(ScheduledAction, proposal.action_id)
            if action is not None and action.kind == "agent_email":
                current_email_approval(self.db, action)
        from aco.features.policies.models import DispatchTicketRecord

        row = DispatchTicketRecord(
            tenant_id=ctx.tenant_id,
            action_id=proposal.action_id,
            attempt_id=proposal.attempt_id,
            read_id=proposal.read_id,
            incarnation=self.incarnation,
            issued_at=self.at,
            bindings=self._bindings(ctx, proposal),
        )
        self.db.add(row)
        self.db.flush()
        return DispatchTicket(
            id=row.id, issued_at=row.issued_at, action_id=row.action_id, attempt_id=row.attempt_id
        )

    def consume_dispatch(self, ctx: ActorContext, ticket_id: UUID, proposal: OutboundProposal) -> None:
        from aco.core.context import ServiceContext
        from aco.features.actions.models import ScheduledAction
        from aco.features.policies.models import DispatchTicketRecord

        if isinstance(ctx, SourceReadContext):
            raise HTTPException(403, "SOURCE_CONTINUATION_OUTREACH_DENIED")
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        ticket = self.db.scalar(
            select(DispatchTicketRecord)
            .where(DispatchTicketRecord.id == ticket_id, DispatchTicketRecord.tenant_id == ctx.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if ticket is None:
            raise HTTPException(409, "TICKET_NOT_FOUND")
        if ticket.consumed_at is not None:
            raise HTTPException(409, "TICKET_ALREADY_CONSUMED")
        if not timedelta(0) <= self.at - ticket.issued_at < timedelta(seconds=60):
            raise HTTPException(409, "TICKET_EXPIRED")
        if ticket.incarnation != self.incarnation:
            raise HTTPException(409, "TICKET_WORKER_RESTARTED")
        if (ticket.action_id, ticket.attempt_id, ticket.read_id) != (
            proposal.action_id,
            proposal.attempt_id,
            proposal.read_id,
        ):
            raise HTTPException(409, "TICKET_ATTEMPT_MISMATCH")
        if ticket.bindings != self._bindings(ctx, proposal):
            raise HTTPException(409, "TICKET_VERSION_CHANGED")
        if not isinstance(ctx, ServiceContext) or ctx.action_id != proposal.action_id:
            raise HTTPException(403, "SERVICE_DISPATCH_REQUIRED")
        action = self.db.get(ScheduledAction, proposal.action_id)
        assert action
        source = self.source_status(ctx, action.invoice_id, proposal.attempt_id, proposal.read_id)
        if not source.allowed:
            raise HTTPException(409, source.reason)
        current = self.evaluate_outbound(ctx, proposal, self.at)
        if not current.allowed:
            raise HTTPException(409, current.reason)
        if action.kind == "agent_email":
            from aco.features.review.email_approvals import current_email_approval

            row = current_email_approval(self.db, action)
            if row.id != proposal.approval_id:
                raise HTTPException(403, "EMAIL_APPROVAL_ACTION_MISMATCH")
            row.consumed_at = self.at
            row.version += 1
            IdentityService(self.db).audit(ctx, "approval.consumed_for_email", row.id)
        ticket.consumed_at = self.at
        self.db.flush()
        # Consumption is one atomic transition; retain the exact post-consumption
        # binding for the existing final pre-SDK equality check.
        ticket.bindings = self._bindings(ctx, proposal)
        self.db.flush()


def denied(reason: str) -> GuardDecision:
    return GuardDecision(allowed=False, reason=reason, reasons=[reason])


PROCESS_INCARNATION = uuid4()


async def read_dispatch_source(
    db: AsyncSession,
    ctx: ActorContext,
    invoice_id: UUID,
    attempt_id: UUID,
    incarnation: UUID,
    adapter: AccountingPort,
    *,
    clock: Clock | None = None,
) -> UUID:
    from aco.core.clock import SystemClock
    from aco.core.context import ServiceContext
    from aco.features.accounting.ingest import AccountingService
    from aco.features.actions.models import ScheduledAction
    from aco.features.policies.models import DispatchRead
    from aco.integrations.ports.accounting import AccountingFailure

    clock = clock or SystemClock()
    await db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.DEMO_PAYMENT))
    invoice = await db.get(InvoiceSnapshot, invoice_id)
    if (
        invoice is None
        or invoice.tenant_id != ctx.tenant_id
        or invoice.provider_connection_id != adapter.connection_id
    ):
        raise HTTPException(404, "INVOICE_NOT_FOUND")
    action = await db.get(ScheduledAction, ctx.action_id) if isinstance(ctx, ServiceContext) else None
    if action is not None and action.invoice_id != invoice_id:
        raise HTTPException(403, "SOURCE_ATTEMPT_OUTSIDE_ACTION")
    service = AccountingService(db, adapter, clock)
    started = clock.now()
    error = None
    try:
        await service.recheck_invoice(ctx, invoice.external_invoice_id)
    except AccountingFailure:
        error = "STALE_ACCOUNTING_DATA"
    read = DispatchRead(
        tenant_id=ctx.tenant_id,
        invoice_id=invoice_id,
        actor_id=ctx.actor_id,
        action_id=action.id if action else None,
        action_version=action.version if action else None,
        lease_token=action.lease_token if action else None,
        attempt_id=attempt_id,
        incarnation=incarnation,
        observation_id=service.last_observation_id,
        error=error,
        started_at=started,
    )
    db.add(read)
    await db.flush()
    return read.id


async def execute_policy_due(db: AsyncSession, ctx: ServiceContext, action: ScheduledAction) -> ActionResult:
    """Real bounded source/eligibility work. It never submits an outbound operation."""
    from aco.core.types import ActionResult, ActionStatus
    from aco.features.actions.models import ActionExecution
    from aco.features.identity.models import now
    from aco.features.policies.dto import OutboundProposal
    from aco.features.policies.models import PolicyOccurrence
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

    attempt = await db.scalar(
        select(ActionExecution).where(
            ActionExecution.action_id == action.id, ActionExecution.lease_token == action.lease_token
        )
    )
    assert attempt is not None
    reasons = []
    try:
        read_id = await read_dispatch_source(
            db,
            ctx,
            action.invoice_id,
            attempt.id,
            PROCESS_INCARNATION,
            DemoAccountingAdapter(db, action.provider_connection_id),
        )
        proposal = OutboundProposal(
            action_id=action.id,
            attempt_id=attempt.id,
            read_id=read_id,
            channel="voice" if action.command["channel"] == "voice" else "email",
            destination=str(action.command["destination"]),
        )
        decision = await db.run_sync(
            lambda store: PolicyGuards(store, now()).evaluate_outbound(ctx, proposal, now())
        )
        reasons = decision.reasons
    except HTTPException as exc:
        # The handler knows it has performed source reads only. A rejection is verified no outreach.
        reasons = [str(exc.detail), "OUTBOUND_EXECUTOR_UNAVAILABLE"]
    occurrence = await db.get(PolicyOccurrence, action.occurrence_id)
    assert occurrence
    occurrence.state = "pending_recompute"
    occurrence.last_reason = ",".join(reasons)
    # Recompute uses recipient calendar/current policy and selects one current action; this is no retry job.
    occurrence.reconsider_after = now() + timedelta(days=1)
    await db.run_sync(
        lambda store: IdentityService(store).audit(
            ctx,
            "policy.eligibility_checked",
            action.id,
            {"eligibility_only": True, "outreach_submitted": False, "reasons": ",".join(reasons)},
        )
    )
    return ActionResult(
        action_id=action.id,
        status=ActionStatus.FAILED,
        error_code="OUTBOUND_EXECUTOR_UNAVAILABLE",
        changed_ids=(occurrence.id,),
    )
