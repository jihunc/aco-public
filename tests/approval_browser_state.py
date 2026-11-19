"""Test-only canonical approvals in a new, paused synthetic browser workspace.

Only the contained mock browser lane may invoke this helper. Identity comes from
persisted OIDC login and current membership, never a caller-provided actor UUID.
No provider, dispatch, approval decision, cleanup or financial UPDATE is exposed.
"""

import argparse
import asyncio
import json
import os
from copy import deepcopy
from datetime import timedelta
from ipaddress import IPv4Address
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.local_runtime import local_settings
from aco.core.models import register_models
from aco.core.settings import Settings
from aco.features.accounting.dto import AccountingDataChanged
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import AccountingEvent, CustomerContact, DemoSourceRecord, InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.actions.service import ActionService, ScheduledCommand
from aco.features.communications.models import Communication, EmailDispatchBinding
from aco.features.identity.models import Actor, LoginSession, Membership, Tenant, User, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.models import CollectionPolicyVersion, DispatchTicketRecord
from aco.features.review.internal_work import ApprovalProposal, InternalWorkService
from aco.features.review.models import ApprovalDecision, ApprovalRequest
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
from aco.integrations.ports.identity import AuthenticatedSubject

register_models()

PREFIX = "Approval browser "


def trusted_control_plane_ip(variable: str) -> str | None:
    try:
        address = IPv4Address(os.getenv(variable, ""))
    except ValueError:
        return None
    return (
        str(address)
        if (
            address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
        )
        else None
    )


def local_issuer(issuer: str | None) -> bool:
    if issuer in {"http://127.0.0.1:58080/realms/aco", "http://localhost:58080/realms/aco"}:
        return True
    parsed = urlsplit(issuer or "")
    trusted = trusted_control_plane_ip("ACO_LOCAL_OIDC_IP")
    return bool(
        trusted
        and parsed.scheme == "http"
        and parsed.hostname == trusted
        and parsed.port == 8080
        and parsed.path == "/realms/aco"
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and not parsed.password
    )


def require_local_profile(profile: str | None, settings: Settings) -> None:
    if profile != "mock":
        raise ValueError("Explicit mock browser profile required")
    if (
        settings.app_env != "local"
        or settings.external_providers != "fake"
        or settings.outbound_enabled
        or not settings.kill_switch
        or settings.synthetic_email_enabled
        or settings.email_adapter != "mock"
        or settings.llm_provider != "fake"
    ):
        raise ValueError("Contained offline runtime with outbound off required")
    if settings.database_url is not None:
        target = make_url(settings.database_url.get_secret_value())
        trusted = trusted_control_plane_ip("ACO_LOCAL_PG_IP")
        local = target.host in {"127.0.0.1", "localhost"} and target.port == 55432
        container = trusted is not None and target.host == trusted and target.port == 5432
        if not (local or container) or target.database != "aco" or target.drivername != "postgresql+psycopg":
            raise ValueError("Exact local application database required")


def require_fresh_workspace(tenant: Tenant) -> None:
    try:
        unique = UUID(tenant.name.removeprefix(PREFIX))
    except ValueError:
        unique = None
    if (
        not tenant.name.startswith(PREFIX)
        or unique is None
        or unique.version != 4
        or not tenant.is_demo
        or tenant.automation_enabled
        or not tenant.outbound_paused
        or not timedelta(0) <= now() - tenant.created_at <= timedelta(minutes=15)
    ):
        raise ValueError("Only a fresh synthetic, unique, paused approval workspace is allowed")


def current_owner(store: Session, workspace: UUID, issuer: str | None) -> TenantContext:
    if not local_issuer(issuer):
        raise ValueError("Actual local OIDC issuer required")
    users = list(
        store.scalars(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.tenant_id == workspace,
                Membership.active.is_(True),
                Membership.role == "owner",
                User.issuer == issuer,
            )
        )
    )
    if len(users) != 1:
        raise ValueError("One actual browser Owner membership required")
    user = users[0]
    login = store.scalar(
        select(LoginSession)
        .where(
            LoginSession.user_id == user.id,
            LoginSession.revoked.is_(False),
            LoginSession.expires_at > now(),
        )
        .order_by(LoginSession.created_at.desc())
        .limit(1)
    )
    actor = store.get(Actor, user.id)
    if login is None or actor is None or actor.kind != "human" or not actor.active:
        raise ValueError("Current actual browser Owner login required")
    ctx = IdentityService(store).resolve_context(
        AuthenticatedSubject(
            user_id=user.id,
            issuer=user.issuer,
            subject=user.subject,
            session_id=login.id,
            authenticated_at=login.created_at,
            correlation_id=uuid4(),
        ),
        workspace,
    )
    tenant = IdentityService(store).authorize(ctx, Permission.SETTINGS)
    require_fresh_workspace(tenant)
    return ctx


def prepare(store: Session, ctx: TenantContext) -> dict[str, object]:
    if store.scalar(
        select(ApprovalRequest.id).where(ApprovalRequest.tenant_id == ctx.tenant_id)
    ) or store.scalar(select(ScheduledAction.id).where(ScheduledAction.tenant_id == ctx.tenant_id)):
        raise ValueError("Fixture requires a fresh workspace with no existing actions or approvals")
    policy = store.scalar(
        select(CollectionPolicyVersion)
        .where(
            CollectionPolicyVersion.tenant_id == ctx.tenant_id,
        )
        .order_by(CollectionPolicyVersion.version.desc())
        .limit(1)
    )
    if policy is None:
        raise ValueError("Seed genuine synthetic accounting through the demo API first")
    refs: list[dict[str, str]] = []
    for number in ("INV-101", "INV-102"):
        invoice = store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == ctx.tenant_id,
                InvoiceSnapshot.number == number,
            )
        )
        if invoice is None or not invoice.synthetic:
            raise ValueError("Actual seeded synthetic invoice required")
        source = store.scalar(
            select(AccountingEvent)
            .where(
                AccountingEvent.tenant_id == ctx.tenant_id,
                AccountingEvent.invoice_id == invoice.id,
                AccountingEvent.source_revision == invoice.source_revision,
            )
            .order_by(AccountingEvent.created_at.desc())
            .limit(1)
        )
        contact = store.scalar(
            select(CustomerContact).where(
                CustomerContact.tenant_id == ctx.tenant_id,
                CustomerContact.customer_id == invoice.customer_id,
            )
        )
        if source is None or contact is None or contact.email is None:
            raise ValueError("Actual scoped accounting source and contact required")
        action = ActionService(store).enqueue_action(
            ctx,
            ScheduledCommand(
                kind="accounting_recheck",
                provider_connection_id=invoice.provider_connection_id,
                invoice_id=invoice.id,
                policy_version_id=policy.id,
                source_event_id=source.id,
                due_at=now(),
            ),
        )
        content = f"Explicit synthetic browser proposal for {number}; review only."
        result = InternalWorkService(store).request_approval(
            ctx,
            ApprovalProposal(
                action_id=action.action_id,
                action_version=action.version,
                content=content,
                destination=contact.email,
                channel="email",
                invoice_ids=(invoice.id,),
                evidence_ids=(source.id,),
                policy_version_id=policy.id,
                cost_bound="0",
                currency="AUD",
                consent_mode="no_capture",
            ),
        )
        refs.append(
            {
                "id": str(result.request_id),
                "invoice_id": str(invoice.id),
                "action_id": str(action.action_id),
                "content": content,
            }
        )
    store.commit()
    return {"approvals": refs, "synthetic": True}


async def reconcile(url: str, ctx: TenantContext, event: AccountingDataChanged) -> None:
    engine = create_async_engine(url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as db:
            await AccountingService(
                db, DemoAccountingAdapter(db, event.provider_connection_id)
            ).apply_accounting_change(ctx, event)
    finally:
        await engine.dispose()


def change_source(store: Session, ctx: TenantContext, approval_id: UUID, url: str) -> dict[str, object]:
    approval = store.scalar(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == ctx.tenant_id,
            ApprovalRequest.id == approval_id,
        )
    )
    action = store.get(ScheduledAction, approval.action_id) if approval else None
    invoice = store.get(InvoiceSnapshot, action.invoice_id) if action else None
    if (
        approval is None
        or action is None
        or invoice is None
        or not invoice.synthetic
        or approval.status != "pending"
    ):
        raise ValueError("Pending canonical synthetic approval in this workspace required")
    latest = store.scalar(
        select(DemoSourceRecord)
        .where(
            DemoSourceRecord.tenant_id == ctx.tenant_id,
            DemoSourceRecord.provider_connection_id == invoice.provider_connection_id,
            DemoSourceRecord.kind == "invoice",
            DemoSourceRecord.external_id == invoice.external_invoice_id,
        )
        .order_by(DemoSourceRecord.revision.desc())
        .limit(1)
    )
    if latest is None or invoice.balance_minor <= 1:
        raise ValueError("Actual positive synthetic invoice source required")
    payload = deepcopy(latest.payload)
    payload["outstanding"] = {
        "minor_units": invoice.balance_minor - 1,
        "currency": invoice.currency,
        "exponent": invoice.exponent,
    }
    row = DemoSourceRecord(
        tenant_id=ctx.tenant_id,
        provider_connection_id=invoice.provider_connection_id,
        organisation_id=latest.organisation_id,
        kind="invoice",
        external_id=invoice.external_invoice_id,
        revision=latest.revision + 1,
        payload=payload,
        effective_at=now(),
        observed_at=now(),
    )
    store.add(row)
    store.flush()
    event = AccountingDataChanged(
        provider_connection_id=invoice.provider_connection_id,
        organisation_id=row.organisation_id,
        external_invoice_id=invoice.external_invoice_id,
        external_event_id=f"approval-browser-synthetic:{row.sequence}",
    )
    previous = invoice.source_revision
    store.commit()
    asyncio.run(reconcile(url, ctx, event))
    store.expire_all()
    current = store.get(InvoiceSnapshot, invoice.id)
    if current is None or current.source_revision == previous or not current.synthetic:
        raise ValueError("Synthetic source reconciliation was not applied")
    return {"previous_revision": previous, "current_revision": current.source_revision}


def evidence(store: Session, ctx: TenantContext) -> dict[str, object]:
    def count(
        model: type[ActionExecution]
        | type[EmailDispatchBinding]
        | type[Communication]
        | type[DispatchTicketRecord],
    ) -> int:
        return int(
            store.scalar(select(func.count()).select_from(model).where(model.tenant_id == ctx.tenant_id)) or 0
        )

    requests = list(store.scalars(select(ApprovalRequest).where(ApprovalRequest.tenant_id == ctx.tenant_id)))
    decisions = list(
        store.scalars(select(ApprovalDecision).where(ApprovalDecision.tenant_id == ctx.tenant_id))
    )
    actions = list(store.scalars(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id)))
    return {
        "synthetic": True,
        "action_executions": count(ActionExecution),
        "dispatch_bindings": count(EmailDispatchBinding),
        "communications": count(Communication),
        "dispatch_tickets": count(DispatchTicketRecord),
        "approvals": [{"id": str(r.id), "status": r.status, "version": r.version} for r in requests],
        "decisions": [
            {"request_id": str(d.request_id), "approver_id": str(d.approver_id), "decision": d.decision}
            for d in decisions
        ],
        "actions": [{"id": str(a.id), "status": a.status, "kind": a.kind} for a in actions],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "change-source", "evidence"))
    parser.add_argument("--workspace", required=True, type=UUID)
    parser.add_argument("--approval", type=UUID)
    args = parser.parse_args()
    settings = local_settings()
    require_local_profile(os.getenv("ACO_TEST_PROFILE"), settings)
    if settings.database_url is None:
        raise ValueError("Local application database required")
    url = settings.database_url.get_secret_value()
    engine = create_engine(url)
    try:
        with Session(engine) as store:
            ctx = current_owner(store, args.workspace, settings.identity_issuer)
            if args.command == "prepare":
                result = prepare(store, ctx)
            elif args.command == "change-source":
                if args.approval is None:
                    parser.error("--approval required for change-source")
                result = change_source(store, ctx, args.approval, url)
            else:
                result = evidence(store, ctx)
            print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
