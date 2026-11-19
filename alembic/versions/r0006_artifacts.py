"""Private originals and durable bounded synthetic retention; frozen migration DDL."""

from sqlalchemy import text

from alembic import op

revision = "r0006_artifacts"
down_revision = "r0005_policies"
branch_labels = None
depends_on = None

DOMAIN_DDL = (
    (
        "\nCREATE TABLE artifact_retention_policies (\n\ttenant_id UUID NOT NULL, \n\tpol"
        "icy_key VARCHAR NOT NULL, \n\tsynthetic BOOLEAN NOT NULL, \n\tretention_days IN"
        "TEGER NOT NULL, \n\tauthority VARCHAR NOT NULL, \n\tid UUID NOT NULL, \n\tcreated"
        "_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZON"
        "E NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenan"
        "t_id, id), \n\tUNIQUE (tenant_id, policy_key), \n\tCHECK (retention_days = 30 A"
        "ND synthetic), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id)\n)\n\n"
    ),
    ("CREATE INDEX ix_artifact_retention_policies_tenant_id ON artifact_retention_policies (tenant_id)"),
    (
        "\nCREATE TABLE artifacts (\n\ttenant_id UUID NOT NULL, \n\tprovider_connection_i"
        "d UUID NOT NULL, \n\tretention_policy_id UUID NOT NULL, \n\tsource_kind VARCHAR"
        " NOT NULL, \n\tsource_reference VARCHAR NOT NULL, \n\tsha256 VARCHAR NOT NULL, "
        "\n\tcontent_type VARCHAR NOT NULL, \n\tsize_bytes INTEGER NOT NULL, \n\tcapture_c"
        "lass VARCHAR NOT NULL, \n\tcaptured_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\te"
        "xpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstorage_state VARCHAR NOT NU"
        "LL, \n\tobject_key VARCHAR NOT NULL, \n\tobject_version VARCHAR, \n\tencryption V"
        "ARCHAR, \n\tfailure_code VARCHAR, \n\tlegal_hold_state VARCHAR NOT NULL, \n\tcons"
        "ent_revoked BOOLEAN NOT NULL, \n\trevoked_at TIMESTAMP WITH TIME ZONE, \n\tdele"
        "ted_at TIMESTAMP WITH TIME ZONE, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP"
        " WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n"
        "\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, id), \n\t"
        "UNIQUE (tenant_id, source_kind, source_reference), \n\tFOREIGN KEY(tenant_id,"
        " provider_connection_id) REFERENCES provider_connections (tenant_id, id), \n"
        "\tFOREIGN KEY(tenant_id, retention_policy_id) REFERENCES artifact_retention_"
        "policies (tenant_id, id), \n\tCHECK (storage_state IN ('pending','stored','fa"
        "iled','quarantined','purge_pending','deleted','blocked')), \n\tCHECK (size_by"
        "tes >= 0), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id)\n)\n\n"
    ),
    ("CREATE INDEX ix_artifacts_tenant_id ON artifacts (tenant_id)"),
    (
        "\nCREATE TABLE artifact_purge_requests (\n\ttenant_id UUID NOT NULL, \n\tartifac"
        "t_id UUID NOT NULL, \n\tdue_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstatus VA"
        "RCHAR NOT NULL, \n\treason VARCHAR NOT NULL, \n\tfailure_code VARCHAR, \n\tconfir"
        "med_at TIMESTAMP WITH TIME ZONE, \n\tprovider_receipt VARCHAR, \n\tid UUID NOT "
        "NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAM"
        "P WITH TIME ZONE NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id), "
        "\n\tUNIQUE (artifact_id), \n\tFOREIGN KEY(tenant_id, artifact_id) REFERENCES ar"
        "tifacts (tenant_id, id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id)\n)"
        "\n\n"
    ),
    ("CREATE INDEX ix_artifact_purge_requests_tenant_id ON artifact_purge_requests (tenant_id)"),
)


def upgrade() -> None:
    for statement in DOMAIN_DDL:
        op.execute(statement)


def downgrade() -> None:
    for name in ("artifact_purge_requests", "artifacts", "artifact_retention_policies"):
        if op.get_bind().scalar(text(f"SELECT EXISTS(SELECT 1 FROM {name})")):
            raise RuntimeError("cannot downgrade: artifact evidence would be lost")
    for name in ("artifact_purge_requests", "artifacts", "artifact_retention_policies"):
        op.drop_table(name)
