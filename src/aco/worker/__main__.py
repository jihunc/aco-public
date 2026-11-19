"""Portable maintained worker; typed APP environment only, fake providers by default."""

import asyncio

from aco.cli import runtime_settings
from aco.core.application import configured_worker


def main() -> None:
    # Only explicit server selection can compose an existing authorized grant.
    asyncio.run(configured_worker(runtime_settings()).run())


if __name__ == "__main__":
    main()
