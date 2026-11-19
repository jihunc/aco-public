"""Normative histories enter only at the synthetic provider boundary; public APIs freeze results."""

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from accounting_support import seed
from boundary_evidence import BoundaryObserver
from fastapi import FastAPI
from fastapi.testclient import TestClient
from identity_support import actor_client
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aco.core.clock import SystemClock
from aco.features.accounting.models import DemoSourceRecord, VerifiedPaymentAllocation
from aco.features.identity import models as identity_models

CASES = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
PERSISTED_IDS = {
    "cohort_post_horizon_reversal_reduces_asof",
    "cohort_linked_refund_reduces_cash",
    "cohort_due_date_correction_versioned",
    "cohort_restated_version_preserves_prior",
    "cohort_pre_D_cash_refund_after_horizon_restated",
    "cohort_pre_D_cash_reversal_after_horizon_restated",
    "cohort_pre_D_cash_refund_late_observed_restated",
    "cohort_refund_observed_after_asof_excluded",
}
FIXTURES = [row for row in CASES if row["id"] in PERSISTED_IDS]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def post(client: TestClient, path: str, command: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=command)
    assert response.status_code in {200, 201}, response.text
    result: dict[str, Any] = response.json()
    return result


class NativeHistory:
    """Publishes declared synthetic upstream revisions, never ACO projections or result rows."""

    def __init__(self, db: Session, tenant: UUID, connection: UUID, data: dict[str, Any]):
        self.db, self.tenant, self.connection, self.data = db, tenant, connection, data
        source = db.scalar(select(DemoSourceRecord).where(DemoSourceRecord.kind == "invoice"))
        assert source is not None
        self.organisation = source.organisation_id
        self.key, self.revision = "NORMATIVE-FINANCIAL-HISTORY", 0
        due = date.fromisoformat(data["due_date"])
        self.issue = due - timedelta(days=30)
        self.issue_at = datetime.combine(self.issue, time.min, ZoneInfo(data["timezone"]))
        self.start = datetime.combine(due, time.min, ZoneInfo(data["timezone"]))
        self.money = {
            "minor_units": data["original_minor_units"],
            "currency": data["currency"],
            "exponent": 2,
        }
        self.payload = dict(source.payload) | {
            "external_id": self.key,
            "number": self.key,
            "issue_date": str(self.issue),
            "due_date": data["due_date"],
            "original": self.money,
        }
        self.balance = data["original_minor_units"]

    def append(
        self,
        kind: str,
        key: str,
        revision: int,
        payload: dict[str, Any],
        at: datetime,
        observed: datetime | None = None,
    ) -> DemoSourceRecord:
        row = DemoSourceRecord(
            tenant_id=self.tenant,
            provider_connection_id=self.connection,
            organisation_id=self.organisation,
            kind=kind,
            external_id=key,
            revision=revision,
            payload=payload,
            effective_at=at,
            observed_at=observed or at,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def invoice(self, at: datetime, observed: datetime | None = None, due: str | None = None) -> None:
        self.revision += 1
        status = (
            "paid"
            if self.balance == 0
            else "partially_paid"
            if self.balance < self.data["original_minor_units"]
            else "open"
        )
        self.append(
            "invoice",
            self.key,
            self.revision,
            self.payload
            | {
                "outstanding": self.money | {"minor_units": self.balance},
                "status": status,
                "source_status": status.upper(),
                "due_date": due or self.data["due_date"],
            },
            at,
            observed,
        )

    def allocation(self, row: dict[str, Any], kind: str, fallback: datetime) -> None:
        at = instant(row.get("effective_at", row.get("at", fallback.isoformat())))
        observed = instant(row.get("observed_at", at.isoformat()))
        key = row.get("id", kind + "-1")
        amount = row["minor_units"] * (1 if kind == "cash" else -1)
        parent = row.get("allocation")
        self.append(
            "payment",
            key + "/1",
            1,
            {
                "external_payment_id": key,
                "external_allocation_id": "1",
                "external_invoice_id": self.key,
                "kind": kind,
                "amount": self.money | {"minor_units": amount},
                "parent_payment_id": parent,
                "parent_allocation_id": "1" if parent else None,
            },
            at,
            observed,
        )
        self.balance -= amount
        self.invoice(at, observed)

    def original(self) -> None:
        self.invoice(self.issue_at)
        for row in self.data.get("pre_D_allocations", []) + self.data.get("horizon_allocations", []):
            self.allocation(row, "cash", self.start + timedelta(days=1))
        self.append(
            "impact_manifest",
            self.key,
            1,
            {
                "history_contract": "complete_from_issue_v1",
                "synthetic": True,
                "timezone": self.data["timezone"],
                "business_segment": "Explicit normative synthetic history",
                "population": [{"external_invoice_id": self.key}],
            },
            self.issue_at,
        )
        self.db.commit()


@pytest.mark.parametrize("fixture", FIXTURES, ids=[row["id"] for row in FIXTURES])
def test_boundary_persisted_financial_history(
    fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    boundary_observer: BoundaryObserver,
) -> None:
    data = fixture["input"]
    # Explicit deterministic application clock: actual API-created imports predate the selected as-of.
    clock = [instant(data["as_of"]) - timedelta(seconds=1)]

    class TestDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "TestDateTime":
            return cls.fromtimestamp(clock[0].timestamp(), tz or UTC)

    monkeypatch.setattr(identity_models, "datetime", TestDateTime)
    monkeypatch.setattr(SystemClock, "now", lambda self: clock[0])
    with actor_client("owner") as (client, db, user, tenant):
        # The synthetic OIDC fixture spans this deterministic four-day replay.
        app = client.app
        assert isinstance(app, FastAPI)
        db.execute(
            update(identity_models.LoginSession).values(
                expires_at=clock[0] + timedelta(days=7),
                encrypted_token=app.state.identity.protect(
                    {
                        "access_token": user.subject,
                        "refresh_token": "synthetic-refresh",
                        "expires_at": (clock[0] + timedelta(days=7)).timestamp(),
                    }
                ),
            )
        )
        db.commit()
        seeded = seed(client, tenant)
        response = client.patch(
            f"/api/workspaces/{tenant}",
            json={"name": "Normative source history", "timezone": data["timezone"]},
        )
        assert response.status_code == 200, response.text
        source = NativeHistory(db, tenant, UUID(seeded["provider_connection_id"]), data)
        source.original()
        clock[0] = instant(data["as_of"])
        path = f"/api/workspaces/{tenant}/impact/cohorts/observed"
        selection = {
            "connection_id": str(source.connection),
            "period_start": data["due_date"],
            "period_end": str(date.fromisoformat(data["due_date"]) + timedelta(days=1)),
            "currency": data["currency"],
            "credit_terms_days": 30,
            "as_of_at": data["as_of"],
        }
        preview = post(client, path + "/preview", selection)
        assert preview["result"]["status"] == "COMPLETE", preview
        original = post(
            client,
            path,
            {
                "request_id": str(uuid4()),
                "selection": preview["selection"],
                "reviewed_hash": preview["reviewed_hash"],
                "evidence": "Owner freezes exact explicitly synthetic normative history.",
            },
        )
        first = original["versions"][0]
        prior = first["results"][-1]
        for group, kind in (("linked_reversals", "reversal"), ("linked_refunds", "refund")):
            for row in data.get(group, []):
                source.allocation(row, kind, clock[0])
        for row in data.get("adjustments", []):
            source.allocation(row, row["kind"], clock[0])
        if "new_source_due_date" in data:
            source.invoice(clock[0] - timedelta(seconds=1), due=data["new_source_due_date"])
        db.commit()
        # Later actual reads can see source records unavailable to the earlier selected as-of.
        if any(
            instant(row.get("observed_at", data["as_of"])) > clock[0] for row in data.get("adjustments", [])
        ):
            clock[0] += timedelta(days=3)
        mode = "RESTATE" if data.get("restatement_approved") else "REFRESH"
        target = path + f"/{original['id']}"
        review = {
            "selection": selection,
            "expected_definition_id": first["id"],
            "expected_result_id": prior["id"],
            "mode": mode,
        }
        fresh = post(client, target + "/preview", review)
        assert fresh["result"]["status"] == "COMPLETE", fresh
        if fixture["id"] == "cohort_refund_observed_after_asof_excluded":
            assert fresh["result"]["raw_net_minor"] == prior["raw_net_minor"]
            assert fresh["result"]["paid_in_full_count"] == prior["paid_in_full_count"]
        changed = post(
            client,
            target + ("/restate" if mode == "RESTATE" else "/refresh"),
            {
                "request_id": str(uuid4()),
                "selection": fresh["selection"],
                "reviewed_hash": fresh["reviewed_hash"],
                "expected_definition_id": first["id"],
                "expected_result_id": prior["id"],
                "evidence": "Owner reviewed actual source revision and retained the original frozen history.",
            },
        )
        latest_version = changed["versions"][-1]
        latest = latest_version["results"][-1]
        retained = changed["versions"][0]["results"][0] == prior
        revisions = latest_version["current_source_members"][0]["source_revision_ids"]
        source_rows = list(
            db.scalars(
                select(DemoSourceRecord).where(
                    DemoSourceRecord.external_id == source.key, DemoSourceRecord.kind == "invoice"
                )
            )
        )
        adjustments = list(
            db.scalars(
                select(VerifiedPaymentAllocation).where(
                    VerifiedPaymentAllocation.kind.in_(["refund", "reversal"])
                )
            )
        )
        values = {
            "recovery_numerator_minor_units": int(latest["recovery_numerator_minor"])
            if latest["recovery_numerator_minor"] is not None
            else None,
            "raw_net_cohort_cash_minor_units": int(latest["raw_net_minor"])
            if latest["raw_net_minor"] is not None
            else None,
            "recovery_denominator_minor_units": int(latest["opening_minor"]),
            "denominator_minor_units": int(latest["opening_minor"]),
            "paid_in_full_count": latest["paid_in_full_count"],
            "recovery_percent": latest["recovery_percent"],
            "reversal_reporting_period": str(adjustments[0].effective_at.date()) if adjustments else None,
            "prior_snapshot_rewritten": not retained,
            "prior_result_preserved": retained,
            "result_version_increment": latest["version"] - prior["version"],
            "label": latest["label"],
            "refunds_minor_units": int(latest["refunds_minor"]),
            "membership_version": f"V{latest_version['version']}",
            "membership_due_date": latest_version["members"][0]["original_due_date"],
            "new_source_history_retained": all(str(row.id) in revisions for row in source_rows),
            "new_version_required": latest_version["version"] > first["version"],
            "prior_version_preserved": changed["versions"][0] == first,
            "new_result_version_required": latest["id"] != prior["id"],
            "prior_result_version": f"M{changed['versions'][0]['results'][0]['version']}",
            "result_version": f"M{latest['version']}",
            "membership_and_denominator_preserved": latest_version["members"] == first["members"]
            and latest["opening_minor"] == prior["opening_minor"],
        }
        actual = {key: values[key] for key in fixture["expected"]}
        assert actual == fixture["expected"]
        boundary_observer.assert_observed(fixture["id"], actual)
