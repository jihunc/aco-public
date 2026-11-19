"""Failure-only CI storage diagnostics. Fixed reads; no configuration or environment dump."""

import re
import subprocess

HEX_CONTROL = re.compile(r"[0-9a-fA-F]{64,}")
OUTPUT_LIMIT = 16000


def capture(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        # Exception text/partial output is deliberately not emitted.
        return None


def emit(label: str, result: subprocess.CompletedProcess[str] | None) -> None:
    print(f"--- {label} ---")
    if result is None:
        print("Diagnostic unavailable or timed out.")
        return
    print(f"Diagnostic command exit: {result.returncode}")
    # prepare-ci generates every storage credential and KEK as 64 hex characters.
    # Redact before truncation so a boundary cannot expose a partial credential.
    # This deliberately also redacts long public hashes. Never read .env or Config.Env.
    output = HEX_CONTROL.sub("[REDACTED_HEX]", result.stderr + result.stdout)
    print(output[:OUTPUT_LIMIT])
    if len(output) > OUTPUT_LIMIT:
        print("[OUTPUT_TRUNCATED]")


def main() -> None:
    emit(
        "compose service state",
        capture(
            [
                "docker",
                "compose",
                "ps",
                "--all",
                "--format",
                "table {{.Name}}\t{{.Service}}\t{{.State}}\t{{.Health}}\t{{.ExitCode}}",
            ]
        ),
    )
    emit(
        "storage-core last 120 log lines",
        capture(["docker", "compose", "logs", "--no-color", "--tail", "120", "storage-core"]),
    )
    identifier = capture(["docker", "compose", "ps", "--all", "-q", "storage-core"])
    if identifier is not None and identifier.returncode == 0:
        container_id = identifier.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            emit(
                "storage-core state and health",
                capture(["docker", "inspect", "--format", "{{json .State}}", container_id]),
            )
        else:
            print("Storage-core container identity unavailable.")
    else:
        print("Storage-core container identity unavailable.")
    emit("runner root filesystem capacity", capture(["df", "-h", "/"]))
    emit("runner memory capacity", capture(["free", "-m"]))


if __name__ == "__main__":
    main()
