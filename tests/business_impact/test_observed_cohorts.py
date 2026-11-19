"""Observed execution freezes actual accounting history, never guessed opening balances."""

from datetime import timedelta
from uuid import uuid4

import pytest
from accounting_support import seed
from identity_support import actor_client

from aco.features.identity.models import now


def test_normal_import_freezes_incomplete_observed_members_without_invented_opening() -> None:
    with actor_client("owner") as (client, _db, _user, tenant):
        connection = seed(client, tenant)
        base = f"/api/workspaces/{tenant}/impact/cohorts/observed"
        listing = client.get(base)
        assert listing.status_code == 200, listing.text
        assert listing.json()["can_build"] is True
        selection = {
            "connection_id": connection["provider_connection_id"],
            "period_start": str(now().date() - timedelta(days=100)),
            "period_end": str(now().date() + timedelta(days=100)),
            "currency": "AUD",
            "credit_terms_days": 21,
            "as_of_at": None,
        }
        preview = client.post(base + "/preview", json=selection)
        assert preview.status_code == 200, preview.text
        facts = preview.json()
        assert facts["synthetic"] is True
        assert facts["members"] and all(row["opening_minor"] is None for row in facts["members"])
        assert facts["result"]["status"] == "INCOMPLETE"
        assert facts["result"]["recovery_percent"] is None
        assert facts["result"]["opening_minor"] is None
        command = {
            "request_id": str(uuid4()),
            "selection": facts["selection"],
            "reviewed_hash": facts["reviewed_hash"],
            "evidence": "Owner reviewed incomplete original history without substituting current balances.",
        }
        saved = client.post(base, json=command)
        assert saved.status_code == 201, saved.text
        cohort = saved.json()
        assert cohort["execution_basis"] == "OBSERVED"
        assert cohort["synthetic"] is True
        assert cohort["versions"][0]["members"] == facts["members"]
        assert client.post(base, json=command).json()["id"] == cohort["id"]
        assert (
            client.post(
                base, json=command | {"evidence": "Changed captured command is forbidden."}
            ).status_code
            == 409
        )
        assert client.get(base).json()["cohorts"][0]["id"] == cohort["id"]


def test_complete_observed_source_preserves_synthetic_pair_and_versions_membership_explicitly() -> None:
    with actor_client("owner") as (client, _db, _user, tenant):
        connection = seed(client, tenant)
        base = f"/api/workspaces/{tenant}/impact/cohorts"
        illustrative = client.post(
            base + "/synthetic",
            json={
                "request_id": str(uuid4()),
                "evidence": "Explicit source history for separate observed execution.",
            },
        )
        assert illustrative.status_code == 200, illustrative.text
        original_pair = illustrative.json()["pairs"][0]
        baseline, after = original_pair["cohorts"]
        observed = base + "/observed"
        selection = {
            "connection_id": connection["provider_connection_id"],
            "period_start": baseline["period_start"],
            "period_end": baseline["period_end"],
            "currency": "AUD",
            "credit_terms_days": 30,
            "as_of_at": None,
        }
        preview = client.post(observed + "/preview", json=selection)
        assert preview.status_code == 200, preview.text
        facts = preview.json()
        assert facts["result"]["status"] == "COMPLETE"
        assert facts["result"]["recovery_percent"] == "60"
        assert facts["synthetic"] is True
        command = {
            "request_id": str(uuid4()),
            "selection": facts["selection"],
            "reviewed_hash": facts["reviewed_hash"],
            "evidence": "Freeze actual complete original source population with synthetic label.",
        }
        response = client.post(observed, json=command)
        assert response.status_code == 201, response.text
        cohort = response.json()
        first = cohort["versions"][0]
        assert first["results"][0]["recovery_percent"] == "60"
        assert first["results"][0]["label"] == "OBSERVED"
        assert client.get(base).json()["pairs"] == [original_pair]
        changed_selection = selection | {
            "period_start": after["period_start"],
            "period_end": after["period_end"],
        }
        review = {
            "selection": changed_selection,
            "expected_definition_id": first["id"],
            "expected_result_id": first["results"][-1]["id"],
            "mode": "RESTATE",
        }
        candidate = client.post(observed + f"/{cohort['id']}/preview", json=review)
        assert candidate.status_code == 200, candidate.text
        restated = candidate.json()
        change = {
            "request_id": str(uuid4()),
            "selection": restated["selection"],
            "reviewed_hash": restated["reviewed_hash"],
            "expected_definition_id": review["expected_definition_id"],
            "expected_result_id": review["expected_result_id"],
            "evidence": "Explicit restatement selects another source due-date period; no causal claim.",
        }
        updated = client.post(observed + f"/{cohort['id']}/restate", json=change)
        assert updated.status_code == 200, updated.text
        versions = updated.json()["versions"]
        assert len(versions) == 2 and versions[0] == first
        assert versions[1]["prior_definition_id"] == first["id"]
        assert versions[1]["results"][0]["label"] == "RESTATED"
        assert versions[1]["results"][0]["recovery_percent"] == "75"
        assert {row["invoice_id"] for row in versions[0]["members"]}.isdisjoint(
            row["invoice_id"] for row in versions[1]["members"]
        )
        assert client.post(observed + f"/{cohort['id']}/restate", json=change).json() == updated.json()
        assert (
            client.post(
                observed + f"/{cohort['id']}/restate", json=change | {"request_id": str(uuid4())}
            ).status_code
            == 409
        )
        assert client.get(base).json()["pairs"] == [original_pair]


def test_source_change_refresh_keeps_opening_and_due_maturity_without_reusing_stale_review() -> None:
    from uuid import UUID

    import pytest
    from accounting_support import publish_invoice
    from sqlalchemy import select, update
    from sqlalchemy.exc import DBAPIError

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.impact.models import InvoiceCohort

    with actor_client("owner") as (client, db, _user, tenant):
        connection = seed(client, tenant)
        base = f"/api/workspaces/{tenant}"
        seeded = client.post(
            base + "/impact/cohorts/synthetic",
            json={
                "request_id": str(uuid4()),
                "evidence": "Synthetic source history for observed refresh evidence.",
            },
        ).json()["pairs"][0]
        baseline = seeded["cohorts"][0]
        path = base + "/impact/cohorts/observed"
        selection = {
            "connection_id": connection["provider_connection_id"],
            "period_start": baseline["period_start"],
            "period_end": baseline["period_end"],
            "currency": "AUD",
            "credit_terms_days": 30,
            "as_of_at": None,
        }
        before = client.post(path + "/preview", json=selection).json()
        command = {
            "request_id": str(uuid4()),
            "selection": before["selection"],
            "reviewed_hash": before["reviewed_hash"],
            "evidence": "Explicit original complete source review before actual source mutation.",
        }
        paid = client.post(
            base + "/demo/accounting/simulate-payment",
            json={
                "provider_connection_id": connection["provider_connection_id"],
                "external_invoice_id": "COHORT-BASELINE_SYNTHETIC-4",
                "amount": {"minor_units": 10000, "currency": "AUD", "exponent": 2},
                "operation_id": str(uuid4()),
            },
        )
        assert paid.status_code == 200, paid.text
        assert client.post(path, json=command).status_code == 409
        fresh = client.post(path + "/preview", json=selection).json()
        saved = client.post(
            path,
            json=command
            | {
                "request_id": str(uuid4()),
                "selection": fresh["selection"],
                "reviewed_hash": fresh["reviewed_hash"],
            },
        )
        assert saved.status_code == 201, saved.text
        cohort = saved.json()
        first = cohort["versions"][0]
        assert first["results"][0]["recovery_percent"] == "60"
        assert first["results"][0]["paid_in_full_percent"] == "50"
        # A real synthetic source revision corrects today's due date; original denominator stays frozen.
        invoice = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant,
                InvoiceSnapshot.external_invoice_id == "COHORT-BASELINE_SYNTHETIC-4",
            )
        )
        assert invoice is not None
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            key=invoice.external_invoice_id,
            due_date=str(invoice.due_date + timedelta(days=5)),
        )
        review = {
            "selection": selection,
            "expected_definition_id": first["id"],
            "expected_result_id": first["results"][-1]["id"],
            "mode": "REFRESH",
        }
        preview = client.post(path + f"/{cohort['id']}/preview", json=review)
        assert preview.status_code == 200, preview.text
        changed = preview.json()
        assert any(member["source_change"] for member in changed["members"])
        refresh = {
            "request_id": str(uuid4()),
            "selection": changed["selection"],
            "reviewed_hash": changed["reviewed_hash"],
            "expected_definition_id": first["id"],
            "expected_result_id": first["results"][-1]["id"],
            "evidence": "Review actual current due correction; preserve original opening and membership.",
        }
        response = client.post(path + f"/{cohort['id']}/refresh", json=refresh)
        assert response.status_code == 200, response.text
        revised = response.json()["versions"][0]
        assert revised["members"] == first["members"]
        assert revised["results"][0] == first["results"][0]
        assert revised["results"][-1]["label"] == "RESTATED"
        assert revised["results"][-1]["opening_minor"] == "40000"
        assert revised["results"][-1]["recovery_percent"] == "60"
        assert any(member["source_change"] for member in revised["current_source_members"])
        assert client.post(path + f"/{cohort['id']}/refresh", json=refresh).json() == response.json()
        assert (
            client.post(
                path + f"/{cohort['id']}/refresh", json=refresh | {"request_id": str(uuid4())}
            ).status_code
            == 409
        )
        assert (
            client.post(
                path + "/preview", json=selection | {"as_of_at": (now() + timedelta(days=1)).isoformat()}
            ).status_code
            == 422
        )
        assert (
            client.post(path + "/preview", json=selection, headers={"x-csrf-token": "wrong"}).status_code
            == 403
        )
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                update(InvoiceCohort).where(InvoiceCohort.id == UUID(first["id"])).values(definition={})
            )


def test_viewer_and_foreign_workspace_cannot_publish_observed_financial_history() -> None:
    with actor_client("viewer") as (client, _db, _user, tenant):
        path = f"/api/workspaces/{tenant}/impact/cohorts/observed"
        assert client.get(path).json()["can_build"] is False
        command = {
            "connection_id": str(uuid4()),
            "period_start": "2026-01-01",
            "period_end": "2026-02-01",
            "currency": "AUD",
            "credit_terms_days": 30,
            "as_of_at": None,
        }
        assert client.post(path + "/preview", json=command).status_code == 403
        assert client.get(f"/api/workspaces/{uuid4()}/impact/cohorts/observed").status_code == 403


def test_late_source_member_requires_explicit_restatement_and_keeps_prior_complete_result() -> None:
    from copy import deepcopy
    from datetime import date
    from uuid import UUID

    from sqlalchemy import select

    from aco.features.accounting.models import DemoSourceRecord

    with actor_client("owner") as (client, db, _user, tenant):
        connection = seed(client, tenant)
        root = f"/api/workspaces/{tenant}/impact/cohorts"
        pair = client.post(
            root + "/synthetic",
            json={"request_id": str(uuid4()), "evidence": "Actual fixed synthetic source history."},
        ).json()["pairs"][0]
        baseline = pair["cohorts"][0]
        base = root + "/observed"
        selection = {
            "connection_id": connection["provider_connection_id"],
            "period_start": baseline["period_start"],
            "period_end": baseline["period_end"],
            "currency": "AUD",
            "credit_terms_days": 30,
            "as_of_at": None,
        }
        preview = client.post(base + "/preview", json=selection).json()
        frozen = client.post(
            base,
            json={
                "request_id": str(uuid4()),
                "selection": preview["selection"],
                "reviewed_hash": preview["reviewed_hash"],
                "evidence": "Freeze before late source arrival.",
            },
        ).json()
        original = frozen["versions"][0]
        assert original["results"][0]["recovery_percent"] == "60"
        source = db.scalar(
            select(DemoSourceRecord).where(
                DemoSourceRecord.tenant_id == tenant,
                DemoSourceRecord.provider_connection_id == UUID(connection["provider_connection_id"]),
                DemoSourceRecord.kind == "invoice",
                DemoSourceRecord.external_id == "COHORT-BASELINE_SYNTHETIC-4",
                DemoSourceRecord.revision == 1,
            )
        )
        assert source is not None
        payload = deepcopy(source.payload)
        payload.update(external_id="LATE-OBSERVED", number="LATE-OBSERVED")
        db.add(
            DemoSourceRecord(
                tenant_id=tenant,
                provider_connection_id=source.provider_connection_id,
                organisation_id=source.organisation_id,
                kind="invoice",
                external_id="LATE-OBSERVED",
                revision=1,
                payload=payload,
                effective_at=source.effective_at,
                observed_at=now(),
            )
        )
        db.commit()
        change_path = base + f"/{frozen['id']}"
        review = {
            "selection": selection,
            "expected_definition_id": original["id"],
            "expected_result_id": original["results"][0]["id"],
            "mode": "REFRESH",
        }
        refresh = client.post(change_path + "/preview", json=review)
        assert refresh.status_code == 200, refresh.text
        facts = refresh.json()
        assert facts["result"]["recovery_percent"] == "60"
        assert len(facts["members"]) == 4
        assert any("New source members" in warning for warning in facts["warnings"])
        restatement = client.post(change_path + "/preview", json=review | {"mode": "RESTATE"})
        assert restatement.status_code == 200, restatement.text
        new = restatement.json()
        assert len(new["members"]) == 5 and new["result"]["status"] == "INCOMPLETE"
        assert new["result"]["recovery_percent"] is None
        assert sum(row["opening_minor"] is None for row in new["members"]) == 1
        response = client.post(
            change_path + "/restate",
            json={
                "request_id": str(uuid4()),
                "selection": new["selection"],
                "reviewed_hash": new["reviewed_hash"],
                "expected_definition_id": original["id"],
                "expected_result_id": original["results"][0]["id"],
                "evidence": "Include newly arrived source, retaining its missing opening evidence.",
            },
        )
        assert response.status_code == 200, response.text
        versions = response.json()["versions"]
        assert versions[0] == original
        assert versions[1]["prior_definition_id"] == original["id"]
        assert versions[1]["results"][0]["recovery_percent"] is None
        # A source member not yet mature at the reviewed as-of never becomes a zero outcome.
        early = date.fromisoformat(baseline["period_start"]) + timedelta(days=2)
        immature = client.post(
            base + "/preview", json=selection | {"as_of_at": early.isoformat() + "T00:00:00Z"}
        )
        assert immature.status_code == 200, immature.text
        assert immature.json()["result"]["recovery_percent"] is None


def test_provider_missing_original_member_retains_frozen_identity_and_unknown_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from aco.core.context import TenantContext
    from aco.features.accounting.dto import AccountingResult
    from aco.integrations.demo_accounting.observed_history import DemoObservedHistoryAdapter
    from aco.integrations.ports.accounting_history import CohortSourceHistory

    with actor_client("owner") as (client, _db, _user, tenant):
        connection = seed(client, tenant)
        root = f"/api/workspaces/{tenant}/impact/cohorts"
        pair = client.post(
            root + "/synthetic",
            json={"request_id": str(uuid4()), "evidence": "Explicit synthetic complete-history source."},
        ).json()["pairs"][0]
        baseline = pair["cohorts"][0]
        base = root + "/observed"
        selection = {
            "connection_id": connection["provider_connection_id"],
            "period_start": baseline["period_start"],
            "period_end": baseline["period_end"],
            "currency": "AUD",
            "credit_terms_days": 30,
            "as_of_at": None,
        }
        preview = client.post(base + "/preview", json=selection).json()
        saved = client.post(
            base,
            json={
                "request_id": str(uuid4()),
                "selection": preview["selection"],
                "reviewed_hash": preview["reviewed_hash"],
                "evidence": "Freeze before synthetic provider missing-member response.",
            },
        ).json()
        definition = saved["versions"][0]
        actual_read = DemoObservedHistoryAdapter._observed

        async def missing(
            self: DemoObservedHistoryAdapter, ctx: TenantContext, at: datetime | None
        ) -> AccountingResult[CohortSourceHistory]:
            result = await actual_read(self, ctx, at)
            # Explicit offline provider substitution: actual original rows are retained; this
            # response simulates source disappearance rather than deleting the private ledger.
            return result.model_copy(
                update={
                    "value": result.value.model_copy(
                        update={
                            "invoices": [
                                row
                                for row in result.value.invoices
                                if row.external_invoice_id != "COHORT-BASELINE_SYNTHETIC-4"
                            ],
                            "source_watermark": "missing-member-" + result.value.source_watermark,
                        }
                    )
                }
            )

        monkeypatch.setattr(DemoObservedHistoryAdapter, "_observed", missing)
        path = base + f"/{saved['id']}"
        response = client.post(
            path + "/preview",
            json={
                "selection": selection,
                "expected_definition_id": definition["id"],
                "expected_result_id": definition["results"][0]["id"],
                "mode": "REFRESH",
            },
        )
        assert response.status_code == 200, response.text
        facts = response.json()
        assert len(facts["members"]) == 4
        assert facts["result"]["recovery_percent"] is None
        missing_views = [
            row for row in facts["members"] if row["source_change"] and "missing" in row["source_change"]
        ]
        assert len(missing_views) == 1
        assert missing_views[0]["eligibility"] == "INCOMPLETE"
        response = client.post(
            path + "/refresh",
            json={
                "request_id": str(uuid4()),
                "selection": facts["selection"],
                "reviewed_hash": facts["reviewed_hash"],
                "expected_definition_id": definition["id"],
                "expected_result_id": definition["results"][0]["id"],
                "evidence": "Retain missing original member; do not shrink the frozen denominator.",
            },
        )
        assert response.status_code == 200, response.text
        current = response.json()["versions"][0]
        assert current["members"] == definition["members"]
        assert current["results"][0] == definition["results"][0]
        assert current["results"][-1]["status"] == "INCOMPLETE"
