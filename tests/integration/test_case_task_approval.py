"""Exact retained case binding through real model reads, review and business-impact API."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from action_support import World, action_world, workers
from case_promise_support import communication
from fastapi import HTTPException
from integration.test_agent_approval import independent_reviewer, wait_run
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.dto import CommunicationEvidence, UpsertCase
from aco.features.cases.service import CaseService
from aco.features.identity.models import now
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.review.models import ApprovalRequest


def prepare_case(world: World, *, multiple: bool = False) -> tuple[UUID, int]:
    invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
    assert invoice is not None
    text = "Synthetic source: the invoice amount needs documentary review."
    rows = [invoice]
    if multiple:
        other = world.store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                InvoiceSnapshot.customer_id == invoice.customer_id,
                InvoiceSnapshot.id != invoice.id,
            )
        )
        assert other is not None
        rows.append(other)
    message = communication(world.store, world.ctx.tenant_id, rows, text)
    case = (
        CaseService(world.store)
        .upsert_case(
            world.ctx,
            UpsertCase(
                customer_id=invoice.customer_id,
                invoice_ids=[row.id for row in rows],
                issue_type="wrong_amount",
                claim_key="exact-reviewed-task",
                substance="Documentary amount review",
                evidence=CommunicationEvidence(communication_id=message.id, excerpt=text, offset=0),
            ),
        )
        .case
    )
    world.store.commit()
    return case.id, case.version


def test_actual_case_task_proposal_review_is_attributed_without_financial_resolution() -> None:
    with action_world() as world:
        case_id, version = prepare_case(world)
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        financial = (invoice.source_revision, invoice.balance_minor, invoice.financial_status)
        world.store.rollback()
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": (
                    f"Request approval before creating an internal task for case {case_id} version {version}"
                ),
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        with workers(world, crash="domain"):
            run = wait_run(world, run_id)
        assert run["status"] == "awaiting_approval", run
        world.store.rollback()
        request = world.store.scalar(select(ApprovalRequest))
        assert request is not None
        assert request.proposal.get("case_binding") == {"case_id": str(case_id), "case_version": version}
        import json

        from aco.features.agent.models import AgentRun
        from aco.features.cases.task_binding import TaskCaseBinding, current_task_case
        from aco.features.review.approval_dto import InternalTaskApprovalProposal

        persisted_run = world.store.get(AgentRun, UUID(run_id))
        assert persisted_run is not None
        proposal = InternalTaskApprovalProposal.model_validate_json(json.dumps(request.proposal))
        assert proposal.case_binding is not None
        for bound, invoice_id, evidence in (
            (
                TaskCaseBinding(case_id=uuid4(), case_version=version),
                world.invoice_id,
                proposal.evidence_step_ids,
            ),
            (proposal.case_binding, uuid4(), proposal.evidence_step_ids),
            (proposal.case_binding, world.invoice_id, []),
        ):
            with pytest.raises(HTTPException):
                current_task_case(world.store, persisted_run, invoice_id, bound, evidence)
        reviewer = independent_reviewer(world)
        service = HumanApprovalService(world.store)
        review = service.get(reviewer, request.id)
        service.decide_approval(
            reviewer,
            request.id,
            ApprovalDecisionCommand(
                expected_version=review.version,
                reviewed_action_version=review.action_version,
                reviewed_material_hash=review.material_hash,
                reviewed_binding_hash=review.current_binding_hash,
                decision="approved",
                reason="Reviewed exact case and task; no change to issue disposition",
            ),
        )
        world.store.commit()
        path = f"/api/workspaces/{world.ctx.tenant_id}/impact/case-outcomes"
        result = world.client.get(
            path,
            params={
                "since": (now() - timedelta(days=1)).isoformat(),
                "until": (now() + timedelta(days=1)).isoformat(),
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["case_approval_count"] == 1
        assert result.json()["cases"][0]["approval_decision_ids"]
        assert result.json()["resolved_count"] == 0
        world.store.rollback()
        with workers(world, crash="domain"):
            assert wait_run(world, run_id)["status"] == "completed"
        world.store.rollback()
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        assert (invoice.source_revision, invoice.balance_minor, invoice.financial_status) == financial


@pytest.mark.parametrize("after_decision", [False, True])
def test_case_version_change_invalidates_review_or_unconsumed_grant(after_decision: bool) -> None:
    from sqlalchemy import func

    from aco.features.cases.dto import TransitionCase
    from aco.features.review.models import CollectionTask

    with action_world() as world:
        case_id, version = prepare_case(world)
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": f"Request approval for internal task for case {case_id} version {version}",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        with workers(world, crash="domain"):
            assert wait_run(world, run_id)["status"] == "awaiting_approval"
        world.store.rollback()
        request = world.store.scalar(select(ApprovalRequest))
        assert request is not None
        request_id = request.id
        reviewer = independent_reviewer(world)
        service = HumanApprovalService(world.store)
        captured = service.get(reviewer, request_id)
        command = ApprovalDecisionCommand(
            expected_version=captured.version,
            reviewed_action_version=captured.action_version,
            reviewed_material_hash=captured.material_hash,
            reviewed_binding_hash=captured.current_binding_hash,
            decision="approved",
            reason="Exact reviewed case task",
        )
        if after_decision:
            service.decide_approval(reviewer, request_id, command)
            world.store.commit()
        CaseService(world.store).transition_case(
            world.ctx,
            TransitionCase(
                case_id=case_id,
                expected_version=version,
                status="INVESTIGATING",
                reason="New substantive case review",
            ),
        )
        world.store.commit()
        current = service.get(reviewer, request_id)
        assert not current.can_approve and "TASK_CASE_CHANGED" in current.reasons
        if not after_decision:
            with pytest.raises(HTTPException):
                service.decide_approval(reviewer, request_id, command)
            world.store.rollback()
        else:
            world.store.rollback()
            with workers(world, crash="domain"):
                run = wait_run(world, run_id)
            assert run["status"] in {"failed", "partially_completed", "completed"}
        world.store.rollback()
        stored = world.store.get(ApprovalRequest, request_id)
        assert stored is not None and stored.consumed_at is None
        assert (
            world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        )  # Original case task only.


def test_case_binding_refuses_incomplete_selected_scope() -> None:
    with action_world() as world:
        case_id, version = prepare_case(world, multiple=True)
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": f"Request approval for internal task for case {case_id} version {version}",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        with workers(world, crash="domain"):
            run = wait_run(world, response.json()["id"])
        assert run["status"] == "failed", run
        world.store.rollback()
        assert world.store.scalar(select(ApprovalRequest)) is None


def test_optional_binding_does_not_change_legacy_canonical_task_payload() -> None:
    from aco.features.tools.gateway import CreateTaskCommand

    invoice_id, evidence = uuid4(), [uuid4(), uuid4()]
    command = CreateTaskCommand(
        invoice_id=invoice_id, title="Unbound history", notes="", evidence_step_ids=evidence
    )
    assert command.model_dump(mode="json") == {
        "invoice_id": str(invoice_id),
        "title": "Unbound history",
        "notes": "",
        "evidence_step_ids": list(map(str, evidence)),
    }
