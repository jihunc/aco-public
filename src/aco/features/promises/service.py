"""Evidence-backed promise capture and independent human confirmation."""

from __future__ import annotations

import builtins
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import Money
from aco.features.accounting.models import Customer, InvoiceSnapshot
from aco.features.cases.dto import CommunicationEvidence
from aco.features.cases.service import (
    authorize_mutation,
    complete_review_task,
    create_review_task,
    place_hold,
    release_hold,
    require_human,
    scope_invoices,
    validate_evidence,
)
from aco.features.communications.models import Communication
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.features.promises.dates import end_of_date_cutoff
from aco.features.promises.dto import (
    CancelPromise,
    ConfirmPromise,
    PromiseAllocation,
    PromiseAllocationView,
    PromiseCheckPlan,
    PromiseHistoryView,
    PromiseInstallmentView,
    PromiseInvoiceView,
    PromiseMoneyJSON,
    PromisePage,
    PromisePredecessorView,
    PromiseView,
    RecordPromise,
)
from aco.features.promises.models import (
    CustomerScopeReview,
    PaymentPromise,
    PromiseEvent,
    PromiseInstallment,
    PromiseInvoiceLink,
    PromiseVerification,
)
from aco.features.voice.evidence_dto import VoiceOperationalEvidence
from aco.features.voice.models import VoiceBusinessEvidence


class PromiseService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _row(self, ctx: Any, promise_id: UUID) -> PaymentPromise:
        row = self.db.scalar(
            select(PaymentPromise)
            .where(PaymentPromise.tenant_id == ctx.tenant_id, PaymentPromise.id == promise_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, "PROMISE_NOT_FOUND")
        return row

    def _links(self, row: PaymentPromise) -> builtins.list[PromiseInvoiceLink]:
        return list(
            self.db.scalars(
                select(PromiseInvoiceLink)
                .where(PromiseInvoiceLink.tenant_id == row.tenant_id, PromiseInvoiceLink.promise_id == row.id)
                .order_by(PromiseInvoiceLink.created_at, PromiseInvoiceLink.id)
                .execution_options(populate_existing=True)
            )
        )

    def _installments(self, row: PaymentPromise) -> builtins.list[PromiseInstallment]:
        return list(
            self.db.scalars(
                select(PromiseInstallment)
                .where(PromiseInstallment.tenant_id == row.tenant_id, PromiseInstallment.promise_id == row.id)
                .order_by(PromiseInstallment.sequence)
                .execution_options(populate_existing=True)
            )
        )

    def _event(self, ctx: Any, row: PaymentPromise, event: str, details: dict[str, object]) -> PromiseEvent:
        audit = IdentityService(self.db).audit(ctx, event, row.id)
        item = PromiseEvent(
            tenant_id=ctx.tenant_id,
            promise_id=row.id,
            actor_id=ctx.actor_id,
            audit_event_id=audit.id,
            event=event,
            details=details,
        )
        self.db.add(item)
        self.db.flush()
        return item

    def _view(self, row: PaymentPromise) -> PromiseView:
        from aco.features.voice.evidence import source_view

        self.db.flush()
        history = self.db.scalars(
            select(PromiseEvent)
            .where(PromiseEvent.tenant_id == row.tenant_id, PromiseEvent.promise_id == row.id)
            .order_by(PromiseEvent.created_at, PromiseEvent.id)
        )
        verification = self.db.scalar(
            select(PromiseVerification)
            .where(PromiseVerification.tenant_id == row.tenant_id, PromiseVerification.promise_id == row.id)
            .order_by(PromiseVerification.created_at.desc(), PromiseVerification.id.desc())
        )
        from aco.features.promises.scheduling import scheduling_status

        return PromiseView(
            id=row.id,
            customer_id=row.customer_id,
            status=cast(Any, row.status),
            version=row.version,
            source_communication_id=row.source_communication_id,
            voice_source=source_view(self.db, row.source_voice_evidence_id),
            source_text=row.source_text,
            source_offset=row.source_offset,
            source_at=row.source_at,
            source_state=row.source_state,
            synthetic=row.synthetic,
            amount=PromiseMoneyJSON.model_validate(row.amount) if row.amount else None,
            due_date=row.due_date,
            timezone=row.timezone,
            cutoff_at=row.cutoff_at,
            confirmed_at=row.confirmed_at,
            confirmation_communication_id=row.confirmation_communication_id,
            confirmation_voice_evidence_id=row.confirmation_voice_evidence_id,
            needs_confirmation=row.status in {"CAPTURED", "NEEDS_CONFIRMATION"},
            confirmation_reasons=row.confirmation_reasons,
            invoice_links=[
                PromiseInvoiceView(
                    invoice_id=link.invoice_id,
                    amount=PromiseMoneyJSON.model_validate(link.amount) if link.amount else None,
                    hold_id=link.hold_id,
                )
                for link in self._links(row)
            ],
            candidate_invoice_ids=[UUID(i) for i in row.candidate_invoice_ids],
            installments=[
                PromiseInstallmentView(
                    id=i.id,
                    due_date=i.due_date,
                    cutoff_at=i.cutoff_at,
                    amount=PromiseMoneyJSON.model_validate(i.amount),
                    allocations=[
                        PromiseAllocationView.model_validate_json(json.dumps(a)) for a in i.allocations
                    ],
                    status=i.status,
                )
                for i in self._installments(row)
            ],
            replacement_lineage=self._captured_lineage(row),
            replaces_promise_id=row.replaces_promise_id,
            superseded_by_id=row.superseded_by_id,
            review_task_id=row.review_task_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            history=[
                PromiseHistoryView(
                    id=e.id, event=e.event, actor_id=e.actor_id, details=e.details, created_at=e.created_at
                )
                for e in history
            ],
            verification_status=cast(Any, verification.verification_status if verification else "UNVERIFIED"),
            scheduling_status=scheduling_status(self.db, row),
        )

    def get(self, ctx: Any, promise_id: UUID) -> PromiseView:
        require_human(self.db, ctx)
        return self._view(self._row(ctx, promise_id))

    def list(self, ctx: Any, invoice_id: UUID | None = None, customer_id: UUID | None = None) -> PromisePage:
        require_human(self.db, ctx)
        q = select(PaymentPromise).where(PaymentPromise.tenant_id == ctx.tenant_id)
        if customer_id:
            q = q.where(PaymentPromise.customer_id == customer_id)
        if invoice_id:
            q = q.where(
                PaymentPromise.id.in_(
                    select(PromiseInvoiceLink.promise_id).where(
                        PromiseInvoiceLink.tenant_id == ctx.tenant_id,
                        PromiseInvoiceLink.invoice_id == invoice_id,
                    )
                )
            )
        return PromisePage(
            items=[
                self._view(r)
                for r in self.db.scalars(
                    q.order_by(PaymentPromise.created_at.desc(), PaymentPromise.id)
                    .limit(100)
                    .execution_options(populate_existing=True)
                )
            ]
        )

    def _validate_terms(
        self, command: RecordPromise, rows: builtins.list[InvoiceSnapshot]
    ) -> tuple[builtins.list[str], datetime | None, dict[UUID, Money]]:
        reasons = ["sender_authority_requires_confirmation"]
        if command.scope == "ambiguous" or not command.invoice_ids:
            reasons.append("invoice_scope_unresolved")
        if command.amount is None:
            reasons.append("amount_missing")
        if command.due_date is None:
            reasons.append("date_unresolved")
        if command.timezone is None:
            reasons.append("timezone_unresolved")
        try:
            cutoff = end_of_date_cutoff(command.due_date, command.timezone)
        except ValueError:
            cutoff = None
            reasons.append("timezone_unresolved")
        if command.amount is not None:
            if any(
                (r.currency, r.exponent) != (command.amount.currency, command.amount.exponent) for r in rows
            ):
                raise HTTPException(422, "PROMISE_CURRENCY_SCOPE_MISMATCH")
        allocations = {}
        for allocation in command.allocations:
            if allocation.invoice_id not in command.invoice_ids or allocation.invoice_id in allocations:
                raise HTTPException(422, "INVALID_PROMISE_ALLOCATION_SCOPE")
            if command.amount is None or (allocation.amount.currency, allocation.amount.exponent) != (
                command.amount.currency,
                command.amount.exponent,
            ):
                raise HTTPException(422, "PROMISE_CURRENCY_MISMATCH")
            allocations[allocation.invoice_id] = allocation.amount
        if command.amount and len(command.invoice_ids) == 1 and not allocations:
            allocations[command.invoice_ids[0]] = command.amount
        if command.amount and (
            set(allocations) != set(command.invoice_ids)
            or sum(a.minor_units for a in allocations.values()) != command.amount.minor_units
        ):
            if allocations:
                raise HTTPException(422, "PROMISE_ALLOCATION_TOTAL_MISMATCH")
            reasons.append("invoice_allocation_unresolved")
        if command.scope == "ambiguous" and command.invoice_ids:
            raise HTTPException(422, "AMBIGUOUS_SCOPE_CANNOT_ASSIGN_INVOICES")
        if command.installments:
            if command.amount is None:
                raise HTTPException(422, "INSTALLMENT_TOTAL_REQUIRED")
            if sum(i.amount.minor_units for i in command.installments) != command.amount.minor_units:
                raise HTTPException(422, "INSTALLMENT_TOTAL_MISMATCH")
            per_invoice = {i: 0 for i in command.invoice_ids}
            for installment in command.installments:
                if (installment.amount.currency, installment.amount.exponent) != (
                    command.amount.currency,
                    command.amount.exponent,
                ) or installment.amount.minor_units <= 0:
                    raise HTTPException(422, "INSTALLMENT_CURRENCY_MISMATCH")
                parts = installment.allocations
                if not parts and len(command.invoice_ids) == 1:
                    parts = [PromiseAllocation(invoice_id=command.invoice_ids[0], amount=installment.amount)]
                if not parts:
                    reasons.append("installment_allocation_unresolved")
                if parts and sum(a.amount.minor_units for a in parts) != installment.amount.minor_units:
                    raise HTTPException(422, "INSTALLMENT_ALLOCATION_MISMATCH")
                seen = set()
                for part in parts:
                    if (
                        part.invoice_id not in per_invoice
                        or part.invoice_id in seen
                        or (part.amount.currency, part.amount.exponent)
                        != (command.amount.currency, command.amount.exponent)
                    ):
                        raise HTTPException(422, "INSTALLMENT_ALLOCATION_MISMATCH")
                    seen.add(part.invoice_id)
                    per_invoice[part.invoice_id] += part.amount.minor_units
            if (
                allocations
                and all(i.allocations or len(command.invoice_ids) == 1 for i in command.installments)
                and any(per_invoice[k] != v.minor_units for k, v in allocations.items())
            ):
                raise HTTPException(422, "INSTALLMENT_INVOICE_TOTAL_MISMATCH")
        return sorted(set(reasons)), cutoff, allocations

    def _pending_clarification(
        self, ctx: Any, old: PaymentPromise, command: RecordPromise, expected_version: int | None
    ) -> None:
        """Fill unknown terms from another retained source; never revise known facts."""
        require_human(self.db, ctx, Permission.TASK_WRITE)
        if expected_version != old.version:
            raise HTTPException(409, "STALE_PRIOR_PROMISE")
        original = RecordPromise.model_validate(old.command)
        old_ids = {link.invoice_id for link in self._links(old)}
        customer = self.db.get(Customer, command.customer_id, populate_existing=True)
        if (
            customer is None
            or customer.tenant_id != ctx.tenant_id
            or customer.id != old.customer_id
            or customer.provider_connection_id != old.provider_connection_id
            or (old_ids and old_ids != set(command.invoice_ids))
        ):
            raise HTTPException(409, "INCOMPATIBLE_PROMISE_CLARIFICATION")
        scope_invoices(self.db, ctx.tenant_id, old.customer_id, list(old_ids))
        validate_evidence(self.db, ctx.tenant_id, old.customer_id, original.evidence, list(old_ids))
        same_source = (
            isinstance(original.evidence, CommunicationEvidence)
            and isinstance(command.evidence, CommunicationEvidence)
            and original.evidence.communication_id == command.evidence.communication_id
        ) or (
            isinstance(original.evidence, VoiceOperationalEvidence)
            and isinstance(command.evidence, VoiceOperationalEvidence)
            and original.evidence.voice_evidence_id == command.evidence.voice_evidence_id
        )
        if same_source:
            raise HTTPException(409, "PROMISE_CLARIFICATION_NEW_SOURCE_REQUIRED")
        for field in ("amount", "due_date", "timezone"):
            known = getattr(original, field)
            if known is not None and known != getattr(command, field):
                raise HTTPException(409, "PROMISE_CLARIFICATION_CONTRADICTS_KNOWN_TERMS")
        # Preserve explicit portions/dates; a missing allocation may be supplied, never guessed.
        parts = {part.invoice_id: part.amount for part in command.allocations}
        if any(parts.get(part.invoice_id) != part.amount for part in original.allocations):
            raise HTTPException(409, "PROMISE_CLARIFICATION_CONTRADICTS_KNOWN_TERMS")
        if original.installments:
            if len(original.installments) != len(command.installments):
                raise HTTPException(409, "PROMISE_CLARIFICATION_CONTRADICTS_KNOWN_TERMS")
            for previous, new in zip(original.installments, command.installments, strict=True):
                allocated = {part.invoice_id: part.amount for part in new.allocations}
                if (
                    previous.due_date != new.due_date
                    or previous.amount != new.amount
                    or any(allocated.get(part.invoice_id) != part.amount for part in previous.allocations)
                ):
                    raise HTTPException(409, "PROMISE_CLARIFICATION_CONTRADICTS_KNOWN_TERMS")

    def _captured_lineage(self, row: PaymentPromise) -> builtins.list[PromisePredecessorView]:
        event = self.db.scalar(
            select(PromiseEvent).where(
                PromiseEvent.tenant_id == row.tenant_id,
                PromiseEvent.promise_id == row.id,
                PromiseEvent.event == "promise.captured",
            )
        )
        if event is None:
            return []
        values = event.details.get("replacement_lineage", [])
        if not isinstance(values, list):
            raise HTTPException(409, "PROMISE_LINEAGE_UNAVAILABLE")
        return [PromisePredecessorView.model_validate_json(json.dumps(value)) for value in values]

    def _replacement_lineage(self, ctx: Any, command: RecordPromise) -> builtins.list[PaymentPromise]:
        result: builtins.list[PaymentPromise] = []
        key = command.replaces_promise_id
        expected = command.replacement_expected_version
        customer = self.db.get(Customer, command.customer_id, populate_existing=True)
        while key is not None:
            if len(result) >= 16 or key in {row.id for row in result}:
                raise HTTPException(409, "PROMISE_LINEAGE_REVIEW_REQUIRED")
            old = self._row(ctx, key)
            if (
                old.customer_id != command.customer_id
                or customer is None
                or old.provider_connection_id != customer.provider_connection_id
            ):
                raise HTTPException(403, "PROMISE_REPLACEMENT_SCOPE_MISMATCH")
            if expected is not None and old.version != expected:
                raise HTTPException(409, "STALE_PRIOR_PROMISE")
            if old.status == "NEEDS_CONFIRMATION":
                self._pending_clarification(ctx, old, command, expected)
            elif old.status != "ACTIVE":
                raise HTTPException(409, "STALE_PRIOR_PROMISE")
            result.append(old)
            if old.status == "ACTIVE":
                # A confirmed ancestor ends this lineage; earlier superseded history remains history.
                break
            previous = RecordPromise.model_validate(old.command)
            key, expected = old.replaces_promise_id, previous.replacement_expected_version
        return result

    def _active_compatibility(self, old: PaymentPromise, command: RecordPromise) -> None:
        if (
            set(command.invoice_ids) != {link.invoice_id for link in self._links(old)}
            or old.amount is None
            or command.amount is None
            or (command.amount.currency, command.amount.exponent)
            != (old.amount["currency"], old.amount["exponent"])
        ):
            raise HTTPException(409, "INCOMPATIBLE_PROMISE_REPLACEMENT")

    def _release_scope_review(self, ctx: Any, row: PaymentPromise, reason: str) -> None:
        block = self.db.scalar(
            select(CustomerScopeReview)
            .where(CustomerScopeReview.tenant_id == ctx.tenant_id, CustomerScopeReview.promise_id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if block is not None and block.released_at is None:
            for hold_id in block.hold_ids:
                release_hold(self.db, ctx, UUID(hold_id), reason)
            block.released_at = now()
            block.release_evidence = reason
            block.version += 1

    def record_promise(self, ctx: Any, command: RecordPromise) -> PromiseView:
        authorize_mutation(self.db, ctx, "record_payment_promise", command)
        rows = scope_invoices(self.db, ctx.tenant_id, command.customer_id, command.invoice_ids)
        scope_invoices(self.db, ctx.tenant_id, command.customer_id, command.candidate_invoice_ids)
        msg = validate_evidence(
            self.db, ctx.tenant_id, command.customer_id, command.evidence, command.invoice_ids
        )
        customer = self.db.scalar(
            select(Customer).where(Customer.tenant_id == ctx.tenant_id, Customer.id == command.customer_id)
        )
        assert customer is not None
        reasons, cutoff, allocations = self._validate_terms(command, rows)
        key = hashlib.sha256(
            json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        prior = self.db.scalar(
            select(PaymentPromise)
            .where(PaymentPromise.tenant_id == ctx.tenant_id, PaymentPromise.capture_key == key)
            .execution_options(populate_existing=True)
        )
        if prior:
            if prior.command != command.model_dump(mode="json"):
                raise HTTPException(409, "PROMISE_IDENTITY_CONFLICT")
            return self._view(prior)
        lineage = self._replacement_lineage(ctx, command)
        at = now()
        row = PaymentPromise(
            tenant_id=ctx.tenant_id,
            customer_id=command.customer_id,
            provider_connection_id=customer.provider_connection_id,
            capture_key=key,
            source_communication_id=msg.id if isinstance(msg, Communication) else None,
            source_voice_evidence_id=msg.id if isinstance(msg, VoiceBusinessEvidence) else None,
            source_text=command.evidence.excerpt,
            source_offset=command.evidence.offset,
            source_at=msg.event_at,
            source_state=msg.source_state,
            synthetic=msg.synthetic,
            amount=command.amount.model_dump(mode="json") if command.amount else None,
            due_date=command.due_date,
            timezone=command.timezone,
            cutoff_at=cutoff,
            confirmation_reasons=reasons,
            candidate_invoice_ids=[str(i) for i in command.candidate_invoice_ids],
            replaces_promise_id=command.replaces_promise_id,
            command=command.model_dump(mode="json"),
            created_at=at,
            updated_at=at,
        )
        self.db.add(row)
        self.db.flush()
        row.review_task_id = create_review_task(
            self.db,
            ctx,
            "Confirm payment commitment",
            "Review original commitment, sender authority, invoice allocation and dates.",
            command.invoice_ids[0] if command.invoice_ids else None,
            at + timedelta(hours=24),
        )
        for invoice_id in command.invoice_ids:
            hold = place_hold(
                self.db,
                ctx,
                invoice_id,
                "promise_confirmation",
                f"Promise {row.id}; source {msg.source_state} {msg.id}",
            )
            self.db.add(
                PromiseInvoiceLink(
                    tenant_id=ctx.tenant_id,
                    promise_id=row.id,
                    invoice_id=invoice_id,
                    amount=allocations[invoice_id].model_dump(mode="json")
                    if invoice_id in allocations
                    else None,
                    hold_id=hold,
                )
            )
        if not command.invoice_ids:
            all_ids = list(
                self.db.scalars(
                    select(InvoiceSnapshot.id).where(
                        InvoiceSnapshot.tenant_id == ctx.tenant_id,
                        InvoiceSnapshot.customer_id == command.customer_id,
                    )
                )
            )
            hold_ids = [
                str(
                    place_hold(
                        self.db, ctx, i, "scope_review", f"Unresolved customer scope; promise {row.id}"
                    )
                )
                for i in all_ids
            ]
            self.db.add(
                CustomerScopeReview(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=customer.provider_connection_id,
                    customer_id=customer.id,
                    promise_id=row.id,
                    hold_ids=hold_ids,
                    expires_at=None,
                )
            )
        installments = command.installments
        if not installments and command.amount and command.due_date:
            from aco.features.promises.dto import PromiseInstallmentCommand

            installments = [
                PromiseInstallmentCommand(
                    due_date=command.due_date,
                    amount=command.amount,
                    allocations=[PromiseAllocation(invoice_id=k, amount=v) for k, v in allocations.items()],
                )
            ]
        for n, inst in enumerate(installments):
            try:
                inst_cutoff = end_of_date_cutoff(inst.due_date, command.timezone)
            except ValueError:
                inst_cutoff = None
            if inst_cutoff is None:
                continue
            parts = inst.allocations
            if not parts and len(command.invoice_ids) == 1:
                parts = [PromiseAllocation(invoice_id=command.invoice_ids[0], amount=inst.amount)]
            self.db.add(
                PromiseInstallment(
                    tenant_id=ctx.tenant_id,
                    promise_id=row.id,
                    sequence=n,
                    due_date=inst.due_date,
                    cutoff_at=inst_cutoff,
                    amount=inst.amount.model_dump(mode="json"),
                    allocations=[a.model_dump(mode="json") for a in parts],
                )
            )
        self._event(
            ctx,
            row,
            "promise.captured",
            {
                ("communication_id" if isinstance(msg, Communication) else "voice_evidence_id"): str(msg.id),
                "source_state": msg.source_state,
                "reasons": reasons,
                "replacement_lineage": [
                    {"id": str(prior.id), "version": prior.version, "status": prior.status}
                    for prior in lineage
                ],
            },
        )
        return self._view(row)

    def confirm_promise(self, ctx: Any, command: ConfirmPromise) -> PromiseView:
        require_human(self.db, ctx, Permission.RESOLVE)
        row = self._row(ctx, command.promise_id)
        if row.version != command.expected_version:
            raise HTTPException(409, "STALE_PROMISE")
        if row.status != "NEEDS_CONFIRMATION":
            raise HTTPException(409, "PROMISE_NOT_CONFIRMABLE")
        links = self._links(row)
        if isinstance(command.confirmation_evidence, VoiceOperationalEvidence) and (
            row.source_voice_evidence_id != command.confirmation_evidence.voice_evidence_id
        ):
            raise HTTPException(403, "VOICE_CONFIRMATION_SOURCE_MISMATCH")
        if row.source_voice_evidence_id and not isinstance(
            command.confirmation_evidence, VoiceOperationalEvidence
        ):
            raise HTTPException(403, "VOICE_CONFIRMATION_REVIEW_REQUIRED")
        validate_evidence(
            self.db,
            ctx.tenant_id,
            row.customer_id,
            command.confirmation_evidence,
            [link.invoice_id for link in links],
        )
        if set(row.confirmation_reasons) - {"sender_authority_requires_confirmation"}:
            raise HTTPException(409, "PROMISE_TERMS_UNRESOLVED")
        if row.replaces_promise_id:
            captured = RecordPromise.model_validate(row.command)
            lineage = self._replacement_lineage(ctx, captured)
            if not lineage or lineage[0].version != command.replacement_expected_version:
                raise HTTPException(409, "STALE_PRIOR_PROMISE")
            snapshots = self._captured_lineage(row)
            current = [
                PromisePredecessorView(id=prior.id, version=prior.version, status=cast(Any, prior.status))
                for prior in lineage
            ]
            if snapshots and snapshots != current:
                raise HTTPException(409, "STALE_PRIOR_PROMISE")
            if lineage[0].status == "NEEDS_CONFIRMATION":
                if command.confirmation_evidence != captured.evidence:
                    raise HTTPException(409, "PROMISE_CLARIFICATION_SOURCE_CHANGED")
                validate_evidence(
                    self.db,
                    ctx.tenant_id,
                    row.customer_id,
                    captured.evidence,
                    [link.invoice_id for link in links],
                )
                active = next((prior for prior in lineage if prior.status == "ACTIVE"), None)
                if active is not None and command.active_predecessor_expected_version != active.version:
                    raise HTTPException(409, "STALE_ACTIVE_PREDECESSOR")
            for old in lineage:
                if old.status == "ACTIVE":
                    self._active_compatibility(old, captured)
            # All captured rows and terms passed before any lifecycle/hold mutation.
            for old in lineage:
                old.status = "SUPERSEDED"
                old.superseded_by_id = row.id
                old.version += 1
                complete_review_task(self.db, ctx, old.review_task_id)
                for link in self._links(old):
                    release_hold(self.db, ctx, link.hold_id, f"Superseded by confirmed promise {row.id}")
                from aco.features.promises.scheduling import cancel_promise_checks

                cancel_promise_checks(self.db, ctx, old)
                self._release_scope_review(
                    ctx, old, f"Clarified by confirmed promise {row.id}: {command.reason}"
                )
                self._event(ctx, old, "promise.superseded", {"replacement_id": str(row.id)})
        row.status = "ACTIVE"
        row.confirmed_at = now()
        complete_review_task(self.db, ctx, row.review_task_id)
        row.confirmation_communication_id = (
            command.confirmation_evidence.communication_id
            if isinstance(command.confirmation_evidence, CommunicationEvidence)
            else None
        )
        row.confirmation_voice_evidence_id = (
            command.confirmation_evidence.voice_evidence_id
            if isinstance(command.confirmation_evidence, VoiceOperationalEvidence)
            else None
        )
        row.confirmation_reasons = []
        row.version += 1
        for link in links:
            release_hold(self.db, ctx, link.hold_id, f"Confirmed promise {row.id}")
            link.hold_id = place_hold(
                self.db,
                ctx,
                link.invoice_id,
                "active_promise",
                f"Confirmed promise {row.id}; source {row.source_state} "
                f"{row.source_communication_id or row.source_voice_evidence_id}",
            )
            link.version += 1
        for installment in self._installments(row):
            installment.status = "ACTIVE"
            installment.version += 1
        from aco.features.promises.scheduling import commitment_snapshot, schedule_promise_checks

        self._event(
            ctx,
            row,
            "promise.confirmed",
            {
                **command.model_dump(mode="json"),
                "commitment": commitment_snapshot(self.db, row),
            },
        )
        schedule_promise_checks(self.db, ctx, row.id, row.version)
        return self._view(row)

    def supersede_promise(
        self, ctx: Any, old_id: UUID, replacement: RecordPromise, expected_version: int
    ) -> PromiseView:
        require_human(self.db, ctx, Permission.TASK_WRITE)
        old = self._row(ctx, old_id)
        if old.version != expected_version:
            raise HTTPException(409, "STALE_PRIOR_PROMISE")
        if replacement.replaces_promise_id not in {None, old_id}:
            raise HTTPException(422, "PRIOR_PROMISE_MISMATCH")
        return self.record_promise(
            ctx,
            replacement.model_copy(
                update={"replaces_promise_id": old_id, "replacement_expected_version": expected_version}
            ),
        )

    def cancel_promise(self, ctx: Any, command: CancelPromise) -> PromiseView:
        require_human(self.db, ctx, Permission.RESOLVE)
        row = self._row(ctx, command.promise_id)
        if row.version != command.expected_version:
            raise HTTPException(409, "STALE_PROMISE")
        if row.status not in {"NEEDS_CONFIRMATION", "ACTIVE"}:
            raise HTTPException(409, "PROMISE_NOT_CANCELLABLE")
        if isinstance(command.evidence, VoiceOperationalEvidence):
            if row.source_voice_evidence_id != command.evidence.voice_evidence_id:
                raise HTTPException(403, "VOICE_CANCELLATION_SOURCE_MISMATCH")
        elif row.source_voice_evidence_id:
            raise HTTPException(403, "VOICE_CANCELLATION_REVIEW_REQUIRED")
        validate_evidence(
            self.db,
            ctx.tenant_id,
            row.customer_id,
            command.evidence,
            [link.invoice_id for link in self._links(row)] or [UUID(i) for i in row.candidate_invoice_ids],
        )
        for link in self._links(row):
            release_hold(self.db, ctx, link.hold_id, f"Authorized cancellation {row.id}: {command.reason}")
        block = self.db.scalar(
            select(CustomerScopeReview)
            .where(CustomerScopeReview.tenant_id == ctx.tenant_id, CustomerScopeReview.promise_id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if block:
            for hold_id in block.hold_ids:
                release_hold(
                    self.db, ctx, UUID(hold_id), f"Authorized scope review disposition: {command.reason}"
                )
            block.released_at = now()
            block.release_evidence = command.reason
            block.version += 1
        from aco.features.promises.scheduling import cancel_promise_checks

        cancel_promise_checks(self.db, ctx, row)
        row.status = "CANCELLED"
        row.version += 1
        complete_review_task(self.db, ctx, row.review_task_id)
        self._event(ctx, row, "promise.cancelled", command.model_dump(mode="json"))
        return self._view(row)

    def prepare_promise_checks(self, ctx: Any, promise_id: UUID) -> builtins.list[PromiseCheckPlan]:
        require_human(self.db, ctx)
        row = self._row(ctx, promise_id)
        if row.status not in {"ACTIVE", "FULFILLED", "MISSED"}:
            raise HTTPException(409, "PROMISE_NOT_ACTIVE")
        event = self.db.scalar(
            select(PromiseEvent)
            .where(
                PromiseEvent.tenant_id == ctx.tenant_id,
                PromiseEvent.promise_id == row.id,
                PromiseEvent.event == "promise.confirmed",
            )
            .order_by(PromiseEvent.created_at.desc())
        )
        if event is None:
            raise HTTPException(409, "PROMISE_EVENT_UNAVAILABLE")
        from aco.features.actions.models import ScheduledAction
        from aco.features.promises.scheduling import scheduling_status

        return [
            PromiseCheckPlan(
                promise_id=row.id,
                promise_version=row.version,
                event_id=event.id,
                due_at=i.cutoff_at,
                invoice_ids=[UUID(str(a["invoice_id"])) for a in i.allocations],
                installment_id=i.id,
                status=cast(Any, scheduling_status(self.db, row)),
                action_ids=list(
                    self.db.scalars(
                        select(ScheduledAction.id)
                        .where(
                            ScheduledAction.tenant_id == ctx.tenant_id,
                            ScheduledAction.kind == "promise_check",
                            ScheduledAction.promise_event_id == event.id,
                            ScheduledAction.promise_installment_id == i.id,
                        )
                        .order_by(ScheduledAction.id)
                    )
                ),
            )
            for i in self._installments(row)
        ]
