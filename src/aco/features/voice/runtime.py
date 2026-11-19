"""Offline voice composition: durable intent, independent approval and atomic callback effects."""

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.clock import Clock, SystemClock
from aco.core.context import TenantContext
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.identity.models import AuditEvent, Tenant, now
from aco.features.identity.service import Permission
from aco.features.review.approvals import HumanApprovalService, digest
from aco.features.review.models import ApprovalRequest
from aco.features.usage.models import UsageAttempt
from aco.features.voice.dto import (
    PrepareVoice,
    SetupVoice,
    VoiceApprovalProposal,
    VoiceCaseCommand,
    VoiceCaseFactCommand,
    VoiceHoldFactCommand,
    VoiceJoinView,
    VoicePage,
    VoicePromiseCommand,
    VoicePromiseFactCommand,
    VoiceSessionView,
    VoiceStopOutcome,
    VoiceTaskCommand,
)
from aco.features.voice.models import VoiceEventRecord, VoiceInvocation, VoiceSession
from aco.features.voice.service import VoiceService, source_links, voice_scope
from aco.features.voice.usage import record_voice_usage
from aco.integrations.ports.voice import (
    CallContext,
    CallObjective,
    ConsentMode,
    ProviderCallRef,
    VerifiedVoiceWebhook,
    VoiceEvent,
    VoiceFailure,
)
from aco.integrations.ports.voice_artifacts import VoiceArtifactPort
from aco.integrations.vapi.adapter import VapiVoiceAdapter
from aco.integrations.vapi.settings import VapiBinding


class VoiceRuntime:
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"] = "MOCKED_HTTP"
    capture_port: VoiceArtifactPort | None = None

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        resolver: Callable[[UUID, UUID], VapiBinding],
        *,
        capability_secret: SecretStr,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        enabled: bool = False,
        clock: Clock | None = None,
        setup_enabled: bool = False,
        environment: str = "local",
    ):
        self.sessions, self.resolver, self.transport_factory = sessions, resolver, transport_factory
        self.enabled = enabled
        self.setup_enabled, self.environment = setup_enabled, environment
        self.clock = clock or SystemClock()
        self.signer = URLSafeSerializer(capability_secret.get_secret_value(), salt="aco-voice-call-v1")

    def binding(self, tenant_id: UUID, connection_id: UUID) -> VapiBinding:
        if not self.enabled or self.transport_factory is None:
            raise HTTPException(403, "VOICE_DISABLED")
        try:
            binding = self.resolver(tenant_id, connection_id)
        except LookupError as exc:
            raise HTTPException(403, "VOICE_BINDING_UNAVAILABLE") from exc
        if (
            binding.tenant_id != tenant_id
            or binding.connection_id != connection_id
            or binding.evidence_mode != "MOCKED_HTTP"
        ):
            raise HTTPException(403, "VOICE_LIVE_NOT_AUTHORIZED")
        return binding

    def connection_available(self, db: Session, ctx: TenantContext, connection_id: UUID) -> VapiBinding:
        return self.binding(ctx.tenant_id, connection_id)

    def start_available(self, db: Session, ctx: TenantContext, row: VoiceSession) -> None:
        pass

    def stop_outcome(self, db: Session, row: VoiceSession) -> VoiceStopOutcome:
        return "NOT_REQUESTED"

    async def setup(self, ctx: TenantContext, command: SetupVoice) -> VoicePage:
        from aco.features.voice.setup import prepare_voice_connection

        return await prepare_voice_connection(self, ctx, command)

    def setup_authorized(self, db: Session, ctx: TenantContext) -> None:
        from aco.features.voice.setup import setup_authorized

        setup_authorized(db, self, ctx)

    def preparation_binding(
        self, db: Session, command: PrepareVoice, initiator_id: UUID
    ) -> tuple[UUID | None, datetime]:
        return None, self.clock.now() + timedelta(minutes=5)

    def approval_metadata(self, db: Session, row: VoiceSession) -> dict[str, object]:
        return {}

    def before_start(self, db: Session, ctx: TenantContext, row: VoiceSession) -> None:
        pass

    def control_permissions(self, db: Session, ctx: TenantContext, row: VoiceSession) -> dict[str, object]:
        return {}

    async def join(self, ctx: TenantContext, key: UUID, expected_version: int) -> VoiceJoinView:
        raise HTTPException(403, "VOICE_BROWSER_JOIN_UNAVAILABLE")

    async def end(self, ctx: TenantContext, key: UUID, expected_version: int) -> VoiceSessionView:
        raise HTTPException(403, "VOICE_SAFETY_STOP_UNAVAILABLE")

    def start_operation(self) -> str:
        return "start_browser"

    async def retain_call(self, key: UUID, result: ProviderCallRef) -> None:
        pass

    def callback_expiry_allowed(self, row: VoiceSession) -> bool:
        return False

    def callback_current(self, db: Session, ctx: CallContext, verified: VerifiedVoiceWebhook) -> VoiceSession:
        return self.current(db, ctx, allow_own_stop_completion=True)

    @staticmethod
    def binding_hash(binding: VapiBinding) -> str:
        return digest(
            {
                "tenant_id": str(binding.tenant_id),
                "connection_id": str(binding.connection_id),
                "assistant_id": str(binding.assistant_id),
                "credential_revision": binding.credential_revision,
                "server_credential_id": str(binding.server_credential_id),
                "api_key_hash": hashlib.sha256(binding.api_key.get_secret_value().encode()).hexdigest(),
                "webhook_hash": hashlib.sha256(
                    binding.webhook_secret.get_secret_value().encode()
                ).hexdigest(),
                "mode": binding.evidence_mode,
                **(
                    {
                        "max_duration_seconds": binding.max_duration_seconds,
                        "control_hosts": list(binding.control_hosts),
                        "control_key_hash": hashlib.sha256(
                            binding.control_api_key.get_secret_value().encode()
                        ).hexdigest()
                        if binding.control_api_key
                        else None,
                    }
                    if binding.evidence_mode == "LIVE"
                    else {}
                ),
            }
        )

    def context(self, row: VoiceSession) -> CallContext:
        token = self.signer.dumps(
            {
                "tenant_id": str(row.tenant_id),
                "session_id": str(row.id),
                "expires_at": int(row.expires_at.timestamp()),
                "binding_hash": row.binding_hash,
            }
        )
        return CallContext(
            tenant_id=row.tenant_id,
            connection_id=row.connection_id,
            session_id=row.id,
            actor_id=row.actor_id,
            correlation_id=row.correlation_id,
            expires_at=row.expires_at,
            provider_call_id=row.provider_call_id,
            callback_capability=SecretStr(token),
        )

    def current(
        self,
        db: Session,
        ctx: CallContext,
        *,
        allow_start: bool = False,
        allow_own_stop_completion: bool = False,
    ) -> VoiceSession:
        db.scalar(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update(key_share=True))
        row = db.scalar(
            select(VoiceSession)
            .where(VoiceSession.id == ctx.session_id, VoiceSession.tenant_id == ctx.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(403, "VOICE_SESSION_SCOPE_DENIED")
        binding = self.binding(row.tenant_id, row.connection_id)
        if (
            ctx.actor_id != row.actor_id
            or ctx.connection_id != row.connection_id
            or ctx.correlation_id != row.correlation_id
            or ctx.expires_at != row.expires_at
            or ctx.provider_call_id != row.provider_call_id
            or self.binding_hash(binding) != row.binding_hash
            or self.clock.now() >= row.expires_at
            or row.state not in ({"starting"} if allow_start else {"active", "connected", "completed"})
        ):
            raise HTTPException(403, "VOICE_CURRENT_AUTHORITY_DENIED")
        approval = db.get(ApprovalRequest, row.approval_id, with_for_update=True, populate_existing=True)
        if approval is None or approval.status != "approved" or approval.consumed_at is None:
            raise HTTPException(403, "VOICE_APPROVAL_REQUIRED")
        _, action, _, material, reasons = voice_scope(
            db, approval, allow_own_stop_completion=allow_own_stop_completion, at=self.clock.now()
        )
        reasons += HumanApprovalService(db, at=self.clock.now())._grant_reasons(approval, material, action)
        if reasons:
            raise HTTPException(403, reasons[0])
        return row

    async def authorize(self, ctx: CallContext, operation: str, material: object) -> None:
        async with self.sessions.begin() as db:

            def check(store: Session) -> None:
                row = self.current(
                    store,
                    ctx,
                    allow_start=operation == "start_call",
                    allow_own_stop_completion=operation == "verify_webhook",
                )
                if operation == "start_call":
                    command = PrepareVoice.model_validate_json(json.dumps(row.command))
                    if not isinstance(material, CallObjective) or material.invoice_ids != tuple(
                        command.invoice_ids
                    ):
                        raise HTTPException(403, "VOICE_START_SCOPE_DENIED")
                elif operation not in {"get_call", "verify_webhook"}:
                    raise HTTPException(403, "VOICE_OPERATION_DENIED")

            await db.run_sync(check)

    def port(self, tenant: UUID, connection: UUID) -> VapiVoiceAdapter:
        binding = self.binding(tenant, connection)
        assert self.transport_factory is not None
        return VapiVoiceAdapter(
            binding,
            authority=self.authorize,
            enabled=self.enabled,
            transport=self.transport_factory(),
            clock=self.clock,
        )

    async def start(self, ctx: TenantContext, key: UUID, expected_version: int) -> VoiceSessionView:
        async with self.sessions.begin() as db:

            def intent(store: Session) -> tuple[CallContext, CallObjective] | VoiceSessionView:
                service = VoiceService(store, self)
                service.human(ctx, Permission.INVESTIGATE)
                row = store.scalar(
                    select(VoiceSession)
                    .where(VoiceSession.id == key, VoiceSession.tenant_id == ctx.tenant_id)
                    .with_for_update()
                )
                if row is None:
                    raise HTTPException(404, "VOICE_SESSION_NOT_FOUND")
                if row.state in {"active", "connected", "completed"}:
                    self.current(store, self.context(row))
                    return service.view(row)
                if row.version != expected_version or row.state != "awaiting_approval":
                    raise HTTPException(409, "VOICE_START_NOT_REPLAYABLE")
                binding = self.binding(row.tenant_id, row.connection_id)
                if self.binding_hash(binding) != row.binding_hash:
                    raise HTTPException(403, "VOICE_CONFIGURATION_CHANGED")
                approval = store.get(ApprovalRequest, row.approval_id, with_for_update=True)
                assert approval
                _, action, _, material, reasons = voice_scope(store, approval, at=self.clock.now())
                reasons += HumanApprovalService(store, at=self.clock.now())._grant_reasons(
                    approval, material, action
                )
                if reasons or approval.status != "approved" or approval.consumed_at:
                    raise HTTPException(403, reasons[0] if reasons else "VOICE_APPROVAL_REQUIRED")
                self.before_start(store, ctx, row)
                approval.consumed_at = now()
                row.state = "starting"
                row.version += 1
                attempt = UsageAttempt(
                    tenant_id=row.tenant_id,
                    provider_connection_id=row.connection_id,
                    actor_id=row.actor_id,
                    correlation_id=row.correlation_id,
                    operation=self.start_operation(),
                    outcome="PENDING",
                )
                store.add(attempt)
                store.flush()
                row.usage_attempt_id = attempt.id
                action.attempts = 1
                action.status = "executing"
                action.lease_token = uuid4()
                action.lease_until = row.expires_at
                store.add(
                    ActionExecution(
                        tenant_id=row.tenant_id,
                        action_id=action.id,
                        attempt=1,
                        lease_token=action.lease_token,
                        status="executing",
                        started_at=now(),
                    )
                )
                command = PrepareVoice.model_validate_json(json.dumps(row.command))
                return self.context(row), CallObjective(
                    primary_invoice_id=command.invoice_ids[0],
                    invoice_ids=tuple(command.invoice_ids),
                    objective=command.objective,
                    source_as_of=now(),
                )

            prepared = await db.run_sync(intent)
        if isinstance(prepared, VoiceSessionView):
            return prepared
        callctx, objective = prepared
        failure = None
        result = None
        try:
            result = await self.port(ctx.tenant_id, callctx.connection_id).start_call(
                objective,
                callctx,
                VoiceApprovalProposal.model_validate_json(
                    json.dumps(await self._proposal(key))
                ).allowed_tools,
                ConsentMode(
                    participant="LIVE_HUMAN_MICROPHONE_CAPTURE_OFF"
                    if self.binding(ctx.tenant_id, callctx.connection_id).evidence_mode == "LIVE"
                    else "SYNTHETIC_OFFLINE"
                ),
            )
        except VoiceFailure as exc:
            failure = exc.failure
        except HTTPException:
            # Fresh pre-wire refusal has no possible provider effect.
            await self._finish(key, None, "NO_EFFECT", "AUTHORITY_DENIED")
            raise
        if result is not None:
            try:
                await self.retain_call(key, result)
            except Exception:
                # The provider already accepted this exact native call. Private capability
                # storage failure cannot erase it or turn a retry into another create.
                await self._finish(
                    key, result.reference.external_id, "UNKNOWN", "PRIVATE_CAPABILITY_UNAVAILABLE"
                )
                raise HTTPException(503, "VOICE_PRIVATE_CAPABILITY_UNAVAILABLE") from None
        await self._finish(
            key,
            result.reference.external_id if result else None,
            "UNKNOWN" if failure and failure.may_have_side_effect else "FAILED" if failure else "SUCCESS",
            failure.code.value if failure else None,
            provider_status=result.status if result else None,
        )
        async with self.sessions.begin() as db:
            return await db.run_sync(lambda store: VoiceService(store, self).get(ctx, key))

    async def _proposal(self, key: UUID) -> dict[str, object]:
        async with self.sessions.begin() as db:

            def read(store: Session) -> dict[str, object]:
                row = store.get(VoiceSession, key)
                assert row is not None
                approval = store.get(ApprovalRequest, row.approval_id)
                assert approval is not None
                return approval.proposal

            return await db.run_sync(read)

    async def _finish(
        self,
        key: UUID,
        native: str | None,
        outcome: str,
        error: str | None,
        *,
        provider_status: str | None = None,
    ) -> None:
        async with self.sessions.begin() as db:

            def finish(store: Session) -> None:
                row = store.get(VoiceSession, key, with_for_update=True)
                assert row
                attempt = store.get(UsageAttempt, row.usage_attempt_id)
                assert attempt
                if row.live_grant_id is None:
                    attempt.outcome = outcome
                attempt.error_reference = error
                row.state = (
                    "completed"
                    if outcome == "SUCCESS" and provider_status == "ended"
                    else "active"
                    if outcome == "SUCCESS"
                    else "unknown_outcome"
                    if outcome == "UNKNOWN"
                    else "failed"
                )
                row.provider_call_id = native
                row.version += 1
                attempt.business_outcome = "COMMITTED" if outcome == "SUCCESS" else "FAILED"
                if outcome != "NO_EFFECT" and row.live_grant_id is None:
                    record_voice_usage(
                        store,
                        row,
                        attempt,
                        component="voice_browser",
                        operation="start_browser",
                        outcome=outcome,
                        native_operation_id=native,
                        session_id=row.id,
                    )
                action = store.scalar(
                    select(ScheduledAction).where(
                        ScheduledAction.voice_session_id == row.id, ScheduledAction.kind == "voice_session"
                    )
                )
                assert action
                action.status = (
                    "succeeded"
                    if outcome == "SUCCESS"
                    else "unknown_outcome"
                    if outcome == "UNKNOWN"
                    else "failed"
                )
                execution = store.scalar(
                    select(ActionExecution).where(ActionExecution.action_id == action.id)
                )
                assert execution
                execution.status = action.status
                execution.finished_at = now()
                execution.provider_operation_id = native

            await db.run_sync(finish)

    async def callback(self, raw: bytes, headers: Mapping[str, str]) -> dict[str, object]:
        if len(raw) > 65536:
            raise HTTPException(413, "VOICE_CALLBACK_TOO_LARGE")
        try:
            payload = json.loads(raw)
            token = payload["message"]["call"]["assistantOverrides"]["variableValues"]["acoCallCapability"]
            claims = self.signer.loads(token)
            tenant, key = UUID(claims["tenant_id"]), UUID(claims["session_id"])
        except (BadSignature, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(403, "VOICE_CAPABILITY_DENIED") from exc
        async with self.sessions.begin() as db:

            def load(store: Session) -> CallContext:
                row = store.get(VoiceSession, key)
                if row is None or row.tenant_id != tenant:
                    raise HTTPException(403, "VOICE_CAPABILITY_DENIED")
                if (
                    claims["expires_at"] != int(row.expires_at.timestamp())
                    or claims["binding_hash"] != row.binding_hash
                    or (self.clock.now() >= row.expires_at and not self.callback_expiry_allowed(row))
                ):
                    raise HTTPException(403, "VOICE_CAPABILITY_EXPIRED")
                return self.context(row)

            ctx = await db.run_sync(load)
        try:
            verified = await self.port(tenant, ctx.connection_id).verify_webhook(ctx, raw, headers)
        except VoiceFailure as exc:
            raise HTTPException(403, "VOICE_CALLBACK_AUTH_FAILED") from exc
        async with self.sessions.begin() as db:

            def apply(store: Session) -> dict[str, object]:
                row = self.callback_current(store, ctx, verified)
                results = []
                for event in verified.events:
                    if event.kind == "tool.requested":
                        if (
                            store.scalar(
                                select(VoiceEventRecord).where(
                                    VoiceEventRecord.tenant_id == row.tenant_id,
                                    VoiceEventRecord.session_id == row.id,
                                    VoiceEventRecord.event_key == "tool:" + str(event.invocation_id),
                                )
                            )
                            is None
                        ):
                            store.add(
                                VoiceEventRecord(
                                    tenant_id=row.tenant_id,
                                    session_id=row.id,
                                    event_key="tool:" + str(event.invocation_id),
                                    kind="tool.requested",
                                    raw_sha256=verified.raw_sha256,
                                )
                            )
                            store.flush()
                        result = self.tool(store, row, event)
                        results.append(
                            {
                                "name": event.tool,
                                "toolCallId": event.invocation_id,
                                "result": json.dumps(result, sort_keys=True),
                            }
                        )
                    elif (
                        store.scalar(
                            select(VoiceEventRecord).where(
                                VoiceEventRecord.session_id == row.id,
                                VoiceEventRecord.event_key == event.event_key,
                            )
                        )
                        is None
                    ):
                        store.add(
                            VoiceEventRecord(
                                tenant_id=row.tenant_id,
                                session_id=row.id,
                                event_key=event.event_key,
                                kind=event.kind,
                                raw_sha256=verified.raw_sha256,
                            )
                        )
                        if event.kind == "call.completed":
                            row.state = "completed"
                        elif row.state != "completed" and event.kind == "call.connected":
                            row.state = "connected"
                        row.version += 1
                return {"results": results}

            result = await db.run_sync(apply)
        # Transaction has committed before a voice assistant can report success.
        return result

    def tool(self, db: Session, row: VoiceSession, event: VoiceEvent) -> dict[str, object]:
        from aco.features.tools.schemas import InvoiceQuery

        if event.invocation_id is None:
            raise HTTPException(422, "VOICE_INVOCATION_REQUIRED")
        approval = db.get(ApprovalRequest, row.approval_id)
        if (
            approval is None
            or event.tool
            not in VoiceApprovalProposal.model_validate_json(json.dumps(approval.proposal)).allowed_tools
        ):
            raise HTTPException(403, "VOICE_TOOL_NOT_APPROVED")
        try:
            if event.tool in {
                "get_invoice",
                "get_communication_history",
                "find_existing_cases",
                "get_payment_promises",
            }:
                command: (
                    InvoiceQuery
                    | VoiceTaskCommand
                    | VoiceCaseCommand
                    | VoicePromiseCommand
                    | VoiceCaseFactCommand
                    | VoicePromiseFactCommand
                    | VoiceHoldFactCommand
                ) = InvoiceQuery.model_validate_json(json.dumps(event.arguments))
            elif event.tool == "upsert_case":
                command = (
                    VoiceCaseFactCommand.model_validate_json(json.dumps(event.arguments))
                    if "case_facts" in event.arguments
                    else VoiceCaseCommand.model_validate_json(json.dumps(event.arguments))
                )
            elif event.tool == "record_promise":
                command = (
                    VoicePromiseFactCommand.model_validate_json(json.dumps(event.arguments))
                    if "promise_facts" in event.arguments
                    else VoicePromiseCommand.model_validate_json(json.dumps(event.arguments))
                )
            elif event.tool == "request_collection_hold":
                command = VoiceHoldFactCommand.model_validate_json(json.dumps(event.arguments))
            else:
                command = VoiceTaskCommand.model_validate_json(json.dumps(event.arguments))
        except ValidationError as exc:
            raise HTTPException(422, "VOICE_TOOL_ARGUMENTS_INVALID") from exc
        arguments = command.model_dump(mode="json")
        if command.invoice_id not in {link.invoice_id for link in source_links(db, row)}:
            raise HTTPException(403, "VOICE_TOOL_SCOPE_DENIED")
        old = db.scalar(
            select(VoiceInvocation).where(
                VoiceInvocation.session_id == row.id, VoiceInvocation.native_id == event.invocation_id
            )
        )
        if old:
            if old.tool != event.tool or old.arguments != arguments:
                raise HTTPException(409, "VOICE_TOOL_REPLAY_CONFLICT")
            if old.result is None:
                raise HTTPException(409, "VOICE_TOOL_INCOMPLETE")
            return old.result
        if row.state not in {"active", "connected"}:
            raise HTTPException(403, "VOICE_CALL_ENDED")
        stopped = db.scalar(
            select(ScheduledAction.id)
            .where(
                ScheduledAction.tenant_id == row.tenant_id,
                ScheduledAction.voice_session_id == row.id,
                ScheduledAction.kind == "voice_internal",
                ScheduledAction.status == "succeeded",
                ScheduledAction.command["hold_facts"].is_not(None),
            )
            .limit(1)
        )
        if stopped is not None:
            raise HTTPException(403, "VOICE_CALL_CONTACT_STOPPED")
        self.current(db, self.context(row))
        count = db.scalar(
            select(func.count()).select_from(VoiceInvocation).where(VoiceInvocation.session_id == row.id)
        )
        if count is None or count >= 8:
            raise HTTPException(403, "VOICE_TOOL_LIMIT_REACHED")
        invocation = VoiceInvocation(
            tenant_id=row.tenant_id,
            session_id=row.id,
            native_id=event.invocation_id,
            tool=event.tool,
            arguments=arguments,
        )
        db.add(invocation)
        db.flush()
        invoice = db.get(InvoiceSnapshot, command.invoice_id)
        assert invoice
        if isinstance(command, InvoiceQuery) and event.tool != "get_invoice":
            from aco.features.voice.domain import read_records

            records = read_records(db, row, invoice, str(event.tool))
            result: dict[str, object] = {
                "invoice_id": str(invoice.id),
                "records": records[:50],
                "truncated": len(records) > 50,
            }
        elif isinstance(command, InvoiceQuery):
            result = {
                "invoice_id": str(invoice.id),
                "number": invoice.number,
                "outstanding_minor": str(invoice.balance_minor),
                "currency": invoice.currency,
                "source_revision": invoice.source_revision,
                "observed_at": invoice.observed_at.isoformat(),
                "synthetic": True,
            }
        else:
            reads = list(
                db.scalars(
                    select(VoiceInvocation).where(
                        VoiceInvocation.session_id == row.id,
                        VoiceInvocation.tool == "get_invoice",
                        VoiceInvocation.result.is_not(None),
                    )
                )
            )
            if not any(read.arguments.get("invoice_id") == str(invoice.id) for read in reads):
                raise HTTPException(403, "VOICE_READ_EVIDENCE_REQUIRED")
            action = VoiceService(db, self).action(row, invoice, "voice_internal", invocation)
            action.attempts = 1
            action.lease_token = uuid4()
            action.lease_until = row.expires_at
            db.add(action)
            db.flush()
            invocation.action_id = action.id
            from aco.features.tools.gateway import execute_voice_task
            from aco.features.voice.tools import VoiceActionContext

            context = VoiceActionContext(self.context(row), invocation.id, action.id, self)
            if isinstance(command, VoiceTaskCommand):
                task_id = execute_voice_task(db, context, command)
                result = {"status": "succeeded", "task_id": str(task_id), "action_id": str(action.id)}
            else:
                from aco.features.tools.gateway import execute_voice_domain

                result = execute_voice_domain(db, context, command)
                result["action_id"] = str(action.id)
            action.status = "succeeded"
            action.result = result
            db.add(
                ActionExecution(
                    tenant_id=row.tenant_id,
                    action_id=action.id,
                    attempt=1,
                    lease_token=action.lease_token,
                    status="succeeded",
                    started_at=now(),
                    finished_at=now(),
                    result=result,
                )
            )
        invocation.result = result
        db.add(
            AuditEvent(
                tenant_id=row.tenant_id,
                actor_id=row.actor_id,
                correlation_id=row.correlation_id,
                action="voice.tool_completed",
                target_id=invocation.id,
                details={"session_id": str(row.id), "tool": str(event.tool)},
            )
        )
        db.flush()
        return result
