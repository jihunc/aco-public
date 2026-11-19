"""Durable LangGraph flow, separate from business/evidence transactions."""

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from procrastinate import App
from procrastinate.jobs import Job
from psycopg import AsyncConnection
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.features.agent.context import RunAuthority, current_run
from aco.features.agent.events import append_event, finish_step
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.agent.nodes import InvestigationNode, ModelFactory
from aco.features.agent.schemas import GraphState
from aco.features.agent.usage import LiveBudgetGate
from aco.features.identity.models import Tenant, now

if TYPE_CHECKING:
    from aco.features.voice.runtime import VoiceRuntime


async def dispatch_agent_runs(sessions: async_sessionmaker[AsyncSession], app: App) -> int:
    async with sessions.begin() as db:
        runs = list(
            await db.scalars(
                select(AgentRun)
                .where(AgentRun.status == "queued", AgentRun.job_id.is_(None))
                .order_by(AgentRun.created_at)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        )
        raw = await (await db.connection()).get_raw_connection()
        for run in runs:
            job = await app.job_manager.defer_job_async(
                Job(
                    task_name="aco.investigate",
                    queue="aco",
                    lock=None,
                    queueing_lock=None,
                    task_kwargs={"run_id": str(run.id), "expected_version": run.job_version},
                ),
                connection=raw.driver_connection,
            )
            run.job_id = job.id
        return len(runs)


async def prepare_agent_checkpoints(conninfo: str) -> None:
    """Maintained schema bootstrap occurs before acquiring any run authority lease."""
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
        assert isinstance(saver.conn, AsyncConnection)
        await saver.conn.execute("SELECT pg_advisory_lock(hashtext(current_schema() || ':aco-checkpoints'))")
        try:
            await saver.setup()
        finally:
            await saver.conn.execute(
                "SELECT pg_advisory_unlock(hashtext(current_schema() || ':aco-checkpoints'))"
            )


async def execute_agent_run(
    sessions: async_sessionmaker[AsyncSession],
    conninfo: str,
    run_id: UUID,
    expected_version: int,
    job_id: int,
    model_factory: ModelFactory | None = None,
    budget: LiveBudgetGate | None = None,
    voice_runtime: "VoiceRuntime | None" = None,
) -> None:
    async with sessions.begin() as db:
        # Peek only to locate the tenant, then follow the same lock order as human
        # reads/decisions and current_run. Re-read the run under the acquired locks.
        tenant_id = await db.scalar(select(AgentRun.tenant_id).where(AgentRun.id == run_id))
        if tenant_id is None:
            return
        await db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True))
        run = await db.get(AgentRun, run_id, with_for_update=True, populate_existing=True)
        if (
            run is None
            or run.status != "queued"
            or run.job_id != job_id
            or run.job_version != expected_version
        ):
            return
        run.status = "running"
        run.incarnation += 1
        run.lease_token = uuid4()
        run.lease_until = now() + timedelta(seconds=90)
        run.version += 1
        authority = RunAuthority(
            run.tenant_id,
            run.worker_actor_id,
            run.correlation_id,
            run.id,
            job_id,
            run.job_version,
            run.incarnation,
            run.lease_token,
        )
        try:
            await db.run_sync(lambda store: current_run(store, authority))
        except HTTPException:
            run.status, run.error_code = "failed", "RUN_AUTHORITY_REVOKED"
            run.lease_token, run.lease_until = None, None
            await db.run_sync(
                lambda store: append_event(store, run, "failed", "Investigation authority is no longer valid")
            )
            return
        await db.run_sync(
            lambda store: append_event(store, run, "running", "Worker started scoped investigation")
        )
        thread_id = run.thread_id
    try:
        node = InvestigationNode(sessions, authority, model_factory, budget, voice_runtime)
        builder = StateGraph(GraphState)
        builder.add_node("investigate", node)
        builder.add_node("execute", node.execute)
        builder.add_node("complete", node.complete)
        builder.add_node("wait_approval", node.wait_approval)
        builder.add_edge(START, "investigate")
        builder.add_edge("investigate", "execute")
        builder.add_conditional_edges(
            "execute",
            lambda state: (
                "wait_approval"
                if state.get("awaiting_approval") or state.get("awaiting_action")
                else "complete"
            ),
        )
        builder.add_edge("wait_approval", "execute")
        builder.add_edge("complete", END)
        async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
            graph = builder.compile(checkpointer=saver)
            config: RunnableConfig = {"configurable": {"thread_id": thread_id}, "recursion_limit": 12}
            snapshot = await graph.aget_state(config)
            await graph.ainvoke(
                Command(resume=True)
                if snapshot.interrupts
                else None
                if snapshot.values
                else {"run_id": str(run_id)},
                config,
            )
            paused = await graph.aget_state(config)
            if paused.interrupts:
                async with sessions.begin() as db:

                    def mark_waiting(store: Session) -> None:
                        from aco.features.review.agent_approvals import queue_decided_run

                        current = current_run(store, authority)
                        current.status = (
                            "awaiting_approval"
                            if paused.values.get("awaiting_approval")
                            else "awaiting_action"
                        )
                        current.version += 1
                        current.lease_token, current.lease_until = None, None
                        append_event(
                            store,
                            current,
                            current.status,
                            "Graph paused durably for independent approval or canonical action completion",
                        )
                        if current.status == "awaiting_approval":
                            queue_decided_run(store, current)
                        else:
                            from aco.features.tools.email import queue_finished_email_run

                            queue_finished_email_run(store, current)

                    await db.run_sync(mark_waiting)
    except Exception as exc:
        async with sessions.begin() as db:
            current = await db.get(AgentRun, run_id, with_for_update=True)
            if (
                current is not None
                and current.status == "running"
                and current.incarnation == authority.incarnation
                and current.lease_token == authority.lease_token
            ):
                current.status = "failed"
                current.error_code = (
                    str(exc.detail) if isinstance(exc, HTTPException) else "INVESTIGATION_FAILED_NO_REPLAY"
                )
                current.lease_token, current.lease_until = None, None
                current.version += 1
                pending = list(
                    await db.scalars(
                        select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.status == "started")
                    )
                )

                def mark_failed(store: Session, value: AgentStep) -> None:
                    assert current is not None
                    finish_step(store, current, value, error=current.error_code)

                for step in pending:
                    await db.run_sync(mark_failed, step)
                await db.run_sync(
                    lambda store: append_event(
                        store,
                        current,
                        "failed",
                        "Investigation could not complete; no automatic model replay",
                    )
                )


async def recover_agent_runs(sessions: async_sessionmaker[AsyncSession], app: App) -> None:
    for job in await app.job_manager.get_stalled_jobs(
        queue="aco", task_name="aco.investigate", seconds_since_heartbeat=3
    ):
        async with sessions.begin() as db:
            run = await db.scalar(select(AgentRun).where(AgentRun.job_id == job.id).with_for_update())
            if run is None or job.task_kwargs != {"run_id": str(run.id), "expected_version": run.job_version}:
                continue
            current = (
                await db.execute(
                    text("SELECT status::text,worker_id,attempts FROM procrastinate_jobs WHERE id=:id"),
                    {"id": job.id},
                )
            ).first()
            if (
                current is None
                or current.status != "doing"
                or current.worker_id != job.worker_id
                or current.attempts != job.attempts
            ):
                continue
            if run.status in {"queued", "running"}:
                if run.model_state in {"not_started", "complete"} and run.incarnation < 3:
                    # A new job generation prevents delayed old acknowledgement/claim races.
                    run.status, run.job_id = "queued", None
                    run.job_version += 1
                    run.version += 1
                    run.lease_token, run.lease_until = None, None
                else:
                    run.status, run.error_code = "failed", "MODEL_ATTEMPT_UNCERTAIN_NO_REPLAY"
                    run.version += 1
                    run.lease_token, run.lease_until = None, None

                    def mark_uncertain(store: Session, value: AgentRun) -> None:
                        append_event(
                            store,
                            value,
                            "failed",
                            "Worker interrupted after model intent; reconciliation required, no replay",
                        )

                    await db.run_sync(mark_uncertain, run)
        # Maintained library handles acknowledgement of historical no-op job.
        await app.job_manager.retry_job(job)
