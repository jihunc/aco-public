"""Actual source, independent review and canonical offline email worker; no live adapter."""

import asyncio
from dataclasses import replace
from datetime import UTC
from uuid import UUID, uuid4

import pytest
from artifact_support import storage_configuration
from fastapi import FastAPI
from policy_support import activate, configure_recipients, policy_world, save
from sqlalchemy import func, select

from aco.core.settings import Settings
from aco.features.communications.ingress import EmailIngressService
from aco.features.communications.models import Communication, EmailSubmission
from aco.features.communications.outbound import ReminderDispatcher
from aco.features.identity.models import ProviderConnection, now
from aco.integrations.demo_email.adapter import DemoEmailRuntime
from aco.integrations.s3.adapter import StorageRuntime
from aco.worker.app import WorkerRuntime
from aco.worker.jobs import DomainExecutor


def wave(app: FastAPI) -> None:
    """Dispatch actual registered jobs; library claims/heartbeat, never forged job state."""
    from aco.features.agent.graph import dispatch_agent_runs

    async def work() -> None:
        dispatcher = ReminderDispatcher(
            app.state.business_sessions, app.state.settings, app.state.ports.email, app.state.ports.storage
        )
        runtime = WorkerRuntime(app.state.settings, DomainExecutor(dispatcher))
        try:
            async with runtime.app.open_async():
                await dispatch_agent_runs(runtime.sessions, runtime.app)
                await runtime.dispatcher.dispatch_outbox(100)
                await runtime.app.run_worker_async(
                    queues=["aco"],
                    concurrency=1,
                    wait=False,
                    listen_notify=False,
                    install_signal_handlers=False,
                )
        finally:
            await runtime.engine.dispose()

    asyncio.run(work())


@pytest.mark.parametrize(
    "mode", ["pending", "approved", "modified", "revoked", "unknown", "crash_native", "crash_observed"]
)
def test_real_graph_email_draft_waits_for_independent_approval_without_submitting(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    uncertain = mode in {"unknown", "crash_native", "crash_observed"}
    with policy_world() as (client, store, ctx, invoices, saved):
        app = client.app
        assert isinstance(app, FastAPI)
        storage = StorageRuntime(**storage_configuration())
        try:
            app.state.settings = Settings.model_validate(
                app.state.settings.model_dump()
                | {"synthetic_email_enabled": True, "outbound_enabled": True, "kill_switch": False}
            )
            app.state.ports = replace(
                app.state.ports, storage=storage, email=DemoEmailRuntime(app.state.business_sessions)
            )
            app.state.email_ingress = EmailIngressService(
                app.state.business_sessions,
                enabled=True,
                default_secret="whsec_c3ludGhldGljLWVtYWlsLXByZXBhcmF0aW9u",
                storage_runtime=storage,
                environment="local",
            )
            base = f"/api/workspaces/{ctx.tenant_id}"
            prepared = client.post(
                base + "/email-ingress/setup",
                json={
                    "request_id": str(uuid4()),
                    "evidence": "Explicit offline email test uses actual APP probe; no receiving activation",
                },
            )
            assert prepared.status_code == 200, prepared.text
            from aco.features.accounting.models import CustomerContact

            store.rollback()
            contact = store.scalar(
                select(CustomerContact).where(CustomerContact.customer_id == invoices[0].customer_id)
            )
            assert contact is not None and contact.email is not None
            response = client.put(
                base + "/email-execution",
                json={
                    "expected_activation_id": None,
                    "sender": "collections@aco.example.test",
                    "destinations": [contact.email],
                    "request_cap": 4,
                    "synthetic_capture_reviewed": True,
                    "sender_and_destinations_reviewed": True,
                    "evidence": "Owner authorizes only offline synthetic reviewed email and private original",
                },
            )
            assert response.status_code == 200, response.text
            store.rollback()
            configure_recipients(client, store, ctx)
            # Actual Owner-reviewed synthetic recipient timezone places this isolated
            # send in its normal 09:00-17:00 window; production clock is not patched.
            from datetime import datetime

            offset = 12 - datetime.now(UTC).hour
            zone = "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
            recipients = client.get(base + "/policy-recipients").json()
            for recipient in recipients:
                response = client.put(
                    base + f"/policy-recipients/{recipient['contact_id']}",
                    json={
                        "expected_version": recipient["version"],
                        "expected_contact_revision": recipient["source_revision"],
                        "expected_destination": recipient["destination"],
                        "timezone": zone,
                        "timezone_verified": True,
                        "owner_confirmed_tenant_timezone": False,
                        "destination_verified": True,
                        "allowed_channels": ["email"],
                        "opt_out": False,
                        "evidence": "Explicit synthetic owner timezone for contained worker proof",
                    },
                )
                assert response.status_code == 200, response.text
            definition = {
                **saved["definition"],
                "autonomy": "draft_and_review",
                "weekdays": list(range(5)),
                "opens_at": "09:00",
                "closes_at": "17:00",
            }
            definition["rules"] = [{**definition["rules"][0], "offset_days": 120}]
            saved = save(
                client,
                ctx.tenant_id,
                policy_id=saved["policy_id"],
                name=saved["name"],
                expected_version=saved["version"],
                definition=definition,
            )
            activate(client, ctx, saved)
            paused = client.post(base + "/automation/pause", json={"scope": "tenant", "paused": False})
            assert paused.status_code == 200, paused.text
            from aco.features.accounting.models import InvoiceSnapshot

            store.rollback()
            financial_before = [
                (i.id, i.source_revision, i.balance_minor, i.financial_status)
                for i in store.scalars(select(InvoiceSnapshot).order_by(InvoiceSnapshot.id))
            ]
            store.rollback()
            response = client.post(
                base + "/agent-runs",
                json={
                    "invoice_ids": [str(invoices[0].id)],
                    "objective": "Prepare a synthetic customer email and request independent approval",
                    "request_id": str(uuid4()),
                },
            )
            assert response.status_code == 202, response.text
            run_id = UUID(response.json()["id"])
            store.rollback()
            wave(app)
            run = client.get(base + f"/agent-runs/{run_id}").json()
            assert run["status"] == "awaiting_approval", run
            proposal = next(p for p in run["result"]["proposals"] if p["kind"] == "request_email_send")
            assert proposal["approval_id"] and not proposal["executed"]
            approval = client.get(base + f"/approvals/{proposal['approval_id']}")
            assert approval.status_code == 200, approval.text
            assert approval.json()["proposal"]["destination"] == contact.email
            assert approval.json()["proposal"]["content"] == proposal["email_command"]["content"]
            assert approval.json()["can_approve"] is False
            store.rollback()
            assert store.scalar(select(func.count()).select_from(Communication)) == 0
            assert store.scalar(select(func.count()).select_from(EmailSubmission)) == 0
            assert all(
                c.status == "READY"
                for c in store.scalars(
                    select(ProviderConnection).where(
                        ProviderConnection.provider_type.in_(["storage", "email"])
                    )
                )
            )

            if mode != "pending":
                from identity_support import add_actor

                from aco.core.types import Role
                from aco.features.identity.models import Membership
                from aco.features.review.approval_dto import ApprovalDecisionCommand
                from aco.features.review.approvals import HumanApprovalService

                user, _ = add_actor(store, app, "independent-email@synthetic.invalid")
                store.add(Membership(tenant_id=ctx.tenant_id, user_id=user.id, role="finance_manager"))
                store.commit()
                reviewer = ctx.model_copy(update={"actor_id": user.id, "role": Role.FINANCE_MANAGER})
                if mode == "modified":
                    view_before = client.get(base + f"/approvals/{proposal['approval_id']}").json()
                    edited = client.post(
                        base + f"/approvals/{proposal['approval_id']}/modifications",
                        json={
                            "expected_version": view_before["version"],
                            "reviewed_action_version": view_before["action_version"],
                            "reviewed_material_hash": view_before["material_hash"],
                            "reviewed_binding_hash": view_before["current_binding_hash"],
                            "reason": "Owner corrects synthetic wording; independent review required",
                            "edit": {
                                "kind": "outbound_content",
                                "content": "SYNTHETIC EDIT: Please contact our review team.",
                            },
                        },
                    )
                    assert edited.status_code == 200, edited.text
                    proposal["approval_id"] = edited.json()["id"]
                    store.rollback()
                service = HumanApprovalService(store)
                view = service.get(reviewer, UUID(proposal["approval_id"]))
                assert view.can_approve, view.reasons
                service.decide_approval(
                    reviewer,
                    view.id,
                    ApprovalDecisionCommand(
                        expected_version=view.version,
                        reviewed_action_version=view.action_version,
                        reviewed_material_hash=view.material_hash,
                        reviewed_binding_hash=view.current_binding_hash,
                        decision="approved",
                        reason="Independent exact synthetic text/source/policy review",
                    ),
                )
                store.commit()
                if mode == "revoked":
                    member = store.scalar(
                        select(Membership).where(
                            Membership.user_id == user.id, Membership.tenant_id == ctx.tenant_id
                        )
                    )
                    assert member is not None
                    member.active = False
                    member.version += 1
                    store.commit()
                probe_errors: list[str] = []
                if mode == "approved":
                    from collections.abc import Awaitable, Callable

                    from fastapi import HTTPException
                    from sqlalchemy.orm import Session

                    from aco.core.context import ServiceContext
                    from aco.features.actions.models import ScheduledAction
                    from aco.features.identity.service import IdentityService, Permission

                    original_prepare: Callable[
                        [ReminderDispatcher, ServiceContext, UUID], Awaitable[tuple[UUID, UUID]]
                    ] = ReminderDispatcher.prepare

                    async def checked_prepare(
                        dispatcher: ReminderDispatcher, authority: ServiceContext, action_id: UUID
                    ) -> tuple[UUID, UUID]:
                        async with dispatcher.sessions.begin() as session:

                            def refuse(store: Session) -> None:
                                for update in (
                                    {"tenant_id": uuid4()},
                                    {"actor_id": uuid4()},
                                    {"action_id": uuid4()},
                                    {"action_version": authority.action_version + 1},
                                    {"job_id": (authority.job_id or 0) + 1000},
                                    {"lease_token": uuid4()},
                                ):
                                    with pytest.raises(HTTPException):
                                        IdentityService(store).authorize(
                                            authority.model_copy(update=update), Permission.INVOICE_READ
                                        )
                                with pytest.raises(HTTPException):
                                    IdentityService(store).authorize(authority, Permission.TASK_WRITE)
                                current = store.get(ScheduledAction, action_id)
                                assert current is not None
                                for key, value in (
                                    ("destination", "forged@example.test"),
                                    ("rule_id", str(uuid4())),
                                    ("email_activation_id", str(uuid4())),
                                ):
                                    from sqlalchemy.exc import DBAPIError

                                    with pytest.raises(DBAPIError, match="immutable action authority"):
                                        with store.begin_nested():
                                            current.command = {**current.command, key: value}
                                            store.flush()

                            try:
                                await session.run_sync(refuse)
                            except Exception as error:
                                import traceback

                                probe_errors.append(
                                    type(error).__name__
                                    + ": "
                                    + str(error)
                                    + " "
                                    + str(traceback.extract_tb(error.__traceback__)[-3:])
                                )
                                raise
                        return await original_prepare(dispatcher, authority, action_id)

                    monkeypatch.setattr(ReminderDispatcher, "prepare", checked_prepare)
                if mode == "unknown":
                    from aco.core.types import ProviderErrorCode, ProviderFailure
                    from aco.integrations.demo_email.adapter import DemoEmailAdapter
                    from aco.integrations.ports.email import EmailFailure

                    original = DemoEmailAdapter.send_email

                    from aco.core.context import ActorContext
                    from aco.integrations.ports.email_dto import DeliveryAttempt, OutboundEmail

                    async def accepted_then_uncertain(
                        self: DemoEmailAdapter, source: ActorContext, command: OutboundEmail
                    ) -> DeliveryAttempt:
                        await original(self, source, command)
                        raise EmailFailure(
                            ProviderFailure(
                                code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
                                retryable=False,
                                may_have_side_effect=True,
                            )
                        )

                    monkeypatch.setattr(DemoEmailAdapter, "send_email", accepted_then_uncertain)
                if mode.startswith("crash_"):
                    import multiprocessing
                    import time

                    # The child owns both the genuine graph resume and email job;
                    # a parent maintenance wave could already dispatch the effect.
                    store.rollback()
                    assert store.scalar(select(func.count()).select_from(EmailSubmission)) == 0
                    store.rollback()
                    child = multiprocessing.get_context("spawn").Process(
                        target=crash_email_worker,
                        args=(app.state.settings.model_dump(), mode),
                    )
                    child.start()
                    try:
                        child.join(timeout=40)
                        if child.exitcode != 74:
                            from aco.features.actions.models import ScheduledAction

                            store.rollback()
                            diagnostic = [
                                (a.kind, a.status, a.last_error, a.attempts)
                                for a in store.scalars(
                                    select(ScheduledAction).where(ScheduledAction.kind == "agent_email")
                                )
                            ]
                            store.rollback()
                            assert child.exitcode == 74, (child.exitcode, diagnostic)
                    finally:
                        if child.is_alive():
                            child.kill()
                            child.join(timeout=5)
                    from aco.integrations.demo_email.models import NativeEmailOperation

                    store.rollback()
                    assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 1
                    submission = store.scalar(select(EmailSubmission))
                    assert submission is not None
                    assert submission.state == ("pending" if mode == "crash_native" else "accepted")
                    store.rollback()
                    time.sleep(4)  # Genuine dead-worker heartbeat age, no DB state mutation.
                    assert asyncio.run(recover_email_worker(app)) == 1
                for _ in range(3):
                    wave(app)
                assert not probe_errors, probe_errors
                finished = client.get(base + f"/agent-runs/{run_id}").json()
                assert finished["status"] == "completed", finished
                store.rollback()
                messages = list(store.scalars(select(Communication)))
                if mode == "revoked":
                    assert messages == []
                    assert store.scalar(select(func.count()).select_from(EmailSubmission)) == 0
                else:
                    from aco.features.actions.models import ScheduledAction
                    from aco.features.artifacts.models import Artifact
                    from aco.features.communications.models import CommunicationSource
                    from aco.features.review.models import ApprovalRequest
                    from aco.integrations.demo_email.models import NativeEmailOperation

                    assert len(messages) == 1, [
                        (a.status, a.last_error)
                        for a in store.scalars(
                            select(ScheduledAction).where(ScheduledAction.kind == "agent_email")
                        )
                    ]
                    assert messages[0].delivery_state == ("unknown_outcome" if uncertain else "accepted")
                    assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 1
                    assert store.scalar(select(func.count()).select_from(CommunicationSource)) == 1
                    assert (
                        store.scalar(
                            select(func.count())
                            .select_from(Artifact)
                            .where(Artifact.storage_state == "stored")
                        )
                        == 1
                    )
                    expected = (
                        "SYNTHETIC EDIT: Please contact our review team."
                        if mode == "modified"
                        else proposal["email_command"]["content"]
                    )
                    assert messages[0].content["text_body"] == expected
                    request = store.get(ApprovalRequest, UUID(proposal["approval_id"]))
                    assert request is not None
                    action = store.get(ScheduledAction, request.action_id)
                    assert action is not None
                    assert action.status == ("unknown_outcome" if uncertain else "succeeded")
                    assert not action.automatic_resend
                    from aco.features.agent.models import AgentStep

                    step = store.get(AgentStep, action.agent_step_id)
                    assert step is not None and step.status == ("failed" if uncertain else "completed")
                    source = store.scalar(
                        select(CommunicationSource).where(
                            CommunicationSource.communication_id == messages[0].id
                        )
                    )
                    assert source is not None and source.representation == "ACO_COMPOSED"
                    store.rollback()
                    raw = client.get(base + f"/artifacts/{source.artifact_id}/content")
                    assert raw.status_code == 200
                    from email.parser import BytesParser
                    from email.policy import default

                    parsed = BytesParser(policy=default).parsebytes(raw.content)
                    assert expected in parsed.get_content()
                    store.rollback()
                    assert [
                        (i.id, i.source_revision, i.balance_minor, i.financial_status)
                        for i in store.scalars(select(InvoiceSnapshot).order_by(InvoiceSnapshot.id))
                    ] == financial_before

                    if uncertain:
                        from aco.features.policies.models import ContactReservation
                        from aco.integrations.demo_email.models import NativeEmailInvocation

                        contact = store.scalar(select(ContactReservation))
                        assert contact is not None and contact.status == "unknown_outcome"
                        assert contact.submitted_at is not None
                        assert contact.evidence["reconciliation_required"] is True
                        # Advance only the synthetic vendor's key clock. Application
                        # authority, source dates and job/lease state are unchanged.
                        from datetime import timedelta

                        from aco.integrations.demo_email import adapter as native_adapter

                        beyond_vendor_ttl = now() + timedelta(days=2)
                        monkeypatch.setattr(native_adapter, "now", lambda: beyond_vendor_ttl)
                        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1
                    if not uncertain:
                        assert request.consumed_at is not None
                    store.rollback()
                    wave(app)
                    store.rollback()
                    assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 1
                    assert store.scalar(select(func.count()).select_from(Communication)) == 1
                    if uncertain:
                        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1
        finally:
            from aco.features.artifacts.models import Artifact
            from aco.features.artifacts.service import object_ref

            store.rollback()
            for artifact in store.scalars(select(Artifact).where(Artifact.tenant_id == ctx.tenant_id)):
                if artifact.storage_state == "stored":
                    asyncio.run(
                        storage.bind(ctx.tenant_id, artifact.provider_connection_id).delete_object(
                            ctx, object_ref(artifact)
                        )
                    )


def crash_email_worker(configuration: dict[str, object], cut: str) -> None:
    """Spawned process exits at an actual independently committed acceptance cut."""
    import os

    from pytest import MonkeyPatch

    from aco.core.context import ActorContext, ServiceContext
    from aco.core.types import ActionResult
    from aco.features.actions.service import ActionLease, ActionService
    from aco.integrations.demo_email.adapter import DemoEmailAdapter
    from aco.integrations.ports.email_dto import DeliveryAttempt, OutboundEmail

    send = DemoEmailAdapter.send_email
    complete = ActionService.complete_action

    async def native_cut(
        adapter: DemoEmailAdapter, ctx: ActorContext, command: OutboundEmail
    ) -> DeliveryAttempt:
        result = await send(adapter, ctx, command)
        os._exit(74)
        return result

    def observed_cut(
        service: ActionService, ctx: ServiceContext, lease: ActionLease, result: ActionResult
    ) -> None:
        if result.provider_ref is not None and result.status.value == "succeeded":
            os._exit(74)
        complete(service, ctx, lease, result)

    async def work() -> None:
        settings = Settings.model_validate(configuration)
        runtime = WorkerRuntime(settings)
        runtime.executor = DomainExecutor(
            ReminderDispatcher(
                runtime.sessions,
                settings,
                DemoEmailRuntime(runtime.sessions),
                StorageRuntime(**storage_configuration()),
            )
        )
        await runtime.run()

    with MonkeyPatch.context() as patch:
        if cut == "crash_native":
            patch.setattr(DemoEmailAdapter, "send_email", native_cut)
        else:
            patch.setattr(ActionService, "complete_action", observed_cut)
        asyncio.run(work())


async def recover_email_worker(app: FastAPI) -> int:
    from aco.worker.recovery import recover_stalled

    runtime = WorkerRuntime(app.state.settings)
    try:
        async with runtime.app.open_async():
            return await recover_stalled(runtime.app, runtime.sessions)
    finally:
        await runtime.engine.dispose()
