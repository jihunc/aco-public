"""Exact event-only fixture, production event transitions/API/replay observer/rendering."""

import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

from action_support import action_world
from boundary_evidence import BoundaryObserver
from sqlalchemy import func, select

from aco.features.agent.events import finish_step, start_step
from aco.features.agent.models import AgentInvoice, AgentRun, AgentStep
from aco.features.usage.models import UsageAttempt


def test_boundary_step_event_cursors(boundary_observer: BoundaryObserver) -> None:
    case = next(
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["domain"] == "sse"
    )
    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        queued = world.client.post(
            path,
            json=dict(
                invoice_ids=[str(world.invoice_id)],
                objective="Establish genuine current identity bindings",
                request_id=str(uuid4()),
            ),
        )
        assert queued.status_code == 202
        template = world.store.get(AgentRun, UUID(queued.json()["id"]))
        assert template
        # Event-protocol fixture only. No fake graph, provider read or business success.
        key = uuid4()
        run = AgentRun(
            id=key,
            request_id=uuid4(),
            thread_id=f"{world.ctx.tenant_id}:{key}",
            objective="Explicit synthetic event replay fixture; no tool or model execution",
            status="completed",
            **{
                name: getattr(template, name)
                for name in [
                    "tenant_id",
                    "initiator_id",
                    "membership_version",
                    "worker_actor_id",
                    "actor_version",
                    "tenant_generation",
                    "provider_connection_id",
                    "connection_version",
                    "correlation_id",
                ]
            },
        )
        world.store.add(run)
        world.store.flush()
        world.store.add(AgentInvoice(tenant_id=run.tenant_id, run_id=run.id, invoice_id=world.invoice_id))
        step = start_step(world.store, run, kind="read", tool_name="get_invoice", invoice_id=world.invoice_id)
        finish_step(world.store, run, step, output={"fixture": "event-only transition; no tool invocation"})
        world.store.commit()
        before_steps = world.store.scalar(select(func.count()).select_from(AgentStep))
        before_usage = world.store.scalar(select(func.count()).select_from(UsageAttempt))
        world.store.rollback()
        headers = {"Last-Event-ID": f"{key}:{case['input']['last_event_id']}"}
        first = world.client.get(f"{path}/{key}/events", headers=headers)
        second = world.client.get(f"{path}/{key}/events", headers=headers)
        assert first.status_code == second.status_code == 200 and first.text == second.text
        payloads = [json.loads(line[6:]) for line in first.text.splitlines() if line.startswith("data: ")]
        assert all(event["step_id"] == str(step.id) for event in payloads)
        current = world.client.get(f"{path}/{key}").json()
        steps = world.client.get(f"{path}/{key}/steps").json()["items"]
        node = shutil.which("node")
        assert node, "Actual frontend consumer requires the existing Node runtime"
        rendered = subprocess.run(
            [node, "web/tests/agent-boundary-render.mjs"],
            input=json.dumps(
                dict(run=current, steps=steps, events=payloads, cursor=case["input"]["last_event_id"])
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        actual = json.loads(rendered.stdout)
        world.store.rollback()
        actual["tool_reexecution_count"] = int(
            world.store.scalar(select(func.count()).select_from(AgentStep)) or 0
        ) - int(before_steps or 0)
        assert world.store.scalar(select(func.count()).select_from(UsageAttempt)) == before_usage
        boundary_observer.assert_observed(case["id"], actual)
