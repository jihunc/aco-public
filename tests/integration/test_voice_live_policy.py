"""Current Voice policy/source/contact checks consume real operational state, never alter accounting."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from voice_live_support import live_world

from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.identity.models import now
from aco.features.policies.dto import RecipientSave
from aco.features.policies.guards import PolicyGuards
from aco.features.policies.service import PolicyService


def test_live_voice_independently_checks_current_policy_time_holds_and_cross_channel_caps() -> None:
    from aco.features.voice.live_policy import voice_policy_snapshot

    with live_world() as fixture:
        db, world, grant = fixture.world.store, fixture.world, fixture.grant
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        before = (invoice.source_revision, invoice.balance_minor, invoice.financial_status)
        at = now().replace(hour=10, minute=0, second=0, microsecond=0)
        if at.weekday() >= 5:
            at -= timedelta(days=at.weekday() - 4)
        _, reasons = voice_policy_snapshot(db, grant, [invoice.id], at)
        assert "recipient_unverified" in reasons
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
                evidence="Explicit synthetic fixture contact review",
            ),
        )
        db.commit()
        material, reasons = voice_policy_snapshot(db, grant, [invoice.id], at)
        assert not reasons and material["participant_id"] == str(grant.participant_id)
        _, quiet = voice_policy_snapshot(db, grant, [invoice.id], at.replace(hour=20))
        assert "quiet_hours" in quiet
        operation = uuid4()
        reservation = PolicyGuards(db, at).reserve_contact(
            world.ctx, invoice.id, "participant@synthetic.invalid", "email", operation
        )
        assert reservation.channel == "email"
        _, capped = voice_policy_snapshot(db, grant, [invoice.id], at)
        assert "customer_contact_cap" in capped
        _, own = voice_policy_snapshot(db, grant, [invoice.id], at, exclude_operation_id=operation)
        assert not own
        PolicyService(db).place_hold(
            world.ctx, invoice.id, "manual_hold", "Explicit fixture human restriction"
        )
        _, held = voice_policy_snapshot(db, grant, [invoice.id], at, exclude_operation_id=operation)
        assert "manual_hold" in held
        with pytest.raises(HTTPException) as foreign:
            voice_policy_snapshot(db, grant, [uuid4()], at)
        assert foreign.value.detail == "VOICE_LIVE_SCOPE_DENIED"
        db.refresh(invoice)
        assert (invoice.source_revision, invoice.balance_minor, invoice.financial_status) == before
