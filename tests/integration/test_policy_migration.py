"""Verify policy constraints against Alembic-migrated PostgreSQL, never create_all."""

from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from identity_support import database_url
from integration.test_policy_authority import setup_active
from policy_support import policy_world
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from aco.features.actions.models import ScheduledAction
from aco.features.identity.models import Base
from aco.features.policies.models import CollectionPolicyVersion, PolicyEvent
from alembic import command


def test_policy_migration_matches_current_foreign_key_bindings() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        inspector = inspect(store.connection())
        for table_name in (
            "scheduled_actions",
            "policy_events",
            "policy_activations",
            "dispatch_reads",
            "contact_reservations",
            "dispatch_tickets",
            "policy_occurrences",
            "policy_recipient_controls",
            "collection_holds",
        ):
            actual = {
                (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in inspector.get_foreign_keys(table_name)
            }
            expected = {
                (
                    tuple(e.parent.name for e in fk.elements),
                    fk.referred_table.name,
                    tuple(e.column.name for e in fk.elements),
                )
                for fk in Base.metadata.tables[table_name].foreign_key_constraints
            }
            assert expected <= actual, (table_name, expected - actual)
            assert {c["name"] for c in inspector.get_columns(table_name)} == set(
                Base.metadata.tables[table_name].columns.keys()
            )


@pytest.mark.parametrize("binding", ["event", "version", "kind", "tenant"])
def test_policy_action_rejects_mismatched_stored_provenance(binding: str) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        action = store.scalar(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        assert action
        event = store.scalar(
            select(PolicyEvent).where(
                PolicyEvent.tenant_id == ctx.tenant_id, PolicyEvent.id != action.policy_event_id
            )
        )
        version = store.scalar(
            select(CollectionPolicyVersion).where(
                CollectionPolicyVersion.tenant_id == ctx.tenant_id,
                CollectionPolicyVersion.id != action.policy_version_id,
            )
        )
        assert event and version
        changes = {
            "event": ("policy_event_id", event.id, "fk_action_activation_event"),
            "version": ("policy_version_id", version.id, "fk_action_activation_version"),
            "kind": ("kind", "accounting_recheck", "ck_action_trigger"),
            "tenant": ("tenant_id", uuid4(), "foreign key"),
        }
        column, value, error = changes[binding]
        columns = list(ScheduledAction.__table__.columns.keys())
        values = [
            ":new_id" if c == "id" else ":new_key" if c == "operation_key" else ":value" if c == column else c
            for c in columns
        ]
        # INSERT reaches the FK/check independently of the existing immutable UPDATE trigger.
        statement = (
            f"INSERT INTO scheduled_actions ({','.join(columns)}) "
            f"SELECT {','.join(values)} FROM scheduled_actions WHERE id=:id"
        )
        with pytest.raises(IntegrityError, match=error):
            store.execute(
                text(statement), {"value": value, "id": action.id, "new_id": uuid4(), "new_key": uuid4().hex}
            )
        store.rollback()


def test_policy_migration_empty_round_trip_and_history_downgrade_refusal() -> None:
    engine = create_engine(database_url())
    schema = "t08_migration_" + uuid4().hex
    with engine.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    scoped = create_engine(url)
    try:
        command.upgrade(config, "r0005_policies")
        command.downgrade(config, "r0004_actions")
        with scoped.connect() as db:
            assert "policy_activations" not in inspect(db).get_table_names()
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "r0004_actions"
        command.upgrade(config, "r0005_policies")
    finally:
        scoped.dispose()
        with engine.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()
    with policy_world() as (client, store, ctx, invoices, saved):
        bind = store.get_bind()
        config.set_main_option(
            "sqlalchemy.url", bind.engine.url.render_as_string(hide_password=False).replace("%", "%%")
        )
        store.rollback()
        before = list(store.execute(text("SELECT to_jsonb(t) FROM policy_events t ORDER BY id")).scalars())
        assert before
        revision = ScriptDirectory.from_config(config).get_revision("r0005_policies")
        assert revision is not None
        # Exercise this migration's own pre-DDL guard, not a later restore refusal.
        with Operations.context(MigrationContext.configure(store.connection())):
            with pytest.raises(RuntimeError, match="policy/dispatch evidence would be lost"):
                revision.module.downgrade()
        assert (
            list(store.execute(text("SELECT to_jsonb(t) FROM policy_events t ORDER BY id")).scalars())
            == before
        )
        assert (
            store.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
