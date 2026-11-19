"""Current-human, tenant-scoped outcome projection from retained case evidence."""

import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.cases.dto import CommunicationEvidence, InvoiceIssueTransition, TransitionCase
from aco.features.cases.models import CaseEvidence, CaseHistory, CaseInvoiceLink, CollectionCase
from aco.features.cases.service import require_human
from aco.features.communications.models import CommunicationInvoiceLink
from aco.features.identity.models import now
from aco.features.impact.case_outcomes import (
    CaseFact,
    CaseOutcomesView,
    HistoryFact,
    calculate_case,
    source_groups,
    summarize,
)
from aco.features.impact.models import HumanHandlingEntry
from aco.features.review.approval_dto import InternalTaskApprovalProposal
from aco.features.review.approvals import digest
from aco.features.review.models import ApprovalDecision, ApprovalRequest
from aco.features.voice.models import VoiceBusinessEvidence


def instant(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
        return result if result is not None and result.utcoffset() is not None else None
    except ValueError:
        return None


class CaseOutcomeService:
    def __init__(self, db: Session):
        self.db = db

    def read(
        self, ctx: TenantContext, since: datetime, until: datetime, as_of: datetime | None = None
    ) -> CaseOutcomesView:
        require_human(self.db, ctx)
        generated = now()
        selected = as_of or generated
        if (
            any(t.utcoffset() is None for t in (since, until, selected))
            or since >= until
            or selected > generated
            or selected < since
        ):
            raise HTTPException(422, "INVALID_CASE_OUTCOME_WINDOW")
        cases = list(
            self.db.scalars(
                select(CollectionCase)
                .where(
                    CollectionCase.tenant_id == ctx.tenant_id,
                    CollectionCase.created_at >= since,
                    CollectionCase.created_at < until,
                    CollectionCase.created_at <= selected,
                )
                .order_by(CollectionCase.created_at, CollectionCase.id)
                .limit(2001)
                .execution_options(populate_existing=True)
            )
        )
        if len(cases) > 2000:
            raise HTTPException(422, "CASE_OUTCOME_WINDOW_TOO_LARGE")
        rows = []
        for case in cases:
            links = list(
                self.db.scalars(
                    select(CaseInvoiceLink)
                    .where(
                        CaseInvoiceLink.tenant_id == ctx.tenant_id,
                        CaseInvoiceLink.case_id == case.id,
                        CaseInvoiceLink.created_at <= selected,
                    )
                    .execution_options(populate_existing=True)
                )
            )
            histories = list(
                self.db.scalars(
                    select(CaseHistory)
                    .where(CaseHistory.tenant_id == ctx.tenant_id, CaseHistory.case_id == case.id)
                    .order_by(CaseHistory.created_at, CaseHistory.id)
                )
            )
            evidence = list(
                self.db.scalars(
                    select(CaseEvidence).where(
                        CaseEvidence.tenant_id == ctx.tenant_id, CaseEvidence.case_id == case.id
                    )
                )
            )
            facts = self.history(case, histories, evidence, {link.invoice_id for link in links})
            outcome = calculate_case(
                CaseFact(
                    case_id=case.id,
                    opened_at=case.created_at,
                    synthetic=case.synthetic,
                    invoice_ids=[link.invoice_id for link in links],
                    invoice_opened_at={link.invoice_id: link.created_at for link in links},
                ),
                facts,
                selected,
            )
            entries = list(
                self.db.scalars(
                    select(HumanHandlingEntry)
                    .where(
                        HumanHandlingEntry.tenant_id == ctx.tenant_id,
                        HumanHandlingEntry.case_id == case.id,
                        HumanHandlingEntry.created_at <= selected,
                    )
                    .order_by(HumanHandlingEntry.version)
                )
            )
            human_ids = {h.id for h in histories if h.substantive_human and h.created_at <= selected}
            latest_entries = {e.history_id: e for e in entries if e.history_id in human_ids}
            known = sum((e.minutes for e in latest_entries.values()), Decimal(0))
            missing = len(human_ids - latest_entries.keys())
            decisions = self.case_decisions(case, selected)
            rows.append(
                outcome.model_copy(
                    update={
                        "approval_decision_ids": [decision.id for decision in decisions],
                        "approval_request_ids": list(
                            dict.fromkeys(decision.request_id for decision in decisions)
                        ),
                        "reported_human_minutes": known
                        if human_ids and not missing and outcome.completeness == "COMPLETE"
                        else None,
                        "known_reported_human_minutes": known,
                        "missing_handling_event_count": missing,
                        "handling_entry_ids": sorted([e.id for e in latest_entries.values()], key=str),
                    }
                )
            )
        totals = summarize(rows)
        groups = source_groups(rows)
        if len(groups) > 1:
            # Separate source labels, never present a blended outcome rate.
            totals = totals.model_copy(
                update={
                    "autonomous_resolution_percent": None,
                    "human_intervention_percent": None,
                    "mean_resolution_seconds": None,
                    "mean_open_age_seconds": None,
                    "reported_human_minutes": None,
                }
            )
        return CaseOutcomesView(
            **totals.model_dump(),
            tenant_id=ctx.tenant_id,
            since=since,
            until=until,
            as_of_at=selected,
            generated_at=generated,
            human_time_reason=(
                "Operator-reported active minutes; corrections retained. "
                "Unreported events and absent explicit zero evidence remain unknown; "
                "no automatic measurement or time-saved claim."
            ),
            case_approval_count=sum(len(row.approval_decision_ids) for row in rows) or None,
            approval_reason=(
                "Only immutable decisions with an exact reviewed case/version/action binding are counted. "
                "Historical unbound approvals remain unattributed; "
                "unchanged approval is not substantive handling."
            ),
            cases=rows,
            groups=groups,
            mixed_sources=len(groups) > 1,
        )

    def case_decisions(self, case: CollectionCase, selected: datetime) -> list[ApprovalDecision]:
        """Historical reviewed bindings survive later case edits; no invoice-overlap attribution."""
        result = []
        pairs = self.db.execute(
            select(ApprovalDecision, ApprovalRequest)
            .join(ApprovalRequest, ApprovalRequest.id == ApprovalDecision.request_id)
            .where(
                ApprovalDecision.tenant_id == case.tenant_id,
                ApprovalRequest.tenant_id == case.tenant_id,
                ApprovalDecision.created_at >= case.created_at,
                ApprovalDecision.created_at <= selected,
                ApprovalRequest.proposal["effect"].as_string() == "internal_task",
            )
            .order_by(ApprovalDecision.created_at, ApprovalDecision.id)
        )
        for decision, request in pairs:
            try:
                proposal = InternalTaskApprovalProposal.model_validate_json(json.dumps(request.proposal))
            except ValidationError:
                continue
            captured = (decision.review_binding or {}).get("scope")
            if (
                proposal.case_binding is None
                or proposal.case_binding.case_id != case.id
                or not isinstance(captured, dict)
                or decision.material_hash != request.material_hash
                or digest(request.proposal) != request.material_hash
                or proposal.action_id != request.action_id
                or proposal.action_version != request.action_version
                or captured.get("proposal") != request.proposal
                or captured.get("request_id") != str(request.id)
            ):
                continue
            binding = captured.get("case_binding")
            if (
                not isinstance(binding, dict)
                or binding.get("case_id") != str(case.id)
                or binding.get("case_version") != proposal.case_binding.case_version
                or binding.get("customer_id") != str(case.customer_id)
                or binding.get("provider_connection_id") != str(case.provider_connection_id)
            ):
                continue
            result.append(decision)
        return result

    def history(
        self,
        case: CollectionCase,
        histories: list[CaseHistory],
        evidence: list[CaseEvidence],
        scope: set[UUID],
    ) -> list[HistoryFact]:
        facts: list[HistoryFact] = []
        last_resolution: HistoryFact | None = None
        for index, row in enumerate(histories):
            fact = HistoryFact(
                id=row.id, at=row.created_at, event="unknown", substantive_human=row.substantive_human
            )
            try:
                if row.event == "case.created":
                    fact.event = "created"
                elif row.event == "case.evidence_added":
                    fact.event = "handling"
                elif row.event == "case.approval_modified":
                    request = self.db.get(ApprovalRequest, UUID(str(row.details.get("approval_id"))))
                    if request is not None and request.tenant_id == case.tenant_id:
                        proposal = InternalTaskApprovalProposal.model_validate_json(
                            json.dumps(request.proposal)
                        )
                        if (
                            proposal.case_binding is not None
                            and proposal.case_binding.case_id == case.id
                            and proposal.case_binding.case_version == row.details.get("case_version")
                            and request.editor_id == row.actor_id
                            and str(request.supersedes_id) == row.details.get("supersedes_id")
                            and request.material_hash == row.details.get("material_hash")
                            and digest(request.proposal) == request.material_hash
                        ):
                            fact.event = "handling"
                elif row.event == "case.invoice_issue_resolved":
                    command = InvoiceIssueTransition.model_validate(row.details)
                    reference = command.evidence
                    voice = (
                        None
                        if isinstance(reference, CommunicationEvidence)
                        else self.db.get(VoiceBusinessEvidence, reference.voice_evidence_id)
                    )
                    source = next(
                        (
                            e
                            for e in evidence
                            if e.tenant_id == case.tenant_id
                            and e.case_id == case.id
                            and e.created_at <= row.created_at
                            and e.kind == "resolution"
                            and e.excerpt == reference.excerpt
                            and e.offset == reference.offset
                            and (
                                (
                                    isinstance(reference, CommunicationEvidence)
                                    and e.communication_id == reference.communication_id
                                    and e.voice_evidence_id is None
                                )
                                or (
                                    not isinstance(reference, CommunicationEvidence)
                                    and e.communication_id is None
                                    and e.voice_evidence_id == reference.voice_evidence_id
                                    and voice is not None
                                    and voice.tenant_id == case.tenant_id
                                    and voice.customer_id == case.customer_id
                                    and voice.accounting_connection_id == case.provider_connection_id
                                    and voice.facts_hash == reference.facts_hash
                                    and voice.created_at <= row.created_at
                                )
                            )
                        ),
                        None,
                    )
                    if command.case_id == case.id and command.invoice_id in scope and source is not None:
                        fact.event, fact.invoice_id, fact.evidence_id = (
                            "issue_resolved",
                            command.invoice_id,
                            source.id,
                        )
                elif row.event == "case.transitioned":
                    transition = TransitionCase.model_validate(row.details)
                    if transition.case_id == case.id:
                        fact.status, fact.closure_reason = transition.status, transition.closure_reason
                        fact.event = (
                            "resolved"
                            if transition.status == "RESOLVED"
                            else "closed"
                            if transition.status == "CLOSED"
                            else "status"
                        )
                        if fact.event == "resolved":
                            last_resolution = fact
                elif row.event == "case.reopened":
                    if last_resolution is not None:
                        last_resolution.resolved_at = instant(row.details.get("previous_resolved_at"))
                    last_resolution = None
                    fact.event = "reopened"
                    source = next(
                        (
                            e
                            for e in evidence
                            if str(e.id) == row.details.get("evidence_id") and e.created_at <= row.created_at
                        ),
                        None,
                    )
                    # Existing upsert histories omit the exact impacted subset. Do not guess it.
                    following = histories[index + 1] if index + 1 < len(histories) else None
                    exact_link_path = following is not None and (
                        following.event == "case.transitioned"
                        or (
                            following.event == "case.evidence_added"
                            and "review_required" not in following.details
                        )
                    )
                    if source is not None and exact_link_path:
                        affected = self.db.scalars(
                            select(CommunicationInvoiceLink.invoice_id).where(
                                CommunicationInvoiceLink.tenant_id == case.tenant_id,
                                CommunicationInvoiceLink.communication_id == source.communication_id,
                                CommunicationInvoiceLink.created_at <= row.created_at,
                            )
                        )
                        fact.affected_invoice_ids = sorted(scope.intersection(affected), key=str)
            except (ValidationError, ValueError):
                fact.event = "unknown"
            facts.append(fact)
        if last_resolution is not None:
            last_resolution.resolved_at = case.resolved_at
        return facts
