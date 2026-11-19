"""Local release preparation; never provisions a service or invokes an external provider."""

import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from identity_support import database_url
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from aco.cli import migrate, runtime_settings
from aco.core.settings import Settings
from aco.features.identity.models import Tenant


def test_local_and_railway_use_same_application_contract() -> None:
    configs = {
        name: tomllib.loads(Path(f"infra/railway.{name}.toml").read_text())
        for name in ("api", "worker", "web")
    }
    assert configs["api"]["build"]["dockerfilePath"] == "infra/api.Dockerfile"
    assert configs["worker"]["build"] == configs["api"]["build"]
    assert configs["web"]["build"]["dockerfilePath"] == "infra/web.Dockerfile"
    assert configs["api"]["deploy"]["startCommand"] == "python -m aco.cli api"
    assert configs["worker"]["deploy"]["startCommand"] == "python -m aco.worker"
    assert configs["web"]["deploy"]["startCommand"] == "node web/server.js"
    assert configs["api"]["deploy"]["preDeployCommand"] == ["python -m aco.cli migrate"]
    assert "preDeployCommand" not in configs["worker"]["deploy"]
    assert "preDeployCommand" not in configs["web"]["deploy"]
    assert configs["api"]["deploy"]["healthcheckPath"] == "/health/ready"
    assert configs["web"]["deploy"]["healthcheckPath"] == "/api/health"
    assert "healthcheckPath" not in configs["worker"]["deploy"]
    for name, config in configs.items():
        assert config["build"]["builder"] == "DOCKERFILE"
        assert config["deploy"]["restartPolicyType"] == "ON_FAILURE"
        assert Path(config["build"]["dockerfilePath"]).is_file()
        # No credential defaults, environment injection or public domain provisioning.
        assert set(config) == {"build", "deploy"}, name


def test_deploy_env_has_no_public_secrets_or_open_outreach() -> None:
    settings = runtime_settings(
        {
            "APP_ENV": "production",
            "WEB_PUBLIC_URL": "https://app.example.test",
            "OPENAI_API_KEY": "synthetic-key-not-a-live-selection",
            "RESEND_API_KEY": "synthetic-key-not-a-live-selection",
            "VAPI_API_KEY": "synthetic-key-not-a-live-selection",
            "STORAGE_ACCESS_KEY": "synthetic-admin-never-app",
        }
    )
    assert settings.app_env == "production"
    assert settings.external_providers == settings.llm_provider == "fake"
    assert settings.kill_switch and not settings.outbound_enabled
    assert not settings.recording_enabled and not settings.transcription_enabled
    assert settings.storage_access_key is None
    for content in (Path("infra/api.Dockerfile").read_text(), Path("infra/web.Dockerfile").read_text()):
        assert ".env" not in content and "COPY . ." not in content
    with pytest.raises(ValueError):
        runtime_settings({"APP_ENV": "production", "WEB_PUBLIC_URL": "http://app.example.test"})


def test_production_customer_cannot_use_demo_routes() -> None:
    from action_support import action_world

    from aco.features.accounting.demo_controls import DemoResetCommand, reset_demo_workspace

    with action_world() as world:
        tenant = world.store.get_one(Tenant, world.ctx.tenant_id)
        generation = tenant.version
        world.store.rollback()
        with pytest.raises(HTTPException, match="DEMO_ONLY"):
            reset_demo_workspace(
                world.store,
                world.ctx,
                DemoResetCommand(
                    request_id=uuid4(), expected_generation=generation, evidence="Synthetic test"
                ),
                app_env="production",
            )
        world.store.rollback()
        assert world.store.get_one(Tenant, world.ctx.tenant_id).version == generation


def test_release_migration_waits_for_same_database_schema_lock() -> None:
    """Actual PostgreSQL lock holder, actual Alembic release, fresh disposable schema."""
    base = create_engine(database_url())
    schema = "release_" + uuid4().hex
    app_name = "release_probe_" + uuid4().hex
    with base.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    target = base.url.update_query_dict({"options": f"-csearch_path={schema}", "application_name": app_name})
    target_engine = create_engine(target)
    lock = text("hashtextextended(current_database() || chr(31) || current_schema(), 0)")
    try:
        with target_engine.connect() as owner, ThreadPoolExecutor(max_workers=1) as pool:
            owner.execute(text(f"SELECT pg_advisory_lock({lock.text})"))
            future = pool.submit(
                migrate, Settings(database_url=SecretStr(target.render_as_string(hide_password=False)))
            )
            observed_wait = False
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not future.done():
                    with base.connect() as probe:
                        observed_wait = bool(
                            probe.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                    "WHERE application_name=:app AND wait_event_type='Lock' "
                                    "AND wait_event='advisory')"
                                ),
                                {"app": app_name},
                            )
                        )
                    if observed_wait:
                        break
                    time.sleep(0.05)
            finally:
                owner.execute(text(f"SELECT pg_advisory_unlock({lock.text})"))
                owner.commit()
            future.result(timeout=60)
            assert observed_wait, "Release migration bypassed required same-schema advisory lock"
        with target_engine.connect() as db:
            assert (
                db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            )
            assert db.scalar(text("SELECT count(*) FROM invoice_snapshots")) == 0
        # Releasing the lock is automatic: a subsequent unchanged release succeeds.
        migrate(Settings(database_url=SecretStr(target.render_as_string(hide_password=False))))
    finally:
        target_engine.dispose()
        with base.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        base.dispose()
