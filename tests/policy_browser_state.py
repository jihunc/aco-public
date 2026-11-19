"""Read-only evidence for the synthetic policy browser workspace in the contained lane."""

import json
import sys
from uuid import UUID

from identity_support import database_url
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.identity.models import AuditEvent, Tenant
from aco.features.policies.models import ContactReservation, DispatchTicketRecord, RecipientControl


def main() -> None:
    key = UUID(sys.argv[1])
    engine = create_engine(database_url())
    with Session(engine) as store:
        tenant = store.get(Tenant, key)
        assert tenant and tenant.is_demo and tenant.name.startswith("T08 browser ")
        actions = list(store.scalars(select(ScheduledAction).where(ScheduledAction.tenant_id == key)))
        print(
            json.dumps(
                {
                    "tenant": str(key),
                    "automation_enabled": tenant.automation_enabled,
                    "tenant_version": tenant.version,
                    "recipient_controls": [
                        dict(row)
                        for row in store.execute(
                            select(RecipientControl.__table__)
                            .where(RecipientControl.tenant_id == key)
                            .order_by(RecipientControl.id)
                        ).mappings()
                    ],
                    "audit_count": len(
                        list(store.scalars(select(AuditEvent.id).where(AuditEvent.tenant_id == key)))
                    ),
                    "actions": [
                        {
                            "id": str(a.id),
                            "version": a.version,
                            "policy_version_id": str(a.policy_version_id),
                            "occurrence_id": str(a.occurrence_id),
                            "invoice_id": str(a.invoice_id),
                            "status": a.status,
                            "operation_key": a.operation_key,
                            "command": a.command,
                            "due_at": a.due_at.isoformat(),
                        }
                        for a in actions
                    ],
                    "attempts": len(
                        list(store.scalars(select(ActionExecution).where(ActionExecution.tenant_id == key)))
                    ),
                    "contacts": len(
                        list(
                            store.scalars(
                                select(ContactReservation).where(ContactReservation.tenant_id == key)
                            )
                        )
                    ),
                    "tickets": len(
                        list(
                            store.scalars(
                                select(DispatchTicketRecord).where(DispatchTicketRecord.tenant_id == key)
                            )
                        )
                    ),
                },
                sort_keys=True,
                default=str,
            )
        )
    engine.dispose()


if __name__ == "__main__":
    main()
