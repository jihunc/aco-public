"""Persisted source/lease-bound capture authority, independent of human roles."""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from aco.features.communications.provider_outbound import ProviderOutboundRuntime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import ServiceContext
from aco.features.actions.models import ScheduledAction
from aco.features.artifacts.dto import ArtifactWrite
from aco.features.communications.activation import validate_activation
from aco.features.communications.models import (
    CaptureGrantRevocation,
    Communication,
    CommunicationCaptureGrant,
    EmailDispatchBinding,
    EmailExecutionActivation,
)
from aco.features.identity.actors import require_job_binding
from aco.features.identity.models import Actor, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.integrations.ports.storage import AuthorizedCaptureContext

_SEAL = object()


@dataclass(frozen=True)
class CaptureInvocation:
    context: AuthorizedCaptureContext
    seal: object


def capture_invocation(db: Session, worker: ServiceContext, grant_id: UUID) -> CaptureInvocation:
    IdentityService(db).authorize(worker, Permission.INVOICE_READ)
    grant = db.get(CommunicationCaptureGrant, grant_id)
    if (
        grant is None
        or grant.tenant_id != worker.tenant_id
        or grant.actor_id != worker.actor_id
        or grant.lease_token != worker.lease_token
        or grant.job_id != worker.job_id
    ):
        raise HTTPException(403, "CAPTURE_GRANT_SCOPE")
    binding = db.get(EmailDispatchBinding, grant.binding_id)
    if binding is None or binding.action_id != worker.action_id:
        raise HTTPException(403, "CAPTURE_GRANT_SCOPE")
    ctx = AuthorizedCaptureContext(
        tenant_id=worker.tenant_id,
        actor_id=worker.actor_id,
        correlation_id=grant.correlation_id,
        grant_id=grant.id,
        communication_id=grant.communication_id,
        storage_connection_id=grant.storage_connection_id,
        artifact_ids=tuple(UUID(str(item["artifact_id"])) for item in grant.objects),
        action_id=worker.action_id,
        action_version=worker.action_version,
        job_id=grant.job_id,
        lease_token=grant.lease_token,
    )
    return CaptureInvocation(ctx, _SEAL)


def validate_capture(
    db: Session,
    invocation: CaptureInvocation,
    grant_id: UUID,
    command: ArtifactWrite | None = None,
    artifact_id: UUID | None = None,
    provider_outbound: "ProviderOutboundRuntime | None" = None,
) -> Tenant:
    if (
        not isinstance(invocation, CaptureInvocation)
        or invocation.seal is not _SEAL
        or invocation.context.grant_id != grant_id
    ):
        raise HTTPException(403, "AUTHENTICATED_CAPTURE_INVOCATION_REQUIRED")
    ctx = invocation.context
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == ctx.tenant_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    grant = db.get(CommunicationCaptureGrant, grant_id, populate_existing=True)
    actor = db.get(Actor, ctx.actor_id, populate_existing=True)
    revoked = db.scalar(select(CaptureGrantRevocation.id).where(CaptureGrantRevocation.grant_id == grant_id))
    if (
        tenant is None
        or grant is None
        or grant.tenant_id != tenant.id
        or not tenant.is_demo
        or grant.tenant_generation != tenant.version
        or actor is None
        or not actor.active
        or actor.version != grant.actor_version
        or actor.kind != "service"
        or actor.purpose != "durable_worker"
        or actor.tenant_id != tenant.id
        or revoked
        or grant.expires_at <= now()
        or grant.issued_at > now()
    ):
        raise HTTPException(403, "CAPTURE_GRANT_REVOKED")
    binding = db.get(EmailDispatchBinding, grant.binding_id)
    message = db.get(Communication, grant.communication_id)
    action = db.get(ScheduledAction, ctx.action_id, populate_existing=True)
    if (
        binding is None
        or message is None
        or binding.tenant_id != tenant.id
        or message.tenant_id != tenant.id
        or binding.communication_id != message.id
        or binding.source_revision != grant.source_revision
        or binding.storage_connection_id != ctx.storage_connection_id
        or action is None
        or action.id != binding.action_id
        or action.version != ctx.action_version
        or action.lease_token != ctx.lease_token
        or action.job_id != ctx.job_id
        or action.worker_actor_id != ctx.actor_id
        or action.status != "executing"
        or action.lease_until is None
        or action.lease_until <= now()
    ):
        raise HTTPException(403, "CAPTURE_SOURCE_OR_JOB_CHANGED")
    require_job_binding(db, action)
    if action.kind == "agent_email":
        from aco.features.review.email_approvals import validate_email_worker

        validate_email_worker(db, action, ctx.lease_token)
    activation = db.get(EmailExecutionActivation, binding.activation_id)
    if activation is None:
        raise HTTPException(403, "CAPTURE_POLICY_REQUIRED")
    validate_activation(db, tenant, activation)
    capture_class = "synthetic_email"
    if activation.provider_binding is not None:
        if provider_outbound is None:
            raise HTTPException(403, "PROVIDER_OUTBOUND_RUNTIME_REQUIRED")
        cap = provider_outbound.validate(db, activation)
        if cap.evidence_mode == "LIVE":
            capture_class = "real"
        if grant.capture_policy != activation.capture_policy:
            raise HTTPException(403, "CAPTURE_POLICY_REQUIRED")
    if command is not None:
        import hashlib

        wanted = {
            "artifact_id": str(command.artifact_id),
            "source_kind": command.source_kind,
            "source_reference": command.source_reference,
            "sha256": hashlib.sha256(command.body).hexdigest(),
            "content_type": command.content_type,
            "size_bytes": len(command.body),
            "operation": "put",
        }
        if command.capture_class != capture_class or wanted not in grant.objects:
            raise HTTPException(403, "CAPTURE_OBJECT_OUTSIDE_GRANT")
    if artifact_id is not None and not any(
        item.get("artifact_id") == str(artifact_id) and item.get("operation") == "read_attachment"
        for item in grant.objects
    ):
        raise HTTPException(403, "ATTACHMENT_OUTSIDE_GRANT")
    return tenant
