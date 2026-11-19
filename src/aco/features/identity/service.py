"""Membership-derived authority; all mutations re-read authority under tenant lock."""

import hashlib
import secrets
from datetime import timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, ServiceContext, TenantContext
from aco.core.types import Role
from aco.features.identity.models import (
    AuditEvent,
    Invitation,
    Membership,
    ProviderConnection,
    Tenant,
    User,
    now,
)
from aco.integrations.ports.identity import AuthenticatedSubject
from aco.integrations.ports.storage import StorageInvocationContext


class Permission(StrEnum):
    INVOICE_READ = "invoice_read"
    TASK_WRITE = "task_write"
    SETTINGS = "settings"
    INVESTIGATE = "investigate"
    RESOLVE = "resolve"
    # Later action guards must prove low-risk factual scope, evidence and lawful hold release.
    RESOLVE_LOW_RISK = "resolve_low_risk"
    APPROVE_EXTERNAL = "approve_external"
    PAUSE = "pause"
    RESUME = "resume"
    EXPORT = "export"
    DELETE_REQUEST = "delete_request"
    DELETE_AUTHORIZE = "delete_authorize"
    DEMO_PAYMENT = "demo_payment"
    DEMO_INBOUND = "demo_inbound"
    DEMO_TIME = "demo_time"
    DEMO_RUN_DUE = "demo_run_due"
    DEMO_RESET = "demo_reset"
    GLOBAL_PAUSE = "global_pause"
    GLOBAL_RESUME = "global_resume"
    CROSS_TENANT_ADMIN = "cross_tenant_admin"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(
        p
        for p in Permission
        if p not in {Permission.GLOBAL_PAUSE, Permission.GLOBAL_RESUME, Permission.CROSS_TENANT_ADMIN}
    ),
    Role.FINANCE_MANAGER: frozenset(
        {
            Permission.INVOICE_READ,
            Permission.TASK_WRITE,
            Permission.INVESTIGATE,
            Permission.RESOLVE,
            Permission.RESOLVE_LOW_RISK,
            Permission.APPROVE_EXTERNAL,
            Permission.PAUSE,
            Permission.RESUME,
            Permission.EXPORT,
            Permission.DELETE_REQUEST,
            Permission.DEMO_PAYMENT,
            Permission.DEMO_INBOUND,
            Permission.DEMO_TIME,
            Permission.DEMO_RUN_DUE,
        }
    ),
    Role.OPERATOR: frozenset(
        {
            Permission.RESOLVE_LOW_RISK,
            Permission.INVOICE_READ,
            Permission.TASK_WRITE,
            Permission.INVESTIGATE,
            Permission.PAUSE,
            Permission.DEMO_INBOUND,
            Permission.DEMO_RUN_DUE,
        }
    ),
    Role.VIEWER: frozenset({Permission.INVOICE_READ}),
}


def require_permission(ctx: TenantContext, permission: Permission) -> None:
    if permission not in ROLE_PERMISSIONS[ctx.role]:
        raise HTTPException(403, "FORBIDDEN")


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateWorkspace(Command):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(max_length=80)

    @field_validator("name")
    @classmethod
    def meaningful_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Workspace name is required")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose an IANA timezone") from exc
        return value


class WorkspaceView(BaseModel):
    id: UUID
    name: str
    timezone: str
    role: Role
    is_demo: bool
    automation_enabled: bool
    outbound_paused: bool
    version: int
    permissions: list[Permission]


class ChangeMember(Command):
    role: Role
    active: bool
    expected_version: int = Field(ge=1)


class MemberView(Command):
    user_id: UUID
    email: str
    role: Role
    active: bool
    version: int


class MembershipAudit(Command):
    id: UUID
    actor_id: UUID
    action: str


class MemberChangeView(Command):
    member: MemberView
    audit: MembershipAudit


class InviteMember(Command):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role: Role


class AcceptInvitation(Command):
    invite_code: str = Field(min_length=20, max_length=200)


class IdentityService:
    def __init__(self, db: Session):
        self.db = db

    def resolve_context(self, subject: AuthenticatedSubject, selected_workspace: UUID) -> TenantContext:
        user = self.db.get(User, subject.user_id)
        if user is None or (user.issuer, user.subject) != (subject.issuer, subject.subject):
            raise HTTPException(401, "AUTH_EXPIRED")
        member = self.db.scalar(
            select(Membership)
            .where(
                Membership.tenant_id == selected_workspace,
                Membership.user_id == subject.user_id,
                Membership.active.is_(True),
            )
            .execution_options(populate_existing=True)
        )
        if member is None:
            raise HTTPException(403, "FORBIDDEN")
        return TenantContext(
            tenant_id=selected_workspace,
            actor_id=user.id,
            role=Role(member.role),
            correlation_id=subject.correlation_id,
        )

    def authorize(self, ctx: ActorContext, permission: Permission) -> Tenant:
        if isinstance(ctx, ServiceContext):
            from aco.features.identity.actors import authorize_service

            return authorize_service(self.db, ctx, permission.value)
        # Serialize tenant changes, including concurrent removal/demotion of owners.
        tenant = self.db.scalar(
            select(Tenant)
            .where(Tenant.id == ctx.tenant_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        member = self.db.scalar(
            select(Membership)
            .where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == ctx.actor_id,
                Membership.active.is_(True),
            )
            .execution_options(populate_existing=True)
        )
        if tenant is None or member is None:
            raise HTTPException(403, "FORBIDDEN")
        require_permission(ctx.model_copy(update={"role": Role(member.role)}), permission)
        return tenant

    def view(self, tenant: Tenant, role: Role) -> WorkspaceView:
        return WorkspaceView(
            id=tenant.id,
            name=tenant.name,
            timezone=tenant.timezone,
            role=role,
            is_demo=tenant.is_demo,
            automation_enabled=tenant.automation_enabled,
            outbound_paused=tenant.outbound_paused,
            version=tenant.version,
            permissions=sorted(ROLE_PERMISSIONS[role]),
        )

    def audit(
        self,
        ctx: StorageInvocationContext,
        action: str,
        target: UUID,
        details: dict[str, str | bool] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action=action,
            target_id=target,
            details=details or {},
        )
        self.db.add(event)
        self.db.flush()
        return event

    def create_workspace(self, ctx: AuthenticatedSubject, command: CreateWorkspace) -> WorkspaceView:
        user = self.db.get(User, ctx.user_id)
        if user is None or (user.issuer, user.subject) != (ctx.issuer, ctx.subject):
            raise HTTPException(401, "AUTH_EXPIRED")
        tenant = Tenant(name=command.name, timezone=command.timezone)
        self.db.add(tenant)
        self.db.flush()
        self.db.add(Membership(tenant_id=tenant.id, user_id=user.id, role="owner"))
        self.db.add(
            ProviderConnection(
                tenant_id=tenant.id, provider_type="accounting", provider="demo", status="NOT_CONFIGURED"
            )
        )
        trusted = TenantContext(
            tenant_id=tenant.id, actor_id=user.id, role=Role.OWNER, correlation_id=ctx.correlation_id
        )
        self.audit(trusted, "workspace.created", tenant.id)
        self.db.flush()
        return self.view(tenant, Role.OWNER)

    def change_member(self, ctx: TenantContext, user_id: UUID, command: ChangeMember) -> AuditEvent:
        self.authorize(ctx, Permission.SETTINGS)
        member = self.db.scalar(
            select(Membership)
            .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        if member is None:
            raise HTTPException(404, "NOT_FOUND")
        if member.version != command.expected_version:
            raise HTTPException(409, "MEMBERSHIP_VERSION_CHANGED")
        if member.active and member.role == "owner" and (not command.active or command.role != Role.OWNER):
            owners = self.db.scalars(
                select(Membership).where(
                    Membership.tenant_id == ctx.tenant_id,
                    Membership.role == "owner",
                    Membership.active.is_(True),
                )
            ).all()
            if len(owners) <= 1:
                raise HTTPException(409, "LAST_OWNER")
        member.role, member.active = command.role.value, command.active
        member.version += 1
        return self.audit(
            ctx, "membership.changed", member.id, {"role": member.role, "active": member.active}
        )

    def invite(self, ctx: TenantContext, command: InviteMember) -> str:
        self.authorize(ctx, Permission.SETTINGS)
        code = secrets.token_urlsafe(32)
        invite = Invitation(
            tenant_id=ctx.tenant_id,
            creator_id=ctx.actor_id,
            email=command.email.casefold(),
            role=command.role.value,
            token_hash=hashlib.sha256(code.encode()).hexdigest(),
            expires_at=now() + timedelta(hours=24),
        )
        self.db.add(invite)
        self.db.flush()
        self.audit(ctx, "invitation.created", invite.id)
        return code  # Explicit manual handoff; no notification/email provider invoked.

    def accept(self, subject: AuthenticatedSubject, code: str) -> WorkspaceView:
        invite = self.db.scalar(
            select(Invitation)
            .where(Invitation.token_hash == hashlib.sha256(code.encode()).hexdigest())
            .with_for_update()
        )
        user = self.db.get(User, subject.user_id)
        if (
            invite is None
            or user is None
            or (user.issuer, user.subject) != (subject.issuer, subject.subject)
            or not user.email_verified
            or user.email.casefold() != invite.email
        ):
            raise HTTPException(403, "FORBIDDEN")
        if invite.used_at or invite.expires_at <= now():
            raise HTTPException(409, "INVITATION_EXPIRED_OR_USED")
        # Invitation ceases to authorize when its creator loses active owner authority.
        inviter = TenantContext(
            tenant_id=invite.tenant_id,
            actor_id=invite.creator_id,
            role=Role.OWNER,
            correlation_id=subject.correlation_id,
        )
        tenant = self.authorize(inviter, Permission.SETTINGS)
        existing = self.db.scalar(
            select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == user.id)
        )
        if existing is not None:
            raise HTTPException(409, "MEMBERSHIP_EXISTS")
        self.db.add(Membership(tenant_id=tenant.id, user_id=user.id, role=invite.role))
        invite.used_at = now()
        ctx = TenantContext(
            tenant_id=tenant.id,
            actor_id=user.id,
            role=Role(invite.role),
            correlation_id=subject.correlation_id,
        )
        self.audit(ctx, "invitation.accepted", invite.id)
        return self.view(tenant, Role(invite.role))
