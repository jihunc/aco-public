"""Current independent approval and exact callback authority against real PostgreSQL."""

import pytest
from voice_support import exercise_voice


@pytest.mark.parametrize(
    "boundary",
    [
        "success",
        "self_approval",
        "member_revoked",
        "source_changed",
        "expired",
        "kill",
        "unknown_start",
        "ended_start",
    ],
)
async def test_voice_tools_require_current_independent_approval_and_exact_call_scope(boundary: str) -> None:
    await exercise_voice(boundary)
