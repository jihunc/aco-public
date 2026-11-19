"""Agent patch excludes fields that could lower urgency or redirect human work."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from aco.features.tools.task_update import TaskUpdate


def test_task_update_roundtrip_preserves_partial_patch_and_rejects_scope_expansion() -> None:
    command = TaskUpdate(task_id=uuid4(), expected_version=4, notes="")
    replay = TaskUpdate.model_validate_json(command.model_dump_json())
    assert replay == command
    assert replay.patch().model_fields_set == {"expected_version", "notes"}
    for forbidden in ("invoice_id", "assignee_id", "priority", "priority_rank", "due_at", "deleted_at"):
        with pytest.raises(ValidationError):
            TaskUpdate.model_validate(command.model_dump() | {forbidden: None})
    with pytest.raises(ValidationError):
        TaskUpdate(task_id=uuid4(), expected_version=1)
    with pytest.raises(ValidationError):
        TaskUpdate(task_id=uuid4(), expected_version=1, title="   ")
