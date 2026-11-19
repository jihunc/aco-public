"""Real PostgreSQL case semantics; synthetic source fixtures, no live ingress."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from accounting_support import apply, context, publish_invoice
from boundary_evidence import BoundaryObserver
from case_promise_support import communication, invoices
from fastapi import HTTPException
from identity_support import actor_client
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.dto import UpsertCase
from aco.features.communications.models import Communication


def command(rows: list[InvoiceSnapshot], msg: Communication, **kw: object) -> UpsertCase:
    from aco.features.cases.dto import CommunicationEvidence, UpsertCase

    body = msg.content["text_body"]
    assert isinstance(body, str)
    data: dict[str, object] = dict(
        customer_id=rows[0].customer_id,
        issue_type="wrong_amount",
        claim_key="invoice-rate-contract",
        substance="Invoice rate differs from agreed contract",
        invoice_ids=[r.id for r in rows],
        evidence=CommunicationEvidence(communication_id=msg.id, excerpt=body, offset=0),
    )
    return UpsertCase.model_validate(data | kw)


def test_repeat_issue_across_threads_appends_evidence_once() -> None:
    from aco.features.cases.models import CaseEvidence, CollectionCase
    from aco.features.cases.service import CaseService
    from aco.features.review.models import CollectionTask

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:2]
        first = communication(db, tenant, rows, "Synthetic fixture: invoice rates are wrong.")
        second = communication(db, tenant, rows[::-1], "Synthetic fixture: same rate issue, expected AUD 90.")
        svc = CaseService(db)
        ctx = context(tenant, user.id)
        a = svc.upsert_case(ctx, command(rows, first))
        db.commit()
        b = svc.upsert_case(ctx, command(rows, second))
        db.commit()
        replay = svc.upsert_case(ctx, command(rows, second))
        db.commit()
        assert a.case.id == b.case.id == replay.case.id
        assert b.case.version == replay.case.version
        assert db.scalar(select(func.count()).select_from(CollectionCase)) == 1
        assert db.scalar(select(func.count()).select_from(CaseEvidence)) == 2
        assert db.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert len(b.case.invoice_links) == 2 and len(b.case.evidence) == 2


def test_distinct_issues_on_one_invoice_remain_distinct() -> None:
    from aco.features.cases.service import CaseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic fixture: PO and amount issues.")
        svc = CaseService(db)
        ctx = context(tenant, user.id)
        a = svc.upsert_case(ctx, command(rows, msg))
        b = svc.upsert_case(
            ctx,
            command(rows, msg, claim_key="different-contract", substance="Second distinct rate discrepancy"),
        )
        assert a.case.id != b.case.id


def test_ambiguous_match_requires_review_and_foreign_evidence_refused() -> None:
    from aco.features.cases.service import CaseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic fixture dispute.")
        svc = CaseService(db)
        ctx = context(tenant, user.id)
        a = svc.upsert_case(ctx, command(rows, msg))
        db.commit()
        ambiguous = svc.upsert_case(ctx, command(rows, msg, matching="ambiguous", matched_case_id=a.case.id))
        assert ambiguous.review_required
        with pytest.raises(HTTPException):
            svc.upsert_case(ctx, command(rows, msg, customer_id=uuid4()))
        with pytest.raises(HTTPException):
            svc.upsert_case(
                ctx,
                command(
                    rows,
                    msg,
                    evidence=command(rows, msg).evidence.model_copy(
                        update={"excerpt": "invented customer statement"}
                    ),
                ),
            )


@pytest.mark.parametrize(
    "fixture_id", ["case_payment_does_not_resolve_dispute", "case_multi_invoice_partial_resolution"]
)
def test_boundary_case_issue_resolution(fixture_id: str, boundary_observer: BoundaryObserver) -> None:
    from aco.features.accounting.models import CollectionRecord
    from aco.features.cases.dto import InvoiceIssueTransition, TransitionCase
    from aco.features.cases.service import CaseService

    cases = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    fixture = next(c for c in cases if c["id"] == fixture_id)
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:2]
        msg = communication(
            db,
            tenant,
            rows,
            "Synthetic fixture: disputed invoice rates. "
            "Authorized disposition confirms first issue corrected.",
        )
        ctx = context(tenant, user.id)
        svc = CaseService(db)
        case = svc.upsert_case(ctx, command(rows, msg)).case
        case = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                status="INVESTIGATING",
                expected_version=case.version,
                reason="Investigating source evidence",
            ),
        )
        db.commit()
        if fixture_id == "case_payment_does_not_resolve_dispute":
            event = publish_invoice(
                db,
                tenant,
                rows[0].provider_connection_id,
                rows[0].external_invoice_id,
                outstanding={"minor_units": 0, "currency": "AUD", "exponent": 2},
                status="paid",
            )
            apply(client.app, ctx, event)
            db.expire_all()
            collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == rows[0].id))
            assert collection is not None
            assert collection.lifecycle == fixture["expected"]["collection_lifecycle"]
            assert (svc.get(ctx, case.id).status == "RESOLVED") is fixture["expected"]["case_resolved"]
            boundary_observer.assert_observed(
                fixture_id,
                {
                    "collection_lifecycle": collection.lifecycle,
                    "case_resolved": svc.get(ctx, case.id).status == "RESOLVED",
                },
            )
        else:
            case = svc.set_invoice_issue_state(
                ctx,
                InvoiceIssueTransition(
                    case_id=case.id,
                    invoice_id=rows[0].id,
                    expected_version=case.version,
                    evidence=command(rows, msg).evidence,
                    resolution_kind="documentary_confirmation",
                    resolution_action="Owner verified documented issue disposition",
                ),
            )
            assert (case.status == "RESOLVED") is fixture["expected"]["case_resolved"]
            assert [link.invoice_id for link in case.invoice_links if link.issue_state == "RESOLVED"] == [
                rows[0].id
            ]
            aliases = {
                row.id: link["invoice"] for row, link in zip(rows, fixture["input"]["links"], strict=True)
            }
            boundary_observer.assert_observed(
                fixture_id,
                {
                    "case_resolved": case.status == "RESOLVED",
                    "resolved_link_ids": [
                        aliases[link.invoice_id]
                        for link in case.invoice_links
                        if link.issue_state == "RESOLVED"
                    ],
                },
            )
            with pytest.raises(HTTPException):
                svc.transition_case(
                    ctx,
                    TransitionCase(
                        case_id=case.id,
                        status="RESOLVED",
                        expected_version=case.version,
                        reason="Cannot resolve second issue",
                    ),
                )


def test_http_case_current_membership_and_csrf() -> None:
    from aco.features.identity.models import Membership

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic dispute fixture.")
        path = f"/api/workspaces/{tenant}/cases"
        payload = command(rows, msg).model_dump(mode="json")
        assert client.post(path, json=payload, headers={"x-csrf-token": "invalid"}).status_code == 403
        created = client.post(path, json=payload)
        assert created.status_code == 201, created.text
        assert (
            client.get(path, params={"invoice_id": str(rows[0].id)}).json()["items"][0]["id"]
            == created.json()["case"]["id"]
        )
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert member is not None
        member.role = "viewer"
        db.commit()
        assert client.post(path, json=payload).status_code == 403
        assert client.get(path).status_code == 200


def test_reopen_with_new_evidence_restores_issue_hold_and_keeps_original_clock() -> None:
    from aco.features.accounting.models import CollectionRecord
    from aco.features.cases.dto import InvoiceIssueTransition, LinkCaseEvidence, TransitionCase
    from aco.features.cases.service import CaseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        first = communication(db, tenant, rows, "Synthetic dispute and authorized written disposition.")
        svc = CaseService(db)
        ctx = context(tenant, user.id)
        case = svc.upsert_case(ctx, command(rows, first)).case
        opened_at = case.opened_at
        case = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id, status="INVESTIGATING", expected_version=case.version, reason="Investigate"
            ),
        )
        case = svc.set_invoice_issue_state(
            ctx,
            InvoiceIssueTransition(
                case_id=case.id,
                invoice_id=rows[0].id,
                expected_version=case.version,
                evidence=command(rows, first).evidence,
                resolution_kind="documentary_confirmation",
                resolution_action="Owner confirmed documented disposition",
            ),
        )
        case = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                status="RESOLVED",
                expected_version=case.version,
                reason="All issue evidence verified",
            ),
        )
        case = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id,
                status="CLOSED",
                expected_version=case.version,
                closure_reason="resolved",
                reason="Resolved",
            ),
        )
        db.commit()
        later = communication(
            db, tenant, rows, "Synthetic new contradictory evidence: correction remains wrong."
        )
        svc.link_case_evidence(
            ctx,
            LinkCaseEvidence(
                case_id=case.id, expected_version=case.version, evidence=command(rows, later).evidence
            ),
        )
        db.commit()
        current = svc.get(ctx, case.id)
        assert current.status == "INVESTIGATING" and current.opened_at == opened_at
        assert current.resolved_at is None and current.closed_at is None
        assert current.invoice_links[0].issue_state == "OPEN"
        collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == rows[0].id))
        assert collection is not None and "active_dispute" in collection.hold_reasons
        assert len([e for e in current.history if e.event == "case.invoice_issue_resolved"]) == 1
        assert any(e.event == "case.reopened" for e in current.history)


def test_concurrent_semantic_upsert_is_safe() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.orm import Session

    from aco.features.cases.models import CaseEvidence, CollectionCase
    from aco.features.cases.service import CaseService
    from aco.features.review.models import CollectionTask

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        first = communication(db, tenant, rows, "Synthetic first dispute.")
        second = communication(db, tenant, rows, "Synthetic repeated dispute.")
        commands = [command(rows, first), command(rows, second)]
        ctx = context(tenant, user.id)
        db.commit()

        def run(cmd: UpsertCase) -> UUID:
            with Session(db.bind) as own:
                result = CaseService(own).upsert_case(ctx, cmd)
                own.commit()
                entity_id = result.case.id
                assert isinstance(entity_id, UUID)
                return entity_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, commands))
        assert results[0] == results[1]
        assert db.scalar(select(func.count()).select_from(CollectionCase)) == 1
        assert db.scalar(select(func.count()).select_from(CaseEvidence)) == 2
        assert db.scalar(select(func.count()).select_from(CollectionTask)) == 1


def test_foreign_case_invoice_and_generic_worker_context_are_refused() -> None:
    from aco.features.cases.service import CaseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic scoped dispute.")
        cmd = command(rows, msg)
        ctx = context(tenant, user.id)
        with pytest.raises(HTTPException):
            CaseService(db).upsert_case(ctx, cmd.model_copy(update={"invoice_ids": [uuid4()]}))
        with pytest.raises(HTTPException):
            CaseService(db).upsert_case(object(), cmd)
        assert (
            db.scalar(
                select(func.count()).select_from(
                    __import__("aco.features.cases.models", fromlist=["CollectionCase"]).CollectionCase
                )
            )
            == 0
        )


def test_full_supported_evidence_span_deduplicates_without_index_size_loss() -> None:
    import secrets

    from aco.features.cases.service import CaseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        body = "Synthetic evidence " + secrets.token_hex(3800)
        msg = communication(db, tenant, rows, body)
        ctx = context(tenant, user.id)
        svc = CaseService(db)
        result = svc.upsert_case(ctx, command(rows, msg))
        db.commit()
        replay = svc.upsert_case(ctx, command(rows, msg))
        db.commit()
        assert replay.case.id == result.case.id and replay.case.evidence[0].excerpt == body
        assert len(replay.case.evidence) == 1


def test_evidence_identity_collision_refuses_different_full_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from aco.features.cases.models import CaseEvidence
    from aco.features.cases.service import CaseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        first = communication(db, tenant, rows, "Synthetic first source evidence.")
        ctx = context(tenant, user.id)
        svc = CaseService(db)
        created = svc.upsert_case(ctx, command(rows, first))
        db.commit()
        evidence_key = db.scalar(select(CaseEvidence.evidence_key))
        assert evidence_key is not None
        second = communication(db, tenant, rows, "Synthetic distinct source evidence.")
        db.commit()

        class CollisionDigest:
            def hexdigest(self) -> str:
                assert isinstance(evidence_key, str)
                return evidence_key

        monkeypatch.setattr("aco.features.cases.service.hashlib.sha256", lambda _: CollisionDigest())
        with pytest.raises(HTTPException) as exc:
            svc._evidence(ctx, svc._row(ctx, created.case.id), command(rows, second).evidence, second)
        assert exc.value.status_code == 409
        assert exc.value.detail == "EVIDENCE_IDENTITY_CONFLICT"
        db.rollback()
        preserved = svc.get(ctx, created.case.id)
        assert len(preserved.evidence) == 1
        assert preserved.evidence[0].communication_id == first.id
        assert preserved.evidence[0].excerpt == "Synthetic first source evidence."


def test_payment_issue_rejects_credit_reversal_and_reversed_cash() -> None:
    from aco.features.accounting.models import CollectionRecord, DemoSourceRecord, VerifiedPaymentAllocation
    from aco.features.cases.dto import InvoiceIssueTransition, TransitionCase
    from aco.features.cases.service import CaseService
    from aco.features.identity.models import now

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        invoice = rows[0]
        msg = communication(db, tenant, rows, "Synthetic payment location dispute and source review.")
        ctx = context(tenant, user.id)
        svc = CaseService(db)
        case = svc.upsert_case(ctx, command(rows, msg, issue_type="payment_not_found")).case
        case = svc.transition_case(
            ctx,
            TransitionCase(
                case_id=case.id, status="INVESTIGATING", expected_version=case.version, reason="Source review"
            ),
        )
        db.commit()
        operation = uuid4()
        response = client.post(
            f"/api/workspaces/{tenant}/demo/accounting/simulate-payment",
            json={
                "provider_connection_id": str(invoice.provider_connection_id),
                "external_invoice_id": invoice.external_invoice_id,
                "amount": {"minor_units": 10000, "currency": "AUD", "exponent": 2},
                "operation_id": str(operation),
            },
        )
        assert response.status_code == 200, response.text
        event = publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            status="open",
            outstanding={"minor_units": 10000, "currency": "AUD", "exponent": 2},
        )
        stamp = now()
        for kind, amount, parent in [("credit", 100, None), ("reversal", -10000, str(operation))]:
            db.add(
                DemoSourceRecord(
                    tenant_id=tenant,
                    provider_connection_id=invoice.provider_connection_id,
                    organisation_id=event.organisation_id,
                    kind="payment",
                    external_id=f"synthetic-{kind}/1",
                    revision=1,
                    effective_at=stamp,
                    observed_at=stamp,
                    payload={
                        "external_payment_id": f"synthetic-{kind}",
                        "external_allocation_id": "1",
                        "external_invoice_id": invoice.external_invoice_id,
                        "kind": kind,
                        "amount": {"minor_units": amount, "currency": "AUD", "exponent": 2},
                        "parent_payment_id": parent,
                        "parent_allocation_id": "1" if parent else None,
                    },
                )
            )
        db.commit()
        apply(client.app, ctx, event)
        db.expire_all()
        allocations = list(
            db.scalars(
                select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.invoice_id == invoice.id)
            )
        )
        assert {a.kind for a in allocations} == {"cash", "credit", "reversal"}
        for allocation in allocations:
            with pytest.raises(HTTPException) as refused:
                svc.set_invoice_issue_state(
                    ctx,
                    InvoiceIssueTransition(
                        case_id=case.id,
                        invoice_id=invoice.id,
                        expected_version=case.version,
                        evidence=command(rows, msg).evidence,
                        resolution_kind="payment_allocation",
                        resolution_reference_id=allocation.id,
                        resolution_action="Review synthetic source allocation",
                    ),
                )
            assert refused.value.status_code == 422
        collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice.id))
        assert collection is not None and "active_dispute" in collection.hold_reasons
        assert svc.get(ctx, case.id).invoice_links[0].issue_state == "OPEN"
        db.rollback()  # Release the read/review transaction before the independent source API transaction.
        valid_operation = uuid4()
        paid = client.post(
            f"/api/workspaces/{tenant}/demo/accounting/simulate-payment",
            json={
                "provider_connection_id": str(invoice.provider_connection_id),
                "external_invoice_id": invoice.external_invoice_id,
                "amount": {"minor_units": 10000, "currency": "AUD", "exponent": 2},
                "operation_id": str(valid_operation),
            },
        )
        assert paid.status_code == 200, paid.text
        db.expire_all()
        valid_cash = db.scalar(
            select(VerifiedPaymentAllocation).where(
                VerifiedPaymentAllocation.invoice_id == invoice.id,
                VerifiedPaymentAllocation.external_payment_id == str(valid_operation),
            )
        )
        assert valid_cash is not None
        resolved = svc.set_invoice_issue_state(
            ctx,
            InvoiceIssueTransition(
                case_id=case.id,
                invoice_id=invoice.id,
                expected_version=case.version,
                evidence=command(rows, msg).evidence,
                resolution_kind="payment_allocation",
                resolution_reference_id=valid_cash.id,
                resolution_action="Owner verified matching actual source cash",
            ),
        )
        assert resolved.invoice_links[0].issue_state == "RESOLVED"
