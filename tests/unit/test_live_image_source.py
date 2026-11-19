"""Public immutable-build identity, without Git/private configuration in the image."""

from pathlib import Path

import pytest

from aco.integrations.provider_smoke import source


def image_tree(tmp_path: Path) -> Path:
    for path in ("src/aco/module.py", "alembic/versions/r1.py", "pyproject.toml", "uv.lock", "alembic.ini"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# public synthetic build input\n")
    return tmp_path


def test_fake_build_needs_no_commit_but_live_requires_trusted_build_identity(tmp_path: Path) -> None:
    root = image_tree(tmp_path)
    source.write_build_stamp(root, "")
    with pytest.raises(ValueError, match="LIVE_BUILD_SOURCE_REQUIRED"):
        source.image_source_commit(root)
    (root / source.STAMP).unlink()
    source.write_build_stamp(root, "a" * 40)
    assert source.image_source_commit(root) == "a" * 40


@pytest.mark.parametrize("change", ["modify", "new", "missing", "symlink", "lock"])
def test_complete_shipped_inventory_refuses_drift(tmp_path: Path, change: str) -> None:
    root = image_tree(tmp_path)
    source.write_build_stamp(root, "a" * 40)
    target = root / "src/aco/module.py"
    if change == "modify":
        target.write_text("# changed source\n")
    elif change == "new":
        (target.parent / "extra.py").write_text("# unrecorded source\n")
    elif change == "missing":
        target.unlink()
    elif change == "symlink":
        target.unlink()
        target.symlink_to(root / "uv.lock")
    else:
        (root / "uv.lock").write_text("# changed dependency\n")
    with pytest.raises(ValueError, match="LIVE_BUILD_SOURCE"):
        source.image_source_commit(root)


def test_checkout_never_uses_stamp_to_hide_dirty_git_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    root = image_tree(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=synthetic",
            "-c",
            "user.email=synthetic@example.test",
            "commit",
            "-qm",
            "public fixture",
        ],
        cwd=root,
        check=True,
    )
    source.write_build_stamp(root, "a" * 40)
    monkeypatch.setattr(source, "source_root", lambda: root)
    (root / "src/aco/module.py").write_text("# dirty\n")
    with pytest.raises(ValueError, match="LIVE_SOURCE_CHANGED"):
        source.current_source_commit()


def test_image_campaign_path_is_fixed_and_cannot_initialize_new_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aco.integrations.provider_smoke import campaign

    root = image_tree(tmp_path)
    source.write_build_stamp(root, "a" * 40)
    monkeypatch.setattr(source, "source_root", lambda: root)
    assert campaign.canonical_campaign_path() == Path("/var/lib/aco/live/live-delivery-2026-11-19.sqlite3")
    with pytest.raises(campaign.CampaignBlocked, match="CAMPAIGN_CREATION_REQUIRES_OPERATOR_CHECKOUT"):
        campaign.LiveCampaign.create(
            campaign.canonical_campaign_path(), authorization_ref=campaign.AUTHORIZATION_REF
        )


@pytest.mark.parametrize("damage", ["absent", "invalid_json", "writable", "symlink"])
def test_image_stamp_itself_is_required_and_not_runtime_configuration(tmp_path: Path, damage: str) -> None:
    root = image_tree(tmp_path)
    source.write_build_stamp(root, "a" * 40)
    stamp = root / source.STAMP
    if damage == "absent":
        stamp.unlink()
    elif damage == "invalid_json":
        stamp.chmod(0o600)
        stamp.write_text("[]")
        stamp.chmod(0o444)
    elif damage == "writable":
        stamp.chmod(0o644)
    else:
        saved = root / "public-copy.json"
        stamp.rename(saved)
        stamp.symlink_to(saved)
    with pytest.raises(ValueError, match="LIVE_BUILD_SOURCE_REQUIRED"):
        source.image_source_commit(root)


def test_api_image_generates_stamp_from_shipped_inputs_with_safe_blank_default() -> None:
    dockerfile = Path("infra/api.Dockerfile").read_text()
    assert 'ARG ACO_SOURCE_COMMIT=""' in dockerfile
    assert "alembic.ini pyproject.toml uv.lock ./" in dockerfile
    assert (
        "PYTHONDONTWRITEBYTECODE=1 /app/.venv/bin/python -m aco.integrations.provider_smoke.source"
        in dockerfile
    )
    assert dockerfile.index('--build-stamp "$ACO_SOURCE_COMMIT"') < dockerfile.index("USER 10001:10001")
    assert "COPY .git" not in dockerfile and "COPY .env" not in dockerfile
