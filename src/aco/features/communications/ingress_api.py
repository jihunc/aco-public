"""Authenticated synthetic activation/read and raw provider callback (no human claim routing)."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session

from aco.features.communications.ingress import EmailIngressService, receipt_view
from aco.features.communications.ingress_dto import (
    ActivateIngress,
    IngressActivationView,
    IngressReceiptView,
    IngressSetupView,
    PrepareIngress,
    RetryIngress,
    SimulateIngress,
)
from aco.features.communications.models import EmailIngressReceipt
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService, Permission

router = APIRouter()


def runtime(request: Request) -> EmailIngressService:
    service = getattr(request.app.state, "email_ingress", None)
    if not isinstance(service, EmailIngressService):
        raise HTTPException(403, "SYNTHETIC_INGRESS_DISABLED")
    return service


@router.post("/api/workspaces/{workspace_id}/email-ingress/activations", response_model=IngressActivationView)
async def activate(
    workspace_id: UUID, command: ActivateIngress, request: Request, subject: Subject, db: Database
) -> IngressActivationView:
    return await db.run_sync(
        lambda store: runtime(request).activate(
            store, IdentityService(store).resolve_context(subject, workspace_id), command
        )
    )


@router.post(
    "/api/workspaces/{workspace_id}/email-ingress/provider-activations", response_model=IngressActivationView
)
async def activate_provider(
    workspace_id: UUID, command: ActivateIngress, request: Request, subject: Subject, db: Database
) -> IngressActivationView:
    selected = runtime(request).provider_ingress
    if selected is None:
        raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
    return await db.run_sync(
        lambda store: selected.activate(
            store, IdentityService(store).resolve_context(subject, workspace_id), command
        )
    )


@router.get(
    "/api/workspaces/{workspace_id}/email-ingress/receipts/{receipt_id}", response_model=IngressReceiptView
)
async def receipt(workspace_id: UUID, receipt_id: UUID, subject: Subject, db: Database) -> IngressReceiptView:
    def read(store: Session) -> IngressReceiptView:
        ctx = IdentityService(store).resolve_context(subject, workspace_id)
        IdentityService(store).authorize(ctx, Permission.INVOICE_READ)
        row = store.get(EmailIngressReceipt, receipt_id)
        if row is None or row.tenant_id != workspace_id:
            raise HTTPException(404, "INGRESS_RECEIPT_NOT_FOUND")
        return receipt_view(row)

    return await db.run_sync(read)


@router.post("/api/email-webhooks/{endpoint_id}", response_model=list[IngressReceiptView], status_code=202)
async def shared_callback(endpoint_id: UUID, request: Request) -> list[IngressReceiptView]:
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > 65536:
            raise HTTPException(413, "INGRESS_NOTIFICATION_TOO_LARGE")
    return await runtime(request).receive_shared(endpoint_id, bytes(raw), request.headers)


@router.post("/api/email-ingress/{activation_id}", response_model=list[IngressReceiptView], status_code=202)
async def callback(activation_id: UUID, request: Request) -> list[IngressReceiptView]:
    return await runtime(request).receive(activation_id, await request.body(), request.headers)


@router.post(
    "/api/workspaces/{workspace_id}/email-ingress/receipts/{receipt_id}/retry",
    response_model=IngressReceiptView,
)
async def retry(
    workspace_id: UUID,
    receipt_id: UUID,
    command: RetryIngress,
    request: Request,
    subject: Subject,
    db: Database,
) -> IngressReceiptView:
    return await db.run_sync(
        lambda store: runtime(request).retry(
            store,
            IdentityService(store).resolve_context(subject, workspace_id),
            receipt_id,
            command.expected_version,
        )
    )


@router.post(
    "/api/workspaces/{workspace_id}/email-ingress/activations/{activation_id}/revoke",
    response_model=IngressActivationView,
)
async def revoke(
    workspace_id: UUID,
    activation_id: UUID,
    command: RetryIngress,
    request: Request,
    subject: Subject,
    db: Database,
) -> IngressActivationView:
    return await db.run_sync(
        lambda store: runtime(request).revoke(
            store,
            IdentityService(store).resolve_context(subject, workspace_id),
            activation_id,
            command.expected_version,
        )
    )


@router.get("/api/workspaces/{workspace_id}/email-ingress/setup", response_model=IngressSetupView)
async def ingress_setup(
    workspace_id: UUID, request: Request, subject: Subject, db: Database
) -> IngressSetupView:
    from aco.features.communications.simulation import setup

    return await db.run_sync(
        lambda store: setup(
            store, IdentityService(store).resolve_context(subject, workspace_id), runtime(request)
        )
    )


@router.get(
    "/api/workspaces/{workspace_id}/email-ingress/activations", response_model=list[IngressActivationView]
)
async def active_ingress(
    workspace_id: UUID, request: Request, subject: Subject, db: Database
) -> list[IngressActivationView]:
    return (await ingress_setup(workspace_id, request, subject, db)).activations


@router.post(
    "/api/workspaces/{workspace_id}/email-ingress/activations/{activation_id}/simulate",
    response_model=IngressReceiptView,
    status_code=202,
)
async def simulate_ingress(
    workspace_id: UUID,
    activation_id: UUID,
    command: SimulateIngress,
    request: Request,
    subject: Subject,
    db: Database,
) -> IngressReceiptView:
    from aco.features.communications.simulation import simulate

    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await simulate(runtime(request), ctx, activation_id, command)


@router.post("/api/workspaces/{workspace_id}/email-ingress/setup", response_model=IngressSetupView)
async def prepare_ingress(
    workspace_id: UUID, command: PrepareIngress, request: Request, subject: Subject, db: Database
) -> IngressSetupView:
    from aco.features.communications.preparation import prepare

    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await prepare(runtime(request), ctx, command)
