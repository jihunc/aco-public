"""Fresh fake-only CI control-plane configuration; stdlib only, no services or credential reads."""

import base64
import os
import secrets
import subprocess
import sys
from pathlib import Path

GENERATED = (
    "DATABASE_PASSWORD",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "STORAGE_APP_ACCESS_KEY",
    "STORAGE_APP_SECRET_KEY",
    "STORAGE_SSE_KEK",
    "KEYCLOAK_ADMIN_PASSWORD",
    "OIDC_CLIENT_SECRET",
    "DEMO_PASSWORD",
    "SESSION_SECRET",
    "TOKEN_ENCRYPTION_KEY",
)
DEFAULTS = {
    "APP_ENV": "local",
    "EXTERNAL_PROVIDERS": "fake",
    "LLM_PROVIDER": "fake",
    "ACCOUNTING_ADAPTER": "demo",
    "EMAIL_ADAPTER": "mock",
    "VOICE_ADAPTER": "mock",
    "ACO_TEST_PROFILE": "mock",
    "OUTBOUND_ENABLED": "false",
    "AUTOMATION_KILL_SWITCH": "true",
    "RECORDING_ENABLED": "false",
    "TRANSCRIPTION_ENABLED": "false",
    "SYNTHETIC_EMAIL_ENABLED": "false",
    "SYNTHETIC_EMAIL_INGRESS_ENABLED": "false",
    "SYNTHETIC_VOICE_ENABLED": "false",
    "ACO_AGENT_LIVE_CONFIG_PATH": "",
    "ACO_AGENT_LIVE_GRANT_ID": "",
    "ACO_EMAIL_LIVE_CONFIG_PATH": "",
    "ACO_LIVE_AUTHORIZATION_REF": "",
    "ACO_LIVE_CAMPAIGN_FILE": "",
    "ACO_LIVE_ALLOCATION_ID": "",
    "ACO_DEPLOYED_ORIGIN": "",
}


def prepare(root: Path) -> None:
    destination = root / ".env"
    identities = root / ".tooling/storage/s3-identities.json"
    if os.path.lexists(destination) or os.path.lexists(identities):
        raise ValueError("Fresh setup requires absent private configuration")
    template = (root / ".env.example").read_text()
    pairs = [line.split("=", 1) for line in template.splitlines() if line and not line.startswith("#")]
    values = dict(pairs)
    if len(values) != len(pairs) or any(values.get(name) != value for name, value in DEFAULTS.items()):
        raise ValueError("Fake-only CI defaults required")
    if any(values.get(name) != "" for name in GENERATED):
        raise ValueError("Private local placeholders required")
    for name in GENERATED:
        value = (
            base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            if name == "TOKEN_ENCRYPTION_KEY"
            else secrets.token_hex(32)
        )
        values[name] = value
        template = template.replace(name + "=\n", name + "=" + value + "\n")
    # O_EXCL prevents accidental regeneration/reset of a pre-existing local installation.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(template)
    # Only generated storage controls reach the canonical renderer. It writes placeholders,
    # not secrets, and keeps the application identity restricted to tenant artifact objects.
    subprocess.run(
        [sys.executable, str(root / "scripts/render_storage_config.py"), "--output", str(identities)],
        env={name: value for name, value in values.items() if name.startswith("STORAGE_")},
        check=True,
        capture_output=True,
    )


if __name__ == "__main__":
    try:
        prepare(Path.cwd())
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Fresh CI setup refused; check fake-only template and absent private outputs.", file=sys.stderr)
        raise SystemExit(2) from None
