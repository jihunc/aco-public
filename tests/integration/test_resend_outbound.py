"""Actual maintained dispatch with HTTPX provider mocks; never live email evidence."""

from typing import NoReturn
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_provider_outbound_defaults_closed_before_factory() -> None:
    from aco.core.settings import Settings
    from aco.features.communications.provider_outbound import ProviderOutboundRuntime

    def forbidden(*args: object) -> NoReturn:
        pytest.fail("disabled runtime must not resolve credentials or call a provider")

    runtime = ProviderOutboundRuntime(
        async_sessionmaker[AsyncSession](), Settings(), None, forbidden, forbidden
    )
    with pytest.raises(HTTPException, match="PROVIDER_OUTBOUND_DISABLED"):
        runtime.capability(uuid4(), uuid4())


@pytest.mark.parametrize(
    "boundary",
    [
        "success",
        "provider_swap",
        "unknown",
        "rotation",
        "archive_rotation",
        "live",
        "rollback_no_request",
        "live_offline",
        "live_outbound_only",
        "identity_observed",
        "identity_missing",
        "identity_failed",
        "identity_kill",
        "identity_intent_kill",
        "identity_revoked",
    ],
)
def test_canonical_worker_uses_provider_submission_fence(
    boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from dataclasses import replace
    from hashlib import sha256
    from typing import Any

    import httpx
    from pydantic import SecretStr
    from reminder_support import reminder_world
    from sqlalchemy import func, select

    from aco.features.artifacts.models import RetentionPolicy
    from aco.features.communications.activation import latest
    from aco.features.communications.ingress_dto import ActivateIngress
    from aco.features.communications.models import (
        Communication,
        CommunicationSource,
        EmailDispatchBinding,
        EmailSubmission,
    )
    from aco.features.communications.provider_ingress import (
        ProviderIngressCapability,
        ProviderIngressRuntime,
    )
    from aco.features.communications.provider_outbound import (
        ProviderOutboundCapability,
        ReviewProviderOutbound,
    )
    from aco.features.identity.models import ProviderConnection
    from aco.features.usage.email import ObservedEmail
    from aco.features.usage.models import ProviderUsageEvent, UsageRevision
    from aco.integrations.resend.outbound import compose_outbound
    from aco.integrations.resend.settings import ResendBinding

    with reminder_world() as (client, store, ctx, invoices, saved):
        app = client.app
        from datetime import UTC, datetime

        from aco.features.accounting.models import CustomerContact
        from aco.features.policies.models import RecipientControl

        offset = 12 - datetime.now(UTC).hour
        zone = f"Etc/GMT{-offset:+d}" if offset else "Etc/GMT"
        for contact in store.scalars(
            select(CustomerContact).where(CustomerContact.tenant_id == ctx.tenant_id)
        ):
            control = store.scalar(select(RecipientControl).where(RecipientControl.contact_id == contact.id))
            assert control is not None
            response = client.put(
                f"/api/workspaces/{ctx.tenant_id}/policy-recipients/{contact.id}",
                json={
                    "expected_version": control.version,
                    "expected_contact_revision": contact.source_revision,
                    "expected_destination": contact.email,
                    "timezone": zone,
                    "timezone_verified": True,
                    "owner_confirmed_tenant_timezone": False,
                    "destination_verified": True,
                    "allowed_channels": ["email"],
                    "opt_out": False,
                    "evidence": "Owner reviewed synthetic noon-zone for current-time offline worker proof",
                },
            )
            assert response.status_code == 200, response.text
        store.rollback()
        retained: dict[str, dict[str, str]] = {}
        retained_original: tuple[str, bytes] | None = None
        swap_invoice_id = None
        old_customer_id = None
        if boundary == "provider_swap":
            from policy_support import activate, save
            from reminder_support import dispatch_maintained

            from aco.features.accounting.models import InvoiceSnapshot
            from aco.features.actions.models import ScheduledAction
            from aco.integrations.demo_email.models import NativeEmailOperation

            first = dispatch_maintained(client, store, ctx)
            assert first.status == "succeeded", first.last_error
            first_invoice = store.get(InvoiceSnapshot, first.invoice_id)
            assert first_invoice is not None
            old_customer_id = first_invoice.customer_id
            old_message = store.scalar(
                select(Communication).where(Communication.origin_invoice_id == first.invoice_id)
            )
            assert old_message is not None
            old_connection = store.get(ProviderConnection, old_message.provider_connection_id)
            assert old_connection is not None and old_connection.provider == "demo_email"
            old_source = store.scalar(
                select(CommunicationSource).where(CommunicationSource.communication_id == old_message.id)
            )
            assert old_source is not None
            original_path = f"/api/workspaces/{ctx.tenant_id}/artifacts/{old_source.artifact_id}/content"
            for model in (
                Communication,
                CommunicationSource,
                EmailSubmission,
                EmailDispatchBinding,
                NativeEmailOperation,
                ProviderUsageEvent,
                UsageRevision,
            ):
                retained[model.__tablename__] = {
                    str(row["id"]): json.dumps(dict(row), default=str, sort_keys=True)
                    for row in store.execute(select(model.__table__)).mappings()
                }
            finance_before = [
                (
                    row.id,
                    row.original_minor,
                    row.balance_minor,
                    row.currency,
                    row.financial_status,
                    row.source_revision,
                )
                for row in store.scalars(select(InvoiceSnapshot).order_by(InvoiceSnapshot.id))
            ]
            store.rollback()
            original_response = client.get(original_path)
            assert original_response.status_code == 200
            retained_original = original_path, original_response.content
            replacement = save(
                client,
                ctx.tenant_id,
                policy_id=saved["policy_id"],
                name=saved["name"],
                expected_version=saved["version"],
                definition=saved["definition"],
            )
            activate(client, ctx, replacement)
            pending = store.scalar(
                select(ScheduledAction)
                .join(InvoiceSnapshot, InvoiceSnapshot.id == ScheduledAction.invoice_id)
                .where(
                    ScheduledAction.tenant_id == ctx.tenant_id,
                    ScheduledAction.status == "queued",
                    InvoiceSnapshot.customer_id != old_customer_id,
                )
                .order_by(ScheduledAction.created_at.desc())
            )
            assert pending is not None
            swap_invoice_id = pending.invoice_id
            pending_before_review = pending.id
            store.rollback()
        previous = latest(store, ctx.tenant_id)
        assert previous is not None
        prior_id, sender, destinations, storage_id = (
            previous.id,
            previous.sender,
            tuple(previous.destinations),
            previous.storage_connection_id,
        )
        email = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            encrypted_credentials_ref="offline-1",
            external_account_id="offline-account",
        )
        policy = store.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.tenant_id == ctx.tenant_id, RetentionPolicy.policy_key == "synthetic_30_days"
            )
        )
        if policy is None:
            policy = RetentionPolicy(
                tenant_id=ctx.tenant_id,
                policy_key="synthetic_30_days",
                synthetic=True,
                retention_days=30,
                authority="synthetic_only_no_real_capture_authority",
            )
            store.add(policy)
        store.add(email)
        store.commit()
        killed = False
        live_authorization = None
        live_grant = None
        observe_identity = boundary.startswith("identity_")
        read_wire = observe_identity and boundary not in {
            "identity_kill",
            "identity_intent_kill",
            "identity_revoked",
        }
        outbound_only = boundary == "live_outbound_only" or observe_identity
        if boundary in {"live_offline", "live_outbound_only"} or observe_identity:
            from datetime import timedelta
            from decimal import Decimal

            from aco.features.accounting.models import InvoiceSnapshot
            from aco.features.actions.models import ScheduledAction
            from aco.features.communications.email_live import (
                ApprovedEmailRetention,
                EmailLiveAuthorization,
                EmailLiveConfig,
                install_email_live_grant,
            )
            from aco.features.identity.models import now

            selected_scope = store.scalar(
                select(ScheduledAction)
                .where(ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.status == "queued")
                .order_by(ScheduledAction.created_at.desc())
            )
            assert selected_scope is not None
            scope_invoice = store.get(InvoiceSnapshot, selected_scope.invoice_id)
            assert scope_invoice is not None
            contact = store.scalar(
                select(CustomerContact).where(
                    CustomerContact.customer_id == scope_invoice.customer_id,
                    CustomerContact.tenant_id == ctx.tenant_id,
                )
            )
            assert contact is not None and contact.email is not None
            destinations = (contact.email,)
            if outbound_only:
                sender = "onboarding@resend.dev"
            live_grant = install_email_live_grant(
                store,
                tenant_id=ctx.tenant_id,
                initiator_id=ctx.actor_id,
                connection_id=email.id,
                storage_connection_id=storage_id,
                account_id="offline-account",
                credential_revision="offline-1",
                config=EmailLiveConfig(
                    authorization_ref="OFFLINE-WORKER-FIXTURE-NOT-LIVE-APPROVAL",
                    source_commit="a" * 40,
                    mode="OUTBOUND_TEST_ONLY" if outbound_only else "ROUNDTRIP_TEST",
                    sender=sender,
                    recipient=destinations[0],
                    reply_mailbox=None if outbound_only else sender,
                    provider_test_account_email=destinations[0] if outbound_only else None,
                    max_received_messages=0 if outbound_only else 1,
                    participant_approval_ref="offline-participant",
                    sender_verification_ref="offline-domain",
                    expires_at=now() + timedelta(minutes=20),
                    max_requests=3,
                    max_messages=1,
                    max_usd=Decimal(".03"),
                    request_usd_ceiling=Decimal(".01"),
                    price_source_ref="offline-ceiling",
                    price_source_at=now(),
                ),
                retention=ApprovedEmailRetention(
                    approval_ref="offline-retention",
                    jurisdiction="offline-only",
                    subject="synthetic fixture participants",
                    purpose="offline application composition proof",
                    reviewer_id=ctx.actor_id,
                    approved_at=now(),
                    effective_until=now() + timedelta(hours=1),
                    retention_days=1,
                    max_bytes=1048576,
                ),
            )
            store.commit()
            live_authorization = EmailLiveAuthorization(
                live_grant.id,
                authorization_ref=live_grant.authorization_ref,
                source_commit="a" * 40,
                kill_switch=lambda: killed,
            )
            policy = store.get(RetentionPolicy, live_grant.retention_policy_id)
            assert policy is not None
        ingress_cap = ProviderIngressCapability(
            tenant_id=ctx.tenant_id,
            connection_id=email.id,
            credential_revision="offline-1",
            inbox_addresses=(sender,),
            webhook_secret_sha256=sha256(b"offline-secret").hexdigest(),
            retention_policy_id=policy.id,
            retention_policy_version=policy.version,
            capture_purpose="approved_live_test_capture"
            if live_grant
            else "offline_provider_original_fixture",
            evidence_mode="LIVE" if live_grant else "MOCKED_HTTP",
            live_grant_id=live_grant.id if live_grant else None,
        )

        def no_ingress(*args: object) -> Any:
            pytest.fail("This outbound checkpoint must not fetch or capture incoming data")

        ingress = ProviderIngressRuntime(
            app.state.business_sessions,
            lambda tenant, key: ingress_cap,
            no_ingress,
            enabled=True,
            live_authorization=live_authorization,
        )
        activated = None
        if not outbound_only:
            activated = ingress.activate(
                store,
                ctx,
                ActivateIngress(
                    email_connection_id=email.id,
                    storage_connection_id=storage_id,
                    evidence="Explicit mocked provider reply mailbox",
                    expires_in_seconds=3600,
                ),
            )
            store.commit()
        cap = ProviderOutboundCapability(
            tenant_id=ctx.tenant_id,
            connection_id=email.id,
            credential_revision="offline-1",
            ingress_activation_id=activated.id if activated else None,
            mode="OUTBOUND_TEST_ONLY" if outbound_only else "ROUNDTRIP_TEST",
            account_id="offline-account",
            sender=sender,
            destinations=destinations,
            request_cap=1 if live_grant else 3,
            evidence_mode="LIVE" if live_grant else "MOCKED_HTTP",
            live_grant_id=live_grant.id if live_grant else None,
            observe_sent_identity=observe_identity or boundary == "unknown",
        )
        active = cap
        requests: list[httpx.Request] = []
        native_id = uuid4()

        def transport(request: httpx.Request) -> httpx.Response:
            nonlocal killed
            requests.append(request)
            if request.method == "GET":
                assert observe_identity and str(request.url) == f"https://api.resend.com/emails/{native_id}"
                if boundary == "identity_failed":
                    return httpx.Response(503, json={"message": "offline unavailable"})
                return httpx.Response(
                    200,
                    json={
                        "id": str(native_id),
                        "from": sender,
                        "to": list(destinations),
                        "created_at": datetime.now(UTC).isoformat(),
                        "message_id": None
                        if boundary == "identity_missing"
                        else "<provider-observed@example.test>",
                    },
                    headers={"x-request-id": "offline-read-request"},
                )
            assert request.method == "POST" and str(request.url) == "https://api.resend.com/emails"
            assert request.headers["idempotency-key"].startswith(f"aco:{ctx.tenant_id}:{email.id}:")
            if boundary == "unknown":
                raise httpx.ReadTimeout("offline unknown response", request=request)
            if boundary == "identity_kill":
                killed = True
            if boundary == "identity_revoked":
                assert live_grant is not None
                live_grant.revoked_at = datetime.now(UTC)
                store.commit()
            return httpx.Response(
                200,
                json={"id": str(native_id)},
                headers={} if boundary == "rollback_no_request" else {"x-request-id": "offline-request-1"},
            )

        def resolve_binding(material: ProviderOutboundCapability) -> ResendBinding:
            assert material.evidence_mode == ("LIVE" if live_grant else "MOCKED_HTTP")
            return ResendBinding(
                tenant_id=material.tenant_id,
                connection_id=material.connection_id,
                api_key=SecretStr("offline-placeholder"),
                inbox_addresses=() if outbound_only else (sender,),
                sender_addresses=(sender,) if outbound_only else (),
                download_hosts=frozenset() if outbound_only else frozenset({"storage.example.test"}),
                evidence_mode="LIVE" if live_grant else "MOCKED_HTTP",
            )

        runtime = compose_outbound(
            app.state.business_sessions,
            app.state.settings,
            None if outbound_only else ingress,
            lambda tenant, key: active,
            resolve_binding,
            transport_factory=lambda: httpx.MockTransport(transport),
            enabled=True,
            live_authorization=live_authorization,
        )
        if boundary == "identity_intent_kill":
            original_reserve = EmailLiveAuthorization.reserve

            def kill_after_reservation(self: Any, *args: Any, **kwargs: Any) -> Any:
                nonlocal killed
                reserved = original_reserve(self, *args, **kwargs)
                if args[3] == "get_sent_message":
                    killed = True
                return reserved

            monkeypatch.setattr(EmailLiveAuthorization, "reserve", kill_after_reservation)
        if read_wire:
            from aco.features.communications import submission_observation

            original_observe = submission_observation.observe_accepted_submission

            async def checked_observe(db: Any, selected_runtime: Any, actual: Any, binding_id: Any) -> None:
                for update in (
                    {"tenant_id": uuid4()},
                    {"action_id": uuid4()},
                    {"lease_token": uuid4()},
                    {"action_version": actual.action_version + 1},
                ):
                    with pytest.raises(HTTPException) as refused:
                        await original_observe(
                            db, selected_runtime, actual.model_copy(update=update), binding_id
                        )
                    assert refused.value.status_code == 403
                from aco.features.communications.rfc_identity import READ_OPERATION
                from aco.features.usage.models import UsageAttempt

                assert (
                    await db.scalar(select(UsageAttempt.id).where(UsageAttempt.operation == READ_OPERATION))
                    is None
                )
                await original_observe(db, selected_runtime, actual, binding_id)

            monkeypatch.setattr(submission_observation, "observe_accepted_submission", checked_observe)
            original_factory = runtime.port_factory

            def checking_factory(material: Any, authority: Any, observer: Any) -> Any:
                port = original_factory(material, authority, observer)
                original_read = port.get_message

                async def checked_read(actual: Any, reference: Any) -> Any:
                    for forged, operation, selected_ref in (
                        (actual.model_copy(update={"tenant_id": uuid4()}), "get_message", reference),
                        (actual.model_copy(update={"action_id": uuid4()}), "get_message", reference),
                        (actual.model_copy(update={"lease_token": uuid4()}), "get_message", reference),
                        (actual, "send_email", reference),
                        (
                            actual,
                            "get_message",
                            reference.model_copy(update={"external_id": "sent:" + str(uuid4())}),
                        ),
                    ):
                        with pytest.raises(HTTPException, match="SUBMISSION_READ_SCOPE"):
                            await authority(forged, operation, selected_ref)
                    return await original_read(actual, reference)

                monkeypatch.setattr(port, "get_message", checked_read)
                return port

            runtime.port_factory = checking_factory
        if boundary == "live":
            active = cap.model_copy(update={"evidence_mode": "LIVE"})
            with pytest.raises(HTTPException, match="REAL_EMAIL_SEND_NOT_AUTHORIZED"):
                runtime.activate(
                    store,
                    ctx,
                    ReviewProviderOutbound(
                        connection_id=email.id,
                        expected_activation_id=prior_id,
                        evidence="Not a live authorization",
                    ),
                )
            assert requests == []
            store.rollback()
            return
        app.state.ports = replace(app.state.ports, email=runtime)
        review_body = {
            "connection_id": str(email.id),
            "expected_activation_id": str(prior_id),
            "evidence": "Owner reviewed explicit mocked HTTP outbound scope",
        }
        store.rollback()
        review_path = f"/api/workspaces/{ctx.tenant_id}/email-execution/provider"
        visible = client.get(review_path)
        assert visible.status_code == 200, visible.text
        configuration = visible.json()["configurations"][0]
        assert configuration["sender"] == sender
        assert configuration["destinations"] == list(destinations)
        assert configuration["evidence_mode"] == ("LIVE" if live_grant else "MOCKED_HTTP")
        assert not {"credential_revision", "api_key", "provider_binding", "live_grant_id"}.intersection(
            configuration
        )
        assert requests == []
        review_body["reviewed_hash"] = configuration["reviewed_hash"]
        stale = client.put(review_path, json=review_body | {"reviewed_hash": "0" * 64})
        assert stale.status_code == 409 and requests == []
        reviewed = client.put(review_path, json=review_body)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["activation"]["current"] is True
        assert reviewed.json()["synthetic"] is (not bool(live_grant))
        assert reviewed.json()["mode"] == ("LIVE" if live_grant else "MOCKED_HTTP")
        assert client.put(review_path, json=review_body).status_code == 409
        assert requests == []
        if outbound_only:
            from aco.features.communications.models import EmailIngressActivation, EmailMailboxClaim

            assert store.scalar(select(func.count()).select_from(EmailIngressActivation)) == 0
            assert store.scalar(select(func.count()).select_from(EmailMailboxClaim)) == 0
            store.rollback()
        if boundary == "rotation":
            original = ObservedEmail.send

            async def rotate(self: ObservedEmail, *args: Any, **kwargs: Any) -> Any:
                nonlocal active
                active = cap.model_copy(update={"credential_revision": "changed"})
                return await original(self, *args, **kwargs)

            monkeypatch.setattr(ObservedEmail, "send", rotate)
        if boundary == "rollback_no_request":
            from aco.features.actions.service import ActionService

            completed = ActionService.complete_action

            def reject_business(self: ActionService, worker: Any, lease: Any, result: Any) -> None:
                if result.provider_ref is not None and result.provider_ref.provider == "resend":
                    raise HTTPException(409, "INJECTED_BUSINESS_COMMIT_REFUSAL")
                completed(self, worker, lease, result)

            monkeypatch.setattr(ActionService, "complete_action", reject_business)
        if boundary == "archive_rotation":
            from sqlalchemy import event

            from aco.features.artifacts.service import ArtifactService

            audited = ArtifactService._audit

            async def rotate_archive(
                self: ArtifactService, db: AsyncSession, *args: Any, **kwargs: Any
            ) -> None:
                await audited(self, db, *args, **kwargs)
                if args[1] == "artifact.capture_intent":

                    def rotate_after_commit(session: Any) -> None:
                        nonlocal active
                        active = cap.model_copy(update={"credential_revision": "late-rotation"})

                    event.listen(db.sync_session, "after_commit", rotate_after_commit, once=True)

            monkeypatch.setattr(ArtifactService, "_audit", rotate_archive)
        import asyncio

        from aco.features.actions.models import OutboxEvent, ScheduledAction
        from aco.features.communications.outbound import ReminderDispatcher
        from aco.features.identity.models import now
        from aco.worker.app import WorkerRuntime
        from aco.worker.jobs import DomainExecutor

        selected = store.scalar(
            select(ScheduledAction)
            .where(
                ScheduledAction.tenant_id == ctx.tenant_id,
                ScheduledAction.status == "queued",
                *([ScheduledAction.invoice_id == swap_invoice_id] if swap_invoice_id is not None else []),
            )
            .order_by(ScheduledAction.created_at.desc())
        )
        assert selected is not None
        if boundary == "provider_swap":
            assert selected.id == pending_before_review
        selected.due_at = now()
        outbox = store.scalar(
            select(OutboxEvent).where(
                OutboxEvent.action_id == selected.id,
                OutboxEvent.action_version == selected.version,
                OutboxEvent.dispatched_at.is_(None),
            )
        )
        assert outbox is not None
        outbox.due_at = selected.due_at
        action_id = selected.id
        store.commit()

        async def execute() -> None:
            dispatcher = ReminderDispatcher(
                app.state.business_sessions, app.state.settings, runtime, app.state.ports.storage
            )
            worker = WorkerRuntime(app.state.settings, DomainExecutor(dispatcher))
            try:
                async with worker.app.open_async():
                    for _ in range(20):
                        await worker.dispatcher.dispatch_outbox(100)
                        async with worker.sessions() as pending:
                            current = await pending.get(ScheduledAction, action_id)
                            assert current is not None
                            if current.job_id is not None:
                                break
                    assert current is not None and current.job_id is not None
                    await worker.app.run_worker_async(
                        queues=["aco"],
                        concurrency=1,
                        wait=False,
                        listen_notify=False,
                        install_signal_handlers=False,
                    )
            finally:
                await worker.engine.dispose()

        asyncio.run(execute())
        store.rollback()
        action = store.get(ScheduledAction, action_id, populate_existing=True)
        assert action is not None
        submission = store.scalar(
            select(EmailSubmission)
            .join(EmailDispatchBinding, EmailDispatchBinding.id == EmailSubmission.binding_id)
            .where(EmailDispatchBinding.action_id == action_id)
        )
        assert submission is not None, (action.status, action.last_error, action.job_id, action.result)
        if boundary == "archive_rotation":
            from aco.features.artifacts.models import Artifact

            assert not requests
            assert (
                action.status == "failed" and action.last_error == "PROVIDER_OUTBOUND_CONFIGURATION_CHANGED"
            )
            assert (
                store.scalar(
                    select(func.count()).select_from(Artifact).where(Artifact.storage_state == "stored")
                )
                == 0
            )
            assert submission.usage_attempt_id is None
            return
        if boundary == "rollback_no_request":
            from aco.features.communications.reconciliation import reconcile_delivery

            assert submission.state == "accepted" and submission.provider_request_id is None, (
                action.status,
                action.last_error,
                len(requests),
            )
            assert submission.provider_message_id == "sent:" + str(native_id)
            result = reconcile_delivery(store, ctx, action.id)
            store.commit()
            assert result.state == "accepted"
            events = list(
                store.scalars(
                    select(ProviderUsageEvent).where(ProviderUsageEvent.provider_connection_id == email.id)
                )
            )
            assert len(events) == 1 and events[0].native_operation_id is None
            assert len(requests) == 1
            return
        if boundary in {"success", "provider_swap", "live_offline", "live_outbound_only"} or observe_identity:
            assert action.status == "succeeded", action.last_error
            assert submission.state == "accepted"
            message = store.scalar(
                select(Communication).where(Communication.operation_id == submission.operation_id)
            )
            assert message is not None and message.native_message_id == "sent:" + str(native_id)
            assert message.synthetic == (not live_grant) and message.delivery_state == "accepted"
            source = store.scalar(
                select(CommunicationSource).where(CommunicationSource.communication_id == message.id)
            )
            assert source is not None and source.representation == "ACO_COMPOSED"
            store.rollback()
            response = client.get(f"/api/workspaces/{ctx.tenant_id}/artifacts/{source.artifact_id}/content")
            assert response.status_code == 200
            assert message.content["message_id"].encode() in response.content
            posted = json.loads(requests[0].content)
            assert posted["headers"]["Message-ID"] == message.content["message_id"]
            events = list(
                store.scalars(
                    select(ProviderUsageEvent).where(ProviderUsageEvent.provider_connection_id == email.id)
                )
            )
            assert len(events) == (2 if read_wire else 1) and events[0].provider == "resend"
            if observe_identity:
                from aco.features.communications.models import (
                    CommunicationDeliveryEvent,
                    CommunicationRfcIdentity,
                )
                from aco.features.communications.rfc_identity import READ_OPERATION
                from aco.features.usage.models import UsageAttempt

                aliases = list(store.scalars(select(CommunicationRfcIdentity)))
                state = store.scalar(
                    select(CommunicationDeliveryEvent).where(
                        CommunicationDeliveryEvent.event_key == f"rfc-identity:{submission.id}"
                    )
                )
                assert state is not None
                expected_state = (
                    "incomplete"
                    if boundary == "identity_missing"
                    else (
                        "unavailable"
                        if boundary
                        in {"identity_failed", "identity_kill", "identity_intent_kill", "identity_revoked"}
                        else "observed"
                    )
                )
                assert state.state == "rfc_identity_" + expected_state, state.evidence
                assert len(aliases) == (1 if boundary == "identity_observed" else 0)
                if not read_wire:
                    assert not any(event.operation == READ_OPERATION for event in events)
                    assert state.evidence["reason"] == (
                        "EMAIL_LIVE_GRANT_REQUIRED"
                        if boundary == "identity_revoked"
                        else "EMAIL_LIVE_KILL_SWITCH"
                    )
                    if boundary == "identity_intent_kill":
                        attempt = store.scalar(
                            select(UsageAttempt).where(UsageAttempt.operation == READ_OPERATION)
                        )
                        assert attempt is not None and attempt.event_id is None
                        assert attempt.outcome == "ATTEMPTED" and attempt.business_outcome == "FAILED"
                else:
                    read_event = next(event for event in events if event.operation == READ_OPERATION)
                    assert read_event.native_subject == submission.provider_message_id
                    attempt = store.scalar(select(UsageAttempt).where(UsageAttempt.event_id == read_event.id))
                    assert attempt is not None
                    if boundary != "identity_failed":
                        assert attempt.outcome == "SUCCEEDED" and attempt.business_outcome == "OBSERVED"
                        assert read_event.native_operation_id == "offline-read-request"
                if aliases:
                    assert aliases[0].rfc_message_id == "<provider-observed@example.test>"
                    assert aliases[0].rfc_message_id != message.content["message_id"]
            revision = store.scalar(select(UsageRevision).where(UsageRevision.event_id == events[0].id))
            assert revision is not None and revision.cost_amount is None
            assert events[0].synthetic == (not live_grant)
            if live_grant:
                from aco.features.artifacts.models import Artifact
                from aco.features.usage.models import BudgetReservation

                artifact = store.get(Artifact, source.artifact_id)
                assert artifact is not None and artifact.capture_class == "real"
                assert artifact.retention_policy_id == live_grant.retention_policy_id
                reservations = list(
                    store.scalars(
                        select(BudgetReservation).where(BudgetReservation.provider_connection_id == email.id)
                    )
                )
                assert len(reservations) == (
                    2 if observe_identity and boundary not in {"identity_kill", "identity_revoked"} else 1
                )
                assert all(row.retained_amount == Decimal(".01") for row in reservations)
                assert reservations[0].status == "UNKNOWN_SIDE_EFFECT"

        elif boundary == "unknown":
            from aco.features.communications.rfc_identity import READ_OPERATION
            from aco.features.usage.models import UsageAttempt

            assert (
                store.scalar(select(UsageAttempt.id).where(UsageAttempt.operation == READ_OPERATION)) is None
            )
            assert submission.state == "unknown_outcome"
            assert not action.automatic_resend
        else:
            assert submission.state == "no_effect"
            assert not requests
            assert (
                store.scalar(
                    select(func.count())
                    .select_from(ProviderUsageEvent)
                    .where(ProviderUsageEvent.provider_connection_id == email.id)
                )
                == 0
            )
        assert len(requests) == (0 if boundary == "rotation" else (2 if read_wire else 1))
        if boundary == "provider_swap":
            from aco.features.communications.models import EmailActivationRevocation

            assert retained_original is not None
            assert client.get(retained_original[0]).content == retained_original[1]
            store.rollback()
            assert (
                store.scalar(
                    select(EmailActivationRevocation).where(
                        EmailActivationRevocation.activation_id == prior_id
                    )
                )
                is not None
            )
            actual_binding = store.scalar(
                select(EmailDispatchBinding).where(EmailDispatchBinding.action_id == action_id)
            )
            assert actual_binding is not None and actual_binding.email_connection_id == email.id
            for model in (
                Communication,
                CommunicationSource,
                EmailSubmission,
                EmailDispatchBinding,
                NativeEmailOperation,
                ProviderUsageEvent,
                UsageRevision,
            ):
                after = {
                    str(row["id"]): json.dumps(dict(row), default=str, sort_keys=True)
                    for row in store.execute(select(model.__table__)).mappings()
                }
                assert all(after[key] == value for key, value in retained[model.__tablename__].items())
            finance_after = [
                (
                    row.id,
                    row.original_minor,
                    row.balance_minor,
                    row.currency,
                    row.financial_status,
                    row.source_revision,
                )
                for row in store.scalars(select(InvoiceSnapshot).order_by(InvoiceSnapshot.id))
            ]
            assert finance_after == finance_before
            assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 1
            assert store.scalar(select(func.count()).select_from(Communication)) == 2
        # Canonical maintenance sees no eligible resend after accepted or unknown effect.
        store.rollback()
        asyncio.run(execute())
        assert len(requests) <= (2 if read_wire else 1)


def test_provider_review_accepts_wire_ids_without_caller_authority() -> None:
    from pydantic import ValidationError

    from aco.features.communications.provider_outbound import ReviewProviderOutbound

    key = uuid4()
    command = {
        "connection_id": str(key),
        "expected_activation_id": None,
        "evidence": "Reviewed explicit server scope",
    }
    assert ReviewProviderOutbound.model_validate(command).connection_id == key
    with pytest.raises(ValidationError):
        ReviewProviderOutbound.model_validate(command | {"destinations": ["unreviewed@example.test"]})
