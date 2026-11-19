"""Boundary consumer inventory. Future consumers remain NOT_RUN, never mock PASS.

Registration is planning metadata; only a consumer's executed real production
assertions establish evidence. T28 tightens repository-wide execution coverage.
"""

import json
from pathlib import Path
from typing import Literal, TypedDict

# Literal reviewed selectors from the release verification traceability fixture.
CONSUMERS = [
    (
        "contact",
        "domain=contact",
        "T08",
        "tests/integration/test_policy_guards.py::test_boundary_customer_contact_caps",
    ),
    (
        "schedule",
        "domain=schedule",
        "T08",
        "tests/unit/test_policy_calendar.py::test_boundary_recipient_calendar",
    ),
    (
        "freshness",
        "domain=freshness",
        "T08",
        "tests/integration/test_policy_guards.py::test_boundary_source_reverification",
    ),
    (
        "ticket",
        "domain=ticket",
        "T10",
        "tests/integration/test_reminder_dispatch.py::test_dispatch_ticket_boundary_cases",
    ),
    (
        "approval",
        "domain=approval",
        "T16",
        "tests/integration/test_approval_scope.py::test_boundary_approval_expiry_scope_and_self_approval",
    ),
    (
        "authorization",
        "domain=authorization",
        "T03",
        "tests/integration/test_identity_tenancy.py::test_boundary_role_authorization",
    ),
    (
        "policy_archive",
        "domain=policy_archive",
        "T08",
        "tests/integration/test_policy_guards.py::test_boundary_policy_archive",
    ),
    (
        "holds",
        "domain=holds",
        "T08",
        "tests/integration/test_policy_guards.py::test_boundary_composed_hold_release",
    ),
    (
        "promise_capture",
        "ids=promise_unknown_allocation_needs_confirmation,promise_unknown_scope_blocks_review_without_invented_links,promise_relative_date_missing_timezone,promise_replacement_requires_confirmation",
        "T13",
        "tests/integration/test_promise_lifecycle.py::test_boundary_promise_confirmation",
    ),
    (
        "promise_residual",
        "ids=promise_reversal_exact_residual_before_cutoff,promise_reversal_excess_residual_before_cutoff,promise_reversal_insufficient_residual_before_cutoff,promise_reversal_exact_residual_after_cutoff,promise_reversal_excess_residual_after_cutoff,promise_reversal_insufficient_residual_after_cutoff,promise_reversal_stale_retains_verification_hold,promise_reversal_excludes_wrong_invoice_cash,promise_reversal_excludes_already_reused_cash,promise_reversed_performance_reopens_missed",
        "T18",
        "tests/integration/test_promise_performance.py::test_boundary_reversal_residual_preserves_history_and_independent_holds",
    ),
    (
        "promise_interval",
        "ids=promise_paid_by_preconfirmation_cash_not_fulfilled,promise_partial_commitment_full_performance_invoice_open,promise_exact_cutoff_cash_is_late,promise_after_cutoff_cash_preserves_missed,promise_full_commitment_within_window",
        "T18",
        "tests/integration/test_promise_performance.py::test_normative_interval_fixture_with_real_source_and_pending_policy",
    ),
    (
        "promise_cutoff",
        "ids=promise_end_of_date_cutoff,promise_partial_instalment_not_fulfilled,promise_stale_at_cutoff_keeps_verification_hold",
        "T18",
        "tests/integration/test_promise_performance.py::test_normative_cutoff_freshness_and_partial_cash",
    ),
    (
        "promise_nonreuse",
        "ids=promise_source_allocation_not_reused",
        "T18",
        "tests/integration/test_promise_performance.py::test_exact_performance_interval_and_nonreuse",
    ),
    (
        "promise_rejected",
        "ids=promise_reversal_ambiguous_allocation_retains_verification_hold,promise_reversal_currency_ambiguity_retains_review_hold",
        "T18",
        "tests/integration/test_promise_rejected_fixtures.py::test_normative_rejected_source_retains_prior_fulfilled_history",
    ),
    (
        "case_lifecycle",
        "ids=case_payment_does_not_resolve_dispute,case_multi_invoice_partial_resolution",
        "T12",
        "tests/integration/test_case_lifecycle.py::test_boundary_case_issue_resolution",
    ),
    (
        "case_metrics",
        "ids=case_approval_only_not_substantive,case_material_approval_edit_is_substantive,case_administrative_closure_not_resolution,case_reopen_retains_original_clock,case_reopened_unresolved_backlog",
        "T25",
        "tests/business_impact/test_case_outcomes.py::test_boundary_case_history_and_handling",
    ),
    (
        "finance_arithmetic",
        "ids=cohort_dst_30_calendar_days,cohort_fully_prepaid_excluded,cohort_immature_not_complete,cohort_includes_D_excludes_H,cohort_missing_opening_history_not_original_amount,cohort_negative_exposure_excluded,cohort_overpayment_capped,cohort_partial_prepayment_reduces_opening,cohort_post_opening_credit_not_cash,cohort_pre_D_cash_refund_inconsistent_opening_unavailable,cohort_pre_D_cash_refund_missing_allocation_unavailable,cohort_pre_D_cash_refund_within_horizon,cohort_pre_D_cash_reversal_preserves_negative_raw_net,cohort_pre_D_cash_reversal_within_horizon,cohort_pre_D_effective_refund_not_deducted_twice,cohort_unallocated_refund_unavailable,cohort_writeoff_not_cash,days_to_collect_uses_final_cash_payment",
        "T24",
        "tests/business_impact/test_financial_edges.py::test_boundary_financial_source_events",
    ),
    (
        "finance_persisted",
        "ids=cohort_post_horizon_reversal_reduces_asof,cohort_linked_refund_reduces_cash,cohort_due_date_correction_versioned,cohort_restated_version_preserves_prior,cohort_pre_D_cash_refund_after_horizon_restated,cohort_pre_D_cash_reversal_after_horizon_restated,cohort_pre_D_cash_refund_late_observed_restated,cohort_refund_observed_after_asof_excluded",
        "T24",
        "tests/business_impact/test_financial_persistence_boundaries.py::test_boundary_persisted_financial_history",
    ),
    (
        "finance_metrics",
        "ids=metric_zero_denominator_null,metric_currency_mix_no_FX",
        "T24",
        "tests/business_impact/test_metric_boundaries.py::test_boundary_unavailable_and_currency_separation",
    ),
    (
        "cost",
        "domain=cost",
        "T25",
        "tests/business_impact/test_cost_allocation.py::test_boundary_cost_component_allocation",
    ),
    (
        "budget",
        "domain=budget",
        "T06",
        "tests/integration/test_usage_budgets.py::test_boundary_budget_authorization_and_reservations",
    ),
    (
        "roi",
        "domain=roi",
        "T25",
        "tests/business_impact/test_labour_estimates.py::test_boundary_estimated_roi",
    ),
    (
        "reliability",
        "domain=reliability",
        "T07",
        "tests/integration/test_action_recovery.py::test_boundary_safe_retry_and_unknown",
    ),
    (
        "voice",
        "domain=voice",
        "T21",
        "tests/integration/test_voice_workflow.py::test_boundary_voice_capture_and_gate_classification",
    ),
    (
        "retention",
        "domain=retention",
        "T23",
        "tests/integration/test_consent_retention.py::test_boundary_retention_and_legal_review",
    ),
    (
        "artifact_basic",
        "ids=artifact_max_size_boundary,artifact_oversize_visible_failure,artifact_proxy_rejects_wrong_tenant",
        "T09",
        "tests/integration/test_private_artifacts.py::test_boundary_artifact_limits_and_tenancy",
    ),
    (
        "artifact_revocation",
        "ids=artifact_proxy_same_tenant_consent_revoked,artifact_proxy_same_tenant_deletion_revoked",
        "T23",
        "tests/integration/test_consent_retention.py::test_same_tenant_read_revocation_boundary_cases",
    ),
    (
        "demo",
        "domain=demo",
        "T27",
        "tests/integration/test_demo_reset_safety.py::test_boundary_demo_guards_and_generation",
    ),
    (
        "identity",
        "domain=identity",
        "T04",
        "tests/integration/test_accounting_integrity.py::test_boundary_connection_scoped_payment_identity",
    ),
    ("sse", "domain=sse", "T17", "tests/integration/test_sse_replay.py::test_boundary_step_event_cursors"),
    (
        "adapter_capability",
        "ids=adapter_unsupported_not_false_success",
        "T26",
        "tests/contracts/test_provider_swap.py::test_boundary_unsupported_capability",
    ),
    (
        "adapter_sender",
        "ids=adapter_webhook_not_sender_authority",
        "T11",
        "tests/integration/test_email_ingress.py::test_boundary_webhook_sender_authority",
    ),
    (
        "evidence",
        "domain=evidence",
        "T02",
        "tests/process/test_evidence_format.py::test_boundary_mock_live_evidence_separation",
    ),
]


class ConsumerInventory(TypedDict):
    task: str
    test: str
    case_ids: list[str]
    state: Literal["NOT_RUN"]


def boundary_inventory() -> dict[str, ConsumerInventory]:
    cases = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    result: dict[str, ConsumerInventory] = {}
    assigned: set[str] = set()
    for name, selector, task, test in CONSUMERS:
        kind, value = selector.split("=", 1)
        matches = [
            case
            for case in cases
            if (case["domain"] == value if kind == "domain" else case["id"] in value.split(","))
        ]
        ids = [case["id"] for case in matches]
        overlap = assigned.intersection(ids)
        if not ids or overlap:
            raise ValueError(f"Empty/overlapping boundary group: {name}")
        assigned.update(ids)
        result[name] = {"task": task, "test": test, "case_ids": ids, "state": "NOT_RUN"}
    if assigned != {case["id"] for case in cases}:
        raise ValueError("Unmapped boundary fixture cases")
    return result
