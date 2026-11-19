"""Independent synthetic original/receipt callback probes; no verified live ingress claim."""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from ingress_support import ingress_world
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aco.features.communications.ingress_dto import InboundAgentSource
from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
from aco.integrations.ports.email_context import EmailIngressContext


async def test_second_notification_reuses_original_captured_without_prior_agent_run() -> None:
    from aco.features.agent.ingress import produce_inbound_run
    from aco.features.agent.models import AgentRun

    with ingress_world() as lane:
        message = lane.message()
        lane.world.store.rollback()
        first_id = await lane.deliver(message)
        await lane.work()
        second_id = await lane.deliver(message)
        assert second_id != first_id
        await lane.work(produce_inbound_run)
        db = lane.world.store
        db.rollback()
        second = db.get(EmailIngressReceipt, second_id)
        assert second is not None
        assert second.state == "processed", second.error_code
        assert second.agent_run_id is not None
        assert db.scalar(select(func.count()).select_from(Communication)) == 1
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 1


async def test_callback_rejects_substituted_receipt_job_actor_and_source() -> None:
    from aco.features.agent.ingress import produce_inbound_run

    with ingress_world() as lane:
        message = lane.message()
        lane.world.store.rollback()
        receipt_id = await lane.deliver(message)

        def probe(db: Session, ctx: EmailIngressContext, source: InboundAgentSource) -> UUID | None:
            for changed in (
                ctx.model_copy(update={"receipt_id": uuid4()}),
                ctx.model_copy(update={"job_id": ctx.job_id + 1000000}),
                ctx.model_copy(update={"actor_id": uuid4()}),
                ctx.model_copy(update={"lease_token": uuid4()}),
            ):
                with pytest.raises(HTTPException) as denied:
                    produce_inbound_run(db, changed, source)
                assert denied.value.status_code == 403
            with pytest.raises(HTTPException) as denied:
                produce_inbound_run(db, ctx, source.model_copy(update={"communication_id": uuid4()}))
            assert denied.value.status_code == 403
            return produce_inbound_run(db, ctx, source)

        await lane.work(probe)
        lane.world.store.rollback()
        receipt = lane.world.store.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.state == "processed", receipt.error_code if receipt else None
        assert receipt.agent_run_id is not None
