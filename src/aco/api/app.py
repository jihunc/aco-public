"""Runtime composition of health, identity and tenant membership boundaries."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from aco.core.logging import runtime_logger
from aco.core.models import register_models
from aco.core.settings import FakePort, Settings, resolve_ports
from aco.integrations.health.probes import dependency_probes

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aco.features.voice.runtime import VoiceRuntime


class LiveHealth(BaseModel):
    status: Literal["alive"] = "alive"


class ReadyHealth(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, str]


async def _send_offline_voice_callback(app: FastAPI, raw: bytes, headers: dict[str, str]) -> None:
    """Deliver only fixed server simulator callbacks to the real ASGI boundary."""
    import httpx

    signed_headers: dict[str, str] = {
        key: headers[key] for key in ("content-type", "x-signature", "x-timestamp") if key in headers
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://aco-offline-voice.invalid",
            timeout=10,
        ) as client:
            response = await client.post(
                "/api/voice/callback",
                content=raw,
                headers=signed_headers,
            )
    except httpx.HTTPError:
        raise HTTPException(503, "VOICE_OFFLINE_CALLBACK_UNAVAILABLE") from None
    if response.status_code != 200:
        raise HTTPException(503, "VOICE_OFFLINE_CALLBACK_UNAVAILABLE")


def create_app(
    settings: Settings,
    *,
    email_config_path: Path | None = None,
    voice_runtime_factory: Callable[["async_sessionmaker[AsyncSession]"], "VoiceRuntime"] | None = None,
) -> FastAPI:
    register_models()
    app = FastAPI(title="ACO")
    logger = runtime_logger()
    app.state.settings = settings
    app.state.ports = resolve_ports(settings)

    @app.get("/health/live", response_model=LiveHealth)
    async def live() -> LiveHealth:
        return LiveHealth()

    @app.get(
        "/health/ready",
        response_model=ReadyHealth,
        responses={503: {"model": ReadyHealth, "description": "Dependency or worker not ready"}},
    )
    async def ready() -> JSONResponse:
        async with dependency_probes(settings) as probes:
            checks = {name: await probe.check() for name, probe in probes.items()}
        logger.info("dependency check completed")
        is_ready = all(value == "READY" for value in checks.values())
        payload = ReadyHealth(status="ready" if is_ready else "not_ready", checks=checks)
        return JSONResponse(payload.model_dump(), status_code=200 if is_ready else 503)

    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from aco.features.accounting.api import router as accounting_router
    from aco.features.accounting.demo_api import router as demo_controls_router
    from aco.features.actions.api import router as actions_router
    from aco.features.artifacts.api import router as artifacts_router
    from aco.features.artifacts.consent_api import router as consent_router
    from aco.features.communications.api import router as communications_router
    from aco.features.communications.association_api import router as communication_association_router
    from aco.features.communications.ingress_api import router as ingress_router
    from aco.features.identity.api import router
    from aco.features.impact.api import router as impact_router
    from aco.features.impact.case_outcome_api import router as case_outcome_router
    from aco.features.impact.cohort_api import router as cohort_router
    from aco.features.impact.economics_api import router as economics_router
    from aco.features.impact.handling_api import router as handling_router
    from aco.features.receivables.api import router as receivables_router
    from aco.features.review.api import router as review_router
    from aco.features.review.approval_api import router as approval_router
    from aco.features.usage.api import router as usage_router
    from aco.features.voice.api import callback_router as voice_callback_router
    from aco.features.voice.api import router as voice_router
    from aco.integrations.demo_accounting.adapter import AccountingRuntime
    from aco.integrations.oidc.adapter import OIDCAdapter
    from aco.integrations.ports.accounting import AccountingFailure
    from aco.integrations.ports.identity import IdentityFailure
    from aco.integrations.ports.storage import StorageFailure

    app.include_router(voice_router)
    app.include_router(consent_router)
    app.include_router(voice_callback_router)
    app.include_router(ingress_router)
    app.include_router(communications_router)
    app.include_router(communication_association_router)
    app.include_router(artifacts_router)

    @app.exception_handler(StorageFailure)
    async def storage_error(request: Request, error: StorageFailure) -> JSONResponse:
        status = 422 if error.failure.code.value in {"VALIDATION_FAILED", "UNSUPPORTED_CAPABILITY"} else 503
        return JSONResponse({"detail": error.failure.code.value}, status_code=status)

    app.include_router(usage_router)
    app.include_router(impact_router)
    app.include_router(cohort_router)
    app.include_router(case_outcome_router)
    app.include_router(handling_router)
    from aco.features.exports.api import router as export_router

    app.include_router(export_router)
    app.include_router(economics_router)
    app.include_router(receivables_router)
    from aco.features.cases.api import router as cases_router
    from aco.features.promises.api import router as promises_router

    app.include_router(promises_router)
    app.include_router(cases_router)
    app.include_router(review_router)
    app.include_router(approval_router)
    app.include_router(router)
    app.include_router(accounting_router)
    app.include_router(demo_controls_router)
    from aco.features.policies.api import router as policies_router

    app.include_router(policies_router)
    app.include_router(actions_router)
    from aco.features.agent.api import router as agent_router

    app.include_router(agent_router)

    @app.exception_handler(AccountingFailure)
    async def accounting_error(request: Request, error: AccountingFailure) -> JSONResponse:
        code = error.failure.code.value
        status = {
            "AUTH_EXPIRED": 401,
            "VALIDATION_FAILED": 422,
            "UNSUPPORTED_CAPABILITY": 422,
            "RATE_LIMITED": 429,
        }.get(code, 503)
        return JSONResponse({"detail": code}, status_code=status)

    if (
        settings.database_url
        and settings.identity_discovery_url
        and settings.identity_client_secret
        and settings.session_secret
        and settings.token_encryption_key
    ):
        engine = None
        try:
            engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
            # Identity offloads its sync SDK; business transactions use the maintained
            # async SQLAlchemy engine/run_sync bridge without loop-bound pooling.
            business_engine = create_async_engine(
                settings.database_url.get_secret_value(), poolclass=NullPool
            )
            sessions = sessionmaker(engine, expire_on_commit=False)
            adapter = OIDCAdapter(settings, sessions)
        except (SQLAlchemyError, ValueError, ImportError):
            if engine is not None:
                engine.dispose()
            # Configuration failure is per-dependency, never a liveness startup failure.
            # Leave the unimplemented identity entry: its routes return sanitized503.
            logger.warning("identity initialization unavailable")
        else:
            app.state.sessions = sessions
            app.state.business_sessions = async_sessionmaker(business_engine, expire_on_commit=False)
            from aco.features.communications.ingress import EmailIngressService

            app.state.email_ingress = EmailIngressService(
                app.state.business_sessions,
                enabled=settings.synthetic_email_ingress_enabled
                and not isinstance(app.state.ports.storage, FakePort),
                default_secret=(
                    settings.synthetic_email_ingress_webhook_secret.get_secret_value()
                    if settings.synthetic_email_ingress_webhook_secret
                    else None
                ),
                storage_runtime=None
                if isinstance(app.state.ports.storage, FakePort)
                else app.state.ports.storage,
                environment=settings.app_env,
            )
            from aco.integrations.demo_email.adapter import DemoEmailRuntime

            app.state.ports = replace(app.state.ports, email=DemoEmailRuntime(app.state.business_sessions))
            if email_config_path is not None:
                from aco.integrations.resend.application import local_email_runtime

                selected_email = local_email_runtime(
                    settings,
                    app.state.business_sessions,
                    None if isinstance(app.state.ports.storage, FakePort) else app.state.ports.storage,
                    config_path=email_config_path,
                )
                if selected_email is None:
                    raise RuntimeError("EXPLICIT_EMAIL_CONFIGURATION_REQUIRED")
                app.state.ports = replace(app.state.ports, email=selected_email.outbound_runtime)
                app.state.email_ingress.provider_ingress = selected_email.ingress_runtime
                app.state.email_ingress.shared_webhooks = dict(selected_email.shared_webhooks)

            from aco.features.voice.runtime import VoiceRuntime
            from aco.integrations.vapi.settings import VapiBinding

            def unavailable_voice_binding(tenant_id: UUID, connection_id: UUID) -> VapiBinding:
                raise LookupError("Voice runtime disabled")

            selected_voice = (
                voice_runtime_factory(app.state.business_sessions) if voice_runtime_factory else None
            )
            if selected_voice is not None:
                app.state.voice_runtime = selected_voice
            elif settings.synthetic_voice_enabled:
                from aco.features.voice.demo import OfflineVoiceDemo

                async def send_voice_callback(raw: bytes, headers: dict[str, str]) -> None:
                    await _send_offline_voice_callback(app, raw, headers)

                app.state.voice_runtime = OfflineVoiceDemo(
                    app.state.business_sessions,
                    capability_secret=settings.session_secret,
                    callback_sender=send_voice_callback,
                    enabled=True,
                    environment=settings.app_env,
                ).runtime
            else:
                app.state.voice_runtime = VoiceRuntime(
                    app.state.business_sessions,
                    unavailable_voice_binding,
                    capability_secret=settings.session_secret,
                    enabled=False,
                )
            app.state.identity = adapter
            if settings.accounting_adapter == "demo":
                app.state.accounting_runtime = AccountingRuntime()
                app.state.ports = replace(app.state.ports, accounting=app.state.accounting_runtime)
            app.state.ports = replace(app.state.ports, identity=adapter)

    @app.exception_handler(IdentityFailure)
    async def identity_error(request: Request, error: IdentityFailure) -> JSONResponse:
        code = error.failure.code.value
        status = {
            "AUTH_EXPIRED": 401,
            "VALIDATION_FAILED": 403,
            "RATE_LIMITED": 429,
            "UNSUPPORTED_CAPABILITY": 422,
        }.get(code, 503)
        return JSONResponse({"detail": code}, status_code=status, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def private_responses(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return app
