"""Initial fail-closed preflight shell. T19/T30 supply scoped executable live gates."""

from collections.abc import Mapping
from datetime import datetime

LEGACY_LIVE_AUTHORIZATION = "user-bounded-provider-smoke-2026-11-18"
DELIVERY_LIVE_AUTHORIZATION = "user-final-local-delivery-2026-11-19"
APPROVED_LIVE_AUTHORIZATIONS = frozenset({LEGACY_LIVE_AUTHORIZATION, DELIVERY_LIVE_AUTHORIZATION})


class LiveActivationBlocked(RuntimeError):
    pass


def authorize_live_profile(env: Mapping[str, str], *, now: datetime) -> None:
    # No marker, credential or budget can authorize an external action. The T02
    # shell intentionally cannot launch live tests; later gates extend this API.
    raise LiveActivationBlocked("Separately scoped live authorization and executable gates are required")
