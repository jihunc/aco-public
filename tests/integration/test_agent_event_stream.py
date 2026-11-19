"""Real ASGI HTTP stream stays open while an independent maintained worker progresses."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from action_support import action_world, workers
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aco.features.agent.models import AgentRun


class ObserveStream:
    def __init__(self, app: ASGIApp):
        self.app, self.heartbeat = app, threading.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observe(message: Message) -> None:
            if message["type"] == "http.response.body" and b": heartbeat" in message.get("body", b""):
                self.heartbeat.set()
            await send(message)

        await self.app(scope, receive, observe)


def test_event_stream_releases_transactions_and_worker_completes_while_stream_open() -> None:
    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        response = world.client.post(
            path,
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Inspect source evidence",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202
        run_id = UUID(response.json()["id"])
        observed = ObserveStream(world.client.app)
        client = TestClient(
            observed,
            base_url="http://127.0.0.1:53000",
            cookies=world.client.cookies,
            headers=dict(world.client.headers),
        )
        world.store.rollback()
        with ThreadPoolExecutor(max_workers=1) as pool:
            stream = pool.submit(client.get, f"{path}/{run_id}/events")
            assert observed.heartbeat.wait(5), "HTTP stream did not emit its first heartbeat"
            assert not stream.done()
            with workers(world, crash="domain"):
                deadline = time.monotonic() + 30
                while True:
                    world.store.rollback()
                    run = world.store.get(AgentRun, run_id, populate_existing=True)
                    assert run is not None
                    if run.status not in {"queued", "running"}:
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
            world.store.rollback()
            result = stream.result(timeout=10)
        assert run.status == "completed", run.error_code
        assert result.status_code == 200
        assert "event: completed" in result.text and "event: step.completed" in result.text
