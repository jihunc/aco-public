"""Private synthetic smoke settings shared by offline provider tests."""

from pathlib import Path


def private_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / ".env.live"
    path.write_text(
        "ACO_LIVE_KILL_SWITCH=false\nOPENAI_API_KEY=fake-secret\nLLM_MODEL=gpt-6.1-sol\n"
        "ACO_LIVE_MAX_REQUESTS=3\nACO_LIVE_MAX_INPUT_TOKENS=2048\n"
        "ACO_LIVE_MAX_OUTPUT_TOKENS=512\nACO_LIVE_MAX_USD=1.00\n" + extra
    )
    path.chmod(0o600)
    return path


