"""Transactional source follow-ups over canonical action and policy records."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.clock import Clock, SystemClock
from aco.core.context import ActorContext, ServiceContext
from aco.features.accounting.models import AccountingObservation, InvoiceSnapshot
from aco.features.accounting.reconcile import SourceReadContext
from aco.features.actions.models import ScheduledAction
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.guards import PolicyGuards
from aco.features.policies.models import PolicyOccurrence
from aco.features.policies.service import PolicyService


def reconcile_source_followups(
    db: Session, ctx: ActorContext, observation_id: UUID, *, clock: Clock | None = None
) -> None:
    identity = IdentityService(db)
    identity.authorize(
        ctx, Permission.INVOICE_READ if isinstance(ctx, SourceReadContext) else Permission.DEMO_PAYMENT
    )
    observation = db.get(AccountingObservation, observation_id)
    if observation is None or observation.tenant_id != ctx.tenant_id or observation.actor_id != ctx.actor_id:
        raise HTTPException(403, "INVALID_SOURCE_OBSERVATION")
    if isinstance(ctx, ServiceContext) and (
        not isinstance(ctx, SourceReadContext) or ctx.observation_id != observation_id
    ):
        raise HTTPException(403, "SOURCE_CONTINUATION_REQUIRED")
    invoice = db.get(InvoiceSnapshot, observation.invoice_id, populate_existing=True)
    if invoice is None or invoice.provider_connection_id != observation.provider_connection_id:
        raise HTTPException(403, "INVALID_SOURCE_OBSERVATION")
    from aco.features.promises.performance import verify_invoice_promises

    current_clock = clock or SystemClock()
    verify_invoice_promises(db, ctx, observation_id, clock=current_clock)
    at = current_clock.now()
    policy = PolicyService(db, at)
    decision = PolicyGuards(db, at).operational_status(ctx, invoice.id)
    actions = list(
        db.scalars(
            select(ScheduledAction)
            .where(
                ScheduledAction.tenant_id == ctx.tenant_id,
                ScheduledAction.invoice_id == invoice.id,
                ScheduledAction.kind == "policy_due",
            )
            .order_by(ScheduledAction.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    existing_ids = {a.id for a in actions}
    cancelled = []
    for action in actions:
        if action.status not in {"queued", "proposed", "validated", "awaiting_approval"}:
            continue
        if decision.allowed and action.source_revision == invoice.source_revision:
            continue
        # Never represent exposure, an executing attempt or an unknown result as recalled.
        if policy._exposed(action.occurrence_id):
            continue
        reason = (
            "SOURCE_PAID"
            if invoice.verified and invoice.financial_status == "paid"
            else (
                "SOURCE_REVISION_CHANGED"
                if action.source_revision != invoice.source_revision
                else "SOURCE_INELIGIBLE"
            )
        )
        action.status, action.last_error = "cancelled", reason
        action.version += 1  # Existing outbox/job version can no longer claim this action.
        occurrence = db.get(PolicyOccurrence, action.occurrence_id)
        if occurrence is not None:
            occurrence.state, occurrence.last_reason = "pending_recompute", reason
            occurrence.reconsider_after = at
        cancelled.append(str(action.id))
    db.flush()
    selected = None
    blocked = None
    if decision.allowed:
        try:
            selected = policy.recompute_next_action(ctx, invoice.id, at)
        except HTTPException as exc:
            if exc.status_code not in {403, 409}:
                raise
            # Source truth survives revoked/missing policy authority; no new grant is created.
            blocked = str(exc.detail)
    if cancelled or (selected is not None and selected.action_id not in existing_ids) or blocked:
        identity.audit(
            ctx,
            "accounting.followups_reconciled",
            invoice.id,
            {
                "observation_id": str(observation.id),
                "source_revision": invoice.source_revision,
                "cancelled_action_ids": ",".join(cancelled),
                "next_action_id": str(selected.action_id) if selected else "",
                "policy_blocked": blocked or "",
                "outreach_submitted": False,
            },
        )
    db.flush()
