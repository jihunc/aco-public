"""Atomic global receiving-address ownership, independent of invoice/thread routing."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aco.features.communications.models import EmailIngressActivation, EmailMailboxClaim, EmailMailboxConflict
from aco.features.identity.models import now
from aco.integrations.ports.email_dto import mailbox


def normalized_mailbox(address: str) -> str:
    return mailbox(address).casefold()


def claim_mailboxes(db: Session, activation: EmailIngressActivation, addresses: tuple[str, ...]) -> None:
    for address in sorted({normalized_mailbox(item) for item in addresses}):
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "inbound-mailbox:" + address},
        )
        conflict = db.scalar(select(EmailMailboxConflict).where(EmailMailboxConflict.address == address))
        if conflict is not None and conflict.resolved_at is None:
            originals = list(
                db.scalars(
                    select(EmailIngressActivation).where(
                        EmailIngressActivation.id.in_([UUID(key) for key in conflict.activation_ids])
                    )
                )
            )
            if any(row.revoked_at is None and row.expires_at > now() for row in originals):
                raise HTTPException(409, "INBOUND_MAILBOX_REVIEW_REQUIRED")
            conflict.resolved_at = now()
        previous = db.scalar(
            select(EmailMailboxClaim).where(
                EmailMailboxClaim.address == address, EmailMailboxClaim.released_at.is_(None)
            )
        )
        if previous is not None:
            owner = db.get(EmailIngressActivation, previous.activation_id, populate_existing=True)
            assert owner is not None
            inactive = owner.revoked_at is not None or owner.expires_at <= now()
            if not inactive and (previous.tenant_id, previous.email_connection_id) != (
                activation.tenant_id,
                activation.email_connection_id,
            ):
                raise HTTPException(409, "INBOUND_MAILBOX_ALREADY_OWNED")
            # Serialize same-owner renewal with any in-flight current receipt processing.
            db.get(
                EmailIngressActivation, previous.activation_id, with_for_update=True, populate_existing=True
            )
            previous.released_at = now()
            previous.version += 1
            db.flush()
        db.add(
            EmailMailboxClaim(
                tenant_id=activation.tenant_id,
                activation_id=activation.id,
                email_connection_id=activation.email_connection_id,
                address=address,
            )
        )
        db.flush()


def validate_mailbox_claims(
    db: Session, activation: EmailIngressActivation, addresses: tuple[str, ...]
) -> None:
    actual = set(
        db.scalars(
            select(EmailMailboxClaim.address).where(
                EmailMailboxClaim.activation_id == activation.id,
                EmailMailboxClaim.tenant_id == activation.tenant_id,
                EmailMailboxClaim.email_connection_id == activation.email_connection_id,
                EmailMailboxClaim.released_at.is_(None),
            )
        )
    )
    if actual != {normalized_mailbox(item) for item in addresses}:
        raise HTTPException(403, "INBOUND_MAILBOX_OWNERSHIP_REVOKED")


def release_mailboxes(db: Session, activation_id: UUID) -> None:
    for row in db.scalars(
        select(EmailMailboxClaim).where(
            EmailMailboxClaim.activation_id == activation_id, EmailMailboxClaim.released_at.is_(None)
        )
    ):
        row.released_at = now()
        row.version += 1
