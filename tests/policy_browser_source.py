"""Append-only synthetic contact fixture; the browser must invoke the actual sync API."""

import json
import sys
from uuid import UUID

from identity_support import database_url
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aco.features.accounting.models import Customer, CustomerContact, DemoSourceRecord
from aco.features.identity.models import Tenant, now


def main() -> None:
    tenant_id, contact_id, destination = UUID(sys.argv[1]), UUID(sys.argv[2]), sys.argv[3]
    assert destination.endswith("@example.test")
    engine = create_engine(database_url())
    with Session(engine) as store:
        tenant = store.get(Tenant, tenant_id)
        assert tenant and tenant.is_demo and tenant.name.startswith("T08 browser ")
        contact = store.get(CustomerContact, contact_id)
        assert contact and contact.tenant_id == tenant_id
        customer = store.get(Customer, contact.customer_id)
        assert customer
        source = store.scalar(
            select(DemoSourceRecord)
            .where(
                DemoSourceRecord.tenant_id == tenant_id,
                DemoSourceRecord.provider_connection_id == contact.provider_connection_id,
                DemoSourceRecord.kind == "customer",
                DemoSourceRecord.external_id == customer.external_customer_id,
            )
            .order_by(DemoSourceRecord.revision.desc())
        )
        assert source
        record = DemoSourceRecord(
            tenant_id=tenant_id,
            provider_connection_id=contact.provider_connection_id,
            organisation_id=source.organisation_id,
            kind="customer",
            external_id=source.external_id,
            revision=source.revision + 1,
            payload={**source.payload, "email": destination},
            effective_at=now(),
            observed_at=now(),
        )
        store.add(record)
        store.commit()
        print(
            json.dumps(
                {"source_id": str(record.id), "provider_connection_id": str(contact.provider_connection_id)}
            )
        )
    engine.dispose()


if __name__ == "__main__":
    main()
