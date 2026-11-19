"""T07 durable transaction and worker behavior, using real isolated PostgreSQL."""

from importlib.util import find_spec
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aco.features.actions.service import ActionService


def implementation() -> type["ActionService"]:
    # This assertion gives the initial RED a useful feature boundary, not an import error.
    assert find_spec("aco.features.actions") is not None, "Durable action/outbox service is absent"
    from aco.features.actions.service import ActionService

    return ActionService


def test_business_write_and_outbox_rollback_together() -> None:
    ActionService = implementation()
    from action_support import action_world, scheduled
    from sqlalchemy import func, select

    from aco.features.actions.models import OutboxEvent, ScheduledAction

    with action_world() as world:
        ActionService(world.store).enqueue_action(world.ctx, scheduled(world))
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert world.store.scalar(select(func.count()).select_from(ScheduledAction)) == 0


def test_duplicate_workers_claim_one_effect() -> None:
    implementation()
    from action_support import action_world, enqueue, wait_action, workers

    with action_world() as world:
        ref = enqueue(world)
        with workers(world, count=2):
            action = wait_action(world, ref.action_id, "succeeded")
            assert action.attempts == 1
            assert world.effect_count(ref.action_id) == 1
            assert world.submission_count(ref.action_id) == 1
        assert len(world.worker_pids) == 2


def test_crash_after_external_acceptance_is_unknown() -> None:
    implementation()
    from action_support import action_world, enqueue, wait_action, workers

    with action_world() as world:
        ref = enqueue(world)
        with workers(world, crash="after_acceptance") as processes:
            world.wait_effect(ref.action_id)
            processes[0].join(10)
            assert processes[0].exitcode == 71
        world.expire_vendor_key(ref.action_id, True)
        with workers(world):
            action = wait_action(world, ref.action_id, "unknown_outcome")
            assert action.automatic_resend is False
            assert world.effect_count(ref.action_id) == 1
            assert world.submission_count(ref.action_id) == 1
            assert action.attempts == 1


def test_bounded_retries_dead_letter_and_operator_inspection() -> None:
    implementation()
    from action_support import action_world, enqueue, run_attempts

    with action_world() as world:
        ref = enqueue(world)
        run_attempts(world, ref, "TEMPORARILY_UNAVAILABLE")
        response = world.client.get(
            f"/api/workspaces/{world.ctx.tenant_id}/scheduled-actions/{ref.action_id}"
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "dead_letter"
        assert result["attempts"] == 3
        assert [a["retry_delay_seconds"] for a in result["executions"]] == [60, 300, None]
        assert result["last_error"] == "TEMPORARILY_UNAVAILABLE"
