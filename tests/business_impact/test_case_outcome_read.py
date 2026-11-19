"""Actual human service lifecycle feeds historical/current outcome reads."""

from datetime import timedelta
from decimal import Decimal

from accounting_support import context
from case_promise_support import communication, invoices
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select

from aco.features.cases.dto import (
    CommunicationEvidence,
    InvoiceIssueTransition,
    LinkCaseEvidence,
    TransitionCase,
    UpsertCase,
)
from aco.features.cases.service import CaseService
from aco.features.identity.models import Membership, now
from aco.features.impact.case_outcome_api import router


def test_real_resolution_reopen_and_asof_history_with_current_permission() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        rows = invoices(client, db, tenant)[:2]
        text = "Synthetic fixture: invoice dispute and documentary resolution."
        msg = communication(db, tenant, rows, text)
        evidence = CommunicationEvidence(communication_id=msg.id, excerpt=text, offset=0)
        ctx = context(tenant, user.id)
        svc = CaseService(db)
        case = svc.upsert_case(
            ctx,
            UpsertCase(
                customer_id=rows[0].customer_id,
                issue_type="wrong_amount",
                claim_key="rates",
                substance="Documented rate dispute",
                invoice_ids=[r.id for r in rows],
                evidence=evidence,
            ),
        ).case
        case = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                expected_version=case.version,
                status="INVESTIGATING",
                reason="Review source evidence",
            ),
        )
        case = svc.set_invoice_issue_state(
            ctx,
            InvoiceIssueTransition(
                case_id=case.id,
                invoice_id=rows[0].id,
                expected_version=case.version,
                evidence=evidence,
                resolution_kind="documentary_confirmation",
                resolution_action="Documented first issue resolution",
            ),
        )
        db.commit()
        path = f"/api/workspaces/{tenant}/impact/case-outcomes"
        params = {
            "since": (case.opened_at - timedelta(seconds=1)).isoformat(),
            "until": (case.opened_at + timedelta(days=1)).isoformat(),
        }
        partial = client.get(path, params=params)
        assert partial.status_code == 200, partial.text
        assert partial.json()["resolved_count"] == 0 and partial.json()["open_backlog_count"] == 1
        case = svc.set_invoice_issue_state(
            ctx,
            InvoiceIssueTransition(
                case_id=case.id,
                invoice_id=rows[1].id,
                expected_version=case.version,
                evidence=evidence,
                resolution_kind="documentary_confirmation",
                resolution_action="Documented second issue resolution",
            ),
        )
        case = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                expected_version=case.version,
                status="RESOLVED",
                reason="Every issue has verified disposition",
            ),
        )
        db.commit()
        resolved_at = now()
        resolved = client.get(path, params=params | {"as_of": resolved_at.isoformat()})
        assert resolved.status_code == 200, resolved.text
        data = resolved.json()
        assert data["resolved_count"] == 1 and data["autonomous_resolved_count"] == 0
        assert Decimal(data["human_intervention_percent"]) == 100
        assert data["reported_human_minutes"] is None and data["case_approval_count"] is None
        assert case.resolved_at is not None
        assert Decimal(data["mean_resolution_seconds"]) == Decimal(
            str((case.resolved_at - case.opened_at).total_seconds())
        )
        later = communication(db, tenant, rows[:1], "Synthetic contradictory first-invoice evidence.")
        svc.link_case_evidence(
            ctx,
            LinkCaseEvidence(
                case_id=case.id,
                expected_version=case.version,
                evidence=CommunicationEvidence(
                    communication_id=later.id,
                    excerpt="Synthetic contradictory first-invoice evidence.",
                    offset=0,
                ),
            ),
        )
        db.commit()
        current = client.get(path, params=params).json()
        assert current["resolved_count"] == 0 and current["open_backlog_count"] == 1
        assert current["cases"][0]["opened_at"] == data["cases"][0]["opened_at"]
        historical = client.get(path, params=params | {"as_of": resolved_at.isoformat()}).json()
        assert historical["cases"] == data["cases"]
        assert (
            client.get(path, params=params | {"until": case.opened_at.isoformat()}).json()["eligible_count"]
            == 0
        )
        assert (
            client.get(path, params=params | {"as_of": (now() + timedelta(days=1)).isoformat()}).status_code
            == 422
        )
        assert client.get(path, params=params | {"since": "2026-01-01T00:00:00"}).status_code == 422
        # Resolve the reopened invoice through its actual authorized service again.
        reopened = svc.get(ctx, case.id)
        reopened = svc.set_invoice_issue_state(
            ctx,
            InvoiceIssueTransition(
                case_id=case.id,
                invoice_id=rows[0].id,
                expected_version=reopened.version,
                evidence=CommunicationEvidence(
                    communication_id=later.id,
                    excerpt="Synthetic contradictory first-invoice evidence.",
                    offset=0,
                ),
                resolution_kind="documentary_confirmation",
                resolution_action="Reviewed renewed source disposition",
            ),
        )
        latest = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                expected_version=reopened.version,
                status="RESOLVED",
                reason="Renewed issue now resolved",
            ),
        )
        db.commit()
        after = client.get(path, params=params).json()
        assert after["resolved_count"] == 1 and after["cases"][0]["resolved_episode_count"] == 2
        assert latest.resolved_at is not None
        assert Decimal(after["mean_resolution_seconds"]) == Decimal(
            str((latest.resolved_at - case.opened_at).total_seconds())
        )
        assert after["autonomous_resolved_count"] == 0
        assert (
            client.get(path, params=params | {"as_of": resolved_at.isoformat()}).json()["cases"]
            == data["cases"]
        )
        admin = svc.upsert_case(
            ctx,
            UpsertCase(
                customer_id=rows[0].customer_id,
                issue_type="wrong_amount",
                claim_key="withdrawn-dispute",
                substance="Separate withdrawn rate claim",
                invoice_ids=[rows[0].id],
                evidence=evidence,
            ),
        ).case
        svc.transition_case(
            ctx,
            TransitionCase(
                case_id=admin.id,
                expected_version=admin.version,
                status="CLOSED",
                reason="Source-backed authorized withdrawal",
                closure_reason="withdrawn",
                evidence=evidence,
            ),
        )
        db.commit()
        withdrawn = client.get(path, params=params).json()
        assert withdrawn["eligible_count"] == 2 and withdrawn["resolved_count"] == 1
        assert withdrawn["administrative_closed_count"] == 1 and withdrawn["open_backlog_count"] == 0
        membership = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert membership is not None
        membership.active = False
        db.commit()
        assert client.get(path, params=params).status_code == 403
