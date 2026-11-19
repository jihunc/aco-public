"""Exact graph-owned internal action grant; never a role or generic service permission."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aco.features.actions.models import ScheduledAction
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.models import AgentStep


@dataclass(frozen=True)
class AgentActionContext:
    authority: RunAuthority
    action_id: UUID
    step_id: UUID

    @property
    def tenant_id(self) -> UUID:
        return self.authority.tenant_id

    @property
    def actor_id(self) -> UUID:
        return self.authority.actor_id

    @property
    def correlation_id(self) -> UUID:
        return self.authority.correlation_id


def require_agent_action(
    db: Session, ctx: AgentActionContext, kind: str, command: BaseModel
) -> ScheduledAction:
    run = current_run(db, ctx.authority)
    action = db.get(ScheduledAction, ctx.action_id, populate_existing=True)
    step = db.get(AgentStep, ctx.step_id, populate_existing=True)
    if (
        action is None
        or step is None
        or action.tenant_id != run.tenant_id
        or action.kind != "agent_internal"
        or action.agent_run_id != run.id
        or action.agent_step_id != step.id
        or step.run_id != run.id
        or action.worker_actor_id != ctx.actor_id
        or action.initiator_id != run.initiator_id
        or action.status != "executing"
        or action.job_id != run.job_id
        or action.lease_token != run.lease_token
        or action.invoice_id not in selected_invoices(db, run)
        or action.command != {"kind": kind, "payload": command.model_dump(mode="json")}
        or step.status != "started"
        or step.kind != "action"
    ):
        raise HTTPException(403, "AGENT_ACTION_SCOPE_DENIED")
    return action
