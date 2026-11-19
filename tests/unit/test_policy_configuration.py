"""Configuration cannot disable hard safety constraints or invent currency/timezone evidence."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from aco.features.policies.dto import PolicyDefinition, RecipientSave


@pytest.mark.parametrize(
    "changes",
    [
        {"cap_24h": 2},
        {"cap_7d": 4},
        {"hold_promises": False},
        {"hold_disputes": False},
        {"ai_external_approval_required": False},
        {"voice_approval_required": False},
        {"approval_currency": "ZZZ", "approval_threshold_minor": "100"},
    ],
)
def test_policy_configuration_rejects_weakened_guards(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate({"rules": [{"id": str(uuid4()), "offset_days": 3}], **changes})


def test_recipient_timezone_evidence_cannot_conflict() -> None:
    with pytest.raises(ValidationError):
        RecipientSave(
            expected_contact_revision="synthetic-revision",
            expected_destination="recipient@example.test",
            expected_version=0,
            timezone="Australia/Melbourne",
            owner_confirmed_tenant_timezone=True,
            expected_tenant_timezone="UTC",
            evidence="Synthetic contradictory evidence",
        )


@pytest.mark.parametrize("fallback,zone", [(True, None), (True, ""), (False, "UTC")])
def test_recipient_fallback_requires_exact_reviewed_timezone(fallback: bool, zone: str | None) -> None:
    with pytest.raises(ValidationError, match="workspace timezone|Workspace timezone"):
        RecipientSave(
            expected_version=0,
            expected_contact_revision="opaque",
            expected_destination=None,
            owner_confirmed_tenant_timezone=fallback,
            expected_tenant_timezone=zone,
            evidence="Synthetic review",
        )
