"""Catch fabricated live PASS and dropped mandatory boundary outputs in persisted evidence."""

import json
from pathlib import Path

import pytest
from boundary_evidence import BoundaryObserver
from pydantic import ValidationError

from aco.core.evidence import VerificationRow, read_verifications, record_verification

FIXTURES = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]


def test_boundary_mock_live_evidence_separation(tmp_path: Path, boundary_observer: BoundaryObserver) -> None:
    cases = [case for case in FIXTURES if case["domain"] == "evidence"]
    assert [case["id"] for case in cases] == ["evidence_mock_not_live"]
    for case in cases:
        row = VerificationRow(
            task="T02",
            requirements=case["requirements"],
            command="synthetic offline assertion",
            lane=case["input"]["lane"],
            result=case["input"]["fake_adapter_result"],
            environment="local",
            commit="3775d59",
            usage={},
            cost_quality="SIMULATED",
            cost_amount="0",
            currency="AUD",
            live_authorized=case["input"]["live_authorized"],
        )
        ledger = tmp_path / "verification.sqlite3"
        record_verification(row, path=ledger)
        persisted = read_verifications(ledger)
        assert len(persisted) == 1
        actual = persisted[0]
        assert set(case["expected"]) == {"offline_result", "required_live_result", "provider_readiness"}
        for key, expected in case["expected"].items():
            assert actual[key] == expected, (case["id"], key)
        boundary_observer.assert_observed(case["id"], {key: actual[key] for key in case["expected"]})
        assert actual["result"] == "PASS"
        assert actual["lane"] == "offline"
        assert actual["cost_quality"] == "SIMULATED"
        assert actual["cost_amount"] == "0"
        assert actual["currency"] == "AUD"
        assert actual["provider_operation_id"] is None
        assert actual["source_ids"] == [] and actual["changed_db_ids"] == []


def test_result_lane_and_cost_are_mandatory() -> None:
    with pytest.raises(ValidationError):
        VerificationRow.model_validate({"task": "T02", "result": "PASS"})
    base: dict[str, object] = dict(
        task="T02",
        requirements=["TP-07"],
        command="check",
        environment="local",
        commit="3775d59",
        lane="live",
        result="PASS",
        usage={},
        cost_quality="UNKNOWN",
        cost_amount=None,
        currency="AUD",
    )
    with pytest.raises(ValidationError):
        VerificationRow.model_validate(base)
    with pytest.raises(ValidationError):
        VerificationRow.model_validate(base | {"lane": "offline", "result": "NOT_RUN"})


def test_cost_currency_is_required_and_unknown_never_becomes_zero() -> None:
    base: dict[str, object] = dict(
        task="T02",
        requirements=["TP-07"],
        command="check",
        environment="local",
        commit="3775d59",
        lane="offline",
        result="PASS",
        usage={},
        cost_quality="SIMULATED",
        cost_amount="0",
    )
    with pytest.raises(ValidationError):
        VerificationRow.model_validate(base)
    with pytest.raises(ValidationError):
        VerificationRow.model_validate(
            base | {"currency": "AUD", "cost_quality": "UNKNOWN", "cost_amount": "0"}
        )


def test_execution_metadata_is_pending_until_independent_review(tmp_path: Path) -> None:
    ledger = tmp_path / "review.sqlite3"
    row = VerificationRow(
        task="T02",
        requirements=["TP-07"],
        command="synthetic test",
        lane="offline",
        result="PASS",
        environment="synthetic",
        commit="3775d59",
        usage={},
        cost_quality="SIMULATED",
        cost_amount="0",
        currency="AUD",
    )
    record_verification(row, path=ledger)
    persisted = read_verifications(ledger)[0]
    assert persisted["critic_outcome"] == "PENDING"
    assert persisted["iteration"] == 1
    assert persisted["defect_refs"] == []
    assert persisted["residual_blockers"] == []
    assert persisted["reviews"] == []


def test_append_only_critic_link_preserves_original_execution(tmp_path: Path) -> None:
    import sqlite3
    from uuid import UUID

    from aco.core.evidence import VerificationReview, record_review

    ledger = tmp_path / "review.sqlite3"
    row = VerificationRow(
        task="T02",
        requirements=["TP-07"],
        command="synthetic test",
        lane="offline",
        result="PASS",
        environment="synthetic",
        commit="3775d59",
        usage={},
        cost_quality="SIMULATED",
        cost_amount="0",
        currency="AUD",
        iteration=2,
        defect_refs=["synthetic:F1"],
        residual_blockers=["review pending"],
    )
    record_verification(row, path=ledger)
    before = read_verifications(ledger)[0]
    with sqlite3.connect(ledger) as connection:
        original_bytes = connection.execute("SELECT payload FROM verification").fetchone()[0]
    record_review(
        VerificationReview(
            verification_id=UUID(str(before["id"])),
            critic_outcome="FAIL",
            iteration=1,
            defect_refs=["synthetic:F1"],
            residual_blockers=["synthetic unresolved finding"],
            reviewer="synthetic-critic-fixture",
            report_ref="synthetic://review/1",
        ),
        path=ledger,
    )
    after = read_verifications(ledger)[0]
    assert after["critic_outcome"] == "PENDING" and after["iteration"] == 2
    assert after["latest_critic_outcome"] == "FAIL"
    reviews = after["reviews"]
    assert isinstance(reviews, list) and len(reviews) == 1
    assert reviews[0]["verification_id"] == before["id"]
    assert reviews[0]["critic_outcome"] == "FAIL" and reviews[0]["iteration"] == 1
    assert reviews[0]["defect_refs"] == ["synthetic:F1"]
    assert reviews[0]["residual_blockers"] == ["synthetic unresolved finding"]
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT payload FROM verification").fetchone()[0] == original_bytes


def test_legacy_evidence_is_unrecorded_not_invented_pending_review(tmp_path: Path) -> None:
    import sqlite3

    ledger = tmp_path / "legacy.sqlite3"
    payload = json.dumps({"id": "legacy", "result": "PASS", "lane": "offline"})
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE TABLE verification (payload TEXT NOT NULL)")
        connection.execute("INSERT INTO verification VALUES (?)", (payload,))
    observed = read_verifications(ledger)[0]
    assert observed["critic_outcome"] == "UNRECORDED"
    assert observed["iteration"] is None and observed["defect_refs"] is None
    assert observed["residual_blockers"] is None
    assert observed["metadata_origin"] == "LEGACY_UNRECORDED"
    assert observed["reviews"] == []
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT payload FROM verification").fetchone()[0] == payload
