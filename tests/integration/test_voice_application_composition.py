"""Normal private loader/restart; all identities and HTTP results are explicitly synthetic."""

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from voice_application_support import selection
from voice_live_support import live_world

from aco.core.clock import FrozenClock
from aco.core.types import Role
from aco.features.identity.models import Membership, now
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.voice.dto import PrepareVoice, SetupVoice
from aco.features.voice.live import VoiceLiveGrant
from aco.features.voice.live_runtime import LiveVoiceRuntime
from aco.features.voice.models import VoiceSession
from aco.features.voice.service import VoiceService


@pytest.mark.parametrize("lost_stop", [False, True])
async def test_private_loader_restart_retains_terminal_only_after_kill_expiry_and_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_stop: bool
) -> None:
    from aco.core.application import configured_app, configured_worker
    from aco.integrations.vapi import application

    with live_world(unpaused=True) as fixture:
        world, binding = fixture.world, fixture.binding
        path, settings = selection(fixture, tmp_path, monkeypatch)
        engine = create_async_engine(world.url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        requests: list[httpx.Request] = []
        native = uuid4()
        at = now().replace(hour=10, minute=0, second=0, microsecond=0)
        if at > now():
            at -= timedelta(days=1)
        while at.weekday() >= 5:
            at -= timedelta(days=1)
        clock = FrozenClock(at)

        def transport(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=fixture.profile
                    if request.url.path.startswith("/assistant/")
                    else fixture.credential,
                )
            if request.url.host == "control.vapi.ai":
                assert request.method == "POST" and json.loads(request.content) == {"type": "end-call"}
                if lost_stop:
                    raise httpx.ReadTimeout("Explicit synthetic stop response loss", request=request)
                return httpx.Response(200, json={"status": "ok"})
            assert request.method == "POST" and request.url.path == "/call/web"
            return httpx.Response(
                201,
                json={
                    "id": str(native),
                    "assistantId": str(binding.assistant_id),
                    "type": "webCall",
                    "status": "queued",
                    "webCallUrl": "https://vapi.daily.co/offline-existing-room",
                    "monitor": {"controlUrl": f"https://control.vapi.ai/{native}/control"},
                },
            )

        try:
            app = configured_app(settings, {})
            runtime = app.state.voice_runtime
            assert isinstance(runtime, LiveVoiceRuntime) and not requests
            # The maintained normal worker validates the same selection before registering jobs.
            worker = configured_worker(settings, {})
            await worker.engine.dispose()
            assert not requests
            runtime.clock, runtime.transport_factory = clock, lambda: httpx.MockTransport(transport)
            await runtime.setup(world.ctx, SetupVoice(request_id=uuid4()))
            db = world.store
            prepared = VoiceService(db, runtime).prepare_call(
                world.ctx,
                PrepareVoice(
                    request_id=uuid4(),
                    connection_id=binding.connection_id,
                    invoice_ids=[world.invoice_id],
                    objective="Explicit offline normal-composition lifecycle",
                ),
            )
            db.commit()
            reviewer = world.ctx.model_copy(
                update={"actor_id": fixture.grant.participant_id, "role": Role.FINANCE_MANAGER}
            )
            service = HumanApprovalService(db, at=clock.now())
            approval = service.get(reviewer, prepared.approval_id)
            assert approval.can_approve
            service.decide_approval(
                reviewer,
                approval.id,
                ApprovalDecisionCommand(
                    expected_version=approval.version,
                    reviewed_action_version=approval.action_version,
                    reviewed_material_hash=approval.material_hash,
                    reviewed_binding_hash=approval.current_binding_hash,
                    decision="approved",
                    reason="Explicit offline independent review, not real microphone consent",
                ),
            )
            db.commit()
            started = await runtime.start(world.ctx, prepared.id, prepared.version)
            assert started.provider_call_id == str(native) and len(requests) == 3
            token = json.loads(requests[-1].content)["assistantOverrides"]["variableValues"][
                "acoCallCapability"
            ]
            # Explicit stop must fence rejoining even while kill remains false and
            # authenticated completion has not arrived. Reserved headroom alone did not.
            assert (await runtime.join(reviewer, prepared.id, started.version)).provider_call_id == str(
                native
            )
            if lost_stop:
                with pytest.raises(HTTPException, match="VOICE_LIVE_STOP_OUTCOME_UNKNOWN"):
                    await runtime.end(world.ctx, prepared.id, started.version)
            else:
                stopped = await runtime.end(world.ctx, prepared.id, started.version)
                assert stopped.state == "active" and stopped.stop_outcome == "ACCEPTED"
            with pytest.raises(HTTPException) as denied_join:
                await runtime.join(reviewer, prepared.id, started.version)
            assert denied_join.value.detail == "VOICE_LIVE_STOP_REQUESTED"
            current = VoiceService(db, runtime).get(reviewer, prepared.id)
            assert not current.can_join and not current.can_end
            assert current.stop_outcome == ("UNKNOWN" if lost_stop else "ACCEPTED")
            db.rollback()
            tool_body = json.dumps(
                {
                    "message": {
                        "type": "tool-calls",
                        "call": {
                            "id": str(native),
                            "assistantId": str(binding.assistant_id),
                            "assistantOverrides": {"variableValues": {"acoCallCapability": token}},
                        },
                        "toolCallList": [
                            {
                                "id": str(uuid4()),
                                "function": {
                                    "name": "get_invoice",
                                    "arguments": {"invoice_id": str(world.invoice_id)},
                                },
                            }
                        ],
                    }
                }
            ).encode()
            tool_stamp = str(int(clock.now().timestamp()))
            with pytest.raises(HTTPException) as denied_tool:
                await runtime.callback(
                    tool_body,
                    {
                        "x-timestamp": tool_stamp,
                        "x-signature": hmac.new(
                            binding.webhook_secret.get_secret_value().encode(),
                            tool_stamp.encode() + b"." + tool_body,
                            hashlib.sha256,
                        ).hexdigest(),
                    },
                )
            assert denied_tool.value.detail == "VOICE_LIVE_STOP_REQUESTED"
            member = db.scalar(
                select(Membership).where(
                    Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
                )
            )
            grant = db.get(VoiceLiveGrant, fixture.grant.id)
            assert member is not None and grant is not None
            member.active, member.version = False, member.version + 1
            grant.revoked_at = now()
            db.commit()
            future = now() + timedelta(minutes=20)
            monkeypatch.setattr("aco.features.voice.live.now", lambda: future)
            retained = application.local_voice_runtime(
                settings.model_copy(update={"kill_switch": True, "outbound_enabled": False}),
                sessions,
                config_path=path,
            )
            assert isinstance(retained, LiveVoiceRuntime) and len(requests) == 4
            db.expire_all()
            current_page = VoiceService(db, retained).list(reviewer)
            assert current_page.can_prepare is False
            assert current_page.evidence_mode == "LIVE"
            db.rollback()  # Close read/control-permission locks before another runtime transaction.
            retained.clock, retained.transport_factory = (
                FrozenClock(future),
                lambda: httpx.MockTransport(transport),
            )
            with pytest.raises(HTTPException):
                await retained.setup(world.ctx, SetupVoice(request_id=uuid4()))
            with pytest.raises(HTTPException):
                await retained.start(world.ctx, prepared.id, started.version)
            with pytest.raises(HTTPException):
                await retained.join(reviewer, prepared.id, started.version)
            # A consumed exact stop replay returns retained history only; it cannot
            # acquire another request/reservation even after action revocation.
            replayed_stop = await retained.end(reviewer, prepared.id, started.version)
            assert replayed_stop.stop_outcome == ("UNKNOWN" if lost_stop else "ACCEPTED")
            assert len(requests) == 4
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
            stamp = str(int(future.timestamp()))
            headers = {
                "x-timestamp": stamp,
                "x-signature": hmac.new(
                    binding.webhook_secret.get_secret_value().encode(),
                    stamp.encode() + b"." + body,
                    hashlib.sha256,
                ).hexdigest(),
            }
            original = path.read_bytes()
            changed = json.loads(original)
            changed["source_commit"] = "b" * 40
            path.write_text(json.dumps(changed))
            with pytest.raises(HTTPException):
                await retained.callback(body, headers)
            with pytest.raises(ValueError, match="^VOICE_PRIVATE_CONFIGURATION_REQUIRED$"):
                application.local_voice_runtime(settings, sessions, config_path=path)
            path.write_bytes(original)
            assert await retained.callback(body, headers) == {"results": []}
            assert await retained.callback(body, headers) == {"results": []}
            db.expire_all()
            saved = db.get(VoiceSession, prepared.id)
            assert saved is not None and saved.state == "completed" and len(requests) == 4
        finally:
            await engine.dispose()
