"""Current customer contact restriction derived from canonical, retained stop evidence."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.policies.models import CollectionHold

REASON = "contact_stop_pending_review"


def contact_stop_source(
    db: Session, action: ScheduledAction
) -> tuple[UUID, UUID | None, UUID | None, str, str] | None:
    """Only retained successful canonical commands establish customer-wide restriction."""
    if action.kind == "agent_internal" and action.command.get("kind") == "request_collection_hold":
        payload = action.command.get("payload")
        try:
            assert isinstance(payload, dict)
            source = payload["evidence"]
            assert isinstance(source, dict)
            customer, source_id = UUID(str(payload["customer_id"])), UUID(str(source["communication_id"]))
            return customer, source_id, None, "contact-stop:" + str(source_id), str(source["excerpt"])
        except (KeyError, ValueError, AssertionError):
            return None
    if action.kind == "voice_internal" and "hold_facts" in action.command:
        from aco.features.voice.models import VoiceBusinessEvidence, VoiceInvocation

        source_row = db.scalar(
            select(VoiceBusinessEvidence).where(
                VoiceBusinessEvidence.tenant_id == action.tenant_id,
                VoiceBusinessEvidence.action_id == action.id,
                VoiceBusinessEvidence.session_id == action.voice_session_id,
                VoiceBusinessEvidence.invocation_id == action.voice_invocation_id,
                VoiceBusinessEvidence.fact_kind == "contact_stop",
            )
        )
        invocation = (
            db.get(VoiceInvocation, action.voice_invocation_id) if action.voice_invocation_id else None
        )
        if (
            source_row is None
            or invocation is None
            or invocation.action_id != action.id
            or invocation.tool != "request_collection_hold"
            or invocation.result != action.result
            or source_row.accounting_connection_id != action.provider_connection_id
            or source_row.facts != action.command.get("hold_facts")
        ):
            return None
        return (
            source_row.customer_id,
            None,
            source_row.id,
            "voice-contact-stop:" + str(source_row.id),
            "Normalized unverified contact-stop business fact; not a transcript or quotation.",
        )
    return None


def stop_actions(db: Session, invoice: InvoiceSnapshot) -> list[ScheduledAction]:
    rows = db.scalars(
        select(ScheduledAction)
        .where(
            ScheduledAction.tenant_id == invoice.tenant_id,
            ScheduledAction.provider_connection_id == invoice.provider_connection_id,
            ScheduledAction.kind.in_(["agent_internal", "voice_internal"]),
            ScheduledAction.status == "succeeded",
        )
        .order_by(ScheduledAction.id)
        .execution_options(populate_existing=True)
    )
    result = []
    for action in rows:
        source = contact_stop_source(db, action)
        payload = action.command.get("payload")
        retained_agent_customer = (
            action.kind == "agent_internal"
            and action.command.get("kind") == "request_collection_hold"
            and isinstance(payload, dict)
            and payload.get("customer_id") == str(invoice.customer_id)
        )
        if retained_agent_customer or (source is not None and source[0] == invoice.customer_id):
            result.append(action)
    return result


def pending_contact_stops(db: Session, invoice: InvoiceSnapshot) -> list[dict[str, object]]:
    """Include later imports without rewriting source or silently releasing original holds.

    The executed command fixes customer/connection scope at acceptance, even if a later
    source correction moves its original invoice. Every original hold needs explicit
    release; missing retained evidence conservatively stays pending.
    """
    pending: list[dict[str, object]] = []
    for action in stop_actions(db, invoice):
        source = contact_stop_source(db, action)
        marker = source[3] if source is not None else ""
        ids = (action.result or {}).get("changed_ids")
        valid = isinstance(ids, list) and bool(ids) and bool(marker)
        holds = []
        if valid and isinstance(ids, list):
            valid = len(set(map(str, ids))) == len(ids)
            for raw in ids:
                try:
                    key = UUID(str(raw))
                except ValueError:
                    valid = False
                    break
                hold = db.get(CollectionHold, key, populate_existing=True)
                if (
                    hold is None
                    or hold.tenant_id != invoice.tenant_id
                    or hold.reason != REASON
                    or hold.evidence != marker
                ):
                    valid = False
                    break
                holds.append(hold)
        if not valid or any(h.released_at is None for h in holds):
            pending.append(
                {
                    "action_id": str(action.id),
                    "evidence_complete": valid,
                    "holds": [
                        {
                            "id": str(h.id),
                            "version": h.version,
                            "released_at": h.released_at.isoformat() if h.released_at else None,
                        }
                        for h in sorted(holds, key=lambda h: str(h.id))
                    ],
                }
            )
    return pending
