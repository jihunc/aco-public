"""Actual maintained claim and current-human read must share Tenant -> Run lock order."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

from action_support import action_world
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from aco.core.settings import Settings
from aco.features.agent.graph import dispatch_agent_runs, prepare_agent_checkpoints
from aco.features.agent.models import AgentRun
from aco.features.agent.service import AgentService
from aco.features.identity.service import IdentityService, Permission
from aco.worker.app import WorkerRuntime


async def test_maintained_claim_and_current_human_read_do_not_invert_locks() -> None:
    with action_world() as world:
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Inspect source evidence",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["id"])
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        first_lock = asyncio.Event()
        claim_pid: list[int] = []

        def entering_lock(
            connection: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool
        ) -> None:
            if (
                not claim_pid
                and "FOR" in statement
                and "UPDATE" in statement
                and ("agent_runs" in statement or "tenants" in statement)
            ):
                claim_pid.append(connection.connection.driver_connection.info.backend_pid)
                first_lock.set()

        try:
            async with runtime.app.open_async():
                world.store.rollback()
                await prepare_agent_checkpoints(
                    make_url(world.url).set(drivername="postgresql").render_as_string(hide_password=False)
                )
                await dispatch_agent_runs(runtime.sessions, runtime.app)
                world.store.rollback()
                # A genuine current-human request holds its canonical tenant authorization lock.
                IdentityService(world.store).authorize(world.ctx, Permission.INVOICE_READ)
                event.listen(runtime.engine.sync_engine, "before_cursor_execute", entering_lock)
                worker = asyncio.create_task(
                    runtime.app.run_worker_async(
                        queues=["aco"], concurrency=1, wait=False, listen_notify=False
                    )
                )
                read_error: DBAPIError | None = None
                try:
                    await asyncio.wait_for(first_lock.wait(), 15)
                    async with runtime.engine.connect() as observer:
                        observer = await observer.execution_options(isolation_level="AUTOCOMMIT")
                        for _ in range(100):
                            waiting = await observer.scalar(
                                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"),
                                {"pid": claim_pid[0]},
                            )
                            if waiting:
                                break
                            await asyncio.sleep(0.02)
                        else:
                            raise AssertionError("Actual claim did not reach the held tenant lock")
                    world.store.execute(text("SET LOCAL lock_timeout = '2s'"))
                    try:
                        assert AgentService(world.store).locked(world.ctx, run_id).id == run_id
                    except DBAPIError as failure:
                        read_error = failure
                finally:
                    world.store.rollback()
                    await asyncio.wait_for(worker, 30)
                assert read_error is None, "Run-first claim blocked the tenant-authorized human reader"
        finally:
            event.remove(runtime.engine.sync_engine, "before_cursor_execute", entering_lock)
            await runtime.engine.dispose()
        world.store.rollback()
        run = world.store.get(AgentRun, run_id)
        assert run is not None and run.status == "completed", run.error_code if run else "missing"
