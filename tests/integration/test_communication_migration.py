"""Tenant constraints, lossless history guard and real additive schema evidence."""

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from identity_support import actor_client
from sqlalchemy import inspect, text

from aco.features.communications import models
from aco.features.identity.models import Base
from aco.integrations.demo_email import models as native
from alembic import command


def test_communication_migration_declared_foreign_keys_and_columns() -> None:
    with actor_client("owner") as (_, store, _, tenant):
        inspector = inspect(store.connection())
        names = {
            value.__tablename__
            for module in (models, native)
            for value in vars(module).values()
            if isinstance(value, type) and hasattr(value, "__tablename__")
        }
        for name in names:
            actual = {
                (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in inspector.get_foreign_keys(name)
            }
            expected = {
                (
                    tuple(e.parent.name for e in fk.elements),
                    fk.referred_table.name,
                    tuple(e.column.name for e in fk.elements),
                )
                for fk in Base.metadata.tables[name].foreign_key_constraints
            }
            assert actual == expected
            assert {column["name"] for column in inspector.get_columns(name)} == set(
                Base.metadata.tables[name].columns.keys()
            )


def test_communication_migration_preserves_existing_and_refuses_native_history() -> None:
    from uuid import uuid4

    from aco.features.identity.models import ProviderConnection

    with actor_client("owner") as (_, store, _, tenant):
        connection = ProviderConnection(
            tenant_id=tenant,
            provider_type="email",
            provider="demo_email",
            status="READY",
            external_account_id="synthetic-migration",
        )
        store.add(connection)
        store.flush()
        row = native.NativeEmailInvocation(
            tenant_id=tenant,
            provider_connection_id=connection.id,
            operation_id=uuid4(),
            method="validate_connection",
            payload_hash="0" * 64,
            native_request_id="synthetic-migration-observation",
            result="NO_OUTREACH",
        )
        store.add(row)
        store.commit()
        before = store.scalar(text("SELECT count(*) FROM tenants"))
        store.rollback()
        config = Config("alembic.ini")
        config.set_main_option(
            "sqlalchemy.url",
            store.get_bind().engine.url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        with pytest.raises(
            RuntimeError,
            match="^Retained Voice grant, request and join authority requires non-destructive restore$",
        ):
            command.downgrade(config, "r0006_artifacts")
        # Later protected migrations stop the whole downgrade first. Exercise
        # T10's own pre-DDL guard against the real retained ledger as well.
        with Operations.context(MigrationContext.configure(store.connection())):
            with pytest.raises(RuntimeError, match="T10 history"):
                ScriptDirectory.from_config(config).get_revision("r0007_communications").module.downgrade()
        assert (
            store.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        assert store.scalar(text("SELECT count(*) FROM tenants")) == before
        retained = store.get(native.NativeEmailInvocation, row.id, populate_existing=True)
        assert retained is not None and retained.result == "NO_OUTREACH"
