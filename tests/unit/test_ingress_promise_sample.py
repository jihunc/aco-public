"""The fixed demo commitment is genuine stored synthetic MIME, never a payment assertion."""

from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import pytest

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.communications.ingress_dto import SimulateIngress
from aco.features.communications.simulation import _message


def test_fixed_promise_sample_has_exact_terms_and_preserved_synthetic_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at = datetime(2026, 11, 30, 23, 59, tzinfo=UTC)
    monkeypatch.setattr("aco.features.communications.simulation.now", lambda: at)
    ctx = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role=Role.OWNER, correlation_id=uuid4())
    connection = uuid4()
    command = SimulateIngress.model_validate_json(
        '{"request_id":"' + str(uuid4()) + '","expected_activation_version":1,'
        '"invoice_ids":["' + str(uuid4()) + '"],"scenario":"promise_reply"}'
    )
    message = _message(
        ctx, connection, "native-1", command.model_dump_json(), command, ["INV-101"],
        "customer@synthetic.invalid",
    )
    assert message.synthetic is True and message.authenticity == "SIMULATED"
    assert message.direction == "incoming" and message.kind == "CUSTOMER_REPLY"
    assert message.received_at == at
    assert message.text_body is not None
    assert "not a customer statement" in message.text_body
    assert "I commit to pay AUD 40.00 for INV-101 by 2026-12-07 in UTC." in message.text_body
    assert message.source is not None and message.source.content is not None
    mime = BytesParser(policy=policy.default).parsebytes(message.source.content)
    plain = mime.get_body(preferencelist=("plain",))
    assert plain is not None
    assert plain.get_content().strip() == message.text_body
