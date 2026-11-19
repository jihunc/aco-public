"""Current identity routes; each business request is one async SQLAlchemy transaction."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.responses import Response

from aco.api.auth import authenticate_request, database, identity
from aco.core.types import Role
from aco.features.identity.connection_api import router as connection_router
from aco.features.identity.models import LoginSession, Membership, Tenant, User
from aco.features.identity.service import (
    AcceptInvitation,
    ChangeMember,
    CreateWorkspace,
    IdentityService,
    InviteMember,
    MemberChangeView,
    MembershipAudit,
    MemberView,
    Permission,
    WorkspaceView,
)
from aco.integrations.ports.identity import AuthenticatedSubject

router = APIRouter()
router.include_router(connection_router)
Subject = Annotated[AuthenticatedSubject, Depends(authenticate_request)]
Database = Annotated[AsyncSession, Depends(database, scope="function")]


@router.get("/api/auth/login")
async def login(request: Request) -> Response:
    return await identity(request).begin_login(request)


@router.get("/api/auth/callback")
async def callback(request: Request) -> Response:
    return await identity(request).complete_login(request)


@router.post("/api/auth/refresh")
async def refresh(request: Request) -> Response:
    return await identity(request).rotate(request)


@router.post("/api/auth/logout")
async def logout(request: Request) -> Response:
    return await identity(request).logout(request)


@router.get("/api/me")
async def me(subject: Subject, db: Database) -> dict[str, object]:
    def read(store: Session) -> dict[str, object]:
        user = store.get(User, subject.user_id)
        session = store.get(LoginSession, subject.session_id)
        if user is None or session is None:
            raise HTTPException(401, "AUTH_EXPIRED")
        service = IdentityService(store)
        rows = store.execute(
            select(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Membership.user_id == subject.user_id, Membership.active.is_(True))
            .order_by(Tenant.created_at, Tenant.id)
        ).all()
        return {
            "id": user.id,
            "email": user.email,
            "email_verified": user.email_verified,
            "csrf_token": session.csrf_token,
            "workspaces": [service.view(t, Role(m.role)) for t, m in rows],
        }

    return await db.run_sync(read)


@router.post("/api/workspaces", status_code=201, response_model=WorkspaceView)
async def create(command: CreateWorkspace, subject: Subject, db: Database) -> WorkspaceView:
    return await db.run_sync(lambda store: IdentityService(store).create_workspace(subject, command))


@router.get("/api/workspaces/{workspace_id}", response_model=WorkspaceView)
async def workspace(workspace_id: UUID, subject: Subject, db: Database) -> WorkspaceView:
    def read(store: Session) -> WorkspaceView:
        service = IdentityService(store)
        ctx = service.resolve_context(subject, workspace_id)
        tenant = store.get(Tenant, workspace_id)
        assert tenant is not None
        return service.view(tenant, ctx.role)

    return await db.run_sync(read)


@router.patch("/api/workspaces/{workspace_id}", response_model=WorkspaceView)
async def update_workspace(
    workspace_id: UUID, command: CreateWorkspace, subject: Subject, db: Database
) -> WorkspaceView:
    def update(store: Session) -> WorkspaceView:
        service = IdentityService(store)
        ctx = service.resolve_context(subject, workspace_id)
        tenant = service.authorize(ctx, Permission.SETTINGS)
        tenant.name, tenant.timezone = command.name, command.timezone
        tenant.version += 1
        service.audit(ctx, "workspace.updated", tenant.id)
        return service.view(tenant, ctx.role)

    return await db.run_sync(update)


@router.get("/api/workspaces/{workspace_id}/members", response_model=list[MemberView])
async def members(workspace_id: UUID, subject: Subject, db: Database) -> list[MemberView]:
    def read(store: Session) -> list[MemberView]:
        service = IdentityService(store)
        ctx = service.resolve_context(subject, workspace_id)
        service.authorize(ctx, Permission.SETTINGS)
        return [
            MemberView(
                user_id=m.user_id, email=u.email, role=Role(m.role), active=m.active, version=m.version
            )
            for m, u in store.execute(
                select(Membership, User)
                .join(User)
                .where(Membership.tenant_id == workspace_id)
                .order_by(User.email, User.id)
            )
        ]

    return await db.run_sync(read)


@router.patch("/api/workspaces/{workspace_id}/members/{user_id}", response_model=MemberChangeView)
async def change_member(
    workspace_id: UUID, user_id: UUID, command: ChangeMember, subject: Subject, db: Database
) -> MemberChangeView:
    def update(store: Session) -> MemberChangeView:
        service = IdentityService(store)
        event = service.change_member(service.resolve_context(subject, workspace_id), user_id, command)
        member = store.scalar(select(Membership).where(Membership.id == event.target_id))
        user = store.get(User, user_id)
        assert member is not None and user is not None
        return MemberChangeView(
            member=MemberView(
                user_id=user.id,
                email=user.email,
                role=Role(member.role),
                active=member.active,
                version=member.version,
            ),
            audit=MembershipAudit(id=event.id, actor_id=event.actor_id, action=event.action),
        )

    return await db.run_sync(update)


@router.post("/api/workspaces/{workspace_id}/invitations", status_code=201)
async def invite(
    workspace_id: UUID, command: InviteMember, subject: Subject, db: Database
) -> dict[str, object]:
    def create_invitation(store: Session) -> dict[str, object]:
        service = IdentityService(store)
        code = service.invite(service.resolve_context(subject, workspace_id), command)
        return {"invite_code": code, "delivery": "MANUAL_ONLY", "expires_in_seconds": 86400}

    return await db.run_sync(create_invitation)


@router.post("/api/invitations/accept", response_model=WorkspaceView)
async def accept(command: AcceptInvitation, subject: Subject, db: Database) -> WorkspaceView:
    return await db.run_sync(lambda store: IdentityService(store).accept(subject, command.invite_code))
