"""Canonical fixture behavior; browser OIDC authority is accepted only in the browser lane."""

from typing import cast
from uuid import UUID

import pytest
from action_support import action_world
from approval_browser_state import change_source, current_owner, evidence, prepare
from sqlalchemy import select

from aco.features.accounting.models import AccountingEvent, InvoiceSnapshot
from aco.features.review.models import ApprovalRequest


def test_browser_fixture_uses_canonical_requests_and_synthetic_source_reconciliation() -> None:
    with action_world() as world:
        # This integration identity has a fake issuer and cannot act as browser authority.
        with pytest.raises(ValueError, match="Actual local OIDC issuer"):
            current_owner(world.store, world.ctx.tenant_id, "https://identity.test")
        with pytest.raises(ValueError, match="actual browser Owner membership"):
            current_owner(world.store, world.ctx.tenant_id, "http://127.0.0.1:58080/realms/aco")
        refs = cast(list[dict[str, str]], prepare(world.store, world.ctx)["approvals"])
        assert len(refs) == 2
        with pytest.raises(ValueError, match="no existing actions or approvals"):
            prepare(world.store, world.ctx)
        target = world.store.get(ApprovalRequest, UUID(refs[1]["id"]))
        assert target is not None and target.status == "pending"
        invoice = world.store.get(InvoiceSnapshot, UUID(refs[1]["invoice_id"]))
        assert invoice is not None
        before = invoice.balance_minor
        changed = change_source(world.store, world.ctx, target.id, world.url)
        assert changed["current_revision"] != changed["previous_revision"]
        world.store.expire_all()
        current = world.store.get(InvoiceSnapshot, invoice.id)
        assert current is not None and current.synthetic and current.balance_minor == before - 1
        assert (
            world.store.scalar(
                select(AccountingEvent.id).where(
                    AccountingEvent.invoice_id == current.id,
                    AccountingEvent.source_revision == current.source_revision,
                )
            )
            is not None
        )
        observed = evidence(world.store, world.ctx)
        assert (
            observed["action_executions"]
            == observed["dispatch_bindings"]
            == observed["communications"]
            == observed["dispatch_tickets"]
            == 0
        )
        assert observed["decisions"] == []


def test_browser_fixture_prepare_flushes_canonical_records_in_a_clean_subprocess() -> None:
    """API/test imports in this process must not register the child's model graph."""
    import json
    import subprocess
    import sys

    with action_world() as world:
        payload = json.dumps({"url": world.url, "context": world.ctx.model_dump(mode="json")})
        world.store.rollback()
        code = """
import json, runpy, sys
fixture = runpy.run_path('tests/approval_browser_state.py', run_name='approval_fixture')
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from aco.core.context import TenantContext
payload = json.loads(sys.stdin.read())
engine = create_engine(payload['url'])
try:
    with Session(engine) as store:
        result = fixture['prepare'](store, TenantContext.model_validate_json(json.dumps(payload['context'])))
        print(json.dumps(result))
finally:
    engine.dispose()
"""
        child = subprocess.run(
            [sys.executable, "-c", code],
            input=payload,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        prepared = json.loads(child.stdout)
        assert prepared["synthetic"] and len(prepared["approvals"]) == 2
        for item in prepared["approvals"]:
            row = world.store.get(ApprovalRequest, UUID(item["id"]))
            assert row is not None and row.status == "pending" and row.initiator_id == world.ctx.actor_id
