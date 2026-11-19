"""Maintained OIDC/session primitives; no provider token enters business DTOs/browser URLs."""

import json
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import anyio
import httpx
import httpx2
from authlib.common.security import generate_token
from authlib.integrations.httpx_client import OAuth2Client
from authlib.oidc.core import CodeIDToken
from cryptography.fernet import Fernet
from itsdangerous import BadSignature, URLSafeTimedSerializer
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from aco.core.settings import Settings
from aco.core.types import ProviderCapabilities, ProviderErrorCode, ProviderFailure
from aco.features.identity.models import IdentityProviderEvent, LoginAttempt, LoginSession, User, now
from aco.integrations.ports.identity import AuthenticatedSubject, IdentityFailure


class OIDCAdapter:
    # Upper bound a parallel request waits for this process's in-flight refresh.
    REFRESH_WAIT_SECONDS = 10.0

    def __init__(self, settings: Settings, sessions: sessionmaker[Session], oauth_transport: Any = None):
        self.settings, self.sessions = settings, sessions
        self.oauth_transport = oauth_transport
        # Refreshes started by THIS process, keyed by their durable tombstone event.
        # A tombstone without a live local owner (crash, other process) still fails closed.
        self._inflight: dict[UUID, threading.Event] = {}
        self._inflight_lock = threading.Lock()
        assert settings.session_secret and settings.token_encryption_key
        self.signer = URLSafeTimedSerializer(
            settings.session_secret.get_secret_value(), salt="aco.session.v1"
        )
        self.cipher = Fernet(settings.token_encryption_key.get_secret_value().encode())
        self.callback = settings.web_public_url + "/api/auth/callback"
        self.connection_id = uuid5(NAMESPACE_URL, str(settings.identity_issuer))

    async def begin_login(self, request: Request) -> Response:
        return await anyio.to_thread.run_sync(self._begin_login, request)

    async def complete_login(self, request: Request) -> Response:
        return await anyio.to_thread.run_sync(self._complete_login, request)

    async def authenticate_request(self, request: Request) -> AuthenticatedSubject:
        return await anyio.to_thread.run_sync(self._authenticate_request, request)

    async def rotate(self, request: Request) -> Response:
        return await anyio.to_thread.run_sync(self._rotate, request)

    async def logout(self, request: Request) -> Response:
        return await anyio.to_thread.run_sync(self._logout, request)

    async def validate_connection(self) -> bool:
        return await anyio.to_thread.run_sync(self._validate_connection)

    async def describe_capabilities(self) -> ProviderCapabilities:
        return await anyio.to_thread.run_sync(self._describe_capabilities)

    def record(
        self,
        operation: str,
        native_id: str | None = None,
        outcome: str = "VERIFIED",
        correlation_id: UUID | None = None,
        db: Session | None = None,
    ) -> None:
        event = IdentityProviderEvent(
            connection_id=self.connection_id,
            correlation_id=correlation_id or uuid4(),
            operation=operation,
            provider=str(self.settings.identity_issuer),
            native_id=native_id,
            outcome=outcome,
            provider_attempted=True,
        )
        if db is not None:
            db.add(event)
        else:
            with self.sessions.begin() as owned:
                owned.add(event)

    def operation_event(
        self,
        db: Session,
        operation: str,
        event_id: UUID,
        correlation_id: UUID,
        outcome: str,
        native_id: str | None = None,
        provider_attempted: bool = True,
    ) -> None:
        event = db.get(IdentityProviderEvent, event_id)
        if event is None:
            db.add(
                IdentityProviderEvent(
                    id=event_id,
                    connection_id=self.connection_id,
                    correlation_id=correlation_id,
                    operation=operation,
                    provider=str(self.settings.identity_issuer),
                    native_id=native_id,
                    outcome=outcome,
                    provider_attempted=provider_attempted,
                )
            )
        else:
            event.outcome = outcome
            if native_id is not None:
                event.native_id = native_id

    def normalize_failure(self, error: Exception, mutated: bool = False) -> IdentityFailure:
        if isinstance(error, IdentityFailure):
            failure = error.failure
            if mutated and not failure.may_have_side_effect:
                failure = failure.model_copy(update={"retryable": False, "may_have_side_effect": True})
            return IdentityFailure(failure)
        if isinstance(error, (httpx.HTTPError, httpx2.HTTPError)):
            code = ProviderErrorCode.TEMPORARILY_UNAVAILABLE
        else:
            # A local/DB failure after a confirmed provider mutation may have lost its
            # commit result. Never classify it as an invalid identity or replay it.
            code = (
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                if mutated
                else ProviderErrorCode.TEMPORARILY_UNAVAILABLE
            )
        return IdentityFailure(
            ProviderFailure(code=code, retryable=not mutated, may_have_side_effect=mutated)
        )

    def failed_operation(
        self,
        operation: str,
        error: IdentityFailure,
        event_id: UUID,
        correlation_id: UUID,
        session_id: UUID | None = None,
        verified_native_id: str | None = None,
    ) -> None:
        # Called only after rollback. The same attempt row is finalized, never a
        # second failed-operation event. Pending durable state survives cleanup outage.
        try:
            with self.sessions.begin() as db:
                if session_id is not None:
                    session = db.get(LoginSession, session_id, with_for_update=True)
                    if session is not None:
                        session.revoked = True
                self.operation_event(
                    db,
                    operation,
                    event_id,
                    correlation_id,
                    error.failure.code.value,
                    verified_native_id,
                    provider_attempted=False,
                )
        except Exception:
            # DB unavailability cannot be repaired by another blind mutation. For
            # refresh, the prior durable tombstone/intent continues to deny all use.
            pass

    def await_inflight_refresh(self, session_id: UUID, event_id: UUID) -> bool:
        """Wait for this process's own in-flight refresh; True once its tombstone is gone.

        Parallel browser requests observe the durable tombstone while the refresh is
        still running. They must wait rather than revoke. An unknown tombstone (no
        local owner) or one that outlives the bounded wait still returns False, so
        the caller keeps the existing fail-closed reconciliation.
        """
        with self._inflight_lock:
            waiter = self._inflight.get(event_id)
        if waiter is not None:
            waiter.wait(timeout=self.REFRESH_WAIT_SECONDS)
        try:
            with self.sessions.begin() as db:
                session = db.get(LoginSession, session_id, populate_existing=True)
                if session is None:
                    return False
                return self.reveal(session.encrypted_token).get("mutation_pending") != str(event_id)
        except Exception:
            return False

    def reconcile_pending(self, session_id: UUID, event_id: UUID) -> None:
        try:
            with self.sessions.begin() as db:
                session = db.get(LoginSession, session_id, with_for_update=True)
                event = db.get(IdentityProviderEvent, event_id)
                if session is not None:
                    session.revoked = True
                if event is not None and event.outcome == "PENDING":
                    event.outcome = ProviderErrorCode.UNKNOWN_SIDE_EFFECT.value
        except Exception:
            pass  # Durable pending state still denies authentication across restarts.

    def failure(self, code: ProviderErrorCode, retryable: bool = False) -> IdentityFailure:
        return IdentityFailure(ProviderFailure(code=code, retryable=retryable, may_have_side_effect=False))

    def protect(self, payload: dict[str, Any]) -> str:
        return self.cipher.encrypt(json.dumps(payload).encode()).decode()

    def reveal(self, encrypted: str) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.cipher.decrypt(encrypted.encode()))
        return value

    def metadata(self) -> dict[str, Any]:
        try:
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.get(str(self.settings.identity_discovery_url))
                self.check_response(response)
                metadata: dict[str, Any] = response.json()
            if metadata["issuer"] != self.settings.identity_issuer:
                raise self.failure(ProviderErrorCode.VALIDATION_FAILED)
            if "S256" not in metadata.get("code_challenge_methods_supported", []):
                raise self.failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
            # This implementation supports same-origin OIDC endpoints only. Fail closed for other deployments.
            issuer = urlsplit(str(self.settings.identity_issuer))
            for key in (
                "authorization_endpoint",
                "token_endpoint",
                "jwks_uri",
                "introspection_endpoint",
                "revocation_endpoint",
            ):
                endpoint = urlsplit(metadata[key])
                if (endpoint.scheme, endpoint.netloc) != (issuer.scheme, issuer.netloc):
                    raise self.failure(ProviderErrorCode.VALIDATION_FAILED)
            return metadata
        except IdentityFailure:
            raise
        except (httpx.HTTPError, ValueError, KeyError):
            raise self.failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE, True) from None

    def check_response(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        code, retryable = ProviderErrorCode.VALIDATION_FAILED, False
        if response.status_code == 400:
            try:
                if response.json().get("error") in {"invalid_grant", "invalid_client"}:
                    code = ProviderErrorCode.AUTH_EXPIRED
            except (ValueError, AttributeError):
                pass
        if response.status_code == 429:
            code, retryable = ProviderErrorCode.RATE_LIMITED, True
        elif response.status_code in (401, 403):
            code = ProviderErrorCode.AUTH_EXPIRED
        elif response.status_code >= 500:
            code, retryable = ProviderErrorCode.TEMPORARILY_UNAVAILABLE, True
        raw_id = response.headers.get("x-request-id", "")
        request_id = raw_id if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", raw_id) else None
        raw_retry = response.headers.get("retry-after", "")
        retry_after = timedelta(seconds=min(int(raw_retry), 86400)) if raw_retry.isdigit() else None
        raise IdentityFailure(
            ProviderFailure(
                code=code,
                retryable=retryable,
                may_have_side_effect=False,
                request_id=request_id,
                retry_after=retry_after,
            )
        )

    def client(self, **kwargs: Any) -> OAuth2Client:
        assert self.settings.identity_client_secret
        client = OAuth2Client(
            self.settings.identity_client_id,
            self.settings.identity_client_secret.get_secret_value(),
            scope="openid email profile",
            redirect_uri=self.callback,
            code_challenge_method="S256",
            trust_env=False,
            timeout=5,
            transport=self.oauth_transport,
            **kwargs,
        )

        def token_response(response: Any) -> Any:
            self.check_response(response)
            return response

        client.register_compliance_hook("access_token_response", token_response)
        client.register_compliance_hook("refresh_token_response", token_response)
        return client

    @contextmanager
    def token_operation(self) -> Iterator[None]:
        """Token mutations cannot be blindly replayed after a lost server response."""
        try:
            yield
        except IdentityFailure as error:
            if error.failure.code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE:
                error = IdentityFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
                        retryable=False,
                        may_have_side_effect=True,
                        request_id=error.failure.request_id,
                    )
                )
            raise error from None
        except (httpx2.ConnectError, httpx2.ConnectTimeout):
            raise self.failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE, True) from None
        except Exception:
            # A response read/decode or SDK failure after submission cannot establish
            # whether the token was consumed. Known connection failures are above.
            raise IdentityFailure(
                ProviderFailure(
                    code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                )
            ) from None

    def cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=True,
            secure=self.settings.app_env != "local",
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"

    @contextmanager
    def observed_read(self, operation: str) -> Iterator[None]:
        event_id, correlation_id = uuid4(), uuid4()
        # Persist before discovery/introspection. No provider request on failed intent.
        with self.sessions.begin() as evidence:
            self.operation_event(evidence, operation, event_id, correlation_id, "PENDING")
        try:
            yield
            with self.sessions.begin() as evidence:
                self.operation_event(evidence, operation, event_id, correlation_id, "VERIFIED")
        except Exception as error:
            failure = self.normalize_failure(error)
            self.failed_operation(operation, failure, event_id, correlation_id)
            raise failure from None

    def _begin_login(self, request: Request) -> Response:
        with self.observed_read("begin_login"):
            return self.begin_login_result(request)

    def begin_login_result(self, request: Request) -> Response:
        metadata = self.metadata()
        nonce, verifier = generate_token(32), generate_token(64)
        with self.client() as client:
            url, state = client.create_authorization_url(
                metadata["authorization_endpoint"], code_verifier=verifier, nonce=nonce, prompt="login"
            )
        with self.sessions.begin() as db:
            db.add(
                LoginAttempt(
                    state=state,
                    encrypted_payload=self.protect({"nonce": nonce, "verifier": verifier}),
                    expires_at=now() + timedelta(minutes=5),
                )
            )
        response = RedirectResponse(url, status_code=302)
        self.cookie(response, "aco_login", self.signer.dumps({"state": state}), 300)
        return response

    def verify_id(self, token: dict[str, Any], metadata: dict[str, Any], nonce: str | None) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.get(metadata["jwks_uri"])
            self.check_response(response)
        try:
            decoded = jwt.decode(
                token["id_token"], KeySet.import_key_set(response.json()), algorithms=["RS256"]
            )
            params = {"client_id": self.settings.identity_client_id, "access_token": token["access_token"]}
            if nonce is not None:
                params["nonce"] = nonce
            claims = CodeIDToken(
                decoded.claims,
                decoded.header,
                {
                    "iss": {"essential": True, "value": self.settings.identity_issuer},
                    "aud": {"essential": True, "value": self.settings.identity_client_id},
                },
                params,
            )
            claims.validate(leeway=0)
            return dict(claims)
        except (JoseError, KeyError, ValueError, TypeError):
            raise self.failure(ProviderErrorCode.AUTH_EXPIRED) from None

    def _complete_login(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        event_id, correlation_id = uuid4(), uuid4()
        mutated = False
        verified_native_id: str | None = None
        try:
            try:
                cookie = self.signer.loads(request.cookies.get("aco_login", ""), max_age=300)
                if not state or not secrets.compare_digest(cookie["state"], state):
                    raise ValueError("state")
            except (BadSignature, KeyError, TypeError, ValueError):
                raise self.failure(ProviderErrorCode.AUTH_EXPIRED) from None
            # Consume and persist operation identity before exchanging a one-use code.
            with self.sessions.begin() as db:
                attempt = db.scalar(select(LoginAttempt).where(LoginAttempt.state == state).with_for_update())
                if attempt is None or attempt.consumed or attempt.expires_at <= now():
                    raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
                payload = self.reveal(attempt.encrypted_payload)
                attempt.consumed = True
                self.operation_event(db, "complete_login", event_id, correlation_id, "PENDING")
            metadata = self.metadata()
            with self.token_operation(), self.client() as client:
                token = dict(
                    client.fetch_token(
                        metadata["token_endpoint"],
                        authorization_response=self.callback + "?" + request.url.query,
                        state=state,
                        code_verifier=payload["verifier"],
                    )
                )
            mutated = True
            claims = self.verify_id(token, metadata, payload["nonce"])
            verified_native_id = claims["sub"]
            with self.sessions.begin() as db:
                stmt = insert(User).values(
                    issuer=claims["iss"],
                    subject=claims["sub"],
                    email=claims.get("email", ""),
                    email_verified=claims.get("email_verified") is True,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["issuer", "subject"],
                    set_={
                        "email": stmt.excluded.email,
                        "email_verified": stmt.excluded.email_verified,
                        "updated_at": now(),
                    },
                )
                user = db.scalar(stmt.returning(User))
                assert user is not None
                session = LoginSession(
                    user_id=user.id,
                    csrf_token=secrets.token_urlsafe(32),
                    encrypted_token=self.protect(token),
                    expires_at=now() + timedelta(seconds=self.settings.session_ttl_seconds),
                )
                db.add(session)
                db.flush()
                session_id = session.id
                self.operation_event(db, "complete_login", event_id, correlation_id, "VERIFIED", user.subject)
            response = RedirectResponse(self.settings.web_public_url + "/onboarding", status_code=302)
            self.cookie(
                response, "aco_session", self.signer.dumps(str(session_id)), self.settings.session_ttl_seconds
            )
            response.delete_cookie("aco_login", path="/")
            return response
        except Exception as error:
            failure = self.normalize_failure(error, mutated)
            self.failed_operation(
                "complete_login", failure, event_id, correlation_id, verified_native_id=verified_native_id
            )
            raise failure from None

    def session_id(self, request: Request) -> UUID:
        try:
            return UUID(
                self.signer.loads(
                    request.cookies.get("aco_session", ""), max_age=self.settings.session_ttl_seconds
                )
            )
        except (BadSignature, ValueError, TypeError):
            raise self.failure(ProviderErrorCode.AUTH_EXPIRED) from None

    def checked_session(self, db: Session, session_id: UUID, request: Request) -> LoginSession:
        session = db.get(LoginSession, session_id, with_for_update=True)
        if session is None or session.revoked or session.expires_at <= now():
            raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.csrf(request, session)
        return session

    def introspect(self, token: dict[str, Any], metadata: dict[str, Any], subject: str) -> None:
        with self.client(token=token) as client:
            response = client.introspect_token(
                metadata["introspection_endpoint"], token=token["access_token"]
            )
            self.check_response(response)
            active = response.json()
            if active.get("active") is not True or active.get("sub") != subject:
                raise self.failure(ProviderErrorCode.AUTH_EXPIRED)

    def _authenticate_request(self, request: Request, retried: bool = False) -> AuthenticatedSubject:
        event_id, correlation_id = uuid4(), uuid4()
        pending: UUID | None = None
        session_id: UUID | None = None
        verified_native_id: str | None = None
        try:
            session_id = self.session_id(request)
            with self.sessions.begin() as db:
                session = self.checked_session(db, session_id, request)
                token = self.reveal(session.encrypted_token)
                if token.get("mutation_pending"):
                    pending = UUID(token["mutation_pending"])
                    raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
                user = db.get(User, session.user_id)
                assert user is not None
                expired = token.get("expires_at", 0) <= now().timestamp()
                if not expired:
                    self.operation_event(db, "authenticate_request", event_id, correlation_id, "PENDING")
                    token_snapshot = session.encrypted_token
            # Commit intent before discovery/introspection, returning the connection to
            # even a one-connection pool. Reacquire the session fence and reject any
            # intervening rotation/revocation before using the snapshotted token.
            if not expired:
                with self.sessions.begin() as db:
                    session = self.checked_session(db, session_id, request)
                    if session.encrypted_token != token_snapshot:
                        raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
                    user = db.get(User, session.user_id)
                    assert user is not None
                    self.introspect(token, self.metadata(), user.subject)
                    verified_native_id = user.subject
                    self.operation_event(
                        db, "authenticate_request", event_id, correlation_id, "VERIFIED", user.subject
                    )
                    subject = AuthenticatedSubject(
                        user.id, user.issuer, user.subject, session.id, session.created_at, correlation_id
                    )
        except Exception as error:
            if (
                pending is not None
                and session_id is not None
                and not retried
                and self.await_inflight_refresh(session_id, pending)
            ):
                # A parallel request finished this process's refresh; read its result once.
                return self._authenticate_request(request, retried=True)
            failure = self.normalize_failure(error)
            if pending is not None and session_id is not None:
                self.reconcile_pending(session_id, pending)
            self.failed_operation(
                "authenticate_request",
                failure,
                event_id,
                correlation_id,
                verified_native_id=verified_native_id,
            )
            raise failure from None
        if expired:
            refreshed = self.refresh_session(request, rotate=False, retried=retried)
            assert isinstance(refreshed, AuthenticatedSubject)
            return refreshed
        return subject

    def csrf(self, request: Request, session: LoginSession) -> None:
        if request.headers.get("origin") != self.settings.web_public_url or not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf_token
        ):
            raise self.failure(ProviderErrorCode.VALIDATION_FAILED)

    def _rotate(self, request: Request) -> Response:
        response = self.refresh_session(request, rotate=True)
        assert isinstance(response, Response)
        return response

    def refresh_session(
        self, request: Request, rotate: bool, retried: bool = False
    ) -> Response | AuthenticatedSubject:
        operation = "rotate" if rotate else "authenticate_request"
        event_id, correlation_id = uuid4(), uuid4()
        session_id: UUID | None = None
        pending: UUID | None = None
        intent_saved = mutated = False
        # Register before the tombstone can become visible, so any parallel request that
        # observes it in this process finds a waiter; released only after final cleanup.
        done = threading.Event()
        with self._inflight_lock:
            self._inflight[event_id] = done
        try:
            session_id = self.session_id(request)
            with self.sessions.begin() as db:
                old = self.checked_session(db, session_id, request)
                token = self.reveal(old.encrypted_token)
                if token.get("mutation_pending"):
                    pending = UUID(token["mutation_pending"])
                    raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
                user = db.get(User, old.user_id)
                assert user is not None
                native_subject, issuer = user.subject, user.issuer
                # Remove reusable credential material durably BEFORE remote submission.
                # Concurrent requests/new processes see the tombstone and cannot replay.
                old.encrypted_token = self.protect({"mutation_pending": str(event_id)})
                self.operation_event(db, operation, event_id, correlation_id, "PENDING", native_subject)
            intent_saved = True
            metadata = self.metadata()
            with self.sessions.begin() as db:
                old = self.checked_session(db, session_id, request)
                if self.reveal(old.encrypted_token).get("mutation_pending") != str(event_id):
                    raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
                with (
                    self.token_operation(),
                    self.client(token=token) as client,
                ):
                    renewed = dict(
                        client.refresh_token(metadata["token_endpoint"], refresh_token=token["refresh_token"])
                    )
                mutated = True
                claims = self.verify_id(renewed, metadata, None)
                if (claims["iss"], claims["sub"]) != (issuer, native_subject):
                    raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
                self.introspect(renewed, metadata, native_subject)
                if rotate:
                    old.revoked = True
                    new = LoginSession(
                        user_id=old.user_id,
                        csrf_token=secrets.token_urlsafe(32),
                        encrypted_token=self.protect(renewed),
                        expires_at=old.expires_at,
                    )
                    db.add(new)
                    db.flush()
                    response = JSONResponse({"csrf_token": new.csrf_token})
                    self.cookie(
                        response,
                        "aco_session",
                        self.signer.dumps(str(new.id)),
                        max(0, int((new.expires_at - now()).total_seconds())),
                    )
                    result: Response | AuthenticatedSubject = response
                else:
                    old.encrypted_token = self.protect(renewed)
                    result = AuthenticatedSubject(
                        old.user_id, issuer, native_subject, old.id, old.created_at, correlation_id
                    )
                self.operation_event(db, operation, event_id, correlation_id, "VERIFIED", native_subject)
            return result  # Only after the final transaction commit has succeeded.
        except Exception as error:
            if (
                pending is not None
                and session_id is not None
                and not retried
                and self.await_inflight_refresh(session_id, pending)
            ):
                # Another request in this process completed the refresh; reuse its result.
                if rotate:
                    return self.refresh_session(request, rotate=True, retried=True)
                return self._authenticate_request(request, retried=True)
            failure = self.normalize_failure(error, mutated)
            if pending is not None and session_id is not None:
                self.reconcile_pending(session_id, pending)
            self.failed_operation(
                operation, failure, event_id, correlation_id, session_id if intent_saved else None
            )
            raise failure from None
        finally:
            with self._inflight_lock:
                self._inflight.pop(event_id, None)
            done.set()

    def _logout(self, request: Request) -> Response:
        # Local revocation does not depend on remote provider availability.
        session_id = self.session_id(request)
        with self.sessions.begin() as db:
            session = db.scalar(select(LoginSession).where(LoginSession.id == session_id).with_for_update())
            if session is None:
                raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
            self.csrf(request, session)
            session.revoked = True
            token = self.reveal(session.encrypted_token)
        provider_revoked = False
        event_id, correlation_id = uuid4(), uuid4()
        with self.sessions.begin() as evidence:
            self.operation_event(
                evidence,
                "logout",
                event_id,
                correlation_id,
                "PENDING",
                provider_attempted=bool(token.get("refresh_token")),
            )
        try:
            if not token.get("refresh_token"):
                # Pending/uncertain refresh deliberately erased the reusable secret.
                # Local logout still works; remote revocation cannot be asserted.
                raise self.failure(ProviderErrorCode.AUTH_EXPIRED)
            metadata = self.metadata()
            with self.client() as client:
                response = client.revoke_token(
                    metadata["revocation_endpoint"],
                    token=token.get("refresh_token"),
                    token_type_hint="refresh_token",
                )
                self.check_response(response)
                provider_revoked = True
        except Exception:
            pass  # Local session is irrevocably dead; truthfully expose remote revocation status.
        with self.sessions.begin() as evidence:
            self.operation_event(
                evidence,
                "logout",
                event_id,
                correlation_id,
                "VERIFIED" if provider_revoked else "LOCAL_ONLY_REMOTE_UNAVAILABLE",
            )
        response = JSONResponse({"signed_out": True, "provider_revoked": provider_revoked})
        response.delete_cookie("aco_session", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    def _validate_connection(self) -> bool:
        with self.observed_read("validate_connection"):
            self.metadata()
        return True

    def _describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset(
                {
                    "authorization_code",
                    "pkce_s256",
                    "signed_id_token",
                    "server_session",
                    "refresh_rotation",
                    "revocation",
                    "registration_redirect",
                }
            ),
            limits={"session_ttl_seconds": self.settings.session_ttl_seconds},
        )
