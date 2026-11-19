"""Retain observed thread/customer scope while importing genuine current customer corrections."""

from sqlalchemy import inspect

from alembic import op

revision = "r0029_invoice_customer_scope"
down_revision = "r0028_demo_reset"
branch_labels = None
depends_on = None


def original_thread_constraint() -> str:
    matches = [
        row["name"]
        for row in inspect(op.get_bind()).get_foreign_keys("communication_threads")
        if row["constrained_columns"] == ["tenant_id", "origin_invoice_id", "customer_id"]
    ]
    assert len(matches) == 1 and matches[0] is not None
    return str(matches[0])


def upgrade() -> None:
    op.execute("""CREATE TABLE invoice_customer_associations (
      tenant_id uuid NOT NULL, invoice_id uuid NOT NULL, customer_id uuid NOT NULL,
      provider_connection_id uuid NOT NULL, source_revision text NOT NULL,
      verified_at_capture boolean NOT NULL, capture_kind text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(tenant_id,invoice_id,customer_id),
      FOREIGN KEY(tenant_id,provider_connection_id,invoice_id)
        REFERENCES invoice_snapshots(tenant_id,provider_connection_id,id),
      FOREIGN KEY(tenant_id,provider_connection_id,customer_id)
        REFERENCES customers(tenant_id,provider_connection_id,id))""")
    # Existing composite FKs already establish these exact current scoped tuples.
    # Capture time is this migration; no earlier observed/verified history is invented.
    op.execute("""INSERT INTO invoice_customer_associations
      (tenant_id,invoice_id,customer_id,provider_connection_id,source_revision,
       verified_at_capture,capture_kind)
      SELECT tenant_id,id,customer_id,provider_connection_id,source_revision,verified,
        'migration_current_projection' FROM invoice_snapshots""")
    op.execute("""CREATE FUNCTION validate_invoice_customer_association() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.capture_kind <> 'current_projection' OR NEW.created_at <> transaction_timestamp()
          OR NOT EXISTS(SELECT 1 FROM invoice_snapshots i WHERE
            (i.tenant_id,i.provider_connection_id,i.id,i.customer_id)=
              (NEW.tenant_id,NEW.provider_connection_id,NEW.invoice_id,NEW.customer_id)
            AND i.source_revision=NEW.source_revision AND i.verified=NEW.verified_at_capture)
        THEN RAISE EXCEPTION 'association requires current snapshot' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER invoice_customer_association_valid
      BEFORE INSERT ON invoice_customer_associations
      FOR EACH ROW EXECUTE FUNCTION validate_invoice_customer_association()""")
    op.execute("""CREATE FUNCTION capture_invoice_customer_association() RETURNS trigger
      LANGUAGE plpgsql AS $$
      BEGIN
        INSERT INTO invoice_customer_associations
          (tenant_id,invoice_id,customer_id,provider_connection_id,source_revision,
           verified_at_capture,capture_kind)
        VALUES (NEW.tenant_id,NEW.id,NEW.customer_id,NEW.provider_connection_id,NEW.source_revision,
          NEW.verified,'current_projection')
        ON CONFLICT(tenant_id,invoice_id,customer_id) DO NOTHING;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER invoice_customer_capture
      AFTER INSERT OR UPDATE OF customer_id ON invoice_snapshots
      FOR EACH ROW EXECUTE FUNCTION capture_invoice_customer_association()""")
    op.execute("""CREATE FUNCTION protect_invoice_customer_association() RETURNS trigger
      LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'invoice customer association history is immutable'; END $$""")
    op.execute("""CREATE TRIGGER invoice_customer_association_immutable
      BEFORE UPDATE OR DELETE ON invoice_customer_associations
      FOR EACH ROW EXECUTE FUNCTION protect_invoice_customer_association()""")
    op.drop_constraint(original_thread_constraint(), "communication_threads", type_="foreignkey")
    op.create_foreign_key(
        "fk_thread_observed_invoice_customer",
        "communication_threads",
        "invoice_customer_associations",
        ["tenant_id", "origin_invoice_id", "customer_id"],
        ["tenant_id", "invoice_id", "customer_id"],
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(
      SELECT 1 FROM invoice_customer_associations a JOIN invoice_snapshots i
        ON (i.tenant_id,i.id)=(a.tenant_id,a.invoice_id) WHERE a.customer_id<>i.customer_id
    ) THEN RAISE EXCEPTION 'retained invoice customer history prevents lossy downgrade'; END IF; END $$""")
    op.drop_constraint("fk_thread_observed_invoice_customer", "communication_threads", type_="foreignkey")
    op.create_foreign_key(
        "communication_threads_tenant_id_origin_invoice_id_customer_fkey",
        "communication_threads",
        "invoice_snapshots",
        ["tenant_id", "origin_invoice_id", "customer_id"],
        ["tenant_id", "id", "customer_id"],
    )
    op.execute("DROP TRIGGER invoice_customer_capture ON invoice_snapshots")
    op.execute("DROP FUNCTION capture_invoice_customer_association()")
    op.drop_table("invoice_customer_associations")
    op.execute("DROP FUNCTION protect_invoice_customer_association()")
    op.execute("DROP FUNCTION validate_invoice_customer_association()")
