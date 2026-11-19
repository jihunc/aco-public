"""Trusted-operator allocations for the user's cumulative live-test ceilings.

An allocation reserves the ENTIRE persisted grant ceiling before activation. It is
never refunded, including after failure/crash/unknown billing. Application grants
still independently enforce per-wire budgets; this ledger grants no API authority.
"""

import json
import os
import secrets
import sqlite3
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.integrations.provider_smoke import source

AUTHORIZATION_REF = DELIVERY_LIVE_AUTHORIZATION
LOCK_TIMEOUT = 5.0
SCALE = Decimal("1000000000000")
POLICY = {
    "authorization_ref": AUTHORIZATION_REF,
    "max_usd": "20",
    "openai_requests": 50,
    "resend_messages": 20,
    "vapi_sessions": 3,
    "vapi_seconds": 900,
    "pstn": False,
}


def canonical_campaign_path() -> Path:
    root = source.source_root()
    if not (root / ".git").exists():
        # No arbitrary runtime path or replacement budget authority. Deployment
        # must mount the SAME existing operator ledger and SQLite journal directory.
        source.image_source_commit(root)
        return Path("/var/lib/aco/live/live-delivery-2026-11-19.sqlite3")
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    return Path(common).parent / ".tooling" / "live-delivery-2026-11-19.sqlite3"


class CampaignBlocked(ValueError):
    pass


class CampaignAllocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    id: UUID
    grant_id: UUID
    provider: Literal["openai", "resend", "vapi"]
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    max_usd: Decimal = Field(gt=0, le=20, max_digits=14, decimal_places=12)
    max_requests: int = Field(ge=1, le=100)
    max_messages: int = Field(default=0, ge=0, le=20)
    max_sessions: int = Field(default=0, ge=0, le=3)
    max_seconds: int = Field(default=0, ge=0, le=900)
    purpose: Literal["APPLICATION", "HISTORICAL_EXPOSURE"] = "APPLICATION"

    @model_validator(mode="after")
    def relevant_caps(self) -> "CampaignAllocation":
        if self.provider != "resend" and self.max_messages:
            raise ValueError("Message capacity is Resend-only")
        if self.provider != "vapi" and (self.max_sessions or self.max_seconds):
            raise ValueError("Voice capacity is Vapi-only")
        if self.max_seconds > self.max_sessions * 300:
            raise ValueError("Maximum five minutes per authorized browser session")
        return self


class LiveCampaign:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(cls, path: Path, *, authorization_ref: str) -> "LiveCampaign":
        if not (source.source_root() / ".git").exists():
            raise CampaignBlocked("CAMPAIGN_CREATION_REQUIRES_OPERATOR_CHECKOUT")
        if authorization_ref != AUTHORIZATION_REF or path != canonical_campaign_path():
            raise CampaignBlocked("CAMPAIGN_AUTHORIZATION_REQUIRED")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        except OSError:
            raise CampaignBlocked("PRIVATE_CAMPAIGN_REQUIRED") from None
        result = cls(path)
        with result._db(initializing=True) as db:
            db.executescript("""
              CREATE TABLE policy(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
              CREATE TABLE allocations(
                id TEXT PRIMARY KEY,grant_id TEXT NOT NULL UNIQUE,provider TEXT NOT NULL,
                microusd INTEGER NOT NULL CHECK(microusd>0),requests INTEGER NOT NULL,
                messages INTEGER NOT NULL,sessions INTEGER NOT NULL,seconds INTEGER NOT NULL,
                payload TEXT NOT NULL);
              CREATE TRIGGER policy_no_update BEFORE UPDATE ON policy
                BEGIN SELECT RAISE(ABORT,'immutable'); END;
              CREATE TRIGGER policy_no_delete BEFORE DELETE ON policy
                BEGIN SELECT RAISE(ABORT,'immutable'); END;
              CREATE TRIGGER allocation_no_update BEFORE UPDATE ON allocations
                BEGIN SELECT RAISE(ABORT,'immutable'); END;
              CREATE TRIGGER allocation_no_delete BEFORE DELETE ON allocations
                BEGIN SELECT RAISE(ABORT,'immutable'); END;
            """)
            db.execute("INSERT INTO policy VALUES(1,?)", (json.dumps(POLICY, sort_keys=True),))
        return result

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        # SQLite byte-range locks alone are not coherent across every host/VM mount.
        # Atomic directory creation serializes ALL current clients of the SAME ledger.
        # A crash leaves the lock blocked; no PID/age-based takeover can erase exposure.
        if self.path != canonical_campaign_path():
            raise CampaignBlocked("PRIVATE_CAMPAIGN_REQUIRED")
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                lock.mkdir(mode=0o700)
                token = secrets.token_hex(32)
                owner = lock / "owner"
                descriptor = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(token)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise CampaignBlocked("CAMPAIGN_LOCKED") from None
                time.sleep(0.01)
            except OSError:
                raise CampaignBlocked("PRIVATE_CAMPAIGN_REQUIRED") from None
        try:
            yield
        finally:
            # Never remove somebody else's replacement or recover an abandoned lock.
            try:
                if lock.is_symlink() or not lock.is_dir():
                    raise CampaignBlocked("CAMPAIGN_LOCK_CHANGED")
                descriptor = os.open(owner, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor) as stream:
                    if stream.read(65) != token:
                        raise CampaignBlocked("CAMPAIGN_LOCK_CHANGED")
                owner.unlink()
                lock.rmdir()
            except OSError:
                raise CampaignBlocked("CAMPAIGN_LOCK_CHANGED") from None

    @contextmanager
    def _db(self, *, initializing: bool = False) -> Iterator[sqlite3.Connection]:
        # Preserved older operator clients use SQLite alone. Actual Docker Desktop
        # mixed-version probing still permits overlapping writers despite this lock.
        # No config flag/stamp/token can waive canonical aggregate exclusion.
        if not (source.source_root() / ".git").exists():
            raise CampaignBlocked("CONTAINER_CAMPAIGN_LOCKING_UNVERIFIED")
        with self._exclusive():
            with self._locked_db(initializing=initializing) as connection:
                yield connection

    @contextmanager
    def _locked_db(self, *, initializing: bool = False) -> Iterator[sqlite3.Connection]:
        descriptor = -1
        connection = None
        try:
            if self.path != canonical_campaign_path():
                raise CampaignBlocked("PRIVATE_CAMPAIGN_REQUIRED")
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise CampaignBlocked("PRIVATE_CAMPAIGN_REQUIRED")
            current = os.stat(self.path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise CampaignBlocked("PRIVATE_CAMPAIGN_REQUIRED")
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA trusted_schema=OFF")
            if not initializing:
                row = connection.execute("SELECT payload FROM policy WHERE id=1").fetchone()
                if row is None or json.loads(row[0]) != POLICY:
                    raise CampaignBlocked("CAMPAIGN_POLICY_CHANGED")
            yield connection
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            raise CampaignBlocked("PRIVATE_CAMPAIGN_REQUIRED") from None
        finally:
            if connection is not None:
                connection.close()
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _totals(db: sqlite3.Connection) -> dict[str, int]:
        amounts = db.execute("""SELECT COALESCE(SUM(microusd),0),
          COALESCE(SUM(CASE WHEN provider='openai' THEN requests ELSE 0 END),0),
          COALESCE(SUM(messages),0),COALESCE(SUM(sessions),0),COALESCE(SUM(seconds),0)
          FROM allocations""").fetchone()
        assert amounts is not None
        return dict(
            zip(
                ("microusd", "openai_requests", "resend_messages", "vapi_sessions", "vapi_seconds"),
                amounts,
                strict=True,
            )
        )

    def allocate(self, allocation: CampaignAllocation) -> None:
        # Validate model_copy/caller-created values again at this trusted boundary.
        allocation = CampaignAllocation.model_validate_json(allocation.model_dump_json())
        encoded = allocation.model_dump_json()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT payload FROM allocations WHERE id=? OR grant_id=?",
                (str(allocation.id), str(allocation.grant_id)),
            ).fetchall()
            if prior:
                if len(prior) != 1 or CampaignAllocation.model_validate_json(prior[0][0]) != allocation:
                    raise CampaignBlocked("ALLOCATION_CONFLICT")
                db.commit()
                return
            cost = int(allocation.max_usd * SCALE)
            totals = self._totals(db)
            if (
                totals["microusd"] + cost > int(Decimal("20") * SCALE)
                or totals["openai_requests"]
                + (allocation.max_requests if allocation.provider == "openai" else 0)
                > 50
                or totals["resend_messages"] + allocation.max_messages > 20
                or totals["vapi_sessions"] + allocation.max_sessions > 3
                or totals["vapi_seconds"] + allocation.max_seconds > 900
            ):
                raise CampaignBlocked("CAMPAIGN_LIMIT")
            db.execute(
                "INSERT INTO allocations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(allocation.id),
                    str(allocation.grant_id),
                    allocation.provider,
                    cost,
                    allocation.max_requests,
                    allocation.max_messages,
                    allocation.max_sessions,
                    allocation.max_seconds,
                    encoded,
                ),
            )
            db.commit()

    def validate(self, allocation: CampaignAllocation) -> None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM allocations WHERE id=?", (str(allocation.id),)).fetchone()
            if row is None or CampaignAllocation.model_validate_json(row[0]) != allocation:
                raise CampaignBlocked("ALLOCATION_REQUIRED")

    def totals(self) -> dict[str, str | int]:
        with self._db() as db:
            totals = self._totals(db)
        return {"reserved_usd": format(Decimal(totals.pop("microusd")) / SCALE, "f"), **totals}


def verify_grant_allocation(
    path: Path,
    allocation_id: UUID,
    *,
    grant_id: UUID,
    provider: Literal["openai", "resend", "vapi"],
    source_commit: str,
    max_requests: int,
    max_usd: Decimal,
    max_messages: int = 0,
    max_sessions: int = 0,
    max_seconds: int = 0,
) -> None:
    campaign = LiveCampaign(path)
    with campaign._db() as db:
        row = db.execute("SELECT payload FROM allocations WHERE id=?", (str(allocation_id),)).fetchone()
        if row is None:
            raise CampaignBlocked("ALLOCATION_REQUIRED")
        allocation = CampaignAllocation.model_validate_json(row[0])
    if (
        allocation.purpose != "APPLICATION"
        or allocation.grant_id != grant_id
        or allocation.provider != provider
        or allocation.source_commit != source_commit
        or not max_usd.is_finite()
        or max_usd <= 0
        or max_usd > allocation.max_usd
        or not 0 < max_requests <= allocation.max_requests
        or not 0 <= max_messages <= allocation.max_messages
        or not 0 <= max_sessions <= allocation.max_sessions
        or not 0 <= max_seconds <= allocation.max_seconds
    ):
        raise CampaignBlocked("GRANT_EXCEEDS_ALLOCATION")
