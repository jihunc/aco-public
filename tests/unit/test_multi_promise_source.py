"""Fixed synthetic MIME terms are deterministic source statements, never arbitrary input."""

from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from uuid import uuid4

import pytest

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.communications.ingress_dto import SimulateIngress
from aco.features.communications.simulation import _message


@pytest.mark.parametrize(
    "scenario,per_invoice,total,day",
    [
        ("multi_promise_reply", "100.00", "200.00", "26"),
        ("multi_promise_amendment", "75.00", "150.00", "03"),
        ("installment_promise_reply", "100.00", "200.00", "03"),
    ],
)
def test_fixed_multi_promise_terms_match_stored_mime_and_actual_capture_date(
    monkeypatch: pytest.MonkeyPatch, scenario: str, per_invoice: str, total: str, day: str
) -> None:
    at = datetime(2026, 11, 19, 23, 59, 59, tzinfo=UTC)
    monkeypatch.setattr("aco.features.communications.simulation.now", lambda: at)
    ctx = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role=Role.OWNER, correlation_id=uuid4())
    command = SimulateIngress.model_validate(
        {
            "request_id": uuid4(),
            "expected_activation_version": 1,
            "invoice_ids": [uuid4(), uuid4()],
            "scenario": scenario,
        }
    )
    message = _message(
        ctx,
        uuid4(),
        "simulated:fixed",
        "fixed-command-binding",
        command,
        ["INV-101", "INV-107"],
        "synthetic@example.test",
    )
    text = message.text_body
    assert isinstance(text, str) and message.source is not None and message.source.content is not None
    assert f"AUD {total} total" in text
    for number in ["INV-101", "INV-107"]:
        assert f"AUD {per_invoice} to {number}" in text
    assert f"-{day} in UTC" in text
    if scenario == "installment_promise_reply":
        assert "Installment 1: AUD 100.00 total by 2026-11-26 in UTC" in text
        assert "Installment 2: AUD 100.00 total by 2026-12-03 in UTC" in text
        for number in ["INV-101", "INV-107"]:
            assert text.count(f"AUD 50.00 to {number}") == 2
    assert "SYNTHETIC DEMO SOURCE" in text
    original = BytesParser(policy=default).parsebytes(message.source.content)
    assert original.get_content().strip() == text.strip()
    assert message.received_at == at and message.synthetic and message.authenticity == "SIMULATED"
