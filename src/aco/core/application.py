"""Trusted startup composition shared by normal API and maintained worker entrypoints.

Environment selects only existing operator configuration; credentials never create authority.
No dotenv loading, provider request, grant installation or silent fallback occurs here.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.settings import Settings
from aco.features.voice.runtime import VoiceRuntime
from aco.integrations.openai.application import local_agent_runtime
from aco.integrations.vapi.application import local_voice_runtime

if TYPE_CHECKING:
    from fastapi import FastAPI

    from aco.worker.app import WorkerRuntime


def email_selection(settings: Settings, values: Mapping[str, str | None]) -> Path | None:
    selected = values.get("ACO_EMAIL_LIVE_CONFIG_PATH")
    path = Path(selected) if selected else None
    if path is not None and settings.email_live_config_path not in {None, path}:
        raise ValueError("EMAIL_PRIVATE_CONFIGURATION_REQUIRED")
    return path or settings.email_live_config_path


def voice_selection(settings: Settings, values: Mapping[str, str | None]) -> Path | None:
    selected = values.get("ACO_VOICE_LIVE_CONFIG_PATH")
    path = Path(selected) if selected else None
    if path is not None and settings.voice_live_config_path not in {None, path}:
        raise ValueError("VOICE_PRIVATE_CONFIGURATION_REQUIRED")
    return path or settings.voice_live_config_path


def configured_app(settings: Settings, values: Mapping[str, str | None] | None = None) -> "FastAPI":
    from aco.api.app import create_app

    source = os.environ if values is None else values
    runtime = local_agent_runtime(settings, source)
    email_path = email_selection(settings, source)
    voice_path = voice_selection(settings, source)
    if (runtime is not None or email_path is not None or voice_path is not None) and not (
        settings.database_url
        and settings.identity_discovery_url
        and settings.identity_client_secret
        and settings.session_secret
        and settings.token_encryption_key
    ):
        raise RuntimeError("EXPLICIT_PROVIDER_CONFIGURATION_BLOCKED")

    def selected_voice(sessions: async_sessionmaker[AsyncSession]) -> VoiceRuntime:
        selected = local_voice_runtime(settings, sessions, config_path=voice_path)
        if selected is None:
            raise RuntimeError("VOICE_PRIVATE_CONFIGURATION_REQUIRED")
        return selected

    app = create_app(
        settings,
        email_config_path=email_path,
        voice_runtime_factory=selected_voice if voice_path is not None else None,
    )
    if runtime is not None:
        if not hasattr(app.state, "business_sessions"):
            raise RuntimeError("EXPLICIT_PROVIDER_CONFIGURATION_BLOCKED")
        app.state.agent_live_grant_id = runtime.grant_id
    if (email_path is not None or voice_path is not None) and not hasattr(app.state, "business_sessions"):
        raise RuntimeError("EXPLICIT_PROVIDER_CONFIGURATION_BLOCKED")
    return app


def configured_worker(settings: Settings, values: Mapping[str, str | None] | None = None) -> "WorkerRuntime":
    from aco.worker.app import WorkerRuntime

    source = os.environ if values is None else values
    runtime = local_agent_runtime(settings, source)
    voice_path = voice_selection(settings, source)

    def selected_voice(sessions: async_sessionmaker[AsyncSession]) -> VoiceRuntime:
        selected = local_voice_runtime(settings, sessions, config_path=voice_path)
        if selected is None:
            raise RuntimeError("VOICE_PRIVATE_CONFIGURATION_REQUIRED")
        return selected

    return WorkerRuntime(
        settings,
        email_config_path=email_selection(settings, source),
        voice_runtime_factory=selected_voice if voice_path is not None else None,
        agent_model_factory=None if runtime is None else runtime.model_factory,
        agent_budget=None if runtime is None else runtime.budget,
    )
