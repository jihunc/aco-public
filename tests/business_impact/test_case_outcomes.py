"""Case outcome arithmetic: retained episodes, genuine dispositions and unknowns."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from boundary_evidence import BoundaryObserver

from aco.features.impact.case_outcomes import CaseFact, HistoryFact, calculate_case, summarize

START = datetime(2026, 11, 1, tzinfo=UTC)


def test_resolution_requires_every_issue_and_preserves_original_clock() -> None:
    first, second = uuid4(), uuid4()
    facts = CaseFact(case_id=uuid4(), opened_at=START, synthetic=True, invoice_ids=[first, second])
    events = [
        HistoryFact(id=uuid4(), at=START, event="created"),
        HistoryFact(id=uuid4(), at=START + timedelta(days=1), event="issue_resolved", invoice_id=first),
        HistoryFact(id=uuid4(), at=START + timedelta(days=1), event="resolved"),
    ]
    incomplete = calculate_case(facts, events, START + timedelta(days=2))
    assert incomplete.eligible_resolved is False
    assert incomplete.completeness == "INCOMPLETE"
    events.insert(
        2, HistoryFact(id=uuid4(), at=START + timedelta(days=1), event="issue_resolved", invoice_id=second)
    )
    resolved = calculate_case(facts, events, START + timedelta(days=2))
    assert resolved.eligible_resolved and resolved.autonomous
    assert resolved.resolution_seconds == Decimal(86400)
    events.extend(
        [
            HistoryFact(
                id=uuid4(),
                at=START + timedelta(days=2),
                event="reopened",
                affected_invoice_ids=[first],
                substantive_human=True,
            ),
            HistoryFact(id=uuid4(), at=START + timedelta(days=3), event="issue_resolved", invoice_id=first),
            HistoryFact(id=uuid4(), at=START + timedelta(days=4), event="resolved"),
        ]
    )
    reopened = calculate_case(facts, events, START + timedelta(days=2, hours=1))
    assert reopened.open_backlog and not reopened.eligible_resolved
    latest = calculate_case(facts, events, START + timedelta(days=5))
    assert latest.resolution_seconds == Decimal(345600)
    assert latest.resolved_episode_count == 2
    assert latest.substantive_human and not latest.autonomous
    assert calculate_case(facts, events, START + timedelta(days=1, hours=1)) == resolved


def test_empty_admin_and_unknown_do_not_manufacture_resolution_or_time() -> None:
    fact = CaseFact(case_id=uuid4(), opened_at=START, synthetic=True, invoice_ids=[uuid4()])
    admin = calculate_case(
        fact,
        [
            HistoryFact(id=uuid4(), at=START, event="created"),
            HistoryFact(id=uuid4(), at=START, event="closed", closure_reason="withdrawn"),
        ],
        START + timedelta(days=1),
    )
    assert not admin.eligible_resolved and not admin.open_backlog
    assert admin.administrative_closed
    unknown = calculate_case(fact, [], START + timedelta(days=1))
    assert unknown.completeness == "INCOMPLETE"
    result = summarize([admin, unknown])
    assert result.autonomous_resolution_percent is None
    assert result.human_intervention_percent is None
    assert result.mean_resolution_seconds is None
    empty = summarize([])
    assert empty.eligible_count == 0 and empty.human_intervention_percent is None


# These consume the normative history arithmetic. Actual approval-to-case
# production attribution is absent and remains explicitly unavailable in the API.
CASE_IDS = {
    "case_approval_only_not_substantive",
    "case_material_approval_edit_is_substantive",
    "case_administrative_closure_not_resolution",
    "case_reopen_retains_original_clock",
    "case_reopened_unresolved_backlog",
}
CASES = [
    c
    for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if c["id"] in CASE_IDS
]


@pytest.mark.parametrize("fixture", CASES, ids=lambda row: row["id"])
def test_boundary_case_history_and_handling(
    fixture: dict[str, Any], boundary_observer: BoundaryObserver
) -> None:
    data = fixture["input"]
    invoice = uuid4()
    opened = datetime.fromisoformat(data.get("opened_at", START.isoformat()))
    fact = CaseFact(case_id=uuid4(), opened_at=opened, synthetic=True, invoice_ids=[invoice])
    events = [HistoryFact(id=uuid4(), at=opened, event="created")]
    human_events = data.get("human_events", [])
    for name in human_events:
        events.append(
            HistoryFact(
                id=uuid4(),
                at=opened,
                event="handling",
                substantive_human=name in {"edit_resolution_content", "investigate_before_reopen"},
            )
        )

    def resolve(at: datetime) -> None:
        events.extend(
            [
                HistoryFact(
                    id=uuid4(), at=at, event="issue_resolved", invoice_id=invoice, evidence_id=uuid4()
                ),
                HistoryFact(id=uuid4(), at=at, event="resolved"),
            ]
        )

    if data.get("issue_evidence_verified") and data.get("all_links_resolved"):
        resolve(opened + timedelta(days=1))
    if data.get("closure_reason"):
        events.append(
            HistoryFact(
                id=uuid4(),
                at=opened + timedelta(days=1),
                event="closed",
                closure_reason=data["closure_reason"],
            )
        )
    prior = None
    if data.get("prior_resolved") or data.get("prior_resolved_at"):
        prior_at = datetime.fromisoformat(
            data.get("prior_resolved_at", (opened + timedelta(days=1)).isoformat())
        )
        resolve(prior_at)
        prior = calculate_case(fact, events, prior_at)
        reopen_at = datetime.fromisoformat(data.get("reopened_at", (opened + timedelta(days=2)).isoformat()))
        events.append(HistoryFact(id=uuid4(), at=reopen_at, event="reopened", affected_invoice_ids=[invoice]))
        if data.get("latest_resolved_at"):
            resolve(datetime.fromisoformat(data["latest_resolved_at"]))
    result = calculate_case(fact, events, opened + timedelta(days=6))
    totals = summarize([result])
    actual = {
        "resolved": result.eligible_resolved,
        "substantive_human_handling": result.substantive_human,
        "autonomous_resolution": result.autonomous,
        "resolved_kpi_count": totals.resolved_count,
        "autonomous_resolved_count": totals.autonomous_resolved_count,
        "resolution_seconds": result.resolution_seconds,
        "history_episodes": result.resolved_episode_count,
        "current_resolved_kpi_count": totals.resolved_count,
        "backlog_count": totals.open_backlog_count,
        "prior_snapshots_preserved": prior is not None
        and calculate_case(fact, events, opened + timedelta(days=1)) == prior,
    }
    observed = {key: actual[key] for key in fixture["expected"]}
    assert observed == fixture["expected"]
    boundary_observer.assert_observed(
        fixture["id"],
        {
            key: int(value) if isinstance(value, Decimal) and value == int(value) else value
            for key, value in observed.items()
        },
    )


def test_mixed_synthetic_and_observed_outcomes_have_separate_groups() -> None:
    from aco.features.impact.case_outcomes import source_groups

    values = []
    for synthetic in (True, False):
        fact = CaseFact(case_id=uuid4(), opened_at=START, synthetic=synthetic, invoice_ids=[uuid4()])
        values.append(
            calculate_case(
                fact, [HistoryFact(id=uuid4(), at=START, event="created")], START + timedelta(days=1)
            )
        )
    groups = source_groups(values)
    assert [(g.synthetic, g.eligible_count) for g in groups] == [(False, 1), (True, 1)]
    assert all(g.resolved_count == 0 and g.autonomous_resolution_percent is None for g in groups)
