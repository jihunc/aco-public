from sqlalchemy import create_engine, text

from aco.core.models import register_models
from aco.features.identity.models import Base
from alembic import context

register_models()

config = context.config

# These exact bootstrap tables are owned by the maintained library/explicit r0004
# migration, not ORM autogeneration. Never propose deleting their queue history.
WORKER_TABLES = {
    "procrastinate_jobs",
    "procrastinate_workers",
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "aco_worker_schema",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    return type_ != "table" or name not in WORKER_TABLES


if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=Base.metadata,
        literal_binds=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    url = config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            # Serialize explicit releases for this database/schema before inspecting
            # Alembic state. Transaction lifetime releases the lock on failure too.
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended("
                    "current_database() || chr(31) || current_schema(), 0))"
                )
            )
            context.configure(connection=connection, target_metadata=Base.metadata, include_name=include_name)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
