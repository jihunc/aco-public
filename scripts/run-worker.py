"""Local worker uses the same explicit provider startup composition as the normal entrypoint."""

import asyncio

from local_runtime import local_settings

from aco.core.application import configured_worker
from aco.core.local_runtime import local_provider_selection

if __name__ == "__main__":
    asyncio.run(configured_worker(local_settings(), local_provider_selection()).run())
