"""Independent adversarial approval probes; actual PostgreSQL, no dispatch/provider calls."""

from collections.abc import Callable
from typing import cast
from uuid import uuid4

import pytest
from action_support import action_world
from fastapi import HTTPException
from integration.test_approval_scope import request, reviewer
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from aco.core.context import TenantContext
from aco.features.actions.models import ScheduledAction
from aco.features.actions.service import worker_context
from aco.features.identity.models import now
from aco.features.review.approval_dto import ApprovalDecisionCommand, ApprovalView
from aco.features.review.approvals import HumanApprovalService
from aco.features.review.models import ApprovalDecision, ApprovalRequest


def decision(view: ApprovalView) -> ApprovalDecisionCommand:
    return ApprovalDecisionCommand(
        expected_version=view.version,
        reviewed_action_version=view.action_version,
        reviewed_material_hash=view.material_hash,
        reviewed_binding_hash=view.current_binding_hash,
        decision="approved",
        reason="Independent exact synthetic evidence review",
    )


def test_human_approval_service_refuses_valid_invoice_bound_worker_and_foreign_workspace() -> None:
    with action_world() as world:
        key = request(world)
        ctx = reviewer(world)
        service = HumanApprovalService(world.store)
        view = service.get(ctx, key)
        action = world.store.get(ScheduledAction, view.action_id)
        assert action is not None
        scoped = cast(TenantContext, worker_context(action))
        operations: tuple[Callable[[], object], ...] = (
            lambda: service.get(scoped, key),
            lambda: service.list(scoped),
            lambda: service.decide_approval(scoped, key, decision(view)),
        )
        for invoke in operations:
            with pytest.raises(HTTPException) as denied:
                invoke()
            assert denied.value.status_code == 403 and denied.value.detail == "HUMAN_REQUIRED"
        with pytest.raises(HTTPException) as denied:
            service.get(ctx.model_copy(update={"tenant_id": uuid4()}), key)
        assert denied.value.status_code == 403
        assert world.store.scalar(select(ApprovalDecision).where(ApprovalDecision.request_id == key)) is None
        assert world.submission_count(view.action_id) == 0


def test_original_pending_proposal_cannot_be_mutated_or_deleted() -> None:
    with action_world() as world:
        key = request(world)
        with pytest.raises(DBAPIError, match="immutable approval proposal"):
            with world.store.begin_nested():
                world.store.execute(
                    update(ApprovalRequest).where(ApprovalRequest.id == key).values(material_hash="0" * 64)
                )
        assert world.store.get(ApprovalRequest, key) is not None
        # A pending proposal has no decision FK yet; its original material is still audit evidence.
        with pytest.raises(DBAPIError, match="immutable approval proposal"):
            with world.store.begin_nested():
                world.store.execute(delete(ApprovalRequest).where(ApprovalRequest.id == key))


def test_decision_is_immutable_and_consumed_approval_cannot_be_reused() -> None:
    with action_world() as world:
        key = request(world)
        ctx = reviewer(world)
        service = HumanApprovalService(world.store)
        view = service.get(ctx, key)
        approved = service.decide_approval(ctx, key, decision(view))
        world.store.commit()
        assert approved.approval_valid
        record = world.store.scalar(select(ApprovalDecision).where(ApprovalDecision.request_id == key))
        assert record is not None
        for statement in (
            update(ApprovalDecision).where(ApprovalDecision.id == record.id).values(reason="changed"),
            delete(ApprovalDecision).where(ApprovalDecision.id == record.id),
        ):
            with pytest.raises(DBAPIError, match="immutable approval decision"):
                with world.store.begin_nested():
                    world.store.execute(statement)
        with pytest.raises(HTTPException) as stale:
            service.decide_approval(ctx, key, decision(approved))
        assert stale.value.status_code == 409
        # Stored-state probe only: no claim that the unavailable dispatch consumer executed.
        row = world.store.get(ApprovalRequest, key)
        assert row is not None
        row.consumed_at = now()
        world.store.commit()
        consumed = service.get(ctx, key)
        assert consumed.effective_status == "consumed" and not consumed.approval_valid
        assert not consumed.can_approve and consumed.new_approval_required
        assert world.submission_count(view.action_id) == 0


def test_r13_preserves_existing_pending_proposal_and_lifecycle() -> None:
    from runpy import run_path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    with action_world() as world:
        key = request(world)
        ctx = reviewer(world)
        original = world.store.get(ApprovalRequest, key)
        assert original is not None
        snapshot = (original.proposal, original.material_hash, original.proposer_id, original.created_at)
        world.store.rollback()
        # Recreate only r12's exact UPDATE-only trigger in this owned schema.
        # Later retained-history migrations must never be destructively downgraded.
        head = world.store.scalar(text("SELECT version_num FROM alembic_version"))
        world.store.execute(text("DROP TRIGGER immutable_approval_proposal ON approval_requests"))
        world.store.execute(
            text(
                "CREATE TRIGGER immutable_approval_proposal BEFORE UPDATE ON approval_requests "
                "FOR EACH ROW EXECUTE FUNCTION preserve_approval_proposal()"
            )
        )
        probe = world.store.begin_nested()
        world.store.execute(delete(ApprovalRequest).where(ApprovalRequest.id == key))
        probe.rollback()
        migration = run_path("alembic/versions/r0013_approval_proposal_history.py")
        with Operations.context(MigrationContext.configure(world.store.connection())):
            migration["upgrade"]()
        assert world.store.scalar(text("SELECT version_num FROM alembic_version")) == head
        world.store.commit()
        world.store.expire_all()
        retained = world.store.get(ApprovalRequest, key)
        assert retained is not None
        assert (
            retained.proposal,
            retained.material_hash,
            retained.proposer_id,
            retained.created_at,
        ) == snapshot
        with pytest.raises(DBAPIError, match="immutable approval proposal"):
            with world.store.begin_nested():
                world.store.execute(delete(ApprovalRequest).where(ApprovalRequest.id == key))
        service = HumanApprovalService(world.store)
        approved = service.decide_approval(ctx, key, decision(service.get(ctx, key)))
        world.store.commit()
        assert approved.approval_valid
        retained.consumed_at = now()
        world.store.commit()
        assert service.get(ctx, key).effective_status == "consumed"
        assert world.submission_count(approved.action_id) == 0
