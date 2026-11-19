"""Thin portable Compose interface; provider selection never creates execution authority."""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from aco.core.development import DevelopmentConfig, load_development

ROOT = Path(__file__).resolve().parents[2]


def initialize(
    path: Path, mode: str, *, root: Path = ROOT, output: Path | None = None,
    project: str | None = None, web_port: int | None = None,
    api_port: int | None = None, identity_port: int | None = None,
) -> None:
    if mode not in {"offline", "live"} or path.exists() or path.is_symlink():
        raise ValueError("DEVELOPMENT_INITIALIZATION_BLOCKED")
    output = (output or root / ".tooling/development" / mode).absolute()
    offset = 0 if mode == "offline" else 10
    subprocess.run(
        [sys.executable, str(root / "scripts/prepare-product.py"), "--synthetic-workflows",
         "--output", str(output), "--project", project or f"aco-product-dev-{mode}-{secrets.token_hex(3)}",
         "--web-port", str(web_port or 53420 + offset), "--api-port", str(api_port or 58420 + offset),
         "--identity-port", str(identity_port or 58480 + offset)],
        check=True, capture_output=True,
    )
    for directory in (output / "providers", output / "campaign"):
        directory.mkdir(mode=0o700)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {
        "DEV_MODE": mode, "DEV_INFRA_CONFIG": str(output / "product.env"),
        "DEV_PROVIDER_DIRECTORY": str(output / "providers"),
        "DEV_CAMPAIGN_DIRECTORY": str(output / "campaign"),
        "APP_ENV": "local", "ACCOUNTING_ADAPTER": "demo", "LLM_PROVIDER": "fake",
        "EMAIL_ADAPTER": "mock", "VOICE_ADAPTER": "mock",
        "OUTBOUND_ENABLED": str(mode == "offline").lower(),
        "AUTOMATION_KILL_SWITCH": str(mode != "offline").lower(),
    }
    write_private(path, values)
    load_development(path)


def write_private(path: Path, values: dict[str, str]) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write("\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "\n")


def process_environment(values: dict[str, str]) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(Path.home()), **values,
    }


def compose(config: DevelopmentConfig, *arguments: str) -> None:
    directory = config.infra_path.parent
    # A fresh per-invocation file avoids modifying operator input or printing resolved secrets.
    reviewed_commit = source_commit(config) if arguments[0] in {"build", "up", "run", "config"} else ""
    private = directory / f"backend-{secrets.token_hex(8)}.env"
    write_private(private, config.backend_environment())
    environment = dict(config.infrastructure)
    environment.update(config.backend_environment())
    environment.update({
        "DEV_UID": str(os.getuid()), "DEV_GID": str(os.getgid()),
        "DEV_BACKEND_ENV": str(private),
        "DEV_PROVIDER_DIRECTORY": str(config.provider_directory),
        "DEV_CAMPAIGN_DIRECTORY": str(config.campaign_directory),
        "DEV_PROVIDER_NETWORK_INTERNAL": str(not real_selected(config)).lower(),
        "DEV_SOURCE_COMMIT": reviewed_commit,
    })
    try:
        subprocess.run(
            ["docker", "compose", "--project-name", config.project, "--env-file", os.devnull,
             "-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "compose.product.yaml"),
             "-f", str(ROOT / "compose.development.yaml"), *arguments],
            cwd=ROOT, env=process_environment(environment), check=True,
        )
    finally:
        private.unlink()


def real_selected(config: DevelopmentConfig) -> bool:
    return (config.llm, config.email, config.voice) != ("fake", "mock", "mock")


def source_commit(config: DevelopmentConfig) -> str:
    if not real_selected(config):
        return ""
    # Same canonical retained ledger, never a new development allowance or copied SQLite file.
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()
    canonical = Path(common).parent / ".tooling"
    ledger = canonical / "live-delivery-2026-11-19.sqlite3"
    if config.campaign_directory != canonical or not ledger.is_file():
        raise ValueError("CANONICAL_RETAINED_CAMPAIGN_REQUIRED")
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    if dirty.strip():
        raise ValueError("CLEAN_REVIEWED_SOURCE_REQUIRED")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def check_runtime() -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from aco.core.application import configured_app, configured_worker
    from aco.core.settings import load_settings

    selected = load_settings(os.environ)
    worker = configured_worker(selected)
    try:
        app = configured_app(selected)
        engines = [worker.engine]
        for name in ("sessions", "business_sessions"):
            sessions = getattr(app.state, name, None)
            if sessions is not None and sessions.kw.get("bind") is not None:
                engines.append(sessions.kw["bind"])

        async def dispose() -> None:
            for engine in engines:
                if isinstance(engine, AsyncEngine):
                    await engine.dispose()
                else:
                    engine.dispose()

        asyncio.run(dispose())
    except BaseException:
        asyncio.run(worker.engine.dispose())
        raise


def up(config: DevelopmentConfig) -> int:
    # An invalid newly selected provider must not leave the previous selection executing.
    compose(config, "stop")
    compose(config, "build")
    compose(config, "up", "-d", "--wait", "--wait-timeout", "180", "postgres", "storage", "identity")
    compose(config, "run", "--rm", "migrate")
    # Existing constructors validate persisted authority before listeners/workers start; no wire/job.
    compose(config, "run", "--rm", "--no-deps", "api", "python", "-m", "aco.development", "check-runtime")
    compose(config, "up", "-d", "--wait", "--wait-timeout", "180", "api", "worker", "web", "product-ingress")
    print(f"ACO: http://127.0.0.1:{config.web_port}; project {config.project}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("init", "up", "down", "restart", "check", "credentials", "check-runtime")
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--project")
    for name in ("web", "api", "identity"):
        parser.add_argument(f"--{name}-port", type=int)
    args = parser.parse_args()
    try:
        if args.action == "check-runtime":
            check_runtime()
            print("PASS: shared API/worker composition; no provider request performed.")
            return
        if args.config is None:
            raise ValueError("EXPLICIT_CONFIGURATION_REQUIRED")
        if args.action == "init":
            initialize(args.config, args.mode, output=args.output, project=args.project,
                       web_port=args.web_port, api_port=args.api_port, identity_port=args.identity_port)
            print("Prepared fresh private configuration; no services, grants or provider calls started.")
            return
        config = load_development(args.config)
        if args.action == "credentials":
            print("Synthetic demo login: owner@example.test")
            print("Password: " + config.infrastructure["DEMO_PASSWORD"])
        elif args.action == "down":
            compose(config, "stop")  # Deliberately retains containers, volumes and every database row.
        elif args.action == "restart":
            raise SystemExit(up(config))
        elif args.action == "check":
            compose(config, "config", "--quiet")
            print("PASS: Compose/configuration shape; provider authority is checked at startup.")
        else:
            raise SystemExit(up(config))
    except (Exception, KeyboardInterrupt):
        parser.exit(2, "DEVELOPMENT_STARTUP_BLOCKED: inspect private setup, authority and readiness.\n")


if __name__ == "__main__":
    main()
