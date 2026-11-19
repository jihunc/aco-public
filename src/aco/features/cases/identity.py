"""Conservative exact claim identity; uncertain similarity never merges cases."""

import hashlib
import json
from uuid import UUID


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def issue_identity(customer_id: UUID, issue_type: str, claim_key: str) -> str:
    if not normalize(claim_key):
        raise ValueError("Claim identity cannot be blank")
    return hashlib.sha256(
        json.dumps([str(customer_id), issue_type, normalize(claim_key)], separators=(",", ":")).encode()
    ).hexdigest()
