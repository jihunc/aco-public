"""Canonical typed actors and durable actions, retaining every human history value."""

import hashlib
import json
from importlib.metadata import version

from procrastinate.schema import SchemaManager
from sqlalchemy import Connection, inspect, text

from alembic import op

revision = "r0004_actions"
down_revision = "r0003a_usage_invariants"
branch_labels = None
depends_on = None


def worker_catalog(connection: Connection) -> str:
    """Fingerprint bootstrap definitions, excluding mutable jobs/counters/statistics."""
    queries = (
        "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname LIKE 'procrastinate_%' ORDER BY c.relname",
        "SELECT c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod),a.attnotnull,"
        "a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
        "WHERE n.nspname=current_schema() AND c.relname LIKE 'procrastinate_%' "
        "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        "SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname LIKE 'procrastinate_%' "
        "ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
        "SELECT c.relname,k.conname,pg_get_constraintdef(k.oid),k.convalidated "
        "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname LIKE 'procrastinate_%' "
        "ORDER BY c.relname,k.conname",
        "SELECT c.relname,pg_get_indexdef(i.indexrelid),i.indisvalid,i.indisready "
        "FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname LIKE 'procrastinate_%' "
        "ORDER BY c.relname,pg_get_indexdef(i.indexrelid)",
        "SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname LIKE 'procrastinate_%' "
        "AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
        "SELECT t.typname,t.typtype,e.enumsortorder,e.enumlabel "
        "FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
        "LEFT JOIN pg_enum e ON e.enumtypid=t.oid "
        "WHERE n.nspname=current_schema() AND t.typname LIKE 'procrastinate_%' "
        "ORDER BY t.typname,e.enumsortorder",
        "SELECT c.relname,format_type(s.seqtypid,NULL),s.seqstart,s.seqincrement,"
        "s.seqmax,s.seqmin,s.seqcache,s.seqcycle "
        "FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname LIKE 'procrastinate_%' ORDER BY c.relname",
    )
    definitions = [[tuple(row) for row in connection.execute(text(query))] for query in queries]
    return hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()


ATTRIBUTIONS = (
    ("identity_audit_events", "actor_id"),
    ("accounting_observations", "actor_id"),
    ("usage_attempts", "actor_id"),
    ("budget_scopes", "creator_id"),
)
TABLES = (
    "scheduled_actions",
    "action_executions",
    "action_outbox",
    "action_sources",
    "collection_tasks",
    "approval_requests",
    "approval_decisions",
)

# Frozen T07 domain DDL; future model changes cannot rewrite this migration.
DOMAIN_DDL = (
    """CREATE TABLE identity_actors (
	kind VARCHAR NOT NULL,
	user_id UUID,
	tenant_id UUID,
	purpose VARCHAR,
	active BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, purpose),
        CHECK ((kind='human' AND user_id IS NOT NULL AND user_id=id AND tenant_id IS NULL AND purpose IS
NULL) OR (kind='service' AND user_id IS NULL AND tenant_id IS NOT NULL AND purpose IS NOT NULL AND
purpose='durable_worker')),
	UNIQUE (user_id),
	FOREIGN KEY(user_id) REFERENCES identity_users (id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)""",
    """CREATE TABLE scheduled_actions (
	tenant_id UUID NOT NULL,
	provider_connection_id UUID NOT NULL,
	invoice_id UUID NOT NULL,
	source_revision VARCHAR NOT NULL,
	source_event_id UUID NOT NULL,
	policy_version_id UUID NOT NULL,
	policy_generation INTEGER NOT NULL,
	initiator_id UUID NOT NULL,
	membership_version INTEGER NOT NULL,
	tenant_version INTEGER NOT NULL,
	outbound_paused BOOLEAN NOT NULL,
	automation_enabled BOOLEAN NOT NULL,
	worker_actor_id UUID NOT NULL,
	actor_version INTEGER NOT NULL,
	connection_version INTEGER NOT NULL,
	correlation_id UUID NOT NULL,
	kind VARCHAR NOT NULL,
	operation_key VARCHAR NOT NULL,
	command JSON NOT NULL,
	due_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR NOT NULL,
	attempts INTEGER NOT NULL,
	automatic_resend BOOLEAN NOT NULL,
	last_error VARCHAR,
	result JSON,
	lease_token UUID,
	lease_until TIMESTAMP WITH TIME ZONE,
	job_id INTEGER,
	reservation_id UUID,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (tenant_id, operation_key),
        FOREIGN KEY(tenant_id, provider_connection_id, invoice_id) REFERENCES invoice_snapshots
(tenant_id, provider_connection_id, id),
	CHECK (attempts BETWEEN 0 AND 3),
        CHECK (status IN
('proposed','validated','awaiting_approval','queued','executing','succeeded','failed','unknown_outcome','cancelled','dead_letter')),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(source_event_id) REFERENCES accounting_events (id),
	FOREIGN KEY(policy_version_id) REFERENCES collection_policy_versions (id),
	FOREIGN KEY(initiator_id) REFERENCES identity_users (id),
	FOREIGN KEY(worker_actor_id) REFERENCES identity_actors (id),
	FOREIGN KEY(reservation_id) REFERENCES budget_reservations (id)
)""",
    """CREATE INDEX ix_scheduled_actions_due_at ON scheduled_actions (due_at)""",
    """CREATE INDEX ix_scheduled_actions_tenant_id ON scheduled_actions (tenant_id)""",
    """CREATE TABLE action_executions (
	tenant_id UUID NOT NULL,
	action_id UUID NOT NULL,
	attempt INTEGER NOT NULL,
	lease_token UUID NOT NULL,
	status VARCHAR NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	retry_delay_seconds INTEGER,
	provider_operation_id VARCHAR,
	error JSON,
	result JSON,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, action_id) REFERENCES scheduled_actions (tenant_id, id),
	UNIQUE (action_id, attempt),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	UNIQUE (lease_token)
)""",
    """CREATE INDEX ix_action_executions_tenant_id ON action_executions (tenant_id)""",
    """CREATE TABLE action_outbox (
	tenant_id UUID NOT NULL,
	action_id UUID NOT NULL,
	action_version INTEGER NOT NULL,
	due_at TIMESTAMP WITH TIME ZONE NOT NULL,
	job_id INTEGER,
	dispatched_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, action_id) REFERENCES scheduled_actions (tenant_id, id),
	UNIQUE (action_id, action_version),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)""",
    """CREATE INDEX ix_action_outbox_tenant_id ON action_outbox (tenant_id)""",
    """CREATE TABLE action_sources (
	tenant_id UUID NOT NULL,
	action_id UUID NOT NULL,
	source_event_id UUID NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (action_id, source_event_id),
	FOREIGN KEY(tenant_id, action_id) REFERENCES scheduled_actions (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(action_id) REFERENCES scheduled_actions (id),
	FOREIGN KEY(source_event_id) REFERENCES accounting_events (id)
)""",
    """CREATE INDEX ix_action_sources_tenant_id ON action_sources (tenant_id)""",
    """CREATE TABLE collection_tasks (
	tenant_id UUID NOT NULL,
	source_action_id UUID NOT NULL,
	source_event_id UUID NOT NULL,
	invoice_id UUID NOT NULL,
	creator_id UUID NOT NULL,
	assignee_id UUID,
	title VARCHAR NOT NULL,
	notes VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	priority_rank NUMERIC(30, 10) NOT NULL,
	due_at TIMESTAMP WITH TIME ZONE,
	deleted_at TIMESTAMP WITH TIME ZONE,
	command JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, source_action_id),
	FOREIGN KEY(tenant_id, source_action_id) REFERENCES scheduled_actions (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(source_action_id) REFERENCES scheduled_actions (id),
	FOREIGN KEY(source_event_id) REFERENCES accounting_events (id),
	FOREIGN KEY(invoice_id) REFERENCES invoice_snapshots (id),
	FOREIGN KEY(creator_id) REFERENCES identity_actors (id),
	FOREIGN KEY(assignee_id) REFERENCES identity_users (id)
)""",
    """CREATE INDEX ix_collection_tasks_tenant_id ON collection_tasks (tenant_id)""",
    """CREATE TABLE approval_requests (
	tenant_id UUID NOT NULL,
	action_id UUID NOT NULL,
	action_version INTEGER NOT NULL,
	proposer_id UUID NOT NULL,
	initiator_id UUID NOT NULL,
	material_hash VARCHAR NOT NULL,
	proposal JSON NOT NULL,
	status VARCHAR NOT NULL,
	granted_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	consumed_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, action_id, action_version, material_hash),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, action_id) REFERENCES scheduled_actions (tenant_id, id),
        CHECK ((granted_at IS NULL AND expires_at IS NULL AND consumed_at IS NULL) OR (granted_at IS NOT
NULL AND expires_at IS NOT NULL AND expires_at > granted_at AND expires_at <= granted_at + interval '24
hours')),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(action_id) REFERENCES scheduled_actions (id),
	FOREIGN KEY(proposer_id) REFERENCES identity_actors (id),
	FOREIGN KEY(initiator_id) REFERENCES identity_users (id)
)""",
    """CREATE INDEX ix_approval_requests_tenant_id ON approval_requests (tenant_id)""",
    """CREATE TABLE approval_decisions (
	tenant_id UUID NOT NULL,
	request_id UUID NOT NULL,
	approver_id UUID NOT NULL,
	decision VARCHAR NOT NULL,
	reason VARCHAR NOT NULL,
	material_hash VARCHAR NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, request_id) REFERENCES approval_requests (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(request_id) REFERENCES approval_requests (id),
	FOREIGN KEY(approver_id) REFERENCES identity_users (id)
)""",
    """CREATE INDEX ix_approval_decisions_tenant_id ON approval_decisions (tenant_id)""",
)


def repoint(target: str) -> None:
    for table, column in ATTRIBUTIONS:
        for fk in inspect(op.get_bind()).get_foreign_keys(table):
            if fk["constrained_columns"] == [column]:
                assert fk["name"] is not None
                op.drop_constraint(fk["name"], table, type_="foreignkey")
        op.create_foreign_key(f"fk_{table}_{column}", table, target, [column], ["id"])


def upgrade() -> None:
    op.execute(DOMAIN_DDL[0])
    op.execute("""INSERT INTO identity_actors
        (id,created_at,updated_at,version,kind,user_id,tenant_id,purpose,active)
        SELECT id,created_at,updated_at,version,'human',id,NULL,NULL,true FROM identity_users""")
    op.execute("""CREATE FUNCTION create_human_actor() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        INSERT INTO identity_actors
        (id,created_at,updated_at,version,kind,user_id,tenant_id,purpose,active)
        VALUES(NEW.id,NEW.created_at,NEW.updated_at,NEW.version,'human',NEW.id,NULL,NULL,true);
        RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER human_actor AFTER INSERT ON identity_users "
        "FOR EACH ROW EXECUTE FUNCTION create_human_actor()"
    )
    repoint("identity_actors")
    for statement in DOMAIN_DDL[1:]:
        op.execute(statement)
    op.execute("""CREATE FUNCTION preserve_action_authority() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF (to_jsonb(NEW) - ARRAY['updated_at','version','due_at','status','attempts','automatic_resend',
          'last_error','result','lease_token','lease_until','job_id']) IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['updated_at','version','due_at','status','attempts','automatic_resend',
          'last_error','result','lease_token','lease_until','job_id']) THEN
        RAISE EXCEPTION 'immutable action authority';
      END IF;
      RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER immutable_action_authority BEFORE UPDATE ON scheduled_actions "
        "FOR EACH ROW EXECUTE FUNCTION preserve_action_authority()"
    )
    # Use the installed maintained library's own DDL in this explicit migration.
    connection = op.get_bind()
    schema = SchemaManager.get_schema()
    fingerprint = {"version": version("procrastinate"), "digest": hashlib.sha256(schema.encode()).hexdigest()}
    existing = connection.scalar(text("SELECT to_regclass('aco_worker_schema')"))
    if existing:
        saved = (
            connection.execute(text("SELECT version,digest,catalog FROM aco_worker_schema")).mappings().one()
        )
        expected = {**fingerprint, "catalog": worker_catalog(connection)}
        if dict(saved) != expected:
            raise RuntimeError("incompatible or partial retained worker schema")
    else:
        if connection.scalar(text("SELECT to_regclass('procrastinate_jobs')")):
            raise RuntimeError("unidentified retained worker schema")
        connection.exec_driver_sql(schema.replace("%", "%%"))
        op.execute(
            "CREATE TABLE aco_worker_schema(version text NOT NULL,digest text NOT NULL,catalog text NOT NULL)"
        )
        connection.execute(
            text("INSERT INTO aco_worker_schema VALUES(:version,:digest,:catalog)"),
            {**fingerprint, "catalog": worker_catalog(connection)},
        )


def downgrade() -> None:
    connection = op.get_bind()
    for model in TABLES:
        if connection.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {model})")):
            raise RuntimeError("cannot downgrade: durable action/review history would be lost")
    for table, column in ATTRIBUTIONS:
        if connection.scalar(
            text(
                f"SELECT EXISTS(SELECT 1 FROM {table} t JOIN identity_actors a "
                f"ON a.id=t.{column} WHERE a.kind='service')"
            )
        ):
            raise RuntimeError("cannot downgrade: service-attributed history would be lost")
    # Procrastinate's independent queue history is intentionally retained, never reset.
    for model in reversed(TABLES):
        op.drop_table(model)
    op.execute("DROP FUNCTION preserve_action_authority()")
    repoint("identity_users")
    op.execute("DROP TRIGGER human_actor ON identity_users")
    op.execute("DROP FUNCTION create_human_actor()")
    op.drop_table("identity_actors")
