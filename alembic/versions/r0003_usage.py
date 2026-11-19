"""Frozen canonical usage and budget foundation; old evidence retained."""

from alembic import op

revision = "r0003_usage"
down_revision = "r0002b_accounting_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE usage_rates (
    tenant_id UUID NOT NULL,
    provider VARCHAR NOT NULL,
    currency VARCHAR NOT NULL,
    unit VARCHAR NOT NULL,
    amount NUMERIC(38, 12) NOT NULL,
    price_source_at TIMESTAMP WITH TIME ZONE NOT NULL,
    synthetic BOOLEAN NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)
"""
    )
    op.execute(
        """CREATE TABLE budget_reservations (
    tenant_id UUID NOT NULL,
    provider_connection_id UUID NOT NULL,
    operation_key VARCHAR NOT NULL,
    currency VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    original_amount NUMERIC(38, 12),
    retained_amount NUMERIC(38, 12) NOT NULL,
    released_amount NUMERIC(38, 12) NOT NULL,
    spent_amount NUMERIC(38, 12) NOT NULL,
    request JSON NOT NULL,
    reason VARCHAR NOT NULL,
    automatic_retry BOOLEAN NOT NULL,
    real_provider_calls INTEGER NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id,
id),
    UNIQUE (tenant_id, provider_connection_id, operation_key),
    UNIQUE (tenant_id, id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)
"""
    )
    op.execute("CREATE INDEX ix_budget_reservations_tenant_id ON budget_reservations (tenant_id)")
    op.execute(
        """CREATE TABLE budget_scopes (
    tenant_id UUID NOT NULL,
    provider_connection_id UUID,
    dimension VARCHAR NOT NULL,
    synthetic BOOLEAN NOT NULL,
    creator_id UUID NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id,
id),
    UNIQUE (tenant_id, id),
    CHECK (dimension IN ('tenant','provider','run','call','demo_session')),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id),
    FOREIGN KEY(creator_id) REFERENCES identity_users (id)
)
"""
    )
    op.execute("CREATE INDEX ix_budget_scopes_tenant_id ON budget_scopes (tenant_id)")
    op.execute(
        """CREATE TABLE provider_usage_events (
    tenant_id UUID,
    scope_kind VARCHAR NOT NULL,
    scope_key VARCHAR NOT NULL,
    provider_connection_id UUID NOT NULL,
    operation_key VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    operation VARCHAR NOT NULL,
    provider_kind VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    correlation_id UUID NOT NULL,
    native_operation_id VARCHAR,
    native_subject VARCHAR,
    synthetic BOOLEAN NOT NULL,
    reconstructible BOOLEAN NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id,
id),
    UNIQUE (scope_key, provider_connection_id, operation_key, component),
    UNIQUE (tenant_id, id),
    CHECK ((scope_kind = 'TENANT' AND tenant_id IS NOT NULL) OR (scope_kind = 'GLOBAL_IDENTITY'
AND tenant_id IS NULL)),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)
"""
    )
    op.execute("CREATE INDEX ix_provider_usage_events_tenant_id ON provider_usage_events (tenant_id)")
    op.execute(
        """CREATE TABLE budget_limits (
    tenant_id UUID NOT NULL,
    scope_id UUID NOT NULL,
    currency VARCHAR NOT NULL,
    amount NUMERIC(38, 12) NOT NULL,
    requests INTEGER NOT NULL,
    unit_caps JSON NOT NULL,
    state VARCHAR NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id, scope_id) REFERENCES budget_scopes (tenant_id, id),
    UNIQUE (scope_id, currency, state),
    CHECK (state IN ('DRAFT','SYNTHETIC_TEST_ONLY')),
    CHECK (amount >= 0 AND requests >= 0),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)
"""
    )
    op.execute("CREATE INDEX ix_budget_limits_tenant_id ON budget_limits (tenant_id)")
    op.execute(
        """CREATE TABLE reservation_outcomes (
    reservation_id UUID NOT NULL,
    outcome_key VARCHAR NOT NULL,
    payload JSON NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (reservation_id, outcome_key),
    FOREIGN KEY(reservation_id) REFERENCES budget_reservations (id)
)
"""
    )
    op.execute(
        """CREATE TABLE reservation_scopes (
    reservation_id UUID NOT NULL,
    scope_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    PRIMARY KEY (reservation_id, scope_id),
    FOREIGN KEY(tenant_id, scope_id) REFERENCES budget_scopes (tenant_id, id),
    FOREIGN KEY(tenant_id, reservation_id) REFERENCES budget_reservations (tenant_id, id)
)
"""
    )
    op.execute(
        """CREATE TABLE usage_revisions (
    event_id UUID NOT NULL,
    reconciliation_key VARCHAR NOT NULL,
    cost_amount NUMERIC(38, 12),
    currency VARCHAR,
    cost_quality VARCHAR NOT NULL,
    outcome VARCHAR NOT NULL,
    payload JSON NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (event_id, reconciliation_key),
    CHECK ((cost_quality = 'UNKNOWN' AND cost_amount IS NULL) OR (cost_quality IN
('PROVIDER_REPORTED','RATE_ESTIMATE') AND cost_amount >= 0 AND currency IS NOT NULL)),
    FOREIGN KEY(event_id) REFERENCES provider_usage_events (id)
)
"""
    )
    op.execute("CREATE INDEX ix_usage_revisions_event_id ON usage_revisions (event_id)")
    op.execute(
        """CREATE TABLE usage_sources (
    event_id UUID NOT NULL,
    source_kind VARCHAR NOT NULL,
    source_id UUID NOT NULL,
    component VARCHAR NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (source_kind, source_id, component),
    FOREIGN KEY(event_id) REFERENCES provider_usage_events (id)
)
"""
    )
    op.execute(
        """CREATE TABLE usage_attempts (
    tenant_id UUID NOT NULL,
    provider_connection_id UUID NOT NULL,
    correlation_id UUID NOT NULL,
    actor_id UUID NOT NULL,
    operation VARCHAR NOT NULL,
    outcome VARCHAR NOT NULL,
    business_outcome VARCHAR NOT NULL,
    event_id UUID,
    error_reference VARCHAR,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id,
id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id),
    FOREIGN KEY(actor_id) REFERENCES identity_users (id),
    FOREIGN KEY(event_id) REFERENCES provider_usage_events (id)
)
"""
    )
    op.execute("CREATE INDEX ix_usage_attempts_tenant_id ON usage_attempts (tenant_id)")
    _bridge_identity()
    _bridge_accounting()
    op.execute(
        "CREATE TRIGGER immutable_usage BEFORE UPDATE OR DELETE ON "
        "provider_usage_events FOR EACH ROW EXECUTE FUNCTION "
        "reject_financial_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER immutable_usage BEFORE UPDATE OR DELETE ON usage_revisions "
        "FOR EACH ROW EXECUTE FUNCTION reject_financial_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER immutable_usage BEFORE UPDATE OR DELETE ON usage_sources FOR "
        "EACH ROW EXECUTE FUNCTION reject_financial_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER immutable_usage BEFORE UPDATE OR DELETE ON usage_rates FOR "
        "EACH ROW EXECUTE FUNCTION reject_financial_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER immutable_usage BEFORE UPDATE OR DELETE ON "
        "reservation_outcomes FOR EACH ROW EXECUTE FUNCTION "
        "reject_financial_history_mutation()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM usage_attempts) OR EXISTS (SELECT 1 FROM provider_usage_events
WHERE NOT reconstructible)
           OR EXISTS (SELECT 1 FROM usage_revisions GROUP BY event_id HAVING COUNT(*) > 1)
           OR EXISTS (SELECT 1 FROM budget_reservations)
           OR EXISTS (SELECT 1 FROM budget_limits)
           OR EXISTS (SELECT 1 FROM usage_rates) THEN
            RAISE EXCEPTION 'cannot downgrade: unreconstructible usage/budget history';
        END IF;
    END $$""")
    op.execute("DROP TRIGGER accounting_usage ON accounting_events")
    op.execute("DROP TRIGGER accounting_usage ON accounting_observations")
    op.execute("DROP FUNCTION bridge_accounting_usage()")
    op.execute("DROP FUNCTION project_accounting_usage(uuid,uuid,uuid,text,json,boolean,timestamptz)")
    op.execute("DROP TRIGGER identity_usage ON identity_provider_events")
    op.execute("DROP FUNCTION bridge_identity_usage()")
    op.drop_table("usage_attempts")
    op.drop_table("usage_sources")
    op.drop_table("usage_revisions")
    op.drop_table("reservation_scopes")
    op.drop_table("reservation_outcomes")
    op.drop_table("budget_limits")
    op.drop_table("provider_usage_events")
    op.drop_table("budget_scopes")
    op.drop_table("budget_reservations")
    op.drop_table("usage_rates")


def _bridge_identity() -> None:
    op.execute("""CREATE FUNCTION bridge_identity_usage() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE fingerprint text; next_version integer;
    BEGIN
        IF NEW.cost_quality <> 'UNKNOWN' OR NEW.cost_amount IS NOT NULL THEN
            RAISE EXCEPTION 'unpriced identity bridge cannot invent charge currency';
        END IF;
        INSERT INTO provider_usage_events
          (id, tenant_id, scope_kind, scope_key, provider_connection_id, operation_key,
           component, operation, provider_kind, provider, correlation_id, native_operation_id,
           native_subject, synthetic, reconstructible, created_at, updated_at, version)
        VALUES (NEW.id, NULL, 'GLOBAL_IDENTITY', 'GLOBAL_IDENTITY', NEW.connection_id,
           NEW.id::text, 'identity', NEW.operation, 'identity', NEW.provider, NEW.correlation_id,
           NULL, NEW.native_id, false, true, NEW.created_at, NEW.updated_at, 1)
        ON CONFLICT DO NOTHING;
        fingerprint := md5(json_build_array(NEW.outcome, NEW.native_id, NEW.cost_quality,
            NEW.cost_amount, NEW.attempt)::text);
        SELECT COALESCE(MAX(version),0)+1 INTO next_version FROM usage_revisions WHERE event_id=NEW.id;
        INSERT INTO usage_revisions
          (id, event_id, reconciliation_key, cost_amount, currency, cost_quality, outcome,
           payload, created_at, updated_at, version)
        VALUES (md5(NEW.id::text || fingerprint)::uuid, NEW.id, fingerprint, NULL, NULL,
          'UNKNOWN', NEW.outcome, json_build_object('native_subject', NEW.native_id,
          'attempt', NEW.attempt, 'currency_availability', 'UNAVAILABLE',
          'source', 'identity_provider_events'), NEW.updated_at, NEW.updated_at, next_version)
        ON CONFLICT DO NOTHING;
        RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER identity_usage AFTER INSERT OR UPDATE ON "
        "identity_provider_events FOR EACH ROW EXECUTE FUNCTION "
        "bridge_identity_usage()"
    )
    # Invoke exactly the same bridge without updating historical identity bytes.
    op.execute("""DO $$ DECLARE r record; BEGIN
        IF EXISTS (SELECT 1 FROM identity_provider_events WHERE cost_quality <> 'UNKNOWN' OR
cost_amount IS NOT NULL) THEN
          RAISE EXCEPTION 'identity currency unavailable for known historical cost';
        END IF;
    END $$""")
    op.execute("""INSERT INTO provider_usage_events
       (id,tenant_id,scope_kind,scope_key,provider_connection_id,operation_key,component,
        operation,provider_kind,provider,correlation_id,native_operation_id,native_subject,
        synthetic,reconstructible,created_at,updated_at,version)
       SELECT id,NULL,'GLOBAL_IDENTITY','GLOBAL_IDENTITY',connection_id,id::text,'identity',
        operation,'identity',provider,correlation_id,NULL,native_id,false,true,created_at,updated_at,1
       FROM identity_provider_events""")
    op.execute("""INSERT INTO usage_revisions
(id,event_id,reconciliation_key,cost_amount,currency,cost_quality,outcome,payload,created_at,updated_at,version)
       SELECT md5(id::text ||
md5(json_build_array(outcome,native_id,cost_quality,cost_amount,attempt)::text))::uuid,
id,md5(json_build_array(outcome,native_id,cost_quality,cost_amount,attempt)::text),NULL,NULL,'UNKNOWN',outcome,
json_build_object('native_subject',native_id,'attempt',attempt,'currency_availability','UNAVAILABLE',
          'source','identity_provider_events'),updated_at,updated_at,1 FROM identity_provider_events""")


def _bridge_accounting() -> None:
    op.get_bind().exec_driver_sql("""CREATE FUNCTION project_accounting_usage(
        tenant uuid, connection uuid, source_id uuid, source_kind text,
        provenance json, is_synthetic boolean, recorded_at timestamptz)
        RETURNS void LANGUAGE plpgsql AS $$
    DECLARE part record; item json; event_key uuid; native text; op_name text;
        cost json; latest record; event_row record; connection_provider text;
    BEGIN
        SELECT provider INTO STRICT connection_provider FROM provider_connections
            WHERE id=connection AND tenant_id=tenant;
        FOR part IN SELECT * FROM json_each(provenance) LOOP
            IF part.key NOT IN ('invoice','payments') THEN
                RAISE EXCEPTION 'unsupported accounting provenance component';
            END IF;
            item := part.value; cost := item->'cost'; native := item->>'native_operation_id';
            op_name := CASE WHEN part.key='invoice' THEN 'get_invoice' ELSE 'list_payments' END;
            IF native IS NULL OR native='' OR item->>'tenant_id' <> tenant::text
                OR item->>'provider_connection_id' <> connection::text
                OR item->>'provider' <> connection_provider
                OR cost->>'currency' IS NULL THEN
                RAISE EXCEPTION 'invalid accounting usage provenance';
            END IF;
            event_key := md5(tenant::text || ':' || connection::text || ':native:' || native ||
':accounting')::uuid;
            INSERT INTO provider_usage_events
                (id,tenant_id,scope_kind,scope_key,provider_connection_id,operation_key,component,
                 operation,provider_kind,provider,correlation_id,native_operation_id,native_subject,
                 synthetic,reconstructible,created_at,updated_at,version)
            VALUES (event_key,tenant,'TENANT',tenant::text,connection,'native:'||native,'accounting',
                op_name,'accounting',connection_provider,(item->>'correlation_id')::uuid,native,NULL,
                is_synthetic,true,recorded_at,recorded_at,1)
            ON CONFLICT (scope_key,provider_connection_id,operation_key,component) DO NOTHING;
            SELECT * INTO STRICT event_row FROM provider_usage_events WHERE scope_key=tenant::text
                AND provider_connection_id=connection AND operation_key='native:'||native AND
component='accounting';
            event_key := event_row.id;
            IF event_row.operation <> op_name OR event_row.synthetic <> is_synthetic THEN
                RAISE EXCEPTION 'accounting usage identity conflict';
            END IF;
            SELECT * INTO latest FROM usage_revisions WHERE event_id=event_key ORDER BY version DESC
LIMIT 1;
            IF FOUND THEN
                IF latest.currency IS DISTINCT FROM cost->>'currency'
                    OR latest.cost_quality IS DISTINCT FROM cost->>'quality'
                    OR latest.cost_amount IS DISTINCT FROM (cost->>'amount')::numeric THEN
                    RAISE EXCEPTION 'accounting usage charge conflict';
                END IF;
            ELSE
                INSERT INTO usage_revisions
(id,event_id,reconciliation_key,cost_amount,currency,cost_quality,outcome,payload,created_at,updated_at,version)
                VALUES (md5(event_key::text||':historical')::uuid,event_key,'historical',
                    (cost->>'amount')::numeric,cost->>'currency',cost->>'quality','VERIFIED',
json_build_object('source','accounting_provenance','provenance',item),recorded_at,recorded_at,1);
            END IF;
            INSERT INTO usage_sources
                (id,event_id,source_kind,source_id,component,created_at,updated_at,version)
            VALUES (md5(source_kind||source_id::text||part.key)::uuid,event_key,source_kind,source_id,
                part.key,recorded_at,recorded_at,1) ON CONFLICT DO NOTHING;
        END LOOP;
    END $$""")
    op.get_bind().exec_driver_sql("""CREATE FUNCTION bridge_accounting_usage()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE is_synthetic boolean;
    BEGIN
        IF TG_TABLE_NAME='accounting_events' THEN
            is_synthetic := (NEW.result->>'synthetic')::boolean;
        ELSE
            is_synthetic := (NEW.invoice_data->'stamp'->>'synthetic')::boolean;
        END IF;
        PERFORM project_accounting_usage(NEW.tenant_id,NEW.provider_connection_id,NEW.id,
            TG_TABLE_NAME,NEW.provenance,is_synthetic,NEW.created_at);
        RETURN NEW;
    END $$""")
    for table in ("accounting_events", "accounting_observations"):
        synthetic = (
            "(result->>'synthetic')::boolean"
            if table == "accounting_events"
            else "(invoice_data->'stamp'->>'synthetic')::boolean"
        )
        op.get_bind().exec_driver_sql(
            f"SELECT project_accounting_usage(tenant_id,provider_connection_id,id,'{table}',"
            f"provenance,{synthetic},created_at) FROM {table}"
        )
        op.get_bind().exec_driver_sql(
            f"CREATE TRIGGER accounting_usage AFTER INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bridge_accounting_usage()"
        )
