"""Explicit server-only composition for one authorized synthetic OpenAI investigation."""

import os
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.models import register_models
from aco.core.settings import Settings
from aco.features.agent.live import BoundedLiveGrant
from aco.features.identity.models import now
from aco.integrations.openai.investigation import OpenAIInvestigationRuntime
from aco.integrations.openai.settings import OpenAISettings
from aco.integrations.provider_smoke.campaign import verify_grant_allocation
from aco.integrations.provider_smoke.config import SmokeConfig, load_config
from aco.integrations.provider_smoke.source import current_source_commit


def verify_grant(settings: Settings, grant_id: UUID, config: SmokeConfig) -> None:
    if settings.database_url is None:
        raise ValueError("DATABASE_REQUIRED")
    register_models()
    engine = create_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
    try:
        with Session(engine) as db:
            grant = db.get(BoundedLiveGrant, grant_id)
            if (
                grant is None
                or not grant.active
                or grant.expires_at <= now()
                or grant.authorization_ref != config.authorization_ref
                or grant.model_id != config.model
                or grant.max_requests > config.max_requests
                or grant.max_input_tokens > config.max_input_tokens
                or grant.max_output_tokens > config.max_output_tokens
                or grant.amount > config.max_usd
            ):
                raise ValueError("CURRENT_BOUNDED_GRANT_REQUIRED")
    finally:
        engine.dispose()


def local_agent_runtime(
    settings: Settings, values: Mapping[str, str | None] | None = None
) -> OpenAIInvestigationRuntime | None:
    source = values if values is not None else os.environ
    selected = source.get("ACO_AGENT_LIVE_GRANT_ID")
    path = source.get("ACO_AGENT_LIVE_CONFIG_PATH")
    if not selected and not path:
        # Default application/tests never read .env.live, even when credentials exist.
        return None
    try:
        if not selected or not path:
            raise ValueError("EXPLICIT_PATH_AND_GRANT_REQUIRED")
        if settings.app_env not in {"local", "demo"}:
            raise ValueError("SYNTHETIC_DEMO_ENVIRONMENT_REQUIRED")
        # Global email enablement is not model authority. Independently selected
        # providers retain separate grants, approvals, destination guards and kills.
        if source.get("ACO_LIVE_AUTHORIZATION_REF") != DELIVERY_LIVE_AUTHORIZATION:
            raise ValueError("CURRENT_CAMPAIGN_AUTHORIZATION_REQUIRED")
        grant_id = UUID(selected)
        config = load_config(
            Path(path),
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            lane="investigation",
        )
        if not config.model or not config.openai_api_key.get_secret_value():
            raise ValueError("MODEL_AND_KEY_REQUIRED")
        verify_grant(settings, grant_id, config)
        expected_source = None
        campaign_file = None
        allocation_id = None
        if config.authorization_ref == DELIVERY_LIVE_AUTHORIZATION:
            if not source.get("ACO_LIVE_CAMPAIGN_FILE") or not source.get("ACO_LIVE_ALLOCATION_ID"):
                raise ValueError("LIVE_CAMPAIGN_REQUIRED")
            campaign_file = Path(source["ACO_LIVE_CAMPAIGN_FILE"] or "")
            allocation_id = UUID(source["ACO_LIVE_ALLOCATION_ID"] or "")
            expected_source = current_source_commit()

        def campaign_check() -> None:
            if expected_source is None:
                return
            if current_source_commit() != expected_source or campaign_file is None or allocation_id is None:
                raise ValueError("LIVE_SOURCE_CHANGED")
            verify_grant_allocation(
                campaign_file,
                allocation_id,
                grant_id=grant_id,
                provider="openai",
                source_commit=expected_source,
                max_requests=config.max_requests,
                max_usd=config.max_usd,
            )

        campaign_check()

        def killed() -> bool:
            try:
                campaign_check()
                current = load_config(
                    config.source_path, authorization_ref=config.authorization_ref, lane="investigation"
                )
                return current != config
            except Exception:
                # A kill, malformed/missing file, rotation or changed cap requires operator review.
                return True

        return OpenAIInvestigationRuntime(
            OpenAISettings(
                api_key=config.openai_api_key,
                model_id=config.model,
                model_route="investigation",
                features=frozenset({"structured_output", "tool_calling"}),
            ),
            grant_id,
            killed,
        )
    except Exception:
        # Configuration/provider/database details and credentials are never log messages.
        raise RuntimeError("BOUNDED_LIVE_CONFIGURATION_BLOCKED") from None
