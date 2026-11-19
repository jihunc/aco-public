"""Synthetic setup through actual persisted policy and accounting services."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from accounting_support import context, seed
from identity_support import actor_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import CustomerContact, InvoiceSnapshot

AT = datetime(2026, 11, 17, 0, tzinfo=UTC)


def definition(**changes: Any) -> dict[str, Any]:
    return {
        "rules": [
            {
                "id": str(uuid4()),
                "offset_days": 3,
                "local_time": "09:00",
                "channel": "email",
                "template_id": "routine",
            }
        ],
        **changes,
    }


def save(client: Any, tenant: Any, **changes: Any) -> dict[str, Any]:
    body = {
        "name": "Editable collections",
        "expected_version": 0,
        "definition": definition(),
        "activate": False,
        **changes,
    }
    policy_id = body.pop("policy_id", None)
    path = f"/api/workspaces/{tenant}/policies" + (f"/{policy_id}" if policy_id else "")
    response = client.request("PUT" if policy_id else "POST", path, json=body)
    assert response.status_code == (200 if policy_id else 201), response.text
    return cast(dict[str, Any], response.json())


@contextmanager
def policy_world() -> Iterator[tuple[Any, Session, TenantContext, list[InvoiceSnapshot], dict[str, Any]]]:
    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        invoices = list(
            store.scalars(
                select(InvoiceSnapshot)
                .where(InvoiceSnapshot.tenant_id == tenant)
                .order_by(InvoiceSnapshot.number)
            )
        )
        saved = save(client, tenant)
        yield client, store, context(tenant, user.id), invoices, saved


def configure_recipients(client: Any, store: Any, ctx: Any) -> None:
    contacts = list(store.scalars(select(CustomerContact).where(CustomerContact.tenant_id == ctx.tenant_id)))
    store.rollback()
    for contact in contacts:
        response = client.put(
            f"/api/workspaces/{ctx.tenant_id}/policy-recipients/{contact.id}",
            json={
                "expected_version": 0,
                "expected_contact_revision": contact.source_revision,
                "expected_destination": contact.email,
                "timezone": "Australia/Melbourne",
                "timezone_verified": True,
                "owner_confirmed_tenant_timezone": False,
                "destination_verified": True,
                "allowed_channels": ["email"],
                "opt_out": False,
                "evidence": "Synthetic owner test review",
            },
        )
        assert response.status_code == 200, response.text


def activate(client: Any, ctx: Any, saved: Any) -> dict[str, Any]:
    return save(
        client,
        ctx.tenant_id,
        policy_id=saved["policy_id"],
        name=saved["name"],
        expected_version=saved["version"],
        definition=saved["definition"],
        activate=True,
        native_reminders_disabled=True,
        configuration_reviewed=True,
    )
