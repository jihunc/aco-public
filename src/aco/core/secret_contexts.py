"""Reviewed public metadata contexts; never disable a detector for an entire file."""

import base64
import json
import re
from hashlib import sha256
from pathlib import Path

from detect_secrets.core.potential_secret import PotentialSecret


def reviewed_context(root: Path, filename: str, candidate: PotentialSecret) -> bool:
    value = candidate.secret_value
    if value is None:
        return False
    lines = (root / filename).read_text().splitlines()
    if not 1 <= candidate.line_number <= len(lines):
        return False
    line = lines[candidate.line_number - 1]
    if candidate.type == "Secret Keyword" and value == "whsec_":
        # This is only the public Svix format prefix, never a signing secret.
        return True
    if candidate.type == "Base64 High Entropy String" and filename == "pnpm-lock.yaml":
        match = re.fullmatch(r"\s*resolution: \{integrity: sha512-([A-Za-z0-9+/]+={0,2})\}\s*", line)
        if match and value in {match[1], "sha512-" + match[1]}:
            return len(base64.b64decode(match[1], validate=True)) == 64
    if candidate.type == "Hex High Entropy String" and re.fullmatch(r"[0-9a-f]{64}", value):
        if filename == "uv.lock" and re.search(r'\bhash = "sha256:' + value + r'"', line):
            return True
        if filename == "src/aco/core/secret_contexts.json":
            return bool(re.fullmatch(r'\s*"line_sha256": "' + value + r'",?\s*', line))
    allowlist = Path(__file__).with_name("secret_contexts.json")
    reviewed = json.loads(allowlist.read_text())
    return any(
        row["path"] == filename
        and row["detector"] == candidate.type
        and row["line_sha256"] == sha256(line.encode()).hexdigest()
        for row in reviewed
    )
