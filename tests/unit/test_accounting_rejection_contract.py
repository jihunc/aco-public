"""Bounded rejection metadata cannot be mistaken for verified accounting facts."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from aco.core.context import TenantContext
from aco.core.types import ProviderErrorCode, Role
from aco.integrations.ports.accounting import (
    AccountingRejectionEvidence,
    RejectedPaymentReference,
    failure,
    rejection_reference,
    source_rejection,
)


def test_rejection_metadata_is_bounded_unverified_and_omitted_for_transport() -> None:
    ctx = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role=Role.OWNER, correlation_id=uuid4())
    reference = rejection_reference(
        {
            "external_payment_id": "P1",
            "external_allocation_id": "A1",
            "kind": "reversal",
            "amount": {"minor_units": -100},
            "private_body": "must-never-be-copied",
        },
        "N1",
        "1",
    )
    assert reference is not None
    related = [RejectedPaymentReference(native_source_id=f"N{i}", kind="reversal") for i in range(2, 12)]
    evidence = source_rejection(ctx, uuid4(), "INV-1", "payment_shape_invalid", reference, related)
    assert evidence is not None and evidence.authenticity == "UNVERIFIED"
    assert len(evidence.related_reversal_refs) == 8 and evidence.related_reversal_refs_truncated
    assert (
        "private_body" not in evidence.model_dump_json() and "minor_units" not in evidence.model_dump_json()
    )
    assert rejection_reference({"external_payment_id": "x" * 257}, "N", "1") is None
    assert rejection_reference({}, "N\nprivate", "1") is None
    assert failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE).rejection_evidence is None
    assert str(failure(rejection_evidence=evidence)) == "VALIDATION_FAILED"
    with pytest.raises(ValidationError):
        AccountingRejectionEvidence.model_validate({**evidence.model_dump(), "authenticity": "VERIFIED"})
