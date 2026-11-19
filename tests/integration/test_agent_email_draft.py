"""A draft is stored source-backed text, never a send activation or dispatch action."""

import pytest
from action_support import action_world
from evaluations.test_collection_agent import proposal, run_sdk
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import OutboxEvent, ScheduledAction
from aco.features.agent.models import AgentStep
from aco.features.agent.nodes import InvestigationNode
from aco.features.agent.schemas import GraphState
from aco.features.communications.models import Communication, EmailExecutionActivation, EmailSubmission
from aco.features.identity.models import AuditEvent, ProviderConnection
from aco.features.policies.models import ContactReservation
from aco.features.review.models import ApprovalRequest
from aco.features.usage.models import BudgetReservation


def test_inert_draft_needs_no_outbound_setup_and_replays_once(monkeypatch: pytest.MonkeyPatch) -> None:
    original = InvestigationNode.execute

    async def replay(node: InvestigationNode, state: GraphState) -> GraphState:
        first = await original(node, state)
        second = await original(node, state)
        assert first == second
        return second

    monkeypatch.setattr(InvestigationNode, "execute", replay)
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        command = {
            "invoice_id": str(invoice.id),
            "subject": "Synthetic source review draft",
            "content": "Please contact our team about the retained invoice. This draft has not been sent.",
        }
        saved = run_sdk(world, [invoice], [proposal(invoice, "draft_customer_email", email_command=command)])
        assert saved["status"] == "completed", saved
        result = saved["result"]["proposals"][0]
        assert result["email_command"] == command
        assert result["executed"] is False and result["action_id"] is None
        assert result["approval_id"] is None and result["changed_ids"] == []
        world.store.rollback()
        steps = list(
            world.store.scalars(select(AgentStep).where(AgentStep.tool_name == "draft_customer_email"))
        )
        assert len(steps) == 1
        assert steps[0].status == "completed" and steps[0].input == command
        assert steps[0].output is not None and steps[0].output["submitted"] is False
        assert (
            world.store.scalar(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "email.drafted")
            )
            == 1
        )
        for model in (
            ScheduledAction,
            OutboxEvent,
            EmailExecutionActivation,
            EmailSubmission,
            ContactReservation,
            Communication,
            ApprovalRequest,
        ):
            assert world.store.scalar(select(func.count()).select_from(model)) == 0

        assert (
            world.store.scalar(
                select(func.count())
                .select_from(BudgetReservation)
                .join(ProviderConnection, ProviderConnection.id == BudgetReservation.provider_connection_id)
                .where(ProviderConnection.provider_type == "email")
            )
            == 0
        )
