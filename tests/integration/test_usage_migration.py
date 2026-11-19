"""Additive migration reconstructs known operations without mutating source rows."""

from uuid import uuid4

import pytest
from alembic.config import Config
from identity_support import database_url
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from alembic import command


def test_usage_migration_preserves_identity_and_refuses_lossy_rollback() -> None:
    engine = create_engine(database_url())
    schema = "t06_migration_" + uuid4().hex
    with engine.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    scoped = create_engine(url)
    try:
        command.upgrade(config, "r0002b_accounting_observations")
        key = uuid4()
        with scoped.begin() as db:
            db.execute(
                text("""INSERT INTO identity_provider_events
                (id,connection_id,correlation_id,operation,provider,native_id,outcome,attempt,
                 cost_quality,cost_amount,created_at,updated_at,version)
                VALUES (:id,:connection,:correlation,'rotate','synthetic','same','PENDING',1,
                 'UNKNOWN',NULL,now(),now(),1)"""),
                {"id": key, "connection": uuid4(), "correlation": uuid4()},
            )
            before = db.execute(text("SELECT row_to_json(e) FROM identity_provider_events e")).scalar_one()
        command.upgrade(config, "r0003a_usage_invariants")
        with scoped.begin() as db:
            after = db.execute(text("SELECT row_to_json(e) FROM identity_provider_events e")).scalar_one()
            assert after.pop("provider_attempted") is None
            assert before == after
            assert db.execute(text("SELECT COUNT(*) FROM provider_usage_events")).scalar_one() == 1
            assert db.execute(text("SELECT cost_amount FROM usage_revisions")).scalar_one() is None
            db.execute(
                text("UPDATE identity_provider_events SET outcome='VERIFIED' WHERE id=:id"), {"id": key}
            )
        # Original PENDING history now exists only in canonical revisions; refuse erasure.
        with scoped.connect() as db:
            retained = {
                table: list(db.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")).scalars())
                for table in ("identity_provider_events", "provider_usage_events", "usage_revisions")
            }
        with pytest.raises(DBAPIError, match="unreconstructible usage/budget history"):
            command.downgrade(config, "r0002b_accounting_observations")
        with scoped.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "r0003a_usage_invariants"
            assert {
                table: list(db.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")).scalars())
                for table in retained
            } == retained
    finally:
        scoped.dispose()
        with engine.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()


def test_accounting_backfill_deduplicates_event_and_observation_without_rewriting() -> None:
    from accounting_support import seed
    from identity_support import actor_client
    from legacy_usage_support import legacy_snapshot

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        with legacy_snapshot(store) as (legacy, config):
            before = {
                table: list(
                    legacy.execute(text(f"SELECT row_to_json(t) FROM {table} t ORDER BY id")).scalars()
                )
                for table in ("accounting_events", "accounting_observations", "identity_provider_events")
            }
            expected = {
                (row["tenant_id"], row["provider_connection_id"], source["native_operation_id"])
                for table in ("accounting_events", "accounting_observations")
                for row in before[table]
                for source in row["provenance"].values()
            }
            links = 2 * (len(before["accounting_events"]) + len(before["accounting_observations"]))
            legacy.rollback()
            command.upgrade(config, "r0003a_usage_invariants")
            assert legacy.scalar(
                text("SELECT COUNT(*) FROM provider_usage_events WHERE scope_kind='TENANT'")
            ) == len(expected)
            assert (
                legacy.scalar(
                    text("SELECT COUNT(*) FROM usage_sources WHERE source_kind LIKE 'accounting_%'")
                )
                == links
            )
            for table, original in before.items():
                migrated = list(
                    legacy.execute(text(f"SELECT row_to_json(t) FROM {table} t ORDER BY id")).scalars()
                )
                if table == "identity_provider_events":
                    for row in migrated:
                        assert row.pop("provider_attempted") is None
                assert migrated == original
            assert (
                legacy.scalar(
                    text(
                        "SELECT COUNT(*) FROM usage_revisions WHERE cost_quality='UNKNOWN' AND "
                        "cost_amount IS NOT NULL"
                    )
                )
                == 0
            )
