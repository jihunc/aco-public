"""Canonical request usage + saved-profile READY publication through actual offline HTTP."""

from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from voice_live_support import live_world

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.usage.models import BudgetReservation, ProviderUsageEvent, UsageRevision
from aco.features.voice.live import VoiceLiveAuthorization, VoiceLiveProfile, config_for
from aco.integrations.ports.voice import CallContext, VoiceSetupContext
from aco.integrations.vapi.profile import VapiSavedProfileProbe


@pytest.mark.parametrize(
    "change", ["none", "late_kill", "credential_rotation", "member_version", "accounting_disconnect"]
)
async def test_real_profile_reads_publish_only_exact_successor_and_keep_unknown_cost(change: str) -> None:
    from aco.features.voice.live_usage import VoiceLiveUsageObserver, publish_profile

    with live_world() as fixture:
        world, binding, grant = fixture.world, fixture.binding, fixture.grant
        config = config_for(grant)
        engine = create_async_engine(world.url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        killed = False
        auth = VoiceLiveAuthorization(
            grant.id,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=lambda: killed,
        )
        ctx = VoiceSetupContext(
            tenant_id=world.ctx.tenant_id,
            connection_id=binding.connection_id,
            actor_id=world.ctx.actor_id,
            correlation_id=world.ctx.correlation_id,
            expires_at=now() + timedelta(minutes=1),
            connection_version=grant.connection_version,
            tenant_version=grant.tenant_generation,
            membership_version=grant.membership_version,
            actor_version=grant.actor_version,
            binding_hash=config.binding_sha256,
        )

        def current(db: Session, received: CallContext | VoiceSetupContext, operation: str) -> None:
            assert received == ctx and operation.startswith("probe_")
            auth.validate(db, ctx.tenant_id, ctx.connection_id, require_ready=False)

        async def authority(received: VoiceSetupContext) -> None:
            assert received == ctx
            async with sessions.begin() as db:
                await db.run_sync(lambda store: current(store, received, "probe_assistant"))

        wires: list[str] = []

        def transport(request: httpx.Request) -> httpx.Response:
            wires.append(request.url.path)
            return httpx.Response(
                200,
                json=fixture.profile if request.url.path.startswith("/assistant/") else fixture.credential,
            )

        observer = VoiceLiveUsageObserver(sessions, auth, current=current)
        probe = VapiSavedProfileProbe(binding, observer=observer, transport=httpx.MockTransport(transport))
        # Close fixture's read-only transaction before independent intent/result commits.
        world.store.rollback()
        try:
            proof = await probe.verify(
                ctx,
                authority,
                account_id=config.account_id,
                callback_url=config.callback_url,
                expected_sha256=config.assistant_profile_sha256,
            )
            if change == "late_kill":
                killed = True
            elif change == "credential_rotation":
                rotated = world.store.get(ProviderConnection, binding.connection_id)
                assert rotated is not None
                rotated.encrypted_credentials_ref = "offline-rotated"
                rotated.version += 1
                world.store.commit()
            elif change == "accounting_disconnect":
                invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
                assert invoice is not None
                account = world.store.get(ProviderConnection, invoice.provider_connection_id)
                assert account is not None
                account.status = "DISCONNECTED"
                account.version += 1
                world.store.commit()
            elif change == "member_version":
                member = world.store.scalar(
                    select(Membership).where(
                        Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
                    )
                )
                assert member is not None
                member.version += 1
                world.store.commit()
            async with sessions.begin() as db:
                if change == "none":
                    await db.run_sync(lambda store: publish_profile(store, auth, ctx, proof))
                else:
                    with pytest.raises(HTTPException):
                        await db.run_sync(lambda store: publish_profile(store, auth, ctx, proof))
            world.store.expire_all()
            connection = world.store.get(ProviderConnection, binding.connection_id)
            assert connection is not None
            saved = world.store.scalar(select(VoiceLiveProfile).where(VoiceLiveProfile.grant_id == grant.id))
            if change == "none":
                assert connection.status == "READY" and connection.version == ctx.connection_version + 1
                assert saved is not None and saved.connection_version == connection.version
            else:
                assert connection.status == "NOT_CONFIGURED" and saved is None
            assert len(wires) == 2
            assert (
                world.store.scalar(
                    select(func.count())
                    .select_from(ProviderUsageEvent)
                    .where(ProviderUsageEvent.provider_connection_id == connection.id)
                )
                == 2
            )
            events = list(
                world.store.scalars(
                    select(ProviderUsageEvent).where(
                        ProviderUsageEvent.provider_connection_id == connection.id
                    )
                )
            )
            assert all(not event.synthetic and event.native_operation_id is None for event in events)
            assert {event.native_subject for event in events} == {
                f"assistant:{binding.assistant_id}",
                f"credential:{binding.server_credential_id}",
            }
            revisions = list(
                world.store.scalars(
                    select(UsageRevision).where(UsageRevision.event_id.in_([e.id for e in events]))
                )
            )
            assert all(row.cost_amount is None and row.cost_quality == "UNKNOWN" for row in revisions)
            reservations = list(
                world.store.scalars(
                    select(BudgetReservation).where(BudgetReservation.provider_connection_id == connection.id)
                )
            )
            assert len(reservations) == 2 and all(
                row.status == "UNKNOWN_SIDE_EFFECT" and not row.automatic_retry for row in reservations
            )
            world.store.rollback()
            # The captured setup generation cannot publish a different successor a second time.
            async with sessions.begin() as db:
                with pytest.raises(HTTPException):
                    await db.run_sync(
                        lambda store: publish_profile(
                            store, auth, ctx.model_copy(update={"connection_version": 99}), proof
                        )
                    )
        finally:
            await engine.dispose()
