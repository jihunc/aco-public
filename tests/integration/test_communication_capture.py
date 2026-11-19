"""Real worker capture grant scope, canonical storage and current revocation."""

from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from reminder_support import dispatch_one, reminder_world
from sqlalchemy import func, select

from aco.features.artifacts.dto import ArtifactWrite
from aco.features.communications.capture import CaptureInvocation, capture_invocation
from aco.features.communications.models import CaptureGrantRevocation
from aco.features.communications.outbound import ReminderDispatcher
from aco.integrations.demo_email.models import NativeEmailInvocation


@pytest.mark.parametrize("revoked", [False, True])
def test_capture_requires_actual_job_exact_object_and_current_grant(monkeypatch: Any, revoked: Any) -> None:
    original = ReminderDispatcher.archive
    checks = []
    with reminder_world() as (client, store, ctx, invoices, saved):

        async def archive(dispatcher: Any, worker: Any, binding_id: Any, grant_id: Any) -> Any:
            async with dispatcher.sessions.begin() as db:
                invocation = await db.run_sync(lambda session: capture_invocation(session, worker, grant_id))
                if revoked:
                    db.add(
                        CaptureGrantRevocation(
                            tenant_id=ctx.tenant_id,
                            grant_id=grant_id,
                            reason="Capture withdrawn before SDK invocation",
                        )
                    )
            command = ArtifactWrite(
                artifact_id=uuid4(),
                source_kind="provider_message",
                source_reference="outside-grant",
                content_type="message/rfc822",
                body=b"unauthorized",
                capture_class="synthetic_email",
            )
            with pytest.raises(HTTPException) as error:
                await dispatcher.artifacts.store_artifact(worker, command)
            assert error.value.detail == "HUMAN_STORAGE_CONTEXT_REQUIRED"
            with pytest.raises(HTTPException) as error:
                await dispatcher.artifacts.store_ingested_artifact(
                    CaptureInvocation(invocation.context, object()), grant_id, command
                )
            assert error.value.detail == "AUTHENTICATED_CAPTURE_INVOCATION_REQUIRED"
            with pytest.raises(HTTPException) as error:
                await dispatcher.artifacts.store_ingested_artifact(invocation, grant_id, command)
            assert error.value.detail == (
                "CAPTURE_GRANT_REVOKED" if revoked else "CAPTURE_OBJECT_OUTSIDE_GRANT"
            )
            checks.append(True)
            await original(dispatcher, worker, binding_id, grant_id)

        monkeypatch.setattr(ReminderDispatcher, "archive", archive)
        action = dispatch_one(client, store, ctx)
        assert checks == [True]
        assert action.status == ("failed" if revoked else "succeeded"), action.last_error
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == (0 if revoked else 1)
        assert client.get(f"/api/workspaces/{uuid4()}/communications").status_code == 403
        assert client.get(f"/api/workspaces/{ctx.tenant_id}/communications/{uuid4()}").status_code == 404
