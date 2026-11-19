"""Ten golden consumers: actual SDK/graph/domain or signed provider boundary, never live IO.

The HTTP double chooses model output; it does not stand in for tools or business services.
SDK-backed runs intentionally use the explicit offline LIVE-composition fixture, not a claim
of actual model judgement. Default production FAKE and genuine live acceptance stay separate.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from action_support import World, action_world
from case_promise_support import communication
from openai_support import response
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.settings import Settings
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.live import (
    BoundedLiveBudget,
    LiveGrantConfig,
    install_bounded_live_grant,
)
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.agent.usage import RunModelGuard
from aco.features.cases.models import CaseEvidence, CollectionCase
from aco.features.communications.models import Communication, CommunicationInvoiceLink
from aco.features.identity.models import Tenant, now
from aco.features.policies.models import CollectionHold
from aco.features.promises.models import PaymentPromise
from aco.features.review.models import CollectionTask
from aco.features.usage.models import BudgetReservation, ProviderUsageEvent, UsageRevision
from aco.features.voice.runtime import VoiceRuntime
from aco.integrations.openai.adapter import OpenAIModelAdapter
from aco.integrations.openai.settings import OpenAISettings
from aco.worker.app import WorkerRuntime


class Golden(BaseModel):
    id: str
    story: str
    message: str
    expected: dict[str, str | int | bool]


def fixtures() -> list[Golden]:
    value = json.loads(Path("tests/fixtures/scenarios/demo_stories.json").read_text())
    cases = [Golden.model_validate(v) for v in value["cases"]]
    assert [v.id for v in cases] == [f"S{i}" for i in range(1, 11)]
    return cases


def count(world: World, model: Any) -> int:
    return int(world.store.scalar(select(func.count()).select_from(model)) or 0)


def financial(world: World) -> list[tuple[UUID, int, str, str]]:
    world.store.expire_all()
    return [
        (r.id, r.balance_minor, r.financial_status, r.source_revision)
        for r in world.store.scalars(select(InvoiceSnapshot).order_by(InvoiceSnapshot.id))
    ]


def evidence(message: Communication) -> dict[str, object]:
    return {"communication_id": str(message.id), "excerpt": message.content["text_body"], "offset": 0}


def promise(row: InvoiceSnapshot, message: Communication, **updates: object) -> dict[str, object]:
    return (
        dict(
            customer_id=str(row.customer_id),
            invoice_ids=[str(row.id)],
            evidence=evidence(message),
            due_date="2026-12-01",
            timezone="UTC",
            amount={"minor_units": 10000, "currency": "AUD", "exponent": 2},
        )
        | updates
    )


def dispute(
    rows: list[InvoiceSnapshot], message: Communication, *, payment: bool = False
) -> dict[str, object]:
    return dict(
        customer_id=str(rows[0].customer_id),
        invoice_ids=[str(r.id) for r in rows],
        evidence=evidence(message),
        issue_type="payment_not_found" if payment else "wrong_amount",
        claim_key="claimed-unverified-payment" if payment else "contract-rate",
        substance="Customer reports payment; verify source" if payment else "Rate differs from contract",
    )


def proposal(row: InvoiceSnapshot, kind: str, **updates: object) -> dict[str, object]:
    return (
        dict(
            invoice_id=str(row.id),
            kind=kind,
            reason="Explicit synthetic source needs guarded review",
            executed=False,
        )
        | updates
    )


def run_sdk(
    world: World,
    rows: list[InvoiceSnapshot],
    proposals: list[dict[str, object]],
    *,
    extra_reads: list[tuple[str, UUID]] | None = None,
    voice_runtime_factory: Callable[[async_sessionmaker[AsyncSession]], VoiceRuntime] | None = None,
) -> dict[str, Any]:
    """Actual public start -> maintained worker -> SDK HTTP -> scoped reads -> graph gateway."""
    grant = install_bounded_live_grant(
        world.store,
        tenant_id=world.ctx.tenant_id,
        initiator_id=world.ctx.actor_id,
        authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
        config=LiveGrantConfig(
            model_id="gpt-6.1-sol",
            expires_at=now() + timedelta(minutes=10),
            price_source_at=now(),
            input_usd_per_token=Decimal(".000005"),
            output_usd_per_token=Decimal(".000010"),
            max_requests=3,
            max_input_tokens=64000,
            max_output_tokens=2048,
            amount=Decimal("1"),
        ),
    )
    world.store.commit()
    grant_id = grant.id
    world.client.app.state.agent_live_grant_id = grant_id
    path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
    command = {
        "invoice_ids": [str(r.id) for r in rows],
        "objective": "Interpret exact synthetic source spans; internal guarded effects only",
        "request_id": str(uuid4()),
    }
    started = world.client.post(path, json=command)
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    before = financial(world)
    calls = [(name, r.id) for r in rows for name in ("get_invoice", "get_verified_payments")]
    calls += extra_reads or []
    assert len(calls) <= 8
    wire: list[dict[str, Any]] = []

    async def reply(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        wire.append(data)
        if len(wire) == 1:
            return cast(
                httpx.Response,
                response(
                    response_id="resp_golden_reads",
                    output=[
                        {
                            "type": "function_call",
                            "id": f"fc_{i}",
                            "call_id": f"call_{i}",
                            "name": name,
                            "arguments": json.dumps({"invoice_id": str(key)}),
                            "status": "completed",
                        }
                        for i, (name, key) in enumerate(calls)
                    ],
                ),
            )
        reads = [json.loads(v["output"]) for v in data["input"] if v.get("type") == "function_call_output"]
        assert len(reads) == len(calls)
        result = {
            "summary": "Synthetic source interpretation; no verified financial mutation.",
            "invoice_findings": [
                {
                    "invoice_id": str(r.id),
                    "assessment": "Inspect persisted accounting and source evidence",
                    "evidence_step_ids": [v["step_id"] for v in reads if v["invoice_id"] == str(r.id)],
                }
                for r in rows
            ],
            "proposals": proposals,
            "limitations": ["Scripted HTTP model response, not live model quality."],
        }
        return cast(
            httpx.Response,
            response(
                response_id="resp_golden_result",
                output=[
                    {
                        "type": "message",
                        "id": "msg_golden",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(result), "annotations": []}],
                    }
                ],
            ),
        )

    def factory(run: AgentRun, guard: RunModelGuard) -> OpenAIModelAdapter:
        assert run.model_id is not None
        return OpenAIModelAdapter(
            tenant_id=run.tenant_id,
            connection_id=run.provider_connection_id,
            settings=OpenAISettings(
                api_key=SecretStr("offline-placeholder"),
                model_id=run.model_id,
                model_route=run.model_route,
                features=frozenset({"structured_output", "tool_calling"}),
            ),
            execution_guard=guard,
            transport_factory=lambda: httpx.MockTransport(reply),
        )

    async def work() -> None:
        runtime = WorkerRuntime(
            Settings(database_url=SecretStr(world.url)),
            agent_model_factory=factory,
            agent_budget=BoundedLiveBudget(grant_id, lambda: False),
            voice_runtime_factory=voice_runtime_factory,
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

    world.store.rollback()
    asyncio.run(work())
    read = world.client.get(path + "/" + run_id)
    if read.status_code == 403:
        from aco.features.agent.service import AgentService

        world.store.rollback()
        persisted = world.store.get(AgentRun, UUID(run_id))
        assert persisted is not None
        saved = AgentService(world.store).view(persisted).model_dump(mode="json")
    else:
        assert read.status_code == 200
        saved = cast(dict[str, Any], read.json())
    assert saved["status"] in {"completed", "failed", "awaiting_approval"}, saved
    assert saved["requests"] == len(wire) == len(saved["usage_ids"]) == 2, str(saved)
    steps = [
        {"kind": step.kind, "status": step.status}
        for step in world.store.scalars(select(AgentStep).where(AgentStep.run_id == UUID(run_id)))
    ]
    assert len([s for s in steps if s["kind"] == "read" and s["status"] == "completed"]) == len(calls)
    world.store.rollback()
    assert financial(world) == before
    reservations = list(
        world.store.scalars(
            select(BudgetReservation).where(
                BudgetReservation.provider_connection_id == grant.provider_connection_id
            )
        )
    )
    assert len(reservations) == 2 and all(
        r.retained_amount > 0 and r.status == "UNKNOWN_SIDE_EFFECT" for r in reservations
    )
    for event_id in saved["usage_ids"]:
        event = world.store.get(ProviderUsageEvent, UUID(event_id))
        assert event is not None and event.provider == "openai"
        revision = world.store.scalar(select(UsageRevision).where(UsageRevision.event_id == event.id))
        assert revision is not None and revision.cost_quality == "UNKNOWN" and revision.cost_amount is None
    tenant = world.store.get(Tenant, world.ctx.tenant_id)
    assert tenant is not None and tenant.outbound_paused and not tenant.automation_enabled
    # Exact request replay must not enqueue a second graph/model/business effect.
    replay = world.client.post(path, json=command)
    if read.status_code == 403:
        assert replay.status_code == 403
    else:
        assert replay.json()["id"] == run_id
    result: dict[str, Any] = saved
    return result


def confirm(world: World, key: UUID, message: Communication, *, prior_version: int | None = None) -> None:
    from aco.features.cases.dto import CommunicationEvidence
    from aco.features.promises.dto import ConfirmPromise
    from aco.features.promises.service import PromiseService

    current = PromiseService(world.store).get(world.ctx, key)
    PromiseService(world.store).confirm_promise(
        world.ctx,
        ConfirmPromise(
            promise_id=key,
            expected_version=current.version,
            replacement_expected_version=prior_version,
            confirmation_evidence=CommunicationEvidence.model_validate(evidence(message)),
            reason="Current human independently reviewed exact synthetic source commitment",
        ),
    )
    world.store.commit()


@pytest.mark.parametrize("case", fixtures()[:7] + [fixtures()[8]], ids=lambda v: v.id + "-" + v.story)
def test_sdk_graph_domain_golden(case: Golden) -> None:
    with action_world() as world:
        first = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert first is not None
        peers = list(
            world.store.scalars(
                select(InvoiceSnapshot)
                .where(
                    InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                    InvoiceSnapshot.customer_id == first.customer_id,
                    InvoiceSnapshot.id != first.id,
                )
                .order_by(InvoiceSnapshot.number)
            )
        )
        rows = [first] + peers[:2] if case.id in {"S2", "S3", "S6"} else [first]
        body = case.message.format(first=first.number, second=peers[0].number, third=peers[1].number)
        message = communication(world.store, world.ctx.tenant_id, rows, body)
        reads = [("get_communication_history", first.id)]
        outputs: list[dict[str, object]] = []
        if case.id in {"S1", "S2", "S4"}:
            updates: dict[str, object] = {}
            if case.id == "S4":
                from aco.features.promises.dto import RecordPromise
                from aco.features.promises.service import PromiseService

                old_message = communication(
                    world.store,
                    world.ctx.tenant_id,
                    rows,
                    f"Synthetic customer: I promise AUD 100.00 for {first.number} on 2026-12-01.",
                )
                old = PromiseService(world.store).record_promise(
                    world.ctx, RecordPromise.model_validate(promise(first, old_message))
                )
                world.store.commit()
                confirm(world, old.id, old_message)
                prior = world.store.get(PaymentPromise, old.id)
                assert prior is not None
                prior_version = prior.version
                updates = {
                    "replaces_promise_id": str(prior.id),
                    "due_date": "2026-12-08",
                    "amount": {"minor_units": 7500, "currency": "AUD", "exponent": 2},
                }
                reads.append(("get_payment_promises", first.id))
            outputs.append(
                proposal(first, "record_promise", promise_command=promise(first, message, **updates))
            )
        if case.id == "S2":
            reads.append(("get_communication_history", rows[1].id))
            outputs.append(proposal(rows[1], "upsert_case", case_command=dispute([rows[1]], message)))
        elif case.id in {"S3", "S5"}:
            reads.append(("find_existing_cases", first.id))
            outputs.append(
                proposal(first, "upsert_case", case_command=dispute(rows, message, payment=case.id == "S5"))
            )
        elif case.id == "S6":
            outputs.append(
                proposal(
                    first,
                    "create_task",
                    title="Clarify exact invoices, amount and date before any promise hold",
                )
            )
        elif case.id == "S7":
            # Malicious model output falsely claims execution: independent graph must refuse it.
            outputs.append(
                proposal(first, "create_task", title="Forbidden fabricated success", executed=True)
            )
        elif case.id == "S9":
            from aco.features.cases.service import place_hold

            other = world.store.scalar(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                    InvoiceSnapshot.customer_id != first.customer_id,
                )
            )
            assert other is not None
            unrelated_holds = [
                place_hold(world.store, world.ctx, row.id, "independent_dispute", "Synthetic unrelated issue")
                for row in (first, other)
            ]
            world.store.commit()
            from action_support import enqueue

            from aco.features.review.internal_work import ApprovalProposal

            future = now() + timedelta(days=1)
            retained_check = enqueue(world, due_at=future)
            pending = enqueue(
                world,
                kind="approval_request",
                due_at=future,
                payload=ApprovalProposal(
                    action_id=retained_check.action_id,
                    action_version=retained_check.version,
                    content="Synthetic pending proposal, no dispatch authority",
                    destination="accounts1@example.test",
                    channel="email",
                    invoice_ids=(first.id,),
                    evidence_ids=(world.source_event_id,),
                    policy_version_id=world.policy_version_id,
                    cost_bound="0",
                    currency="AUD",
                    consent_mode="no_capture",
                ).model_dump(mode="json"),
            )
            outputs.append(
                proposal(
                    first,
                    "request_collection_hold",
                    hold_command={
                        "invoice_id": str(first.id),
                        "customer_id": str(first.customer_id),
                        "evidence": evidence(message),
                    },
                )
            )
        if case.id == "S3":
            from aco.features.cases.dto import UpsertCase
            from aco.features.cases.service import CaseService

            CaseService(world.store).upsert_case(world.ctx, UpsertCase.model_validate(dispute(rows, message)))
            world.store.commit()
            second = communication(
                world.store,
                world.ctx.tenant_id,
                list(reversed(rows)),
                body + " Repeated in a distinct thread.",
            )
            assert second.thread_id != message.thread_id
            outputs = [proposal(first, "upsert_case", case_command=dispute(rows, second))]
        saved = run_sdk(world, rows, outputs, extra_reads=reads)
        assert saved["status"] == case.expected.get("run_status", "completed"), saved
        if case.id == "S3":
            assert count(world, CaseEvidence) == case.expected["evidence"]
            assert count(world, Communication) == case.expected["messages"]
        if case.id in {"S1", "S2", "S4"}:
            key = UUID(saved["result"]["proposals"][0]["changed_ids"][0])
            current = world.store.get(PaymentPromise, key)
            assert current is not None and current.status == "NEEDS_CONFIRMATION"
            assert current.source_communication_id == message.id and current.source_text == body
            if case.id == "S4":
                assert prior is not None
                world.store.refresh(prior)
                assert prior.status == case.expected["prior_before_confirmation"]
                confirm(world, key, message, prior_version=prior_version)
                world.store.refresh(prior)
                world.store.refresh(current)
                assert prior.status == case.expected["prior_after_confirmation"]
                assert current.status == case.expected["new_after_confirmation"]
            else:
                assert case.expected["confirmation_required"] is True
                confirm(world, key, message)
                world.store.refresh(current)
                assert current.status == "ACTIVE" and current.confirmed_at is not None
                from aco.features.promises.service import PromiseService

                assert PromiseService(world.store).get(world.ctx, key).scheduling_status == "QUEUED"
        if case.id == "S9":
            customer_ids = set(
                world.store.scalars(
                    select(InvoiceSnapshot.id).where(
                        InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                        InvoiceSnapshot.provider_connection_id == first.provider_connection_id,
                        InvoiceSnapshot.customer_id == first.customer_id,
                    )
                )
            )
            holds = list(
                world.store.scalars(
                    select(CollectionHold).where(
                        CollectionHold.reason == "contact_stop_pending_review",
                        CollectionHold.released_at.is_(None),
                    )
                )
            )
            assert {h.invoice_id for h in holds} == customer_ids
            assert bool(holds) is case.expected["suppressed"]
            from aco.features.actions.models import ScheduledAction
            from aco.features.policies.models import RecipientControl

            pending_row = world.store.get(ScheduledAction, pending.action_id)
            retained_row = world.store.get(ScheduledAction, retained_check.action_id)
            assert (
                pending_row is not None
                and pending_row.status == "cancelled"
                and not pending_row.automatic_resend
            )
            assert retained_row is not None and retained_row.status == "queued"
            assert count(world, RecipientControl) == 0
            assert count(world, CollectionTask) == 1
            for hold_id in unrelated_holds:
                hold = world.store.get(CollectionHold, hold_id)
                assert hold is not None and hold.released_at is None
        if "outbound" in case.expected:
            assert Settings().outbound_enabled is case.expected["outbound"]
        world.store.expire_all()
        for label, model in [
            ("promises", PaymentPromise),
            ("cases", CollectionCase),
            ("tasks", CollectionTask),
            ("holds", CollectionHold),
        ]:
            if label in case.expected:
                assert count(world, model) == case.expected[label]
        if case.id == "S2":
            assert count(world, CommunicationInvoiceLink) == case.expected["linked_invoices"]
            assert count(world, Communication) == 1
            payment_read = world.store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.id == rows[2].id))
            assert payment_read is not None  # financial no-mutation asserted around the actual run.
        if case.id == "S5":
            world.store.refresh(first)
            assert first.balance_minor == case.expected["balance_minor"]
            assert first.financial_status == case.expected["financial_status"]


async def test_golden_signed_simultaneous_duplicate_inbound() -> None:
    from ingress_support import ingress_world

    from aco.features.agent.ingress import produce_inbound_run
    from aco.features.communications.models import EmailIngressReceipt

    case = fixtures()[7]
    with ingress_world() as lane:
        message = lane.message(body=case.message)
        from aco.integrations.demo_email.adapter import DemoEmailAdapter

        lane.world.store.rollback()
        await DemoEmailAdapter(
            lane.service.sessions, lane.world.ctx.tenant_id, lane.email_id
        ).publish_inbound(lane.world.ctx, message)
        raw, headers = lane.notification(message, event_id="simulated-concurrent-golden")
        a, b = await asyncio.gather(
            lane.service.receive(lane.activation_id, raw, headers),
            lane.service.receive(lane.activation_id, raw, headers),
        )
        assert a[0].id == b[0].id
        lane.world.store.rollback()
        await lane.work(produce_inbound_run)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(lane.world.url)))
        try:
            async with runtime.app.open_async():
                await dispatch_agent_runs(runtime.sessions, runtime.app)
                await runtime.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
        finally:
            await runtime.engine.dispose()
        lane.world.store.rollback()
        from aco.features.communications.models import CommunicationSource

        receipt = lane.world.store.get(EmailIngressReceipt, a[0].id)
        assert receipt is not None and receipt.state == "processed" and receipt.agent_run_id is not None
        saved = lane.world.store.get(Communication, receipt.communication_id)
        assert saved is not None and saved.source_state == "stored" and saved.synthetic
        original = lane.world.store.scalar(
            select(CommunicationSource).where(CommunicationSource.communication_id == saved.id)
        )
        assert original is not None and original.sha256 == message.source.integrity_sha256
        run = lane.world.store.get(AgentRun, receipt.agent_run_id)
        assert run is not None and run.source_communication_id == saved.id and run.authenticity == "FAKE"
        assert count(lane.world, CommunicationSource) == 1
        for label, model in [
            ("receipts", EmailIngressReceipt),
            ("messages", Communication),
            ("runs", AgentRun),
            ("tasks", CollectionTask),
        ]:
            assert count(lane.world, model) == case.expected[label]


async def test_golden_postcall_reuses_actual_committed_tool_action() -> None:
    from voice_support import exercise_voice

    case = fixtures()[9]
    actual = await exercise_voice("success")
    assert actual is not None and actual.state == case.expected["state"]
    assert len(actual.changed_ids) == case.expected["tasks"]
    assert actual.transcript_available is case.expected["transcript_available"]
    assert actual.recording_available is case.expected["recording_available"]


@pytest.mark.parametrize(
    "boundary",
    ["forged_customer", "foreign_evidence", "outgoing", "ambiguous", "source_unread", "membership_revoked"],
)
def test_contact_stop_independent_boundaries(boundary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from aco.features.agent.nodes import InvestigationNode
    from aco.features.agent.schemas import GraphState
    from aco.features.identity.models import Membership

    with action_world() as world:
        row = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert row is not None
        message = communication(
            world.store, world.ctx.tenant_id, [row], "Synthetic: Stop contacting me about this invoice."
        )
        command: dict[str, object] = {
            "invoice_id": str(row.id),
            "customer_id": str(row.customer_id),
            "evidence": evidence(message),
        }
        if boundary == "forged_customer":
            other = world.store.scalar(
                select(InvoiceSnapshot).where(InvoiceSnapshot.customer_id != row.customer_id)
            )
            assert other is not None
            command["customer_id"] = str(other.customer_id)
        elif boundary == "foreign_evidence":
            created = world.client.post(
                "/api/workspaces", json={"name": "Other synthetic golden tenant", "timezone": "UTC"}
            )
            assert created.status_code == 201, created.text
            tenant = UUID(created.json()["id"])
            imported = world.client.post(f"/api/workspaces/{tenant}/demo/accounting/seed")
            assert imported.status_code == 201
            foreign = world.store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant))
            assert foreign is not None
            other_message = communication(world.store, tenant, [foreign], "Synthetic: Stop contacting me.")
            command["evidence"] = evidence(other_message)
        elif boundary == "outgoing":
            message.direction = "outgoing"
            world.store.commit()
        elif boundary == "ambiguous":
            world.store.add(
                CommunicationInvoiceLink(
                    tenant_id=world.ctx.tenant_id,
                    communication_id=message.id,
                    invoice_id=row.id,
                    claim_or_context_key="ambiguous-stop",
                    provenance="synthetic_fixture",
                    confidence="ambiguous",
                    evidence_excerpt="",
                    evidence_offset=0,
                    classification="review_required",
                )
            )
            world.store.commit()
        elif boundary == "membership_revoked":
            original = InvestigationNode.execute

            async def revoked(self: InvestigationNode, state: GraphState) -> GraphState:
                async with self.sessions.begin() as db:
                    member = await db.scalar(
                        select(Membership).where(
                            Membership.tenant_id == world.ctx.tenant_id,
                            Membership.user_id == world.ctx.actor_id,
                        )
                    )
                    assert member is not None
                    member.active = False
                    member.version += 1
                return await original(self, state)

            monkeypatch.setattr(InvestigationNode, "execute", revoked)
        saved = run_sdk(
            world,
            [row],
            [proposal(row, "request_collection_hold", hold_command=command)],
            extra_reads=[] if boundary == "source_unread" else [("get_communication_history", row.id)],
        )
        assert saved["status"] == "failed", saved
        assert count(world, CollectionHold) == count(world, CollectionTask) == 0


def test_contact_stop_never_promotes_human_context() -> None:
    from fastapi import HTTPException

    from aco.features.tools.authorization import AgentActionContext
    from aco.features.tools.contact_stop import RequestCollectionHold, request_collection_hold

    with action_world() as world:
        row = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert row is not None
        message = communication(world.store, world.ctx.tenant_id, [row], "Synthetic: Stop contacting me.")
        with pytest.raises(HTTPException) as denied:
            request_collection_hold(
                world.store,
                cast(AgentActionContext, world.ctx),
                RequestCollectionHold.model_validate(
                    {
                        "invoice_id": str(row.id),
                        "customer_id": str(row.customer_id),
                        "evidence": evidence(message),
                    }
                ),
            )
        assert denied.value.status_code == 403
        assert count(world, CollectionHold) == 0


def test_contact_stop_covers_new_canonical_invoice() -> None:
    """A later source import must not silently bypass the customer's pending stop."""
    from copy import deepcopy

    from accounting_support import apply

    from aco.features.accounting.dto import AccountingDataChanged
    from aco.features.accounting.models import DemoSourceRecord
    from aco.features.policies.guards import PolicyGuards

    with action_world() as world:
        first = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert first is not None
        message = communication(world.store, world.ctx.tenant_id, [first], "Please stop contacting me.")
        saved = run_sdk(
            world,
            [first],
            [
                proposal(
                    first,
                    "request_collection_hold",
                    hold_command={
                        "invoice_id": str(first.id),
                        "customer_id": str(first.customer_id),
                        "evidence": evidence(message),
                    },
                )
            ],
            extra_reads=[("get_communication_history", first.id)],
        )
        assert saved["status"] == "completed", saved
        source = world.store.scalar(
            select(DemoSourceRecord)
            .where(
                DemoSourceRecord.provider_connection_id == world.connection_id,
                DemoSourceRecord.kind == "invoice",
                DemoSourceRecord.external_id == "INV-101",
            )
            .order_by(DemoSourceRecord.revision.desc())
        )
        assert source is not None
        payload = deepcopy(source.payload)
        key = "INV-CONTACT-NEW"
        payload.update(external_id=key, number=key)
        new_source = DemoSourceRecord(
            tenant_id=world.ctx.tenant_id,
            provider_connection_id=world.connection_id,
            organisation_id=source.organisation_id,
            kind="invoice",
            external_id=key,
            revision=1,
            payload=payload,
            effective_at=now(),
            observed_at=now(),
        )
        world.store.add(new_source)
        world.store.commit()
        event = AccountingDataChanged(
            provider_connection_id=world.connection_id,
            organisation_id=new_source.organisation_id,
            external_invoice_id=key,
            external_event_id=f"contact-stop-import:{new_source.sequence}",
        )
        world.store.rollback()
        imported = apply(world.client.app, world.ctx, event)
        invoice = world.store.get(InvoiceSnapshot, imported.invoice_id)
        assert invoice is not None and invoice.customer_id == first.customer_id
        assert invoice.balance_minor == 10000 and invoice.verified
        status = PolicyGuards(world.store, now()).operational_status(world.ctx, invoice.id)
        assert "contact_stop_pending_review" in status.reasons, status

        # Current email approval material independently sees the later invoice restriction.
        from dataclasses import replace

        import httpx as voice_httpx
        from action_support import enqueue
        from fastapi import HTTPException

        from aco.features.accounting.models import AccountingEvent, CustomerContact
        from aco.features.identity.models import ProviderConnection
        from aco.features.identity.service import IdentityService, Permission
        from aco.features.review.approvals import HumanApprovalService
        from aco.features.review.internal_work import ApprovalProposal, InternalWorkService
        from aco.features.voice.dto import PrepareVoice
        from aco.features.voice.runtime import VoiceRuntime
        from aco.features.voice.service import VoiceService
        from aco.integrations.vapi.settings import VapiBinding

        event_row = world.store.scalar(
            select(AccountingEvent).where(AccountingEvent.invoice_id == invoice.id)
        )
        assert event_row is not None
        later = replace(world, invoice_id=invoice.id, source_event_id=event_row.id)
        action = enqueue(later)
        contact = world.store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
        )
        assert contact is not None and contact.email
        requested = InternalWorkService(world.store).request_approval(
            world.ctx,
            ApprovalProposal(
                action_id=action.action_id,
                action_version=1,
                content="Synthetic later-invoice review",
                destination=contact.email,
                channel="email",
                invoice_ids=(invoice.id,),
                evidence_ids=(event_row.id,),
                policy_version_id=world.policy_version_id,
                cost_bound="0",
                currency="AUD",
                consent_mode="no_capture",
            ),
        )
        world.store.commit()
        approval = HumanApprovalService(world.store).get(world.ctx, requested.request_id)
        assert "COLLECTION_HELD" in approval.reasons and not approval.can_approve

        voice_connection = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="vapi",
            provider_type="voice",
            status="READY",
            encrypted_credentials_ref="offline-contact-stop",
        )
        world.store.add(voice_connection)
        world.store.commit()
        binding = VapiBinding(
            tenant_id=world.ctx.tenant_id,
            connection_id=voice_connection.id,
            assistant_id=uuid4(),
            api_key=SecretStr("offline-not-a-key"),
            webhook_secret=SecretStr("synthetic-contact-stop"),
            credential_revision="offline-contact-stop",
            evidence_mode="MOCKED_HTTP",
        )
        runtime = VoiceRuntime(
            world.client.app.state.business_sessions,
            lambda tenant, conn: binding,
            capability_secret=SecretStr("offline-capability-contact-stop"),
            transport_factory=lambda: voice_httpx.MockTransport(
                lambda request: pytest.fail("Contact-stop must prevent voice wire")
            ),
            enabled=True,
        )
        with pytest.raises(HTTPException) as refused:
            VoiceService(world.store, runtime).prepare_call(
                world.ctx,
                PrepareVoice(
                    request_id=uuid4(),
                    connection_id=voice_connection.id,
                    invoice_ids=[invoice.id],
                    objective="Synthetic current guard check; do not place a call",
                ),
            )
        assert refused.value.detail == "VOICE_CONTACT_STOP_PENDING_REVIEW"
        world.store.rollback()

        # Only explicit human release of every original source hold clears this scope.
        from aco.features.cases.service import release_hold
        from aco.features.policies.contact_stop import pending_contact_stops
        from aco.features.policies.models import RecipientControl

        unrelated = world.store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                InvoiceSnapshot.customer_id != invoice.customer_id,
            )
        )
        assert unrelated is not None and pending_contact_stops(world.store, unrelated) == []
        restrictions = pending_contact_stops(world.store, invoice)
        assert len(restrictions) == 1
        hold_ids = saved["result"]["proposals"][0]["changed_ids"]
        assert len(hold_ids) > 1
        IdentityService(world.store).authorize(world.ctx, Permission.RESOLVE)
        for index, hold_id in enumerate(hold_ids):
            release_hold(
                world.store, world.ctx, UUID(hold_id), "Current human disposition of exact source stop"
            )
            world.store.flush()
            decision = PolicyGuards(world.store, now()).operational_status(world.ctx, invoice.id)
            assert ("contact_stop_pending_review" in decision.reasons) is (index < len(hold_ids) - 1)
        assert pending_contact_stops(world.store, invoice) == []
        assert count(world, RecipientControl) == 0
