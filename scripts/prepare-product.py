"""Generate a fresh private local product project; never reads or replaces existing credentials."""

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

RESERVED_NATIVE_PORTS = {53000, 58000, 58080, 55432, 59000, 58333}


def write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


def prepare(
    output: Path, project: str, web_port: int, api_port: int, identity_port: int,
    *, synthetic_workflows: bool = False,
) -> Path:
    if not re.fullmatch(r"aco-product-[a-z0-9][a-z0-9-]{0,30}", project):
        raise ValueError("Use a dedicated aco-product- project name")
    ports = {web_port, api_port, identity_port}
    if len(ports) != 3 or any(p < 1024 or p > 65535 for p in ports) or ports & RESERVED_NATIVE_PORTS:
        raise ValueError("Choose three distinct non-native unprivileged ports")
    output = output.resolve()
    if any(character in str(output) for character in "\n\r\"'"):
        raise ValueError("Unsupported output path")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    root = Path(__file__).resolve().parents[1]
    database_password = secrets.token_hex(24)
    values = {
        "PRODUCT_PROJECT": project,
        "PRODUCT_WEB_PORT": str(web_port),
        "PRODUCT_API_PORT": str(api_port),
        "PRODUCT_IDENTITY_PORT": str(identity_port),
        "PRODUCT_DATABASE_URL": f"postgresql+psycopg://aco:{database_password}@postgres:5432/aco",
        "PRODUCT_STORAGE_CONFIG": str(output / "s3-identities.json"),
        "PRODUCT_REALM_CONFIG": str(output / "realm.json"),
        "DATABASE_PASSWORD": database_password,
        "STORAGE_BUCKET": "aco-artifacts",
        "STORAGE_ACCESS_KEY": secrets.token_hex(16),
        "STORAGE_SECRET_KEY": secrets.token_hex(32),
        "STORAGE_APP_ACCESS_KEY": secrets.token_hex(16),
        "STORAGE_APP_SECRET_KEY": secrets.token_hex(32),
        "STORAGE_SSE_KEK": secrets.token_hex(32),
        "KEYCLOAK_ADMIN_USER": "aco-local-admin",
        "KEYCLOAK_ADMIN_PASSWORD": secrets.token_hex(24),
        "OIDC_CLIENT_SECRET": secrets.token_hex(32),
        "DEMO_PASSWORD": secrets.token_urlsafe(24),
        "SESSION_SECRET": secrets.token_hex(32),
        "TOKEN_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "SYNTHETIC_EMAIL_ENABLED": str(synthetic_workflows).lower(),
        "SYNTHETIC_VOICE_ENABLED": str(synthetic_workflows).lower(),
        "SYNTHETIC_EMAIL_INGRESS_ENABLED": str(synthetic_workflows).lower(),
        "PRODUCT_SYNTHETIC_OUTBOUND_ENABLED": str(synthetic_workflows).lower(),
        "PRODUCT_SYNTHETIC_KILL_SWITCH": str(not synthetic_workflows).lower(),
        "SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET": "whsec_"
        + base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    # Only newly generated inputs reach the existing policy renderer.
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts/render_storage_config.py"),
            "--output",
            values["PRODUCT_STORAGE_CONFIG"],
        ],
        env=values,
        check=True,
        capture_output=True,
    )
    realm = json.loads((root / "infra/identity/realm.json").read_text())
    realm["clients"][0]["redirectUris"] = [f"http://127.0.0.1:{web_port}/api/auth/callback"]
    realm["clients"][0]["webOrigins"] = [f"http://127.0.0.1:{web_port}"]
    write_private(output / "realm.json", json.dumps(realm, indent=2) + "\n")
    private = output / "product.env"
    write_private(private, "\n".join(f'{name}="{value}"' for name, value in values.items()) + "\n")
    return private


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="aco-product-local")
    parser.add_argument("--output", type=Path, default=Path(".tooling/product/local"))
    parser.add_argument("--web-port", type=int, default=53100)
    parser.add_argument("--api-port", type=int, default=58100)
    parser.add_argument("--identity-port", type=int, default=58180)
    parser.add_argument(
        "--synthetic-workflows", action="store_true",
        help="Explicit fake-only email/voice workflow rehearsal; never selects live providers",
    )
    args = parser.parse_args()
    try:
        private = prepare(
            args.output, args.project, args.web_port, args.api_port, args.identity_port,
            synthetic_workflows=args.synthetic_workflows,
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.exit(
            2,
            "Product setup refused or incomplete; existing files remain untouched. "
            "Choose a fresh directory and project.\n",
        )
    print(f"Fresh private local setup prepared at {private}. No credentials printed; no services started.")


if __name__ == "__main__":
    main()
