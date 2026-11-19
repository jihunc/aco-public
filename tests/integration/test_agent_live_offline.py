"""Real worker/graph/OpenAI adapter with synthetic HTTP: no live provider acceptance."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from action_support import action_world
from openai_support import response
from pydantic import SecretStr
from sqlalchemy import select, text

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.settings import Settings
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.live import (
    BoundedLiveBudget,
    LiveGrantConfig,
    install_bounded_live_grant,
)
from aco.features.agent.models import AgentRun
from aco.features.agent.usage import RunModelGuard
from aco.features.identity.models import now
from aco.features.usage.models import BudgetReservation, ProviderUsageEvent
from aco.integrations.openai.adapter import OpenAIModelAdapter
from aco.integrations.openai.settings import OpenAISettings
from aco.worker.app import WorkerRuntime


@pytest.mark.parametrize(
    "denial",
    [
        "none",
        "kill_first",
        "cap",
        "kill_after_first",
        "wrong_invoice",
        "source_unread",
        "command_scope",
        "implicit_case",
        "three_turn",
        "request_exhausted",
    ],
)
@pytest.mark.parametrize("business", ["task", "case", "promise"])
def test_real_adapter_graph_task_and_budget_are_durable_offline(denial: str, business: str) -> None:
    if denial in {"three_turn", "request_exhausted"} and business != "task":
        pytest.skip("Three-turn budget proof uses the task workflow")
    if denial == "implicit_case" and business != "case":
        pytest.skip("Implicit claim identity is specific to Case")
    if (business != "task" and denial not in {"none", "source_unread", "command_scope", "implicit_case"}) or (
        business == "task" and denial in {"source_unread", "command_scope"}
    ):
        pytest.skip("Denial matrix uses the shared task gateway")
    with action_world() as world:
        from case_promise_support import communication

        from aco.features.accounting.models import InvoiceSnapshot

        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        body = "The purchase order is wrong. I promise to pay AUD 100.00 on 2026-12-01."
        msg = communication(world.store, world.ctx.tenant_id, [invoice], body)
        if denial == "implicit_case":
            from aco.features.cases.dto import CommunicationEvidence, UpsertCase
            from aco.features.cases.service import CaseService

            CaseService(world.store).upsert_case(
                world.ctx,
                UpsertCase(
                    customer_id=invoice.customer_id,
                    invoice_ids=[invoice.id],
                    issue_type="wrong_po",
                    claim_key="incorrect purchase order",
                    substance=body,
                    evidence=CommunicationEvidence(communication_id=msg.id, excerpt=body, offset=0),
                ),
            )
            world.store.commit()
        grant = install_bounded_live_grant(
            world.store,
            tenant_id=world.ctx.tenant_id,
            initiator_id=world.ctx.actor_id,
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            config=LiveGrantConfig(
                model_id="gpt-6.1-sol",
                expires_at=now() + timedelta(minutes=10),
                price_source_at=now(),
                input_usd_per_token=Decimal(".000002"),
                output_usd_per_token=Decimal(".000008"),
                max_requests=3,
                max_input_tokens=2048 if denial == "cap" else 64000,
                max_output_tokens=2048 if denial in {"three_turn", "request_exhausted"} else 512,
                amount=Decimal("1"),
            ),
        )
        world.store.commit()
        world.client.app.state.agent_live_grant_id = grant.id
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        r = world.client.post(
            path,
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Create an internal follow-up task from verified invoice evidence",
                "request_id": str(uuid4()),
            },
        )
        assert r.status_code == 202, r.text
        run_id = r.json()["id"]
        wire: list[int] = []
        output_caps: list[int] = []

        async def reply(request: httpx.Request) -> httpx.Response:
            wire.append(len(request.content) + 128)
            data = json.loads(request.content)
            output_caps.append(data["max_output_tokens"])
            assert sum(output_caps) <= grant.max_output_tokens
            assert wire[-1] <= 32000  # The independent per-request guard is unchanged.
            assert sum(wire) <= grant.max_input_tokens
            assert len(wire) <= grant.max_requests
            staged = denial in {"three_turn", "request_exhausted"}
            if len(wire) == 1 or (staged and (len(wire) == 2 or denial == "request_exhausted")):
                return cast(
                    httpx.Response,
                    response(
                        response_id=f"resp_agent_{len(wire)}",
                        output=[
                            {
                                "type": "function_call",
                                "id": f"fc_{len(wire)}_" + str(i),
                                "call_id": f"call_{len(wire)}_" + str(i),
                                "name": name,
                                "arguments": json.dumps(
                                    {
                                        "invoice_id": str(
                                            uuid4() if denial == "wrong_invoice" else world.invoice_id
                                        )
                                    }
                                ),
                                "status": "completed",
                            }
                            for i, name in enumerate(
                                (("get_invoice",) if len(wire) == 1 else ("get_verified_payments",))
                                if staged
                                else ("get_invoice", "get_verified_payments")
                                if denial == "source_unread"
                                else ("get_invoice", "get_verified_payments", "get_communication_history")
                            )
                        ],
                    ),
                )
            reads = [
                json.loads(i["output"]) for i in data["input"] if i.get("type") == "function_call_output"
            ]
            result: dict[str, Any] = {
                "summary": "Source evidence supports internal review.",
                "invoice_findings": [
                    {
                        "invoice_id": str(world.invoice_id),
                        "assessment": "Imported snapshot is last-known, verification needed.",
                        "evidence_step_ids": [v["step_id"] for v in reads],
                    }
                ],
                "proposals": [
                    {
                        "invoice_id": str(world.invoice_id),
                        "kind": "create_task",
                        "title": "Review invoice evidence",
                        "reason": "Verify accounting evidence before outreach",
                        "executed": False,
                        "action_id": None,
                        "changed_ids": [],
                    }
                ],
                "limitations": ["Last-known snapshots only."],
            }
            if business != "task":
                evidence = {"communication_id": str(msg.id), "excerpt": body, "offset": 0}
                proposal = result["proposals"][0]
                if business == "case":
                    proposal.update(
                        kind="upsert_case",
                        case_command={
                            "customer_id": str(invoice.customer_id),
                            "invoice_ids": [str(invoice.id)],
                            "issue_type": "wrong_po",
                            "claim_key": "incorrect purchase order",
                            "substance": body,
                            "evidence": evidence,
                        },
                    )
                else:
                    proposal.update(
                        kind="record_promise",
                        promise_command={
                            "customer_id": str(invoice.customer_id),
                            "invoice_ids": [str(invoice.id)],
                            "evidence": evidence,
                            "due_date": "2026-12-01",
                            "timezone": "Australia/Melbourne",
                            "amount": {"minor_units": 10000, "currency": "AUD", "exponent": 2},
                        },
                    )
            if denial == "command_scope":
                result["proposals"][0]["case_command" if business == "case" else "promise_command"][
                    "invoice_ids"
                ].append(str(uuid4()))
            print({"business": business, "result_utf8_bytes": len(json.dumps(result).encode())})
            return cast(
                httpx.Response,
                response(
                    response_id="resp_agent_2",
                    output=[
                        {
                            "type": "message",
                            "id": "msg_agent",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": json.dumps(result), "annotations": []}
                            ],
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
                    api_key=SecretStr("synthetic-placeholder"),
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
                agent_budget=BoundedLiveBudget(
                    grant.id, lambda: denial == "kill_first" or (denial == "kill_after_first" and bool(wire))
                ),
            )
            try:
                async with runtime.app.open_async():
                    await dispatch_agent_runs(runtime.sessions, runtime.app)
                    await runtime.app.run_worker_async(
                        queues=["aco"], concurrency=1, wait=False, listen_notify=False
                    )
            finally:
                await runtime.engine.dispose()

        world.store.rollback()
        asyncio.run(work())
        run = world.client.get(path + "/" + run_id).json()
        if denial not in {"none", "three_turn"}:
            assert run["status"] == "failed", run
            assert run["execution"] in {"UNAVAILABLE", "PROPOSED"}
            from aco.features.actions.models import ScheduledAction

            assert (
                world.store.scalar(
                    select(ScheduledAction).where(ScheduledAction.agent_run_id == UUID(run_id))
                )
                is None
            )
            assert len(wire) == (
                0
                if denial in {"kill_first", "cap"}
                else (
                    3
                    if denial == "request_exhausted"
                    else (2 if denial in {"source_unread", "command_scope", "implicit_case"} else 1)
                )
            )
            assert len(run["usage_ids"]) == len(wire)
            return
        assert run["status"] == "completed", str(run) + str(
            world.client.get(path + "/" + run_id + "/steps").json()
        )
        assert run["execution"] == "EXECUTED" and run["authenticity"] == "LIVE"
        count = 3 if denial == "three_turn" else 2
        assert (
            run["provider_attempted"]
            and run["input_tokens"] == 110 * count
            and run["output_tokens"] == 21 * count
        )
        assert len(wire) == count
        assert output_caps == [grant.max_output_tokens // grant.max_requests] * count
        changed = run["result"]["proposals"][0]["changed_ids"]
        assert len(changed) == 1
        if business == "case":
            detail = world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/cases/{changed[0]}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["issue_type"] == "wrong_po"
        elif business == "promise":
            detail = world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/promises/{changed[0]}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["status"] == "NEEDS_CONFIRMATION"
        world.store.rollback()
        reservations = list(
            world.store.scalars(
                select(BudgetReservation).where(
                    BudgetReservation.provider_connection_id == grant.provider_connection_id
                )
            )
        )
        assert len(reservations) == count and all(
            r.retained_amount > 0 and r.status == "UNKNOWN_SIDE_EFFECT" for r in reservations
        )
        assert len(run["usage_ids"]) == count
        events = [world.store.get(ProviderUsageEvent, UUID(i)) for i in run["usage_ids"]]
        assert all(e is not None and e.provider == "openai" for e in events)
        assert (
            world.store.scalar(
                text("SELECT count(*) FROM checkpoints WHERE thread_id=:thread"),
                {"thread": f"{world.ctx.tenant_id}:{run_id}"},
            )
            or 0
        ) >= 3
        print(
            {
                "request_bounds": wire,
                "output_caps": output_caps,
                "full_source_first_request_bound": wire[0],
                "full_source_second_request_bound": wire[1],
                "full_source_cumulative_bound": sum(wire),
                "external_calls": 0,
                "transport": "SYNTHETIC_HTTP",
            }
        )
