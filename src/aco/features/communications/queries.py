"""Read-only current Membership scoped communications, artifacts and delivery evidence."""

import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.artifacts.models import Artifact
from aco.features.communications.association import reviewed_command
from aco.features.communications.dto import (
    CommunicationFilters,
    CommunicationPage,
    CommunicationView,
    DeliveryEventView,
    InvoiceLinkView,
    SourceView,
    ThreadView,
)
from aco.features.communications.models import (
    Communication,
    CommunicationDeliveryEvent,
    CommunicationInvoiceLink,
    CommunicationSource,
    CommunicationThread,
    EmailDispatchBinding,
)
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.integrations.ports.email_dto import EmailContent


class CommunicationQueries:
    def __init__(self, db: Session, ctx: TenantContext):
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        IdentityService(db).authorize(ctx, Permission.INVOICE_READ)
        self.db, self.ctx = db, ctx

    def view(self, row: Communication) -> CommunicationView:
        links = self.db.scalars(
            select(CommunicationInvoiceLink).where(
                CommunicationInvoiceLink.tenant_id == self.ctx.tenant_id,
                CommunicationInvoiceLink.communication_id == row.id,
            )
        ).all()
        sources = self.db.execute(
            select(CommunicationSource, Artifact)
            .join(Artifact, Artifact.id == CommunicationSource.artifact_id)
            .where(
                CommunicationSource.tenant_id == self.ctx.tenant_id,
                Artifact.tenant_id == self.ctx.tenant_id,
                CommunicationSource.communication_id == row.id,
            )
        ).all()
        events = self.db.scalars(
            select(CommunicationDeliveryEvent)
            .where(
                CommunicationDeliveryEvent.tenant_id == self.ctx.tenant_id,
                CommunicationDeliveryEvent.communication_id == row.id,
            )
            .order_by(CommunicationDeliveryEvent.occurred_at, CommunicationDeliveryEvent.id)
        ).all()
        binding = self.db.scalar(
            select(EmailDispatchBinding).where(
                EmailDispatchBinding.tenant_id == self.ctx.tenant_id,
                EmailDispatchBinding.communication_id == row.id,
            )
        )
        return CommunicationView(
            **{
                key: getattr(row, key)
                for key in (
                    "id",
                    "thread_id",
                    "origin_invoice_id",
                    "provider_connection_id",
                    "native_message_id",
                    "direction",
                    "channel",
                    "kind",
                    "synthetic",
                    "event_at",
                    "delivery_state",
                    "source_state",
                )
            },
            content=EmailContent.model_validate_json(json.dumps(row.content)),
            invoice_links=[InvoiceLinkView.model_validate(link, from_attributes=True) for link in links],
            sources=[
                SourceView(
                    artifact_id=source.artifact_id,
                    representation=source.representation,
                    sha256=source.sha256,
                    storage_state=artifact.storage_state,
                    available=artifact.storage_state == "stored"
                    and artifact.revoked_at is None
                    and not artifact.consent_revoked
                    and artifact.legal_hold_state == "none"
                    and artifact.expires_at > now(),
                )
                for source, artifact in sources
            ],
            delivery_events=[
                DeliveryEventView.model_validate(event, from_attributes=True) for event in events
            ],
            action_id=binding.action_id if binding else None,
        )

    def get(self, key: UUID) -> CommunicationView:
        row = self.db.scalar(
            select(Communication).where(
                Communication.tenant_id == self.ctx.tenant_id, Communication.id == key
            )
        )
        if row is None:
            raise HTTPException(404, "COMMUNICATION_NOT_FOUND")
        return self.view(row)

    def customer_scope(self, customer_id: UUID) -> ColumnElement[bool]:
        """Retained origin/reviewed customer identity; current invoice reassignment is not history."""
        reviewed_ids: set[UUID] = set()
        for link in self.db.scalars(
            select(CommunicationInvoiceLink).where(
                CommunicationInvoiceLink.tenant_id == self.ctx.tenant_id,
                CommunicationInvoiceLink.classification == "reviewed_reference",
            )
        ):
            try:
                captured = reviewed_command(self.db, link)
            except HTTPException:
                continue
            if captured.customer_id == customer_id:
                reviewed_ids.add(link.communication_id)
        return or_(
            Communication.thread_id.in_(
                select(CommunicationThread.id).where(
                    CommunicationThread.tenant_id == self.ctx.tenant_id,
                    CommunicationThread.customer_id == customer_id,
                )
            ),
            Communication.id.in_(reviewed_ids),
        )

    def list(self, filters: CommunicationFilters) -> CommunicationPage:
        query = select(Communication).where(Communication.tenant_id == self.ctx.tenant_id)
        if filters.invoice_id:
            query = query.where(
                Communication.id.in_(
                    select(CommunicationInvoiceLink.communication_id).where(
                        CommunicationInvoiceLink.tenant_id == self.ctx.tenant_id,
                        CommunicationInvoiceLink.invoice_id == filters.invoice_id,
                    )
                )
            )
        if filters.customer_id:
            query = query.where(self.customer_scope(filters.customer_id))
        if filters.thread_id:
            query = query.where(Communication.thread_id == filters.thread_id)
        if filters.direction:
            query = query.where(Communication.direction == filters.direction)
        total = self.db.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.db.scalars(
            query.order_by(Communication.event_at, Communication.id)
            .offset((filters.page - 1) * filters.page_size)
            .limit(filters.page_size)
        ).all()
        return CommunicationPage(
            items=[self.view(row) for row in rows],
            total=total,
            page=filters.page,
            page_size=filters.page_size,
        )

    def thread(self, key: UUID) -> ThreadView:
        row = self.db.scalar(
            select(CommunicationThread).where(
                CommunicationThread.tenant_id == self.ctx.tenant_id, CommunicationThread.id == key
            )
        )
        if row is None:
            raise HTTPException(404, "THREAD_NOT_FOUND")
        return ThreadView(
            id=row.id,
            provider_connection_id=row.provider_connection_id,
            origin_invoice_id=row.origin_invoice_id,
            customer_id=row.customer_id,
            native_thread_id=row.native_thread_id,
            messages=self.list(CommunicationFilters(thread_id=key, page_size=100)).items,
        )
