#!/usr/bin/env python3
"""No baseline auto-acceptance, credential verification or untracked-file traversal."""

from pathlib import Path

from aco.core.secret_scan import scan_tracked_secrets


def main() -> int:
    try:
        findings = scan_tracked_secrets(Path(__file__).resolve().parents[1])
    except Exception as exc:
        print(f"BLOCKED: maintained secret scan could not complete ({type(exc).__name__})")
        return 2
    for finding in findings:
        print(f"REVIEW_REQUIRED: {finding.path}:{finding.line}: {finding.kind}")
    print(f"{'FAIL' if findings else 'PASS'}: offline tracked-file secret scan; {len(findings)} findings")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
