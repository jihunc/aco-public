"""Normal process composition with genuine disposable grants; no provider wire is made."""

import json
import sys
from datetime import timedelta
from decimal import Decimal
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import httpx2
import pytest
from action_support import action_world
from artifact_support import storage_configuration
from fastapi import FastAPI, HTTPException
from provider_smoke_support import private_config
from sqlalchemy import func, select

from aco import cli
from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.settings import Settings
from aco.features.agent.live import LiveGrantConfig, install_bounded_live_grant
from aco.features.communications.email_live import (
    ApprovedEmailRetention,
    EmailLiveConfig,
    install_email_live_grant,
)
from aco.features.communications.provider_outbound import ProviderOutboundRuntime
from aco.features.identity.models import ProviderConnection, now
from aco.features.usage.models import BudgetReservation
from aco.integrations.openai import application as openai_application
from aco.integrations.openai.investigation import OpenAIInvestigationRuntime
from aco.integrations.provider_smoke.campaign import CampaignAllocation, LiveCampaign
from aco.integrations.resend import application as email_application
from aco.worker import __main__ as worker_main
from aco.worker.app import WorkerRuntime
from aco.worker.jobs import DomainExecutor


@pytest.mark.parametrize("environment", ["local", "demo", "production"])
def test_combined_normal_startup_retains_independent_current_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")
    # Provider SDK seams are explicitly mock-only even though startup must make no wire.
    from aco.integrations.openai.adapter import OpenAIModelAdapter
    from aco.integrations.resend.runtime import ResendRuntime

    wires: list[str] = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        wires.append("resend")
        raise AssertionError("Startup must not make provider requests")

    def forbidden_ai(request: httpx2.Request) -> httpx2.Response:
        wires.append("openai")
        raise AssertionError("Startup must not make provider requests")

    monkeypatch.setattr(
        "aco.integrations.openai.investigation.OpenAIModelAdapter",
        partial(OpenAIModelAdapter, transport_factory=lambda: httpx2.MockTransport(forbidden_ai)),
    )
    monkeypatch.setattr(
        "aco.integrations.resend.outbound.ResendRuntime",
        partial(ResendRuntime, transport_factory=lambda: httpx.MockTransport(forbidden)),
    )
    with action_world() as world:
        db, ctx = world.store, world.ctx
        from aco.features.usage.live_tenant import (
            CompleteLiveUnits,
            ReviewLiveTenantBudget,
            review_live_tenant_budget,
        )

        envelope = review_live_tenant_budget(
            db,
            ctx,
            ReviewLiveTenantBudget(
                authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
                expected_limit_id=None,
                expected_version=None,
                amount=Decimal("1.03"),
                requests=6,
                units=CompleteLiveUnits(
                    requests=6,
                    input_tokens=2048,
                    output_tokens=512,
                    cached_tokens=2048,
                    messages=1,
                    duration_seconds=Decimal(0),
                ),
                review_reason="Isolated normal-process combined provider startup review",
            ),
        )
        email = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            external_account_id="offline-account",
            encrypted_credentials_ref="offline-credential-revision",
        )
        storage = ProviderConnection(
            tenant_id=ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
        )
        db.add_all([email, storage])
        db.flush()
        ai = install_bounded_live_grant(
            db,
            tenant_id=ctx.tenant_id,
            initiator_id=ctx.actor_id,
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            config=LiveGrantConfig(
                model_id="gpt-6.1-sol",
                expires_at=now() + timedelta(minutes=10),
                price_source_at=now(),
                input_usd_per_token=Decimal(".000005"),
                output_usd_per_token=Decimal(".00001"),
                max_requests=3,
                max_input_tokens=2048,
                max_output_tokens=512,
                amount=Decimal("1"),
            ),
        )
        email_config = EmailLiveConfig(
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            source_commit="a" * 40,
            mode="OUTBOUND_TEST_ONLY",
            sender="onboarding@resend.dev",
            recipient="delivery-recipient@synthetic.test",
            reply_mailbox=None,
            max_received_messages=0,
            provider_test_account_email="delivery-recipient@synthetic.test",
            participant_approval_ref="offline-only-participant",
            sender_verification_ref="offline-only-sender",
            expires_at=now() + timedelta(minutes=10),
            max_requests=3,
            max_messages=1,
            max_usd=Decimal(".03"),
            request_usd_ceiling=Decimal(".01"),
            price_source_ref="offline-only-ceiling",
            price_source_at=now(),
        )
        email_grant = install_email_live_grant(
            db,
            tenant_id=ctx.tenant_id,
            initiator_id=ctx.actor_id,
            connection_id=email.id,
            storage_connection_id=storage.id,
            account_id="offline-account",
            credential_revision="offline-credential-revision",
            config=email_config,
            retention=ApprovedEmailRetention(
                approval_ref="offline-only-retention",
                jurisdiction="offline-fixture",
                subject="synthetic",
                purpose="offline startup proof",
                reviewer_id=ctx.actor_id,
                approved_at=now(),
                effective_until=now() + timedelta(hours=1),
                retention_days=1,
                max_bytes=1048576,
            ),
        )
        assert envelope.amount == Decimal("1.03") and envelope.requests == 6
        assert envelope.unit_caps["messages"] == 1 and envelope.unit_caps["input_tokens"] == 2048
        db.commit()
        ledger = tmp_path / "campaign.sqlite3"
        monkeypatch.setattr(
            "aco.integrations.provider_smoke.campaign.canonical_campaign_path", lambda: ledger
        )
        monkeypatch.setattr(openai_application, "current_source_commit", lambda: "a" * 40)
        monkeypatch.setattr(email_application, "current_source_commit", lambda: "a" * 40)
        campaign = LiveCampaign.create(ledger, authorization_ref=DELIVERY_LIVE_AUTHORIZATION)
        ai_allocation = CampaignAllocation(
            id=uuid4(),
            grant_id=ai.id,
            provider="openai",
            source_commit="a" * 40,
            max_usd=Decimal("1"),
            max_requests=3,
        )
        mail_allocation = CampaignAllocation(
            id=uuid4(),
            grant_id=email_grant.id,
            provider="resend",
            source_commit="a" * 40,
            max_usd=Decimal(".03"),
            max_requests=3,
            max_messages=1,
        )
        campaign.allocate(ai_allocation)
        campaign.allocate(mail_allocation)
        credentials = tmp_path / "synthetic-credentials.json"
        credentials.write_text(json.dumps({"api_key": "offline-placeholder"}))
        credentials.chmod(0o600)
        selected = tmp_path / "email-selection.json"
        material = {
            "grant_id": str(email_grant.id),
            "tenant_id": str(ctx.tenant_id),
            "connection_id": str(email.id),
            "authorization_ref": DELIVERY_LIVE_AUTHORIZATION,
            "source_commit": "a" * 40,
            "credential_file": str(credentials),
            "credential_sha256": sha256(credentials.read_bytes()).hexdigest(),
            "kill_switch": False,
            "campaign_file": str(ledger),
            "allocation_id": str(mail_allocation.id),
        }
        selected.write_text(json.dumps(material))
        selected.chmod(0o600)
        ai_path = private_config(tmp_path)
        for name, value in {
            "ACO_AGENT_LIVE_CONFIG_PATH": str(ai_path),
            "ACO_AGENT_LIVE_GRANT_ID": str(ai.id),
            "ACO_LIVE_AUTHORIZATION_REF": DELIVERY_LIVE_AUTHORIZATION,
            "ACO_LIVE_CAMPAIGN_FILE": str(ledger),
            "ACO_LIVE_ALLOCATION_ID": str(ai_allocation.id),
            "ACO_EMAIL_LIVE_CONFIG_PATH": str(selected),
        }.items():
            monkeypatch.setenv(name, value)
        storage_config = storage_configuration()
        settings = (
            Settings.model_validate(
                {
                    **world.client.app.state.settings.model_dump(),
                    "app_env": environment,
                    "web_public_url": "https://app.synthetic.invalid",
                    "synthetic_email_enabled": True,
                    "outbound_enabled": True,
                    "kill_switch": False,
                    "storage_endpoint": storage_config["endpoint"],
                    "storage_bucket": storage_config["bucket"],
                    "storage_access_key": storage_config["access_key"],
                    "storage_secret_key": storage_config["secret_key"],
                    **(
                        {"storage_local_endpoint_ip": storage_config["local_endpoint_ip"]}
                        if "local_endpoint_ip" in storage_config
                        else {}
                    ),
                }
            )
            if environment != "production"
            else Settings(app_env="production", web_public_url="https://app.synthetic.invalid")
        )
        monkeypatch.setattr(cli, "runtime_settings", lambda: settings)
        monkeypatch.setattr(worker_main, "runtime_settings", lambda: settings)
        monkeypatch.setattr(sys, "argv", ["aco", "api"])
        applications: list[FastAPI] = []
        runtimes: list[OpenAIInvestigationRuntime] = []
        constructor = OpenAIInvestigationRuntime

        def observed(*args: Any, **kwargs: Any) -> OpenAIInvestigationRuntime:
            runtime = constructor(*args, **kwargs)
            runtimes.append(runtime)
            return runtime

        monkeypatch.setattr(openai_application, "OpenAIInvestigationRuntime", observed)

        def serve(app: FastAPI, **kwargs: object) -> None:
            applications.append(app)

        monkeypatch.setattr("uvicorn.run", serve)
        workers: list[WorkerRuntime] = []

        async def work(runtime: WorkerRuntime) -> None:
            workers.append(runtime)
            assert isinstance(runtime.executor, DomainExecutor)
            assert runtime.executor.reminders is not None
            assert isinstance(runtime.executor.reminders.email_runtime, ProviderOutboundRuntime)
            await runtime.engine.dispose()

        monkeypatch.setattr(WorkerRuntime, "run", work)
        if environment == "production":
            for start in (cli.main, worker_main.main):
                with pytest.raises(RuntimeError, match="^BOUNDED_LIVE_CONFIGURATION_BLOCKED$"):
                    start()
            assert not applications and not workers and not runtimes
            return
        cli.main()
        worker_main.main()
        assert len(applications) == 1 and len(workers) == 1 and len(runtimes) == 2
        assert applications[0].state.agent_live_grant_id == ai.id
        mail = applications[0].state.ports.email
        assert isinstance(mail, ProviderOutboundRuntime)
        cap = mail.capability(ctx.tenant_id, email.id)
        assert cap.live_grant_id == email_grant.id and cap.destinations == (email_config.recipient,)
        assert cap.mode == "OUTBOUND_TEST_ONLY" and cap.ingress_activation_id is None
        assert all(not runtime.budget.kill_switch() for runtime in runtimes)
        assert all(runtime.grant_id == ai.id for runtime in runtimes)
        # Late shared-ledger failure blocks BOTH independently composed providers.
        ledger.chmod(0o644)
        assert all(runtime.budget.kill_switch() for runtime in runtimes)
        with pytest.raises(HTTPException, match="EMAIL_LIVE_KILL_SWITCH"):
            mail.capability(ctx.tenant_id, email.id)
        with pytest.raises(RuntimeError, match="BOUNDED_LIVE_CONFIGURATION_BLOCKED"):
            cli.main()
        ledger.chmod(0o600)
        # Each private kill remains current, not merely checked on startup.
        original_ai = ai_path.read_text()
        ai_path.write_text(original_ai.replace("ACO_LIVE_KILL_SWITCH=false", "ACO_LIVE_KILL_SWITCH=true"))
        assert all(runtime.budget.kill_switch() for runtime in runtimes)
        with pytest.raises(RuntimeError, match="BOUNDED_LIVE_CONFIGURATION_BLOCKED"):
            worker_main.main()
        ai_path.write_text(original_ai)
        selected.write_text(json.dumps({**material, "kill_switch": True}))
        with pytest.raises(HTTPException, match="EMAIL_LIVE_KILL_SWITCH"):
            mail.capability(ctx.tenant_id, email.id)
        with pytest.raises(RuntimeError, match="BOUNDED_EMAIL_CONFIGURATION_BLOCKED"):
            cli.main()
        selected.write_text(json.dumps(material))
        ai.active = False
        db.commit()
        with pytest.raises(RuntimeError, match="BOUNDED_LIVE_CONFIGURATION_BLOCKED"):
            worker_main.main()
        ai.active = True
        email_grant.revoked_at = now()
        db.commit()
        with pytest.raises(HTTPException, match="EMAIL_LIVE_GRANT_REQUIRED"):
            mail.capability(ctx.tenant_id, email.id)
        with pytest.raises(RuntimeError, match="BOUNDED_EMAIL_CONFIGURATION_BLOCKED"):
            cli.main()
        assert db.scalar(select(func.count()).select_from(BudgetReservation)) == 0
        assert campaign.totals()["reserved_usd"] == "1.03"
        assert wires == []
