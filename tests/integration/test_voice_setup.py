"""Actual offline setup probe and signed simulator callbacks; no provider calls."""

from uuid import uuid4

import httpx
import pytest
from action_support import action_world
from fastapi import FastAPI
from identity_support import add_actor
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aco.core.types import Role
from aco.features.identity.models import Membership, ProviderConnection
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.review.models import CollectionTask


@pytest.mark.parametrize(
    "boundary",
    [
        "success",
        "business_facts",
        "disabled",
        "malformed",
        "rotation",
        "viewer",
        "member_revoked",
        "member_changed",
    ],
)
async def test_owner_probe_and_actual_mocked_callback_application_path(boundary: str) -> None:
    from aco.features.voice.api import callback_router, router
    from aco.features.voice.demo import OfflineVoiceDemo
    from aco.features.voice.dto import VoicePage, VoiceSessionView
    from aco.features.voice.models import VoiceSession

    with action_world() as world:
        app = world.client.app
        assert isinstance(app, FastAPI)
        engine = create_async_engine(world.url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        app.include_router(router)
        app.include_router(callback_router)
        callbacks: list[str] = []

        async def deliver(raw: bytes, headers: dict[str, str]) -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/voice/callback", content=raw, headers=headers)
                assert response.status_code == 200, response.text
                callbacks.append(response.text)

        demo = OfflineVoiceDemo(
            sessions,
            capability_secret=SecretStr("synthetic-local-voice"),
            callback_sender=deliver,
            enabled=True,
        )
        runtime = demo.runtime
        app.state.voice_runtime = runtime
        if boundary == "disabled":
            runtime.enabled = False
        if boundary == "viewer":
            member = world.store.scalar(
                select(Membership).where(
                    Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
                )
            )
            assert member is not None
            member.role = "viewer"
            member.version += 1
            world.store.commit()
        base_transport = demo.respond

        def transport(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and boundary == "malformed":
                return httpx.Response(200, json={"id": "not-an-assistant"})
            response = base_transport(request)
            if request.method == "GET" and boundary == "rotation":
                runtime.enabled = False
            if request.method == "GET" and boundary in {"member_revoked", "member_changed"}:
                member = world.store.scalar(
                    select(Membership).where(
                        Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
                    )
                )
                assert member is not None
                if boundary == "member_revoked":
                    member.active = False
                member.version += 1
                world.store.commit()
            return response

        runtime.transport_factory = lambda: httpx.MockTransport(transport)
        prefix = f"/api/workspaces/{world.ctx.tenant_id}/voice-sessions"
        page = world.client.get(prefix)
        assert page.status_code == 200
        assert page.json()["can_setup"] == (boundary not in {"disabled", "viewer"})
        operation = {"request_id": str(uuid4())}
        if boundary in {"success", "business_facts"}:
            denied = world.client.post(prefix + "/setup", json=operation, headers={"x-csrf-token": "invalid"})
            assert denied.status_code == 403
            foreign = world.client.post(f"/api/workspaces/{uuid4()}/voice-sessions/setup", json=operation)
            assert foreign.status_code == 403
        setup = world.client.post(prefix + "/setup", json=operation)
        if boundary not in {"success", "business_facts"}:
            assert setup.status_code in {403, 503}, setup.text
            world.store.expire_all()
            assert (
                world.store.scalar(
                    select(func.count())
                    .select_from(ProviderConnection)
                    .where(ProviderConnection.provider == "vapi", ProviderConnection.status == "READY")
                )
                == 0
            )
            assert world.store.scalar(select(func.count()).select_from(VoiceSession)) == 0
            assert callbacks == []
            await engine.dispose()
            return
        assert setup.status_code == 200, setup.text
        ready = VoicePage.model_validate_json(setup.text)
        assert ready.can_prepare and len(ready.connections) == 1
        from aco.features.usage.models import ProviderUsageEvent, UsageAttempt

        world.store.expire_all()
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(ProviderUsageEvent)
                .where(ProviderUsageEvent.provider_connection_id == ready.connections[0].id)
            )
            == 1
        )
        world.store.rollback()
        assert (
            world.client.post(prefix + "/setup", json=operation).json()["connections"]
            == setup.json()["connections"]
        )
        prepared = world.client.post(
            prefix,
            json={
                "request_id": str(uuid4()),
                "connection_id": str(ready.connections[0].id),
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Review the synthetic invoice and create one internal task",
                **({"demo_scenario": "business_facts"} if boundary == "business_facts" else {}),
            },
        )
        assert prepared.status_code == 201, prepared.text
        view = VoiceSessionView.model_validate_json(prepared.text)
        reviewer, _ = add_actor(world.store, app, "voice-setup-reviewer@synthetic.invalid")
        world.store.add(
            Membership(tenant_id=world.ctx.tenant_id, user_id=reviewer.id, role="finance_manager")
        )
        world.store.commit()
        ctx = world.ctx.model_copy(update={"actor_id": reviewer.id, "role": Role.FINANCE_MANAGER})
        service = HumanApprovalService(world.store)
        approval = service.get(ctx, view.approval_id)
        service.decide_approval(
            ctx,
            view.approval_id,
            ApprovalDecisionCommand(
                expected_version=approval.version,
                reviewed_action_version=approval.action_version,
                reviewed_material_hash=approval.material_hash,
                reviewed_binding_hash=approval.current_binding_hash,
                decision="approved",
                reason="Independent synthetic test review",
            ),
        )
        world.store.commit()
        started = world.client.post(prefix + f"/{view.id}/start", json={"expected_version": view.version})
        assert started.status_code == 200, started.text
        actual = VoiceSessionView.model_validate_json(started.text)
        assert actual.state == "completed" and actual.evidence_mode == "MOCKED_HTTP"
        expected_tools = 5 if boundary == "business_facts" else 2
        assert len(actual.changed_ids) == 1 and len(actual.invocations) == expected_tools
        assert len(callbacks) == expected_tools + 1
        assert [event.kind for event in actual.events] == ["tool.requested"] * expected_tools + [
            "call.completed"
        ]
        if boundary == "business_facts":
            from aco.features.cases.models import CollectionCase
            from aco.features.promises.models import PaymentPromise
            from aco.features.voice.models import VoiceBusinessEvidence

            world.store.rollback()
            assert {o.record_kind for o in actual.outcomes} == {"task", "case", "promise", "hold"}
            assert world.store.scalar(select(func.count()).select_from(VoiceBusinessEvidence)) == 3
            assert world.store.scalar(select(func.count()).select_from(CollectionCase)) == 1
            promise = world.store.scalar(select(PaymentPromise))
            assert promise and promise.status == "NEEDS_CONFIRMATION"
            assert not actual.transcript_available and not actual.recording_available
            world.store.rollback()
        assert world.client.get(prefix + f"/{view.id}").json()["changed_ids"] == started.json()["changed_ids"]
        reconciled = world.client.post(prefix + f"/{view.id}/reconcile")
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["communication_id"] and not reconciled.json()["artifacts"]
        assert (
            world.client.post(prefix + f"/{view.id}/reconcile").json()["communication_id"]
            == reconciled.json()["communication_id"]
        )
        world.store.expire_all()
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == (
            4 if boundary == "business_facts" else 1
        )
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(ProviderUsageEvent)
                .where(ProviderUsageEvent.provider_connection_id == ready.connections[0].id)
            )
            == 2
        )
        attempts = list(
            world.store.scalars(
                select(UsageAttempt).where(UsageAttempt.provider_connection_id == ready.connections[0].id)
            )
        )
        assert {attempt.operation for attempt in attempts} == {"probe_assistant", "start_browser"}
        assert all(
            attempt.outcome == "SUCCESS" and attempt.business_outcome == "COMMITTED" for attempt in attempts
        )
        await engine.dispose()
