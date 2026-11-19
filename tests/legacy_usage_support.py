"""Copy synthetic historical source bytes to an isolated pre-usage schema for old migration tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import Engine, MetaData, create_engine, text
from sqlalchemy.orm import Session

from alembic import command


@contextmanager
def legacy_snapshot(
    store: Session,
    *,
    revision: Literal["r0002b_accounting_observations", "r0003a_usage_invariants"] = (
        "r0002b_accounting_observations"
    ),
) -> Iterator[tuple[Session, Config]]:
    if revision not in {"r0002b_accounting_observations", "r0003a_usage_invariants"}:
        raise ValueError("Only exact pre-action historical snapshot revisions are supported")
    source = store.get_bind()
    assert isinstance(source, Engine)
    store.rollback()
    with source.connect() as connection:
        source_schema = connection.scalar(text("SELECT current_schema()"))
    schema = "legacy_" + uuid4().hex
    with source.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = source.url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    try:
        command.upgrade(config, revision)
        metadata = MetaData()
        metadata.reflect(engine)
        with engine.begin() as connection:
            # At r3a these append-only source bridges are active. Copy their
            # retained canonical usage first, then source producers, so normal
            # bridge idempotency preserves the exact existing rows. No trigger
            # or current-schema guard is disabled to build the rehearsal.
            producers = {"identity_provider_events", "accounting_events", "accounting_observations"}
            tables = sorted(metadata.sorted_tables, key=lambda table: table.name in producers)
            for table in tables:
                if table.name == "alembic_version":
                    continue
                # Historical schema is authoritative; no usage tables/trigger are
                # dropped or disabled in the current production schema.
                columns = ",".join(f'"{c.name}"' for c in table.columns)
                connection.execute(
                    text(
                        f'INSERT INTO "{schema}"."{table.name}" ({columns}) '
                        f'OVERRIDING SYSTEM VALUE SELECT {columns} FROM "{source_schema}"."{table.name}"'
                    )
                )
            for table in tables:
                if table.name == "alembic_version":
                    continue
                columns = ",".join(f'"{c.name}"' for c in table.columns)
                # JSONB equality stays in PG (no float/decimal coercion) and
                # includes duplicate multiplicity and every historical column.
                historical_rows = f'SELECT to_jsonb(ROW({columns})) FROM "{schema}"."{table.name}"'
                source_rows = f'SELECT to_jsonb(ROW({columns})) FROM "{source_schema}"."{table.name}"'
                assert not connection.scalar(
                    text(
                        f"SELECT EXISTS(({historical_rows} EXCEPT ALL {source_rows}) "
                        f"UNION ALL ({source_rows} EXCEPT ALL {historical_rows}))"
                    )
                ), table.name
        with Session(engine) as historical:
            yield historical, config
    finally:
        engine.dispose()
        with source.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
