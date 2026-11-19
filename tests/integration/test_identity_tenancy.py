"""Identity/RBAC consumers: removal of membership, permission or CSRF guards must fail."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from boundary_evidence import BoundaryObserver
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from aco.api.app import create_app
from aco.core.settings import Settings

CASES = [
    c
    for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if c["domain"] == "authorization"
]


def test_real_identity_maps_only_authorized_membership() -> None:
    client = TestClient(create_app(Settings()))
    assert client.get("/api/me").status_code == 401
    assert client.get(f"/api/workspaces/{uuid4()}").status_code == 401
    # Offline adapter transport drives the production auth consumer. Actual Keycloak
    # sign-in is independently exercised by onboarding.spec.ts in both OS lanes.
    from identity_support import actor_client

    from aco.features.identity.models import Membership

    with actor_client("viewer") as (authenticated, store, user, workspace):
        assert authenticated.get("/api/me").json()["id"] == str(user.id)
        assert authenticated.get(f"/api/workspaces/{workspace}").status_code == 200
        assert authenticated.get(f"/api/workspaces/{uuid4()}").status_code == 403
        member = store.query(Membership).filter_by(tenant_id=workspace, user_id=user.id).one()
        member.active = False
        store.commit()
        assert authenticated.get(f"/api/workspaces/{workspace}").status_code == 403


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_boundary_role_authorization(case: dict[str, Any], boundary_observer: BoundaryObserver) -> None:
    # This first assertion records a behavioral RED before any new module exists.
    assert TestClient(create_app(Settings())).get("/api/me").status_code == 401
    from identity_support import actor_client
    from sqlalchemy import func, select

    from aco.features.identity.models import AuditEvent

    with actor_client(case["input"]["role"]) as (client, store, user, workspace):
        permissions = case["input"].get("operations", [case["input"].get("permission")])
        before = store.scalar(select(func.count()).select_from(AuditEvent))
        from fastapi import HTTPException
        from starlette.requests import Request

        from aco.features.identity.service import IdentityService, Permission, require_permission

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "headers": [(b"cookie", f"aco_session={client.cookies.get('aco_session')}".encode())],
            }
        )
        subject = asyncio.run(cast(FastAPI, client.app).state.identity.authenticate_request(request))
        ctx = IdentityService(store).resolve_context(subject, workspace)
        view = client.get(f"/api/workspaces/{workspace}")
        assert view.status_code == 200
        actual, statuses = [], []
        for p in permissions:
            try:
                require_permission(ctx, Permission(p))
                actual.append(True)
                statuses.append(200)
            except HTTPException as error:
                actual.append(False)
                statuses.append(error.status_code)
            assert (p in view.json()["permissions"]) == actual[-1]
        assert statuses == [200 if allowed else 403 for allowed in actual]
        expected = case["expected"]
        observed = {
            "allowed": actual if isinstance(expected["allowed"], list) else actual[0],
            "http_status": statuses[0],
        }
        boundary_observer.assert_observed(case["id"], {key: observed[key] for key in expected})
        assert (actual if isinstance(expected["allowed"], list) else actual[0]) == expected["allowed"]
        if "http_status" in expected:
            assert statuses[0] == expected["http_status"]
        assert store.scalar(select(func.count()).select_from(AuditEvent)) == before
        from aco.features.identity.models import Membership

        store.expire_all()
        member = store.query(Membership).filter_by(tenant_id=workspace, user_id=user.id).one()
        assert member.active is True and member.role == case["input"]["role"]
        assert client.get(f"/api/workspaces/{uuid4()}").status_code == 403


def test_roles_cannot_elevate_via_body_or_query() -> None:
    assert TestClient(create_app(Settings())).get("/api/me").status_code == 401
    from identity_support import actor_client

    with actor_client("viewer") as (client, store, user, workspace):
        response = client.patch(
            f"/api/workspaces/{workspace}?role=owner", json={"name": "Stolen", "timezone": "UTC"}
        )
        assert response.status_code == 403
        assert client.get(f"/api/workspaces/{workspace}?role=owner").json()["name"] == "Synthetic"
        assert (
            client.post(
                "/api/workspaces", json={"name": "Bad", "timezone": "UTC", "role": "owner"}
            ).status_code
            == 422
        )


def test_csrf_and_session_expiry() -> None:
    assert TestClient(create_app(Settings())).get("/api/me").status_code == 401
    from datetime import UTC, datetime, timedelta

    from identity_support import actor_client

    from aco.features.identity.models import LoginSession

    with actor_client("owner") as (client, store, user, workspace):
        assert (
            client.post(
                "/api/workspaces",
                json={"name": "bad", "timezone": "UTC"},
                headers={"origin": "https://evil.test"},
            ).status_code
            == 403
        )
        csrf = client.headers.pop("x-csrf-token")
        assert client.post("/api/workspaces", json={"name": "bad", "timezone": "UTC"}).status_code == 403
        client.headers["x-csrf-token"] = csrf
        session = store.query(LoginSession).filter_by(user_id=user.id).one()
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        store.commit()
        assert client.get("/api/me").status_code == 401


def test_create_join_last_owner_audit_and_revoked_membership() -> None:
    assert TestClient(create_app(Settings())).get("/api/me").status_code == 401
    from identity_support import actor_client, add_actor

    from aco.features.identity.models import AuditEvent, Membership, ProviderConnection, Tenant

    with actor_client("owner") as (client, store, user, workspace):
        created = client.post(
            "/api/workspaces", json={"name": "New synthetic", "timezone": "Australia/Melbourne"}
        )
        assert created.status_code == 201
        data = created.json()
        assert data["automation_enabled"] is False and data["outbound_paused"] is True
        assert data["is_demo"] is True and data["timezone"] == "Australia/Melbourne"
        assert store.get(Tenant, data["id"]) is not None
        assert (
            store.query(ProviderConnection).filter_by(tenant_id=data["id"]).one().status == "NOT_CONFIGURED"
        )
        assert (
            client.patch(
                f"/api/workspaces/{workspace}/members/{user.id}",
                json={"role": "viewer", "active": True, "expected_version": 1},
            ).status_code
            == 409
        )
        other, other_client = add_actor(store, cast(FastAPI, client.app), "new@example.test")
        invitation = client.post(
            f"/api/workspaces/{workspace}/invitations", json={"email": other.email, "role": "viewer"}
        )
        assert invitation.status_code == 201
        token = invitation.json()["invite_code"]
        assert client.post("/api/invitations/accept", json={"invite_code": token}).status_code == 403
        accepted = other_client.post("/api/invitations/accept", json={"invite_code": token})
        assert accepted.status_code == 200 and accepted.json()["role"] == "viewer"
        assert other_client.post("/api/invitations/accept", json={"invite_code": token}).status_code == 409
        changed = client.patch(
            f"/api/workspaces/{workspace}/members/{other.id}",
            json={"role": "operator", "active": False, "expected_version": 1},
        )
        assert changed.status_code == 200
        assert changed.json()["audit"]["actor_id"] == str(user.id)
        assert other_client.get(f"/api/workspaces/{workspace}").status_code == 403
        member = store.query(Membership).filter_by(tenant_id=workspace, user_id=other.id).one()
        assert member.active is False
        audit = store.query(AuditEvent).filter_by(id=changed.json()["audit"]["id"]).one()
        assert audit.actor_id == user.id and audit.tenant_id == workspace
        assert (
            client.post("/api/workspaces", json={"name": "bad", "timezone": "Mars/Olympus"}).status_code
            == 422
        )


@pytest.mark.parametrize(
    "role, allowed",
    [
        (
            "owner",
            {
                "invoice_read",
                "task_write",
                "settings",
                "investigate",
                "resolve",
                "resolve_low_risk",
                "approve_external",
                "pause",
                "resume",
                "export",
                "delete_request",
                "delete_authorize",
                "demo_payment",
                "demo_inbound",
                "demo_time",
                "demo_run_due",
                "demo_reset",
            },
        ),
        (
            "finance_manager",
            {
                "invoice_read",
                "task_write",
                "investigate",
                "resolve",
                "resolve_low_risk",
                "approve_external",
                "pause",
                "resume",
                "export",
                "delete_request",
                "demo_payment",
                "demo_inbound",
                "demo_time",
                "demo_run_due",
            },
        ),
        (
            "operator",
            {
                "invoice_read",
                "task_write",
                "investigate",
                "resolve_low_risk",
                "pause",
                "demo_inbound",
                "demo_run_due",
            },
        ),
        ("viewer", {"invoice_read"}),
    ],
)
def test_complete_role_matrix_and_settings_guard(role: str, allowed: set[str]) -> None:
    from identity_support import actor_client

    with actor_client(role) as (client, store, user, workspace):
        assert set(client.get(f"/api/workspaces/{workspace}").json()["permissions"]) == allowed
        result = client.patch(f"/api/workspaces/{workspace}", json={"name": "Saved", "timezone": "UTC"})
        assert result.status_code == (200 if role == "owner" else 403)
        assert client.get(f"/api/workspaces/{workspace}/members").status_code == (
            200 if role == "owner" else 403
        )
        assert client.post(
            f"/api/workspaces/{workspace}/invitations", json={"email": "test@example.test", "role": "owner"}
        ).status_code == (201 if role == "owner" else 403)


def test_concurrent_last_owner_and_no_cross_tenant_member_assignment() -> None:
    import httpx
    from identity_support import actor_client, add_actor

    from aco.features.identity.models import Membership

    with actor_client("owner") as (client, store, user, workspace):
        other, other_client = add_actor(store, cast(FastAPI, client.app), "second@example.test")
        store.add(Membership(tenant_id=workspace, user_id=other.id, role="owner"))
        store.commit()

        async def concurrent_requests() -> list[int]:
            # One application's concurrent requests share its event loop, as in Uvicorn.
            # Two TestClient portals would create two independent event loops for one engine.
            transport = httpx.ASGITransport(app=client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:53000") as caller:

                async def remove_self(c: TestClient, user_id: object) -> int:
                    headers = dict(c.headers)
                    headers["cookie"] = f"aco_session={c.cookies.get('aco_session')}"
                    response = await caller.patch(
                        f"/api/workspaces/{workspace}/members/{user_id}",
                        headers=headers,
                        json={"role": "viewer", "active": True, "expected_version": 1},
                    )
                    return response.status_code

                return list(
                    await asyncio.wait_for(
                        asyncio.gather(remove_self(client, user.id), remove_self(other_client, other.id)),
                        timeout=10,
                    )
                )

        statuses = asyncio.run(concurrent_requests())
        assert sorted(statuses) == [200, 409]
        store.expire_all()
        assert store.query(Membership).filter_by(tenant_id=workspace, role="owner", active=True).count() == 1
        foreign, _ = add_actor(store, cast(FastAPI, client.app), "foreign@example.test")
        owner_client = client if statuses[0] == 409 else other_client
        assert (
            owner_client.patch(
                f"/api/workspaces/{workspace}/members/{foreign.id}",
                json={"role": "owner", "active": True, "expected_version": 1},
            ).status_code
            == 404
        )


def test_stale_context_cannot_reuse_removed_owner_authority() -> None:
    from fastapi import HTTPException
    from identity_support import actor_client

    from aco.core.context import TenantContext
    from aco.core.types import Role
    from aco.features.identity.models import Membership
    from aco.features.identity.service import IdentityService, Permission

    with actor_client("viewer") as (_, store, user, workspace):
        forged = TenantContext(tenant_id=workspace, actor_id=user.id, role=Role.OWNER, correlation_id=uuid4())
        with pytest.raises(HTTPException) as denied:
            IdentityService(store).authorize(forged, Permission.SETTINGS)
        assert denied.value.status_code == 403
        member = store.query(Membership).filter_by(tenant_id=workspace, user_id=user.id).one()
        member.active = False
        store.commit()
        with pytest.raises(HTTPException) as denied:
            IdentityService(store).authorize(forged, Permission.INVOICE_READ)
        assert denied.value.status_code == 403


def test_invalid_invites_and_local_logout_remain_safe_on_provider_failure() -> None:
    from datetime import timedelta

    import httpx2
    from identity_support import actor_client, add_actor

    from aco.features.identity.models import Invitation, LoginSession, now

    with actor_client("owner") as (client, store, user, workspace):
        other, recipient = add_actor(store, cast(FastAPI, client.app), "recipient@example.test")
        code = client.post(
            f"/api/workspaces/{workspace}/invitations", json={"email": other.email, "role": "operator"}
        ).json()["invite_code"]
        other.email_verified = False
        store.commit()
        assert recipient.post("/api/invitations/accept", json={"invite_code": code}).status_code == 403
        other.email_verified = True
        store.query(Invitation).filter_by(tenant_id=workspace).one().expires_at = now() - timedelta(seconds=1)
        store.commit()
        assert recipient.post("/api/invitations/accept", json={"invite_code": code}).status_code == 409
        app = cast(FastAPI, client.app)

        def unavailable(request: Any) -> Any:
            return httpx2.Response(503)

        app.state.identity.oauth_transport = httpx2.MockTransport(unavailable)
        result = client.post("/api/auth/logout")
        assert result.status_code == 200 and result.json() == {"signed_out": True, "provider_revoked": False}
        assert client.get("/api/me").status_code == 401
        store.expire_all()
        assert store.query(LoginSession).filter_by(user_id=user.id).one().revoked is True


def test_identity_migrations_match_models_and_rollback_is_bounded() -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from identity_support import actor_client
    from sqlalchemy import inspect, text

    from aco.features.identity.models import Base
    from alembic import command

    with actor_client("owner") as (client, store, user, workspace):
        connection = store.connection()
        # Library-owned bootstrap tables have their own migration catalog check;
        # canonical ORM tables must still have no migration/model differences.
        worker_tables = {
            "procrastinate_jobs",
            "procrastinate_workers",
            "procrastinate_events",
            "procrastinate_periodic_defers",
            "aco_worker_schema",
        }
        assert all(inspect(connection).has_table(name) for name in worker_tables)
        migration = MigrationContext.configure(
            connection,
            opts={
                "include_name": lambda name, type_, parents: type_ != "table" or name not in worker_tables,
            },
        )
        assert compare_metadata(migration, Base.metadata) == []
        # Exercise actual source import; preservation must cover populated financial
        # records and immutable source/audit history, not only empty DDL.
        from accounting_support import seed

        seeded = seed(client, workspace)
        payment = client.post(
            f"/api/workspaces/{workspace}/demo/accounting/simulate-payment",
            json={
                "provider_connection_id": seeded["provider_connection_id"],
                "external_invoice_id": "INV-101",
                "amount": {"minor_units": 1000, "currency": "AUD", "exponent": 2},
                "operation_id": str(uuid4()),
            },
        )
        assert payment.status_code == 200
        store.commit()

        def retained_snapshot() -> dict[str, object]:
            conn = store.connection()
            current = inspect(conn)
            rows: dict[str, tuple[str, ...]] = {}
            for name in current.get_table_names():
                quoted = conn.dialect.identifier_preparer.quote_identifier(name)
                rows[name] = tuple(
                    store.scalars(
                        text(f"SELECT to_jsonb(row)::text FROM {quoted} row ORDER BY to_jsonb(row)::text")
                    )
                )
            definitions = tuple(
                store.execute(
                    text("""
                SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
                  pg_get_expr(d.adbin,d.adrelid)
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_attribute a ON a.attrelid=c.oid
                LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
                WHERE n.nspname=current_schema() AND c.relkind='r'
                  AND a.attnum>0 AND NOT a.attisdropped
                ORDER BY c.relname,a.attnum
            """)
                )
            )
            constraints = tuple(
                store.execute(
                    text("""
                SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
                FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=current_schema() ORDER BY c.relname,k.conname
            """)
                )
            )
            indexes = tuple(
                store.execute(
                    text("""
                SELECT tablename,indexname,indexdef FROM pg_indexes
                WHERE schemaname=current_schema() ORDER BY tablename,indexname
            """)
                )
            )
            triggers = tuple(
                store.execute(
                    text("""
                SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid)
                FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=current_schema() AND NOT t.tgisinternal
                ORDER BY c.relname,t.tgname
            """)
                )
            )
            return {
                "rows": rows,
                "columns": definitions,
                "constraints": constraints,
                "indexes": indexes,
                "triggers": triggers,
            }

        assert store.scalar(text("SELECT count(*) FROM accounting_events")) > 0
        assert store.scalar(text("SELECT count(*) FROM verified_payment_allocations")) > 0
        before = retained_snapshot()
        store.commit()  # Release read locks before the separate migration connection.
        config = Config("alembic.ini")
        config.set_main_option(
            "sqlalchemy.url",
            cast(Engine, store.get_bind()).url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        with pytest.raises(
            RuntimeError,
            match="^Retained Voice grant, request and join authority requires non-destructive restore$",
        ):
            command.downgrade(config, "base")
        assert retained_snapshot() == before
        store.commit()
        command.upgrade(config, "head")  # Current-head reapplication is a no-op.
        assert retained_snapshot() == before


def test_failed_business_commit_never_returns_success_or_partial_workspace() -> None:
    from identity_support import actor_client
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.orm import Session

    from aco.features.identity.models import AuditEvent, Tenant

    class RejectCommit(Session):
        pass

    @event.listens_for(RejectCommit, "before_commit")
    def unavailable_commit(store: Session) -> None:
        raise RuntimeError("synthetic database commit failure")

    with actor_client("owner") as (client, store, _, _):
        app = cast(FastAPI, client.app)
        app.state.business_sessions = async_sessionmaker(
            app.state.business_sessions.kw["bind"], sync_session_class=RejectCommit, expire_on_commit=False
        )
        caller = TestClient(app, base_url="http://127.0.0.1:53000", raise_server_exceptions=False)
        caller.cookies.update(client.cookies)
        caller.headers.update(client.headers)
        tenants, audits = store.query(Tenant).count(), store.query(AuditEvent).count()
        response = caller.post("/api/workspaces", json={"name": "Must roll back", "timezone": "UTC"})
        assert response.status_code == 500
        store.expire_all()
        assert store.query(Tenant).count() == tenants
        assert store.query(AuditEvent).count() == audits


def test_operator_retains_conditional_low_risk_resolution_not_broad_resolution() -> None:
    from fastapi import HTTPException
    from identity_support import actor_client
    from starlette.requests import Request

    from aco.features.identity.service import IdentityService, Permission, require_permission

    with actor_client("operator") as (client, store, _, workspace):
        response = client.get(f"/api/workspaces/{workspace}")
        assert response.status_code == 200
        assert "resolve_low_risk" in response.json()["permissions"]
        assert "resolve" not in response.json()["permissions"]
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "headers": [(b"cookie", f"aco_session={client.cookies.get('aco_session')}".encode())],
            }
        )
        subject = asyncio.run(cast(FastAPI, client.app).state.identity.authenticate_request(request))
        ctx = IdentityService(store).resolve_context(subject, workspace)
        require_permission(ctx, Permission("resolve_low_risk"))
        with pytest.raises(HTTPException) as forbidden:
            require_permission(ctx, Permission.RESOLVE)
        assert forbidden.value.status_code == 403


def test_automatic_access_refresh_preserves_persisted_session_and_membership() -> None:
    from typing import Any, cast

    import httpx2
    from contracts.test_identity_swappability import identity_shape, login

    from aco.features.identity.models import IdentityProviderEvent, LoginSession, now
    from aco.integrations.oidc.adapter import OIDCAdapter

    with identity_shape("oidc") as (client, store, user, workspace, port, grant):
        login(client, grant)
        before = client.get("/api/me").json()
        adapter = cast(OIDCAdapter, port)
        cookie = client.cookies.get("aco_session")
        assert cookie is not None
        session = store.get(LoginSession, adapter.signer.loads(cookie))
        assert session is not None
        session_id = session.id
        token = adapter.reveal(session.encrypted_token)
        token["expires_at"] = 0
        session.encrypted_token = adapter.protect(token)
        store.commit()
        original = adapter.oauth_transport
        refreshes: list[bool] = []

        def count(request: Any) -> Any:
            result = original.handle_request(request)
            if b"grant_type=refresh_token" in request.content:
                refreshes.append(True)
            return result

        adapter.oauth_transport = httpx2.MockTransport(count)
        result = client.get("/api/me")
        assert result.status_code == 200
        assert result.json() == before and result.json()["id"] == str(user.id)
        assert client.cookies.get("aco_session") == cookie
        assert client.get(f"/api/workspaces/{workspace}").status_code == 200
        assert refreshes == [True]
        store.refresh(session)
        assert session.id == session_id and session.revoked is False
        refreshed = adapter.reveal(session.encrypted_token)
        assert refreshed["expires_at"] > now().timestamp() and "mutation_pending" not in refreshed
        assert store.query(IdentityProviderEvent).filter_by(outcome="PENDING").count() == 0


@pytest.mark.parametrize("second_entry", ["authenticate", "refresh"])
def test_concurrent_request_during_automatic_refresh_waits_instead_of_revoking(second_entry: str) -> None:
    """A browser sends parallel requests; one in-flight refresh must not revoke the session.

    "refresh" models a parallel request that read the expired token before the tombstone
    was written and therefore entered refresh_session itself.
    """
    import threading
    import time
    from typing import Any, cast

    import httpx2
    from contracts.test_identity_swappability import identity_shape, login
    from starlette.requests import Request

    from aco.features.identity.models import IdentityProviderEvent, LoginSession, now
    from aco.integrations.oidc.adapter import OIDCAdapter

    with identity_shape("oidc") as (client, store, user, _, port, grant):
        login(client, grant)
        adapter = cast(OIDCAdapter, port)
        cookie = client.cookies.get("aco_session")
        assert cookie is not None
        session = store.get(LoginSession, adapter.signer.loads(cookie))
        assert session is not None
        token = adapter.reveal(session.encrypted_token)
        token["expires_at"] = 0
        session.encrypted_token = adapter.protect(token)
        store.commit()
        original_transport = adapter.oauth_transport
        refreshes: list[bool] = []

        def count(request: Any) -> Any:
            result = original_transport.handle_request(request)
            if b"grant_type=refresh_token" in request.content:
                refreshes.append(True)
            return result

        adapter.oauth_transport = httpx2.MockTransport(count)

        def browser_request() -> Request:
            return Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/me",
                    "headers": [(b"cookie", f"aco_session={cookie}".encode())],
                }
            )

        concurrent: dict[str, Any] = {}

        def second_request() -> None:
            try:
                if second_entry == "refresh":
                    concurrent["subject"] = adapter.refresh_session(browser_request(), rotate=False)
                else:
                    concurrent["subject"] = adapter._authenticate_request(browser_request())
            except Exception as error:  # recorded for the assertion below
                concurrent["error"] = error

        original_metadata = adapter.metadata
        started: list[threading.Thread] = []

        def metadata_with_concurrent_request() -> Any:
            # The first refresh has durably tombstoned the token and released its row
            # lock; a parallel browser request arrives exactly in that window.
            if not started:
                thread = threading.Thread(target=second_request)
                started.append(thread)
                thread.start()
                time.sleep(0.5)
            return original_metadata()

        adapter.metadata = metadata_with_concurrent_request  # type: ignore[method-assign]
        first = adapter._authenticate_request(browser_request())
        started[0].join(timeout=20)
        assert not started[0].is_alive()
        assert "error" not in concurrent, concurrent.get("error")
        assert first.user_id == user.id and concurrent["subject"].user_id == user.id
        assert refreshes == [True]
        store.expire_all()
        session = store.get(LoginSession, session.id)
        assert session is not None and session.revoked is False
        refreshed = adapter.reveal(session.encrypted_token)
        assert refreshed["expires_at"] > now().timestamp() and "mutation_pending" not in refreshed
        assert store.query(IdentityProviderEvent).filter_by(outcome="PENDING").count() == 0
        assert store.query(IdentityProviderEvent).filter_by(outcome="UNKNOWN_SIDE_EFFECT").count() == 0
