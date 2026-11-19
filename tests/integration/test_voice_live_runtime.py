"""Offline LIVE-code lifecycle through real approval, intent, private join and terminal receipt.

HTTPX responses and test identities are explicitly synthetic; no provider request,
participant consent, real microphone, capture, live grant or external call is installed.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from voice_live_support import live_world

from aco.core.clock import FrozenClock
from aco.core.types import ProviderReference, Role
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.identity.models import Membership, now
from aco.features.policies.service import PolicyService
from aco.features.promises.models import PaymentPromise
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.review.models import CollectionTask
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageSource
from aco.features.voice.dto import PrepareVoice, SetupVoice, VoiceApprovalProposal
from aco.features.voice.live import VoiceBrowserJoin, VoiceLiveAuthorization, config_for
from aco.features.voice.models import VoiceBusinessEvidence, VoiceSession
from aco.features.voice.service import VoiceService


@pytest.mark.parametrize("lost_stop,retention_failure", [(False, False), (True, False), (False, True)])
async def test_live_code_start_join_kill_stop_and_terminal_are_independently_scoped(
    lost_stop: bool, retention_failure: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.features.voice.live_runtime import LiveVoiceRuntime

    with live_world(unpaused=True) as fixture:
        world, grant, binding = fixture.world, fixture.grant, fixture.binding
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        before = (invoice.source_revision, invoice.balance_minor, invoice.financial_status)
        # The fixture installs the immutable grant only after actual reviewed activation/unpause.
        initial_tasks = db.scalar(
            select(func.count())
            .select_from(CollectionTask)
            .where(CollectionTask.tenant_id == world.ctx.tenant_id)
        )
        assert initial_tasks is not None
        db.commit()
        at = now().replace(hour=10, minute=0, second=0, microsecond=0)
        if at > now():
            at -= timedelta(days=1)
        while at.weekday() >= 5:
            at -= timedelta(days=1)
        clock = FrozenClock(at)
        engine = create_async_engine(world.url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        config = config_for(grant)
        killed = False
        auth = VoiceLiveAuthorization(
            grant.id,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=lambda: killed,
        )
        native = uuid4()
        requests: list[httpx.Request] = []

        def transport(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET" and request.url.path.startswith("/call/"):
                return httpx.Response(
                    200,
                    json={
                        "id": str(native),
                        "assistantId": str(binding.assistant_id),
                        "type": "webCall",
                        "status": "in-progress",
                    },
                )
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=fixture.profile
                    if request.url.path.startswith("/assistant/")
                    else fixture.credential,
                )
            if request.url.host == "control.vapi.ai":
                assert request.url.path == f"/{native}/control"
                assert json.loads(request.content) == {"type": "end-call"}
                if lost_stop:
                    raise httpx.ReadTimeout("Offline lost stop response", request=request)
                return httpx.Response(200, json={"status": "ok"})
            assert request.url.path == "/call/web"
            return httpx.Response(
                201,
                json={
                    "id": str(native),
                    "assistantId": str(binding.assistant_id),
                    "type": "webCall",
                    "status": "queued",
                    "webCallUrl": "https://vapi.daily.co/offline-owned-room",
                    "monitor": {"controlUrl": f"https://control.vapi.ai/{native}/control"},
                },
            )

        runtime = LiveVoiceRuntime(
            sessions,
            binding,
            authorization=auth,
            capability_secret=SecretStr("offline-only-call-capability"),
            join_encryption_key=SecretStr(Fernet.generate_key().decode()),
            transport_factory=lambda: httpx.MockTransport(transport),
            clock=clock,
        )
        # The LIVE-code branch must actually probe before exposing a prepare choice.
        db.rollback()
        try:
            ready = await runtime.setup(world.ctx, SetupVoice(request_id=uuid4()))
            assert ready.can_prepare and len(requests) == 2
            command = PrepareVoice(
                request_id=uuid4(),
                connection_id=binding.connection_id,
                invoice_ids=[world.invoice_id],
                objective="Review explicit synthetic business facts, capture off",
            )
            runtime.bound = binding.model_copy(update={"api_key": SecretStr("changed-offline-key")})
            with pytest.raises(HTTPException) as changed:
                runtime.preparation_binding(db, command, world.ctx.actor_id)
            assert changed.value.detail == "VOICE_LIVE_CONFIGURATION_CHANGED"
            runtime.bound = binding
            view = VoiceService(db, runtime).prepare_call(world.ctx, command)
            assert view.evidence_mode == "LIVE" and view.state == "awaiting_approval"
            db.commit()
            reviewer = world.ctx.model_copy(
                update={"actor_id": grant.participant_id, "role": Role.FINANCE_MANAGER}
            )
            approvals = HumanApprovalService(db, at=clock.now())
            approval = approvals.get(reviewer, view.approval_id)
            assert isinstance(approval.proposal, VoiceApprovalProposal)
            assert (
                approval.can_approve and approval.proposal.participant == "LIVE_HUMAN_MICROPHONE_CAPTURE_OFF"
            )
            approvals.decide_approval(
                reviewer,
                view.approval_id,
                ApprovalDecisionCommand(
                    expected_version=approval.version,
                    reviewed_action_version=approval.action_version,
                    reviewed_material_hash=approval.material_hash,
                    reviewed_binding_hash=approval.current_binding_hash,
                    decision="approved",
                    reason="Offline independent human review, not actual participant consent",
                ),
            )
            db.commit()
            if retention_failure:

                async def unavailable(*args: object) -> None:
                    raise RuntimeError("Explicit offline capability persistence failure after acceptance")

                monkeypatch.setattr(runtime, "retain_call", unavailable)
                with pytest.raises(HTTPException) as retention:
                    await runtime.start(world.ctx, view.id, view.version)
                assert retention.value.status_code == 503
                db.expire_all()
                retained = db.get(VoiceSession, view.id)
                assert retained is not None and retained.provider_call_id == str(native)
                assert retained.state == "unknown_outcome" and len(requests) == 3
                assert (
                    db.scalar(select(VoiceBrowserJoin.id).where(VoiceBrowserJoin.session_id == retained.id))
                    is None
                )
                version = retained.version
                db.commit()
                with pytest.raises(HTTPException):
                    await runtime.start(world.ctx, view.id, version)
                assert len(requests) == 3
                token = json.loads(requests[-1].content)["assistantOverrides"]["variableValues"][
                    "acoCallCapability"
                ]
                body = json.dumps(
                    {
                        "message": {
                            "type": "status-update",
                            "status": "ended",
                            "call": {
                                "id": str(native),
                                "assistantId": str(binding.assistant_id),
                                "assistantOverrides": {"variableValues": {"acoCallCapability": token}},
                            },
                        }
                    }
                ).encode()
                stamp = str(int(clock.now().timestamp()))
                headers = {
                    "x-timestamp": stamp,
                    "x-signature": hmac.new(
                        binding.webhook_secret.get_secret_value().encode(),
                        stamp.encode() + b"." + body,
                        hashlib.sha256,
                    ).hexdigest(),
                }
                assert await runtime.callback(body, headers) == {"results": []}
                assert await runtime.callback(body, headers) == {"results": []}
                db.expire_all()
                retained = db.get(VoiceSession, view.id)
                assert retained is not None and retained.state == "completed"
                assert len(requests) == 3
                db.refresh(invoice)
                assert (invoice.source_revision, invoice.balance_minor, invoice.financial_status) == before
                return
            started = await runtime.start(world.ctx, view.id, view.version)
            assert started.provider_call_id == str(native) and started.evidence_mode == "LIVE"
            assert "offline-owned-room" not in started.model_dump_json()
            assert len(requests) == 3
            with pytest.raises(HTTPException):
                await runtime.join(world.ctx, view.id, started.version)
            joined = await runtime.join(reviewer, view.id, started.version)
            assert joined.web_call_url == "https://vapi.daily.co/offline-owned-room"
            assert joined.provider_call_id == str(native) and len(requests) == 3
            assert (await runtime.start(world.ctx, view.id, started.version)).provider_call_id == str(native)
            assert len(requests) == 3
            saved_call = db.get(VoiceSession, view.id, populate_existing=True)
            assert saved_call is not None
            call_context = runtime.context(saved_call)
            db.commit()
            reference = ProviderReference(
                provider="vapi", connection_id=binding.connection_id, external_id=str(native)
            )
            await runtime.port(world.ctx.tenant_id, binding.connection_id).get_call(call_context, reference)
            await runtime.port(world.ctx.tenant_id, binding.connection_id).get_call(call_context, reference)
            with pytest.raises(HTTPException) as exhausted:
                await runtime.port(world.ctx.tenant_id, binding.connection_id).get_call(
                    call_context, reference
                )
            assert exhausted.value.detail == "VOICE_LIVE_BUDGET_EXHAUSTED" and len(requests) == 5
            # Stop headroom was reserved before create, so exhausted ordinary reads cannot prevent it.
            post = next(request for request in requests if request.url.path == "/call/web")
            token = json.loads(post.content)["assistantOverrides"]["variableValues"]["acoCallCapability"]

            async def callback(
                kind: str,
                *,
                foreign: bool = False,
                status: str = "ended",
                mixed: bool = False,
                tool: str | None = None,
                arguments: dict[str, object] | None = None,
            ) -> dict[str, object]:
                message: dict[str, object] = {
                    "type": kind,
                    "status": status,
                    "call": {
                        "id": str(uuid4() if foreign else native),
                        "assistantId": str(binding.assistant_id),
                        "assistantOverrides": {"variableValues": {"acoCallCapability": token}},
                    },
                }
                if tool is not None:
                    message["toolCallList"] = [
                        {"id": str(uuid4()), "function": {"name": tool, "arguments": arguments or {}}}
                    ]
                if mixed:
                    message["transcript"] = "must not persist this mixed capture-bearing event"
                raw = json.dumps({"message": message}).encode()
                stamp = str(int(clock.now().timestamp()))
                return await runtime.callback(
                    raw,
                    {
                        "x-timestamp": stamp,
                        "x-signature": hmac.new(
                            binding.webhook_secret.get_secret_value().encode(),
                            stamp.encode() + b"." + raw,
                            hashlib.sha256,
                        ).hexdigest(),
                    },
                )

            await callback("tool-calls", tool="get_invoice", arguments={"invoice_id": str(world.invoice_id)})
            await callback(
                "tool-calls",
                tool="record_promise",
                arguments={
                    "invoice_id": str(world.invoice_id),
                    "promise_facts": {
                        "invoice_ids": [str(world.invoice_id)],
                        "amount": {"minor_units": 1000, "currency": "AUD", "exponent": 2},
                        "due_date": (now() + timedelta(days=7)).date().isoformat(),
                        "timezone": "UTC",
                    },
                },
            )
            # Its own new pending-commitment hold cannot prevent the same active call's read.
            await callback("tool-calls", tool="get_invoice", arguments={"invoice_id": str(world.invoice_id)})
            db.expire_all()
            evidence = db.scalar(
                select(VoiceBusinessEvidence).where(VoiceBusinessEvidence.session_id == view.id)
            )
            assert evidence is not None and not evidence.synthetic and evidence.evidence_mode == "LIVE"
            promise = db.scalar(
                select(PaymentPromise).where(PaymentPromise.source_voice_evidence_id == evidence.id)
            )
            assert promise is not None and promise.status == "NEEDS_CONFIRMATION" and not promise.synthetic
            current_session = db.get(VoiceSession, view.id)
            assert current_session is not None
            started = VoiceService(db, runtime).view(current_session)
            tasks_after_facts = db.scalar(
                select(func.count())
                .select_from(CollectionTask)
                .where(CollectionTask.tenant_id == world.ctx.tenant_id)
            )
            assert (
                tasks_after_facts == initial_tasks + 1
            )  # Actual human clarification task, not financial success.
            db.commit()
            PolicyService(db).place_hold(
                world.ctx,
                world.invoice_id,
                "manual_hold",
                "Independent current human restriction, not this call's own facts",
            )
            db.commit()
            with pytest.raises(HTTPException) as held:
                await callback(
                    "tool-calls", tool="get_invoice", arguments={"invoice_id": str(world.invoice_id)}
                )
            assert held.value.detail == "manual_hold"
            killed = True
            with pytest.raises(HTTPException):
                await runtime.join(reviewer, view.id, started.version)
            with pytest.raises(HTTPException):
                await runtime.end(world.ctx, uuid4(), started.version)
            if lost_stop:
                with pytest.raises(HTTPException) as failure:
                    await runtime.end(world.ctx, view.id, started.version)
                assert failure.value.detail == "VOICE_LIVE_STOP_OUTCOME_UNKNOWN"
                stopped = started
            else:
                stopped = await runtime.end(world.ctx, view.id, started.version)
            db.expire_all()
            current = VoiceService(db, runtime).get(world.ctx, view.id)
            assert current.stop_outcome == ("UNKNOWN" if lost_stop else "ACCEPTED")
            db.rollback()  # Release current-view locks before the exact retry transaction.
            assert stopped.state != "completed" and len(requests) == 6
            assert (await runtime.end(world.ctx, view.id, stopped.version)).state == stopped.state
            assert len(requests) == 6
            with pytest.raises(HTTPException):
                await callback("transcript")
            with pytest.raises(HTTPException):
                await callback("status-update", foreign=True)
            with pytest.raises(HTTPException):
                await callback("status-update", status="in-progress")
            with pytest.raises(HTTPException):
                await callback("status-update", mixed=True)
            member = db.scalar(
                select(Membership).where(
                    Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
                )
            )
            assert member is not None
            member.active = False
            member.version += 1
            db.commit()
            clock = FrozenClock(now() + timedelta(minutes=10))
            runtime.clock = clock
            assert await callback("status-update") == {"results": []}
            assert await callback("status-update") == {"results": []}
            db.expire_all()
            saved = db.get(VoiceSession, view.id)
            assert saved is not None and saved.state == "completed"
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(CollectionTask)
                    .where(CollectionTask.tenant_id == world.ctx.tenant_id)
                )
                == tasks_after_facts
            )
            stop_attempt = db.scalar(
                select(UsageAttempt).where(
                    UsageAttempt.provider_connection_id == binding.connection_id,
                    UsageAttempt.operation == "voice_end_call",
                )
            )
            assert stop_attempt is not None and stop_attempt.outcome == (
                "UNKNOWN" if lost_stop else "SUCCEEDED"
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(ProviderUsageEvent)
                    .where(
                        ProviderUsageEvent.provider_connection_id == binding.connection_id,
                        ProviderUsageEvent.synthetic.is_(False),
                    )
                )
                == 6
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(UsageSource)
                    .where(UsageSource.source_kind == "voice_session", UsageSource.source_id == view.id)
                )
                == 4
            )
            db.refresh(invoice)
            assert (invoice.source_revision, invoice.balance_minor, invoice.financial_status) == before
            assert len(requests) == 6
        finally:
            await engine.dispose()
