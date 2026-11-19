"""Catch fixture omissions while keeping future consumer execution honest."""

from boundary_contracts import CONSUMERS, boundary_inventory


def test_all_boundary_cases_have_exactly_one_registered_consumer() -> None:
    inventory = boundary_inventory()
    assert len(inventory) == len(CONSUMERS)
    assert len({case for row in inventory.values() for case in row["case_ids"]}) == 128
    assert sum(len(row["case_ids"]) for row in inventory.values()) == 128
    assert inventory["evidence"]["test"] == (
        "tests/process/test_evidence_format.py::test_boundary_mock_live_evidence_separation"
    )
    assert all(row["state"] == "NOT_RUN" for row in inventory.values())
