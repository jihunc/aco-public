"""Actual company disconnect retains financial/communication identities; no remote revocation claim."""

from uuid import uuid4

import pytest
from identity_support import actor_client
from reminder_support import dispatch_maintained
from sqlalchemy import select

from aco.features.identity.models import AuditEvent, ProviderConnection


@pytest.mark.parametrize("role", ["owner", "finance_manager", "operator", "viewer"])
@pytest.mark.parametrize("provider_type", ["email", "llm", "voice"])
def test_provider_connection_controls_require_current_owner(role: str, provider_type: str) -> None:
    with actor_client(role) as (client, store, _user, tenant_id):
        connection = ProviderConnection(
            tenant_id=tenant_id,
            provider_type=provider_type,
            provider={"email": "demo_email", "llm": "mock", "voice": "vapi"}[provider_type],
            status="NOT_CONFIGURED",
            encrypted_credentials_ref="private-reference-never-returned",
            external_account_id="synthetic-account",
        )
        store.add(connection)
        store.commit()
        key, version = connection.id, connection.version
        store.rollback()
        base = f"/api/workspaces/{tenant_id}/provider-connections"
        listed = client.get(base)
        expected = 200 if role == "owner" else 403
        assert listed.status_code == expected, listed.text
        disconnected = client.post(base + f"/{key}/disconnect", json={"expected_version": version})
        assert disconnected.status_code == expected, disconnected.text
        if role == "owner":
            assert "private-reference" not in listed.text
            row = disconnected.json()
            assert row["status"] == "DISCONNECTED" and row["remote_revocation"] == "UNKNOWN"
            assert row["version"] == version + 1
            assert (
                client.post(base + f"/{key}/disconnect", json={"expected_version": version}).status_code
                == 409
            )
            assert (
                client.post(base + f"/{uuid4()}/disconnect", json={"expected_version": 1}).status_code == 404
            )
        store.rollback()
        stored = store.get(ProviderConnection, key, populate_existing=True)
        assert stored is not None and stored.encrypted_credentials_ref == "private-reference-never-returned"
        assert stored.status == ("DISCONNECTED" if role == "owner" else "NOT_CONFIGURED")
        audits = list(
            store.scalars(select(AuditEvent).where(AuditEvent.action == "provider.connection_disconnected"))
        )
        assert len(audits) == (1 if role == "owner" else 0)


def test_disconnect_preserves_actual_sent_history_and_fences_future_dispatch() -> None:
    import json

    from reminder_support import reminder_world
    from sqlalchemy import func

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.communications.models import Communication, CommunicationSource, EmailSubmission
    from aco.features.usage.models import ProviderUsageEvent
    from aco.integrations.demo_email.models import NativeEmailOperation

    with reminder_world() as (client, store, ctx, _invoices, _saved):
        from datetime import UTC, datetime

        from aco.features.accounting.models import CustomerContact
        from aco.features.policies.models import RecipientControl

        offset = 12 - datetime.now(UTC).hour
        zone = f"Etc/GMT{-offset:+d}" if offset else "Etc/GMT"
        for contact in store.scalars(
            select(CustomerContact).where(CustomerContact.tenant_id == ctx.tenant_id)
        ):
            control = store.scalar(select(RecipientControl).where(RecipientControl.contact_id == contact.id))
            assert control is not None
            reviewed = client.put(
                f"/api/workspaces/{ctx.tenant_id}/policy-recipients/{contact.id}",
                json={
                    "expected_version": control.version,
                    "expected_contact_revision": contact.source_revision,
                    "expected_destination": contact.email,
                    "timezone": zone,
                    "timezone_verified": True,
                    "owner_confirmed_tenant_timezone": False,
                    "destination_verified": True,
                    "allowed_channels": ["email"],
                    "opt_out": False,
                    "evidence": "Owner reviewed synthetic current noon-zone for offline history proof",
                },
            )
            assert reviewed.status_code == 200, reviewed.text
        store.rollback()
        completed = dispatch_maintained(client, store, ctx)
        assert completed.status == "succeeded", completed.last_error
        message = store.scalar(
            select(Communication).where(Communication.origin_invoice_id == completed.invoice_id)
        )
        assert message is not None
        source = store.scalar(
            select(CommunicationSource).where(CommunicationSource.communication_id == message.id)
        )
        assert source is not None
        message_id, artifact_id, connection_id = (
            message.id,
            source.artifact_id,
            message.provider_connection_id,
        )
        before: dict[str, dict[str, str]] = {}
        for model in (
            InvoiceSnapshot,
            Communication,
            CommunicationSource,
            EmailSubmission,
            ProviderUsageEvent,
        ):
            before[model.__tablename__] = {
                str(row["id"]): json.dumps(dict(row), default=str, sort_keys=True)
                for row in store.execute(select(model.__table__)).mappings()
            }
        native_count = store.scalar(select(func.count()).select_from(NativeEmailOperation))
        store.rollback()
        # Use the actual public policy replacement producer for fresh queued
        # actions; never rewrite an already published Procrastinate job.
        from policy_support import activate, save

        replacement = save(
            client,
            ctx.tenant_id,
            policy_id=_saved["policy_id"],
            name="Retained connection history",
            expected_version=_saved["version"],
            definition=_saved["definition"],
        )
        activate(client, ctx, replacement)
        base = f"/api/workspaces/{ctx.tenant_id}"
        original = client.get(base + f"/artifacts/{artifact_id}/content")
        assert original.status_code == 200
        listed = client.get(base + "/provider-connections")
        assert listed.status_code == 200, listed.text
        connection = next(row for row in listed.json() if row["id"] == str(connection_id))
        disconnected = client.post(
            base + f"/provider-connections/{connection_id}/disconnect",
            json={
                "expected_version": connection["version"],
            },
        )
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["remote_revocation"] == "UNKNOWN"
        assert client.get(base + "/email-execution").json()["activation"]["current"] is False
        assert client.get(base + f"/communications/{message_id}").status_code == 200
        assert client.get(base + f"/artifacts/{artifact_id}/content").content == original.content
        denied = dispatch_maintained(client, store, ctx)
        assert denied.status != "succeeded"
        assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == native_count
        for model in (
            InvoiceSnapshot,
            Communication,
            CommunicationSource,
            EmailSubmission,
            ProviderUsageEvent,
        ):
            after = {
                str(row["id"]): json.dumps(dict(row), default=str, sort_keys=True)
                for row in store.execute(select(model.__table__)).mappings()
            }
            assert all(after[key] == value for key, value in before[model.__tablename__].items())


def test_disconnect_csrf_tenant_and_unsupported_type_do_not_mutate() -> None:
    with actor_client("owner") as (client, store, _user, tenant_id):
        connection = ProviderConnection(tenant_id=tenant_id, provider_type="storage", provider="s3")
        store.add(connection)
        store.commit()
        key, version = connection.id, connection.version
        store.rollback()
        base = f"/api/workspaces/{tenant_id}/provider-connections"
        denied = client.post(base + f"/{key}/disconnect", json={"expected_version": version})
        assert denied.status_code == 409 and denied.json()["detail"] == "PROVIDER_LIFECYCLE_UNAVAILABLE"
        assert (
            client.post(
                base + f"/{key}/disconnect",
                json={"expected_version": version},
                headers={"x-csrf-token": "incorrect"},
            ).status_code
            == 403
        )
        other = client.post("/api/workspaces", json={"name": "Other company", "timezone": "UTC"})
        assert other.status_code == 201
        foreign = f"/api/workspaces/{other.json()['id']}/provider-connections/{key}/disconnect"
        assert client.post(foreign, json={"expected_version": version}).status_code == 404
        store.rollback()
        persisted = store.get(ProviderConnection, key, populate_existing=True)
        assert persisted is not None and persisted.version == version and persisted.status == "NOT_CONFIGURED"
