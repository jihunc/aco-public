"""Explicit human reports, current actor scope, immutable corrections and unknown time."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from accounting_support import context
from case_promise_support import communication, invoices
from fastapi import FastAPI
from identity_support import actor_client, add_actor
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aco.features.cases.dto import CommunicationEvidence, TransitionCase, UpsertCase
from aco.features.cases.service import CaseService
from aco.features.identity.models import Membership, now
from aco.features.impact.case_outcome_api import router as outcomes_router
from aco.features.impact.handling_api import router


def test_reported_minutes_exact_replay_correction_history_and_revocation() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        app.include_router(outcomes_router)
        rows = invoices(client, db, tenant)[:1]
        body = "Synthetic invoice dispute requiring actual human source review."
        msg = communication(db, tenant, rows, body)
        case = (
            CaseService(db)
            .upsert_case(
                context(tenant, user.id),
                UpsertCase(
                    customer_id=rows[0].customer_id,
                    issue_type="wrong_amount",
                    claim_key="time-report",
                    substance="Human investigated source discrepancy",
                    invoice_ids=[rows[0].id],
                    evidence=CommunicationEvidence(communication_id=msg.id, excerpt=body, offset=0),
                ),
            )
            .case
        )
        db.commit()
        path = f"/api/workspaces/{tenant}/cases/{case.id}/handling-time"
        outcome_path = f"/api/workspaces/{tenant}/impact/case-outcomes"
        window = {
            "since": (case.opened_at - timedelta(seconds=1)).isoformat(),
            "until": (case.opened_at + timedelta(days=1)).isoformat(),
        }
        before = client.get(outcome_path, params=window).json()
        assert before["reported_human_minutes"] is None
        command = {
            "request_id": str(uuid4()),
            "history_id": str(case.history[0].id),
            "expected_case_version": case.version,
            "minutes": "4.25",
            "evidence": "Operator-reported active review time; excludes passive waiting.",
            "supersedes_id": None,
        }
        assert client.post(path, json=command, headers={"x-csrf-token": "invalid"}).status_code == 403
        created = client.post(path, json=command)
        assert created.status_code == 201, created.text
        entry = created.json()
        assert entry["basis"] == "MANUAL_REPORTED" and Decimal(entry["minutes"]) == Decimal("4.25")
        assert client.post(path, json=command).json()["id"] == entry["id"]
        assert client.post(path, json=command | {"minutes": "5"}).status_code == 409
        assert (
            client.post(path, json=command | {"request_id": str(uuid4()), "minutes": "4.251"}).status_code
            == 422
        )
        assert (
            client.post(
                path, json=command | {"request_id": str(uuid4()), "expected_case_version": case.version + 1}
            ).status_code
            == 409
        )
        assert (
            client.post(
                path, json=command | {"request_id": str(uuid4()), "history_id": str(uuid4())}
            ).status_code
            == 403
        )
        original_asof = now()
        measured = client.get(outcome_path, params=window).json()
        assert Decimal(measured["reported_human_minutes"]) == Decimal("4.25")
        assert measured["missing_handling_event_count"] == 0
        corrected = client.post(
            path,
            json=command
            | {
                "request_id": str(uuid4()),
                "minutes": "3.50",
                "supersedes_id": entry["id"],
                "evidence": "Corrected my report after checking actual review duration.",
            },
        )
        assert corrected.status_code == 201, corrected.text
        assert (
            client.post(
                path,
                json=command | {"request_id": str(uuid4()), "minutes": "2", "supersedes_id": entry["id"]},
            ).status_code
            == 409
        )
        assert Decimal(client.get(outcome_path, params=window).json()["reported_human_minutes"]) == Decimal(
            "3.50"
        )
        assert Decimal(
            client.get(outcome_path, params=window | {"as_of": original_asof.isoformat()}).json()[
                "reported_human_minutes"
            ]
        ) == Decimal("4.25")
        history = client.get(path).json()
        assert len(history["entries"]) == 2 and history["current_entry_ids"] == [corrected.json()["id"]]
        for statement in (
            "UPDATE human_handling_entries SET minutes=0 WHERE id=:id",
            "DELETE FROM human_handling_entries WHERE id=:id",
        ):
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(text(statement), {"id": entry["id"]})
        other, other_client = add_actor(db, app, "other-handler@synthetic.invalid")
        db.add(Membership(tenant_id=tenant, user_id=other.id, role="owner"))
        db.commit()
        try:
            assert other_client.post(path, json=command | {"request_id": str(uuid4())}).status_code == 403
            assert other_client.get(path).json()["can_record_history_ids"] == []
        finally:
            other_client.close()
        investigating = CaseService(db).transition_case(
            context(tenant, user.id),
            TransitionCase(
                case_id=case.id,
                expected_version=case.version,
                status="INVESTIGATING",
                reason="Additional actual evidence investigation",
            ),
        )
        db.commit()
        partial = client.get(outcome_path, params=window).json()
        assert partial["reported_human_minutes"] is None
        assert Decimal(partial["known_reported_human_minutes"]) == Decimal("3.50")
        assert partial["missing_handling_event_count"] == 1
        extra = next(h for h in investigating.history if h.event == "case.transitioned")
        zero = client.post(
            path,
            json=command
            | {
                "request_id": str(uuid4()),
                "history_id": str(extra.id),
                "expected_case_version": investigating.version,
                "minutes": "0",
                "evidence": "Explicit report: status transition added no separately timed active work.",
            },
        )
        assert zero.status_code == 201, zero.text
        covered = client.get(outcome_path, params=window).json()
        assert Decimal(covered["reported_human_minutes"]) == Decimal("3.50")
        assert covered["human_intervention_count"] == 1
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert member is not None
        member.role = "viewer"
        db.commit()
        assert client.post(path, json=command).status_code == 403
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("value", [1.5, "NaN", "Infinity", "-1", "1440.01", "0.001"])
def test_reported_duration_rejects_lossy_unbounded_or_overprecision_values(value: object) -> None:
    from pydantic import ValidationError

    from aco.features.impact.handling_dto import RecordHandlingTime

    with pytest.raises(ValidationError):
        RecordHandlingTime.model_validate(
            {
                "request_id": uuid4(),
                "history_id": uuid4(),
                "expected_case_version": 1,
                "minutes": value,
                "evidence": "Explicit manual reporting fixture",
            }
        )
