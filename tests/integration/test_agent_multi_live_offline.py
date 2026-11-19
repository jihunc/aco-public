"""Two-invoice actual SDK/graph/worker with mocked provider wire and real durable state."""

import asyncio
import json
import sys
from datetime import timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from action_support import action_world
from fastapi import FastAPI
from openai_support import response
from provider_smoke_support import private_config
from pydantic import SecretStr
from sqlalchemy import select
from starlette.testclient import TestClient

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.settings import Settings
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.live import (
    AUTHORIZATION,
    BoundedLiveBudget,
    LiveGrantConfig,
    install_bounded_live_grant,
)
from aco.features.agent.models import AgentRun
from aco.features.agent.usage import RunModelGuard
from aco.features.identity.models import now
from aco.features.review.models import CollectionTask
from aco.features.usage.models import BudgetReservation
from aco.integrations.openai.adapter import OpenAIModelAdapter
from aco.integrations.openai.settings import OpenAISettings
from aco.integrations.openai.transport import OpenAITransport
from aco.worker.app import WorkerRuntime


@pytest.mark.parametrize(
    ("authorization", "entrypoint"),
    [(DELIVERY_LIVE_AUTHORIZATION, False), (DELIVERY_LIVE_AUTHORIZATION, True)],
)
def test_two_invoice_actual_sdk_worker_executes_one_guarded_task_within_two_requests(
    authorization: str,
    entrypoint: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_invoice_sdk_scenario(authorization, entrypoint, tmp_path, monkeypatch)


def test_two_invoice_legacy_input_cap_blocks_second_wire_without_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_invoice_sdk_scenario(AUTHORIZATION, False, tmp_path, monkeypatch)


def _two_invoice_sdk_scenario(
    authorization: str,
    entrypoint: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = authorization == AUTHORIZATION
    attempted_bounds: list[int] = []
    original = OpenAITransport.handle_async_request

    async def observe_bound(self: OpenAITransport, request: httpx.Request) -> httpx.Response:
        attempted_bounds.append(len(request.content) + 128)
        return await original(self, request)

    monkeypatch.setattr(OpenAITransport, "handle_async_request", observe_bound)
    with action_world() as world:
        invoices = list(
            world.store.scalars(
                select(InvoiceSnapshot)
                .where(
                    InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                    InvoiceSnapshot.number.in_(["INV-101", "INV-102"]),
                )
                .order_by(InvoiceSnapshot.number)
            )
        )
        assert len(invoices) == 2
        ids = [r.id for r in invoices]
        financial = [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in invoices]
        task_query = (
            select(CollectionTask.__table__)
            .where(CollectionTask.tenant_id == world.ctx.tenant_id)
            .order_by(CollectionTask.id)
        )
        original_tasks = list(world.store.execute(task_query).mappings())
        grant = install_bounded_live_grant(
            world.store,
            tenant_id=world.ctx.tenant_id,
            initiator_id=world.ctx.actor_id,
            authorization_ref=authorization,
            config=LiveGrantConfig(
                model_id="gpt-6.1-sol",
                expires_at=now() + timedelta(minutes=10),
                price_source_at=now(),
                input_usd_per_token=Decimal(".000005"),
                output_usd_per_token=Decimal(".000010"),
                max_requests=3,
                max_input_tokens=32000 if legacy else 64000,
                max_output_tokens=2048,
                amount=Decimal("1"),
            ),
        )
        world.store.commit()
        from aco import cli
        from aco.worker import __main__ as worker_main

        settings = world.client.app.state.settings
        if entrypoint:
            from aco.integrations.provider_smoke.campaign import CampaignAllocation, LiveCampaign

            private = private_config(tmp_path)
            private.write_text(private.read_text().replace("=2048", "=64000").replace("=512", "=2048"))
            for name in ("ACO_EMAIL_LIVE_CONFIG_PATH", "ACO_LIVE_CAMPAIGN_FILE", "ACO_LIVE_ALLOCATION_ID"):
                monkeypatch.delenv(name, raising=False)
            for name, value in {
                "ACO_AGENT_LIVE_CONFIG_PATH": str(private),
                "ACO_AGENT_LIVE_GRANT_ID": str(grant.id),
                "ACO_LIVE_AUTHORIZATION_REF": authorization,
            }.items():
                monkeypatch.setenv(name, value)
            if authorization == DELIVERY_LIVE_AUTHORIZATION:
                ledger = tmp_path / "campaign.sqlite3"
                source = "a" * 40
                monkeypatch.setattr(
                    "aco.integrations.provider_smoke.campaign.canonical_campaign_path", lambda: ledger
                )
                monkeypatch.setattr(
                    "aco.integrations.openai.application.current_source_commit", lambda: source
                )
                campaign = LiveCampaign.create(ledger, authorization_ref=authorization)
                allocation = CampaignAllocation(
                    id=uuid4(),
                    grant_id=grant.id,
                    provider="openai",
                    source_commit=source,
                    max_usd=Decimal("1"),
                    max_requests=3,
                )
                campaign.allocate(allocation)
                monkeypatch.setenv("ACO_LIVE_CAMPAIGN_FILE", str(ledger))
                monkeypatch.setenv("ACO_LIVE_ALLOCATION_ID", str(allocation.id))
            applications: list[FastAPI] = []

            def serve(app: FastAPI, **kwargs: object) -> None:
                applications.append(app)

            monkeypatch.setattr(cli, "runtime_settings", lambda: settings)
            monkeypatch.setattr(worker_main, "runtime_settings", lambda: settings)
            monkeypatch.setattr("uvicorn.run", serve)
            monkeypatch.setattr(sys, "argv", ["aco", "api"])
            cli.main()
            assert len(applications) == 1
            app = applications[0]
            assert app.state.agent_live_grant_id == grant.id
            app.state.identity.oauth_transport = world.client.app.state.identity.oauth_transport
            world.client = TestClient(
                app,
                base_url="http://127.0.0.1:53000",
                cookies=world.client.cookies,
                headers=dict(world.client.headers),
            )
        else:
            world.client.app.state.agent_live_grant_id = grant.id
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        started = world.client.post(
            path,
            json={
                "invoice_ids": list(map(str, ids)),
                "objective": "Review both invoices and create one internal task only",
                "request_id": str(uuid4()),
            },
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["id"]
        wire: list[dict[str, Any]] = []

        async def reply(request: httpx.Request) -> httpx.Response:
            data = json.loads(request.content)
            wire.append(data)
            assert data["parallel_tool_calls"] is True
            if len(wire) == 1:
                return cast(
                    httpx.Response,
                    response(
                        response_id="resp_multi_reads",
                        output=[
                            {
                                "type": "function_call",
                                "id": f"fc_{i}_{kind}",
                                "call_id": f"call_{i}_{kind}",
                                "name": name,
                                "arguments": json.dumps({"invoice_id": str(invoice)}),
                                "status": "completed",
                            }
                            for i, invoice in enumerate(ids)
                            for kind, name in enumerate(("get_invoice", "get_verified_payments"))
                        ],
                    ),
                )
            reads = [
                json.loads(v["output"]) for v in data["input"] if v.get("type") == "function_call_output"
            ]
            assert len(reads) == 4
            result = {
                "summary": "Both source-backed invoices require internal evidence review.",
                "invoice_findings": [
                    {
                        "invoice_id": str(invoice),
                        "assessment": "Review actual verified source balance.",
                        "evidence_step_ids": [
                            v["step_id"] for v in reads if str(v["invoice_id"]) == str(invoice)
                        ],
                    }
                    for invoice in ids
                ],
                "proposals": [
                    {
                        "invoice_id": str(ids[0]),
                        "kind": "create_task",
                        "title": "Review both invoice records",
                        "reason": "Internal remittance review only",
                        "executed": False,
                    }
                ],
                "limitations": ["Synthetic accounting source."],
            }
            return cast(
                httpx.Response,
                response(
                    response_id="resp_multi_result",
                    output=[
                        {
                            "type": "message",
                            "id": "msg_multi",
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
                    api_key=SecretStr("offline-placeholder"),
                    model_id=run.model_id,
                    model_route=run.model_route,
                    features=frozenset({"structured_output", "tool_calling"}),
                ),
                execution_guard=guard,
                transport_factory=lambda: httpx.MockTransport(reply),
            )

        async def run_worker(runtime: WorkerRuntime) -> None:
            try:
                async with runtime.app.open_async():
                    await dispatch_agent_runs(runtime.sessions, runtime.app)
                    await runtime.app.run_worker_async(
                        queues=["aco"], concurrency=1, wait=False, listen_notify=False
                    )
            finally:
                await runtime.engine.dispose()

        async def work() -> None:
            runtime = WorkerRuntime(
                Settings(database_url=SecretStr(world.url)),
                agent_model_factory=factory,
                agent_budget=BoundedLiveBudget(grant.id, lambda: False),
            )
            await run_worker(runtime)

        world.store.rollback()
        if entrypoint:
            monkeypatch.setattr(
                "aco.integrations.openai.investigation.OpenAIModelAdapter",
                partial(OpenAIModelAdapter, transport_factory=lambda: httpx.MockTransport(reply)),
            )
            monkeypatch.setattr(WorkerRuntime, "run", run_worker)
            worker_main.main()
        else:
            asyncio.run(work())
        run = world.client.get(path + "/" + run_id).json()
        assert all(bound <= 32000 for bound in attempted_bounds)
        if legacy:
            # Current schema exceeds the consumed historical grant; preserve its refusal.
            assert len(attempted_bounds) == 2 and sum(attempted_bounds) > grant.max_input_tokens == 32000
            assert len(wire) == run["requests"] == len(run["usage_ids"]) == 1
            assert run["status"] == "failed" and run["error_code"] == "MODEL_OUTPUT_UNAVAILABLE"
            assert run["execution"] in {"UNAVAILABLE", "PROPOSED"}
            assert list(world.store.execute(task_query).mappings()) == original_tasks
            assert (
                world.store.scalar(
                    select(ScheduledAction).where(ScheduledAction.agent_run_id == UUID(run_id))
                )
                is None
            )
        else:
            assert sum(attempted_bounds) <= grant.max_input_tokens == 64000
            assert run["status"] == "completed", (
                run,
                world.client.get(path + "/" + run_id + "/steps").json(),
            )
            assert run["execution"] == "EXECUTED" and run["authenticity"] == "LIVE"
            assert run["requests"] == len(wire) == len(run["usage_ids"]) == 2
            assert len(run["invoice_ids"]) == len(run["result"]["invoice_findings"]) == 2
            task_id = run["result"]["proposals"][0]["changed_ids"][0]
            assert (
                world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/tasks/{task_id}").status_code == 200
            )
            steps = world.client.get(path + "/" + run_id + "/steps").json()["items"]
            assert len([s for s in steps if s["kind"] == "read" and s["status"] == "completed"]) == 4
        world.store.rollback()
        current = list(
            world.store.scalars(
                select(InvoiceSnapshot).where(InvoiceSnapshot.id.in_(ids)).order_by(InvoiceSnapshot.number)
            )
        )
        assert [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in current] == financial
        reservations = list(
            world.store.scalars(
                select(BudgetReservation).where(
                    BudgetReservation.provider_connection_id == grant.provider_connection_id
                )
            )
        )
        assert len(reservations) == (1 if legacy else 2) and all(
            r.status == "UNKNOWN_SIDE_EFFECT" and r.retained_amount > 0 for r in reservations
        )
        print(
            {
                "authorization": "legacy32k" if legacy else "current64k",
                "attempted_bounds": attempted_bounds,
                "wire_requests": len(wire),
                "retained_reservations": len(reservations),
                "external_calls": 0,
                "transport": "SYNTHETIC_HTTP",
            }
        )
