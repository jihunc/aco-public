"""Operator Voice authority reuses canonical budgets; no actual provider access/grants."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from action_support import action_world
from fastapi import FastAPI, HTTPException
from identity_support import add_actor
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.policies.dto import PolicyDefinition, PolicyRule, PolicySave
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion
from aco.features.policies.service import PolicyService
from aco.features.usage.models import BudgetReservation, UsageAttempt


def test_voice_live_grant_is_immutable_attempt_bound_and_preserves_unknown_exposure() -> None:
    from aco.features.voice.live import (
        VoiceLiveAuthorization,
        VoiceLiveConfig,
        VoiceLiveGrant,
        install_voice_live_grant,
    )

    with action_world() as world:
        db = world.store
        old = db.get(CollectionPolicyVersion, world.policy_version_id)
        assert old is not None
        policy = db.get(CollectionPolicy, old.policy_id)
        assert policy is not None
        definition = PolicyDefinition(
            rules=[PolicyRule(id=uuid4(), offset_days=0, channel="voice")],
            voice_enabled=True,
            voice_after_days=0,
        )
        saved = PolicyService(db).save_policy(
            world.ctx,
            PolicySave(
                name=policy.name,
                definition=definition,
                expected_version=policy.version,
                activate=True,
                native_reminders_disabled=True,
                configuration_reviewed=True,
            ),
            policy.version,
            policy.id,
        )
        db.commit()
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        connection = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="vapi",
            provider_type="voice",
            status="NOT_CONFIGURED",
            external_account_id="synthetic-test-account",
            encrypted_credentials_ref="offline-live-code-only",
        )
        db.add(connection)
        db.commit()
        assert isinstance(world.client.app, FastAPI)
        participant, _ = add_actor(db, world.client.app, "participant@synthetic.invalid")
        db.add(Membership(tenant_id=world.ctx.tenant_id, user_id=participant.id, role="finance_manager"))
        db.commit()
        config = VoiceLiveConfig(
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            source_commit="a" * 40,
            account_id="synthetic-test-account",
            assistant_id=uuid4(),
            participant_email="participant@synthetic.invalid",
            participant_approval_ref="Offline explicit participant fixture, not actual consent",
            participant_timezone="UTC",
            no_capture_approval_ref="Offline saved privacy profile fixture, not actual provider verification",
            callback_url="https://voice.example.test/api/voice/callback",
            server_credential_id=uuid4(),
            assistant_profile_sha256="b" * 64,
            binding_sha256="c" * 64,
            policy_version_id=saved.id,
            invoice_ids=[invoice.id],
            expires_at=now() + timedelta(minutes=10),
            max_calls=1,
            max_call_seconds=300,
            max_total_seconds=300,
            max_requests=6,
            max_usd=Decimal("1"),
            all_component_usd_per_second=Decimal("0.001"),
            api_request_usd_ceiling=Decimal("0.01"),
            price_source_ref="Offline verified ceiling fixture only",
            price_source_at=now(),
        )
        grant = install_voice_live_grant(db, world.ctx, connection.id, config)
        db.commit()
        assert grant.participant_id == participant.id
        key = grant.id
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(update(VoiceLiveGrant).where(VoiceLiveGrant.id == key).values(material={}))
        authorization = VoiceLiveAuthorization(
            key,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=lambda: False,
        )
        attempt = UsageAttempt(
            tenant_id=world.ctx.tenant_id,
            provider_connection_id=connection.id,
            actor_id=world.ctx.actor_id,
            correlation_id=world.ctx.correlation_id,
            operation="voice_probe_assistant",
            outcome="PENDING",
        )
        db.add(attempt)
        db.flush()
        foreign = world.ctx.model_copy(update={"correlation_id": uuid4()})
        with pytest.raises(HTTPException) as forged:
            authorization.reserve(db, foreign, "probe_assistant", attempt_id=attempt.id)
        assert forged.value.detail == "VOICE_LIVE_ATTEMPT_REQUIRED"
        reservation = authorization.reserve(db, world.ctx, "probe_assistant", attempt_id=attempt.id)
        assert reservation.status == "UNKNOWN_SIDE_EFFECT" and reservation.retained_amount == Decimal("0.01")
        db.commit()
        with pytest.raises(HTTPException) as replay:
            authorization.reserve(db, world.ctx, "probe_assistant", attempt_id=attempt.id)
        assert replay.value.detail == "VOICE_LIVE_REQUEST_ALREADY_RESERVED"
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(update(VoiceLiveGrant).where(VoiceLiveGrant.id == key).values(connection_version=99))
        db.rollback()
        killed = VoiceLiveAuthorization(
            key,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=lambda: True,
        )
        with pytest.raises(HTTPException) as denied:
            killed.validate(db, world.ctx.tenant_id, connection.id, require_ready=False)
        assert denied.value.detail == "VOICE_LIVE_KILL_SWITCH"
        db.rollback()
        retained = db.scalar(select(BudgetReservation).where(BudgetReservation.id == reservation.id))
        assert (
            retained is not None
            and retained.retained_amount == Decimal("0.01")
            and not retained.automatic_retry
        )
