"""Independent real-DB review of Root's public local composition; zero provider traffic."""

import importlib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from action_support import action_world
from pydantic import SecretStr

from aco.core.settings import Settings
from aco.features.agent.live import AUTHORIZATION, LiveGrantConfig, install_bounded_live_grant
from aco.features.identity.models import now
from aco.integrations.provider_smoke.config import SmokeConfig


def reviewed_composition() -> ModuleType:
    return importlib.import_module("aco.integrations.openai.application")


def test_real_persisted_grant_must_fit_current_private_model_and_every_limit(tmp_path: Path) -> None:
    module = reviewed_composition()
    config = SmokeConfig(
        openai_api_key=SecretStr("synthetic-review-key"),
        resend_api_key=SecretStr(""),
        vapi_api_key=SecretStr(""),
        model="gpt-6.1-sol",
        assistant_id="",
        max_requests=3,
        max_input_tokens=2048,
        max_output_tokens=512,
        max_usd=Decimal("1"),
        source_path=tmp_path / "not-read-by-verify-grant",
    )
    with action_world() as world:
        grant = install_bounded_live_grant(
            world.store,
            tenant_id=world.ctx.tenant_id,
            initiator_id=world.ctx.actor_id,
            authorization_ref=AUTHORIZATION,
            config=LiveGrantConfig(
                model_id=config.model,
                expires_at=now() + timedelta(minutes=10),
                price_source_at=now(),
                input_usd_per_token=Decimal(".000002"),
                output_usd_per_token=Decimal(".000008"),
                max_requests=2,
                max_input_tokens=1024,
                max_output_tokens=256,
                amount=Decimal(".25"),
            ),
        )
        world.store.commit()
        settings = Settings(database_url=SecretStr(world.url))
        module.verify_grant(settings, grant.id, config)
        with pytest.raises(ValueError, match="^CURRENT_BOUNDED_GRANT_REQUIRED$"):
            module.verify_grant(settings, uuid4(), config)
        # Each test changes one current private limit; the genuine durable grant is unchanged.
        for update in (
            {"max_requests": 1},
            {"max_input_tokens": 1023},
            {"max_output_tokens": 255},
            {"max_usd": Decimal(".249999999999")},
            {"model": "different-synthetic-model"},
        ):
            with pytest.raises(ValueError, match="^CURRENT_BOUNDED_GRANT_REQUIRED$"):
                module.verify_grant(settings, grant.id, config.model_copy(update=update))
        grant.active = False
        world.store.commit()
        with pytest.raises(ValueError, match="^CURRENT_BOUNDED_GRANT_REQUIRED$"):
            module.verify_grant(settings, grant.id, config)
        grant.active = True
        grant.expires_at = now() - timedelta(seconds=1)
        world.store.commit()
        with pytest.raises(ValueError, match="^CURRENT_BOUNDED_GRANT_REQUIRED$"):
            module.verify_grant(settings, grant.id, config)
        grant.expires_at = now() + timedelta(minutes=5)
        grant.authorization_ref = "unapproved-synthetic-reference"
        world.store.commit()
        with pytest.raises(ValueError, match="^CURRENT_BOUNDED_GRANT_REQUIRED$"):
            module.verify_grant(settings, grant.id, config)
        assert not config.source_path.exists()


def test_new_campaign_binds_actual_grant_source_and_full_limits_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from provider_smoke_support import private_config
    from sqlalchemy import func, select

    from aco.features.usage.models import BudgetReservation
    from aco.integrations.provider_smoke.campaign import (
        AUTHORIZATION_REF as DELIVERY_AUTHORIZATION,
    )
    from aco.integrations.provider_smoke.campaign import (
        CampaignAllocation,
        LiveCampaign,
    )

    module = reviewed_composition()
    source = "a" * 40
    monkeypatch.setattr(module, "current_source_commit", lambda: source)
    path = private_config(tmp_path)
    ledger = tmp_path / "campaign.sqlite3"
    monkeypatch.setattr("aco.integrations.provider_smoke.campaign.canonical_campaign_path", lambda: ledger)
    campaign = LiveCampaign.create(ledger, authorization_ref=DELIVERY_AUTHORIZATION)
    with action_world() as world:
        grant = install_bounded_live_grant(
            world.store,
            tenant_id=world.ctx.tenant_id,
            initiator_id=world.ctx.actor_id,
            authorization_ref=DELIVERY_AUTHORIZATION,
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
        world.store.commit()
        settings = Settings(database_url=SecretStr(world.url))
        values = {
            "ACO_AGENT_LIVE_GRANT_ID": str(grant.id),
            "ACO_AGENT_LIVE_CONFIG_PATH": str(path),
            "ACO_LIVE_AUTHORIZATION_REF": DELIVERY_AUTHORIZATION,
            "ACO_LIVE_CAMPAIGN_FILE": str(ledger),
            "ACO_LIVE_ALLOCATION_ID": str(uuid4()),
        }
        with pytest.raises(RuntimeError, match="BOUNDED_LIVE_CONFIGURATION_BLOCKED"):
            module.local_agent_runtime(settings, values)
        allocation = CampaignAllocation(
            id=uuid4(),
            grant_id=grant.id,
            provider="openai",
            source_commit=source,
            max_usd=Decimal("1"),
            max_requests=3,
        )
        campaign.allocate(allocation)
        values["ACO_LIVE_ALLOCATION_ID"] = str(allocation.id)
        runtime = module.local_agent_runtime(settings, values)
        assert runtime is not None and not runtime.budget.kill_switch()
        assert runtime.grant_id == grant.id and "fake-secret" not in repr(runtime.settings)
        source = "b" * 40
        assert runtime.budget.kill_switch()
        with pytest.raises(RuntimeError, match="BOUNDED_LIVE_CONFIGURATION_BLOCKED"):
            module.local_agent_runtime(settings, values)
        source = "a" * 40
        assert not runtime.budget.kill_switch()
        ledger.chmod(0o644)
        assert runtime.budget.kill_switch()
        assert world.store.scalar(select(func.count()).select_from(BudgetReservation)) == 0


@pytest.mark.parametrize("entrypoint", ["api", "worker"])
def test_normal_startup_rejects_consumed_legacy_authorization_without_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    import sys

    from provider_smoke_support import private_config
    from sqlalchemy import func, select

    from aco import cli
    from aco.features.usage.models import BudgetReservation
    from aco.worker import __main__ as worker_main

    with action_world() as world:
        grant = install_bounded_live_grant(
            world.store,
            tenant_id=world.ctx.tenant_id,
            initiator_id=world.ctx.actor_id,
            authorization_ref=AUTHORIZATION,
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
        world.store.commit()
        settings = world.client.app.state.settings
        monkeypatch.setenv("ACO_AGENT_LIVE_CONFIG_PATH", str(private_config(tmp_path)))
        monkeypatch.setenv("ACO_AGENT_LIVE_GRANT_ID", str(grant.id))
        monkeypatch.setenv("ACO_LIVE_AUTHORIZATION_REF", AUTHORIZATION)
        monkeypatch.delenv("ACO_LIVE_CAMPAIGN_FILE", raising=False)
        monkeypatch.delenv("ACO_LIVE_ALLOCATION_ID", raising=False)
        monkeypatch.setattr(cli, "runtime_settings", lambda: settings)
        monkeypatch.setattr(worker_main, "runtime_settings", lambda: settings)
        monkeypatch.setattr(sys, "argv", ["aco", "api"])
        # Old code reached runtime construction without the current campaign.
        created: list[bool] = []

        def factory(*args: object) -> None:
            created.append(True)

        monkeypatch.setattr(reviewed_composition(), "OpenAIInvestigationRuntime", factory)
        monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
        if entrypoint == "worker":
            # Stop after startup composition; never claim a real worker job in this refusal test.
            from aco.worker.app import WorkerRuntime

            async def stop(self: WorkerRuntime) -> None:
                await self.engine.dispose()

            monkeypatch.setattr(WorkerRuntime, "run", stop)
        with pytest.raises(RuntimeError, match="^BOUNDED_LIVE_CONFIGURATION_BLOCKED$"):
            (cli.main if entrypoint == "api" else worker_main.main)()
        assert created == []
        assert world.store.scalar(select(func.count()).select_from(BudgetReservation)) == 0


def test_larger_schema_grant_requires_current_directive_and_current_private_limits(tmp_path: Path) -> None:
    from fastapi import HTTPException
    from provider_smoke_support import private_config
    from sqlalchemy import func, select

    from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
    from aco.features.agent.live import BoundedLiveGrant
    from aco.features.usage.models import BudgetReservation
    from aco.integrations.provider_smoke.config import load_config

    path = private_config(tmp_path)
    path.write_text(path.read_text().replace("=2048", "=64000").replace("=512", "=2048"))
    with action_world() as world:
        grant_config = LiveGrantConfig(
            model_id="gpt-6.1-sol",
            expires_at=now() + timedelta(minutes=10),
            price_source_at=now(),
            input_usd_per_token=Decimal(".000005"),
            output_usd_per_token=Decimal(".00001"),
            max_requests=3,
            max_input_tokens=64000,
            max_output_tokens=2048,
            amount=Decimal("1"),
        )
        with pytest.raises(HTTPException, match="BOUNDED_LIVE_AUTHORIZATION_REQUIRED"):
            install_bounded_live_grant(
                world.store,
                tenant_id=world.ctx.tenant_id,
                initiator_id=world.ctx.actor_id,
                authorization_ref=AUTHORIZATION,
                config=grant_config,
            )
        assert world.store.scalar(select(func.count()).select_from(BoundedLiveGrant)) == 0
        grant = install_bounded_live_grant(
            world.store,
            tenant_id=world.ctx.tenant_id,
            initiator_id=world.ctx.actor_id,
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            config=grant_config,
        )
        world.store.commit()
        cfg = load_config(path, authorization_ref=DELIVERY_LIVE_AUTHORIZATION, lane="investigation")
        settings = Settings(database_url=SecretStr(world.url))
        reviewed_composition().verify_grant(settings, grant.id, cfg)
        assert grant.max_input_tokens == 64000
        from aco.features.agent.live import BoundedLiveBudget
        from aco.features.agent.models import AgentRun
        from aco.features.agent.schemas import AgentTrigger
        from aco.features.agent.service import AgentService

        started = AgentService(world.store).start(
            world.ctx,
            AgentTrigger(
                invoice_ids=[world.invoice_id],
                objective="Synthetic schema-bound investigation",
                request_id=uuid4(),
            ),
            live_grant_id=grant.id,
        )
        world.store.commit()
        run = world.store.get(AgentRun, started.id)
        assert run is not None
        policy = BoundedLiveBudget(grant.id, lambda: False).policy(world.store, run)
        assert (policy.max_requests, policy.max_input_tokens, policy.max_output_tokens) == (3, 64000, 2048)
        assert world.store.scalar(select(func.count()).select_from(BudgetReservation)) == 0
        with pytest.raises(ValueError, match="CURRENT_BOUNDED_GRANT_REQUIRED"):
            reviewed_composition().verify_grant(
                settings, grant.id, cfg.model_copy(update={"max_input_tokens": 32000})
            )
