"""Pure replay of recorded case events; no financial or causal inference."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from aco.core.types import StrictModel


class CaseFact(StrictModel):
    case_id: UUID
    opened_at: datetime
    synthetic: bool
    invoice_ids: list[UUID]
    invoice_opened_at: dict[UUID, datetime] = Field(default_factory=dict)


class HistoryFact(StrictModel):
    model_config = ConfigDict(frozen=False)
    id: UUID
    at: datetime
    event: Literal[
        "created", "status", "issue_resolved", "resolved", "closed", "reopened", "handling", "unknown"
    ]
    substantive_human: bool = False
    status: str | None = None
    resolved_at: datetime | None = None
    closure_reason: str | None = None
    invoice_id: UUID | None = None
    affected_invoice_ids: list[UUID] = Field(default_factory=list)
    evidence_id: UUID | None = None


class CaseOutcomeView(StrictModel):
    case_id: UUID
    synthetic: bool
    opened_at: datetime
    status: str
    closure_reason: str | None
    resolved_at: datetime | None
    eligible_resolved: bool
    autonomous: bool
    substantive_human: bool
    open_backlog: bool
    administrative_closed: bool
    completeness: Literal["COMPLETE", "INCOMPLETE"]
    issues: list[str]
    resolution_seconds: Decimal | None
    backlog_age_seconds: Decimal | None
    resolved_episode_count: int
    history_ids: list[UUID]
    resolution_evidence_ids: list[UUID]
    reported_human_minutes: Decimal | None = None
    known_reported_human_minutes: Decimal = Decimal(0)
    missing_handling_event_count: int = 0
    handling_entry_ids: list[UUID] = Field(default_factory=list)
    approval_decision_ids: list[UUID] = Field(default_factory=list)
    approval_request_ids: list[UUID] = Field(default_factory=list)


class CaseOutcomeTotals(StrictModel):
    eligible_count: int
    resolved_count: int
    autonomous_resolved_count: int
    human_intervention_count: int
    open_backlog_count: int
    administrative_closed_count: int
    unknown_count: int
    autonomous_resolution_percent: Decimal | None
    human_intervention_percent: Decimal | None
    mean_resolution_seconds: Decimal | None
    mean_open_age_seconds: Decimal | None
    reported_human_minutes: Decimal | None = None
    known_reported_human_minutes: Decimal = Decimal(0)
    missing_handling_event_count: int = 0


class CaseOutcomeGroup(CaseOutcomeTotals):
    synthetic: bool


class CaseOutcomesView(CaseOutcomeTotals):
    tenant_id: UUID
    since: datetime
    until: datetime
    as_of_at: datetime
    generated_at: datetime
    algorithm: Literal["case_history_v1"] = "case_history_v1"
    source: Literal["RECORDED_CASE_HISTORY"] = "RECORDED_CASE_HISTORY"
    human_time_reason: str = "No recorded handling-time inputs; activity is not measured duration."
    case_approval_count: int | None = None
    approval_reason: str = (
        "Approval decisions have no verified case binding; invoice overlap is not attribution."
    )
    cases: list[CaseOutcomeView]
    groups: list[CaseOutcomeGroup]
    mixed_sources: bool
    caveat: str = "Recorded issue outcomes do not establish that ACO caused collection or labour savings."


def seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000


def calculate_case(fact: CaseFact, history: list[HistoryFact], as_of: datetime) -> CaseOutcomeView:
    events = [e for e in history if fact.opened_at <= e.at <= as_of]
    # Caller preserves stored order for identical timestamps.
    events.sort(key=lambda e: e.at)
    status = "OPEN"
    closure: str | None = None
    resolved: datetime | None = None
    dispositions: dict[UUID, UUID | None] = {}
    issues: set[str] = set()
    episodes = 0
    if not events or events[0].event != "created":
        issues.add("creation_history_missing")
    for event in events:
        if event.event == "unknown":
            issues.add("unverifiable_history")
        elif event.event == "issue_resolved":
            if event.invoice_id not in fact.invoice_ids:
                issues.add("issue_scope_mismatch")
            elif event.invoice_id is not None:
                dispositions[event.invoice_id] = event.evidence_id
        elif event.event == "reopened":
            status, closure, resolved = "INVESTIGATING", None, None
            if not event.affected_invoice_ids:
                issues.add("reopen_scope_unknown")
            for invoice in event.affected_invoice_ids:
                dispositions.pop(invoice, None)
        elif event.event == "resolved":
            scope = {i for i in fact.invoice_ids if fact.invoice_opened_at.get(i, fact.opened_at) <= event.at}
            if not scope or not scope.issubset(dispositions):
                issues.add("resolution_disposition_missing")
            else:
                episodes += 1
            status, resolved, closure = "RESOLVED", event.resolved_at or event.at, None
        elif event.event == "closed":
            status, closure = "CLOSED", event.closure_reason
            if closure == "resolved" and resolved is None:
                issues.add("resolution_history_missing")
            elif closure not in {
                "resolved",
                "duplicate_linked",
                "withdrawn",
                "out_of_scope",
                "invalid_claim",
            }:
                issues.add("closure_reason_unknown")
        elif event.event == "status":
            if event.status is None:
                issues.add("status_history_missing")
            else:
                status = event.status
    human = any(e.substantive_human for e in events)
    successful = status == "RESOLVED" or (status == "CLOSED" and closure == "resolved")
    scope = {i for i in fact.invoice_ids if fact.invoice_opened_at.get(i, fact.opened_at) <= as_of}
    if successful and (not scope or not scope.issubset(dispositions)):
        issues.add("resolution_disposition_missing")
    eligible = successful and resolved is not None and not issues
    backlog = status not in {"RESOLVED", "CLOSED"}
    return CaseOutcomeView(
        case_id=fact.case_id,
        synthetic=fact.synthetic,
        opened_at=fact.opened_at,
        status=status,
        closure_reason=closure,
        resolved_at=resolved,
        eligible_resolved=eligible,
        autonomous=eligible and not human,
        substantive_human=human,
        open_backlog=backlog,
        administrative_closed=status == "CLOSED"
        and closure in {"duplicate_linked", "withdrawn", "out_of_scope", "invalid_claim"},
        completeness="INCOMPLETE" if issues else "COMPLETE",
        issues=sorted(issues),
        resolution_seconds=seconds(fact.opened_at, resolved) if eligible and resolved else None,
        backlog_age_seconds=seconds(fact.opened_at, as_of) if backlog else None,
        missing_handling_event_count=sum(e.substantive_human for e in events),
        resolved_episode_count=episodes,
        history_ids=[e.id for e in events],
        resolution_evidence_ids=sorted({v for v in dispositions.values() if v is not None}, key=str),
    )


def summarize(rows: list[CaseOutcomeView]) -> CaseOutcomeTotals:
    resolved = sum(r.eligible_resolved for r in rows)
    autonomous = sum(r.autonomous for r in rows)
    human = sum(r.substantive_human for r in rows)
    unknown = sum(r.completeness == "INCOMPLETE" for r in rows)
    durations = [r.resolution_seconds for r in rows if r.resolution_seconds is not None]
    ages = [r.backlog_age_seconds for r in rows if r.backlog_age_seconds is not None]
    return CaseOutcomeTotals(
        reported_human_minutes=sum(
            (r.reported_human_minutes for r in rows if r.reported_human_minutes is not None), Decimal(0)
        )
        if rows and not unknown and all(r.reported_human_minutes is not None for r in rows)
        else None,
        known_reported_human_minutes=sum((r.known_reported_human_minutes for r in rows), Decimal(0)),
        missing_handling_event_count=sum(r.missing_handling_event_count for r in rows),
        eligible_count=len(rows),
        resolved_count=resolved,
        autonomous_resolved_count=autonomous,
        human_intervention_count=human,
        open_backlog_count=sum(r.open_backlog for r in rows),
        administrative_closed_count=sum(r.administrative_closed for r in rows),
        unknown_count=unknown,
        autonomous_resolution_percent=Decimal(autonomous) * 100 / resolved
        if resolved and not unknown
        else None,
        human_intervention_percent=Decimal(human) * 100 / len(rows) if rows and not unknown else None,
        mean_resolution_seconds=sum(durations, Decimal(0)) / len(durations)
        if durations and not unknown
        else None,
        mean_open_age_seconds=sum(ages, Decimal(0)) / len(ages) if ages and not unknown else None,
    )


def source_groups(rows: list[CaseOutcomeView]) -> list[CaseOutcomeGroup]:
    return [
        CaseOutcomeGroup(
            **summarize([r for r in rows if r.synthetic == synthetic]).model_dump(), synthetic=synthetic
        )
        for synthetic in (False, True)
        if any(r.synthetic == synthetic for r in rows)
    ]
