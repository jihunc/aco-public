"""Durable synthetic mailbox contract across adapter recreation and native key expiry."""

import asyncio
import json
from datetime import timedelta
from typing import Any

from reminder_support import dispatch_one, reminder_world
from sqlalchemy import func, select

from aco.core.types import ProviderReference
from aco.features.communications.models import Communication, EmailDispatchBinding
from aco.features.identity.models import now
from aco.integrations.demo_email.adapter import DemoEmailRuntime
from aco.integrations.demo_email.models import NativeEmailInvocation, NativeEmailOperation
from aco.integrations.ports.email_dto import OutboundEmail


def test_durable_mailbox_reads_and_native_expiry_do_not_pretend_permanent_vendor_deduplication() -> None:
    with reminder_world() as (client, store, ctx, invoices, saved):
        action = dispatch_one(client, store, ctx)
        assert action.status == "succeeded"
        binding = store.scalar(select(EmailDispatchBinding))
        message = store.scalar(select(Communication))
        command = OutboundEmail.model_validate_json(json.dumps(binding.command))
        runtime = DemoEmailRuntime(client.app.state.business_sessions)
        ref = ProviderReference(
            provider="demo_email",
            connection_id=binding.email_connection_id,
            external_id=message.native_message_id,
        )

        async def read_and_repeat() -> Any:
            port = runtime.bind(ctx.tenant_id, binding.email_connection_id)
            found = (await port.get_message(ctx, ref)).value
            assert found.synthetic and found.native_identity == "SIMULATED"
            assert found.origin_invoice_id == action.invoice_id and found.source.kind == "UNAVAILABLE"
            assert found.provider_thread_ref is not None
            thread = (await port.fetch_thread(ctx, found.provider_thread_ref)).value
            assert len(thread) == 1 and thread[0].provider_ref == ref
            page = (await port.sync_messages(ctx)).value
            assert len(page.messages) == 1
            assert not (await port.sync_messages(ctx, page.next_cursor)).value.messages
            accepted = await port.send_email(ctx, command)
            assert accepted.message_ref == ref

        store.rollback()
        asyncio.run(read_and_repeat())
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 2
        assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 1
        native = store.scalar(select(NativeEmailOperation))
        native.vendor_key_expires_at = now() - timedelta(seconds=1)
        store.commit()

        async def expire() -> Any:
            return (
                await DemoEmailRuntime(client.app.state.business_sessions)
                .bind(ctx.tenant_id, binding.email_connection_id)
                .send_email(ctx, command)
            )

        next_acceptance = asyncio.run(expire())
        assert next_acceptance.message_ref != ref
        store.rollback()
        assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 2
