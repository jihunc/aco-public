"""Human approval decisions over genuine canonical actions, no dispatch grant."""

import json
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from action_support import World, action_world, enqueue
from boundary_evidence import BoundaryObserver
from fastapi import FastAPI, HTTPException
from identity_support import add_actor
from sqlalchemy import select

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.identity.models import Membership, now
from aco.features.review.internal_work import ApprovalProposal, InternalWorkService


def request(world: World) -> UUID:
    ref = enqueue(world)
    invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
    assert invoice is not None
    contact = world.store.scalar(
        select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
    )
    assert contact is not None and contact.email is not None
    command = ApprovalProposal(
        action_id=ref.action_id,
        action_version=1,
        content="Explicit synthetic approval fixture; review only.",
        destination=contact.email,
        channel="email",
        invoice_ids=(world.invoice_id,),
        evidence_ids=(world.source_event_id,),
        policy_version_id=world.policy_version_id,
        cost_bound="0",
        currency="AUD",
        consent_mode="no_capture",
    )
    result = InternalWorkService(world.store).request_approval(world.ctx, command)
    world.store.commit()
    return result.request_id


def reviewer(world: World, role: str = "finance_manager") -> TenantContext:
    user, _ = add_actor(world.store, cast(FastAPI, world.client.app), "reviewer@synthetic.invalid")
    world.store.add(Membership(tenant_id=world.ctx.tenant_id, user_id=user.id, role=role))
    world.store.commit()
    result = world.ctx.model_copy(update={"actor_id": user.id, "role": Role(role)})
    assert isinstance(result, TenantContext)
    return result


def test_independent_human_approval_records_exact_scope_without_dispatch() -> None:
    from aco.features.review.approval_dto import ApprovalDecisionCommand
    from aco.features.review.approvals import HumanApprovalService
    from aco.features.review.models import ApprovalDecision

    with action_world() as world:
        request_id = request(world)
        ctx = reviewer(world)
        svc = HumanApprovalService(world.store)
        view = svc.get(ctx, request_id)
        assert view.can_approve and view.dispatch_status == "NOT_ENABLED"
        approved = svc.decide_approval(
            ctx,
            request_id,
            ApprovalDecisionCommand(
                expected_version=view.version,
                reviewed_action_version=view.action_version,
                reviewed_material_hash=view.material_hash,
                reviewed_binding_hash=view.current_binding_hash,
                decision="approved",
                reason="Independently reviewed exact synthetic scope",
                expires_in_seconds=3600,
            ),
        )
        world.store.commit()
        assert approved.status == "approved" and approved.effective_status == "approved"
        assert approved.granted_at is not None and approved.expires_at == approved.granted_at + timedelta(
            hours=1
        )
        decision = world.store.scalar(
            select(ApprovalDecision).where(ApprovalDecision.request_id == request_id)
        )
        assert decision is not None and decision.approver_id == ctx.actor_id
        assert decision.review_binding is not None
        assert world.submission_count(view.action_id) == 0
        assert world.action(view.action_id).status == "queued"


@pytest.mark.parametrize(
    "fixture_id",
    [
        "approval_elapsed_86399",
        "approval_elapsed_86400",
        "approval_elapsed_86401",
        "approval_material_edit_invalidates",
        "approval_high_risk_self_approval_blocked",
    ],
)
def test_boundary_approval_expiry_scope_and_self_approval(
    fixture_id: str, boundary_observer: BoundaryObserver
) -> None:
    from aco.features.review.approval_dto import ApprovalDecisionCommand
    from aco.features.review.approvals import HumanApprovalService

    fixture = next(
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["id"] == fixture_id
    )
    with action_world() as world:
        request_id = request(world)
        ctx = world.ctx if fixture["input"]["proposer"] == fixture["input"]["approver"] else reviewer(world)
        at = now()
        svc = HumanApprovalService(world.store, at=at)
        view = svc.get(ctx, request_id)
        command = ApprovalDecisionCommand(
            expected_version=view.version,
            reviewed_action_version=view.action_version,
            reviewed_material_hash=view.material_hash,
            reviewed_binding_hash=view.current_binding_hash,
            decision="approved",
            reason="Boundary synthetic source review",
        )
        if fixture_id == "approval_high_risk_self_approval_blocked":
            with pytest.raises(HTTPException) as refused:
                svc.decide_approval(ctx, request_id, command)
            observed = {
                "reason": refused.value.detail,
                "approval_valid": svc.get(ctx, request_id).approval_valid,
            }
            assert refused.value.detail == fixture["expected"]["reason"]
            assert svc.get(ctx, request_id).approval_valid is fixture["expected"]["approval_valid"]
        else:
            granted = svc.decide_approval(ctx, request_id, command)
            world.store.commit()
            if fixture_id == "approval_material_edit_invalidates":
                invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
                assert invoice is not None
                contact = world.store.scalar(
                    select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
                )
                assert contact is not None
                contact.email = "changed@synthetic.invalid"
                contact.version += 1
                world.store.commit()
            current = HumanApprovalService(
                world.store, at=at + timedelta(seconds=fixture["input"]["seconds_since_grant"])
            ).get(ctx, request_id)
            observed = {
                "approval_valid": current.approval_valid,
                "new_approval_required": current.new_approval_required,
            }
            assert current.approval_valid is fixture["expected"]["approval_valid"]
            if "new_approval_required" in fixture["expected"]:
                assert current.new_approval_required is fixture["expected"]["new_approval_required"]
            assert granted.consumed_at is None
        assert world.submission_count(view.action_id) == 0
        boundary_observer.assert_observed(fixture["id"], {key: observed[key] for key in fixture["expected"]})


def test_approval_http_review_snapshot_csrf_current_role_and_readable_source() -> None:
    from aco.features.review.approval_api import router
    from aco.features.review.models import ApprovalDecision

    with action_world() as world:
        request_id = request(world)
        app = cast(FastAPI, world.client.app)
        app.include_router(router)
        user, client = add_actor(world.store, app, "http-reviewer@synthetic.invalid")
        member = Membership(tenant_id=world.ctx.tenant_id, user_id=user.id, role="finance_manager")
        world.store.add(member)
        world.store.commit()
        path = f"/api/workspaces/{world.ctx.tenant_id}/approvals/{request_id}"
        response = client.get(path)
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["evidence"][0]["id"] == str(world.source_event_id)
        assert "provenance" in json.loads(view["evidence"][0]["source_json"])
        assert view["invoice_scope"]["synthetic"]
        assert isinstance(view["invoice_scope"]["outstanding"]["minor_units"], str)
        payload = dict(
            expected_version=view["version"],
            reviewed_action_version=view["action_version"],
            reviewed_material_hash=view["material_hash"],
            reviewed_binding_hash=view["current_binding_hash"],
            decision="approved",
            reason="Read exact evidence and captured state",
            expires_in_seconds=60,
        )
        assert (
            client.post(path + "/decisions", json=payload, headers={"x-csrf-token": "invalid"}).status_code
            == 403
        )
        member.role = "operator"
        world.store.commit()
        assert client.post(path + "/decisions", json=payload).status_code == 403
        member.role = "finance_manager"
        world.store.commit()
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        contact = world.store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
        )
        assert contact is not None
        contact.version += 1
        world.store.commit()
        assert client.post(path + "/decisions", json=payload).status_code == 409
        assert (
            world.store.scalar(select(ApprovalDecision).where(ApprovalDecision.request_id == request_id))
            is None
        )
        world.store.rollback()
        fresh = client.get(path).json()
        payload.update(
            expected_version=fresh["version"],
            reviewed_binding_hash=fresh["current_binding_hash"],
            decision="denied",
        )
        denied = client.post(path + "/decisions", json=payload)
        assert denied.status_code == 200, denied.text
        assert denied.json()["status"] == "denied" and not denied.json()["approval_valid"]
        assert client.post(path + "/decisions", json=payload).status_code == 409
        assert client.get(path.rsplit("/", 1)[0], params={"invoice_id": str(world.invoice_id)}).json()[
            "items"
        ][0]["id"] == str(request_id)
        assert world.submission_count(UUID(view["action_id"])) == 0


def approve(world: World, request_id: UUID, ctx: TenantContext) -> UUID:
    from aco.features.review.approval_dto import ApprovalDecisionCommand
    from aco.features.review.approvals import HumanApprovalService

    svc = HumanApprovalService(world.store)
    view = svc.get(ctx, request_id)
    result = svc.decide_approval(
        ctx,
        request_id,
        ApprovalDecisionCommand(
            expected_version=view.version,
            reviewed_action_version=view.action_version,
            reviewed_material_hash=view.material_hash,
            reviewed_binding_hash=view.current_binding_hash,
            decision="approved",
            reason="Independent source-backed synthetic review",
        ),
    )
    world.store.commit()
    return result.action_id


@pytest.mark.parametrize("changed", ["source", "policy", "recipient", "hold", "approver", "initiator"])
def test_approval_current_material_and_authority_changes_invalidate(changed: str) -> None:
    from accounting_support import apply, publish_invoice

    from aco.features.cases.service import place_hold
    from aco.features.policies.dto import PolicyDefinition, PolicyRule, PolicySave, RecipientSave
    from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion
    from aco.features.policies.service import PolicyService
    from aco.features.review.approvals import HumanApprovalService

    with action_world() as world:
        request_id = request(world)
        ctx = reviewer(world)
        action_id = approve(world, request_id, ctx)
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        if changed == "source":
            event = publish_invoice(
                world.store,
                world.ctx.tenant_id,
                world.connection_id,
                outstanding={"minor_units": 9999, "currency": "AUD", "exponent": 2},
            )
            world.store.commit()
            apply(world.client.app, world.ctx, event)
        elif changed == "policy":
            old = world.store.get(CollectionPolicyVersion, world.policy_version_id)
            assert old is not None
            policy = world.store.get(CollectionPolicy, old.policy_id)
            assert policy is not None
            PolicyService(world.store).save_policy(
                world.ctx,
                PolicySave(
                    name=policy.name,
                    definition=PolicyDefinition(rules=[PolicyRule(id=uuid4(), offset_days=3)]),
                    expected_version=policy.version,
                ),
                policy.version,
                policy.id,
            )
            world.store.commit()
        elif changed == "recipient":
            contact = world.store.scalar(
                select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
            )
            assert contact is not None
            PolicyService(world.store).save_recipient(
                world.ctx,
                contact.id,
                RecipientSave(
                    expected_version=0,
                    expected_contact_revision=contact.source_revision,
                    expected_destination=contact.email,
                    opt_out=True,
                    evidence="Synthetic documented opt-out control",
                ),
            )
            world.store.commit()
        elif changed == "hold":
            place_hold(world.store, world.ctx, invoice.id, "active_dispute", "Synthetic unresolved issue")
            world.store.commit()
        else:
            member = world.store.scalar(
                select(Membership).where(
                    Membership.tenant_id == world.ctx.tenant_id,
                    Membership.user_id == (ctx.actor_id if changed == "approver" else world.ctx.actor_id),
                )
            )
            assert member is not None
            member.role = "viewer"
            member.version += 1
            world.store.commit()
        current = HumanApprovalService(world.store).get(ctx, request_id)
        assert (
            not current.approval_valid
            and current.effective_status == "stale"
            and current.new_approval_required
        )
        assert world.submission_count(action_id) == 0


def test_concurrent_approval_decisions_have_one_winner_and_one_immutable_decision() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import func
    from sqlalchemy.orm import Session

    from aco.features.review.approval_dto import ApprovalDecisionCommand
    from aco.features.review.approvals import HumanApprovalService
    from aco.features.review.models import ApprovalDecision

    with action_world() as world:
        request_id = request(world)
        ctx = reviewer(world)
        view = HumanApprovalService(world.store).get(ctx, request_id)
        world.store.commit()
        command = ApprovalDecisionCommand(
            expected_version=view.version,
            reviewed_action_version=view.action_version,
            reviewed_material_hash=view.material_hash,
            reviewed_binding_hash=view.current_binding_hash,
            decision="approved",
            reason="Concurrent independent review",
        )

        def decide(decision: str) -> str:
            with Session(world.store.bind) as db:
                try:
                    result = HumanApprovalService(db).decide_approval(
                        ctx, request_id, command.model_copy(update={"decision": decision})
                    )
                    db.commit()
                    return result.status
                except HTTPException as exc:
                    db.rollback()
                    assert exc.status_code == 409
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ["approved", "denied"]))
        assert results.count("conflict") == 1
        assert world.store.scalar(select(func.count()).select_from(ApprovalDecision)) == 1
        assert world.submission_count(view.action_id) == 0


def test_approval_proposal_and_decision_history_are_immutable_and_consumed_fails_closed() -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from aco.features.review.approvals import HumanApprovalService
    from aco.features.review.models import ApprovalRequest

    with action_world() as world:
        request_id = request(world)
        ctx = reviewer(world)
        action_id = approve(world, request_id, ctx)
        with pytest.raises(DBAPIError), world.store.begin_nested():
            world.store.execute(
                text("UPDATE approval_requests SET proposal='{}'::json WHERE id=:id"), {"id": request_id}
            )
        with pytest.raises(DBAPIError), world.store.begin_nested():
            world.store.execute(
                text("UPDATE approval_decisions SET reason='changed' WHERE request_id=:id"),
                {"id": request_id},
            )
        row = world.store.get(ApprovalRequest, request_id)
        assert row is not None
        # Explicit consumed-state fixture only: actual executor consume integration remains unavailable.
        row.consumed_at = now()
        row.version += 1
        world.store.commit()
        current = HumanApprovalService(world.store).get(ctx, request_id)
        assert current.effective_status == "consumed" and not current.approval_valid
        assert world.submission_count(action_id) == 0


def test_approval_foreign_workspace_and_generic_service_context_are_refused() -> None:
    from uuid import uuid4

    from aco.core.context import ServiceContext
    from aco.features.identity.models import Tenant
    from aco.features.review.approvals import HumanApprovalService

    with action_world() as world:
        request_id = request(world)
        foreign = Tenant(name="Other synthetic workspace", timezone="UTC")
        world.store.add(foreign)
        world.store.flush()
        world.store.add(Membership(tenant_id=foreign.id, user_id=world.ctx.actor_id, role="owner"))
        world.store.commit()
        ctx = world.ctx.model_copy(update={"tenant_id": foreign.id})
        with pytest.raises(HTTPException) as refused:
            HumanApprovalService(world.store).get(ctx, request_id)
        assert refused.value.status_code == 404
        assert HumanApprovalService(world.store).list(ctx).items == []
        worker = ServiceContext(
            tenant_id=world.ctx.tenant_id,
            actor_id=uuid4(),
            correlation_id=uuid4(),
            action_id=uuid4(),
            action_version=1,
        )
        with pytest.raises(HTTPException) as denied:
            HumanApprovalService(world.store).get(cast(TenantContext, worker), request_id)
        assert denied.value.detail == "HUMAN_REQUIRED"


def test_approval_migration_preserves_existing_requests_and_refuses_lossy_downgrade() -> None:
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from legacy_usage_support import legacy_snapshot
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError

    from alembic import command

    with action_world() as world:
        request_id = request(world)
        ctx = reviewer(world)
        source_schema = world.store.scalar(text("SELECT current_schema()"))
        assert isinstance(source_schema, str)
        world.store.rollback()
        # Genuine current fixture bytes are projected into an isolated historical
        # schema. Never downgrade or disable guards on the current fixture.
        with legacy_snapshot(world.store) as (historical, config):
            historical.rollback()
            command.upgrade(config, "r0010_agent")
            for table in ("identity_actors", "scheduled_actions", "approval_requests"):
                columns = ",".join(
                    f'"{c["name"]}"' for c in inspect(historical.connection()).get_columns(table)
                )
                predicate = " WHERE kind='service'" if table == "identity_actors" else ""
                historical.execute(
                    text(
                        f'INSERT INTO "{table}" ({columns}) SELECT {columns} '
                        f'FROM "{source_schema}"."{table}"{predicate}'
                    )
                )
                assert list(
                    historical.execute(
                        text(
                            f"SELECT to_jsonb(t) FROM (SELECT {columns} "
                            f'FROM "{table}"{predicate} ORDER BY id) t'
                        )
                    ).scalars()
                ) == list(
                    historical.execute(
                        text(
                            f"SELECT to_jsonb(t) FROM (SELECT {columns} "
                            f'FROM "{source_schema}"."{table}"{predicate} ORDER BY id) t'
                        )
                    ).scalars()
                )
            original = historical.scalar(
                text("SELECT to_jsonb(a) FROM approval_requests a WHERE id=:id"), {"id": request_id}
            )
            assert original is not None
            historical.commit()
            command.upgrade(config, "r0012_approval_decisions")
            command.downgrade(config, "r0010_agent")
            command.upgrade(config, "r0012_approval_decisions")
            assert (
                historical.scalar(text("SELECT version_num FROM alembic_version"))
                == "r0012_approval_decisions"
            )
            assert (
                historical.scalar(
                    text("SELECT to_jsonb(a) FROM approval_requests a WHERE id=:id"), {"id": request_id}
                )
                == original
            )
        # Independently reach r12's own retained-decision guard with a real
        # current human approval, without r33 masking the intended refusal.
        approve(world, request_id, ctx)
        config = Config("alembic.ini")
        revision = ScriptDirectory.from_config(config).get_revision("r0012_approval_decisions")
        assert revision is not None
        retained = {
            table: list(world.store.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")).scalars())
            for table in ("approval_requests", "approval_decisions")
        }
        assert len(retained["approval_decisions"]) == 1
        with pytest.raises(DBAPIError, match="approval evidence prevents lossy downgrade"):
            with world.store.begin_nested():
                with Operations.context(MigrationContext.configure(world.store.connection())):
                    revision.module.downgrade()
        assert (
            world.store.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        assert {
            table: list(world.store.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")).scalars())
            for table in retained
        } == retained
