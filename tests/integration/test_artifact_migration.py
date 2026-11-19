"""Artifact DDL identity and lossless rollback against isolated migrated PostgreSQL."""

from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from identity_support import actor_client, database_url
from sqlalchemy import create_engine, inspect, text

from aco.features.artifacts.models import Artifact, ArtifactPurge, RetentionPolicy
from aco.features.identity.models import Base
from alembic import command


def test_artifact_migration_matches_declared_columns_and_tenant_bindings() -> None:
    with actor_client("owner") as (_, store, _, _):
        inspector = inspect(store.connection())
        for model in (Artifact, ArtifactPurge, RetentionPolicy):
            table = model.__tablename__
            actual = {
                (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in inspector.get_foreign_keys(table)
            }
            expected = {
                (
                    tuple(e.parent.name for e in fk.elements),
                    fk.referred_table.name,
                    tuple(e.column.name for e in fk.elements),
                )
                for fk in Base.metadata.tables[table].foreign_key_constraints
            }
            assert expected == actual
            assert {c["name"] for c in inspector.get_columns(table)} == set(
                Base.metadata.tables[table].columns.keys()
            )


def test_artifact_migration_empty_roundtrip_and_policy_history_guard() -> None:
    engine = create_engine(database_url())
    schema = "t09_migration_" + uuid4().hex
    with engine.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    scoped = create_engine(url)
    try:
        command.upgrade(config, "r0006_artifacts")
        command.downgrade(config, "r0005_policies")
        with scoped.connect() as db:
            assert "artifacts" not in inspect(db).get_table_names()
        command.upgrade(config, "r0006_artifacts")
    finally:
        scoped.dispose()
        with engine.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()
    with actor_client("owner") as (_, store, _, tenant):
        store.add(
            RetentionPolicy(
                tenant_id=tenant,
                policy_key="synthetic_30_days",
                synthetic=True,
                retention_days=30,
                authority="synthetic_only",
            )
        )
        store.commit()
        config.set_main_option(
            "sqlalchemy.url",
            store.get_bind().engine.url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        before = list(
            store.execute(text("SELECT to_jsonb(t) FROM artifact_retention_policies t ORDER BY id")).scalars()
        )
        assert before
        revision = ScriptDirectory.from_config(config).get_revision("r0006_artifacts")
        assert revision is not None
        # Exercise this migration's own pre-DDL guard, not a later restore refusal.
        with Operations.context(MigrationContext.configure(store.connection())):
            with pytest.raises(RuntimeError, match="artifact evidence would be lost"):
                revision.module.downgrade()
        assert (
            list(
                store.execute(
                    text("SELECT to_jsonb(t) FROM artifact_retention_policies t ORDER BY id")
                ).scalars()
            )
            == before
        )
        assert (
            store.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
