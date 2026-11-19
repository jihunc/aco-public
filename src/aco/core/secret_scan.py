"""Maintained offline secret detection over tracked files; never read private untracked config."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import default_settings

from aco.core.secret_contexts import reviewed_context


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    kind: str


def scan_tracked_secrets(root: Path) -> list[SecretFinding]:
    root = root.resolve()
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    findings = []
    collection = SecretsCollection(root=str(root))
    with default_settings() as settings:
        # This is precisely the maintained --no-verify behavior. Never transmit
        # a detected candidate to any provider, even if its shape looks genuine.
        settings.disable_filters(
            "detect_secrets.filters.common.is_ignored_due_to_verification_policies",
            "detect_secrets.filters.heuristic.is_non_text_file",
            "detect_secrets.filters.heuristic.is_lock_file",
        )
        # Maintained extension heuristics skip textual CSS/lockfiles too. Scan
        # those files; the library handles genuinely undecodable binary input.
        for raw in listing.stdout.split(b"\0"):
            if not raw:
                continue
            name = raw.decode("utf-8")
            path = root / name
            if path.name.startswith(".env") and path.name != ".env.example":
                findings.append(SecretFinding(name, 0, "FORBIDDEN_PRIVATE_FILE"))
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                findings.append(SecretFinding(name, 0, "UNSCANNED_SYMLINK"))
                continue
            if not path.is_file():
                # Deleted tracked files cannot leak into the next checkout.
                continue
            collection.scan_file(name)
    # The library's JSON is hash-only; deliberately expose less: no value, hash,
    # source line or detector exception text in command/CI output.
    for filename, candidate in collection:
        if not reviewed_context(root, filename, candidate):
            findings.append(SecretFinding(filename, candidate.line_number, candidate.type))
    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.kind))
