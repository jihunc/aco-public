"""Actual isolated grant installers for canonical tenant-ceiling composition probes."""

from datetime import timedelta
from decimal import Decimal

from action_support import World

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.features.agent.live import BoundedLiveGrant, LiveGrantConfig, install_bounded_live_grant
from aco.features.communications.email_live import (
    ApprovedEmailRetention,
    EmailLiveConfig,
    EmailLiveGrant,
    install_email_live_grant,
)
from aco.features.identity.models import ProviderConnection, now


def ai_grant(world: World) -> BoundedLiveGrant:
    return install_bounded_live_grant(
        world.store,
        tenant_id=world.ctx.tenant_id,
        initiator_id=world.ctx.actor_id,
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


def email_grant(world: World) -> EmailLiveGrant:
    email = ProviderConnection(
        tenant_id=world.ctx.tenant_id,
        provider="resend",
        provider_type="email",
        status="READY",
        external_account_id="offline",
        encrypted_credentials_ref="offline",
    )
    storage = ProviderConnection(
        tenant_id=world.ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
    )
    world.store.add_all([email, storage])
    world.store.flush()
    return install_email_live_grant(
        world.store,
        tenant_id=world.ctx.tenant_id,
        initiator_id=world.ctx.actor_id,
        connection_id=email.id,
        storage_connection_id=storage.id,
        account_id="offline",
        credential_revision="offline",
        config=EmailLiveConfig(
            authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
            source_commit="a" * 40,
            mode="OUTBOUND_TEST_ONLY",
            sender="onboarding@resend.dev",
            recipient="delivery-recipient@synthetic.test",
            reply_mailbox=None,
            provider_test_account_email="delivery-recipient@synthetic.test",
            max_received_messages=0,
            participant_approval_ref="offline-participant",
            sender_verification_ref="offline-sender",
            expires_at=now() + timedelta(minutes=10),
            max_requests=3,
            max_messages=1,
            max_usd=Decimal(".03"),
            request_usd_ceiling=Decimal(".01"),
            price_source_ref="offline-price",
            price_source_at=now(),
        ),
        retention=ApprovedEmailRetention(
            approval_ref="offline-retention",
            jurisdiction="offline",
            subject="synthetic",
            purpose="offline-test",
            reviewer_id=world.ctx.actor_id,
            approved_at=now(),
            effective_until=now() + timedelta(hours=1),
            retention_days=1,
            max_bytes=1048576,
        ),
    )
