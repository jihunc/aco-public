"""Native read subjects are inserted once, never amended on immutable usage events."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from identity_support import actor_client
from sqlalchemy import select
from usage_support import setup

from aco.core.types import CostQuality, ProviderCost
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import UsageRevision
from aco.features.usage.service import UsageService


def test_usage_native_subject_is_initial_identity_and_legacy_none_replay_still_matches() -> None:
    with actor_client("owner") as (_, db, user, tenant):
        ctx, connection, _, _ = setup(db, tenant, user)
        material = ProviderUsageInput(
            provider_connection_id=connection,
            operation_key=str(uuid4()),
            component="request",
            operation="email_submission_get_message",
            provider_kind="llm",
            provider="mock",
            synthetic=True,
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
            units=Units(requests=1),
            native_subject="sent:" + str(uuid4()),
        )
        service = UsageService(db)
        saved = service.record_usage(ctx, material)
        db.commit()
        assert saved.native_subject == material.native_subject
        assert service.record_usage(ctx, material).id == saved.id
        with pytest.raises(HTTPException, match="USAGE_IDENTITY_CONFLICT"):
            service.record_usage(
                ctx,
                material.model_copy(
                    update={"native_subject": "sent:" + str(uuid4()), "reconciliation_key": "later"}
                ),
            )
        legacy = material.model_copy(update={"operation_key": str(uuid4()), "native_subject": None})
        old = service.record_usage(ctx, legacy)
        db.commit()
        revision = db.scalar(select(UsageRevision).where(UsageRevision.event_id == old.id))
        assert revision is not None and "native_subject" not in revision.payload
        assert service.record_usage(ctx, legacy).id == old.id
