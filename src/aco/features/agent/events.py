"""One locked run orders stable steps and append-only transitions in the same transaction."""

from uuid import UUID

from sqlalchemy.orm import Session

from aco.features.agent.models import AgentEvent, AgentRun, AgentStep


def append_event(
    db: Session, run: AgentRun, kind: str, description: str, step_id: UUID | None = None
) -> AgentEvent:
    run.last_event_sequence += 1
    event = AgentEvent(
        tenant_id=run.tenant_id,
        run_id=run.id,
        event_sequence=run.last_event_sequence,
        kind=kind,
        description=description,
        step_id=step_id,
    )
    db.add(event)
    db.flush()
    return event


def start_step(
    db: Session,
    run: AgentRun,
    *,
    kind: str,
    tool_name: str | None = None,
    invoice_id: UUID | None = None,
    arguments: dict[str, object] | None = None,
) -> AgentStep:
    run.last_step_sequence += 1
    step = AgentStep(
        tenant_id=run.tenant_id,
        run_id=run.id,
        step_sequence=run.last_step_sequence,
        kind=kind,
        tool_name=tool_name,
        invoice_id=invoice_id,
        status="started",
        description="Reading selected invoice evidence" if kind == "read" else "Analysing scoped evidence",
        input=arguments or {},
    )
    db.add(step)
    db.flush()
    append_event(db, run, "step.started", step.description, step.id)
    return step


def finish_step(
    db: Session,
    run: AgentRun,
    step: AgentStep,
    *,
    output: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    step.status = "failed" if error else "completed"
    step.error_code = error
    step.output = output
    step.description = (
        "Evidence read unavailable"
        if error
        else (
            "Source-stamped evidence read"
            if step.kind == "read"
            else "Internal business action committed"
            if step.kind == "action"
            else "Analysis complete; proposed actions not executed"
        )
    )
    append_event(db, run, "step." + step.status, step.description, step.id)
