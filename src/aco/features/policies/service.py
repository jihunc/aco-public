"""Versioned configuration and narrow, persisted policy scheduling authority."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, ServiceContext, TenantContext
from aco.core.types import Role
from aco.features.accounting.models import CollectionRecord, CustomerContact, InvoiceSnapshot
from aco.features.accounting.reconcile import SourceReadContext
from aco.features.actions.models import ActionExecution, OutboxEvent, ScheduledAction
from aco.features.identity.models import Actor, AuditEvent, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import ROLE_PERMISSIONS, IdentityService, Permission
from aco.features.policies.dto import (
    NextAction,
    PauseState,
    PolicyArchive,
    PolicyArchiveResult,
    PolicyDefinition,
    PolicyPreview,
    PolicySave,
    PolicyVersion,
    PreviewItem,
    RecipientSave,
    RecipientView,
)
from aco.features.policies.models import (
    CollectionHold,
    CollectionPolicy,
    CollectionPolicyVersion,
    ContactReservation,
    PolicyActivation,
    PolicyEvent,
    PolicyOccurrence,
    RecipientControl,
)
from aco.features.policies.schedule import recipient_calendar


@dataclass(frozen=True)
class _SchedulingProof:
    """Internal only; not an ActorContext, request model or transferable permission."""

    tenant: Tenant
    policy: CollectionPolicy
    version: CollectionPolicyVersion
    activation: PolicyActivation


class PolicyService:
    def __init__(self, db: Session, at: datetime | None = None):
        self.db, self.at = db, at or now()
        self.identity = IdentityService(db)

    def policy(self, ctx: ActorContext, key: UUID) -> CollectionPolicy:
        self.identity.authorize(ctx, Permission.INVOICE_READ)
        if isinstance(ctx, ServiceContext):
            action = self.db.get(ScheduledAction, ctx.action_id)
            if isinstance(ctx, SourceReadContext) and action is not None and action.kind == "promise_check":
                # Promise authority permits source verification only. A separate current
                # reviewed policy activation is required before reconsidering its invoice.
                proof = self._proof(ctx)
                if proof is None or proof.policy.id != key:
                    raise HTTPException(403, "POLICY_SCOPE_OUTSIDE_ACTION")
            else:
                current = (
                    self.db.get(CollectionPolicyVersion, action.policy_version_id)
                    if action and action.policy_version_id
                    else None
                )
                if current is None or current.policy_id != key:
                    raise HTTPException(403, "POLICY_SCOPE_OUTSIDE_ACTION")
        row = self.db.get(CollectionPolicy, key, populate_existing=True)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "POLICY_NOT_FOUND")
        return row

    def latest(self, policy: CollectionPolicy) -> CollectionPolicyVersion:
        row = self.db.scalar(
            select(CollectionPolicyVersion)
            .where(
                CollectionPolicyVersion.tenant_id == policy.tenant_id,
                CollectionPolicyVersion.policy_id == policy.id,
            )
            .order_by(CollectionPolicyVersion.version.desc())
        )
        if row is None:
            raise HTTPException(409, "POLICY_VERSION_MISSING")
        return row

    def view(self, policy: CollectionPolicy, version: CollectionPolicyVersion) -> PolicyVersion:
        saved_audit = self.db.scalar(
            select(AuditEvent)
            .join(PolicyEvent, PolicyEvent.audit_event_id == AuditEvent.id)
            .where(
                PolicyEvent.tenant_id == policy.tenant_id,
                PolicyEvent.policy_version_id == version.id,
                PolicyEvent.kind.in_(["saved", "legacy_name_preserved"]),
            )
        )
        name = saved_audit.details.get("policy_name") if saved_audit else None
        return PolicyVersion(
            id=version.id,
            policy_id=policy.id,
            name=name if isinstance(name, str) else policy.name,
            version=version.version,
            definition=version.definition,
            active=policy.active,
            archived=policy.archived_at is not None,
            effective_at=version.created_at,
            state=version.state,
            synthetic=version.synthetic,
        )

    def list_policies(self, ctx: ActorContext) -> list[PolicyVersion]:
        if isinstance(ctx, ServiceContext):
            raise HTTPException(403, "TENANT_WIDE_POLICY_SCOPE_FORBIDDEN")
        self.identity.authorize(ctx, Permission.INVOICE_READ)
        return [
            self.view(p, self.latest(p))
            for p in self.db.scalars(
                select(CollectionPolicy)
                .where(CollectionPolicy.tenant_id == ctx.tenant_id)
                .order_by(CollectionPolicy.created_at)
            )
        ]

    def history(self, ctx: ActorContext, policy_id: UUID) -> list[PolicyVersion]:
        if isinstance(ctx, ServiceContext):
            raise HTTPException(403, "TENANT_WIDE_POLICY_SCOPE_FORBIDDEN")
        policy = self.policy(ctx, policy_id)
        return [
            self.view(policy, v)
            for v in self.db.scalars(
                select(CollectionPolicyVersion)
                .where(CollectionPolicyVersion.policy_id == policy.id)
                .order_by(CollectionPolicyVersion.version)
            )
        ]

    def _event(self, ctx: ActorContext, version: CollectionPolicyVersion, kind: str) -> PolicyEvent:
        policy = self.db.get(CollectionPolicy, version.policy_id)
        assert policy
        audit = self.identity.audit(ctx, "policy." + kind, version.id, {"policy_name": policy.name})
        event = PolicyEvent(
            tenant_id=ctx.tenant_id, policy_version_id=version.id, audit_event_id=audit.id, kind=kind
        )
        self.db.add(event)
        self.db.flush()
        return event

    def _activation(
        self,
        ctx: ActorContext,
        tenant: Tenant,
        version: CollectionPolicyVersion,
        event: PolicyEvent,
        enabling_human: UUID,
    ) -> PolicyActivation:
        member = self.db.scalar(
            select(Membership)
            .where(Membership.tenant_id == tenant.id, Membership.user_id == enabling_human)
            .execution_options(populate_existing=True)
        )
        if (
            member is None
            or not member.active
            or Permission.SETTINGS not in ROLE_PERMISSIONS[Role(member.role)]
        ):
            raise HTTPException(403, "POLICY_ENABLER_REVOKED")
        actor = self.db.scalar(
            select(Actor).where(Actor.tenant_id == tenant.id, Actor.purpose == "durable_worker")
        )
        if actor is None:
            actor = Actor(kind="service", tenant_id=tenant.id, purpose="durable_worker")
            self.db.add(actor)
            self.db.flush()
        if not actor.active:
            raise HTTPException(409, "POLICY_WORKER_DISABLED")
        activation = PolicyActivation(
            tenant_id=tenant.id,
            policy_version_id=version.id,
            event_id=event.id,
            enabling_human_id=enabling_human,
            membership_version=member.version,
            worker_actor_id=actor.id,
            actor_version=actor.version,
            tenant_generation=tenant.version,
            outbound_paused=tenant.outbound_paused,
            native_reminders_disabled=True,
            configuration_reviewed=True,
        )
        self.db.add(activation)
        self.db.flush()
        return activation

    def save_policy(
        self, ctx: TenantContext, command: PolicySave, expected_version: int, policy_id: UUID | None = None
    ) -> PolicyVersion:
        tenant = self.identity.authorize(ctx, Permission.SETTINGS)
        if expected_version != command.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        if command.activate and not command.native_reminders_disabled:
            raise HTTPException(409, "NATIVE_REMINDER_ATTESTATION_REQUIRED")
        if command.activate and not command.configuration_reviewed:
            raise HTTPException(409, "CONFIGURATION_REVIEW_REQUIRED")
        policy = self.policy(ctx, policy_id) if policy_id else None
        if policy is not None and policy.archived_at is not None:
            raise HTTPException(409, "POLICY_ARCHIVED")
        if (policy.version if policy else 0) != expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        duplicate = self.db.scalar(
            select(CollectionPolicy).where(
                CollectionPolicy.tenant_id == tenant.id, CollectionPolicy.name == command.name
            )
        )
        if duplicate is not None and (policy is None or duplicate.id != policy.id):
            raise HTTPException(409, "POLICY_NAME_EXISTS")
        for override in command.definition.overrides:
            from aco.features.accounting.models import Customer

            customer = self.db.get(Customer, override.customer_id)
            if customer is None or customer.tenant_id != ctx.tenant_id:
                raise HTTPException(422, "INVALID_CUSTOMER_OVERRIDE")
        if policy is None:
            policy = CollectionPolicy(tenant_id=tenant.id, name=command.name)
            self.db.add(policy)
            self.db.flush()
        else:
            if policy.name != command.name:
                # Preserve the label actually observed before this rename. Do not
                # invent a creation-time name for pre-T08 versions lacking that evidence.
                for old in self.db.scalars(
                    select(CollectionPolicyVersion).where(
                        CollectionPolicyVersion.tenant_id == tenant.id,
                        CollectionPolicyVersion.policy_id == policy.id,
                    )
                ):
                    named = self.db.scalar(
                        select(PolicyEvent.id).where(
                            PolicyEvent.policy_version_id == old.id,
                            PolicyEvent.kind.in_(["saved", "legacy_name_preserved"]),
                        )
                    )
                    if named is None:
                        self._event(ctx, old, "legacy_name_preserved")
            self._cancel_future(ctx, policy.id)
            policy.version += 1
            policy.name = command.name
        if policy.active and not command.activate:
            tenant.automation_enabled = False
            tenant.version += 1
        if command.activate:
            existing = self.db.scalar(
                select(CollectionPolicy).where(
                    CollectionPolicy.tenant_id == tenant.id,
                    CollectionPolicy.active.is_(True),
                    CollectionPolicy.id != policy.id,
                )
            )
            if existing is not None:
                raise HTTPException(409, "ACTIVE_POLICY_REPLACEMENT_REQUIRED")
            tenant.automation_enabled = True
            tenant.version += 1
        policy.active = command.activate
        version = CollectionPolicyVersion(
            tenant_id=tenant.id,
            policy_id=policy.id,
            version=policy.version,
            state="ACTIVE" if command.activate else "DRAFT",
            definition=command.definition.model_dump(mode="json"),
            synthetic=tenant.is_demo,
        )
        self.db.add(version)
        self.db.flush()
        event = self._event(ctx, version, "saved")
        if command.activate:
            self._activation(ctx, tenant, version, event, ctx.actor_id)
            self._recompute_all(ctx)
        return self.view(policy, version)

    def _proof(self, ctx: ActorContext, *, resuming: bool = False) -> _SchedulingProof | None:
        tenant = self.identity.authorize(ctx, Permission.INVOICE_READ)
        if not tenant.automation_enabled:
            return None
        policy = self.db.scalar(
            select(CollectionPolicy).where(
                CollectionPolicy.tenant_id == tenant.id,
                CollectionPolicy.active.is_(True),
                CollectionPolicy.archived_at.is_(None),
            )
        )
        if policy is None:
            return None
        version = self.latest(policy)
        activation = self.db.scalar(
            select(PolicyActivation)
            .where(PolicyActivation.tenant_id == tenant.id, PolicyActivation.policy_version_id == version.id)
            .order_by(PolicyActivation.created_at.desc())
        )
        if activation is None:
            raise HTTPException(409, "POLICY_ACTIVATION_MISSING")
        validate_activation(self.db, tenant, policy, version, activation, check_generation=not resuming)
        return _SchedulingProof(tenant, policy, version, activation)

    def _cancel_future(self, ctx: ActorContext, policy_id: UUID) -> int:
        versions = select(CollectionPolicyVersion.id).where(CollectionPolicyVersion.policy_id == policy_id)
        actions = list(
            self.db.scalars(
                select(ScheduledAction)
                .where(
                    ScheduledAction.tenant_id == ctx.tenant_id,
                    ScheduledAction.policy_version_id.in_(versions),
                    ScheduledAction.kind == "policy_due",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        cancelled = 0
        for action in actions:
            if action.status in {"queued", "proposed", "validated", "awaiting_approval"}:
                if self._exposed(action.occurrence_id):
                    continue
                action.status = "cancelled"
                action.version += 1
                action.last_error = "POLICY_RECOMPUTED"
                cancelled += 1
        self.db.flush()
        return cancelled

    def _exposed(self, occurrence_id: UUID | None) -> bool:
        if occurrence_id is None:
            return True
        exposures = self.db.scalar(
            select(ContactReservation.id)
            .where(ContactReservation.occurrence_id == occurrence_id, ContactReservation.status != "released")
            .limit(1)
        )
        actions = list(
            self.db.scalars(
                select(ScheduledAction)
                .where(ScheduledAction.occurrence_id == occurrence_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if exposures or any(a.status in {"executing", "unknown_outcome"} for a in actions):
            return True
        for action in actions:
            attempts = list(
                self.db.scalars(select(ActionExecution).where(ActionExecution.action_id == action.id))
            )
            if any(a.status in {"executing", "unknown_outcome"} for a in attempts):
                return True
            if action.status == "succeeded" and not (action.result or {}).get("eligibility_only"):
                return True
        return False

    def _recompute_all(self, ctx: ActorContext) -> None:
        if isinstance(ctx, ServiceContext):
            raise HTTPException(403, "TENANT_WIDE_POLICY_SCOPE_FORBIDDEN")
        for key in self.db.scalars(
            select(InvoiceSnapshot.id).where(InvoiceSnapshot.tenant_id == ctx.tenant_id)
        ):
            self.recompute_next_action(ctx, key, self.at)

    def recompute_next_action(self, ctx: ActorContext, invoice_id: UUID, now: datetime) -> NextAction | None:
        proof = self._proof(ctx)
        if proof is None:
            return None
        if isinstance(ctx, ServiceContext):
            scoped = self.db.get(ScheduledAction, ctx.action_id)
            if scoped is None or scoped.invoice_id != invoice_id:
                raise HTTPException(403, "SCHEDULING_SCOPE_OUTSIDE_ACTION")
        invoice = self.db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if invoice is None or invoice.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "INVOICE_NOT_FOUND")
        preview = self.preview_policy(ctx, self.view(proof.policy, proof.version), now)
        candidates = [
            item
            for item in preview.items
            if item.invoice_id == invoice_id
            and item.next_at is not None
            and not [r for r in item.reasons if r != "OUTBOUND_EXECUTOR_UNAVAILABLE"]
        ]
        # Reconsideration selects one current next action, never a queue of suppressed reminders.
        rule_offsets = {
            str(r.id): r.offset_days for r in PolicyDefinition.model_validate(proof.version.definition).rules
        }
        candidates.sort(key=lambda i: (i.next_at, -rule_offsets[str(i.rule_id)], str(i.rule_id)))
        candidates = candidates[:1]
        chosen_rule = candidates[0].rule_id if candidates else None
        if chosen_rule:
            for old in self.db.scalars(
                select(PolicyOccurrence).where(
                    PolicyOccurrence.tenant_id == ctx.tenant_id,
                    PolicyOccurrence.invoice_id == invoice_id,
                    PolicyOccurrence.policy_id == proof.policy.id,
                    PolicyOccurrence.state == "pending_recompute",
                )
            ):
                if old.rule_id != chosen_rule or old.due_date_anchor != invoice.due_date:
                    if not self._exposed(old.id):
                        old.state, old.last_reason = "superseded", "CURRENT_NEXT_ACTION_SELECTED_NO_BACKLOG"
        created: list[NextAction] = []
        for item in candidates:
            assert item.next_at is not None
            key = hashlib.sha256(
                json.dumps(
                    [
                        str(ctx.tenant_id),
                        str(invoice.provider_connection_id),
                        str(invoice.id),
                        str(proof.policy.id),
                        str(item.rule_id),
                        invoice.due_date.isoformat(),
                    ]
                ).encode()
            ).hexdigest()
            occurrence = self.db.scalar(
                select(PolicyOccurrence)
                .where(PolicyOccurrence.tenant_id == ctx.tenant_id, PolicyOccurrence.operation_key == key)
                .with_for_update()
            )
            if occurrence is None:
                occurrence = PolicyOccurrence(
                    tenant_id=ctx.tenant_id,
                    invoice_id=invoice.id,
                    policy_id=proof.policy.id,
                    rule_id=item.rule_id,
                    due_date_anchor=invoice.due_date,
                    operation_key=key,
                )
                self.db.add(occurrence)
                self.db.flush()
            if self._exposed(occurrence.id):
                continue
            if occurrence.state == "pending_recompute":
                occurrence.version += 1
                occurrence.state = "available"
            # Immutable grant generations retain one stable occurrence across continuation.
            action_key = hashlib.sha256(
                f"{key}:{proof.version.id}:{proof.activation.id}:{occurrence.version}".encode()
            ).hexdigest()
            action = self.db.scalar(
                select(ScheduledAction).where(
                    ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.operation_key == action_key
                )
            )
            if action is not None:
                if action.status == "queued":
                    created.append(
                        NextAction(
                            action_id=action.id,
                            occurrence_id=occurrence.id,
                            rule_id=item.rule_id,
                            due_at=action.due_at,
                            policy_version_id=proof.version.id,
                        )
                    )
                continue
            connection = self.db.get(
                ProviderConnection, invoice.provider_connection_id, populate_existing=True
            )
            if (
                connection is None
                or connection.status != "READY"
                or connection.provider not in {"demo", "mock"}
            ):
                continue
            action = ScheduledAction(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                invoice_id=invoice.id,
                source_revision=invoice.source_revision,
                source_event_id=None,
                policy_version_id=proof.version.id,
                policy_generation=proof.policy.version,
                policy_event_id=proof.activation.event_id,
                policy_activation_id=proof.activation.id,
                occurrence_id=occurrence.id,
                initiator_id=proof.activation.enabling_human_id,
                membership_version=proof.activation.membership_version,
                tenant_version=proof.tenant.version,
                outbound_paused=proof.tenant.outbound_paused,
                automation_enabled=proof.tenant.automation_enabled,
                worker_actor_id=proof.activation.worker_actor_id,
                actor_version=proof.activation.actor_version,
                connection_version=connection.version,
                correlation_id=ctx.correlation_id,
                kind="policy_due",
                operation_key=action_key,
                command={
                    "rule_id": str(item.rule_id),
                    "channel": item.channel,
                    "destination": item.destination,
                },
                due_at=item.next_at,
            )
            self.db.add(action)
            self.db.flush()
            self.db.add(
                OutboxEvent(
                    tenant_id=ctx.tenant_id,
                    action_id=action.id,
                    action_version=action.version,
                    due_at=action.due_at,
                )
            )
            self.identity.audit(ctx, "policy.action_queued", action.id)
            created.append(
                NextAction(
                    action_id=action.id,
                    occurrence_id=occurrence.id,
                    rule_id=item.rule_id,
                    due_at=action.due_at,
                    policy_version_id=proof.version.id,
                )
            )
        return min(created, key=lambda a: a.due_at) if created else None

    def preview_policy(
        self,
        ctx: ActorContext,
        version: PolicyVersion,
        as_of: datetime,
        *,
        exclude_operation_id: UUID | None = None,
    ) -> PolicyPreview:
        from aco.features.policies.guards import PolicyGuards

        policy = self.policy(ctx, version.policy_id)
        saved = self.db.get(CollectionPolicyVersion, version.id)
        if saved is None or saved.policy_id != policy.id:
            raise HTTPException(404, "POLICY_VERSION_NOT_FOUND")
        try:
            definition = PolicyDefinition.model_validate(saved.definition)
        except ValueError as exc:
            raise HTTPException(409, "POLICY_CONFIGURATION_REQUIRED") from exc
        tenant = self.db.get(Tenant, ctx.tenant_id)
        assert tenant
        items = []
        query = (
            select(InvoiceSnapshot)
            .where(InvoiceSnapshot.tenant_id == ctx.tenant_id)
            .execution_options(populate_existing=True)
        )
        if isinstance(ctx, ServiceContext):
            action = self.db.get(ScheduledAction, ctx.action_id)
            source_promise = (
                isinstance(ctx, SourceReadContext)
                and action is not None
                and action.kind == "promise_check"
                and saved.id == self.latest(policy).id
            )
            if action is None or (not source_promise and action.policy_version_id != saved.id):
                raise HTTPException(403, "POLICY_SCOPE_OUTSIDE_ACTION")
            query = query.where(InvoiceSnapshot.id == action.invoice_id)
        for invoice in self.db.scalars(query.order_by(InvoiceSnapshot.number)):
            contact = self.db.scalar(
                select(CustomerContact)
                .where(
                    CustomerContact.tenant_id == ctx.tenant_id,
                    CustomerContact.customer_id == invoice.customer_id,
                )
                .execution_options(populate_existing=True)
            )
            control = (
                self.db.scalar(
                    select(RecipientControl)
                    .where(
                        RecipientControl.tenant_id == ctx.tenant_id, RecipientControl.contact_id == contact.id
                    )
                    .execution_options(populate_existing=True)
                )
                if contact
                else None
            )
            collection = self.db.scalar(
                select(CollectionRecord).where(
                    CollectionRecord.tenant_id == ctx.tenant_id, CollectionRecord.invoice_id == invoice.id
                )
            )
            override = next((o for o in definition.overrides if o.customer_id == invoice.customer_id), None)
            for rule in definition.rules:
                reasons = []
                valid = bool(
                    contact
                    and control
                    and control.destination_verified
                    and control.reviewed_destination == contact.email
                    and control.reviewed_contact_revision == contact.source_revision
                )
                if not valid:
                    reasons.append("recipient_unverified")
                if control and (control.opt_out or rule.channel not in control.allowed_channels):
                    reasons.append("recipient_channel_excluded")
                if override and (override.opt_out or rule.channel not in override.allowed_channels):
                    reasons.append("customer_override_excluded")
                if rule.channel == "voice":
                    if not definition.voice_enabled:
                        reasons.append("voice_disabled")
                    if (
                        as_of.astimezone(ZoneInfo(tenant.timezone)).date() - invoice.due_date
                    ).days < definition.voice_after_days:
                        reasons.append("voice_overdue_threshold")
                    reasons.append("voice_permission_unavailable")
                zone = control.timezone if control and control.timezone_verified else None
                fallback = bool(
                    control
                    and control.owner_confirmed_tenant_timezone
                    and control.confirmed_tenant_timezone == tenant.timezone
                )
                candidate = datetime.combine(
                    invoice.due_date
                    + timedelta(days=rule.offset_days + (override.offset_adjustment_days if override else 0)),
                    time.fromisoformat(rule.local_time),
                )
                calendar = recipient_calendar(
                    candidate,
                    zone,
                    tenant_timezone=tenant.timezone,
                    owner_confirmed=fallback,
                    opens_at=definition.opens_at,
                    closes_at=definition.closes_at,
                    weekdays=tuple(definition.weekdays),
                )
                future = calendar.next_local.astimezone(UTC) if calendar.next_local else None
                if calendar.reason == "timezone_review":
                    reasons.append(calendar.reason)
                if future is not None and future < as_of:
                    occurrence = self.db.scalar(
                        select(PolicyOccurrence).where(
                            PolicyOccurrence.tenant_id == ctx.tenant_id,
                            PolicyOccurrence.invoice_id == invoice.id,
                            PolicyOccurrence.policy_id == policy.id,
                            PolicyOccurrence.rule_id == rule.id,
                            PolicyOccurrence.due_date_anchor == invoice.due_date,
                        )
                    )
                    if occurrence and occurrence.state == "pending_recompute" and occurrence.reconsider_after:
                        continuation = recipient_calendar(
                            max(as_of + timedelta(seconds=1), occurrence.reconsider_after),
                            zone,
                            tenant_timezone=tenant.timezone,
                            owner_confirmed=fallback,
                            opens_at=definition.opens_at,
                            closes_at=definition.closes_at,
                            weekdays=tuple(definition.weekdays),
                        )
                        future = continuation.next_local.astimezone(UTC) if continuation.next_local else None
                    else:
                        future = None
                        reasons.append("past_occurrence_no_backlog")
                operational = PolicyGuards(self.db, as_of).operational_status(ctx, invoice.id)
                reasons.extend(operational.reasons)
                if contact and contact.email:
                    cap = PolicyGuards(self.db, as_of).contact_status(
                        ctx,
                        invoice.customer_id,
                        contact.email,
                        cap_24h=definition.cap_24h,
                        cap_7d=definition.cap_7d,
                        exclude_operation_id=exclude_operation_id,
                    )
                    reasons.extend(cap.reasons)
                reasons.append("OUTBOUND_EXECUTOR_UNAVAILABLE")
                items.append(
                    PreviewItem(
                        invoice_id=invoice.id,
                        invoice_number=invoice.number,
                        customer_id=invoice.customer_id,
                        due_date=invoice.due_date,
                        rule_id=rule.id,
                        channel=rule.channel,
                        destination=contact.email if contact else None,
                        recipient_timezone=zone or (tenant.timezone if fallback else None),
                        next_at=future,
                        holds=collection.hold_reasons if collection else None,
                        reasons=list(dict.fromkeys(reasons)),
                        source_health="current"
                        if invoice.verified
                        and timedelta(0) <= as_of - invoice.observed_at <= timedelta(minutes=15)
                        else "reverification_required",
                    )
                )
        activation = self.db.scalar(
            select(PolicyActivation.id).where(PolicyActivation.policy_version_id == saved.id)
        )
        return PolicyPreview(
            policy_version_id=saved.id, as_of=as_of, native_reminder_warning=activation is None, items=items
        )

    def archive_policy(
        self, ctx: TenantContext, policy_id: UUID, expected_version: int, command: PolicyArchive | None = None
    ) -> PolicyArchiveResult:
        tenant = self.identity.authorize(ctx, Permission.SETTINGS)
        policy = self.policy(ctx, policy_id)
        if policy.version != expected_version or policy.archived_at:
            raise HTTPException(409, "VERSION_CONFLICT")
        command = command or PolicyArchive(expected_version=expected_version)
        replacement = None
        if policy.active:
            if command.replacement_policy_id:
                replacement = self.policy(ctx, command.replacement_policy_id)
                if command.replacement_expected_version is None:
                    raise HTTPException(409, "REPLACEMENT_VERSION_REQUIRED")
                if replacement.version != command.replacement_expected_version:
                    raise HTTPException(409, "REPLACEMENT_VERSION_CONFLICT")
                if replacement.id == policy.id or replacement.archived_at or replacement.active:
                    raise HTTPException(409, "INVALID_REPLACEMENT_POLICY")
                PolicyDefinition.model_validate(self.latest(replacement).definition)
                if not command.native_reminders_disabled or not command.configuration_reviewed:
                    raise HTTPException(409, "REPLACEMENT_REVIEW_REQUIRED")
            elif not command.suspend_automation:
                raise HTTPException(409, "REPLACEMENT_OR_SUSPEND_REQUIRED")
        count = self._cancel_future(ctx, policy.id)
        was_active = policy.active
        policy.active, policy.archived_at = False, self.at
        policy.version += 1
        self._event(ctx, self.latest(policy), "archived")
        if was_active:
            tenant.version += 1
            tenant.automation_enabled = replacement is not None
        if replacement:
            replacement.active = True
            version = self.latest(replacement)
            event = self._event(ctx, version, "replacement_activated")
            self._activation(ctx, tenant, version, event, ctx.actor_id)
            self._recompute_all(ctx)
        return PolicyArchiveResult(
            policy_id=policy.id,
            archived=True,
            automation_enabled=tenant.automation_enabled,
            cancelled_actions=count,
            replacement_policy_id=replacement.id if replacement else None,
        )

    def recompute_policy(
        self, ctx: TenantContext, policy_id: UUID, expected_version: int
    ) -> list[NextAction]:
        self.identity.authorize(ctx, Permission.SETTINGS)
        policy = self.policy(ctx, policy_id)
        if policy.version != expected_version or not policy.active or policy.archived_at:
            raise HTTPException(409, "ACTIVE_POLICY_VERSION_REQUIRED")
        proof = self._proof(ctx)
        if proof is None or proof.policy.id != policy.id:
            raise HTTPException(409, "ACTIVE_REVIEWED_POLICY_REQUIRED")
        self._cancel_future(ctx, policy.id)
        event = self._event(ctx, proof.version, "recomputed")
        self._activation(ctx, proof.tenant, proof.version, event, proof.activation.enabling_human_id)
        results = []
        for key in self.db.scalars(
            select(InvoiceSnapshot.id).where(InvoiceSnapshot.tenant_id == ctx.tenant_id)
        ):
            action = self.recompute_next_action(ctx, key, self.at)
            if action:
                results.append(action)
        return results

    def recipients(self, ctx: ActorContext) -> list[RecipientView]:
        if isinstance(ctx, ServiceContext):
            raise HTTPException(403, "TENANT_WIDE_POLICY_SCOPE_FORBIDDEN")
        tenant = self.identity.authorize(ctx, Permission.INVOICE_READ)
        rows = []
        for contact in self.db.scalars(
            select(CustomerContact)
            .where(CustomerContact.tenant_id == ctx.tenant_id)
            .execution_options(populate_existing=True)
        ):
            control = self.db.scalar(
                select(RecipientControl)
                .where(RecipientControl.tenant_id == ctx.tenant_id, RecipientControl.contact_id == contact.id)
                .execution_options(populate_existing=True)
            )
            rows.append(
                RecipientView(
                    contact_id=contact.id,
                    customer_id=contact.customer_id,
                    destination=contact.email,
                    source_revision=contact.source_revision,
                    reviewed_destination=control.reviewed_destination if control else None,
                    reviewed_contact_revision=control.reviewed_contact_revision if control else None,
                    review_matches_source=bool(
                        control
                        and control.reviewed_destination == contact.email
                        and control.reviewed_contact_revision == contact.source_revision
                    ),
                    tenant_timezone=tenant.timezone,
                    confirmed_tenant_timezone=control.confirmed_tenant_timezone if control else None,
                    version=control.version if control else 0,
                    timezone=control.timezone if control else None,
                    timezone_verified=bool(control and control.timezone_verified),
                    owner_confirmed_tenant_timezone=bool(control and control.owner_confirmed_tenant_timezone),
                    destination_verified=bool(control and control.destination_verified),
                    allowed_channels=control.allowed_channels if control else [],
                    opt_out=bool(control and control.opt_out),
                    evidence=control.evidence if control else None,
                )
            )
        return rows

    def save_recipient(self, ctx: TenantContext, key: UUID, command: RecipientSave) -> RecipientView:
        tenant = self.identity.authorize(ctx, Permission.SETTINGS)
        contact = self.db.get(CustomerContact, key, populate_existing=True)
        if contact is None or contact.tenant_id != tenant.id:
            raise HTTPException(404, "CONTACT_NOT_FOUND")
        row = self.db.scalar(
            select(RecipientControl)
            .where(RecipientControl.tenant_id == tenant.id, RecipientControl.contact_id == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (row.version if row else 0) != command.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        if (
            command.expected_contact_revision != contact.source_revision
            or command.expected_destination != contact.email
        ):
            raise HTTPException(409, "RECIPIENT_SOURCE_CHANGED")
        if command.owner_confirmed_tenant_timezone and command.expected_tenant_timezone != tenant.timezone:
            raise HTTPException(409, "RECIPIENT_TIMEZONE_CHANGED")
        if row is None:
            row = RecipientControl(
                tenant_id=tenant.id, provider_connection_id=contact.provider_connection_id, contact_id=key
            )
            self.db.add(row)
        else:
            row.version += 1
        for field, value in command.model_dump(
            exclude={
                "expected_version",
                "expected_contact_revision",
                "expected_destination",
                "expected_tenant_timezone",
            }
        ).items():
            setattr(row, field, value)
        row.reviewer_id = ctx.actor_id
        row.reviewed_destination, row.reviewed_contact_revision = contact.email, contact.source_revision
        row.confirmed_tenant_timezone = tenant.timezone if command.owner_confirmed_tenant_timezone else None
        self.db.flush()
        self.identity.audit(ctx, "policy.recipient_reviewed", row.id)
        active = self.db.scalar(
            select(CollectionPolicy).where(
                CollectionPolicy.tenant_id == tenant.id, CollectionPolicy.active.is_(True)
            )
        )
        if active:
            self._cancel_future(ctx, active.id)
            # Changed destination grants require fresh Owner-reviewed authority.
            tenant.version += 1
            version = self.latest(active)
            event = self._event(ctx, version, "recipient_recomputed")
            self._activation(ctx, tenant, version, event, ctx.actor_id)
            self._recompute_all(ctx)
        return next(r for r in self.recipients(ctx) if r.contact_id == key)

    def set_outbound_pause(self, ctx: TenantContext, scope: str, paused: bool) -> PauseState:
        permission = (
            (Permission.GLOBAL_PAUSE if paused else Permission.GLOBAL_RESUME)
            if scope == "global"
            else (Permission.PAUSE if paused else Permission.RESUME)
        )
        tenant = self.identity.authorize(ctx, permission)
        if scope != "tenant":
            raise HTTPException(403, "GLOBAL_SYSTEM_AUTHORITY_REQUIRED")
        active = self.db.scalar(
            select(CollectionPolicy).where(
                CollectionPolicy.tenant_id == tenant.id, CollectionPolicy.active.is_(True)
            )
        )
        proof = self._proof(ctx, resuming=True) if not paused else None
        if not paused and proof is None:
            raise HTTPException(409, "ACTIVE_REVIEWED_POLICY_REQUIRED")
        if tenant.outbound_paused != paused:
            if active:
                self._cancel_future(ctx, active.id)
            tenant.outbound_paused = paused
            tenant.version += 1
            if proof:
                event = self._event(ctx, proof.version, "resumed")
                self._activation(ctx, tenant, proof.version, event, proof.activation.enabling_human_id)
                self._recompute_all(ctx)
        self.identity.audit(ctx, "policy.paused" if paused else "policy.resumed", tenant.id)
        return PauseState(
            scope=scope,
            paused=tenant.outbound_paused,
            automation_enabled=tenant.automation_enabled,
            version=tenant.version,
        )

    def _collection(self, ctx: ActorContext, invoice_id: UUID) -> CollectionRecord:
        row = self.db.scalar(
            select(CollectionRecord)
            .where(CollectionRecord.tenant_id == ctx.tenant_id, CollectionRecord.invoice_id == invoice_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(409, "OPERATIONAL_PROJECTION_MISSING")
        return row

    def place_hold(
        self,
        ctx: TenantContext,
        invoice_id: UUID,
        reason: str,
        evidence: str,
        expires_at: datetime | None = None,
    ) -> UUID:
        self.identity.authorize(ctx, Permission.INVESTIGATE)
        if (
            reason not in {"active_promise", "active_dispute", "manual_hold", "contact_review", "legal_hold"}
            or not evidence.strip()
        ):
            raise HTTPException(422, "INVALID_HOLD_EVIDENCE")
        if expires_at is not None and reason != "active_promise":
            raise HTTPException(422, "HOLD_REQUIRES_EXPLICIT_RELEASE")
        collection = self._collection(ctx, invoice_id)
        hold = CollectionHold(
            tenant_id=ctx.tenant_id,
            invoice_id=invoice_id,
            reason=reason,
            evidence=evidence,
            creator_id=ctx.actor_id,
            expires_at=expires_at,
        )
        self.db.add(hold)
        collection.hold_reasons = sorted(set(collection.hold_reasons) | {reason})
        collection.version += 1
        self.db.flush()
        self.identity.audit(ctx, "policy.hold_placed", hold.id)
        return hold.id

    def release_expired_holds(self, ctx: TenantContext, invoice_id: UUID, at: datetime) -> None:
        self.identity.authorize(ctx, Permission.RESOLVE)
        collection = self._collection(ctx, invoice_id)
        rows = list(
            self.db.scalars(
                select(CollectionHold)
                .where(
                    CollectionHold.tenant_id == ctx.tenant_id,
                    CollectionHold.invoice_id == invoice_id,
                    CollectionHold.released_at.is_(None),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        released = set()
        for row in rows:
            if row.reason == "active_promise" and row.expires_at and row.expires_at <= at:
                row.released_at, row.release_evidence = at, "Stored promise expiry reached"
                released.add(row.reason)
                self.identity.audit(ctx, "policy.hold_expired", row.id)
        remaining = {row.reason for row in rows if row.released_at is None}
        collection.hold_reasons = sorted(set(collection.hold_reasons) - (released - remaining))
        if released:
            collection.version += 1
            self.recompute_next_action(ctx, invoice_id, at)

    def submitted_contacts(self, ctx: ActorContext, invoice_id: UUID) -> list[ContactReservation]:
        self.identity.authorize(ctx, Permission.INVOICE_READ)
        if isinstance(ctx, ServiceContext):
            action = self.db.get(ScheduledAction, ctx.action_id)
            if action is None or action.invoice_id != invoice_id:
                raise HTTPException(403, "CONTACT_SCOPE_OUTSIDE_ACTION")
        return list(
            self.db.scalars(
                select(ContactReservation).where(
                    ContactReservation.tenant_id == ctx.tenant_id,
                    ContactReservation.invoice_id == invoice_id,
                    ContactReservation.submitted_at.is_not(None),
                )
            )
        )


def validate_activation(
    db: Session,
    tenant: Tenant,
    policy: CollectionPolicy,
    version: CollectionPolicyVersion,
    activation: PolicyActivation,
    *,
    check_generation: bool = True,
) -> None:
    actor = db.get(Actor, activation.worker_actor_id, populate_existing=True)
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == tenant.id, Membership.user_id == activation.enabling_human_id)
        .execution_options(populate_existing=True)
    )
    event = db.get(PolicyEvent, activation.event_id)
    if (
        activation.tenant_id != tenant.id
        or activation.policy_version_id != version.id
        or not policy.active
        or policy.archived_at
        or policy.version != version.version
        or (check_generation and activation.tenant_generation != tenant.version)
        or (check_generation and activation.outbound_paused != tenant.outbound_paused)
        or not tenant.automation_enabled
        or not activation.native_reminders_disabled
        or not activation.configuration_reviewed
        or actor is None
        or not actor.active
        or actor.kind != "service"
        or actor.tenant_id != tenant.id
        or actor.purpose != "durable_worker"
        or actor.version != activation.actor_version
        or member is None
        or not member.active
        or member.version != activation.membership_version
        or Permission.SETTINGS not in ROLE_PERMISSIONS[Role(member.role)]
        or event is None
        or event.tenant_id != tenant.id
        or event.policy_version_id != version.id
    ):
        raise HTTPException(403, "STALE_POLICY_AUTHORITY")
