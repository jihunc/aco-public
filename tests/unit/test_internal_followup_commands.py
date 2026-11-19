"""Internal work input cannot carry authority or silently infer a due timezone."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from aco.features.tools.internal_followup import RequestHumanReview, ScheduleFollowup


def test_followup_requires_explicit_aware_time_and_rejects_authority_fields() -> None:
    payload = {
        "invoice_id": str(uuid4()),
        "title": "Review incoming commitment",
        "evidence_step_ids": [str(uuid4()), str(uuid4())],
    }
    with pytest.raises(ValidationError):
        ScheduleFollowup.model_validate_json(__import__("json").dumps(payload))
    with pytest.raises(ValidationError):
        ScheduleFollowup.model_validate_json(
            __import__("json").dumps(payload | {"due_at": "2027-01-15T12:00:00"})
        )
    command = ScheduleFollowup.model_validate_json(
        __import__("json").dumps(payload | {"due_at": "2027-01-15T12:00:00+11:00"})
    )
    assert command.due_at.isoformat() == "2027-01-15T12:00:00+11:00"
    for extra in ({"assignee_id": str(uuid4())}, {"tenant_id": str(uuid4())}, {"approved": True}):
        with pytest.raises(ValidationError):
            RequestHumanReview.model_validate_json(__import__("json").dumps(payload | extra))
    with pytest.raises(ValidationError):
        RequestHumanReview.model_validate_json(__import__("json").dumps(payload | {"title": "  "}))
