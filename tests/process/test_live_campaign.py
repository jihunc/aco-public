"""Durable trusted-operator aggregate bounds; no provider invocation or credentials."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from aco.integrations.provider_smoke.campaign import (
    AUTHORIZATION_REF,
    CampaignAllocation,
    CampaignBlocked,
    LiveCampaign,
)


@pytest.fixture(autouse=True)
def isolated_canonical_campaign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aco.integrations.provider_smoke.campaign.canonical_campaign_path",
        lambda: tmp_path / "campaign.sqlite3",
    )


def allocation(**overrides: object) -> CampaignAllocation:
    return CampaignAllocation.model_validate(
        {
            "id": uuid4(),
            "grant_id": uuid4(),
            "provider": "openai",
            "source_commit": "a" * 40,
            "max_usd": Decimal("1"),
            "max_requests": 3,
            "max_messages": 0,
            "max_sessions": 0,
            "max_seconds": 0,
            **overrides,
        }
    )


def test_retained_caps_are_global_idempotent_and_never_released(tmp_path: Path) -> None:
    path = tmp_path / "campaign.sqlite3"
    campaign = LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)
    assert path.stat().st_mode & 0o777 == 0o600
    first = allocation(max_usd=Decimal("19"))
    campaign.allocate(first)
    campaign.allocate(first)
    other = allocation(provider="resend", max_messages=1)
    campaign.allocate(other)
    assert campaign.totals()["reserved_usd"] == "20"
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_LIMIT"):
        campaign.allocate(allocation(max_usd=Decimal(".000000000001")))
    with pytest.raises(CampaignBlocked, match="ALLOCATION_CONFLICT"):
        campaign.allocate(first.model_copy(update={"max_requests": 2}))
    campaign.validate(first)
    with pytest.raises(CampaignBlocked, match="ALLOCATION_REQUIRED"):
        campaign.validate(allocation())
    with pytest.raises(CampaignBlocked):
        LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)
    assert campaign.totals()["reserved_usd"] == "20"


def test_request_email_voice_caps_and_no_pstn_scope(tmp_path: Path) -> None:
    campaign = LiveCampaign.create(tmp_path / "campaign.sqlite3", authorization_ref=AUTHORIZATION_REF)
    campaign.allocate(allocation(max_requests=50))
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_LIMIT"):
        campaign.allocate(allocation(max_requests=1))
    campaign.allocate(allocation(provider="resend", max_requests=20, max_messages=20))
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_LIMIT"):
        campaign.allocate(allocation(provider="resend", max_messages=1))
    campaign.allocate(allocation(provider="vapi", max_requests=3, max_sessions=3, max_seconds=900))
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_LIMIT"):
        campaign.allocate(allocation(provider="vapi", max_sessions=1, max_seconds=1))
    with pytest.raises(ValueError):
        allocation(provider="pstn")


def test_concurrent_allocations_cannot_overspend_and_require_exact_private_file(tmp_path: Path) -> None:
    path = tmp_path / "campaign.sqlite3"
    campaign = LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)

    def reserve(_: int) -> bool:
        try:
            LiveCampaign(path).allocate(allocation(max_usd=Decimal("11")))
            return True
        except CampaignBlocked:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, [1, 2])) == [False, True]
    assert campaign.totals()["reserved_usd"] == "11"
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(path)
    with pytest.raises(CampaignBlocked, match="PRIVATE_CAMPAIGN_REQUIRED"):
        LiveCampaign(alias).totals()
    path.chmod(0o644)
    with pytest.raises(CampaignBlocked, match="PRIVATE_CAMPAIGN_REQUIRED"):
        campaign.totals()


def test_one_authorization_cannot_open_an_independent_budget_file(tmp_path: Path) -> None:
    first = LiveCampaign.create(tmp_path / "campaign.sqlite3", authorization_ref=AUTHORIZATION_REF)
    first.allocate(allocation(max_usd=Decimal("20")))
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_AUTHORIZATION_REQUIRED"):
        LiveCampaign.create(tmp_path / "second.sqlite3", authorization_ref=AUTHORIZATION_REF)
    assert first.totals()["reserved_usd"] == "20"


def test_every_transaction_holds_shared_directory_lock_until_sqlite_close(tmp_path: Path) -> None:
    path = tmp_path / "campaign.sqlite3"
    campaign = LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)
    lock = path.with_name(path.name + ".lock")
    with campaign._db() as db:
        assert lock.is_dir()
        assert lock.stat().st_mode & 0o777 == 0o700
        assert db.execute("SELECT count(*) FROM policy").fetchone() == (1,)
    assert not lock.exists()


def test_abandoned_or_symlink_lock_blocks_without_automatic_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aco.integrations.provider_smoke.campaign as module

    path = tmp_path / "campaign.sqlite3"
    campaign = LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)
    lock = path.with_name(path.name + ".lock")
    lock.mkdir(mode=0o700)
    monkeypatch.setattr(module, "LOCK_TIMEOUT", 0.02, raising=False)
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_LOCKED"):
        campaign.totals()
    assert lock.is_dir()
    lock.rmdir()  # Explicit test-owner reconciliation only, never application takeover.
    lock.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_LOCKED"):
        campaign.totals()
    assert lock.is_symlink()


def test_transaction_exception_releases_only_owned_lock(tmp_path: Path) -> None:
    path = tmp_path / "campaign.sqlite3"
    campaign = LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)
    with pytest.raises(RuntimeError, match="synthetic abort"):
        with campaign._db():
            raise RuntimeError("synthetic abort")
    assert not path.with_name(path.name + ".lock").exists()
    assert campaign.totals()["reserved_usd"] == "0"


def test_replaced_lock_owner_is_not_removed(tmp_path: Path) -> None:
    path = tmp_path / "campaign.sqlite3"
    campaign = LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)
    lock = path.with_name(path.name + ".lock")
    with pytest.raises(CampaignBlocked, match="CAMPAIGN_LOCK_CHANGED"):
        with campaign._db():
            (lock / "owner").write_text("different-owner")
    assert lock.is_dir()


def test_gitless_container_cannot_use_campaign_before_mixed_client_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.integrations.provider_smoke import source

    path = tmp_path / "campaign.sqlite3"
    campaign = LiveCampaign.create(path, authorization_ref=AUTHORIZATION_REF)
    campaign.allocate(allocation(max_usd=Decimal("7")))
    before = path.read_bytes()
    image = tmp_path / "gitless-image"
    image.mkdir()
    monkeypatch.setattr(source, "source_root", lambda: image)
    with pytest.raises(CampaignBlocked, match="CONTAINER_CAMPAIGN_LOCKING_UNVERIFIED"):
        campaign.totals()
    assert path.read_bytes() == before
