"""Persist bounded investigation authority, stable steps and append-only event cursors."""

import sqlalchemy as sa

from alembic import op

revision = "r0010_agent"
down_revision = "r0009_promises"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        
            "\nCREATE TABLE agent_runs (\n\ttenant_id UUID NOT NULL, \n\trequest_id UUID NOT "
            "NULL, \n\tinitiator_id UUID NOT NULL, \n\tmembership_version INTEGER NOT NULL, "
            "\n\tworker_actor_id UUID NOT NULL, \n\tactor_version INTEGER NOT NULL, \n\ttenant"
            "_generation INTEGER NOT NULL, \n\tprovider_connection_id UUID NOT NULL, \n\tcon"
            "nection_version INTEGER NOT NULL, \n\tcorrelation_id UUID NOT NULL, \n\tobjecti"
            "ve VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tauthenticity VARCHAR NOT "
            "NULL, \n\tmodel_route VARCHAR NOT NULL, \n\tmodel_id VARCHAR, \n\tprompt_version "
            "VARCHAR NOT NULL, \n\tschema_version VARCHAR NOT NULL, \n\tthread_id VARCHAR NO"
            "T NULL, \n\tjob_id INTEGER, \n\tjob_version INTEGER NOT NULL, \n\tincarnation INT"
            "EGER NOT NULL, \n\tlease_token UUID, \n\tlease_until TIMESTAMP WITH TIME ZONE, "
            "\n\tmodel_state VARCHAR NOT NULL, \n\tresult JSON, \n\terror_code VARCHAR, \n\tlast"
            "_event_sequence INTEGER NOT NULL, \n\tlast_step_sequence INTEGER NOT NULL, \n\t"
            "provider_attempted BOOLEAN NOT NULL, \n\trequests INTEGER NOT NULL, \n\tinput_t"
            "okens INTEGER, \n\toutput_tokens INTEGER, \n\tid UUID NOT NULL, \n\tcreated_at TI"
            "MESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT "
            "NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, "
            "id), \n\tUNIQUE (tenant_id, request_id), \n\tFOREIGN KEY(tenant_id, initiator_i"
            "d) REFERENCES memberships (tenant_id, user_id), \n\tFOREIGN KEY(tenant_id, wo"
            "rker_actor_id) REFERENCES identity_actors (tenant_id, id), \n\tFOREIGN KEY(te"
            "nant_id, provider_connection_id) REFERENCES provider_connections (tenant_id"
            ", id), \n\tCHECK (status IN ('queued','running','awaiting_approval','complete"
            "d','partially_completed','failed','cancelled')), \n\tCHECK (authenticity IN ("
            "'FAKE','LIVE')), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id), \n\tUNIQUE"
            " (thread_id)\n)\n\n"
        
    )
    op.execute("CREATE INDEX ix_agent_runs_tenant_id ON agent_runs (tenant_id)")
    op.execute(
        
            "\nCREATE TABLE agent_run_invoices (\n\ttenant_id UUID NOT NULL, \n\trun_id UUID "
            "NOT NULL, \n\tinvoice_id UUID NOT NULL, \n\tPRIMARY KEY (tenant_id, run_id, inv"
            "oice_id), \n\tFOREIGN KEY(tenant_id, run_id) REFERENCES agent_runs (tenant_id"
            ", id), \n\tFOREIGN KEY(tenant_id, invoice_id) REFERENCES invoice_snapshots (t"
            "enant_id, id)\n)\n\n"
        
    )
    op.execute(
        
            "\nCREATE TABLE agent_steps (\n\ttenant_id UUID NOT NULL, \n\trun_id UUID NOT NUL"
            "L, \n\tstep_sequence INTEGER NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tstatus VARC"
            "HAR NOT NULL, \n\ttool_name VARCHAR, \n\tdescription VARCHAR NOT NULL, \n\tinvoic"
            "e_id UUID, \n\tinput JSON NOT NULL, \n\toutput JSON, \n\terror_code VARCHAR, \n\tid"
            " UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_a"
            "t TIMESTAMP WITH TIME ZONE NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY "
            "KEY (id), \n\tUNIQUE (tenant_id, run_id, id), \n\tUNIQUE (run_id, step_sequence"
            "), \n\tFOREIGN KEY(tenant_id, run_id) REFERENCES agent_runs (tenant_id, id), "
            "\n\tFOREIGN KEY(tenant_id, invoice_id) REFERENCES invoice_snapshots (tenant_i"
            "d, id), \n\tCHECK (status IN ('started','completed','failed'))\n)\n\n"
        
    )
    op.execute("CREATE INDEX ix_agent_steps_run_id ON agent_steps (run_id)")
    op.execute("CREATE INDEX ix_agent_steps_tenant_id ON agent_steps (tenant_id)")
    op.execute(
        
            "\nCREATE TABLE agent_events (\n\ttenant_id UUID NOT NULL, \n\trun_id UUID NOT NU"
            "LL, \n\tevent_sequence INTEGER NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tstep_id U"
            "UID, \n\tdescription VARCHAR NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMES"
            "TAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NUL"
            "L, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (run_id, event_"
            "sequence), \n\tFOREIGN KEY(tenant_id, run_id) REFERENCES agent_runs (tenant_i"
            "d, id), \n\tFOREIGN KEY(tenant_id, run_id, step_id) REFERENCES agent_steps (t"
            "enant_id, run_id, id)\n)\n\n"
        
    )
    op.execute("CREATE INDEX ix_agent_events_tenant_id ON agent_events (tenant_id)")
    op.execute("CREATE INDEX ix_agent_events_run_id ON agent_events (run_id)")
    op.execute(
        
            "\nCREATE TABLE agent_usage (\n\ttenant_id UUID NOT NULL, \n\trun_id UUID NOT NUL"
            "L, \n\tstep_id UUID NOT NULL, \n\tattempt_id UUID NOT NULL, \n\tevent_id UUID, \n\t"
            "reservation_id UUID, \n\tobservation JSON, \n\tPRIMARY KEY (tenant_id, run_id, "
            "attempt_id), \n\tFOREIGN KEY(tenant_id, run_id, step_id) REFERENCES agent_ste"
            "ps (tenant_id, run_id, id), \n\tFOREIGN KEY(tenant_id, attempt_id) REFERENCES"
            " usage_attempts (tenant_id, id), \n\tFOREIGN KEY(tenant_id, event_id) REFEREN"
            "CES provider_usage_events (tenant_id, id), \n\tFOREIGN KEY(tenant_id, reserva"
            "tion_id) REFERENCES budget_reservations (tenant_id, id)\n)\n\n"
        
    )
    op.execute(
        
            "CREATE FUNCTION aco_agent_events_append_only() RETURNS trigger LANGUAGE plp"
            "gsql AS $$ BEGIN RAISE EXCEPTION 'agent events are append only'; END $$"
        
    )
    op.execute(
        
            "CREATE TRIGGER agent_events_append_only BEFORE UPDATE OR DELETE ON agent_ev"
            "ents FOR EACH ROW EXECUTE FUNCTION aco_agent_events_append_only()"
        
    )

    op.add_column("scheduled_actions", sa.Column("agent_run_id", sa.UUID(), nullable=True))
    op.add_column("scheduled_actions", sa.Column("agent_step_id", sa.UUID(), nullable=True))
    op.alter_column("scheduled_actions", "policy_version_id", existing_type=sa.UUID(), nullable=True)
    op.create_foreign_key(
        ("fk_action_agent_step"),
        ("scheduled_actions"),
        ("agent_steps"),
        [("tenant_id"), ("agent_run_id"), ("agent_step_id")],
        [("tenant_id"), ("run_id"), ("id")],
    )
    op.drop_constraint("ck_action_trigger", "scheduled_actions", type_="check")
    op.create_check_constraint(
        ("ck_action_trigger"),
        ("scheduled_actions"),
        (
            "(kind='policy_due' AND source_event_id IS NULL AND policy_event_id IS NOT N"
            "ULL AND policy_activation_id IS NOT NULL AND occurrence_id IS NOT NULL AND "
            "agent_run_id IS NULL AND agent_step_id IS NULL) OR (kind IN ('accounting_re"
            "check','internal_task','approval_request') AND source_event_id IS NOT NULL "
            "AND policy_event_id IS NULL AND policy_activation_id IS NULL AND occurrence"
            "_id IS NULL AND agent_run_id IS NULL AND agent_step_id IS NULL) OR (kind='a"
            "gent_internal' AND source_event_id IS NULL AND policy_event_id IS NULL AND "
            "policy_activation_id IS NULL AND occurrence_id IS NULL AND policy_version_i"
            "d IS NULL AND agent_run_id IS NOT NULL AND agent_step_id IS NOT NULL)"
        ),
    )
    op.add_column(
        ("collection_tasks"),
        sa.Column(("source_agent_run_id"), sa.UUID(), sa.ForeignKey("agent_runs.id"), nullable=True),
    )
    op.drop_constraint("ck_collection_tasks_source_pair", "collection_tasks", type_="check")
    op.create_check_constraint(
        ("ck_collection_tasks_source_pair"),
        ("collection_tasks"),
        (
            "(source_action_id IS NULL AND source_event_id IS NULL AND source_agent_run_"
            "id IS NULL) OR (source_action_id IS NOT NULL AND source_event_id IS NOT NUL"
            "L AND source_agent_run_id IS NULL) OR (source_action_id IS NOT NULL AND sou"
            "rce_event_id IS NULL AND source_agent_run_id IS NOT NULL)"
        ),
    )
    op.execute(
        
            "\nCREATE TABLE agent_bounded_live_grants (\n\ttenant_id UUID NOT NULL, \n\tiniti"
            "ator_id UUID NOT NULL, \n\tmembership_version INTEGER NOT NULL, \n\tprovider_co"
            "nnection_id UUID NOT NULL, \n\tmodel_id VARCHAR NOT NULL, \n\tauthorization_ref"
            " VARCHAR NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tactive"
            " BOOLEAN NOT NULL, \n\trun_id UUID, \n\tscope_ids JSON NOT NULL, \n\tinput_rate_i"
            "d UUID NOT NULL, \n\toutput_rate_id UUID NOT NULL, \n\tmax_requests INTEGER NOT"
            " NULL, \n\tmax_input_tokens INTEGER NOT NULL, \n\tmax_output_tokens INTEGER NOT"
            " NULL, \n\tamount NUMERIC(38, 12) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at "
            "TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NO"
            "T NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id"
            ", id), \n\tFOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider"
            "_connections (tenant_id, id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants ("
            "id), \n\tFOREIGN KEY(initiator_id) REFERENCES identity_users (id), \n\tUNIQUE ("
            "run_id), \n\tFOREIGN KEY(run_id) REFERENCES agent_runs (id), \n\tFOREIGN KEY(in"
            "put_rate_id) REFERENCES usage_rates (id), \n\tFOREIGN KEY(output_rate_id) REF"
            "ERENCES usage_rates (id)\n)\n\n"
        
    )
    for constraint in sa.inspect(op.get_bind()).get_check_constraints("budget_limits"):
        if "state" in constraint["sqltext"]:
            name = constraint["name"]
            if name is None:
                raise RuntimeError("Named budget-state constraint required for the additive upgrade")
            op.drop_constraint(name, "budget_limits", type_="check")
    op.create_check_constraint(
        ("ck_budget_limit_state"),
        ("budget_limits"),
        ("state IN ('DRAFT','SYNTHETIC_TEST_ONLY','BOUNDED_LIVE_TEST')"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM agent_runs)")):
        raise RuntimeError("Cannot discard agent evidence")
    raise RuntimeError("Agent action schema downgrade requires explicit reviewed rollback")
    op.execute("DROP FUNCTION aco_agent_events_append_only() CASCADE")
    op.drop_table("agent_usage")
    op.drop_table("agent_events")
    op.drop_table("agent_steps")
    op.drop_table("agent_run_invoices")
    op.drop_table("agent_runs")
