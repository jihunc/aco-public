"""Independent test-only IdentityPort: opaque grants, no OIDC SDK or password implementation.

Never selected by application settings. Provider state is deliberately in memory; canonical
application sessions and provenance use the real database. It proves consumer substitution,
not authentication security, a second live provider, or production persistence of grants.
"""

import secrets
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from aco.core.types import ProviderCapabilities, ProviderErrorCode, ProviderFailure
from aco.features.identity.models import IdentityProviderEvent, LoginSession, User, now
from aco.integrations.ports.identity import AuthenticatedSubject, IdentityFailure


class SyntheticGrantIdentity:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], user_id: UUID, origin: str):
        self.sessions, self.user_id, self.origin = sessions, user_id, origin
        self.attempts: dict[str, datetime] = {}
        self.grants: dict[str, str] = {}
        self.cookies: dict[str, UUID] = {}
        self.available = True

    def reject(self, code: ProviderErrorCode = ProviderErrorCode.AUTH_EXPIRED) -> IdentityFailure:
        return IdentityFailure(
            ProviderFailure(
                code=code,
                retryable=code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                may_have_side_effect=False,
            )
        )

    def check_available(self) -> None:
        if not self.available:
            raise self.reject(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)

    def issue_grant(self, state: str) -> str:
        """Explicit test-provider approval, outside the IdentityPort/HTTP interface."""
        code = secrets.token_urlsafe(32)
        self.grants[code] = state
        return code

    def record(self, db: AsyncSession, operation: str, user: User | None = None) -> UUID:
        correlation = uuid4()
        db.add(
            IdentityProviderEvent(
                connection_id=uuid5(NAMESPACE_URL, "synthetic-grant-provider"),
                correlation_id=correlation,
                operation=operation,
                provider="synthetic-grant-provider",
                native_id=user.subject if user else None,
                outcome="VERIFIED",
            )
        )
        return correlation

    async def begin_login(self, request: Request) -> Response:
        self.check_available()
        state = secrets.token_urlsafe(32)
        self.attempts[state] = now() + timedelta(minutes=5)
        async with self.sessions.begin() as db:
            self.record(db, "begin_login")
        response = RedirectResponse("https://synthetic.invalid/approve?state=" + state, status_code=302)
        response.set_cookie("aco_login", state, httponly=True, samesite="lax", max_age=300)
        return response

    async def complete_login(self, request: Request) -> Response:
        self.check_available()
        state = request.query_params.get("state", "")
        grant = self.grants.pop(request.query_params.get("code", ""), None)
        expiry = self.attempts.pop(state, None)
        if grant != state or request.cookies.get("aco_login") != state or expiry is None or expiry <= now():
            raise self.reject()
        async with self.sessions.begin() as db:
            user = await db.get(User, self.user_id)
            assert user is not None
            session = self.new_session(user.id, now() + timedelta(hours=1))
            db.add(session)
            await db.flush()
            self.record(db, "complete_login", user)
            response = RedirectResponse(self.origin + "/onboarding", status_code=302)
            self.set_session(response, session.id)
        response.delete_cookie("aco_login")
        return response

    def new_session(self, user_id: UUID, expires_at: datetime) -> LoginSession:
        return LoginSession(
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            encrypted_token="SYNTHETIC_NO_PROVIDER_CREDENTIAL",
            expires_at=expires_at,
        )

    def set_session(self, response: Response, session_id: UUID) -> None:
        opaque = secrets.token_urlsafe(32)
        self.cookies[opaque] = session_id
        response.set_cookie("aco_session", opaque, httponly=True, samesite="lax")

    async def session(self, db: AsyncSession, request: Request) -> LoginSession:
        session_id = self.cookies.get(request.cookies.get("aco_session", ""))
        value = await db.get(LoginSession, session_id, with_for_update=True) if session_id else None
        if value is None or value.revoked or value.expires_at <= now():
            raise self.reject()
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("origin") != self.origin
            or request.headers.get("x-csrf-token") != value.csrf_token
        ):
            raise self.reject(ProviderErrorCode.VALIDATION_FAILED)
        return value

    async def authenticate_request(self, request: Request) -> AuthenticatedSubject:
        self.check_available()
        async with self.sessions.begin() as db:
            session = await self.session(db, request)
            user = await db.get(User, session.user_id)
            assert user is not None
            correlation = self.record(db, "authenticate_request", user)
            return AuthenticatedSubject(
                user.id, user.issuer, user.subject, session.id, session.created_at, correlation
            )

    async def rotate(self, request: Request) -> Response:
        self.check_available()
        async with self.sessions.begin() as db:
            old = await self.session(db, request)
            user = await db.get(User, old.user_id)
            assert user is not None
            old.revoked = True
            new = self.new_session(old.user_id, old.expires_at)
            db.add(new)
            await db.flush()
            self.record(db, "rotate", user)
            response = JSONResponse({"csrf_token": new.csrf_token})
            self.set_session(response, new.id)
            return response

    async def logout(self, request: Request) -> Response:
        async with self.sessions.begin() as db:
            session = await self.session(db, request)
            session.revoked = True
            self.record(db, "logout")
        response = JSONResponse({"signed_out": True, "provider_revoked": self.available})
        response.delete_cookie("aco_session")
        return response

    async def validate_connection(self) -> bool:
        self.check_available()
        async with self.sessions.begin() as db:
            self.record(db, "validate_connection")
        return True

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset({"synthetic_grant", "server_session", "refresh_rotation", "revocation"}),
            limits={"session_ttl_seconds": 3600},
        )
