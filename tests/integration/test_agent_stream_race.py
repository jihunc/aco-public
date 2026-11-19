"""Actual nonterminal fetch/subscribe/reconnect gap, with one maintained execution."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from action_support import action_world
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aco.core.settings import Settings
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ActionExecution
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.models import AgentUsage
from aco.features.review.models import CollectionTask
from aco.integrations.fake.investigation import InvestigationFake
from aco.integrations.ports.llm import LLMInput, LLMRequestPolicy, ReadTool, TypedResult
from aco.worker.app import WorkerRuntime


class DisconnectAfterHeartbeat:
    """Close the genuine ASGI client after one poll, without changing server events."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self.bodies: list[bytes] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        disconnected = anyio.Event()
        first = True

        async def incoming() -> Message:
            nonlocal first
            if first:
                first = False
                return await receive()
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def outgoing(message: Message) -> None:
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                self.bodies.append(body)
                await send(message)
                if b": heartbeat" in body:
                    disconnected.set()
            else:
                await send(message)

        await self.app(scope, incoming, outgoing)


def events(chunks: list[bytes]) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in b"".join(chunks).decode().splitlines()
        if line.startswith("data: ")
    ]


def test_nonterminal_cursor_closes_fetch_subscribe_gap_without_reexecution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    actual = InvestigationFake.run_typed
    calls = 0

    async def paused[OutputT: BaseModel](
        self: InvestigationFake,
        model_route: str,
        input_context: LLMInput,
        tools: tuple[ReadTool[Any, Any], ...],
        output_schema: type[OutputT],
        request_policy: LLMRequestPolicy,
    ) -> TypedResult[OutputT]:
        nonlocal calls
        calls += 1
        entered.set()
        assert await asyncio.to_thread(release.wait, 20), "Test did not release actual model execution"
        return await actual(self, model_route, input_context, tools, output_schema, request_policy)

    monkeypatch.setattr(InvestigationFake, "run_typed", paused)
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        financial = (invoice.source_revision, invoice.balance_minor, invoice.financial_status)
        world.store.rollback()
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        started = world.client.post(
            path,
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Create an internal follow-up task from verified invoice evidence",
                "request_id": str(uuid4()),
            },
        )
        assert started.status_code == 202, started.text
        run_id = UUID(started.json()["id"])
        detail = f"{path}/{run_id}"
        initial = world.client.get(detail + "/steps").json()
        watermark = initial["last_event_sequence"]
        assert initial["items"] == []

        async def work() -> None:
            runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
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

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(asyncio.run, work())
            try:
                assert entered.wait(15), "Maintained worker did not reach actual model boundary"
                assert world.client.get(detail).json()["status"] == "running"
                # The initial GET predates these committed worker events; no stream was open yet.
                persisted_gap = world.client.get(detail + f"/event-log?after={watermark}").json()["items"]
                assert persisted_gap and all(item["event_sequence"] > watermark for item in persisted_gap)
                first = DisconnectAfterHeartbeat(world.client.app)
                client = TestClient(first, base_url="http://127.0.0.1:53000", cookies=world.client.cookies)
                assert client.get(
                    detail + "/events", headers={"Last-Event-ID": f"{run_id}:{watermark}"}
                ).status_code == 200
                replayed = events(first.bodies)
                assert replayed == persisted_gap
                cursor = replayed[-1]["id"]
                # Reconnect while the same genuine job is still running: no replay duplicates.
                second = DisconnectAfterHeartbeat(world.client.app)
                client = TestClient(second, base_url="http://127.0.0.1:53000", cookies=world.client.cookies)
                assert client.get(detail + "/events", headers={"Last-Event-ID": cursor}).status_code == 200
                assert events(second.bodies) == []
                assert world.client.get(detail).json()["status"] == "running"
            finally:
                release.set()
            worker.result(timeout=25)
        completed = world.client.get(detail).json()
        assert completed["status"] == "completed" and completed["execution"] == "EXECUTED", completed
        tail = world.client.get(detail + "/events", headers={"Last-Event-ID": cursor})
        assert tail.status_code == 200
        remaining = events([tail.content])
        assert remaining and remaining[-1]["kind"] == "completed"
        log = world.client.get(detail + f"/event-log?after={watermark}").json()["items"]
        assert replayed + remaining == log
        assert len({item["id"] for item in log}) == len(log)
        assert world.client.get(detail + "/events", headers={"Last-Event-ID": log[-1]["id"]}).text == ""
        assert calls == 1
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(AgentUsage)) == 1
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert world.store.scalar(select(func.count()).select_from(ActionExecution)) == 1
        current = world.store.get(InvoiceSnapshot, world.invoice_id, populate_existing=True)
        assert current is not None
        assert (current.source_revision, current.balance_minor, current.financial_status) == financial
