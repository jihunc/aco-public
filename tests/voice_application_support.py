"""Private, ephemeral synthetic selection and real canonical campaign for offline Voice tests."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from voice_live_support import LiveWorld

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.settings import Settings
from aco.features.voice.live import config_for
from aco.integrations.provider_smoke import campaign


def selection(fixture: LiveWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Settings]:
    from aco.integrations.vapi import application

    config, binding = config_for(fixture.grant), fixture.binding
    assert binding.control_api_key is not None
    credentials = tmp_path / "synthetic-voice-credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "api_key": binding.api_key.get_secret_value(),
                "webhook_secret": binding.webhook_secret.get_secret_value(),
                "control_api_key": binding.control_api_key.get_secret_value(),
                "capability_secret": "explicit-offline-only-capability-secret-for-test",
                "join_encryption_key": Fernet.generate_key().decode(),
            }
        )
    )
    credentials.chmod(0o600)
    ledger = tmp_path / "synthetic-campaign.sqlite3"
    monkeypatch.setattr(campaign, "canonical_campaign_path", lambda: ledger)
    allocation = campaign.CampaignAllocation(
        id=uuid4(),
        grant_id=fixture.grant.id,
        provider="vapi",
        source_commit=config.source_commit,
        max_usd=config.max_usd,
        max_requests=config.max_requests,
        max_sessions=config.max_calls,
        max_seconds=config.max_total_seconds,
    )
    campaign.LiveCampaign.create(ledger, authorization_ref=DELIVERY_LIVE_AUTHORIZATION).allocate(allocation)
    path = tmp_path / "synthetic-voice-selection.json"
    path.write_text(
        json.dumps(
            {
                "grant_id": str(fixture.grant.id),
                "tenant_id": str(fixture.world.ctx.tenant_id),
                "connection_id": str(binding.connection_id),
                "authorization_ref": DELIVERY_LIVE_AUTHORIZATION,
                "source_commit": config.source_commit,
                "credential_file": str(credentials),
                "credential_sha256": hashlib.sha256(credentials.read_bytes()).hexdigest(),
                "campaign_file": str(ledger),
                "allocation_id": str(allocation.id),
                "control_hosts": list(binding.control_hosts),
                "kill_switch": False,
            }
        )
    )
    path.chmod(0o600)
    # This explicit fake-source seam never installs/changes a real campaign allocation.
    monkeypatch.setattr(application, "current_source_commit", lambda: config.source_commit)
    assert isinstance(fixture.world.client.app, FastAPI)
    return path, Settings.model_validate(
        fixture.world.client.app.state.settings.model_dump()
        | {
            "app_env": "local",
            "external_providers": "fake",
            "synthetic_voice_enabled": False,
            "synthetic_email_enabled": False,
            "voice_live_config_path": path,
            "outbound_enabled": True,
            "kill_switch": False,
        }
    )
