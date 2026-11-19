"""Contained provider ingress composition; all external HTTP is explicitly mocked."""

from typing import NoReturn
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_disabled_provider_binding_never_resolves_credentials() -> None:
    from aco.features.communications.provider_ingress import ProviderIngressRuntime

    def forbidden(*args: object) -> NoReturn:
        pytest.fail("disabled provider ingress must not resolve capability or credentials")

    runtime = ProviderIngressRuntime(async_sessionmaker[AsyncSession](), forbidden, forbidden)
    with pytest.raises(HTTPException) as refused:
        runtime.capability(uuid4(), uuid4())
    assert refused.value.detail == "PROVIDER_INGRESS_DISABLED"


@pytest.mark.parametrize(
    "boundary",
    [
        "success",
        "disabled",
        "rotation",
        "membership",
        "retention",
        "archive_rotation",
        "transport_failure",
        "intent_rotation",
        "intent_removal",
        "wire_intent_rotation",
    ],
)
async def test_signed_mocked_http_receipt_runs_canonical_capture_and_fake_task(
    boundary: str, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    import json
    from datetime import timedelta
    from hashlib import sha256
    from typing import cast

    import httpx
    from fastapi import FastAPI
    from ingress_support import ingress_settings, ingress_world
    from pydantic import SecretStr
    from sqlalchemy import func, select
    from svix.webhooks import Webhook

    from aco.features.agent.graph import dispatch_agent_runs
    from aco.features.agent.models import AgentRun
    from aco.features.artifacts.models import Artifact, RetentionPolicy
    from aco.features.communications.ingress import EmailIngressService, dispatch_email_ingress
    from aco.features.communications.ingress_dto import ActivateIngress
    from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
    from aco.features.communications.provider_ingress import ProviderIngressCapability, ProviderIngressRuntime
    from aco.features.identity.models import Membership, ProviderConnection, now
    from aco.features.review.models import CollectionTask
    from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageRevision
    from aco.integrations.ports.email import EmailPort
    from aco.integrations.ports.email_observer import EmailRequestObserver
    from aco.integrations.resend.runtime import ResendRuntime
    from aco.integrations.resend.settings import ResendBinding
    from aco.worker.app import WorkerRuntime

    with ingress_world() as lane:
        world = lane.world
        original = lane.message()
        assert original.source.content is not None
        native = uuid4()
        connection = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            encrypted_credentials_ref="offline-credential-revision-1",
        )
        policy = RetentionPolicy(
            tenant_id=world.ctx.tenant_id,
            policy_key="synthetic_30_days",
            synthetic=True,
            retention_days=30,
            authority="synthetic_only_no_real_capture_authority",
        )
        world.store.add_all([connection, policy])
        world.store.commit()
        assert connection.encrypted_credentials_ref is not None
        assert original.received_at is not None
        cap = ProviderIngressCapability(
            tenant_id=world.ctx.tenant_id,
            connection_id=connection.id,
            credential_revision=connection.encrypted_credentials_ref,
            inbox_addresses=original.to,
            webhook_secret_sha256=sha256(lane.secret.encode()).hexdigest(),
            retention_policy_id=policy.id,
            retention_policy_version=policy.version,
        )
        active_cap = cap
        binding_available = True

        def resolve_cap(tenant: UUID, key: UUID) -> ProviderIngressCapability:
            if not binding_available:
                raise KeyError("offline capability removed")
            return active_cap

        from aco.features.accounting.models import InvoiceSnapshot

        financial = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert financial is not None
        source_facts = (financial.balance_minor, financial.source_revision, financial.financial_status)
        from sqlalchemy import event
        from sqlalchemy.orm import Session

        from aco.features.artifacts.service import ArtifactService
        from aco.integrations.ports.storage import StorageInvocationContext

        audited = ArtifactService._audit
        intent_barriers: list[UUID] = []

        async def after_intent(
            self: ArtifactService,
            db: AsyncSession,
            ctx: StorageInvocationContext,
            action: str,
            target: UUID,
            details: dict[str, str | bool] | None = None,
        ) -> None:
            await audited(self, db, ctx, action, target, details)
            if action == "artifact.capture_intent" and boundary in {"intent_rotation", "intent_removal"}:

                def rotate_after_commit(session: Session) -> None:
                    nonlocal active_cap, binding_available
                    intent_barriers.append(target)
                    if boundary == "intent_removal":
                        binding_available = False
                    else:
                        active_cap = cap.model_copy(
                            update={"credential_revision": "offline-after-intent-rotation"}
                        )

                event.listen(db.sync_session, "after_commit", rotate_after_commit, once=True)

        monkeypatch.setattr(ArtifactService, "_audit", after_intent)
        if boundary == "wire_intent_rotation":

            def after_storage_flush(session: Session, flush_context: object) -> None:
                if any(
                    isinstance(row, UsageAttempt)
                    and row.tenant_id == world.ctx.tenant_id
                    and row.operation == "put_private_artifact"
                    for row in session.new
                ):

                    def rotate_wire(session: Session) -> None:
                        nonlocal active_cap
                        intent_barriers.append(uuid4())
                        active_cap = cap.model_copy(
                            update={"credential_revision": "offline-after-storage-intent"}
                        )

                    event.listen(session, "after_commit", rotate_wire, once=True)

            event.listen(Session, "after_flush", after_storage_flush)
            request.addfinalizer(lambda: event.remove(Session, "after_flush", after_storage_flush))

        calls: list[httpx.Request] = []
        authority_probes: list[str] = []

        def transport(request: httpx.Request) -> httpx.Response:
            nonlocal active_cap
            calls.append(request)
            if boundary == "transport_failure":
                raise httpx.ConnectError("mocked transport unavailable", request=request)
            if request.url.host == "api.resend.com":
                assert original.received_at is not None
                assert request.headers["authorization"] == "Bearer re_mocked_not_a_key"
                return httpx.Response(
                    200,
                    headers={"x-request-id": "mocked-native-request"},
                    json={
                        "id": str(native),
                        "from": original.sender,
                        "to": list(original.to),
                        "subject": original.subject,
                        "text": original.text_body,
                        "html": None,
                        "created_at": original.received_at.isoformat(),
                        "message_id": original.message_id,
                        "headers": dict(original.headers),
                        "attachments": [],
                        "raw": {
                            "download_url": "https://source.resend.test/raw?signature=never-persist",
                            "expires_at": (now() + timedelta(hours=1)).isoformat(),
                        },
                    },
                )
            assert request.url.host == "source.resend.test" and "authorization" not in request.headers
            if boundary == "archive_rotation":
                active_cap = cap.model_copy(update={"credential_revision": "offline-rotation-2"})
            return httpx.Response(200, content=original.source.content)

        from aco.features.communications.provider_ingress import Authority, EmailContext
        from aco.integrations.ports.email_context import EmailIngressContext

        def factory(
            material: ProviderIngressCapability, authority: Authority, observer: EmailRequestObserver
        ) -> EmailPort:
            async def checked(ctx: EmailContext, operation: str, value: object | None) -> None:
                if boundary == "success" and isinstance(ctx, EmailIngressContext) and not authority_probes:
                    for field in ("tenant_id", "actor_id", "receipt_id", "lease_token"):
                        with pytest.raises(HTTPException):
                            await authority(ctx.model_copy(update={field: uuid4()}), operation, value)
                        authority_probes.append(field)
                    with pytest.raises(HTTPException):
                        await authority(ctx, "send_email", value)
                    authority_probes.append("send_email")
                await authority(ctx, operation, value)

            return ResendRuntime(
                lambda tenant, key: ResendBinding(
                    tenant_id=tenant,
                    connection_id=key,
                    api_key=SecretStr("re_mocked_not_a_key"),
                    inbox_addresses=material.inbox_addresses,
                    download_hosts=frozenset({"source.resend.test"}),
                    webhook_secret=SecretStr(lane.secret),
                    evidence_mode="MOCKED_HTTP",
                ),
                enabled=True,
                authority=checked,
                observer=observer,
                transport_factory=lambda: httpx.MockTransport(transport),
            ).bind(material.tenant_id, material.connection_id)

        app = cast(FastAPI, world.client.app)
        runtime = ProviderIngressRuntime(app.state.business_sessions, resolve_cap, factory, enabled=True)
        service = EmailIngressService(app.state.business_sessions, provider_ingress=runtime)
        activation = runtime.activate(
            world.store,
            world.ctx,
            ActivateIngress(
                email_connection_id=connection.id,
                storage_connection_id=lane.storage_id,
                evidence="Explicit mocked HTTP original capture proof only.",
            ),
        )
        world.store.commit()
        app.state.email_ingress = service
        from dataclasses import replace

        from artifact_support import storage_configuration

        from aco.integrations.s3.adapter import StorageRuntime

        app.state.ports = replace(app.state.ports, storage=StorageRuntime(**storage_configuration()))
        at, event_id = now(), f"msg_{uuid4()}"
        raw = json.dumps(
            {
                "type": "email.received",
                "created_at": at.isoformat(),
                "data": {"email_id": str(native), "to": list(original.to)},
            }
        ).encode()
        headers = {
            "svix-id": event_id,
            "svix-timestamp": str(int(at.timestamp())),
            "svix-signature": Webhook(lane.secret).sign(event_id, at, raw.decode()),
        }
        response = world.client.post(f"/api/email-ingress/{activation.id}", content=raw, headers=headers)
        assert response.status_code == 202, response.text
        receipt_id = response.json()[0]["id"]
        repeat = world.client.post(f"/api/email-ingress/{activation.id}", content=raw, headers=headers)
        assert repeat.json()[0]["id"] == receipt_id and calls == []
        world.store.rollback()
        if boundary == "rotation":
            active_cap = cap.model_copy(update={"webhook_secret_sha256": "f" * 64})
        elif boundary == "membership":
            member = world.store.scalar(
                select(Membership).where(
                    Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
                )
            )
            assert member is not None
            member.version += 1
            world.store.commit()
        elif boundary == "retention":
            policy.version += 1
            world.store.commit()
        settings = ingress_settings(world.url, lane.secret).model_copy(
            update={"synthetic_email_ingress_enabled": False}
        )
        worker = WorkerRuntime(
            settings,
            provider_ingress_factory=(
                None
                if boundary == "disabled"
                else lambda sessions: ProviderIngressRuntime(sessions, resolve_cap, factory, enabled=True)
            ),
        )
        try:
            async with worker.app.open_async():
                await dispatch_email_ingress(worker.sessions, worker.app)
                await worker.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
                world.store.rollback()
                receipt = world.store.get(EmailIngressReceipt, UUID(receipt_id))
                financial = world.store.get(InvoiceSnapshot, world.invoice_id, populate_existing=True)
                assert financial is not None
                assert (
                    financial.balance_minor,
                    financial.source_revision,
                    financial.financial_status,
                ) == source_facts
                if boundary != "success":
                    assert receipt is not None and receipt.state == "failed"
                    expected = {
                        "disabled": "PROVIDER_INGRESS_DISABLED",
                        "rotation": "PROVIDER_INGRESS_CONFIGURATION_CHANGED",
                        "membership": "INGRESS_ACTIVATION_REVOKED",
                        "retention": "PROVIDER_INGRESS_MATERIAL_REVOKED",
                        "archive_rotation": "PROVIDER_INGRESS_CONFIGURATION_CHANGED",
                        "transport_failure": "INGRESS_FETCH_OR_CAPTURE_FAILED",
                        "intent_rotation": "PROVIDER_INGRESS_CONFIGURATION_CHANGED",
                        "intent_removal": "PROVIDER_INGRESS_BINDING_UNAVAILABLE",
                        "wire_intent_rotation": "PROVIDER_INGRESS_CONFIGURATION_CHANGED",
                    }
                    assert receipt.error_code == expected[boundary], receipt.error_code
                    assert receipt.agent_run_id is None
                    late = boundary in {"intent_rotation", "intent_removal", "wire_intent_rotation"}
                    assert world.store.scalar(select(func.count()).select_from(Communication)) == int(late)
                    assert world.store.scalar(select(func.count()).select_from(Artifact)) == int(late)
                    if late:
                        assert len(intent_barriers) == 1
                        pending_artifact = world.store.scalar(select(Artifact))
                        assert pending_artifact is not None and pending_artifact.storage_state == "pending"
                        assert pending_artifact.object_version is None
                        storage_attempts = list(
                            world.store.scalars(
                                select(UsageAttempt).where(UsageAttempt.operation == "put_private_artifact")
                            )
                        )
                        assert len(storage_attempts) == int(boundary == "wire_intent_rotation")
                        if storage_attempts:
                            assert (
                                storage_attempts[0].outcome == "NO_EFFECT"
                                and storage_attempts[0].event_id is None
                            )
                    assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 0
                    assert len(calls) == (
                        2
                        if boundary
                        in {"archive_rotation", "intent_rotation", "intent_removal", "wire_intent_rotation"}
                        else 1
                        if boundary == "transport_failure"
                        else 0
                    )
                    failed_attempts = list(
                        world.store.scalars(
                            select(UsageAttempt).where(UsageAttempt.provider_connection_id == connection.id)
                        )
                    )
                    assert len(failed_attempts) == len(calls)
                    assert all(row.business_outcome == "FAILED" for row in failed_attempts)
                    if boundary == "transport_failure":
                        assert (
                            failed_attempts[0].outcome == "FAILED" and failed_attempts[0].event_id is not None
                        )
                    return
                assert receipt is not None and receipt.state == "processed", (
                    None if receipt is None else receipt.error_code
                )
                assert receipt.agent_run_id is not None
                run_id = receipt.agent_run_id
                world.store.rollback()
                await dispatch_agent_runs(worker.sessions, worker.app)
                await worker.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
        finally:
            await worker.engine.dispose()
        world.store.rollback()
        run = world.store.get(AgentRun, run_id)
        assert run is not None and run.status == "completed", None if run is None else run.error_code
        assert run.authenticity == "FAKE" and run.provider_attempted is False
        message = world.store.get(Communication, receipt.communication_id)
        assert message is not None and message.synthetic and message.source_state == "stored"
        source = world.store.scalar(
            select(CommunicationSource).where(CommunicationSource.communication_id == message.id)
        )
        assert source is not None and source.sha256 == sha256(original.source.content).hexdigest()
        assert source.evidence["provider_evidence_mode"] == "MOCKED_HTTP"
        assert source.evidence["source_provider"] == "resend"
        artifact = world.store.get(Artifact, source.artifact_id)
        assert (
            artifact is not None
            and artifact.storage_state == "stored"
            and artifact.retention_policy_id == policy.id
        )
        assert len(authority_probes) == 5
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        attempts = list(
            world.store.scalars(
                select(UsageAttempt).where(UsageAttempt.provider_connection_id == connection.id)
            )
        )
        assert all(row.business_outcome == "COMMITTED" for row in attempts)
        assert len(calls) == len(attempts) == 2 and all(
            row.event_id and row.outcome == "VERIFIED" for row in attempts
        )
        events = list(
            world.store.scalars(
                select(ProviderUsageEvent).where(ProviderUsageEvent.provider_connection_id == connection.id)
            )
        )
        assert len(events) == 2 and all(row.synthetic for row in events)
        assert {row.native_operation_id for row in events} == {"mocked-native-request", None}
        revisions = list(
            world.store.scalars(
                select(UsageRevision).where(UsageRevision.event_id.in_([row.id for row in events]))
            )
        )
        assert all(row.cost_amount is None and row.cost_quality == "UNKNOWN" for row in revisions)
        assert "never-persist" not in str([row.payload for row in revisions])
        download = world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/artifacts/{artifact.id}/content")
        assert download.status_code == 200 and download.content == original.source.content
        from sqlalchemy import text

        second_id = f"msg_{uuid4()}"
        second_headers = {
            **headers,
            "svix-id": second_id,
            "svix-signature": Webhook(lane.secret).sign(second_id, at, raw.decode()),
        }
        second = world.client.post(f"/api/email-ingress/{activation.id}", content=raw, headers=second_headers)
        assert second.status_code == 202 and second.json()[0]["id"] != receipt_id
        world.store.rollback()
        replay_worker = WorkerRuntime(
            settings,
            provider_ingress_factory=lambda sessions: ProviderIngressRuntime(
                sessions, resolve_cap, factory, enabled=True
            ),
        )
        try:
            async with replay_worker.app.open_async():
                await dispatch_email_ingress(replay_worker.sessions, replay_worker.app)
                await replay_worker.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
        finally:
            await replay_worker.engine.dispose()
        world.store.rollback()
        replay_receipt = world.store.get(EmailIngressReceipt, UUID(second.json()[0]["id"]))
        assert (
            replay_receipt is not None
            and replay_receipt.state == "processed"
            and replay_receipt.agent_run_id == run_id
        )
        assert len(calls) == 4
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 1
        assert world.store.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        assert world.store.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        from sqlalchemy.exc import DBAPIError

        with pytest.raises(DBAPIError), world.store.begin_nested():
            world.store.execute(
                text("UPDATE email_ingress_activations SET provider_binding=NULL WHERE id=:id"),
                {"id": activation.id},
            )
        assert (
            world.store.scalar(
                select(func.count()).select_from(Communication).where(Communication.direction == "outgoing")
            )
            == 0
        )


@pytest.mark.parametrize("boundary", ["missing", "foreign", "live"])
def test_server_capability_cannot_create_missing_cross_tenant_or_live_authority(boundary: str) -> None:
    from aco.features.communications.provider_ingress import ProviderIngressCapability, ProviderIngressRuntime

    tenant, connection = uuid4(), uuid4()
    cap = ProviderIngressCapability(
        tenant_id=tenant,
        connection_id=connection,
        credential_revision="opaque-test-revision",
        inbox_addresses=("fixture@example.test",),
        webhook_secret_sha256="a" * 64,
        retention_policy_id=uuid4(),
        retention_policy_version=1,
    )

    def resolve(tenant_id: UUID, connection_id: UUID) -> ProviderIngressCapability:
        if boundary == "missing":
            raise KeyError("opaque binding absent")
        return (
            cap.model_copy(update={"tenant_id": uuid4()})
            if boundary == "foreign"
            else cap.model_copy(update={"evidence_mode": "LIVE"})
        )

    def no_credentials(*args: object) -> NoReturn:
        pytest.fail("no rejected capability may resolve adapter credentials")

    runtime = ProviderIngressRuntime(
        async_sessionmaker[AsyncSession](), resolve, no_credentials, enabled=True
    )
    with pytest.raises(HTTPException) as denied:
        runtime.capability(tenant, connection)
    assert (
        denied.value.detail
        == {
            "missing": "PROVIDER_INGRESS_BINDING_UNAVAILABLE",
            "foreign": "PROVIDER_INGRESS_BINDING_INVALID",
            "live": "REAL_EMAIL_CAPTURE_NOT_AUTHORIZED",
        }[boundary]
    )
