"""Actual SDK/maintained-worker context reads never widen action authority."""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from accounting_support import seed
from action_support import action_world
from case_promise_support import communication
from evaluations import test_collection_agent as sdk_fixture
from evaluations.test_collection_agent import run_sdk
from fastapi import HTTPException
from openai_support import response as sdk_response
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import CustomerContact, DemoSourceRecord, InvoiceSnapshot
from aco.features.agent.models import AgentStep
from aco.features.agent.nodes import InvestigationNode
from aco.features.agent.schemas import GraphState
from aco.features.cases.dto import CommunicationEvidence
from aco.features.identity.models import Membership, now
from aco.features.policies.guards import PolicyGuards
from aco.features.policies.models import RecipientControl
from aco.features.policies.service import PolicyService
from aco.features.promises.dto import RecordPromise
from aco.features.promises.models import CustomerScopeReview
from aco.features.promises.service import PromiseService
from aco.features.tools.gateway import execute_internal
from aco.features.tools.internal_followup import RequestHumanReview
from aco.features.tools.reads import ScopedReads
from aco.features.tools.schemas import CustomerInvoiceFact, InvoiceQuery, PolicyContactFact, ReadEvidence


def test_sdk_customer_inventory_and_contact_snapshot_keep_current_scoped_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with action_world() as world:
        db = world.store
        anchor = db.get(InvoiceSnapshot, world.invoice_id)
        assert anchor is not None
        expected = list(
            db.scalars(
                select(InvoiceSnapshot.id).where(
                    InvoiceSnapshot.tenant_id == anchor.tenant_id,
                    InvoiceSnapshot.customer_id == anchor.customer_id,
                    InvoiceSnapshot.provider_connection_id == anchor.provider_connection_id,
                )
            )
        )
        assert len(expected) > 1
        contact = db.scalar(select(CustomerContact).where(CustomerContact.customer_id == anchor.customer_id))
        assert contact is not None and contact.email
        control = RecipientControl(
            tenant_id=anchor.tenant_id,
            provider_connection_id=anchor.provider_connection_id,
            contact_id=contact.id,
            reviewer_id=world.ctx.actor_id,
            reviewed_destination=contact.email,
            reviewed_contact_revision=contact.source_revision,
            timezone="UTC",
            timezone_verified=True,
            destination_verified=True,
            allowed_channels=["email"],
            opt_out=True,
            evidence="Explicit synthetic contact read fixture",
        )
        db.add(control)
        PolicyService(db).place_hold(world.ctx, anchor.id, "active_dispute", "Actual current read hold")
        reservation = PolicyGuards(db, now()).reserve_contact(
            world.ctx, anchor.id, contact.email, "email", uuid4()
        )
        # Explicit retained uncertainty fixture; no provider event or send is claimed.
        reservation.status = "unknown_outcome"
        reservation.submitted_at = now() - timedelta(minutes=5)
        db.commit()
        message = communication(db, anchor.tenant_id, [anchor], "I may pay some of this balance.")
        pending = PromiseService(db).record_promise(
            world.ctx,
            RecordPromise(
                customer_id=anchor.customer_id,
                invoice_ids=[],
                candidate_invoice_ids=[anchor.id],
                evidence=CommunicationEvidence(
                    communication_id=message.id, excerpt=str(message.content["text_body"]), offset=0
                ),
                scope="ambiguous",
            ),
        )
        review = db.scalar(select(CustomerScopeReview).where(CustomerScopeReview.promise_id == pending.id))
        assert review is not None
        review.expires_at = now() - timedelta(days=1)
        db.commit()
        made = world.client.post("/api/workspaces", json={"name": "Foreign read tenant", "timezone": "UTC"})
        assert made.status_code == 201
        seed(world.client, UUID(made.json()["id"]))
        saved = run_sdk(
            world,
            [anchor],
            [],
            extra_reads=[
                ("list_customer_invoices", anchor.id),
                ("get_policy_and_contact_status", anchor.id),
            ],
        )
        assert saved["status"] == "completed", saved
        db.rollback()
        steps = list(db.scalars(select(AgentStep).where(AgentStep.run_id == UUID(saved["id"]))))
        inventory = next(row.output for row in steps if row.tool_name == "list_customer_invoices")
        status = next(row.output for row in steps if row.tool_name == "get_policy_and_contact_status")
        assert inventory and status
        inventory_facts = ReadEvidence.model_validate_json(json.dumps(inventory))
        status_facts = ReadEvidence.model_validate_json(json.dumps(status))
        inventory_ids = set()
        for fact in inventory_facts.records:
            assert isinstance(fact, CustomerInvoiceFact)
            inventory_ids.add(fact.invoice.invoice_id)
            assert fact.action_scope == "CONTEXT_ONLY"
        assert inventory_ids == set(expected)
        assert inventory_facts.truncated is False and inventory_facts.as_of
        current = status_facts.records[0]
        assert isinstance(current, PolicyContactFact)
        assert current.invoice_id == anchor.id
        assert "active_dispute" in current.hold_reasons and current.active_hold_count >= 1
        assert current.recipient is not None
        assert current.recipient.opt_out is True
        assert current.recipient.review_matches_current_source is True
        assert current.verified_email_recipient_available is False
        assert current.customer_exposure_24h == current.recipient_exposure_24h == 1
        assert current.unknown_exposure_7d == 1
        assert current.unresolved_customer_scope_reviews == 1
        assert "scope_review" in current.hold_reasons
        assert current.policy_state == "UNAVAILABLE" and current.policy is None
        assert current.outbound_paused is True
        assert current.dispatch_authority == "NOT_GRANTED"
        assert current.source_freshness_basis == "LAST_KNOWN_SNAPSHOT_NOT_DISPATCH_READ"


@pytest.mark.parametrize("revoke", [False, True])
def test_context_reads_recheck_current_run_and_never_expand_action_scope(
    monkeypatch: pytest.MonkeyPatch,
    revoke: bool,
) -> None:
    original = InvestigationNode.execute
    checked = False
    with action_world() as world:
        db = world.store
        anchor = db.get(InvoiceSnapshot, world.invoice_id)
        assert anchor is not None
        unselected = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == anchor.tenant_id,
                InvoiceSnapshot.customer_id == anchor.customer_id,
                InvoiceSnapshot.id != anchor.id,
            )
        )
        unrelated = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == anchor.tenant_id,
                InvoiceSnapshot.customer_id != anchor.customer_id,
            )
        )
        assert unselected is not None and unrelated is not None
        made = world.client.post("/api/workspaces", json={"name": "Foreign scoped anchor", "timezone": "UTC"})
        assert made.status_code == 201
        foreign_tenant = UUID(made.json()["id"])
        seed(world.client, foreign_tenant)
        foreign = db.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == foreign_tenant))
        assert foreign is not None
        denied_ids = (unselected.id, unrelated.id, foreign.id)
        anchor_id = anchor.id

        async def inspect(self: InvestigationNode, state: GraphState) -> GraphState:
            nonlocal checked
            reads = ScopedReads(self.sessions, self.authority)
            tools = {tool.name: tool for tool in reads.tools()}
            if revoke:
                db.rollback()
                member = db.scalar(
                    select(Membership).where(
                        Membership.tenant_id == world.ctx.tenant_id,
                        Membership.user_id == world.ctx.actor_id,
                    )
                )
                assert member is not None
                member.version += 1
                db.commit()
            for name in ("list_customer_invoices", "get_policy_and_contact_status"):
                for target in (anchor_id,) if revoke else denied_ids:
                    with pytest.raises(HTTPException) as refused:
                        await tools[name].invoke(InvoiceQuery(invoice_id=target))
                    assert refused.value.status_code == 403
                    assert refused.value.detail == (
                        "RUN_AUTHORITY_REVOKED" if revoke else "INVOICE_OUTSIDE_RUN_SCOPE"
                    )
            if not revoke:
                async with self.sessions.begin() as session:

                    def attempt(store: Session) -> None:
                        evidence = list(
                            store.scalars(
                                select(AgentStep.id).where(
                                    AgentStep.run_id == self.authority.run_id,
                                    AgentStep.kind == "read",
                                    AgentStep.status == "completed",
                                )
                            )
                        )
                        command = RequestHumanReview(
                            invoice_id=denied_ids[0],
                            title="Unselected inventory is not authority",
                            evidence_step_ids=evidence,
                        )
                        with pytest.raises(HTTPException) as refused:
                            execute_internal(store, self.authority, command, 99, command.invoice_id, evidence)
                        assert (
                            refused.value.status_code == 403
                            and refused.value.detail == "INVOICE_OUTSIDE_RUN_SCOPE"
                        )

                    await session.run_sync(attempt)
            checked = True
            return await original(self, state)

        monkeypatch.setattr(InvestigationNode, "execute", inspect)
        saved = run_sdk(world, [anchor], [], extra_reads=[("list_customer_invoices", anchor.id)])
        assert checked
        assert saved["status"] == ("failed" if revoke else "completed")


def test_customer_inventory_is_deterministically_bounded_after_actual_source_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = InvestigationNode.execute
    checked = False
    with action_world() as world:
        db = world.store
        anchor = db.get(InvoiceSnapshot, world.invoice_id)
        assert anchor is not None
        source = db.scalar(
            select(DemoSourceRecord)
            .where(
                DemoSourceRecord.tenant_id == anchor.tenant_id,
                DemoSourceRecord.provider_connection_id == anchor.provider_connection_id,
                DemoSourceRecord.kind == "invoice",
                DemoSourceRecord.external_id == anchor.external_invoice_id,
            )
            .order_by(DemoSourceRecord.revision.desc())
        )
        assert source is not None
        for index in range(50):
            key = f"SYNTHETIC-CONTEXT-{index:03}"
            payload = deepcopy(source.payload)
            payload.update(external_id=key, number=key)
            db.add(
                DemoSourceRecord(
                    tenant_id=source.tenant_id,
                    provider_connection_id=source.provider_connection_id,
                    organisation_id=source.organisation_id,
                    kind="invoice",
                    external_id=key,
                    revision=1,
                    payload=payload,
                    effective_at=now(),
                    observed_at=now(),
                )
            )
        db.commit()
        imported = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/accounting/sync",
            json={"provider_connection_id": str(anchor.provider_connection_id)},
        )
        assert imported.status_code == 200, imported.text
        db.rollback()
        expected = list(
            db.scalars(
                select(InvoiceSnapshot.id)
                .where(
                    InvoiceSnapshot.tenant_id == anchor.tenant_id,
                    InvoiceSnapshot.customer_id == anchor.customer_id,
                    InvoiceSnapshot.provider_connection_id == anchor.provider_connection_id,
                )
                .order_by(InvoiceSnapshot.number, InvoiceSnapshot.id)
            )
        )
        assert len(expected) > 50

        async def inspect(self: InvestigationNode, state: GraphState) -> GraphState:
            nonlocal checked
            tools = {tool.name: tool for tool in ScopedReads(self.sessions, self.authority).tools()}
            value = await tools["list_customer_invoices"].invoke(InvoiceQuery(invoice_id=anchor.id))
            assert value.truncated is True and len(value.records) == 50
            assert [row.model_dump()["invoice"]["invoice_id"] for row in value.records] == expected[:50]
            assert all(row.model_dump()["invoice"]["source"]["synthetic"] for row in value.records)
            assert value.as_of is not None and value.basis == "LAST_KNOWN_SNAPSHOT"
            checked = True
            return await original(self, state)

        monkeypatch.setattr(InvestigationNode, "execute", inspect)
        from pydantic import SecretStr

        from aco.core.settings import Settings
        from aco.features.agent.graph import dispatch_agent_runs
        from aco.worker.app import WorkerRuntime

        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        db.rollback()
        started = world.client.post(
            path,
            json={
                "invoice_ids": [str(anchor.id)],
                "objective": "Bounded synthetic context read",
                "request_id": str(uuid4()),
            },
        )
        assert started.status_code == 202

        async def work() -> None:
            runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
            try:
                async with runtime.app.open_async():
                    await dispatch_agent_runs(runtime.sessions, runtime.app)
                    await asyncio.wait_for(
                        runtime.app.run_worker_async(
                            queues=["aco"], concurrency=1, wait=False, listen_notify=False
                        ),
                        35,
                    )
            finally:
                await runtime.engine.dispose()

        db.rollback()  # Never retain a fixture snapshot across maintained checkpoint bootstrap.
        asyncio.run(work())
        saved = world.client.get(path + "/" + started.json()["id"]).json()
        assert checked and saved["status"] == "completed"


@pytest.mark.parametrize("source_state", ["current", "expired", "customer_changed"])
def test_sdk_case_evidence_requires_current_private_original_and_exact_customer(source_state: str) -> None:
    from accounting_support import apply, publish_invoice
    from ingress_support import ingress_world

    from aco.features.accounting.models import Customer
    from aco.features.artifacts.models import Artifact
    from aco.features.cases.dto import UpsertCase
    from aco.features.cases.service import CaseService
    from aco.features.communications.models import Communication, CommunicationSource

    with ingress_world() as lane:
        world = lane.world
        message = lane.message(body="Synthetic original: INV-101 has a disputed amount.")
        world.store.rollback()
        asyncio.run(lane.deliver(message))
        asyncio.run(lane.work())
        db = world.store
        db.rollback()
        anchor = db.get_one(InvoiceSnapshot, world.invoice_id)
        incoming = db.scalar(select(Communication).where(Communication.direction == "incoming"))
        assert incoming is not None and incoming.source_state == "stored"
        body = str(incoming.content["text_body"])
        created = (
            CaseService(db)
            .upsert_case(
                world.ctx,
                UpsertCase(
                    customer_id=anchor.customer_id,
                    invoice_ids=[anchor.id],
                    issue_type="wrong_amount",
                    claim_key="actual-retained-context",
                    substance="Customer dispute pending verification",
                    evidence=CommunicationEvidence(communication_id=incoming.id, excerpt=body, offset=0),
                ),
            )
            .case
        )
        other = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == anchor.tenant_id,
                InvoiceSnapshot.customer_id != anchor.customer_id,
            )
        )
        assert other is not None
        other_message = communication(db, anchor.tenant_id, [other], "Unrelated private claim")
        other_case = (
            CaseService(db)
            .upsert_case(
                world.ctx,
                UpsertCase(
                    customer_id=other.customer_id,
                    invoice_ids=[other.id],
                    issue_type="other",
                    claim_key="unrelated",
                    substance="Must not be returned",
                    evidence=CommunicationEvidence(
                        communication_id=other_message.id, excerpt="Unrelated private claim", offset=0
                    ),
                ),
            )
            .case
        )
        db.commit()
        if source_state == "expired":
            source = db.scalar(
                select(CommunicationSource).where(
                    CommunicationSource.communication_id == incoming.id,
                    CommunicationSource.representation == "provider_original",
                )
            )
            assert source is not None
            artifact = db.get_one(Artifact, source.artifact_id)
            artifact.expires_at = now() - timedelta(seconds=1)
            db.commit()
        elif source_state == "customer_changed":
            external = db.get_one(Customer, other.customer_id).external_customer_id
            event = publish_invoice(
                db, world.ctx.tenant_id, world.connection_id, external_customer_id=external
            )
            db.rollback()
            apply(world.client.app, world.ctx, event)
            db.expire_all()
            anchor = db.get_one(InvoiceSnapshot, world.invoice_id)
        saved = run_sdk(world, [anchor], [], extra_reads=[("search_case_evidence", anchor.id)])
        assert saved["status"] == "completed", saved
        db.rollback()
        step = db.scalar(
            select(AgentStep).where(
                AgentStep.run_id == UUID(saved["id"]), AgentStep.tool_name == "search_case_evidence"
            )
        )
        assert step is not None and step.output is not None
        records = step.output["records"]
        assert isinstance(records, list)
        assert all(row["case_id"] != str(other_case.id) for row in records)
        if source_state == "customer_changed":
            assert records == []
        else:
            assert len(records) == 1
            record = records[0]
            assert record["case_id"] == str(created.id)
            assert record["available"] is (source_state == "current")
            if source_state == "current":
                assert record["excerpt"] == body and record["communication_id"] == str(incoming.id)
                assert record["source_sha256"] and record["source_state"] == "stored"
            else:
                assert record["excerpt"] is None and record["communication_id"] is None
                assert record["unavailable_reason"] == "CURRENT_SOURCE_UNAVAILABLE"


async def test_sdk_case_evidence_retains_actual_signed_voice_reference_without_original() -> None:
    from action_support import World
    from sqlalchemy import func
    from voice_support import VoiceCallback, exercise_voice

    from aco.features.cases.models import CaseEvidence
    from aco.features.communications.models import Communication
    from aco.features.voice.dto import VoiceSessionView
    from aco.features.voice.models import VoiceBusinessEvidence
    from aco.features.voice.runtime import VoiceRuntime

    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        invoice = world.store.get_one(InvoiceSnapshot, world.invoice_id)
        world.store.rollback()
        await callback(
            "case-evidence-agent-read",
            "upsert_case",
            {
                "invoice_id": str(invoice.id),
                "case_facts": {
                    "invoice_ids": [str(invoice.id)],
                    "issue_type": "wrong_amount",
                    "claim_reference": "exact-signed-operational-claim",
                },
            },
        )
        world.store.rollback()
        evidence = world.store.scalar(select(CaseEvidence))
        assert evidence is not None and evidence.communication_id is None
        source = world.store.get_one(VoiceBusinessEvidence, evidence.voice_evidence_id)
        source_id, facts_hash = source.id, source.facts_hash
        world.store.rollback()
        saved = await asyncio.to_thread(
            run_sdk, world, [invoice], [], extra_reads=[("search_case_evidence", invoice.id)]
        )
        assert saved["status"] == "completed", saved
        world.store.rollback()
        step = world.store.scalar(
            select(AgentStep).where(
                AgentStep.run_id == UUID(saved["id"]),
                AgentStep.tool_name == "search_case_evidence",
            )
        )
        assert step is not None and step.output is not None
        records = step.output["records"]
        assert isinstance(records, list) and len(records) == 1
        record = records[0]
        assert record["available"] and record["source_kind"] == "voice_operational"
        assert record["voice_source"] == {
            "kind": "voice_operational",
            "voice_evidence_id": str(source_id),
            "facts_hash": facts_hash,
        }
        assert record["source_state"] == "VOICE_OPERATIONAL_METADATA"
        assert record["communication_id"] is record["excerpt"] is record["source_sha256"] is None
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 0

    await exercise_voice("case-evidence-agent-read", scenario)


@pytest.mark.parametrize("effect", ["create_task", "request_task_approval", "upsert_case"])
def test_sdk_alias_only_reads_preserve_exact_action_and_review_evidence(
    monkeypatch: pytest.MonkeyPatch, effect: str
) -> None:
    from evaluations.test_collection_agent import proposal
    from integration.test_case_task_approval import prepare_case

    from aco.features.agent.context import current_run
    from aco.features.cases.models import CaseEvidence, CollectionCase
    from aco.features.review.agent_approvals import internal_scope
    from aco.features.review.models import ApprovalRequest, CollectionTask
    from aco.features.tools.email import email_evidence
    from aco.features.voice.agent_request import RequestVoiceCall, validate_voice_request

    original_response = sdk_response
    original_execute = InvestigationNode.execute
    checked = False

    def alias_response(**kwargs: Any) -> Any:
        for item in kwargs.get("output", []):
            if item.get("type") == "function_call":
                item["name"] = {
                    "get_verified_payments": "get_payment_records",
                    "find_existing_cases": "find_collection_cases",
                }.get(item["name"], item["name"])
        return original_response(**kwargs)

    async def check_producers(node: InvestigationNode, state: GraphState) -> GraphState:
        nonlocal checked
        async with node.sessions.begin() as session:

            def check(db: Session) -> None:
                nonlocal checked
                run = current_run(db, node.authority)
                invoice = db.get_one(InvoiceSnapshot, world.invoice_id)
                ids = list(
                    db.scalars(
                        select(AgentStep.id).where(
                            AgentStep.run_id == run.id,
                            AgentStep.kind == "read",
                            AgentStep.status == "completed",
                        )
                    )
                )
                assert len(email_evidence(db, run, invoice, ids)) == (4 if effect == "upsert_case" else 3)
                assert (
                    len(
                        validate_voice_request(
                            db,
                            node.authority,
                            RequestVoiceCall(
                                invoice_id=invoice.id,
                                invoice_ids=[invoice.id],
                                objective="Review actual source",
                            ),
                        )
                    )
                    == 2
                )
                checked = True

            await session.run_sync(check)
        return await original_execute(node, state)

    monkeypatch.setattr(sdk_fixture, "response", alias_response)
    monkeypatch.setattr(InvestigationNode, "execute", check_producers)
    with action_world() as world:
        case_id, version = prepare_case(world)
        invoice = world.store.get_one(InvoiceSnapshot, world.invoice_id)
        case = world.store.get_one(CollectionCase, case_id)
        source = world.store.scalar(select(CaseEvidence).where(CaseEvidence.case_id == case_id))
        assert source is not None and source.communication_id is not None
        binding = {"case_id": str(case_id), "case_version": version}
        command = proposal(invoice, effect, case_binding=binding, title="Alias evidence task")
        if effect == "upsert_case":
            command = proposal(
                invoice,
                effect,
                case_command={
                    "customer_id": str(invoice.customer_id),
                    "invoice_ids": [str(invoice.id)],
                    "issue_type": case.issue_type,
                    "claim_key": case.claim_key,
                    "substance": "Retained case reviewed using the registered read alias",
                    "matching": "same",
                    "matched_case_id": str(case_id),
                    "expected_version": version,
                    "evidence": {
                        "communication_id": str(source.communication_id),
                        "excerpt": source.excerpt,
                        "offset": source.offset,
                    },
                },
            )
        extra = [("find_existing_cases", invoice.id)]
        if effect == "upsert_case":
            extra.append(("get_communication_history", invoice.id))
        saved = run_sdk(world, [invoice], [command], extra_reads=extra)
        assert saved["status"] == (
            "awaiting_approval" if effect == "request_task_approval" else "completed"
        ), saved
        assert checked
        world.store.rollback()
        names = set(
            world.store.scalars(
                select(AgentStep.tool_name).where(
                    AgentStep.run_id == UUID(saved["id"]), AgentStep.kind == "read"
                )
            )
        )
        assert names == {"get_invoice", "get_payment_records", "find_collection_cases"} | (
            {"get_communication_history"} if effect == "upsert_case" else set()
        )
        result = saved["result"]["proposals"][0]
        if effect == "request_task_approval":
            approval = world.store.get_one(ApprovalRequest, UUID(result["approval_id"]))
            assert internal_scope(world.store, approval)[-1] == []
        elif effect == "create_task":
            task = world.store.get_one(CollectionTask, UUID(result["changed_ids"][0]))
            assert task.title == "Alias evidence task"
        else:
            assert result["executed"] is True and result["changed_ids"] == [str(case_id)]
