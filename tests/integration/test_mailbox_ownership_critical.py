"""Independent current-owner mailbox refusal controls; isolated real PostgreSQL only."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier
from typing import cast
from uuid import UUID

import pytest
from accounting_support import context
from fastapi import FastAPI, HTTPException
from identity_support import actor_client, add_actor
from integration.test_shared_email_routing import material
from sqlalchemy import Engine, delete, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.communications.ingress import EmailIngressService, current_activation
from aco.features.communications.ingress_dto import ActivateIngress
from aco.features.communications.models import EmailIngressActivation, EmailMailboxClaim, EmailMailboxConflict
from aco.features.communications.provider_ingress import ProviderIngressCapability, ProviderIngressRuntime
from aco.features.identity.models import Membership, Tenant, now
from aco.integrations.ports.email import EmailPort
from alembic import command


@dataclass(frozen=True)
class Owner:
    ctx: TenantContext
    cap: ProviderIngressCapability
    storage_id: UUID

    def command(self) -> ActivateIngress:
        return ActivateIngress(
            email_connection_id=self.cap.connection_id,
            storage_connection_id=self.storage_id,
            evidence="Independent explicit synthetic mailbox ownership review.",
        )


@dataclass
class RegistryWorld:
    db: Session
    runtime: ProviderIngressRuntime
    ingress: EmailIngressService
    owners: tuple[Owner, Owner]


@contextmanager
def registry_world(
    *, addresses: tuple[str, str] = ("shared@synthetic.invalid",) * 2
) -> Iterator[RegistryWorld]:
    with actor_client("owner") as (client, db, user, tenant):
        app = cast(FastAPI, client.app)
        other_user, _ = add_actor(db, app, "independent-mailbox-reviewer@example.test")
        other = Tenant(name="Independent synthetic mailbox owner", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=other_user.id, role="owner"))
        db.commit()
        first, storage_a = material(db, tenant, addresses[0], "offline-mailbox-review-key")
        second, storage_b = material(db, other.id, addresses[1], "offline-mailbox-review-key")
        caps = {first.connection_id: first, second.connection_id: second}

        def resolve(tenant_id: UUID, connection_id: UUID) -> ProviderIngressCapability:
            cap = caps[connection_id]
            assert isinstance(cap, ProviderIngressCapability)
            assert cap.tenant_id == tenant_id
            return cap

        def forbidden(*args: object) -> EmailPort:
            raise AssertionError("Mailbox registry controls cannot invoke a provider")

        yield RegistryWorld(
            db,
            ProviderIngressRuntime(app.state.business_sessions, resolve, forbidden, enabled=True),
            EmailIngressService(app.state.business_sessions, enabled=True, secrets={}),
            (
                Owner(context(tenant, user.id), first, storage_a),
                Owner(context(other.id, other_user.id), second, storage_b),
            ),
        )


def test_conflicting_current_owners_activate_concurrently_with_only_one_commit() -> None:
    with registry_world() as lane:
        engine = lane.db.get_bind()
        assert isinstance(engine, Engine)
        lane.db.rollback()
        ready = Barrier(2)

        def activate(owner: Owner) -> tuple[int, str]:
            with Session(engine) as db:
                db.execute(text("SET LOCAL lock_timeout='5s'"))
                ready.wait(timeout=5)
                try:
                    result = lane.runtime.activate(db, owner.ctx, owner.command())
                    db.commit()
                    return 200, str(result.id)
                except HTTPException as error:
                    db.rollback()
                    return error.status_code, str(error.detail)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(activate, owner) for owner in lane.owners]
            outcomes = [future.result(timeout=15) for future in futures]
        assert sorted(status for status, _ in outcomes) == [200, 409]
        assert next(detail for status, detail in outcomes if status == 409) == "INBOUND_MAILBOX_ALREADY_OWNED"
        claims = list(lane.db.scalars(select(EmailMailboxClaim)))
        assert len(claims) == 1 and claims[0].released_at is None
        assert str(claims[0].activation_id) == next(detail for status, detail in outcomes if status == 200)
        assert lane.db.scalar(select(func.count()).select_from(EmailIngressActivation)) == 1


@pytest.mark.parametrize("release", ["revoke", "expire"])
def test_reassignment_rejects_old_activation_and_retains_released_owner(
    release: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with registry_world() as lane:
        first, second = lane.owners
        original = lane.runtime.activate(lane.db, first.ctx, first.command())
        lane.db.commit()
        if release == "revoke":
            lane.ingress.revoke(lane.db, first.ctx, original.id, original.version)
            lane.db.commit()
        else:
            # Advance the activation clock without mutating its immutable expires_at.
            future = now() + timedelta(hours=2)
            for module in ("mailboxes", "ingress", "provider_ingress"):
                monkeypatch.setattr(f"aco.features.communications.{module}.now", lambda: future)
        replacement = lane.runtime.activate(lane.db, second.ctx, second.command())
        lane.db.commit()
        with pytest.raises(HTTPException) as refused:
            current_activation(lane.db, original.id, first.ctx.tenant_id)
        assert refused.value.status_code == 403
        lane.db.rollback()
        current = current_activation(lane.db, replacement.id, second.ctx.tenant_id)
        assert lane.runtime.validate(lane.db, current) == second.cap
        rows = list(lane.db.scalars(select(EmailMailboxClaim)))
        assert len(rows) == 2
        assert next(row for row in rows if row.activation_id == original.id).released_at is not None
        assert next(row for row in rows if row.activation_id == replacement.id).released_at is None


def test_same_owner_renewal_and_database_history_are_irreversible() -> None:
    with registry_world() as lane:
        owner = lane.owners[0]
        original = lane.runtime.activate(lane.db, owner.ctx, owner.command())
        lane.db.commit()
        renewed = lane.runtime.activate(lane.db, owner.ctx, owner.command())
        lane.db.commit()
        old = lane.db.scalar(select(EmailMailboxClaim).where(EmailMailboxClaim.activation_id == original.id))
        assert old is not None and old.released_at is not None
        prior = (old.id, old.address, old.activation_id, old.tenant_id, old.released_at)
        # Even a direct duplicate insert cannot bypass the active partial index.
        with pytest.raises(DBAPIError, match="email_mailbox_one_active_owner"):
            with lane.db.begin_nested():
                lane.db.add(
                    EmailMailboxClaim(
                        tenant_id=owner.ctx.tenant_id,
                        activation_id=renewed.id,
                        email_connection_id=owner.cap.connection_id,
                        address=old.address,
                    )
                )
                lane.db.flush()
        attacks = [
            update(EmailMailboxClaim)
            .where(EmailMailboxClaim.id == old.id)
            .values(address="hijacked@synthetic.invalid"),
            update(EmailMailboxClaim).where(EmailMailboxClaim.id == old.id).values(released_at=None),
            delete(EmailMailboxClaim).where(EmailMailboxClaim.id == old.id),
        ]
        for attack in attacks:
            with pytest.raises(DBAPIError, match="mailbox"):
                with lane.db.begin_nested():
                    lane.db.execute(attack)
        lane.db.expire_all()
        retained = lane.db.get(EmailMailboxClaim, old.id)
        assert retained is not None
        assert (
            retained.id,
            retained.address,
            retained.activation_id,
            retained.tenant_id,
            retained.released_at,
        ) == prior
        with pytest.raises(HTTPException, match="INBOUND_MAILBOX_OWNERSHIP_REVOKED"):
            current_activation(lane.db, original.id, owner.ctx.tenant_id)
        lane.db.rollback()
        assert current_activation(lane.db, renewed.id, owner.ctx.tenant_id).id == renewed.id


def test_legacy_duplicate_mailbox_migration_quarantines_without_changing_activations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with registry_world() as lane:
        # Exact pre-r22 behavior had no registry claim. Only this additive call is
        # suppressed while retaining actual owner, material, actor and activation creation.
        with monkeypatch.context() as legacy:
            legacy.setattr("aco.features.communications.mailboxes.claim_mailboxes", lambda *_args: None)
            keys = [lane.runtime.activate(lane.db, owner.ctx, owner.command()).id for owner in lane.owners]
            lane.db.commit()
        before = lane.db.execute(
            text("SELECT id::text,to_jsonb(a)::text FROM email_ingress_activations a ORDER BY id")
        ).all()
        from legacy_usage_support import legacy_snapshot

        source_schema = lane.db.scalar(text("SELECT current_schema()"))
        assert isinstance(source_schema, str)
        lane.db.rollback()
        # Copy real pre-registry activation bytes into an actual r21 schema.
        # No protected current-head downgrade or trigger disable is involved.
        with legacy_snapshot(lane.db) as (historical, config):
            historical.rollback()
            command.upgrade(config, "r0021_voice")
            for table in ("identity_actors", "artifact_retention_policies", "email_ingress_activations"):
                columns = ",".join(
                    f'"{c["name"]}"' for c in inspect(historical.connection()).get_columns(table)
                )
                predicate = " WHERE kind='service'" if table == "identity_actors" else ""
                historical.execute(
                    text(
                        f'INSERT INTO "{table}" ({columns}) SELECT {columns} '
                        f'FROM "{source_schema}"."{table}"{predicate}'
                    )
                )
            assert (
                historical.execute(
                    text("SELECT id::text,to_jsonb(a)::text FROM email_ingress_activations a ORDER BY id")
                ).all()
                == before
            )
            historical.commit()
            command.upgrade(config, "r0022_email_mailboxes")
            assert (
                historical.scalar(text("SELECT version_num FROM alembic_version")) == "r0022_email_mailboxes"
            )
            historical.rollback()
            # Current application consumers run only after ordinary full upgrade.
            command.upgrade(config, "head")
            lane.db = historical
            lane.db.expire_all()
            after = lane.db.execute(
                text("SELECT id::text,to_jsonb(a)::text FROM email_ingress_activations a ORDER BY id")
            ).all()
            assert after == before
            conflict = lane.db.scalar(select(EmailMailboxConflict))
            assert conflict is not None and set(conflict.activation_ids) == {str(key) for key in keys}
            assert conflict.resolved_at is None
            for attack in (
                update(EmailMailboxConflict)
                .where(EmailMailboxConflict.id == conflict.id)
                .values(activation_ids=[]),
                delete(EmailMailboxConflict).where(EmailMailboxConflict.id == conflict.id),
            ):
                with pytest.raises(DBAPIError, match="mailbox"):
                    with lane.db.begin_nested():
                        lane.db.execute(attack)
            assert lane.db.scalar(select(func.count()).select_from(EmailMailboxClaim)) == 0
            for owner, key in zip(lane.owners, keys, strict=True):
                with pytest.raises(HTTPException, match="INBOUND_MAILBOX_OWNERSHIP_REVOKED"):
                    current_activation(lane.db, key, owner.ctx.tenant_id)
                lane.db.rollback()
                with pytest.raises(HTTPException, match="INBOUND_MAILBOX_REVIEW_REQUIRED"):
                    lane.runtime.activate(lane.db, owner.ctx, owner.command())
                lane.db.rollback()
            for owner, key in zip(lane.owners, keys, strict=True):
                old = lane.db.get(EmailIngressActivation, key)
                assert old is not None
                lane.ingress.revoke(lane.db, owner.ctx, key, old.version)
                lane.db.commit()
            restored = lane.runtime.activate(lane.db, lane.owners[0].ctx, lane.owners[0].command())
            lane.db.commit()
            assert current_activation(lane.db, restored.id, lane.owners[0].ctx.tenant_id).id == restored.id
            conflict = lane.db.scalar(select(EmailMailboxConflict))
            assert conflict is not None and conflict.resolved_at is not None
