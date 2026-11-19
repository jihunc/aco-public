"""Bounded human business export; fixed projections, never a database/private-object dump."""

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import (
    AccountingEvent,
    Customer,
    InvoiceSnapshot,
    VerifiedPaymentAllocation,
)
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.artifacts.consent import voice_read_allowed
from aco.features.artifacts.models import Artifact
from aco.features.cases.models import CaseEvidence, CaseHistory, CaseInvoiceLink, CollectionCase
from aco.features.communications.models import Communication, CommunicationInvoiceLink
from aco.features.communications.queries import CommunicationQueries
from aco.features.identity.models import AuditEvent, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.dto import PolicyDefinition
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion
from aco.features.promises.models import PaymentPromise, PromiseEvent, PromiseInstallment, PromiseInvoiceLink
from aco.features.review.models import ApprovalDecision, ApprovalRequest, CollectionTask
from aco.features.review.queue import task_view
from aco.features.usage.models import ProviderUsageEvent, UsageRevision
from aco.features.voice.models import VoiceBusinessEvidence, VoiceEventRecord, VoiceInvoiceLink, VoiceSession

Resource = Literal[
    "customers",
    "invoices",
    "payments",
    "accounting_events",
    "tasks",
    "cases",
    "promises",
    "actions",
    "communications",
    "artifacts",
    "agent_runs",
    "approvals",
    "voice_sessions",
    "usage",
    "policies",
    "audit_history",
]
MAX_RECORDS = 5000
MAX_BYTES = 10 * 1024 * 1024


class ExportCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    resources: list[Resource] = Field(min_length=1, max_length=16)
    created_from: AwareDatetime
    created_before: AwareDatetime

    @model_validator(mode="after")
    def bounded_window(self) -> "ExportCommand":
        if len(set(self.resources)) != len(self.resources):
            raise ValueError("Choose each resource once")
        if not timedelta(0) < self.created_before - self.created_from <= timedelta(days=366):
            raise ValueError("Choose a positive creation window of at most 366 days")
        return self


# Only these fields can leave the database. No ORM __dict__, arbitrary table, generic JSON
# payload, credential, grant, signed URL, private key, transcript or source body is serialized.
PROJECTIONS: dict[str, tuple[type[Any], tuple[str, ...]]] = {
    "customers": (
        Customer,
        (
            "name",
            "external_customer_id",
            "provider_connection_id",
            "source_revision",
            "observed_at",
            "synthetic",
        ),
    ),
    "invoices": (
        InvoiceSnapshot,
        (
            "number",
            "customer_id",
            "provider_connection_id",
            "external_invoice_id",
            "issue_date",
            "due_date",
            "original_minor",
            "balance_minor",
            "currency",
            "exponent",
            "financial_status",
            "source_status",
            "source_revision",
            "observed_at",
            "last_read_at",
            "verified",
            "synthetic",
        ),
    ),
    "payments": (
        VerifiedPaymentAllocation,
        (
            "invoice_id",
            "provider_connection_id",
            "external_payment_id",
            "external_allocation_id",
            "kind",
            "minor_units",
            "currency",
            "exponent",
            "parent_id",
            "source_revision",
            "effective_at",
            "observed_at",
            "synthetic",
        ),
    ),
    "accounting_events": (
        AccountingEvent,
        ("invoice_id", "provider_connection_id", "external_event_id", "origin", "source_revision"),
    ),
    "tasks": (CollectionTask, ()),
    "cases": (
        CollectionCase,
        (
            "customer_id",
            "provider_connection_id",
            "issue_type",
            "status",
            "version",
            "review_required",
            "task_id",
            "synthetic",
            "resolved_at",
            "closed_at",
        ),
    ),
    "promises": (
        PaymentPromise,
        (
            "customer_id",
            "provider_connection_id",
            "status",
            "version",
            "source_communication_id",
            "source_voice_evidence_id",
            "confirmation_communication_id",
            "confirmation_voice_evidence_id",
            "source_at",
            "source_state",
            "synthetic",
            "due_date",
            "timezone",
            "cutoff_at",
            "confirmed_at",
            "replaces_promise_id",
            "superseded_by_id",
            "review_task_id",
        ),
    ),
    "actions": (
        ScheduledAction,
        ("invoice_id", "kind", "status", "version", "attempts", "due_at", "last_error", "automatic_resend"),
    ),
    "communications": (
        Communication,
        (
            "thread_id",
            "origin_invoice_id",
            "provider_connection_id",
            "native_message_id",
            "direction",
            "channel",
            "kind",
            "synthetic",
            "event_at",
            "delivery_state",
            "source_state",
        ),
    ),
    "artifacts": (
        Artifact,
        (
            "sha256",
            "content_type",
            "size_bytes",
            "storage_state",
            "captured_at",
            "expires_at",
            "retention_policy_id",
            "capture_class",
        ),
    ),
    "agent_runs": (
        AgentRun,
        (
            "initiator_id",
            "status",
            "authenticity",
            "provider_connection_id",
            "model_id",
            "prompt_version",
            "schema_version",
            "model_state",
            "error_code",
            "provider_attempted",
            "requests",
            "input_tokens",
            "output_tokens",
            "source_communication_id",
        ),
    ),
    "approvals": (
        ApprovalRequest,
        (
            "action_id",
            "action_version",
            "initiator_id",
            "proposer_id",
            "status",
            "granted_at",
            "expires_at",
            "consumed_at",
        ),
    ),
    "voice_sessions": (
        VoiceSession,
        (
            "connection_id",
            "initiator_id",
            "state",
            "provider_call_id",
            "approval_id",
            "usage_attempt_id",
            "expires_at",
        ),
    ),
    "usage": (
        ProviderUsageEvent,
        (
            "provider_connection_id",
            "component",
            "operation",
            "provider_kind",
            "provider",
            "synthetic",
            "correlation_id",
        ),
    ),
    "policies": (CollectionPolicy, ("name", "active", "archived_at", "version")),
    "audit_history": (AuditEvent, ("actor_id", "correlation_id", "action", "target_id")),
}
# Related rows are also tenant-qualified (usage revisions through their selected tenant event),
# counted against the same hard limit and represented by fixed non-sensitive history fields.
RELATIONS: dict[str, tuple[tuple[str, type[Any], str, tuple[str, ...]], ...]] = {
    "cases": (
        ("evidence", CaseEvidence, "case_id", ("communication_id", "voice_evidence_id", "kind")),
        ("invoice_links", CaseInvoiceLink, "case_id", ("invoice_id", "issue_state", "impact", "resolved_at")),
        ("history", CaseHistory, "case_id", ("event", "actor_id", "substantive_human")),
    ),
    "promises": (
        ("invoice_links", PromiseInvoiceLink, "promise_id", ("invoice_id",)),
        ("installments", PromiseInstallment, "promise_id", ("due_date", "cutoff_at", "status")),
        ("history", PromiseEvent, "promise_id", ("event", "actor_id")),
    ),
    "actions": (
        (
            "executions",
            ActionExecution,
            "action_id",
            ("attempt", "status", "started_at", "finished_at", "retry_delay_seconds"),
        ),
    ),
    "communications": (
        (
            "invoice_links",
            CommunicationInvoiceLink,
            "communication_id",
            ("invoice_id", "provenance", "classification"),
        ),
    ),
    "agent_runs": (
        (
            "steps",
            AgentStep,
            "run_id",
            ("step_sequence", "kind", "tool_name", "status", "invoice_id", "created_at", "updated_at"),
        ),
    ),
    "approvals": (("decisions", ApprovalDecision, "request_id", ("approver_id", "decision")),),
    "voice_sessions": (
        (
            "business_evidence",
            VoiceBusinessEvidence,
            "session_id",
            (
                "invocation_id",
                "source_event_id",
                "action_id",
                "provider_connection_id",
                "accounting_connection_id",
                "customer_id",
                "provider_call_id",
                "native_invocation_id",
                "fact_kind",
                "facts_hash",
                "synthetic",
                "evidence_mode",
                "source_state",
            ),
        ),
        ("invoice_links", VoiceInvoiceLink, "session_id", ("invoice_id", "source_revision")),
        ("events", VoiceEventRecord, "session_id", ("kind",)),
    ),
    "usage": (
        ("cost_history", UsageRevision, "event_id", ("cost_amount", "currency", "cost_quality", "outcome")),
    ),
    "policies": (("versions", CollectionPolicyVersion, "policy_id", ("version", "state", "synthetic")),),
}
EXCLUSIONS = [
    "Raw original bytes, communication bodies/headers, source excerpts, transcripts and recordings; "
    "use separately current-authorized private downloads.",
    "Credentials, sessions, grants, operation capabilities, internal storage keys, signed URLs "
    "and arbitrary provider/audit payloads.",
    "Unselected resources and primary records created outside the requested window. "
    "Current projections are not historical as-of reconstruction.",
]


class LegacyPolicyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reminder_offsets_days: list[int]
    customer_24h_cap: int
    customer_7d_cap: int
    timezone: str
    approval_required: bool
    synthetic_variant: str


def policy_definition(value: object) -> dict[str, object]:
    try:
        return {"format": "current", "value": PolicyDefinition.model_validate(value).model_dump(mode="json")}
    except ValidationError:
        try:
            return {
                "format": "legacy_synthetic",
                "value": LegacyPolicyDefinition.model_validate(value).model_dump(mode="json"),
            }
        except ValidationError:
            raise HTTPException(409, "EXPORT_POLICY_FORMAT_UNAVAILABLE") from None


def encode(value: object) -> object:
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("Unsupported export value")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode, allow_nan=False).encode()


def fields(row: Any, names: tuple[str, ...]) -> dict[str, object]:
    result = {key: getattr(row, key) for key in ("id", "created_at", *names)}
    for key in ("original_minor", "balance_minor", "minor_units"):
        if key in result:
            result[key] = str(result[key])
    return result


class ExportService:
    def __init__(self, db: Session):
        self.db = db
        self.record_count = 0

    def count(self, size: int) -> None:
        self.record_count += size
        if self.record_count > MAX_RECORDS:
            raise HTTPException(413, "EXPORT_ROW_LIMIT_NARROW_SELECTION_OR_WINDOW")

    def download(self, ctx: TenantContext, command: ExportCommand) -> tuple[bytes, str]:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        IdentityService(self.db).authorize(ctx, Permission.EXPORT)
        self.record_count = 0
        as_of = now()
        records: dict[str, list[dict[str, object]]] = {}
        for resource in command.resources:
            model, names = PROJECTIONS[resource]
            rows = self.db.scalars(
                select(model)
                .where(
                    model.tenant_id == ctx.tenant_id,
                    model.created_at >= command.created_from,
                    model.created_at < command.created_before,
                    model.created_at <= as_of,
                )
                .order_by(model.created_at, model.id)
                .limit(MAX_RECORDS + 1)
                .execution_options(populate_existing=True)
            ).all()
            self.count(len(rows))
            records[resource] = []
            for row in rows:
                data = fields(row, names)
                if resource == "tasks":
                    data.update(task_view(row).model_dump(mode="json"))
                elif resource == "communications":
                    # Same current read authority as the communication UI; metadata only,
                    # so expired/quarantined content cannot leak through this projection.
                    CommunicationQueries(self.db, ctx)
                elif resource == "artifacts":
                    data["privacy_read_allowed"] = bool(
                        row.storage_state == "stored"
                        and row.revoked_at is None
                        and row.expires_at > as_of
                        and not row.consent_revoked
                        and row.legal_hold_state == "none"
                        and voice_read_allowed(self.db, row)
                    )
                elif resource == "promises":
                    data["amount"] = self.money(row.amount)
                for label, child, parent_key, child_names in RELATIONS.get(resource, ()):
                    query = select(child).where(
                        getattr(child, parent_key) == row.id, child.created_at <= as_of
                    )
                    if child is not UsageRevision:
                        query = query.where(child.tenant_id == ctx.tenant_id)
                    children = self.db.scalars(
                        query.order_by(child.created_at, child.id).limit(MAX_RECORDS + 1)
                    ).all()
                    self.count(len(children))
                    related = []
                    for item in children:
                        value = fields(item, child_names)
                        if child is CollectionPolicyVersion:
                            value["definition"] = policy_definition(item.definition)
                        if child is PromiseInstallment:
                            value["amount"] = self.money(item.amount)
                        related.append(value)
                    data[label] = related
                records[resource].append(data)
        counts = {key: len(rows) for key, rows in records.items()}
        body = canonical(
            {
                "schema_version": "aco-business-export-v1",
                "tenant_id": ctx.tenant_id,
                "as_of": as_of,
                "selection": command.model_dump(mode="json"),
                "window_field": "primary_record.created_at",
                "related_history": "Selected primary records, through as_of; counted against the same limit",
                "counts": counts,
                "total_records_including_history": self.record_count,
                "data_sha256": hashlib.sha256(canonical(records)).hexdigest(),
                "records": records,
                "raw_originals_included": False,
                "exclusions": EXCLUSIONS,
                "limits": {"records_including_history": MAX_RECORDS, "bytes": MAX_BYTES},
            }
        )
        if len(body) > MAX_BYTES:
            raise HTTPException(413, "EXPORT_BYTE_LIMIT_NARROW_SELECTION_OR_WINDOW")
        IdentityService(self.db).authorize(ctx, Permission.EXPORT)
        digest = hashlib.sha256(body).hexdigest()
        self.db.add(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.correlation_id,
                action="tenant.business_export",
                target_id=uuid4(),
                details={
                    "resources": ",".join(command.resources),
                    "created_from": command.created_from.isoformat(),
                    "created_before": command.created_before.isoformat(),
                    "as_of": as_of.isoformat(),
                    "counts": json.dumps(counts, sort_keys=True),
                    "source_sha256": digest,
                    "bytes": str(len(body)),
                    "raw_originals_included": False,
                },
            )
        )
        self.db.flush()
        return body, digest

    @staticmethod
    def money(value: object) -> dict[str, object] | None:
        if value is None:
            return None
        from aco.features.promises.dto import PromiseMoneyJSON

        return PromiseMoneyJSON.model_validate(value).model_dump(mode="json")
