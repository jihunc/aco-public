"""One retained allocation basis, derived only from exact canonical source relationships."""

import hashlib
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.models import AgentInvoice, AgentRun, AgentUsage
from aco.features.cases.models import CaseEvidence, CaseInvoiceLink, CollectionCase
from aco.features.cases.service import require_human
from aco.features.communications.models import (
    Communication,
    CommunicationInvoiceLink,
    EmailDispatchBinding,
    EmailIngressReceipt,
    EmailSubmission,
)
from aco.features.identity.models import Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.features.impact.economics import equal_weights
from aco.features.impact.economics_dto import AllocationLeaf, CostAllocationView, SaveCostAllocation
from aco.features.impact.models import CostAllocationVersion
from aco.features.usage.models import ProviderUsageEvent, UsageRevision, UsageSource
from aco.features.voice.models import VoiceInvoiceLink, VoiceSession


def source_hash(event_id: UUID, leaves: list[AllocationLeaf], sources: list[UUID]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "event_id": str(event_id),
                "leaves": [leaf.model_dump(mode="json") for leaf in leaves],
                "sources": [str(key) for key in sources],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class CostAllocationService:
    def __init__(self, db: Session):
        self.db = db

    def event(self, ctx: TenantContext, event_id: UUID) -> ProviderUsageEvent:
        row = self.db.scalar(
            select(ProviderUsageEvent)
            .where(ProviderUsageEvent.tenant_id == ctx.tenant_id, ProviderUsageEvent.id == event_id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, "USAGE_EVENT_NOT_FOUND")
        return row

    def verified_leaves(self, event: ProviderUsageEvent) -> tuple[list[AllocationLeaf], list[UUID]]:
        invoices: set[UUID] = set()
        messages: set[UUID] = set()
        sources: set[UUID] = set()
        for link in self.db.scalars(select(UsageSource).where(UsageSource.event_id == event.id)):
            if link.component != event.component:
                continue
            if link.source_kind == "voice_session":
                session = self.db.get(VoiceSession, link.source_id)
                if (
                    session is not None
                    and session.tenant_id == event.tenant_id
                    and session.connection_id == event.provider_connection_id
                ):
                    sources.add(session.id)
                    invoices.update(
                        self.db.scalars(
                            select(VoiceInvoiceLink.invoice_id).where(
                                VoiceInvoiceLink.tenant_id == event.tenant_id,
                                VoiceInvoiceLink.session_id == session.id,
                            )
                        )
                    )
            elif link.source_kind == "communication":
                message = self.db.get(Communication, link.source_id)
                if (
                    message is not None
                    and message.tenant_id == event.tenant_id
                    and message.provider_connection_id == event.provider_connection_id
                ):
                    messages.add(message.id)
            elif link.source_kind == "email_ingress_receipt":
                receipt = self.db.get(EmailIngressReceipt, link.source_id)
                if (
                    receipt is not None
                    and receipt.tenant_id == event.tenant_id
                    and receipt.email_connection_id == event.provider_connection_id
                    and receipt.communication_id is not None
                ):
                    sources.add(receipt.id)
                    messages.add(receipt.communication_id)
            elif link.source_kind == "email_attempt":
                submission = self.db.scalar(
                    select(EmailSubmission).where(
                        EmailSubmission.tenant_id == event.tenant_id,
                        EmailSubmission.usage_attempt_id == link.source_id,
                    )
                )
                binding = self.db.get(EmailDispatchBinding, submission.binding_id) if submission else None
                if (
                    binding is not None
                    and binding.tenant_id == event.tenant_id
                    and binding.email_connection_id == event.provider_connection_id
                ):
                    assert submission is not None
                    sources.add(submission.id)
                    messages.add(binding.communication_id)
        for agent_link in self.db.scalars(
            select(AgentUsage).where(AgentUsage.tenant_id == event.tenant_id, AgentUsage.event_id == event.id)
        ):
            run = self.db.get(AgentRun, agent_link.run_id)
            if (
                run is not None
                and run.tenant_id == event.tenant_id
                and run.provider_connection_id == event.provider_connection_id
            ):
                sources.add(run.id)
                invoices.update(
                    self.db.scalars(
                        select(AgentInvoice.invoice_id).where(
                            AgentInvoice.tenant_id == event.tenant_id, AgentInvoice.run_id == run.id
                        )
                    )
                )
        case_invoices: dict[UUID, set[UUID]] = {}
        for message_id in messages:
            message = self.db.get(Communication, message_id)
            if message is None or message.tenant_id != event.tenant_id:
                continue
            sources.add(message.id)
            linked = set(
                self.db.scalars(
                    select(CommunicationInvoiceLink.invoice_id).where(
                        CommunicationInvoiceLink.tenant_id == event.tenant_id,
                        CommunicationInvoiceLink.communication_id == message.id,
                    )
                )
            )
            invoices.update(linked)
            case_ids = set(
                self.db.scalars(
                    select(CaseEvidence.case_id).where(
                        CaseEvidence.tenant_id == event.tenant_id, CaseEvidence.communication_id == message.id
                    )
                )
            )
            for case_id in case_ids:
                case = self.db.get(CollectionCase, case_id)
                if case is None or case.synthetic != event.synthetic:
                    continue
                for invoice in self.db.scalars(
                    select(CaseInvoiceLink.invoice_id).where(
                        CaseInvoiceLink.tenant_id == event.tenant_id,
                        CaseInvoiceLink.case_id == case_id,
                        CaseInvoiceLink.invoice_id.in_(linked),
                    )
                ):
                    case_invoices.setdefault(invoice, set()).add(case_id)
        leaves: list[tuple[UUID | None, UUID | None]] = []
        for invoice_id in sorted(invoices, key=str):
            invoice_row = self.db.get(InvoiceSnapshot, invoice_id)
            if invoice_row is None or invoice_row.tenant_id != event.tenant_id:
                continue
            cases = sorted(case_invoices.get(invoice_id, set()), key=str)
            leaves.extend((case_id, invoice_id) for case_id in cases) if cases else leaves.append(
                (None, invoice_id)
            )
        if not leaves:
            leaves = [(None, None)]
        return [
            AllocationLeaf(case_id=case, invoice_id=invoice, weight=weight)
            for (case, invoice), weight in zip(leaves, equal_weights(len(leaves)), strict=True)
        ], sorted(sources, key=str)

    def latest(self, event: ProviderUsageEvent) -> CostAllocationVersion | None:
        return self.db.scalar(
            select(CostAllocationVersion)
            .where(
                CostAllocationVersion.tenant_id == event.tenant_id, CostAllocationVersion.event_id == event.id
            )
            .order_by(CostAllocationVersion.version.desc())
            .limit(1)
        )

    def view(self, event: ProviderUsageEvent, row: CostAllocationVersion | None = None) -> CostAllocationView:
        supported, source_ids = self.verified_leaves(event)
        if row is None:
            return CostAllocationView(
                id=None,
                event_id=event.id,
                version=0,
                previous_version_id=None,
                basis_revision_id=None,
                basis="UNALLOCATED" if supported[0].invoice_id is None else "EQUAL_VERIFIED_LEAVES",
                leaves=supported,
                supported_leaves=supported,
                source_hash=source_hash(event.id, supported, source_ids),
                source_ids=source_ids,
                evidence=None,
                created_at=None,
                current_source_matches=True,
            )
        leaves = [AllocationLeaf.model_validate(leaf) for leaf in row.leaves]
        return CostAllocationView(
            id=row.id,
            event_id=event.id,
            version=row.version,
            previous_version_id=row.previous_version_id,
            basis_revision_id=row.revision_id,
            basis=(
                "UNALLOCATED"
                if row.basis == "UNALLOCATED"
                else "EQUAL_VERIFIED_LEAVES"
                if row.basis == "EQUAL_VERIFIED_LEAVES"
                else "OWNER_REVIEWED_WEIGHTS"
            ),
            leaves=leaves,
            supported_leaves=supported,
            source_hash=source_hash(event.id, supported, source_ids),
            source_ids=[UUID(key) for key in row.source_ids],
            evidence=str(row.command["evidence"]),
            created_at=row.created_at,
            current_source_matches={(leaf.case_id, leaf.invoice_id) for leaf in leaves}
            == {(leaf.case_id, leaf.invoice_id) for leaf in supported}
            and set(row.source_ids) == {str(key) for key in source_ids},
        )

    def save(self, ctx: TenantContext, event_id: UUID, command: SaveCostAllocation) -> CostAllocationView:
        require_human(self.db, ctx, Permission.SETTINGS)
        self.db.scalar(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update(key_share=True))
        event = self.event(ctx, event_id)
        payload = {"event_id": str(event_id), **command.model_dump(mode="json")}
        previous_request = self.db.scalar(
            select(CostAllocationVersion).where(
                CostAllocationVersion.tenant_id == ctx.tenant_id,
                CostAllocationVersion.request_id == command.request_id,
            )
        )
        if previous_request is not None:
            if previous_request.command != payload or previous_request.actor_id != ctx.actor_id:
                raise HTTPException(409, "COST_ALLOCATION_REQUEST_CONFLICT")
            return self.view(event, previous_request)
        previous = self.latest(event)
        if command.expected_version_id != (previous.id if previous else None):
            raise HTTPException(409, "COST_ALLOCATION_VERSION_CHANGED")
        supported, source_ids = self.verified_leaves(event)
        if command.reviewed_source_hash != source_hash(event.id, supported, source_ids):
            raise HTTPException(409, "COST_ALLOCATION_SOURCE_CHANGED")
        leaves = command.leaves if command.leaves is not None else supported
        if {(leaf.case_id, leaf.invoice_id) for leaf in leaves} != {
            (leaf.case_id, leaf.invoice_id) for leaf in supported
        }:
            raise HTTPException(403, "COST_ALLOCATION_OUTSIDE_VERIFIED_SOURCE")
        revision = self.db.scalar(
            select(UsageRevision)
            .where(UsageRevision.event_id == event.id)
            .order_by(UsageRevision.version.desc())
            .limit(1)
        )
        if revision is None or command.expected_revision_id != revision.id:
            raise HTTPException(409, "USAGE_REVISION_CHANGED")
        key = uuid4()
        audit = IdentityService(self.db).audit(
            ctx, "usage.cost_allocated", key, {"event_id": str(event.id), "revision_id": str(revision.id)}
        )
        row = CostAllocationVersion(
            id=key,
            tenant_id=ctx.tenant_id,
            event_id=event.id,
            revision_id=revision.id,
            request_id=command.request_id,
            actor_id=ctx.actor_id,
            audit_id=audit.id,
            previous_version_id=previous.id if previous else None,
            version=previous.version + 1 if previous else 1,
            basis="UNALLOCATED"
            if leaves[0].invoice_id is None
            else "OWNER_REVIEWED_WEIGHTS"
            if command.leaves is not None
            else "EQUAL_VERIFIED_LEAVES",
            command=payload,
            leaves=[leaf.model_dump(mode="json") for leaf in leaves],
            source_ids=[str(key) for key in source_ids],
        )
        self.db.add(row)
        self.db.flush()
        return self.view(event, row)
