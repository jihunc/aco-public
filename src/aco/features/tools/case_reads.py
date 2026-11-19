"""Retained case evidence after current sealed invoice authorization; no original bytes."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.dto import CommunicationEvidence
from aco.features.cases.models import CaseEvidence, CaseInvoiceLink, CollectionCase
from aco.features.cases.service import validate_evidence
from aco.features.communications.association import retained_original
from aco.features.communications.models import Communication
from aco.features.tools.schemas import CaseEvidenceFact
from aco.features.voice.evidence import facts_digest, validate_reference
from aco.features.voice.evidence_dto import VoiceOperationalEvidence
from aco.features.voice.models import VoiceBusinessEvidence


def case_evidence(db: Session, invoice: InvoiceSnapshot) -> list[CaseEvidenceFact]:
    rows = db.execute(
        select(CaseEvidence, CollectionCase)
        .join(CollectionCase, CollectionCase.id == CaseEvidence.case_id)
        .join(CaseInvoiceLink, CaseInvoiceLink.case_id == CollectionCase.id)
        .where(
            CaseEvidence.tenant_id == invoice.tenant_id,
            CollectionCase.tenant_id == invoice.tenant_id,
            CollectionCase.customer_id == invoice.customer_id,
            CollectionCase.provider_connection_id == invoice.provider_connection_id,
            CaseInvoiceLink.tenant_id == invoice.tenant_id,
            CaseInvoiceLink.invoice_id == invoice.id,
        )
        .order_by(CaseEvidence.created_at.desc(), CaseEvidence.id)
        .limit(51)
        .execution_options(populate_existing=True)
    )
    facts = []
    for evidence, case in rows:
        fact = CaseEvidenceFact(
            evidence_id=evidence.id,
            case_id=case.id,
            case_version=case.version,
            evidence_kind=evidence.kind,
            source_kind="voice_operational" if evidence.voice_evidence_id else "communication",
            source_state=evidence.source_state,
            observed_at=evidence.created_at,
            synthetic=evidence.synthetic,
            available=False,
            unavailable_reason="CURRENT_SOURCE_UNAVAILABLE",
        )
        try:
            if evidence.communication_id is not None:
                command = CommunicationEvidence(
                    communication_id=evidence.communication_id,
                    excerpt=evidence.excerpt,
                    offset=evidence.offset,
                )
                message = validate_evidence(db, invoice.tenant_id, invoice.customer_id, command, [invoice.id])
                assert isinstance(message, Communication)
                if message.synthetic != evidence.synthetic:
                    raise HTTPException(409, "CASE_SOURCE_PROVENANCE_CHANGED")
                digest = retained_original(db, message)
                fact = fact.model_copy(
                    update={
                        "available": True,
                        "unavailable_reason": None,
                        "communication_id": message.id,
                        "source_sha256": digest,
                        "source_state": message.source_state,
                        "excerpt": evidence.excerpt[:2000],
                        "offset": evidence.offset,
                        "text_truncated": len(evidence.excerpt) > 2000,
                    }
                )
            elif evidence.voice_evidence_id is not None:
                source = db.get(VoiceBusinessEvidence, evidence.voice_evidence_id, populate_existing=True)
                if source is None or source.synthetic != evidence.synthetic:
                    raise HTTPException(409, "VOICE_BUSINESS_SOURCE_UNAVAILABLE")
                reference = VoiceOperationalEvidence(
                    voice_evidence_id=source.id, facts_hash=facts_digest(source.facts)
                )
                validate_reference(db, invoice.tenant_id, invoice.customer_id, reference, [invoice.id])
                # Exact immutable normalized fact reference, never a fabricated email or verbatim capture.
                fact = fact.model_copy(
                    update={
                        "available": True,
                        "unavailable_reason": None,
                        "voice_source": reference,
                        "source_state": source.source_state,
                    }
                )
        except HTTPException:
            pass  # Retain evidence identity, withhold source identity/text when current scope/privacy fails.
        facts.append(fact)
    return facts
