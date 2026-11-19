"""Actual isolated state for LIVE-code offline tests; no real participant/provider approval."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from action_support import World, action_world
from fastapi import FastAPI
from identity_support import add_actor
from pydantic import SecretStr
from sqlalchemy import select

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.policies.dto import PolicyDefinition, PolicyRule, PolicySave, RecipientSave
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion
from aco.features.policies.service import PolicyService
from aco.features.voice.live import VoiceLiveConfig, VoiceLiveGrant, install_voice_live_grant
from aco.features.voice.runtime import VoiceRuntime
from aco.integrations.vapi.settings import VapiBinding


@dataclass
class LiveWorld:
    world: World
    grant: VoiceLiveGrant
    binding: VapiBinding
    profile: dict[str, object]
    credential: dict[str, object]


@contextmanager
def live_world(*, unpaused: bool = False) -> Iterator[LiveWorld]:
    with action_world() as world:
        db = world.store
        old = db.get(CollectionPolicyVersion, world.policy_version_id)
        assert old is not None
        policy = db.get(CollectionPolicy, old.policy_id)
        assert policy is not None
        saved = PolicyService(db).save_policy(
            world.ctx,
            PolicySave(
                name=policy.name,
                definition=PolicyDefinition(
                    rules=[PolicyRule(id=uuid4(), offset_days=0, channel="voice")],
                    voice_enabled=True,
                    voice_after_days=0,
                ),
                expected_version=policy.version,
                activate=True,
                native_reminders_disabled=True,
                configuration_reviewed=True,
            ),
            policy.version,
            policy.id,
        )
        db.commit()
        if unpaused:
            invoice = db.get(InvoiceSnapshot, world.invoice_id)
            assert invoice is not None
            contact = db.scalar(
                select(CustomerContact).where(
                    CustomerContact.tenant_id == world.ctx.tenant_id,
                    CustomerContact.customer_id == invoice.customer_id,
                )
            )
            assert contact is not None
            PolicyService(db).save_recipient(
                world.ctx,
                contact.id,
                RecipientSave(
                    expected_version=0,
                    expected_contact_revision=contact.source_revision,
                    expected_destination=contact.email,
                    timezone="UTC",
                    timezone_verified=True,
                    destination_verified=True,
                    allowed_channels=["email", "voice"],
                    evidence="Explicit offline synthetic recipient review before grant installation",
                ),
            )
            db.commit()
            PolicyService(db).set_outbound_pause(world.ctx, "tenant", False)
            db.commit()
        assert isinstance(world.client.app, FastAPI)
        participant, _ = add_actor(db, world.client.app, "participant@synthetic.invalid")
        db.add(Membership(tenant_id=world.ctx.tenant_id, user_id=participant.id, role="finance_manager"))
        connection = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="vapi",
            provider_type="voice",
            status="NOT_CONFIGURED",
            external_account_id="offline-account",
            encrypted_credentials_ref="offline-live-only",
        )
        db.add(connection)
        db.commit()
        binding = VapiBinding(
            tenant_id=world.ctx.tenant_id,
            connection_id=connection.id,
            assistant_id=uuid4(),
            server_credential_id=uuid4(),
            api_key=SecretStr("offline-not-a-key"),
            webhook_secret=SecretStr("offline-signing-secret"),
            credential_revision=connection.encrypted_credentials_ref or "",
            evidence_mode="LIVE",
            control_api_key=SecretStr("offline-public-control-key"),
            control_hosts=("control.vapi.ai",),
        )
        profile: dict[str, object] = {
            "id": str(binding.assistant_id),
            "orgId": "offline-account",
            "server": {
                "url": "https://voice.example.test/api/voice/callback",
                "credentialId": str(binding.server_credential_id),
            },
            "artifactPlan": {
                "recordingEnabled": False,
                "loggingEnabled": False,
                "videoRecordingEnabled": False,
                "transcriptPlan": {"enabled": False},
            },
            "maxDurationSeconds": 300,
            "monitorPlan": {"controlAuthenticationEnabled": True},
        }
        credential: dict[str, object] = {
            "id": str(binding.server_credential_id),
            "orgId": "offline-account",
            "provider": "custom-credential",
            "authenticationPlan": {
                "type": "hmac",
                "algorithm": "sha256",
                "secretKey": "offline-signing-secret",
                "signatureHeader": "x-signature",
                "timestampHeader": "x-timestamp",
                "payloadFormat": "{timestamp}.{body}",
                "signatureEncoding": "hex",
                "includeTimestamp": True,
            },
        }
        assert binding.server_credential_id is not None
        config = VoiceLiveConfig(
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            source_commit="a" * 40,
            account_id="offline-account",
            assistant_id=binding.assistant_id,
            participant_email=participant.email,
            participant_approval_ref="Offline synthetic participant fixture, not real consent",
            participant_timezone="UTC",
            no_capture_approval_ref="Offline profile fixture, not actual provider verification",
            callback_url="https://voice.example.test/api/voice/callback",
            server_credential_id=binding.server_credential_id,
            assistant_profile_sha256=hashlib.sha256(
                json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            binding_sha256=VoiceRuntime.binding_hash(binding),
            policy_version_id=saved.id,
            invoice_ids=[world.invoice_id],
            expires_at=now() + timedelta(minutes=10),
            max_calls=1,
            max_call_seconds=300,
            max_total_seconds=300,
            max_requests=6,
            max_usd=Decimal("1"),
            all_component_usd_per_second=Decimal("0.001"),
            api_request_usd_ceiling=Decimal("0.01"),
            price_source_ref="Offline bounded test rate fixture",
            price_source_at=now(),
        )
        grant = install_voice_live_grant(db, world.ctx, connection.id, config)
        db.commit()
        yield LiveWorld(world, grant, binding, profile, credential)
