"""Portable application entrypoints; migration is always an explicit command."""

import argparse
import os
from collections.abc import Mapping
from pathlib import Path

from aco.core.settings import Settings, load_settings


def runtime_settings(environment: Mapping[str, str] | None = None) -> Settings:
    # load_settings consumes its explicit APP whitelist, never a dotenv file.
    return load_settings(os.environ if environment is None else environment)


def migrate(settings: Settings) -> None:
    from alembic.config import Config

    from alembic import command

    if settings.database_url is None:
        raise ValueError("Explicit DATABASE_URL required for migration")
    config = Config(str(Path("alembic.ini")))
    config.set_main_option("sqlalchemy.url", settings.database_url.get_secret_value().replace("%", "%%"))
    command.upgrade(config, "head")


def main() -> None:
    parser = argparse.ArgumentParser(description="ACO application runtime; never auto-seeds or resets data")
    parser.add_argument("command", choices=("api", "migrate"))
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be in 1..65535")
    settings = runtime_settings()
    if args.command == "migrate":
        try:
            migrate(settings)
        except Exception:
            # Database/provider exception text can contain connection configuration.
            parser.exit(1, "Migration failed; inspect controlled database diagnostics. No reset performed.\n")
        print("Explicit additive migration completed.")
        return
    import uvicorn

    from aco.core.application import configured_app

    uvicorn.run(configured_app(settings), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
