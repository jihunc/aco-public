"""Product configuration contracts; real Docker proof is an explicit setup lane.

These tests never call Docker or broaden the default offline kernel policy.
"""

from pathlib import Path


def test_images_have_no_embedded_secrets() -> None:
    api = Path("infra/api.Dockerfile").read_text()
    web = Path("infra/web.Dockerfile").read_text()
    for content in (api, web):
        assert "COPY . ." not in content
        assert "COPY --chown=10001:10001 . ." not in content
        assert ".env" not in content
        assert "USER 10001:10001" in content
    assert "uv sync --frozen --no-dev" in api
    assert "pnpm install --frozen-lockfile" in web
    assert ".next/standalone" in web and ".next/static" in web


def test_runtime_keeps_app_secrets_and_infrastructure_authority_separate() -> None:
    config = Path("compose.product.yaml").read_text()
    assert "env_file:" not in config
    assert "network_mode:" not in config
    app = config.split("x-product-app:", 1)[1].split("services:", 1)[0]
    for forbidden in (
        "KEYCLOAK_ADMIN",
        "STORAGE_ACCESS_KEY",
        "STORAGE_SECRET_KEY",
        "STORAGE_SSE_KEK",
        "OPENAI_API_KEY",
        "RESEND_API_KEY",
        "VAPI_API_KEY",
        ".env.live",
    ):
        assert forbidden not in app
    assert "STORAGE_APP_ACCESS_KEY" in app and "STORAGE_APP_SECRET_KEY" in app
    assert "EXTERNAL_PROVIDERS: fake" in app
    assert "AUTOMATION_KILL_SWITCH: ${PRODUCT_SYNTHETIC_KILL_SWITCH:-true}" in app
    assert "aco.worker" in config
    assert "aco.cli" in config and "migrate" in config
    assert "identity.aco.localhost" in config
    assert "native-only" in config


def test_missing_external_credentials_supports_offline_demo() -> None:
    from aco.cli import runtime_settings

    settings = runtime_settings(
        {
            "OPENAI_API_KEY": "must-not-be-used",
            "STORAGE_ACCESS_KEY": "admin-do-not-load",
            "LLM_PROVIDER": "fake",
        }
    )
    assert settings.external_providers == settings.llm_provider == "fake"
    assert settings.kill_switch and not settings.outbound_enabled
    assert settings.storage_access_key is None
    assert settings.storage_secret_key is None


def test_product_setup_is_private_nonoverwriting_and_does_not_read_existing_env(tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    target = tmp_path / "product"
    command = [
        sys.executable,
        "scripts/prepare-product.py",
        "--output",
        str(target),
        "--project",
        "aco-product-test",
    ]
    result = subprocess.run(command, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    private = target / "product.env"
    assert private.stat().st_mode & 0o777 == 0o600
    before = private.read_bytes()
    realm = json.loads((target / "realm.json").read_text())
    assert realm["clients"][0]["redirectUris"] == ["http://127.0.0.1:53100/api/auth/callback"]
    assert "${ACO_DEMO_PASSWORD}" in (target / "realm.json").read_text()
    assert b"OPENAI_API_KEY" not in before and b"RESEND_API_KEY" not in before
    repeated = subprocess.run(command, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert repeated.returncode != 0
    assert private.read_bytes() == before
    assert before.decode() not in result.stdout


def test_product_wrapper_refuses_main_project_before_docker(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    private = tmp_path / "wrong.env"
    private.write_text('PRODUCT_PROJECT="aco"\n')
    result = subprocess.run(
        [sys.executable, "scripts/product-compose.py", "--config", str(private), "ps"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and "dedicated product project" in result.stderr


def test_product_browser_runner_has_its_own_narrow_kernel_policy() -> None:
    policy = Path("infra/testing/product-smoke-macos.sb").read_text()
    runner = Path("scripts/smoke-product").read_text()
    source = Path("scripts/smoke-product.mjs").read_text()
    assert "(deny network*)" in policy
    assert "localhost:53100" in policy and "localhost:58180" in policy
    assert "localhost:58080" not in policy and "docker.sock" not in policy
    assert "sandbox-exec -f infra/testing/product-smoke-macos.sb" in runner
    assert "spawnSync" not in source and "execSync" not in source
    assert "--verify-restart" in source
    assert "sha256_matches" in source and "attachment_header" in source
