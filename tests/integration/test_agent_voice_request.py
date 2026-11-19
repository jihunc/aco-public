"""Actual SDK/maintained-worker voice preparation; no provider call or automatic start."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from action_support import action_world
from evaluations.test_collection_agent import proposal, run_sdk
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.context import RunAuthority
from aco.features.agent.models import AgentStep
from aco.features.agent.nodes import InvestigationNode
from aco.features.agent.schemas import GraphState, Proposal
from aco.features.review.models import ApprovalRequest
from aco.features.tools.gateway import ExecutedAction, execute_internal
from aco.features.voice.agent_request import RequestVoiceCall
from aco.features.voice.demo import OfflineVoiceDemo, preparation_runtime
from aco.features.voice.models import VoiceInvocation, VoiceSession
from aco.features.voice.runtime import VoiceRuntime


def test_sdk_voice_request_persists_review_and_never_starts_call(monkeypatch: pytest.MonkeyPatch) -> None:
    original = InvestigationNode.execute
    repetitions = 0

    async def replay(self: InvestigationNode, state: GraphState) -> GraphState:
        nonlocal repetitions
        async with self.sessions.begin() as db:
            reads = list(
                await db.scalars(
                    select(AgentStep).where(
                        AgentStep.run_id == self.authority.run_id,
                        AgentStep.kind == "read",
                        AgentStep.status == "completed",
                    )
                )
            )
            invoice_id = reads[0].invoice_id
            assert invoice_id is not None
            command = RequestVoiceCall(
                invoice_id=invoice_id, invoice_ids=[invoice_id], objective="Refusal probe"
            )
            for authority, request, expected in (
                (replace(self.authority, tenant_id=uuid4()), command, "RUN_AUTHORITY_DENIED"),
                (replace(self.authority, actor_id=uuid4()), command, "RUN_AUTHORITY_REVOKED"),
                (replace(self.authority, lease_token=uuid4()), command, "RUN_AUTHORITY_REVOKED"),
                (
                    self.authority,
                    command.model_copy(update={"invoice_ids": [invoice_id, uuid4()]}),
                    "VOICE_COMMAND_OUTSIDE_RUN_SCOPE",
                ),
                (
                    self.authority,
                    command.model_copy(update={"invoice_ids": [invoice_id, invoice_id]}),
                    "VOICE_COMMAND_OUTSIDE_RUN_SCOPE",
                ),
            ):
                with pytest.raises(HTTPException) as refused:

                    def probe(
                        store: Session,
                        authority: RunAuthority = authority,
                        request: RequestVoiceCall = request,
                    ) -> ExecutedAction:
                        return execute_internal(
                            store,
                            authority,
                            request,
                            999,
                            invoice_id,
                            [r.id for r in reads],
                            voice_runtime=self.voice_runtime,
                        )

                    await db.run_sync(probe)
                assert refused.value.status_code == 403 and refused.value.detail == expected
        first = await original(self, state)
        second = await original(self, state)
        repetitions += 1
        assert first == second
        return second

    monkeypatch.setattr(InvestigationNode, "execute", replay)
    with action_world() as world:
        secret = SecretStr("explicit-synthetic-agent-voice-test")

        async def forbidden_callback(raw: bytes, headers: dict[str, str]) -> None:
            raise AssertionError("Preparation cannot deliver a call or callback")

        def runtime(sessions: async_sessionmaker[AsyncSession]) -> VoiceRuntime:
            return OfflineVoiceDemo(
                sessions, capability_secret=secret, callback_sender=forbidden_callback, enabled=True
            ).runtime

        world.client.app.state.voice_runtime = runtime(world.client.app.state.business_sessions)
        base = f"/api/workspaces/{world.ctx.tenant_id}"
        setup = world.client.post(base + "/voice-sessions/setup", json={"request_id": str(uuid4())})
        assert setup.status_code == 200, setup.text
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        command = proposal(
            invoice,
            "request_voice_call",
            voice_command={
                "invoice_id": str(invoice.id),
                "invoice_ids": [str(invoice.id)],
                "objective": "Request an independently reviewed browser conversation",
            },
        )
        saved = run_sdk(world, [invoice], [command], voice_runtime_factory=runtime)
        assert saved["status"] == "completed" and repetitions == 1, saved
        outcome = saved["result"]["proposals"][0]
        assert outcome["executed"] is True and outcome["changed_ids"] == []
        assert outcome["approval_id"] and outcome["voice_session_id"]
        world.store.rollback()
        session = world.store.get(VoiceSession, UUID(outcome["voice_session_id"]))
        approval = world.store.get(ApprovalRequest, UUID(outcome["approval_id"]))
        action = world.store.get(ScheduledAction, UUID(outcome["action_id"]))
        assert session is not None and approval is not None and action is not None
        assert session.state == "awaiting_approval" and session.provider_call_id is None
        assert session.initiator_id == approval.initiator_id == world.ctx.actor_id
        assert session.approval_id == approval.id and approval.status == "pending"
        assert approval.proposal["capture"] == "DISABLED"
        assert action.agent_run_id == UUID(saved["id"]) and action.status == "succeeded"
        assert action.result is not None
        assert action.result["voice_session_id"] == str(session.id)
        step = world.store.get(AgentStep, action.agent_step_id)
        assert step is not None and step.tool_name == "request_voice_call"
        assert step.output == action.result
        assert world.store.scalar(select(func.count()).select_from(VoiceSession)) == 1
        assert world.store.scalar(select(func.count()).select_from(VoiceInvocation)) == 0
        world.store.rollback()
        # Approval remains independently reviewed and never starts a call on decision.
        from integration.test_approval_scope import reviewer

        from aco.features.review.approval_dto import ApprovalDecisionCommand
        from aco.features.review.approvals import HumanApprovalService

        svc = HumanApprovalService(world.store)
        own = svc.get(world.ctx, approval.id)
        assert not own.can_approve
        ctx = reviewer(world)
        view = svc.get(ctx, approval.id)
        assert view.can_approve
        approved = svc.decide_approval(
            ctx,
            approval.id,
            ApprovalDecisionCommand(
                expected_version=view.version,
                reviewed_action_version=view.action_version,
                reviewed_material_hash=view.material_hash,
                reviewed_binding_hash=view.current_binding_hash,
                decision="approved",
                reason="Independent review of exact synthetic browser scope",
            ),
        )
        world.store.commit()
        assert approved.approval_valid
        world.store.refresh(session)
        assert session.provider_call_id is None and session.state == "awaiting_approval"
        world.store.rollback()
        detail = world.client.get(base + "/voice-sessions/" + str(session.id))
        assert detail.status_code == 200 and detail.json()["state"] == "awaiting_approval"


@pytest.mark.parametrize("refusal", ["missing_runtime", "missing_ready_connection", "unselected_invoice"])
def test_sdk_voice_request_refuses_without_current_preparation_authority(refusal: str) -> None:
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        command = proposal(
            invoice,
            "request_voice_call",
            voice_command={
                "invoice_id": str(invoice.id),
                "invoice_ids": [str(invoice.id)]
                + ([str(uuid4())] if refusal == "unselected_invoice" else []),
                "objective": "Prepare only under current voice authority",
            },
        )
        factory = (
            None
            if refusal == "missing_runtime"
            else lambda sessions: preparation_runtime(
                sessions,
                capability_secret=SecretStr("synthetic-preparation-test-secret"),
                environment="local",
            )
        )
        saved = run_sdk(world, [invoice], [command], voice_runtime_factory=factory)
        assert saved["status"] == "failed", saved
        expected = {
            "missing_runtime": "VOICE_PREPARATION_RUNTIME_UNAVAILABLE",
            "missing_ready_connection": "VOICE_REVIEW_CONNECTION_UNAVAILABLE",
            "unselected_invoice": "VOICE_COMMAND_OUTSIDE_RUN_SCOPE",
        }[refusal]
        assert saved["error_code"] == expected
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(VoiceSession)) == 0
        assert world.store.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
        assert world.store.scalar(select(func.count()).select_from(VoiceInvocation)) == 0


def test_voice_request_does_not_accept_provider_or_capture_authority() -> None:
    body = {
        "invoice_id": str(uuid4()),
        "kind": "request_voice_call",
        "reason": "Review only",
        "voice_command": {
            "invoice_id": str(uuid4()),
            "invoice_ids": [str(uuid4())],
            "objective": "Review only",
        },
    }
    for key, value in (
        ("connection_id", str(uuid4())),
        ("consent_id", str(uuid4())),
        ("capture", True),
        ("destination", "+10000000000"),
    ):
        unsafe = {
            "invoice_id": body["invoice_id"],
            "invoice_ids": [body["invoice_id"]],
            "objective": "Review only",
            key: value,
        }
        with pytest.raises(ValidationError):
            Proposal.model_validate_json(__import__("json").dumps(body | {"voice_command": unsafe}))


def test_public_fake_voice_request_uses_explicit_worker_composition_without_start() -> None:
    import asyncio

    from aco.core.settings import Settings
    from aco.features.agent.graph import dispatch_agent_runs
    from aco.worker.app import WorkerRuntime

    with action_world() as world:
        secret = SecretStr("synthetic-worker-voice-secret-at-least-32-chars")

        async def forbidden_callback(raw: bytes, headers: dict[str, str]) -> None:
            raise AssertionError("An agent request cannot execute a call")

        demo = OfflineVoiceDemo(
            world.client.app.state.business_sessions,
            capability_secret=secret,
            callback_sender=forbidden_callback,
            enabled=True,
        )
        world.client.app.state.voice_runtime = demo.runtime
        base = f"/api/workspaces/{world.ctx.tenant_id}"
        setup = world.client.post(base + "/voice-sessions/setup", json={"request_id": str(uuid4())})
        assert setup.status_code == 200, setup.text
        started = world.client.post(
            base + "/agent-runs",
            json={
                "request_id": str(uuid4()),
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Request an independently reviewed synthetic browser voice session",
            },
        )
        assert started.status_code == 202, started.text
        world.store.rollback()

        async def work() -> None:
            runtime = WorkerRuntime(
                Settings(
                    database_url=SecretStr(world.url), session_secret=secret, synthetic_voice_enabled=True
                )
            )
            try:
                async with runtime.app.open_async():
                    await dispatch_agent_runs(runtime.sessions, runtime.app)
                    await asyncio.wait_for(
                        runtime.app.run_worker_async(
                            queues=["aco"], concurrency=1, wait=False, listen_notify=False
                        ),
                        timeout=35,
                    )
            finally:
                await runtime.engine.dispose()

        asyncio.run(work())
        saved = world.client.get(base + "/agent-runs/" + started.json()["id"])
        assert saved.status_code == 200 and saved.json()["status"] == "completed", saved.text
        result = saved.json()["result"]["proposals"][0]
        assert result["kind"] == "request_voice_call" and result["executed"]
        row = world.store.get(VoiceSession, UUID(result["voice_session_id"]))
        assert row is not None and row.provider_call_id is None and row.state == "awaiting_approval"
        assert world.store.scalar(select(func.count()).select_from(VoiceInvocation)) == 0
        assert demo.requests == {}
