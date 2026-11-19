"""Retained signed source, actual membership and additive invoice references."""

import asyncio
from email import policy
from email.parser import BytesParser
from hashlib import sha256

import pytest
from ingress_support import IngressWorld, ingress_world
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.communications.models import Communication, CommunicationInvoiceLink, CommunicationSource
from aco.integrations.ports.email_dto import EmailMessage, EmailSource


def unverified_message(lane: IngressWorld, body: str) -> EmailMessage:
    message: EmailMessage = lane.message(body=body)
    mime = BytesParser(policy=policy.default).parsebytes(message.source.content or b"")
    mime.replace_header("From", "unverified-review@synthetic.invalid")
    original = mime.as_bytes()
    return message.model_copy(
        update={
            "sender": "unverified-review@synthetic.invalid",
            "headers": tuple(mime.items()),
            "source": EmailSource(
                kind="PROVIDER_ORIGINAL", content=original, integrity_sha256=sha256(original).hexdigest()
            ),
        }
    )


def test_review_unlinked_original_adds_exact_invoice_links_once_without_retagging_thread() -> None:
    with ingress_world() as lane:
        message = unverified_message(lane, "Explicit synthetic source: INV-101 and INV-107 need review.")
        lane.world.store.rollback()
        asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        db, client = lane.world.store, lane.world.client
        db.rollback()
        source = db.scalar(select(Communication))
        assert source is not None and source.source_state == "stored"
        assert db.scalar(select(func.count()).select_from(CommunicationInvoiceLink)) == 0
        source_id, tenant_id = source.id, lane.world.ctx.tenant_id
        before = source.content, source.message_sha256, source.thread_id, source.origin_invoice_id
        invoices = list(
            db.scalars(select(InvoiceSnapshot).where(InvoiceSnapshot.number.in_(["INV-101", "INV-107"])))
        )
        snapshots = [(r.id, r.source_revision, r.balance_minor, r.financial_status) for r in invoices]
        db.rollback()
        path = f"/api/workspaces/{tenant_id}/communications/{source_id}"
        review = client.get(path + "/link-review")
        assert review.status_code == 200, review.text
        data = review.json()
        assert data["can_link"] and data["sender_authority"] == "UNVERIFIED", data["reasons"]
        assert {r["invoice_id"] for r in data["candidates"]} == {str(r.id) for r in invoices}
        command = {
            "customer_id": data["candidates"][0]["customer_id"],
            "invoice_ids": [str(r.id) for r in invoices],
            "expected_version": data["communication_version"],
            "source_sha256": data["source_sha256"],
            "evidence": {"communication_id": str(source_id), "excerpt": message.text_body, "offset": 0},
        }
        saved = client.post(path + "/invoice-links", json=command)
        assert saved.status_code == 200, saved.text
        assert {r["invoice_id"] for r in saved.json()["invoice_links"]} == {str(r.id) for r in invoices}
        assert client.post(path + "/invoice-links", json=command).status_code == 200
        db.expire_all()
        current = db.get(Communication, source_id)
        assert current is not None
        assert before == (
            current.content,
            current.message_sha256,
            current.thread_id,
            current.origin_invoice_id,
        )
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        links = list(db.scalars(select(CommunicationInvoiceLink)))
        assert len(links) == 2 and all(r.confidence == "contextual_unverified_sender" for r in links)
        assert snapshots == [(r.id, r.source_revision, r.balance_minor, r.financial_status) for r in invoices]
        assert current.version == data["communication_version"] + 1
        db.rollback()
        case = client.post(
            f"/api/workspaces/{tenant_id}/cases",
            json={
                "customer_id": command["customer_id"],
                "invoice_ids": command["invoice_ids"],
                "issue_type": "other",
                "claim_key": "reviewed source",
                "substance": "Needs human review",
                "evidence": command["evidence"],
            },
        )
        assert case.status_code == 201, case.text


@pytest.mark.parametrize("source_read", [True, False])
def test_actual_agent_links_retained_read_source_within_complete_selected_scope(
    source_read: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evaluations.test_collection_agent import proposal, run_sdk

    from aco.features.actions.models import ScheduledAction
    from aco.features.agent.models import AgentStep
    from aco.features.agent.nodes import InvestigationNode
    from aco.features.agent.schemas import GraphState
    from aco.features.cases.dto import CommunicationEvidence
    from aco.features.communications.association import retained_original
    from aco.features.communications.association_dto import LinkCommunication

    original_execute = InvestigationNode.execute

    async def replay(self: InvestigationNode, state: GraphState) -> GraphState:
        first = await original_execute(self, state)
        second = await original_execute(self, state)
        assert first == second
        return second

    monkeypatch.setattr(InvestigationNode, "execute", replay)
    with ingress_world() as lane:
        message = unverified_message(lane, "Synthetic evidence explicitly mentions INV-101 and INV-107.")
        lane.world.store.rollback()
        asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        db = lane.world.store
        db.rollback()
        source = db.scalar(select(Communication))
        assert source is not None and isinstance(message.text_body, str)
        invoices = list(
            db.scalars(
                select(InvoiceSnapshot)
                .where(InvoiceSnapshot.number.in_(["INV-101", "INV-107"]))
                .order_by(InvoiceSnapshot.number)
            )
        )
        source_id = source.id
        first_command = LinkCommunication(
            customer_id=invoices[0].customer_id,
            invoice_ids=[invoices[0].id],
            expected_version=source.version,
            source_sha256=retained_original(db, source),
            evidence=CommunicationEvidence(communication_id=source.id, excerpt=message.text_body, offset=0),
        )
        db.commit()
        linked = lane.world.client.post(
            f"/api/workspaces/{lane.world.ctx.tenant_id}/communications/{source_id}/invoice-links",
            json=first_command.model_dump(mode="json"),
        )
        assert linked.status_code == 200, linked.text
        db.expire_all()
        source = db.get(Communication, source_id)
        assert source is not None
        before_links = set(db.scalars(select(CommunicationInvoiceLink.invoice_id)))
        assert before_links == {invoices[0].id}
        command = LinkCommunication(
            customer_id=invoices[0].customer_id,
            invoice_ids=[r.id for r in invoices],
            expected_version=source.version,
            source_sha256=retained_original(db, source),
            evidence=CommunicationEvidence(communication_id=source.id, excerpt=message.text_body, offset=0),
        )
        source_id = source.id
        proposed = proposal(invoices[0], "link_communication", link_command=command.model_dump(mode="json"))
        db.commit()
        saved = run_sdk(
            lane.world,
            invoices,
            [proposed],
            extra_reads=[("get_communication_history", invoices[0].id)] if source_read else [],
        )
        if not source_read:
            assert saved["status"] == "failed" and saved["error_code"] == "COMMAND_SOURCE_NOT_READ", saved
            db.rollback()
            assert set(db.scalars(select(CommunicationInvoiceLink.invoice_id))) == before_links
            return
        assert saved["status"] == "completed", saved
        result = saved["result"]["proposals"][0]
        assert result["executed"] and result["changed_ids"] == [str(source_id)]
        db.rollback()
        from uuid import UUID

        action = db.get(ScheduledAction, UUID(result["action_id"]))
        assert (
            action is not None
            and action.status == "succeeded"
            and action.command["kind"] == "link_communication"
        )
        step = db.get(AgentStep, action.agent_step_id)
        assert step is not None and step.status == "completed"
        assert set(db.scalars(select(CommunicationInvoiceLink.invoice_id))) == {r.id for r in invoices}
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        assert db.scalar(select(func.count()).select_from(Communication)) == 1


def test_link_refuses_wrong_span_hash_scope_version_csrf_and_current_membership() -> None:
    from uuid import uuid4

    from aco.features.identity.models import Membership

    with ingress_world() as lane:
        incoming = lane.message()
        lane.world.store.rollback()
        asyncio.run(lane.deliver(incoming))
        asyncio.run(lane.work())
        db, client = lane.world.store, lane.world.client
        db.rollback()
        source = db.scalar(select(Communication))
        invoice = db.get(InvoiceSnapshot, lane.world.invoice_id)
        assert source is not None and invoice is not None
        path = f"/api/workspaces/{lane.world.ctx.tenant_id}/communications/{source.id}"
        source_id, customer_id, invoice_id = source.id, invoice.customer_id, invoice.id
        original_links = db.scalar(select(func.count()).select_from(CommunicationInvoiceLink))
        db.rollback()
        review = client.get(path + "/link-review").json()
        command = dict(
            customer_id=str(customer_id),
            invoice_ids=[str(invoice_id)],
            expected_version=review["communication_version"],
            source_sha256=review["source_sha256"],
            evidence=dict(communication_id=str(source_id), excerpt=incoming.text_body, offset=0),
        )
        for changed, expected in [
            ({"source_sha256": "0" * 64}, "COMMUNICATION_SOURCE_CHANGED"),
            ({"expected_version": review["communication_version"] + 1}, "COMMUNICATION_VERSION_CONFLICT"),
            ({"customer_id": str(uuid4())}, "COMMUNICATION_LINK_SCOPE_DENIED"),
            ({"invoice_ids": [str(uuid4())]}, "COMMUNICATION_LINK_SCOPE_DENIED"),
            (
                {"evidence": dict(communication_id=str(source_id), excerpt="invented", offset=0)},
                "EVIDENCE_SPAN_MISMATCH",
            ),
        ]:
            refused = client.post(path + "/invoice-links", json=command | changed)
            assert refused.status_code in {403, 409, 422} and refused.json()["detail"] == expected
        csrf = client.headers.pop("x-csrf-token")
        assert client.post(path + "/invoice-links", json=command).status_code == 403
        client.headers["x-csrf-token"] = csrf
        member = db.scalar(
            select(Membership).where(
                Membership.tenant_id == lane.world.ctx.tenant_id,
                Membership.user_id == lane.world.ctx.actor_id,
            )
        )
        assert member is not None
        member.role, member.version = "viewer", member.version + 1
        db.commit()
        assert client.post(path + "/invoice-links", json=command).status_code == 403
        assert client.get(path + "/link-review").json()["can_link"] is False
        db.rollback()
        assert db.scalar(select(func.count()).select_from(CommunicationInvoiceLink)) == original_links
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1


def test_cross_customer_original_remains_review_required_without_partial_linking() -> None:
    with ingress_world() as lane:
        incoming = lane.message(body="Synthetic ambiguous source mentions INV-101 and INV-102.")
        lane.world.store.rollback()
        asyncio.run(lane.deliver(incoming))
        asyncio.run(lane.work())
        db = lane.world.store
        db.rollback()
        source = db.scalar(select(Communication))
        assert source is not None
        key = source.id
        db.rollback()
        review = lane.world.client.get(
            f"/api/workspaces/{lane.world.ctx.tenant_id}/communications/{key}/link-review"
        )
        assert review.status_code == 200
        assert review.json()["can_link"] is False and review.json()["candidates"] == []
        assert review.json()["reasons"] == ["COMMUNICATION_CUSTOMER_AMBIGUOUS"]
        assert db.scalar(select(func.count()).select_from(CommunicationInvoiceLink)) == 0


def test_fixed_unknown_sender_reference_sample_preserves_source_and_requires_review() -> None:
    from uuid import uuid4

    from fastapi import FastAPI

    from aco.features.agent.models import AgentRun
    from aco.features.communications.models import EmailIngressReceipt

    with ingress_world() as lane:
        app = lane.world.client.app
        assert isinstance(app, FastAPI)
        app.state.email_ingress = lane.service
        root = f"/api/workspaces/{lane.world.ctx.tenant_id}"
        setup = lane.world.client.get(root + "/email-ingress/setup").json()
        activation = next(row for row in setup["activations"] if row["id"] == str(lane.activation_id))
        command = {
            "request_id": str(uuid4()),
            "expected_activation_version": activation["version"],
            "invoice_ids": [str(lane.world.invoice_id)],
            "scenario": "unverified_reference_reply",
        }
        path = root + f"/email-ingress/activations/{lane.activation_id}/simulate"
        response = lane.world.client.post(path, json=command)
        assert response.status_code == 202, response.text
        assert lane.world.client.post(path, json=command).json()["id"] == response.json()["id"]
        lane.world.store.rollback()
        asyncio.run(lane.work())
        db = lane.world.store
        db.rollback()
        source = db.scalar(select(Communication))
        receipt = db.scalar(select(EmailIngressReceipt))
        assert source is not None and receipt is not None
        assert receipt.state == "processed" and receipt.review_required and receipt.agent_run_id is None
        assert "INV-101" in str(source.content["text_body"])
        assert source.content["sender"] == "unknown@synthetic.invalid"
        assert db.scalar(select(func.count()).select_from(CommunicationInvoiceLink)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0
        key = source.id
        db.rollback()
        review = lane.world.client.get(root + f"/communications/{key}/link-review")
        assert review.status_code == 200 and review.json()["can_link"] is True


def test_retained_origin_and_rfc_context_remain_valid_without_inventing_invoice_mentions() -> None:
    with ingress_world() as lane:
        first = lane.message(body="Synthetic source refers to INV-101 and INV-107.")
        assert first.provider_thread_ref is not None and first.message_id is not None
        lane.world.store.rollback()
        asyncio.run(lane.deliver(first))
        asyncio.run(lane.work())
        reply = lane.message(
            thread=first.provider_thread_ref.external_id,
            body="Synthetic reply: please review this prior context.",
        )
        mime = BytesParser(policy=policy.default).parsebytes(reply.source.content or b"")
        mime["In-Reply-To"] = first.message_id
        mime["References"] = first.message_id
        raw = mime.as_bytes()
        reply = reply.model_copy(
            update={
                "in_reply_to": first.message_id,
                "references": (first.message_id,),
                "headers": tuple(mime.items()),
                "source": EmailSource(
                    kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
                ),
            }
        )
        lane.world.store.rollback()
        asyncio.run(lane.deliver(reply))
        asyncio.run(lane.work())
        db = lane.world.store
        db.rollback()
        source = db.scalar(
            select(Communication).where(Communication.native_message_id == reply.provider_ref.external_id)
        )
        assert source is not None
        key = source.id
        retained_origin = source.origin_invoice_id
        assert retained_origin is not None
        db.rollback()
        path = f"/api/workspaces/{lane.world.ctx.tenant_id}/communications/{key}"
        review = lane.world.client.get(path + "/link-review").json()
        assert review["can_link"] is True, review
        assert {c["number"] for c in review["candidates"]} == {"INV-101", "INV-107"}
        assert all(
            c["provenance"]
            == ("origin_thread" if c["invoice_id"] == str(retained_origin) else "contextual_inference")
            for c in review["candidates"]
        )
        command = dict(
            customer_id=review["candidates"][0]["customer_id"],
            invoice_ids=[c["invoice_id"] for c in review["candidates"]],
            expected_version=review["communication_version"],
            source_sha256=review["source_sha256"],
            evidence=dict(communication_id=str(key), excerpt=reply.text_body, offset=0),
        )
        linked = lane.world.client.post(path + "/invoice-links", json=command)
        assert linked.status_code == 200, linked.text
        db.rollback()
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 2
        reviewed = list(
            db.scalars(
                select(CommunicationInvoiceLink).where(
                    CommunicationInvoiceLink.communication_id == key,
                    CommunicationInvoiceLink.classification == "reviewed_reference",
                )
            )
        )
        assert len(reviewed) == 2 and all(link.evidence_excerpt == reply.text_body for link in reviewed)


@pytest.mark.parametrize("rfc_reply", [False, True])
def test_reviewed_null_thread_does_not_transfer_customer_authority_after_real_source_correction(
    rfc_reply: bool,
) -> None:
    from accounting_support import apply, publish_invoice

    from aco.features.accounting.models import Customer

    with ingress_world() as lane:
        incoming = unverified_message(lane, "Synthetic original refers to INV-101.")
        lane.world.store.rollback()
        asyncio.run(lane.deliver(incoming))
        asyncio.run(lane.work())
        db, client = lane.world.store, lane.world.client
        db.rollback()
        source = db.scalar(select(Communication))
        invoice = db.get_one(InvoiceSnapshot, lane.world.invoice_id)
        assert source is not None
        source_id, invoice_id, old_customer = source.id, invoice.id, invoice.customer_id
        path = f"/api/workspaces/{lane.world.ctx.tenant_id}/communications/{source_id}"
        db.rollback()
        review = client.get(path + "/link-review").json()
        command = dict(
            customer_id=str(old_customer),
            invoice_ids=[str(invoice_id)],
            expected_version=review["communication_version"],
            source_sha256=review["source_sha256"],
            evidence=dict(communication_id=str(source_id), excerpt=incoming.text_body, offset=0),
        )
        assert client.post(path + "/invoice-links", json=command).status_code == 200
        other = db.scalar(select(Customer).where(Customer.id != old_customer))
        assert other is not None
        event = publish_invoice(
            db,
            lane.world.ctx.tenant_id,
            lane.world.connection_id,
            external_customer_id=other.external_customer_id,
        )
        db.rollback()
        apply(client.app, lane.world.ctx, event)
        new_customer = other.id
        new_timeline = client.get(
            f"/api/workspaces/{lane.world.ctx.tenant_id}/communications",
            params={"customer_id": str(new_customer)},
        )
        assert new_timeline.status_code == 200
        assert str(source_id) not in {row["id"] for row in new_timeline.json()["items"]}
        old_timeline = client.get(
            f"/api/workspaces/{lane.world.ctx.tenant_id}/communications",
            params={"customer_id": str(old_customer)},
        )
        assert old_timeline.status_code == 200
        assert str(source_id) in {row["id"] for row in old_timeline.json()["items"]}
        customer_base = f"/api/workspaces/{lane.world.ctx.tenant_id}/customers"
        assert client.get(f"{customer_base}/{old_customer}").json()["interactions"]["original_count"] == 1
        assert client.get(f"{customer_base}/{new_customer}").json()["interactions"]["original_count"] == 0
        if rfc_reply:
            assert incoming.provider_thread_ref is not None and incoming.message_id is not None
            reply = lane.message(
                thread=incoming.provider_thread_ref.external_id,
                body="Synthetic reply about the retained prior context.",
            )
            mime = BytesParser(policy=policy.default).parsebytes(reply.source.content or b"")
            mime.replace_header("From", "unverified-review@synthetic.invalid")
            mime["In-Reply-To"] = incoming.message_id
            mime["References"] = incoming.message_id
            raw = mime.as_bytes()
            reply = reply.model_copy(
                update={
                    "sender": "unverified-review@synthetic.invalid",
                    "in_reply_to": incoming.message_id,
                    "references": (incoming.message_id,),
                    "headers": tuple(mime.items()),
                    "source": EmailSource(
                        kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
                    ),
                }
            )
            db.rollback()
            asyncio.run(lane.deliver(reply))
            asyncio.run(lane.work())
            db.rollback()
            child = db.scalar(
                select(Communication).where(Communication.native_message_id == reply.provider_ref.external_id)
            )
            assert child is not None
            path = f"/api/workspaces/{lane.world.ctx.tenant_id}/communications/{child.id}"
            db.rollback()
        reviewed = client.get(path + "/link-review").json()
        assert reviewed["can_link"] is False, reviewed
        assert reviewed["reasons"] == ["COMMUNICATION_CUSTOMER_CHANGED"]
        db.rollback()
        assert db.scalar(select(func.count()).select_from(CommunicationInvoiceLink)) == 1
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == (2 if rfc_reply else 1)


@pytest.mark.parametrize("surface", ["link_review", "customer_timeline"])
def test_human_link_review_rejects_genuine_invoice_service_context(surface: str) -> None:
    from typing import cast

    from action_support import enqueue
    from fastapi import HTTPException
    from pydantic import SecretStr

    from aco.core.context import TenantContext
    from aco.core.settings import Settings
    from aco.features.actions.service import ActionService, worker_context
    from aco.features.communications.association import link_review
    from aco.features.communications.queries import CommunicationQueries
    from aco.features.identity.service import IdentityService, Permission
    from aco.worker.app import WorkerRuntime

    with ingress_world() as lane:
        incoming = unverified_message(lane, "Synthetic unrelated source refers to INV-103.")
        lane.world.store.rollback()
        asyncio.run(lane.deliver(incoming))
        asyncio.run(lane.work())
        world = lane.world
        world.store.rollback()
        target = world.store.scalar(select(Communication))
        assert target is not None
        target_id = target.id
        assert (
            world.store.scalar(
                select(CommunicationInvoiceLink.invoice_id).where(
                    CommunicationInvoiceLink.communication_id == target_id
                )
            )
            is None
        )
        world.store.rollback()
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            try:
                async with runtime.app.open_async():
                    await runtime.dispatcher.dispatch_outbox(10)
                action = world.action(ref.action_id)
                context = worker_context(action)
                lease = ActionService(world.store).claim_action(context, action.id, action.version)
                world.store.commit()
                service = context.model_copy(update={"lease_token": lease.token})
                async with runtime.sessions.begin() as db:
                    await db.run_sync(
                        lambda store: IdentityService(store).authorize(service, Permission.INVOICE_READ)
                    )
                    # Deliberate annotation bypass tests the real runtime boundary, not a new authority.
                    with pytest.raises(HTTPException, match="HUMAN_CONTEXT_REQUIRED"):
                        if surface == "link_review":
                            await db.run_sync(
                                lambda store: link_review(store, cast(TenantContext, service), target_id)
                            )
                        else:
                            await db.run_sync(
                                lambda store: CommunicationQueries(store, cast(TenantContext, service))
                            )
            finally:
                await runtime.engine.dispose()

        asyncio.run(run())


def test_retained_nonnull_thread_keeps_original_customer_timeline_after_source_correction() -> None:
    from accounting_support import apply, publish_invoice

    from aco.features.accounting.models import Customer

    with ingress_world() as lane:
        incoming = lane.message(body="Synthetic original references INV-101.")
        lane.world.store.rollback()
        asyncio.run(lane.deliver(incoming))
        asyncio.run(lane.work())
        db, client = lane.world.store, lane.world.client
        db.rollback()
        source = db.scalar(select(Communication))
        invoice = db.get_one(InvoiceSnapshot, lane.world.invoice_id)
        assert source is not None and source.origin_invoice_id == invoice.id
        source_id, invoice_id, old_customer = source.id, invoice.id, invoice.customer_id
        other = db.scalar(select(Customer).where(Customer.id != old_customer))
        assert other is not None
        new_customer = other.id
        event = publish_invoice(
            db,
            lane.world.ctx.tenant_id,
            lane.world.connection_id,
            external_customer_id=other.external_customer_id,
        )
        db.rollback()
        apply(client.app, lane.world.ctx, event)
        base = f"/api/workspaces/{lane.world.ctx.tenant_id}/communications"
        for customer, present in ((old_customer, True), (new_customer, False)):
            response = client.get(base, params={"customer_id": str(customer)})
            assert response.status_code == 200, response.text
            assert (str(source_id) in {row["id"] for row in response.json()["items"]}) is present
            detail = client.get(f"/api/workspaces/{lane.world.ctx.tenant_id}/customers/{customer}")
            assert detail.status_code == 200
            assert detail.json()["interactions"]["original_count"] == int(present)
        history = client.get(base, params={"invoice_id": str(invoice_id)})
        assert history.status_code == 200
        assert str(source_id) in {row["id"] for row in history.json()["items"]}
        db.rollback()
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1
