"""Internal post-read authority tied to immutable evidence, never an outreach grant."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aco.core.context import ServiceContext
from aco.features.accounting.dto import InvoiceData
from aco.features.accounting.models import AccountingObservation, InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.identity.models import now

SOURCE_INCARNATION = uuid4()


class SourceReadContext(ServiceContext):
    """Internal continuation only. Construction does not establish authority."""

    observation_id: UUID


def execution_binding(db: Session, ctx: ServiceContext) -> dict[str, object]:
    from aco.features.identity.service import IdentityService, Permission

    # Original action authority must still be current before mutation. Ordinary actions
    # retain their revision fence; genuine promise checks validate immutable terms instead.
    IdentityService(db).authorize(ctx, Permission.DEMO_PAYMENT)
    action = db.get(ScheduledAction, ctx.action_id)
    attempt = db.scalar(select(ActionExecution).where(ActionExecution.lease_token == ctx.lease_token))
    if action is None or attempt is None or attempt.status != "executing":
        raise HTTPException(403, "SOURCE_EXECUTION_REQUIRED")
    return {
        "action_id": str(action.id),
        "action_version": action.version,
        "attempt_id": str(attempt.id),
        "attempt": attempt.attempt,
        "job_id": action.job_id,
        "lease_token": str(ctx.lease_token),
        "incarnation": str(SOURCE_INCARNATION),
        "requested_revision": action.source_revision,
        "correlation_id": str(ctx.correlation_id),
    }


def validate_source_read(
    db: Session, ctx: SourceReadContext, action: ScheduledAction, invoice: InvoiceSnapshot
) -> None:
    observation = db.get(AccountingObservation, ctx.observation_id)
    attempt = db.scalar(select(ActionExecution).where(ActionExecution.lease_token == ctx.lease_token))
    job = db.execute(
        text(
            "SELECT j.status::text, w.last_heartbeat FROM procrastinate_jobs j "
            "LEFT JOIN procrastinate_workers w ON w.id=j.worker_id WHERE j.id=:id"
        ),
        {"id": ctx.job_id},
    ).first()
    if (
        job is None
        or job.status != "doing"
        or job.last_heartbeat is None
        or job.last_heartbeat < now() - timedelta(seconds=90)
        or observation is None
        or attempt is None
        or action.status != "executing"
        or action.kind not in {"accounting_recheck", "policy_due", "promise_check", "agent_email"}
        or attempt.status != "executing"
        or attempt.finished_at is not None
        or attempt.tenant_id != ctx.tenant_id
        or attempt.action_id != action.id
        or attempt.attempt != action.attempts
        or observation.tenant_id != ctx.tenant_id
        or observation.actor_id != ctx.actor_id
        or observation.invoice_id != action.invoice_id
        or observation.provider_connection_id != action.provider_connection_id
        or not (attempt.started_at <= observation.created_at <= now())
        or observation.execution_binding
        != {
            "action_id": str(action.id),
            "action_version": action.version,
            "attempt_id": str(attempt.id),
            "attempt": attempt.attempt,
            "job_id": action.job_id,
            "lease_token": str(ctx.lease_token),
            "incarnation": str(SOURCE_INCARNATION),
            "requested_revision": action.source_revision,
            "correlation_id": str(ctx.correlation_id),
        }
    ):
        raise HTTPException(403, "INVALID_SOURCE_CONTINUATION")
    try:
        source = InvoiceData.model_validate_json(json.dumps(observation.invoice_data))
    except ValueError as exc:
        raise HTTPException(403, "INVALID_SOURCE_CONTINUATION") from exc
    if (
        not source.verified
        or not invoice.verified
        or source.stamp.revision != invoice.source_revision
        or source.external_id != invoice.external_invoice_id
        or source.stamp.connection_id != invoice.provider_connection_id
        or source.organisation_id != invoice.organisation_id
        or invoice.last_read_at != observation.observed_at
        or not timedelta(0) <= now() - observation.observed_at <= timedelta(minutes=15)
        or not timedelta(0) <= now() - source.stamp.observed_at <= timedelta(minutes=15)
    ):
        raise HTTPException(403, "INVALID_SOURCE_CONTINUATION")
    for key in ("invoice", "payments"):
        proof = observation.provenance.get(key)
        if (
            not isinstance(proof, dict)
            or proof.get("authenticity") != "VERIFIED"
            or proof.get("currentness") != "CURRENT"
        ):
            raise HTTPException(403, "INVALID_SOURCE_CONTINUATION")
