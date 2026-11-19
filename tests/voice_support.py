"""Real database approval, callback tools and canonical action effects; mocked HTTP only."""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import pytest
from action_support import World, action_world
from fastapi import FastAPI, HTTPException
from identity_support import add_actor
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aco.core.types import Role
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.voice.dto import VoiceSessionView

if TYPE_CHECKING:
    from aco.features.voice.runtime import VoiceRuntime

VoiceCallback = Callable[[str, str, dict[str, object]], Awaitable[dict[str, object]]]
VoiceScenario = Callable[[World, "VoiceRuntime", VoiceSessionView, VoiceCallback], Awaitable[None]]


async def exercise_voice(boundary: str, scenario: VoiceScenario | None = None) -> VoiceSessionView | None:
    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.actions.models import ActionExecution
    from aco.features.review.models import CollectionTask
    from aco.features.voice.api import router
    from aco.features.voice.dto import PrepareVoice, VoiceSessionView
    from aco.features.voice.models import VoiceInvocation
    from aco.features.voice.runtime import VoiceRuntime
    from aco.features.voice.service import VoiceService
    from aco.integrations.vapi.settings import VapiBinding

    with action_world() as world:
        app = world.client.app
        assert isinstance(app, FastAPI)
        reviewer, _ = add_actor(world.store, app, "voice-reviewer@synthetic.invalid")
        world.store.add(
            Membership(tenant_id=world.ctx.tenant_id, user_id=reviewer.id, role="finance_manager")
        )
        connection = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="vapi",
            provider_type="voice",
            status="READY",
            encrypted_credentials_ref="offline-voice-1",
        )
        world.store.add(connection)
        world.store.commit()
        binding = VapiBinding(
            tenant_id=world.ctx.tenant_id,
            connection_id=connection.id,
            assistant_id=uuid4(),
            api_key=SecretStr("offline-not-a-key"),
            webhook_secret=SecretStr("synthetic-signing-secret"),
            credential_revision="offline-voice-1",
            evidence_mode="MOCKED_HTTP",
        )
        native = uuid4()
        requests: list[httpx.Request] = []

        def transport(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if boundary == "unknown_start":
                raise httpx.ReadTimeout("Synthetic lost response", request=request)
            return httpx.Response(
                201,
                json={
                    "id": str(native),
                    "type": "webCall",
                    "status": "ended" if boundary == "ended_start" else "queued",
                    "assistantId": str(binding.assistant_id),
                },
            )

        engine = create_async_engine(world.url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        runtime = VoiceRuntime(
            sessions,
            lambda tenant, conn: binding,
            transport_factory=lambda: httpx.MockTransport(transport),
            capability_secret=SecretStr("local-test-capability-secret"),
            enabled=True,
        )
        command = PrepareVoice(
            request_id=uuid4(),
            connection_id=connection.id,
            invoice_ids=[world.invoice_id],
            objective="Review the synthetic invoice and create one internal follow-up task",
        )
        app.include_router(router)
        app.state.voice_runtime = runtime
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/voice-sessions", json=command.model_dump(mode="json")
        )
        assert response.status_code == 201, response.text
        view = VoiceSessionView.model_validate_json(response.text)
        replay = VoiceService(world.store, runtime).prepare_call(world.ctx, command)
        assert replay.id == view.id and replay.approval_id == view.approval_id
        world.store.commit()
        context = world.ctx.model_copy(update={"actor_id": reviewer.id, "role": Role.FINANCE_MANAGER})
        if boundary == "self_approval":
            context = world.ctx
        service = HumanApprovalService(world.store)
        approval = service.get(context, view.approval_id)
        decision = ApprovalDecisionCommand(
            expected_version=approval.version,
            reviewed_action_version=approval.action_version,
            reviewed_material_hash=approval.material_hash,
            reviewed_binding_hash=approval.current_binding_hash,
            decision="approved",
            reason="Synthetic offline test",
        )
        if boundary == "self_approval":
            with pytest.raises(HTTPException) as refused:
                service.decide_approval(context, view.approval_id, decision)
            assert refused.value.status_code == 403
            world.store.rollback()
            await engine.dispose()
            return None
        assert approval.effect == "voice_session" and approval.can_approve
        service.decide_approval(context, view.approval_id, decision)
        world.store.commit()
        started = await runtime.start(world.ctx, view.id, view.version)
        if boundary == "unknown_start":
            from aco.features.usage.models import UsageAttempt

            assert started.state == "unknown_outcome" and started.provider_call_id is None
            assert len(requests) == 1
            with pytest.raises(HTTPException) as duplicate:
                await runtime.start(world.ctx, view.id, started.version)
            assert duplicate.value.status_code == 409 and len(requests) == 1
            world.store.expire_all()
            attempt = world.store.scalar(
                select(UsageAttempt).where(UsageAttempt.operation == "start_browser")
            )
            assert attempt is not None and attempt.outcome == "UNKNOWN"
            await engine.dispose()
            return started
        assert started.provider_call_id == str(native) and started.evidence_mode == "MOCKED_HTTP"
        from aco.features.usage.dto import ProviderUsageInput
        from aco.features.usage.models import ProviderUsageEvent, UsageRevision

        event = world.store.scalar(
            select(ProviderUsageEvent).where(ProviderUsageEvent.provider_connection_id == connection.id)
        )
        assert event is not None and event.scope_key == str(world.ctx.tenant_id)
        revision = world.store.scalar(select(UsageRevision).where(UsageRevision.event_id == event.id))
        assert revision is not None
        observed = ProviderUsageInput.model_validate_json(json.dumps(revision.payload))
        assert observed.units.requests == 1 and observed.native_operation_id == str(native)
        assert observed.cost.amount is None and observed.cost.quality.value == "UNKNOWN"
        world.store.rollback()
        assert len(requests) == 1
        assert (await runtime.start(world.ctx, view.id, started.version)).provider_call_id == str(native)
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        token = body["assistantOverrides"]["variableValues"]["acoCallCapability"]

        async def callback(tool_id: str, tool: str, arguments: dict[str, object]) -> dict[str, object]:
            message = {
                "type": "tool-calls",
                "call": {
                    "id": str(native),
                    "assistantId": str(binding.assistant_id),
                    "assistantOverrides": {"variableValues": {"acoCallCapability": token}},
                },
                "toolCallList": [
                    {"id": tool_id, "type": "function", "function": {"name": tool, "arguments": arguments}}
                ],
            }
            raw = json.dumps({"message": message}).encode()
            stamp = str(int(now().timestamp()))
            return await runtime.callback(
                raw,
                {
                    "x-timestamp": stamp,
                    "x-signature": hmac.new(
                        b"synthetic-signing-secret", stamp.encode() + b"." + raw, hashlib.sha256
                    ).hexdigest(),
                },
            )

        if boundary == "ended_start":
            assert started.state == "completed"
            new_tools: list[tuple[str, dict[str, object]]] = [
                ("get_invoice", {"invoice_id": str(world.invoice_id)}),
                (
                    "create_collection_task",
                    {"invoice_id": str(world.invoice_id), "title": "Forbidden postcall task"},
                ),
            ]
            for tool, args in new_tools:
                with pytest.raises(HTTPException) as ended:
                    await callback("new-after-ended-" + tool, tool, args)
                assert ended.value.detail == "VOICE_CALL_ENDED"
            assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 0
            assert world.store.scalar(select(func.count()).select_from(VoiceInvocation)) == 0
            await engine.dispose()
            return started

        # A signed provider request still cannot widen the exact call invoice set.
        with pytest.raises(HTTPException):
            await callback("foreign", "get_invoice", {"invoice_id": str(uuid4())})
        read = await callback("read-1", "get_invoice", {"invoice_id": str(world.invoice_id)})
        assert "results" in read
        if scenario is not None:
            try:
                await scenario(world, runtime, started, callback)
                world.store.rollback()
                return VoiceService(world.store, runtime).get(world.ctx, view.id)
            finally:
                await engine.dispose()
        if boundary == "member_revoked":
            member = world.store.scalar(select(Membership).where(Membership.user_id == reviewer.id))
            assert member
            member.active = False
            member.version += 1
            world.store.commit()
        elif boundary == "source_changed":
            invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
            assert invoice
            invoice.source_revision = "changed-after-approval"
            world.store.commit()
        elif boundary == "expired":
            from datetime import timedelta

            from aco.core.clock import FrozenClock

            runtime.clock = FrozenClock(now() + timedelta(minutes=6))
        elif boundary == "connection_disconnected":
            from sqlalchemy.inspection import inspect

            from aco.features.voice.models import VoiceSession

            def retained() -> list[dict[str, object]]:
                world.store.rollback()
                return [
                    {column.key: getattr(row, column.key) for column in inspect(type(row)).columns}
                    for statement in (
                        select(VoiceSession).where(VoiceSession.id == view.id),
                        select(VoiceInvocation).where(VoiceInvocation.session_id == view.id),
                        select(ProviderUsageEvent).where(
                            ProviderUsageEvent.provider_connection_id == binding.connection_id
                        ),
                        select(UsageRevision).where(UsageRevision.event_id == event.id),
                    )
                    for row in world.store.scalars(statement)
                ]

            before = retained()
            base = f"/api/workspaces/{world.ctx.tenant_id}/provider-connections"
            listed = world.client.get(base)
            assert listed.status_code == 200, listed.text
            current = next(row for row in listed.json() if row["id"] == str(binding.connection_id))
            world.store.rollback()
            response = world.client.post(
                base + f"/{binding.connection_id}/disconnect",
                json={"expected_version": current["version"]},
            )
            assert response.status_code == 200, response.text
            assert response.json()["remote_revocation"] == "UNKNOWN"
            assert retained() == before
            world.store.rollback()
        elif boundary == "kill":
            runtime.enabled = False
        arguments: dict[str, object] = {
            "invoice_id": str(world.invoice_id),
            "title": "Review synthetic voice follow-up",
        }
        detail = None
        if boundary != "success":
            with pytest.raises(HTTPException):
                await callback("write-1", "create_collection_task", arguments)
            world.store.rollback()
            assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 0
            if boundary == "connection_disconnected":
                assert retained() == before
                assert len(requests) == 1
                saved = VoiceService(world.store, runtime).get(world.ctx, view.id)
                assert saved.provider_call_id == str(native)
                assert not saved.can_start and len(saved.invocations) == 1
        else:
            result = await callback("write-1", "create_collection_task", arguments)
            assert await callback("write-1", "create_collection_task", arguments) == result
            with pytest.raises(HTTPException):
                await callback("write-1", "create_collection_task", arguments | {"title": "Changed replay"})
            raw = json.dumps(
                {
                    "message": {
                        "type": "end-of-call-report",
                        "call": {
                            "id": str(native),
                            "assistantId": str(binding.assistant_id),
                            "assistantOverrides": {"variableValues": {"acoCallCapability": token}},
                        },
                        "artifact": {
                            "transcript": "Unapproved transcript must not be retained",
                            "recordingUrl": "https://private.invalid",
                        },
                    }
                }
            ).encode()
            stamp = str(int(now().timestamp()))
            headers = {
                "x-timestamp": stamp,
                "x-signature": hmac.new(
                    b"synthetic-signing-secret", stamp.encode() + b"." + raw, hashlib.sha256
                ).hexdigest(),
            }
            await runtime.callback(raw, headers)
            await runtime.callback(raw, headers)
            assert await callback("write-1", "create_collection_task", arguments) == result
            with pytest.raises(HTTPException):
                await callback("new-postcall", "create_collection_task", arguments)
            world.store.rollback()
            assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
            assert world.store.scalar(select(func.count()).select_from(ActionExecution)) == 2
            task = world.store.scalar(select(CollectionTask))
            assert task and task.source_action_id
            assert world.store.scalar(select(func.count()).select_from(VoiceInvocation)) == 2
            detail = VoiceService(world.store, runtime).get(world.ctx, view.id)
            assert detail.state == "completed" and detail.changed_ids == [task.id]
            assert detail.transcript_available is False and detail.recording_available is False
            assert "Unapproved transcript" not in detail.model_dump_json()
        await engine.dispose()
        return detail
