"""Run only a named private product stack with no inherited provider environment."""

import argparse
import os
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(".tooling/product/local/product.env"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    private = args.config.resolve()
    values = dotenv_values(private) if private.is_file() else {}
    project = values.get("PRODUCT_PROJECT") or ""
    if not re.fullmatch(r"aco-product-[a-z0-9][a-z0-9-]{0,30}", project):
        parser.exit(2, "Refusing anything except an explicitly configured dedicated product project.\n")
    if private.stat().st_mode & 0o077:
        parser.exit(2, "Private product configuration must have mode 0600.\n")
    if not args.arguments or args.arguments[0] not in {
        "build",
        "up",
        "run",
        "restart",
        "stop",
        "ps",
        "logs",
        "exec",
        "down",
        "config",
    }:
        parser.exit(2, "Choose an explicit product Compose action.\n")
    if any(
        arg in {"-p", "-f", "--project-name", "--file", "--env-file"}
        or arg.startswith(("--project-name=", "--file=", "--env-file="))
        for arg in args.arguments
    ):
        parser.exit(2, "Project/configuration overrides are not allowed.\n")
    if args.arguments[0] == "config" and args.arguments != ["config", "--services"]:
        parser.exit(2, "Full resolved config can expose secrets; only config --services is allowed.\n")
    root = Path(__file__).resolve().parents[1]
    # Docker context metadata stays available; no provider/database/application
    # environment can override the explicit private project file.
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(Path.home())}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(private),
            "-f",
            str(root / "compose.yaml"),
            "-f",
            str(root / "compose.product.yaml"),
            *args.arguments,
        ],
        cwd=root,
        env=environment,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
