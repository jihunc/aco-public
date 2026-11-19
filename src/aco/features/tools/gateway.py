"""Typed internal domain command executor. No external outreach or financial write tools."""

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.events import finish_step, start_step
from aco.features.agent.models import AgentStep
from aco.features.cases.dto import CommunicationEvidence, UpsertCase
from aco.features.cases.task_binding import TaskCaseBinding, current_task_case
from aco.features.communications.association_dto import LinkCommunication
from aco.features.identity.models import AuditEvent, ProviderConnection, Tenant, now
from aco.features.promises.dto import RecordPromise
from aco.features.review.models import CollectionTask
from aco.features.review.queue import ReviewQueue
from aco.features.tools.authorization import AgentActionContext, require_agent_action
from aco.features.tools.contact_stop import (
    RequestCollectionHold,
    request_collection_hold,
    validate_contact_stop,
)
from aco.features.tools.internal_followup import (
    RequestHumanReview,
    ScheduleFollowup,
    apply_internal_work,
    internal_work_kind,
    validate_internal_work,
)
from aco.features.tools.read_names import canonical_read_name
from aco.features.tools.schemas import CaseFact, CommunicationFact, InvoiceFact, PromiseFact, ReadEvidence
from aco.features.tools.task_update import UpdateCollectionTask, update_collection_task, validate_task_update
from aco.features.voice.agent_request import (
    RequestVoiceCall,
    prepare_agent_voice_review,
    validate_voice_request,
)

if TYPE_CHECKING:
    from aco.features.voice.dto import (
        VoiceCaseCommand,
        VoiceCaseFactCommand,
        VoiceHoldFactCommand,
        VoicePromiseCommand,
        VoicePromiseFactCommand,
        VoiceTaskCommand,
    )
    from aco.features.voice.runtime import VoiceRuntime
    from aco.features.voice.tools import VoiceActionContext


class CreateTaskCommand(StrictModel):
    case_binding: TaskCaseBinding | None = Field(default=None, exclude_if=lambda value: value is None)
    invoice_id: UUID
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=4000)
    evidence_step_ids: list[UUID] = Field(min_length=2, max_length=8)


InternalCommand = (
    CreateTaskCommand
    | UpdateCollectionTask
    | UpsertCase
    | RecordPromise
    | RequestCollectionHold
    | RequestHumanReview
    | ScheduleFollowup
    | RequestVoiceCall
    | LinkCommunication
)


class ExecutedAction(StrictModel):
    action_id: UUID
    step_id: UUID
    changed_ids: list[UUID]
    status: str
    approval_id: UUID | None = None
    voice_session_id: UUID | None = None


def create_task(db: Session, ctx: AgentActionContext, command: CreateTaskCommand) -> list[UUID]:
    action = require_agent_action(db, ctx, "create_collection_task", command)
    if action.invoice_id != command.invoice_id:
        raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
    if command.case_binding is not None:
        current_task_case(
            db,
            current_run(db, ctx.authority),
            command.invoice_id,
            command.case_binding,
            command.evidence_step_ids,
        )
    queue = ReviewQueue(db)
    queue_state = queue.state(ctx.tenant_id)
    rows = queue.rows(ctx.tenant_id)
    task = CollectionTask(
        tenant_id=ctx.tenant_id,
        source_action_id=action.id,
        source_event_id=None,
        source_agent_run_id=action.agent_run_id,
        invoice_id=action.invoice_id,
        creator_id=ctx.actor_id,
        title=command.title,
        notes=command.notes,
        command=command.model_dump(mode="json"),
        priority_rank=max((row.priority_rank for row in rows), default=Decimal(0)) + 1,
    )
    db.add(task)
    queue_state.version += 1
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="task.created_by_agent",
            target_id=task.id,
            details={"run_id": str(action.agent_run_id), "action_id": str(action.id)},
        )
    )
    return [task.id]


def execute_task(
    db: Session, authority: RunAuthority, command: CreateTaskCommand, index: int
) -> ExecutedAction:
    return execute_internal(db, authority, command, index, command.invoice_id, command.evidence_step_ids)


def execute_internal(
    db: Session,
    authority: RunAuthority,
    command: InternalCommand,
    index: int,
    invoice_id: UUID,
    evidence_step_ids: list[UUID],
    *,
    require_approval: bool = False,
    voice_runtime: "VoiceRuntime | None" = None,
) -> ExecutedAction:
    run = current_run(db, authority)
    if invoice_id not in selected_invoices(db, run):
        raise HTTPException(403, "INVOICE_OUTSIDE_RUN_SCOPE")
    kind = (
        "create_collection_task"
        if isinstance(command, CreateTaskCommand)
        else (
            "upsert_collection_case"
            if isinstance(command, UpsertCase)
            else "request_collection_hold"
            if isinstance(command, RequestCollectionHold)
            else "record_payment_promise"
        )
    )
    if isinstance(command, UpdateCollectionTask):
        kind = "update_collection_task"
    if isinstance(command, RequestHumanReview):
        kind = internal_work_kind(command)
    if isinstance(command, RequestVoiceCall):
        kind = "request_voice_call"
    if isinstance(command, LinkCommunication):
        kind = "link_communication"
    material = {"kind": kind, "payload": command.model_dump(mode="json")}
    key = f"agent:{run.id}:proposal:{index}"
    old = db.scalar(
        select(ScheduledAction).where(
            ScheduledAction.tenant_id == run.tenant_id, ScheduledAction.operation_key == key
        )
    )
    if old:
        if old.command != material:
            raise HTTPException(409, "ACTION_REPLAY_CONFLICT")
        if require_approval and isinstance(command, CreateTaskCommand):
            from aco.features.review.models import ApprovalRequest
            from aco.features.review.modifications import replacement

            request = db.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.tenant_id == run.tenant_id, ApprovalRequest.action_id == old.id
                )
            )
            seen: set[UUID] = set()
            while request is not None and (successor := replacement(db, request)) is not None:
                if request.id in seen or request.status != "superseded" or old.status != "cancelled":
                    raise HTTPException(409, "INVALID_APPROVAL_LINEAGE")
                seen.add(request.id)
                next_action = db.get(ScheduledAction, successor.action_id, populate_existing=True)
                if (
                    next_action is None
                    or next_action.tenant_id != run.tenant_id
                    or next_action.agent_run_id != run.id
                ):
                    raise HTTPException(403, "APPROVAL_REPLACEMENT_OUTSIDE_RUN")
                old, request = next_action, successor
            if seen:
                command = CreateTaskCommand.model_validate_json(
                    __import__("json").dumps(old.command["payload"])
                )
                material = {"kind": kind, "payload": command.model_dump(mode="json")}
                if (
                    old.command != material
                    or command.invoice_id != invoice_id
                    or command.evidence_step_ids != evidence_step_ids
                ):
                    raise HTTPException(403, "APPROVAL_REPLACEMENT_SCOPE_CHANGED")
        if old.status == "succeeded" or (
            require_approval and old.status in {"cancelled", "failed"} and old.result
        ):
            return ExecutedAction.model_validate_json(__import__("json").dumps(old.result))
        if not require_approval or old.status != "awaiting_approval":
            raise HTTPException(409, "ACTION_REPLAY_CONFLICT")
    if isinstance(command, RequestCollectionHold):
        if command.invoice_id != invoice_id:
            raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
        validate_contact_stop(db, authority, command)
    elif isinstance(command, UpdateCollectionTask):
        if command.invoice_id != invoice_id or command.evidence_step_ids != evidence_step_ids:
            raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
        validate_task_update(db, authority, command)
    elif isinstance(command, RequestHumanReview):
        if command.invoice_id != invoice_id or command.evidence_step_ids != evidence_step_ids:
            raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
        validate_internal_work(db, authority, command)
    elif isinstance(command, RequestVoiceCall):
        if command.invoice_id != invoice_id:
            raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
        validate_voice_request(db, authority, command)
    elif not isinstance(command, CreateTaskCommand):
        validate_domain_scope(db, authority, command, invoice_id)
    evidence = list(
        db.scalars(
            select(AgentStep).where(
                AgentStep.tenant_id == run.tenant_id,
                AgentStep.run_id == run.id,
                AgentStep.invoice_id == invoice_id,
                AgentStep.id.in_(evidence_step_ids),
                AgentStep.status == "completed",
                AgentStep.kind == "read",
            )
        )
    )
    if len(evidence) != len(set(evidence_step_ids)) or not {
        "get_invoice",
        "get_verified_payments",
    }.issubset({canonical_read_name(r.tool_name) for r in evidence}):
        raise HTTPException(403, "ACTION_EVIDENCE_DENIED")
    if isinstance(command, CreateTaskCommand) and command.case_binding is not None:
        current_task_case(db, run, invoice_id, command.case_binding, evidence_step_ids)
    invoice = db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
    if invoice is None or not invoice.verified or invoice.tenant_id != run.tenant_id:
        raise HTTPException(409, "SOURCE_EVIDENCE_UNAVAILABLE")
    stamp = next(r.output for r in evidence if r.tool_name == "get_invoice")
    source = ReadEvidence.model_validate_json(__import__("json").dumps(stamp))
    if (
        not isinstance(source.records[0], InvoiceFact)
        or source.records[0].source.source_revision != invoice.source_revision
    ):
        raise HTTPException(409, "SOURCE_CHANGED_REINVESTIGATE")
    connection = db.get(ProviderConnection, invoice.provider_connection_id)
    tenant = db.get(Tenant, run.tenant_id)
    assert tenant is not None and connection is not None
    if old is None:
        step = start_step(
            db,
            run,
            kind="action",
            tool_name=kind,
            invoice_id=invoice.id,
            arguments=command.model_dump(mode="json"),
        )
        action = ScheduledAction(
            tenant_id=run.tenant_id,
            provider_connection_id=invoice.provider_connection_id,
            invoice_id=invoice.id,
            source_revision=invoice.source_revision,
            source_event_id=None,
            policy_version_id=None,
            policy_generation=0,
            initiator_id=run.initiator_id,
            membership_version=run.membership_version,
            tenant_version=run.tenant_generation,
            outbound_paused=tenant.outbound_paused,
            automation_enabled=tenant.automation_enabled,
            worker_actor_id=run.worker_actor_id,
            actor_version=run.actor_version,
            connection_version=connection.version,
            correlation_id=run.correlation_id,
            kind="agent_internal",
            operation_key=key,
            command=material,
            due_at=now(),
            status="executing",
            attempts=1,
            lease_token=run.lease_token,
            lease_until=run.lease_until,
            job_id=run.job_id,
            agent_run_id=run.id,
            agent_step_id=step.id,
        )
        db.add(action)
        db.flush()
    else:
        action = old
        existing_step = db.get(AgentStep, action.agent_step_id)
        if existing_step is None or existing_step.status != "started":
            raise HTTPException(409, "APPROVAL_STEP_NOT_CURRENT")
        step = existing_step
    approval_id = None
    if require_approval:
        if not isinstance(command, CreateTaskCommand):
            raise HTTPException(403, "UNSUPPORTED_APPROVAL_EFFECT")
        from aco.features.review.agent_approvals import task_approval

        approval_id, approval_state = task_approval(db, authority, action, command)
        if approval_state != "approved":
            result = ExecutedAction(
                action_id=action.id,
                step_id=step.id,
                changed_ids=[],
                status=approval_state,
                approval_id=approval_id,
            )
            if approval_state != "pending":
                action.status = "cancelled" if approval_state == "denied" else "failed"
                action.result = result.model_dump(mode="json")
                finish_step(db, run, step, error="APPROVAL_" + approval_state.upper())
            return result
    action.status = "executing"
    action.attempts = 1
    action.job_id, action.lease_token, action.lease_until = run.job_id, run.lease_token, run.lease_until
    execution = ActionExecution(
        tenant_id=run.tenant_id, action_id=action.id, attempt=1, lease_token=uuid4(), started_at=now()
    )
    db.add(execution)
    ctx = AgentActionContext(authority, action.id, step.id)
    voice_session_id = None
    if isinstance(command, RequestVoiceCall):
        prepared = prepare_agent_voice_review(db, ctx, command, voice_runtime)
        voice_session_id, approval_id = prepared.id, prepared.approval_id
        changes = []
    elif isinstance(command, UpdateCollectionTask):
        changes = update_collection_task(db, ctx, command)
    elif isinstance(command, CreateTaskCommand):
        changes = create_task(db, ctx, command)
    elif isinstance(command, RequestHumanReview):
        changes = apply_internal_work(db, ctx, command)
    elif isinstance(command, RequestCollectionHold):
        changes = request_collection_hold(db, ctx, command)
    elif isinstance(command, LinkCommunication):
        from aco.features.communications.association import link_communication

        changes = [link_communication(db, ctx, command)]
    elif isinstance(command, UpsertCase):
        from aco.features.cases.service import CaseService

        mutation = CaseService(db).upsert_case(ctx, command)
        changes = [mutation.case.id]
        task_id = mutation.case.task_id
        if mutation.created:
            task = db.get(CollectionTask, task_id)
            assert task is not None
            task.source_action_id, task.source_agent_run_id = action.id, run.id
    else:
        from aco.features.promises.service import PromiseService

        promise = PromiseService(db).record_promise(ctx, command)
        changes = [promise.id]
        if promise.review_task_id:
            task = db.get(CollectionTask, promise.review_task_id)
            assert task is not None
            if task.source_action_id is None:
                task.source_action_id, task.source_agent_run_id = action.id, run.id

    result = ExecutedAction(
        action_id=action.id,
        step_id=step.id,
        changed_ids=changes,
        status="succeeded",
        approval_id=approval_id,
        voice_session_id=voice_session_id,
    )
    action.status = "succeeded"
    action.result = result.model_dump(mode="json")
    execution.status, execution.result, execution.finished_at = "succeeded", action.result, now()
    finish_step(db, run, step, output=action.result)
    db.add(
        AuditEvent(
            tenant_id=run.tenant_id,
            actor_id=run.worker_actor_id,
            correlation_id=run.correlation_id,
            action="agent.action_succeeded",
            target_id=action.id,
            details={"kind": kind, "run_id": str(run.id)},
        )
    )
    return result


def validate_domain_scope(
    db: Session,
    authority: RunAuthority,
    command: UpsertCase | RecordPromise | LinkCommunication,
    primary: UUID,
) -> None:
    if not isinstance(command.evidence, CommunicationEvidence):
        raise HTTPException(403, "AGENT_SOURCE_KIND_DENIED")
    run = current_run(db, authority)
    selected = set(selected_invoices(db, run))
    affected = set(command.invoice_ids)
    if isinstance(command, RecordPromise):
        affected.update(command.candidate_invoice_ids)
        affected.update(a.invoice_id for a in command.allocations)
        affected.update(a.invoice_id for i in command.installments for a in i.allocations)
        if not command.invoice_ids:
            affected.update(
                db.scalars(
                    select(InvoiceSnapshot.id).where(
                        InvoiceSnapshot.tenant_id == run.tenant_id,
                        InvoiceSnapshot.customer_id == command.customer_id,
                    )
                )
            )
    if primary not in affected or not affected.issubset(selected):
        raise HTTPException(403, "COMMAND_INVOICE_OUTSIDE_RUN_SCOPE")
    reads = list(
        db.scalars(
            select(AgentStep).where(
                AgentStep.tenant_id == run.tenant_id,
                AgentStep.run_id == run.id,
                AgentStep.kind == "read",
                AgentStep.status == "completed",
            )
        )
    )
    source_seen = False
    reused_step: AgentStep | None = None
    for step in reads:
        if step.tool_name != "get_communication_history":
            continue
        source = ReadEvidence.model_validate_json(__import__("json").dumps(step.output))
        for fact in source.records:
            if (
                isinstance(fact, CommunicationFact)
                and fact.communication_id == command.evidence.communication_id
            ):
                start = command.evidence.offset - fact.offset
                exact_span = (
                    start >= 0
                    and fact.excerpt[start : start + len(command.evidence.excerpt)]
                    == command.evidence.excerpt
                )
                if isinstance(command, LinkCommunication) and (
                    fact.communication_version != command.expected_version
                    or fact.source_sha256 != command.source_sha256
                ):
                    continue
                if exact_span and step.invoice_id == primary:
                    source_seen = True
                elif exact_span:
                    from aco.features.tools.domain_source import can_reuse_original

                    primary_invoice = db.get(InvoiceSnapshot, primary, populate_existing=True)
                    if primary_invoice is not None and can_reuse_original(
                        db, run, step, primary_invoice, command.evidence
                    ):
                        source_seen = True
                        reused_step = step
    if not source_seen:
        raise HTTPException(403, "COMMAND_SOURCE_NOT_READ")
    # Every affected snapshot must still match the source read by this run.
    for invoice_id in affected:
        invoice = db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if invoice is None or invoice.customer_id != command.customer_id:
            raise HTTPException(403, "COMMAND_CUSTOMER_CONFLICT")
        match = next((s for s in reads if s.invoice_id == invoice_id and s.tool_name == "get_invoice"), None)
        if match is None:
            raise HTTPException(403, "COMMAND_INVOICE_NOT_READ")
        facts = ReadEvidence.model_validate_json(__import__("json").dumps(match.output)).records
        if (
            not facts
            or not isinstance(facts[0], InvoiceFact)
            or facts[0].source.source_revision != invoice.source_revision
        ):
            raise HTTPException(409, "SOURCE_CHANGED_REINVESTIGATE")

    if isinstance(command, LinkCommunication):
        return
    target = command.matched_case_id if isinstance(command, UpsertCase) else command.replaces_promise_id
    if isinstance(command, UpsertCase):
        from aco.features.cases.identity import issue_identity
        from aco.features.cases.models import CollectionCase

        implicit = db.scalar(
            select(CollectionCase)
            .where(
                CollectionCase.tenant_id == run.tenant_id,
                CollectionCase.issue_identity
                == issue_identity(command.customer_id, command.issue_type, command.claim_key),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if implicit is not None:
            if target is not None and target != implicit.id:
                raise HTTPException(409, "COMMAND_CASE_IDENTITY_CONFLICT")
            target = implicit.id
    if target is not None:
        observed = False
        for step in reads:
            if canonical_read_name(step.tool_name) not in {"find_existing_cases", "get_payment_promises"}:
                continue
            facts = ReadEvidence.model_validate_json(__import__("json").dumps(step.output)).records
            observed = observed or any(
                (isinstance(f, CaseFact) and isinstance(command, UpsertCase) and f.case_id == target)
                or (
                    isinstance(f, PromiseFact)
                    and isinstance(command, RecordPromise)
                    and f.promise_id == target
                )
                for f in facts
            )
        if not observed:
            raise HTTPException(403, "COMMAND_TARGET_NOT_READ")
        if isinstance(command, UpsertCase):
            from aco.features.cases.models import CaseInvoiceLink

            existing_ids = set(
                db.scalars(
                    select(CaseInvoiceLink.invoice_id).where(
                        CaseInvoiceLink.tenant_id == run.tenant_id, CaseInvoiceLink.case_id == target
                    )
                )
            )
        else:
            from aco.features.promises.models import PromiseInvoiceLink

            existing_ids = set(
                db.scalars(
                    select(PromiseInvoiceLink.invoice_id).where(
                        PromiseInvoiceLink.tenant_id == run.tenant_id, PromiseInvoiceLink.promise_id == target
                    )
                )
            )
        if not existing_ids.issubset(selected):
            raise HTTPException(403, "COMMAND_TARGET_OUTSIDE_RUN_SCOPE")

    if reused_step is not None:
        db.add(
            AuditEvent(
                tenant_id=run.tenant_id,
                actor_id=run.worker_actor_id,
                correlation_id=run.correlation_id,
                action="agent.original_source_reused",
                target_id=command.evidence.communication_id,
                details={
                    "run_id": str(run.id),
                    "read_step_id": str(reused_step.id),
                    "read_invoice_id": str(reused_step.invoice_id),
                    "command_invoice_id": str(primary),
                },
            )
        )


def execute_voice_task(db: Session, ctx: "VoiceActionContext", command: "VoiceTaskCommand") -> UUID:
    """Only the separately validated current voice invocation can create this internal task."""
    from aco.features.voice.tools import require_voice_action

    action = require_voice_action(db, ctx, command)
    queue = ReviewQueue(db)
    state = queue.state(ctx.call.tenant_id)
    rows = queue.rows(ctx.call.tenant_id)
    task = CollectionTask(
        tenant_id=ctx.call.tenant_id,
        source_action_id=action.id,
        source_agent_run_id=None,
        source_voice_session_id=ctx.call.session_id,
        source_event_id=None,
        invoice_id=command.invoice_id,
        creator_id=ctx.call.actor_id,
        title=command.title,
        notes="Synthetic offline voice follow-up; no transcript captured.",
        command=command.model_dump(mode="json"),
        priority_rank=max((r.priority_rank for r in rows), default=Decimal(0)) + 1,
    )
    db.add(task)
    state.version += 1
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=ctx.call.tenant_id,
            actor_id=ctx.call.actor_id,
            correlation_id=ctx.call.correlation_id,
            action="task.created_by_voice",
            target_id=task.id,
            details={
                "session_id": str(ctx.call.session_id),
                "invocation_id": str(ctx.invocation_id),
                "action_id": str(action.id),
            },
        )
    )
    return task.id


def execute_voice_domain(
    db: Session,
    ctx: "VoiceActionContext",
    command: (
        "VoiceCaseCommand | VoicePromiseCommand | VoiceCaseFactCommand"
        " | VoicePromiseFactCommand | VoiceHoldFactCommand"
    ),
) -> dict[str, object]:
    """Voice has its own exact invocation grant; no generic service/human promotion."""
    from aco.features.cases.service import CaseService
    from aco.features.promises.service import PromiseService
    from aco.features.voice.dto import (
        VoiceCaseCommand,
        VoiceCaseFactCommand,
        VoiceHoldFactCommand,
        VoicePromiseCommand,
        VoicePromiseFactCommand,
    )
    from aco.features.voice.evidence import persist_fact_source

    if isinstance(command, (VoiceCaseFactCommand, VoicePromiseFactCommand, VoiceHoldFactCommand)):
        derived = persist_fact_source(db, ctx, command)
        if isinstance(derived, UpsertCase):
            saved_case = CaseService(db).upsert_case(ctx, derived).case
            return {"status": "succeeded", "record_kind": "case", "changed_ids": [str(saved_case.id)]}
        if isinstance(derived, RecordPromise):
            saved_promise_fact = PromiseService(db).record_promise(ctx, derived)
            return {
                "status": "succeeded",
                "record_kind": "promise",
                "changed_ids": [str(saved_promise_fact.id)],
            }
        if isinstance(derived, RequestCollectionHold):
            held = request_collection_hold(db, ctx, derived)
            return {"status": "succeeded", "record_kind": "hold", "changed_ids": list(map(str, held))}
        raise HTTPException(403, "VOICE_DOMAIN_COMMAND_DENIED")
    if isinstance(command, VoiceCaseCommand):
        saved = CaseService(db).upsert_case(ctx, command.case_command).case
        return {"status": "succeeded", "record_kind": "case", "changed_ids": [str(saved.id)]}
    if isinstance(command, VoicePromiseCommand):
        saved_promise = PromiseService(db).record_promise(ctx, command.promise_command)
        return {"status": "succeeded", "record_kind": "promise", "changed_ids": [str(saved_promise.id)]}
    raise HTTPException(403, "VOICE_DOMAIN_COMMAND_DENIED")
