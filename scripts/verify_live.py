#!/usr/bin/env python3
"""No provider invocation in the bootstrap authorization shell."""

import os
from datetime import UTC, datetime

from aco.core.live import LiveActivationBlocked, authorize_live_profile


def main() -> int:
    try:
        authorize_live_profile(os.environ, now=datetime.now(UTC))
    except LiveActivationBlocked as error:
        print(f"BLOCKED: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
