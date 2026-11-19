"""All application ORM metadata must match the actual retained migration chain."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from identity_support import actor_client

from aco.features.identity.models import Base


def test_all_application_models_match_migrated_schema() -> None:
    with actor_client("owner") as (_, store, _, _):
        # Maintained Procrastinate DDL has its own schema catalog verification.
        library_tables = {
            "procrastinate_jobs",
            "procrastinate_workers",
            "procrastinate_events",
            "procrastinate_periodic_defers",
            "aco_worker_schema",
        }

        def include_name(name: str | None, type_: str, parents: dict[str, str | None]) -> bool:
            return type_ != "table" or name not in library_tables

        migration = MigrationContext.configure(store.connection(), opts={"include_name": include_name})
        assert compare_metadata(migration, Base.metadata) == []
