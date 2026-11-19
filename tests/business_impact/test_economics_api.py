"""Real current-human assumptions, canonical source allocation and partial economics."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from accounting_support import context
from case_promise_support import communication, invoices
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aco.core.types import CostQuality, ProviderCost
from aco.features.cases.dto import CommunicationEvidence, UpsertCase
from aco.features.cases.service import CaseService
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.impact.economics_api import router
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.models import UsageSource
from aco.features.usage.service import UsageService


def test_owner_draft_review_hash_current_version_replay_and_revocation() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        instant = now()
        base = f"/api/workspaces/{tenant}/impact/economics"
        command = {
            "request_id": str(uuid4()),
            "expected_previous_version_id": None,
            "since": (instant - timedelta(days=1)).isoformat(),
            "until": (instant + timedelta(days=1)).isoformat(),
            "currency": "AUD",
            "synthetic": True,
            "baseline_minutes_per_issue": "10",
            "wage_per_hour": "60",
            "fixed_cost": "20",
            "fixed_cost_evidence": "Explicit illustrative period infrastructure allocation, not a bill.",
            "evidence": "Owner proposed synthetic baseline and wage for an estimate only.",
        }
        assert (
            client.post(base + "/assumptions", json=command, headers={"x-csrf-token": "bad"}).status_code
            == 403
        )
        response = client.post(base + "/assumptions", json=command)
        assert response.status_code == 201, response.text
        draft = response.json()
        assert draft["approval_id"] is None
        assert client.post(base + "/assumptions", json=command).json()["id"] == draft["id"]
        assert client.post(base + "/assumptions", json=command | {"wage_per_hour": "61"}).status_code == 409
        path = base + f"/assumptions/{draft['id']}/approve"
        approval = {
            "request_id": str(uuid4()),
            "expected_current_approval_id": None,
            "reviewed_hash": draft["reviewed_hash"],
            "evidence": "Owner reviewed exact displayed assumption values and reporting window.",
        }
        assert client.post(path, json=approval | {"reviewed_hash": "0" * 64}).status_code == 409
        approved = client.post(path, json=approval)
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_by"] == str(user.id)
        assert client.post(path, json=approval).json()["approval_id"] == approved.json()["approval_id"]
        read = client.get(base, params={"since": command["since"], "until": command["until"]})
        assert read.status_code == 200, read.text
        assert read.json()["current_approval_id"] == approved.json()["approval_id"]
        assert read.json()["groups"][0]["capacity"]["estimated_roi_percent"] is None
        newer = client.post(
            base + "/assumptions",
            json=command
            | {
                "request_id": str(uuid4()),
                "expected_previous_version_id": draft["id"],
                "wage_per_hour": "65",
            },
        ).json()
        assert newer["version"] == 2 and newer["approval_id"] is None
        assert (
            client.post(
                base + f"/assumptions/{newer['id']}/approve",
                json=approval | {"request_id": str(uuid4()), "reviewed_hash": newer["reviewed_hash"]},
            ).status_code
            == 409
        )
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert member is not None
        member.role = "viewer"
        db.commit()
        assert client.post(path, json=approval).status_code == 403
        assert (
            client.get(base, params={"since": command["since"], "until": command["until"]}).json()[
                "can_manage"
            ]
            is False
        )
        db.rollback()
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(text("DELETE FROM economics_assumptions WHERE id=:id"), {"id": draft["id"]})


def test_verified_shared_source_has_one_allocation_set_and_unknown_stays_partial() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        rows = invoices(client, db, tenant)[:3]
        body = "Synthetic source fixture for explicit common activity attribution."
        msg = communication(db, tenant, rows, body)
        ctx = context(tenant, user.id)
        case = (
            CaseService(db)
            .upsert_case(
                ctx,
                UpsertCase(
                    customer_id=rows[0].customer_id,
                    issue_type="wrong_amount",
                    claim_key="allocation",
                    substance="Actual shared source review",
                    invoice_ids=[r.id for r in rows[:2]],
                    evidence=CommunicationEvidence(communication_id=msg.id, excerpt=body, offset=0),
                ),
            )
            .case
        )
        connection = db.get(ProviderConnection, msg.provider_connection_id)
        assert connection is not None
        event = UsageService(db).record_usage(
            ctx,
            ProviderUsageInput(
                provider_connection_id=connection.id,
                operation_key=str(uuid4()),
                component="voice_total",
                operation="fixture_observation",
                provider_kind=connection.provider_type,
                provider=connection.provider,
                synthetic=True,
                cost=ProviderCost(
                    amount=Decimal("0.000000000003"), currency="AUD", quality=CostQuality.PROVIDER_REPORTED
                ),
            ),
        )
        db.add(
            UsageSource(
                event_id=event.id, source_kind="communication", source_id=msg.id, component=event.component
            )
        )
        db.commit()
        base = f"/api/workspaces/{tenant}/impact/economics"
        path = base + f"/usage/{event.id}/allocation"
        preview = client.get(
            base,
            params={
                "since": (event.created_at - timedelta(seconds=1)).isoformat(),
                "until": (now() + timedelta(days=1)).isoformat(),
            },
        ).json()
        selected_event = next(item for item in preview["events"] if item["event_id"] == str(event.id))
        command = {
            "request_id": str(uuid4()),
            "expected_revision_id": selected_event["revision_id"],
            "reviewed_source_hash": selected_event["allocation"]["source_hash"],
            "expected_version_id": None,
            "leaves": None,
            "evidence": "Owner reviewed genuine original communication and its complete invoice/case leaves.",
        }
        assert client.post(path, json=command | {"reviewed_source_hash": "0" * 64}).status_code == 409
        assert client.post(path, json=command | {"expected_revision_id": str(uuid4())}).status_code == 409
        response = client.post(path, json=command)
        assert response.status_code == 201, response.text
        allocation = response.json()
        assert len(allocation["leaves"]) == 3
        assert sum(Decimal(leaf["weight"]) for leaf in allocation["leaves"]) == 1
        assert sum(leaf["case_id"] == str(case.id) for leaf in allocation["leaves"]) == 2
        assert client.post(path, json=command).json()["id"] == allocation["id"]
        assert client.post(path, json=command | {"request_id": str(uuid4())}).status_code == 409
        changed = allocation["leaves"]
        changed[0]["invoice_id"] = str(uuid4())
        assert (
            client.post(
                path,
                json=command
                | {"request_id": str(uuid4()), "expected_version_id": allocation["id"], "leaves": changed},
            ).status_code
            == 403
        )
        result = client.get(
            base,
            params={
                "since": (event.created_at - timedelta(seconds=1)).isoformat(),
                "until": (now() + timedelta(days=1)).isoformat(),
            },
        )
        assert result.status_code == 200, result.text
        facts = result.json()
        matching = [item for item in facts["events"] if item["event_id"] == str(event.id)]
        assert len(matching) == 1 and matching[0]["revision_id"] is not None
        assert Decimal(matching[0]["amount"]) == Decimal("0.000000000003")
        assert matching[0]["allocation"]["id"] == allocation["id"]
        assert all(group["costs"]["complete_cost_per_resolution"] is None for group in facts["groups"])
        assert all(group["capacity"]["estimated_roi_percent"] is None for group in facts["groups"])
        db.rollback()
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("UPDATE cost_allocation_versions SET leaves='[]' WHERE id=:id"), {"id": allocation["id"]}
            )


def test_actual_resolved_denominator_reported_capacity_and_negative_estimate() -> None:
    from aco.features.cases.dto import InvoiceIssueTransition, TransitionCase
    from aco.features.impact.handling_api import router as handling_router

    with actor_client("owner") as (client, db, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        app.include_router(handling_router)
        rows = invoices(client, db, tenant)[:1]
        start = now()
        msg = communication(
            db, tenant, rows, "Synthetic fixture: source confirms the documented invoice issue was corrected."
        )
        source_text = msg.content["text_body"]
        assert isinstance(source_text, str)
        evidence = CommunicationEvidence(communication_id=msg.id, excerpt=source_text, offset=0)
        ctx = context(tenant, user.id)
        service = CaseService(db)
        case = service.upsert_case(
            ctx,
            UpsertCase(
                customer_id=rows[0].customer_id,
                issue_type="wrong_amount",
                claim_key="economics-resolved",
                substance="Synthetic documented invoice issue",
                invoice_ids=[rows[0].id],
                evidence=evidence,
            ),
        ).case
        case = service.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                expected_version=case.version,
                status="INVESTIGATING",
                reason="Review the actual synthetic source before issue disposition",
            ),
        )
        case = service.set_invoice_issue_state(
            ctx,
            InvoiceIssueTransition(
                case_id=case.id,
                invoice_id=rows[0].id,
                expected_version=case.version,
                evidence=evidence,
                resolution_kind="documentary_confirmation",
                resolution_action="Documented issue resolution from retained source",
            ),
        )
        case = service.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                expected_version=case.version,
                status="RESOLVED",
                reason="All actual scoped issues have documentary resolution",
            ),
        )
        connection = db.get(ProviderConnection, msg.provider_connection_id)
        assert connection is not None
        event = UsageService(db).record_usage(
            ctx,
            ProviderUsageInput(
                provider_connection_id=connection.id,
                operation_key=str(uuid4()),
                component="fixture_email",
                operation="fixture_observation",
                provider_kind=connection.provider_type,
                provider=connection.provider,
                synthetic=True,
                cost=ProviderCost(amount=Decimal(10), currency="AUD", quality=CostQuality.PROVIDER_REPORTED),
            ),
        )
        db.add(
            UsageSource(
                event_id=event.id, source_kind="communication", source_id=msg.id, component=event.component
            )
        )
        db.commit()
        handling = f"/api/workspaces/{tenant}/cases/{case.id}/handling-time"
        reports = client.get(handling).json()
        for history_id in reports["can_record_history_ids"]:
            result = client.post(
                handling,
                json={
                    "request_id": str(uuid4()),
                    "history_id": history_id,
                    "expected_case_version": case.version,
                    "minutes": "5",
                    "evidence": "Operator reports five active minutes for this distinct source-backed event.",
                },
            )
            assert result.status_code == 201, result.text
        base = f"/api/workspaces/{tenant}/impact/economics"
        window = {"since": start.isoformat(), "until": (start + timedelta(days=1)).isoformat()}
        draft = client.post(
            base + "/assumptions",
            json={
                "request_id": str(uuid4()),
                "expected_previous_version_id": None,
                **window,
                "currency": "AUD",
                "synthetic": True,
                "baseline_minutes_per_issue": "1",
                "wage_per_hour": "60",
                "fixed_cost": "20",
                "fixed_cost_evidence": "Explicit synthetic period allocation reviewed by Owner.",
                "evidence": "Synthetic assumed one-minute baseline, not customer measured history.",
            },
        ).json()
        approved = client.post(
            base + f"/assumptions/{draft['id']}/approve",
            json={
                "request_id": str(uuid4()),
                "expected_current_approval_id": None,
                "reviewed_hash": draft["reviewed_hash"],
                "evidence": "Owner reviewed saved values, period and explicit synthetic estimate label.",
            },
        )
        assert approved.status_code == 200, approved.text
        response = client.get(base, params=window)
        assert response.status_code == 200, response.text
        group = next(
            item for item in response.json()["groups"] if item["currency"] == "AUD" and item["synthetic"]
        )
        assert group["eligible_case_count"] == 1 and group["resolved_case_count"] == 1
        assert group["human_time_basis"] == "MANUAL_REPORTED" and group["measured_human_cost"] is None
        assert Decimal(group["reported_human_minutes"]) == len(reports["can_record_history_ids"]) * 5
        assert Decimal(group["capacity"]["estimated_time_saved_minutes"]) < 0
        assert Decimal(group["capacity"]["estimated_net_benefit"]) < 0
        assert group["capacity"]["realized_payroll_savings_claim"] is False
        assert group["capacity"]["estimated_roi_percent"] is None
        assert group["capacity"]["label"] == "Insufficient data"
        assert Decimal(group["estimated_cost_per_resolution"]) == Decimal(30) + Decimal(
            group["reported_human_minutes"]
        )
        assert group["costs"]["complete_cost_per_resolution"] is None
        assert sum(Decimal(item["known_amount"]) for item in response.json()["case_costs"]) == 10
        assert sum(Decimal(item["known_amount"]) for item in response.json()["invoice_costs"]) == 10
        # A returned fee bundle shares one original parent; unknown child remains visible.
        parent = ProviderUsageInput(
            provider_connection_id=connection.id,
            operation_key=event.operation_key,
            component=event.component,
            operation=event.operation,
            provider_kind=connection.provider_type,
            provider=connection.provider,
            synthetic=True,
            cost=ProviderCost(amount=Decimal(10), currency="AUD", quality=CostQuality.PROVIDER_REPORTED),
            bundle="synthetic_bundle",
            reconciliation_key="fixture_bundle_revision",
        )
        UsageService(db).record_usage(ctx, parent)
        UsageService(db).record_usage(
            ctx,
            parent.model_copy(
                update={
                    "component": "contained_fixture_fee",
                    "included_in": event.component,
                    "reconciliation_key": "initial",
                    "cost": ProviderCost(amount=None, currency="AUD", quality=CostQuality.UNKNOWN),
                }
            ),
        )
        db.commit()
        bundled = client.get(base, params=window).json()
        aud = next(item for item in bundled["groups"] if item["currency"] == "AUD" and item["synthetic"])
        assert Decimal(aud["known_provider_cost"]) == 10
        assert aud["provider_basis_complete"] is False
        assert aud["capacity"]["estimated_roi_percent"] is None
        assert any(item["amount"] is None and item["additive"] is False for item in bundled["events"])
        assert sum(Decimal(item["known_amount"]) for item in bundled["case_costs"]) == 10
        assert sum(Decimal(item["known_amount"]) for item in bundled["invoice_costs"]) == 10
        UsageService(db).record_usage(
            ctx,
            parent.model_copy(
                update={
                    "operation_key": str(uuid4()),
                    "bundle": None,
                    "reconciliation_key": "initial",
                    "cost": ProviderCost(
                        amount=Decimal(2), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        db.commit()
        mixed = client.get(base, params=window).json()
        assert {item["currency"] for item in mixed["groups"]} == {"AUD", "USD"}
        assert all(item["capacity"]["estimated_roi_percent"] is None for item in mixed["groups"])
        assert any(
            "Other provider currencies" in reason for item in mixed["groups"] for reason in item["reasons"]
        )
        from aco.features.cases.dto import LinkCaseEvidence

        new_body = "Synthetic later contradictory evidence reopens the original recorded issue."
        later = communication(db, tenant, rows, new_body)
        service.link_case_evidence(
            ctx,
            LinkCaseEvidence(
                case_id=case.id,
                expected_version=case.version,
                evidence=CommunicationEvidence(communication_id=later.id, excerpt=new_body, offset=0),
            ),
        )
        db.commit()
        reopened = client.get(base, params=window).json()
        assert all(
            item["resolved_case_count"] == 0 and item["estimated_cost_per_resolution"] is None
            for item in reopened["groups"]
        )
