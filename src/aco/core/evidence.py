"""Append-only local verification ledger, distinct from application/business data."""

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Fixed, public, committed inputs consumed by offline process tests. Never glob review artifacts.
PUBLIC_VERIFICATION_FIXTURES = ("tests/fixtures/security/public-metadata.json",)


class VerificationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    critic_outcome: Literal["PENDING"] = "PENDING"
    iteration: int = Field(default=1, ge=1)
    defect_refs: list[str] = Field(default_factory=list)
    residual_blockers: list[str] = Field(default_factory=list)
    task: str = Field(pattern=r"^T\d{2}$")
    requirements: list[str] = Field(min_length=1)
    command: str = Field(min_length=1)
    lane: Literal["offline", "live", "deployed"]
    result: Literal["PASS", "FAIL", "SKIPPED", "BLOCKED"]
    environment: str
    commit: str
    usage: dict[str, int]
    cost_quality: Literal["SIMULATED", "PROVIDER_REPORTED", "RATE_ESTIMATE", "UNKNOWN"]
    cost_amount: str | None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    live_authorized: bool = False
    authorization_ref: str | None = None
    provider_operation_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    changed_db_ids: list[str] = Field(default_factory=list)
    ui_path: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    blocker: str | None = None
    recorded_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def truthful_provenance(self) -> "VerificationRow":
        if any(value < 0 for value in self.usage.values()):
            raise ValueError("Usage cannot be negative")
        if self.cost_amount is not None and not Decimal(self.cost_amount).is_finite():
            raise ValueError("Cost must be finite")
        if self.cost_quality == "UNKNOWN" and self.cost_amount is not None:
            raise ValueError("Unknown charge has no amount")
        if self.cost_quality != "UNKNOWN" and self.cost_amount is None:
            raise ValueError("Known cost requires amount")
        if self.lane == "offline" and (
            self.live_authorized or self.provider_operation_id or self.cost_quality != "SIMULATED"
        ):
            raise ValueError("Offline result cannot claim actual provider evidence")
        if self.lane == "live" and self.result in {"PASS", "FAIL"}:
            if not self.live_authorized or not self.authorization_ref or not self.provider_operation_id:
                raise ValueError("Executed live result needs scoped authorization and native operation ID")
            if self.cost_quality == "SIMULATED":
                raise ValueError("Simulated charge is not live evidence")
        return self


def record_verification(row: VerificationRow, *, path: Path = Path(".tooling/verification.sqlite3")) -> None:
    """Persist the observed row plus separated lane status, never promote fake to live."""
    row = VerificationRow.model_validate(row.model_dump())
    payload = row.model_dump(mode="json")
    payload.update(
        {
            "id": str(uuid4()),
            "offline_result": row.result if row.lane == "offline" else None,
            "required_live_result": row.result if row.lane == "live" else "BLOCKED",
            "provider_readiness": "TESTED"
            if row.lane == "live" and row.result == "PASS"
            else "NOT_CONFIGURED",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS verification (payload TEXT NOT NULL)")
        connection.execute("INSERT INTO verification VALUES (?)", (json.dumps(payload),))


class VerificationReview(BaseModel):
    """A later independently attributed outcome; never rewrites executed evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    verification_id: UUID
    critic_outcome: Literal["PASS", "FAIL", "BLOCKED"]
    iteration: int = Field(ge=1)
    defect_refs: list[str]
    residual_blockers: list[str]
    reviewer: str = Field(min_length=1)
    report_ref: str = Field(min_length=1)
    recorded_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


def record_review(review: VerificationReview, *, path: Path) -> None:
    review = VerificationReview.model_validate(review.model_dump())
    payload = review.model_dump(mode="json") | {"id": str(uuid4())}
    with sqlite3.connect(path) as connection:
        existing = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM verification")]
        if not any(row.get("id") == str(review.verification_id) for row in existing):
            raise ValueError("Review must link to an existing execution")
        connection.execute("CREATE TABLE IF NOT EXISTS verification_review (payload TEXT NOT NULL)")
        connection.execute("INSERT INTO verification_review VALUES (?)", (json.dumps(payload),))


def read_verifications(path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(path) as connection:
        rows = [
            json.loads(row[0])
            for row in connection.execute("SELECT payload FROM verification ORDER BY rowid")
        ]
        has_reviews = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='verification_review'"
        ).fetchone()
        reviews = (
            [
                json.loads(row[0])
                for row in connection.execute("SELECT payload FROM verification_review ORDER BY rowid")
            ]
            if has_reviews
            else []
        )
    for row in rows:
        if row.get("schema_version") != 2:
            # Historical rows never recorded these fields. Unknown is not pending,
            # approved, no defects or no blockers; their stored bytes stay unchanged.
            row.update(
                {
                    "critic_outcome": "UNRECORDED",
                    "iteration": None,
                    "defect_refs": None,
                    "residual_blockers": None,
                    "metadata_origin": "LEGACY_UNRECORDED",
                }
            )
        else:
            row["metadata_origin"] = "RECORDED"
        linked = [review for review in reviews if review["verification_id"] == row.get("id")]
        row["reviews"] = linked
        row["latest_critic_outcome"] = linked[-1]["critic_outcome"] if linked else row["critic_outcome"]
    return rows


def verification_source_digest(root: Path) -> str:
    """Bind generated offline results to executable sources, not private environment files."""
    from hashlib import sha256

    digest = sha256()
    # Fixed public build inputs are part of the executable environment. Presence
    # and content both bind the report; private env/config files are never read.
    public_inputs = (
        "README.md",
        ".dockerignore",
        "uv.lock",
        "pyproject.toml",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "tsconfig.json",
        "playwright.config.ts",
        "compose.yaml",
        "compose.product.yaml",
        "web/package.json",
        "web/pnpm-lock.yaml",
        "web/tsconfig.json",
        "web/next.config.ts",
        "web/playwright.config.ts",
        "web/eslint.config.mjs",
        ".github/workflows/ci.yml",
        ".github/workflows/full-regression.yml",
        "infra/api.Dockerfile",
        "infra/web.Dockerfile",
        "infra/testing/Dockerfile",
    )
    for name in (*public_inputs, *PUBLIC_VERIFICATION_FIXTURES):
        path = root / name
        relative = name.encode()
        content = path.read_bytes() if path.is_file() else b""
        digest.update(len(relative).to_bytes(8, "big") + relative)
        digest.update(b"present" if path.is_file() else b"absent")
        digest.update(len(content).to_bytes(8, "big") + content)
    for directory in ("src", "tests", "scripts", "alembic", "web/src", "web/tests"):
        base = root / directory
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".ts", ".tsx", ".mjs", ".json", ".sh", ".sb"} and path.suffix:
                continue
            # Never hash generated test output, local config or credential material.
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            relative = path.relative_to(root).as_posix().encode()
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big") + relative)
            digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def boundary_outputs_match(actual: object, expected: dict[str, object]) -> bool:
    """Exact mandatory outputs, with explicit integer maximums retaining observed counts."""
    if not isinstance(actual, dict) or set(actual) != set(expected):
        return False
    for key, limit in expected.items():
        observed = actual[key]
        if key.endswith("_max") and type(limit) is int:
            if type(observed) is not int or not 0 <= observed <= limit:
                return False
        elif observed != limit or isinstance(limit, bool) and type(observed) is not bool:
            return False
    return True
