"""Actual graph/review/worker with LIVE code and offline HTTPX only, never live evidence."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from agent_email_live_support import configure_live_code
from artifact_support import storage_configuration
from fastapi import FastAPI
from identity_support import add_actor
from integration.test_agent_email_dispatch import wave
from policy_support import activate, configure_recipients, policy_world, save
from sqlalchemy import func, select

from aco.core.settings import Settings
from aco.core.types import Role
from aco.features.communications.ingress import EmailIngressService
from aco.features.communications.models import Communication, EmailSubmission
from aco.features.identity.models import Membership
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.integrations.demo_email.adapter import DemoEmailRuntime
from aco.integrations.s3.adapter import StorageRuntime


@pytest.mark.parametrize("mode", ["modified", "revoked", "kill", "unknown", "identity_budget", "intent_kill"])
def test_agent_email_live_code_requires_reviewed_ceiling_and_current_wire_authority(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            from aco.features.actions.models import ScheduledAction
            from aco.features.communications.email_live import EmailLiveGrant
            from aco.features.communications.models import CommunicationSource
            from aco.features.review.models import ApprovalRequest
            from aco.features.usage.models import BudgetReservation, ProviderUsageEvent, UsageRevision

            store.rollback()
            financial_before = [
                (i.id, i.source_revision, i.balance_minor, i.financial_status)
                for i in store.scalars(select(InvoiceSnapshot).order_by(InvoiceSnapshot.id))
            ]
            store.rollback()
            probe = configure_live_code(
                client, store, ctx, contact.email, max_requests=1 if mode == "identity_budget" else 3
            )
            started = client.post(
                base + "/agent-runs",
                json={
                    "invoice_ids": [str(invoices[0].id)],
                    "objective": "Prepare a synthetic customer email and request independent approval",
                    "request_id": str(uuid4()),
                },
            )
            assert started.status_code == 202, started.text
            run_id = UUID(started.json()["id"])
            store.rollback()
            wave(app)
            run = client.get(base + f"/agent-runs/{run_id}").json()
            assert run["status"] == "awaiting_approval", run
            proposal = next(p for p in run["result"]["proposals"] if p["kind"] == "request_email_send")
            approval_id = UUID(proposal["approval_id"])
            shown = client.get(base + f"/approvals/{approval_id}").json()
            cost = shown["proposal"]
            assert cost["cost_basis"] == "SHARED_GRANT_AUTHORIZATION_CEILING"
            assert Decimal(cost["cost_bound"]) == Decimal(".01" if mode == "identity_budget" else ".03")
            assert cost["currency"] == "USD"
            reviewed = cost["live_cost_scope"]
            assert reviewed["grant_id"] == str(probe.grant_id)
            assert reviewed["max_requests"] == (1 if mode == "identity_budget" else 3)
            assert reviewed["max_messages"] == 1
            assert Decimal(reviewed["request_usd_ceiling"]) == Decimal(".01")
            assert reviewed["price_source_ref"] == "offline-ceiling-not-provider-price"
            assert reviewed["price_source_at"]
            assert not shown["can_approve"]
            assert probe.requests == []
            store.rollback()
            assert store.scalar(select(func.count()).select_from(EmailSubmission)) == 0
            store.rollback()
            expected = proposal["email_command"]["content"]
            if mode == "modified":
                expected = "OFFLINE LIVE-CODE EDIT: Please contact our review team."
                changed = client.post(
                    base + f"/approvals/{approval_id}/modifications",
                    json={
                        "expected_version": shown["version"],
                        "reviewed_action_version": shown["action_version"],
                        "reviewed_material_hash": shown["material_hash"],
                        "reviewed_binding_hash": shown["current_binding_hash"],
                        "reason": "Explicit offline wording correction before independent review",
                        "edit": {"kind": "outbound_content", "content": expected},
                    },
                )
                assert changed.status_code == 200, changed.text
                approval_id = UUID(changed.json()["id"])
                assert changed.json()["proposal"]["live_cost_scope"] == reviewed
            store.rollback()
            user, _ = add_actor(store, app, "offline-independent-reviewer@synthetic.invalid")
            store.add(Membership(tenant_id=ctx.tenant_id, user_id=user.id, role="finance_manager"))
            store.commit()
            reviewer = ctx.model_copy(update={"actor_id": user.id, "role": Role.FINANCE_MANAGER})
            service = HumanApprovalService(store)
            view = service.get(reviewer, approval_id)
            assert view.can_approve, view.reasons
            service.decide_approval(
                reviewer,
                approval_id,
                ApprovalDecisionCommand(
                    expected_version=view.version,
                    reviewed_action_version=view.action_version,
                    reviewed_material_hash=view.material_hash,
                    reviewed_binding_hash=view.current_binding_hash,
                    decision="approved",
                    reason="Independent exact content and shared authorization ceiling review",
                ),
            )
            store.commit()
            if mode == "revoked":
                from aco.features.identity.models import now

                grant = store.get(EmailLiveGrant, probe.grant_id)
                assert grant is not None
                grant.revoked_at = now()
                store.commit()
            if mode == "kill":
                probe.killed = True
            probe.unknown = mode == "unknown"
            if mode == "intent_kill":
                from sqlalchemy.orm import Session

                from aco.features.communications.email_live import EmailLiveAuthorization
                from aco.features.usage.service import UsageContext

                reserve = EmailLiveAuthorization.reserve

                def kill_after_reservation(
                    authority: EmailLiveAuthorization,
                    db: Session,
                    usage_ctx: UsageContext,
                    connection_id: UUID,
                    operation: str,
                    *,
                    attempt_id: UUID,
                ) -> BudgetReservation:
                    result = reserve(
                        authority, db, usage_ctx, connection_id, operation, attempt_id=attempt_id
                    )
                    if operation == "send_email":
                        probe.killed = True
                    return result

                monkeypatch.setattr(EmailLiveAuthorization, "reserve", kill_after_reservation)
            store.rollback()
            for _ in range(3):
                wave(app)
            store.rollback()
            messages = list(store.scalars(select(Communication)))
            request = store.get(ApprovalRequest, approval_id)
            assert request is not None
            action = store.get(ScheduledAction, request.action_id)
            assert action is not None
            if mode in {"kill", "revoked"}:
                assert not probe.requests and not messages, action.last_error
                assert action.status in {"failed", "cancelled"}, (action.status, action.last_error)
            elif mode == "intent_kill":
                assert probe.killed and not probe.requests
                assert action.status == "failed", (action.status, action.last_error)
                assert all(message.delivery_state == "failed" for message in messages)
                assert not action.automatic_resend
            else:
                assert len(messages) == 1, (action.status, action.last_error)
                assert messages[0].content["text_body"] == expected
                assert messages[0].delivery_state == ("unknown_outcome" if mode == "unknown" else "accepted")
                posts = [r for r in probe.requests if r.method == "POST"]
                assert len(posts) == 1 and json.loads(posts[0].content)["text"] == expected
                assert len(probe.requests) == (1 if mode in {"unknown", "identity_budget"} else 2)
                assert not action.automatic_resend
                if mode == "unknown":
                    submission = store.scalar(select(EmailSubmission))
                    assert submission is not None and submission.state == "unknown_outcome"
                    assert submission.intent_at is not None and action.status == "unknown_outcome"
                else:
                    assert request.consumed_at is not None
                source = store.scalar(
                    select(CommunicationSource).where(CommunicationSource.communication_id == messages[0].id)
                )
                assert source is not None and source.artifact_id is not None
                store.rollback()
                original = client.get(base + f"/artifacts/{source.artifact_id}/content")
                assert original.status_code == 200
                from email.parser import BytesParser
                from email.policy import default

                parsed = BytesParser(policy=default).parsebytes(original.content)
                assert parsed.get_content().rstrip("\r\n") == expected
                store.rollback()
                usage = list(
                    store.scalars(select(ProviderUsageEvent).where(ProviderUsageEvent.provider == "resend"))
                )
                assert len(usage) == len(probe.requests)
                for event in usage:
                    revision = store.scalar(
                        select(UsageRevision)
                        .where(UsageRevision.event_id == event.id)
                        .order_by(UsageRevision.created_at.desc())
                    )
                    assert (
                        revision is not None
                        and revision.cost_amount is None
                        and revision.cost_quality == "UNKNOWN"
                    )
            store.rollback()
            assert [
                (i.id, i.source_revision, i.balance_minor, i.financial_status)
                for i in store.scalars(select(InvoiceSnapshot).order_by(InvoiceSnapshot.id))
            ] == financial_before
            reservations = list(
                store.scalars(
                    select(BudgetReservation).where(
                        BudgetReservation.operation_key.like(f"email-live:{probe.grant_id}:%")
                    )
                )
            )
            retained = sum((r.retained_amount for r in reservations), Decimal(0))
            if mode in {"modified", "unknown", "identity_budget"}:
                assert retained == len(probe.requests) * Decimal(".01")
                assert retained <= Decimal(cost["cost_bound"])
                assert all(r.released_amount == 0 for r in reservations)
            store.rollback()
            wires = len(probe.requests)
            wave(app)
            assert len(probe.requests) == wires
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
