"""Compatibility API module; shares fail-closed normal provider composition."""

import os

import uvicorn

from aco.core.application import configured_app
from aco.core.settings import load_settings

if __name__ == "__main__":
    uvicorn.run(
        configured_app(load_settings(os.environ)),
        host="0.0.0.0",
        port=58000,
        access_log=False,
    )
