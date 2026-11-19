"""Actual retained schema accepts only a genuine graph-bound email trigger."""

from uuid import UUID, uuid4

import pytest
from action_support import action_world
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import OutboxEvent, ScheduledAction
from aco.features.agent.events import start_step
from aco.features.agent.models import AgentRun
from aco.features.communications.models import Communication
from aco.features.identity.models import Tenant, now


def test_email_draft_has_no_client_supplied_dispatch_authority() -> None:
    from aco.features.tools.email_dto import DraftCustomerEmail

    value = {
        "invoice_id": uuid4(),
        "subject": "Invoice evidence",
        "content": "Please review the attached invoice reference.",
    }
    assert DraftCustomerEmail.model_validate(value).content == value["content"]
    for field, supplied in {
        "destination": "other@example.test",
        "approved": True,
        "policy_version_id": uuid4(),
        "cost_bound": "0",
        "capture": True,
    }.items():
        with pytest.raises(ValidationError):
            DraftCustomerEmail.model_validate({**value, field: supplied})
    for subject in (" ", "Subject\r\nBcc: other@example.test"):
        with pytest.raises(ValidationError):
            DraftCustomerEmail.model_validate({**value, "subject": subject})


def test_agent_email_trigger_requires_real_run_step_policy_and_no_invented_source_trigger() -> None:
    with action_world() as world:
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Draft a customer email for review",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        world.store.rollback()
        run = world.store.get(AgentRun, UUID(response.json()["id"]))
        tenant = world.store.get(Tenant, world.ctx.tenant_id)
        assert run is not None and tenant is not None
        step = start_step(
            world.store, run, kind="action", tool_name="draft_customer_email", invoice_id=world.invoice_id
        )
        source = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert source is not None
        values = dict(
            tenant_id=run.tenant_id,
            provider_connection_id=world.connection_id,
            invoice_id=world.invoice_id,
            source_revision=source.source_revision,
            source_event_id=None,
            policy_version_id=world.policy_version_id,
            policy_generation=1,
            initiator_id=run.initiator_id,
            membership_version=run.membership_version,
            tenant_version=run.tenant_generation,
            outbound_paused=tenant.outbound_paused,
            automation_enabled=tenant.automation_enabled,
            worker_actor_id=run.worker_actor_id,
            actor_version=run.actor_version,
            connection_version=1,
            correlation_id=run.correlation_id,
            kind="agent_email",
            command={"kind": "draft_customer_email"},
            due_at=now(),
            status="proposed",
            agent_run_id=run.id,
            agent_step_id=step.id,
        )
        action = ScheduledAction(**values, operation_key="schema-email-draft")
        world.store.add(action)
        run.status = "awaiting_action"
        world.store.flush()
        for changed in (
            {"source_event_id": world.source_event_id},
            {"agent_step_id": None},
            {"policy_version_id": None},
            {"agent_run_id": uuid4()},
        ):
            with pytest.raises(DBAPIError), world.store.begin_nested():
                world.store.add(ScheduledAction(**(values | changed), operation_key=str(uuid4())))
                world.store.flush()
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 0
        assert (
            world.store.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.action_id == action.id)
            )
            == 0
        )
