"""Stored policy authority, reason-specific holds and dispatch guard evidence."""

from sqlalchemy import text

from alembic import op

revision = "r0005_policies"
down_revision = "r0004_actions"
branch_labels = None
depends_on = None

# Frozen DDL: never import current application models in a historical migration.
DOMAIN_DDL = (
    (
        "CREATE TABLE policy_events (\n\ttenant_id UUID NOT NULL, \n\tpolicy_v"
        "ersion_id UUID NOT NULL, \n\taudit_event_id UUID NOT NULL, \n\tkind V"
        "ARCHAR NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH "
        "TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NUL"
        "L, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tena"
        "nt_id, id), \n\tFOREIGN KEY(tenant_id, policy_version_id) REFERENCE"
        "S collection_policy_versions (tenant_id, id), \n\tFOREIGN KEY(tenan"
        "t_id) REFERENCES tenants (id), \n\tFOREIGN KEY(audit_event_id) REFE"
        "RENCES identity_audit_events (id)\n)"
    ),
    "CREATE INDEX ix_policy_events_tenant_id ON policy_events (tenant_id)",
    (
        "CREATE TABLE policy_activations (\n\ttenant_id UUID NOT NULL, \n\tpol"
        "icy_version_id UUID NOT NULL, \n\tevent_id UUID NOT NULL, \n\tenablin"
        "g_human_id UUID NOT NULL, \n\tmembership_version INTEGER NOT NULL, "
        "\n\tworker_actor_id UUID NOT NULL, \n\tactor_version INTEGER NOT NULL"
        ", \n\ttenant_generation INTEGER NOT NULL, \n\toutbound_paused BOOLEAN"
        " NOT NULL, \n\tnative_reminders_disabled BOOLEAN NOT NULL, \n\tconfig"
        "uration_reviewed BOOLEAN NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_"
        "at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH"
        " TIME ZONE NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (i"
        "d), \n\tUNIQUE (tenant_id, id), \n\tFOREIGN KEY(tenant_id, policy_ver"
        "sion_id) REFERENCES collection_policy_versions (tenant_id, id), \n"
        "\tFOREIGN KEY(tenant_id, event_id) REFERENCES policy_events (tenan"
        "t_id, id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id), \n\tFO"
        "REIGN KEY(enabling_human_id) REFERENCES identity_users (id), \n\tFO"
        "REIGN KEY(worker_actor_id) REFERENCES identity_actors (id)\n)"
    ),
    "CREATE INDEX ix_policy_activations_tenant_id ON policy_activations (tenant_id)",
    (
        "CREATE TABLE policy_recipient_controls (\n\ttenant_id UUID NOT NULL"
        ", \n\tprovider_connection_id UUID NOT NULL, \n\tcontact_id UUID NOT N"
        "ULL, \n\treviewer_id UUID NOT NULL, \n\treviewed_destination VARCHAR,"
        " \n\treviewed_contact_revision VARCHAR NOT NULL, \n\ttimezone VARCHAR"
        ", \n\ttimezone_verified BOOLEAN NOT NULL, \n\towner_confirmed_tenant_"
        "timezone BOOLEAN NOT NULL, \n\tconfirmed_tenant_timezone VARCHAR, \n"
        "\tdestination_verified BOOLEAN NOT NULL, \n\tallowed_channels JSON N"
        "OT NULL, \n\topt_out BOOLEAN NOT NULL, \n\tevidence VARCHAR NOT NULL,"
        " \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NU"
        "LL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tversion INT"
        "EGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, contact_i"
        "d), \n\tFOREIGN KEY(tenant_id, provider_connection_id, contact_id) "
        "REFERENCES customer_contacts (tenant_id, provider_connection_id, "
        "id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id), \n\tFOREIGN "
        "KEY(reviewer_id) REFERENCES identity_users (id)\n)"
    ),
    "CREATE INDEX ix_policy_recipient_controls_tenant_id ON policy_recipient_controls (tenant_id)",
    (
        "CREATE TABLE policy_occurrences (\n\ttenant_id UUID NOT NULL, \n\tinv"
        "oice_id UUID NOT NULL, \n\tpolicy_id UUID NOT NULL, \n\trule_id UUID "
        "NOT NULL, \n\tdue_date_anchor DATE NOT NULL, \n\toperation_key VARCHA"
        "R NOT NULL, \n\tstate VARCHAR NOT NULL, \n\treconsider_after TIMESTAM"
        "P WITH TIME ZONE, \n\tlast_reason VARCHAR, \n\tid UUID NOT NULL, \n\tcr"
        "eated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAM"
        "P WITH TIME ZONE NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY "
        "KEY (id), \n\tUNIQUE (tenant_id, id), \n\tUNIQUE (tenant_id, operatio"
        "n_key), \n\tFOREIGN KEY(tenant_id, invoice_id) REFERENCES invoice_s"
        "napshots (tenant_id, id), \n\tFOREIGN KEY(tenant_id, policy_id) REF"
        "ERENCES collection_policies (tenant_id, id), \n\tFOREIGN KEY(tenant"
        "_id) REFERENCES tenants (id)\n)"
    ),
    "CREATE INDEX ix_policy_occurrences_tenant_id ON policy_occurrences (tenant_id)",
    (
        "CREATE TABLE collection_holds (\n\ttenant_id UUID NOT NULL, \n\tinvoi"
        "ce_id UUID NOT NULL, \n\treason VARCHAR NOT NULL, \n\tevidence VARCHA"
        "R NOT NULL, \n\tcreator_id UUID NOT NULL, \n\texpires_at TIMESTAMP WI"
        "TH TIME ZONE, \n\treleased_at TIMESTAMP WITH TIME ZONE, \n\trelease_e"
        "vidence VARCHAR, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH "
        "TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NUL"
        "L, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY("
        "tenant_id, invoice_id) REFERENCES invoice_snapshots (tenant_id, i"
        "d), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id), \n\tFOREIGN K"
        "EY(creator_id) REFERENCES identity_actors (id)\n)"
    ),
    "CREATE INDEX ix_collection_holds_tenant_id ON collection_holds (tenant_id)",
    (
        "CREATE TABLE contact_reservations (\n\ttenant_id UUID NOT NULL, \n\ti"
        "nvoice_id UUID NOT NULL, \n\tcustomer_id UUID NOT NULL, \n\tdestinati"
        "on VARCHAR NOT NULL, \n\tchannel VARCHAR NOT NULL, \n\toperation_id U"
        "UID NOT NULL, \n\toccurrence_id UUID, \n\taction_id UUID, \n\tstatus VA"
        "RCHAR NOT NULL, \n\treserved_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "\n\tsubmitted_at TIMESTAMP WITH TIME ZONE, \n\treleased_at TIMESTAMP "
        "WITH TIME ZONE, \n\tevidence JSON NOT NULL, \n\tid UUID NOT NULL, \n\tc"
        "reated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTA"
        "MP WITH TIME ZONE NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY"
        " KEY (id), \n\tUNIQUE (tenant_id, operation_id), \n\tFOREIGN KEY(tena"
        "nt_id, invoice_id, customer_id) REFERENCES invoice_snapshots (ten"
        "ant_id, id, customer_id), \n\tFOREIGN KEY(tenant_id, invoice_id) RE"
        "FERENCES invoice_snapshots (tenant_id, id), \n\tFOREIGN KEY(tenant_"
        "id, occurrence_id) REFERENCES policy_occurrences (tenant_id, id),"
        " \n\tCHECK (status IN ('reserved','inflight','accepted','connected'"
        ",'unknown_outcome','released')), \n\tFOREIGN KEY(tenant_id) REFEREN"
        "CES tenants (id), \n\tFOREIGN KEY(customer_id) REFERENCES customers"
        " (id), \n\tFOREIGN KEY(action_id) REFERENCES scheduled_actions (id)"
        "\n)"
    ),
    "CREATE INDEX ix_contact_reservations_customer_id ON contact_reservations (customer_id)",
    "CREATE INDEX ix_contact_reservations_destination ON contact_reservations (destination)",
    "CREATE INDEX ix_contact_reservations_tenant_id ON contact_reservations (tenant_id)",
    (
        "CREATE TABLE dispatch_reads (\n\ttenant_id UUID NOT NULL, \n\tinvoice"
        "_id UUID NOT NULL, \n\tactor_id UUID NOT NULL, \n\taction_id UUID, \n\t"
        "action_version INTEGER, \n\tlease_token UUID, \n\tattempt_id UUID NOT"
        " NULL, \n\tincarnation UUID NOT NULL, \n\tobservation_id UUID, \n\terro"
        "r VARCHAR, \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid U"
        "UID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tu"
        "pdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tversion INTEGER NO"
        "T NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, id), \n\tFOREIGN K"
        "EY(tenant_id, invoice_id) REFERENCES invoice_snapshots (tenant_id"
        ", id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id), \n\tFOREIG"
        "N KEY(actor_id) REFERENCES identity_actors (id), \n\tFOREIGN KEY(ac"
        "tion_id) REFERENCES scheduled_actions (id), \n\tFOREIGN KEY(observa"
        "tion_id) REFERENCES accounting_observations (id)\n)"
    ),
    "CREATE INDEX ix_dispatch_reads_tenant_id ON dispatch_reads (tenant_id)",
    (
        "CREATE TABLE dispatch_tickets (\n\ttenant_id UUID NOT NULL, \n\tactio"
        "n_id UUID NOT NULL, \n\tattempt_id UUID NOT NULL, \n\tread_id UUID NO"
        "T NULL, \n\tincarnation UUID NOT NULL, \n\tissued_at TIMESTAMP WITH T"
        "IME ZONE NOT NULL, \n\tconsumed_at TIMESTAMP WITH TIME ZONE, \n\tbind"
        "ings JSON NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WI"
        "TH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT "
        "NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN K"
        "EY(tenant_id, action_id) REFERENCES scheduled_actions (tenant_id,"
        " id), \n\tFOREIGN KEY(tenant_id, read_id) REFERENCES dispatch_reads"
        " (tenant_id, id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id"
        ")\n)"
    ),
    "CREATE INDEX ix_dispatch_tickets_tenant_id ON dispatch_tickets (tenant_id)",
)


def upgrade() -> None:
    op.execute("ALTER TABLE collection_policies ADD COLUMN archived_at timestamptz")
    op.create_unique_constraint(
        "uq_policy_version_tenant_id", "collection_policy_versions", ["tenant_id", "id"]
    )
    op.create_unique_constraint(
        "uq_invoice_customer_binding", "invoice_snapshots", ["tenant_id", "id", "customer_id"]
    )
    op.create_unique_constraint("uq_policy_actor_tenant", "identity_actors", ["tenant_id", "id"])
    op.create_unique_constraint("uq_policy_audit_tenant", "identity_audit_events", ["tenant_id", "id"])
    op.create_unique_constraint(
        "uq_dispatch_observation_invoice", "accounting_observations", ["tenant_id", "invoice_id", "id"]
    )
    for statement in DOMAIN_DDL:
        op.execute(statement)
    op.execute("ALTER TABLE scheduled_actions ALTER COLUMN source_event_id DROP NOT NULL")
    for column, target in (
        ("policy_event_id", "policy_events"),
        ("policy_activation_id", "policy_activations"),
        ("occurrence_id", "policy_occurrences"),
    ):
        op.execute(f"ALTER TABLE scheduled_actions ADD COLUMN {column} uuid")
        op.create_foreign_key(
            f"fk_action_{column}", "scheduled_actions", target, ["tenant_id", column], ["tenant_id", "id"]
        )
    op.create_foreign_key(
        "fk_policy_event_audit_tenant",
        "policy_events",
        "identity_audit_events",
        ["tenant_id", "audit_event_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_policy_activation_member",
        "policy_activations",
        "memberships",
        ["tenant_id", "enabling_human_id"],
        ["tenant_id", "user_id"],
    )
    op.create_foreign_key(
        "fk_policy_activation_actor_tenant",
        "policy_activations",
        "identity_actors",
        ["tenant_id", "worker_actor_id"],
        ["tenant_id", "id"],
    )
    op.create_unique_constraint("uq_activation_event", "policy_activations", ["tenant_id", "id", "event_id"])
    op.create_unique_constraint(
        "uq_activation_version", "policy_activations", ["tenant_id", "id", "policy_version_id"]
    )
    op.create_foreign_key(
        "fk_action_policy_tenant",
        "scheduled_actions",
        "collection_policy_versions",
        ["tenant_id", "policy_version_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_action_activation_event",
        "scheduled_actions",
        "policy_activations",
        ["tenant_id", "policy_activation_id", "policy_event_id"],
        ["tenant_id", "id", "event_id"],
    )
    op.create_foreign_key(
        "fk_action_activation_version",
        "scheduled_actions",
        "policy_activations",
        ["tenant_id", "policy_activation_id", "policy_version_id"],
        ["tenant_id", "id", "policy_version_id"],
    )
    op.create_foreign_key(
        "fk_dispatch_read_observation",
        "dispatch_reads",
        "accounting_observations",
        ["tenant_id", "invoice_id", "observation_id"],
        ["tenant_id", "invoice_id", "id"],
    )
    op.create_check_constraint(
        "ck_action_trigger",
        "scheduled_actions",
        (
            "(kind='policy_due' AND source_event_id IS NULL AND policy_event_i"
            "d IS NOT NULL AND policy_activation_id IS NOT NULL AND occurrence"
            "_id IS NOT NULL) OR (kind IN ('accounting_recheck','internal_task"
            "','approval_request') AND source_event_id IS NOT NULL AND policy_"
            "event_id IS NULL AND policy_activation_id IS NULL AND occurrence_"
            "id IS NULL)"
        ),
    )
    op.execute(
        "CREATE FUNCTION preserve_policy_history() RETURNS trigger LANGUAG"
        "E plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable policy history';"
        " END $$"
    )
    for table in ("policy_events", "policy_activations", "dispatch_reads"):
        op.execute(
            f"CREATE TRIGGER immutable_policy_history BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION preserve_policy_history()"
        )


def downgrade() -> None:
    connection = op.get_bind()
    for table in TABLES:
        if connection.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table})")):
            raise RuntimeError("cannot downgrade: policy/dispatch evidence would be lost")
    if connection.scalar(
        text("SELECT EXISTS(SELECT 1 FROM collection_policies WHERE archived_at IS NOT NULL)")
    ):
        raise RuntimeError("cannot downgrade: policy archive history would be lost")
    op.drop_constraint("ck_action_trigger", "scheduled_actions", type_="check")
    op.drop_constraint("fk_action_policy_tenant", "scheduled_actions", type_="foreignkey")
    for column in ("policy_event_id", "policy_activation_id", "occurrence_id"):
        op.drop_column("scheduled_actions", column)
    op.execute("ALTER TABLE scheduled_actions ALTER COLUMN source_event_id SET NOT NULL")
    for table in reversed(TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION preserve_policy_history()")
    op.drop_constraint("uq_policy_version_tenant_id", "collection_policy_versions", type_="unique")
    op.drop_column("collection_policies", "archived_at")
    op.drop_constraint("uq_policy_actor_tenant", "identity_actors", type_="unique")
    op.drop_constraint("uq_policy_audit_tenant", "identity_audit_events", type_="unique")
    op.drop_constraint("uq_dispatch_observation_invoice", "accounting_observations", type_="unique")
    op.drop_constraint("uq_invoice_customer_binding", "invoice_snapshots", type_="unique")


TABLES = (
    "policy_events",
    "policy_activations",
    "policy_recipient_controls",
    "policy_occurrences",
    "collection_holds",
    "contact_reservations",
    "dispatch_reads",
    "dispatch_tickets",
)
