#!/usr/bin/env python3
"""Render the pinned provider's static identity policy without writing credential values."""

import argparse
import json
import os
import re
import tempfile
from pathlib import Path


def render(output: Path) -> None:
    bucket = os.environ.get("STORAGE_BUCKET", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Storage bucket must be a valid conservative S3 bucket name")
    keys = [
        os.environ.get(name, "")
        for name in (
            "STORAGE_ACCESS_KEY",
            "STORAGE_SECRET_KEY",
            "STORAGE_APP_ACCESS_KEY",
            "STORAGE_APP_SECRET_KEY",
        )
    ]
    if (
        any(not value or any(ord(c) < 33 or ord(c) > 126 for c in value) for value in keys)
        or any(len(keys[index]) < 16 for index in (1, 3))
        or keys[0] == keys[2]
        or keys[1] == keys[3]
    ):
        raise ValueError("Distinct managed admin/application storage credentials required")
    if not re.fullmatch("[a-fA-F0-9]{64}", os.environ.get("STORAGE_SSE_KEK", "")):
        raise ValueError("Persistent native encryption key required")
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/tenants/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetEncryptionConfiguration", "s3:GetBucketVersioning"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
        ],
    }
    document = {
        "identities": [
            {
                "name": "aco-storage-administrator",
                "credentials": [
                    {"accessKey": "${AWS_ACCESS_KEY_ID}", "secretKey": "${AWS_SECRET_ACCESS_KEY}"}
                ],
                "actions": ["Admin", "Read", "Write", "List", "Tagging"],
            },
            {
                "name": "aco-artifact-application",
                "credentials": [
                    {"accessKey": "${STORAGE_APP_ACCESS_KEY}", "secretKey": "${STORAGE_APP_SECRET_KEY}"}
                ],
                "policyNames": ["aco-artifact-data"],
            },
        ],
        "policies": [{"name": "aco-artifact-data", "content": json.dumps(policy, separators=(",", ":"))}],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".storage-identities-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".tooling/storage/s3-identities.json"))
    args = parser.parse_args()
    try:
        render(args.output)
    except (OSError, ValueError):
        parser.exit(2, "Storage identity configuration rejected; check managed setup inputs.\n")
