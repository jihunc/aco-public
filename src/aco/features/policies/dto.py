"""Policy configuration is strict data; it never supplies execution authority."""

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from iso4217 import Currency
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyRule(Command):
    id: UUID
    offset_days: int = Field(ge=-365, le=365)
    local_time: str = "09:00"
    channel: Literal["email", "voice"] = "email"
    template_id: str = "routine"

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
            raise ValueError("Use local HH:MM")
        return parsed.isoformat(timespec="minutes")


class Template(Command):
    id: str = Field(min_length=1, max_length=80)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


def email_channel() -> list[Literal["email", "voice"]]:
    return ["email"]


class CustomerOverride(Command):
    customer_id: UUID
    opt_out: bool = False
    offset_adjustment_days: int = Field(default=0, ge=-365, le=365)
    allowed_channels: list[Literal["email", "voice"]] = Field(default_factory=email_channel)


class PolicyDefinition(Command):
    rules: list[PolicyRule] = Field(min_length=1, max_length=30)
    templates: list[Template] = Field(
        default_factory=lambda: [
            Template(
                id="routine", subject="Invoice reminder", body="Please review the verified invoice balance."
            )
        ]
    )
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    opens_at: str = "09:00"
    closes_at: str = "17:00"
    cap_24h: int = Field(default=1, ge=0, le=1)
    cap_7d: int = Field(default=3, ge=0, le=3)
    overrides: list[CustomerOverride] = Field(default_factory=list)
    hold_disputes: Literal[True] = True
    hold_promises: Literal[True] = True
    ai_external_approval_required: Literal[True] = True
    autonomy: Literal["internal_only", "draft_and_review"] = "internal_only"
    approval_threshold_minor: str | None = Field(default=None, pattern=r"^\d+$")
    approval_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    voice_enabled: bool = False
    voice_after_days: int = Field(default=30, ge=0, le=365)
    voice_approval_required: Literal[True] = True

    @model_validator(mode="after")
    def coherent(self) -> "PolicyDefinition":
        if (
            not self.weekdays
            or len(set(self.weekdays)) != len(self.weekdays)
            or any(day not in range(5) for day in self.weekdays)
        ):
            raise ValueError("Select distinct Monday–Friday days")
        opens, closes = time.fromisoformat(self.opens_at), time.fromisoformat(self.closes_at)
        if opens.tzinfo or closes.tzinfo or not time(9) <= opens < closes <= time(17):
            raise ValueError("Windows must be within 09:00–17:00")
        if any(t.second or t.microsecond for t in (opens, closes)):
            raise ValueError("Use minute precision")
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Rule IDs must be distinct")
        templates = {template.id for template in self.templates}
        if len(templates) != len(self.templates) or any(r.template_id not in templates for r in self.rules):
            raise ValueError("Each rule requires one existing template")
        if len({item.customer_id for item in self.overrides}) != len(self.overrides):
            raise ValueError("Duplicate customer override")
        if bool(self.approval_currency) != (self.approval_threshold_minor is not None):
            raise ValueError("Approval threshold requires currency and amount")
        if self.approval_currency is not None:
            Currency(self.approval_currency)
        return self


class PolicySave(Command):
    name: str = Field(min_length=1, max_length=120)
    definition: PolicyDefinition
    expected_version: int = Field(ge=0)
    activate: bool = False
    native_reminders_disabled: bool = False
    configuration_reviewed: bool = False

    @field_validator("name")
    @classmethod
    def meaningful(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Name is required")
        return value.strip()


class PolicyVersion(Command):
    id: UUID
    policy_id: UUID
    name: str
    version: int
    definition: dict[str, object]
    active: bool
    archived: bool
    effective_at: datetime
    state: str
    synthetic: bool


class PolicyArchive(Command):
    expected_version: int = Field(ge=1)
    replacement_policy_id: UUID | None = None
    replacement_expected_version: int | None = Field(default=None, ge=1)
    suspend_automation: bool = False
    native_reminders_disabled: bool = False
    configuration_reviewed: bool = False


class PolicyArchiveResult(Command):
    policy_id: UUID
    archived: bool
    automation_enabled: bool
    cancelled_actions: int
    replacement_policy_id: UUID | None


class RecipientSave(Command):
    expected_version: int = Field(ge=0)
    expected_contact_revision: str = Field(min_length=1)
    expected_destination: str | None
    expected_tenant_timezone: str | None = None
    timezone: str | None = None
    timezone_verified: bool = False
    owner_confirmed_tenant_timezone: bool = False
    destination_verified: bool = False
    allowed_channels: list[Literal["email", "voice"]] = Field(default_factory=email_channel)
    opt_out: bool = False
    evidence: str = Field(min_length=1, max_length=1000)

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str | None) -> str | None:
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Choose an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def coherent(self) -> "RecipientSave":
        if self.owner_confirmed_tenant_timezone:
            if not self.expected_tenant_timezone:
                raise ValueError("The reviewed workspace timezone is required")
        elif self.expected_tenant_timezone is not None:
            raise ValueError("Workspace timezone binding requires fallback confirmation")
        if self.timezone_verified and not self.timezone:
            raise ValueError("Verified timezone is required")
        if self.timezone and self.owner_confirmed_tenant_timezone:
            raise ValueError("Choose one timezone evidence source")
        return self


class RecipientView(Command):
    contact_id: UUID
    customer_id: UUID
    destination: str | None
    source_revision: str
    reviewed_destination: str | None
    reviewed_contact_revision: str | None
    review_matches_source: bool
    tenant_timezone: str
    confirmed_tenant_timezone: str | None
    version: int
    timezone: str | None
    timezone_verified: bool
    owner_confirmed_tenant_timezone: bool
    destination_verified: bool
    allowed_channels: list[str]
    opt_out: bool
    evidence: str | None


class GuardDecision(Command):
    allowed: bool
    reason: str | None = None
    reasons: list[str] = Field(default_factory=list)
    count_24h: int = 0
    count_7d: int = 0


class OutboundProposal(Command):
    action_id: UUID
    attempt_id: UUID
    read_id: UUID | None = None
    channel: Literal["email", "voice"] = "email"
    destination: str
    approval_id: UUID | None = None


class DispatchTicket(Command):
    id: UUID
    issued_at: datetime
    action_id: UUID
    attempt_id: UUID


class NextAction(Command):
    action_id: UUID
    occurrence_id: UUID
    rule_id: UUID
    due_at: datetime
    policy_version_id: UUID


class PreviewItem(Command):
    invoice_id: UUID
    invoice_number: str
    customer_id: UUID
    due_date: date
    rule_id: UUID
    channel: str
    destination: str | None
    recipient_timezone: str | None
    next_at: datetime | None
    holds: list[str] | None
    reasons: list[str]
    source_health: str


class PolicyPreview(Command):
    policy_version_id: UUID
    as_of: datetime
    native_reminder_warning: bool
    outbound_available: Literal[False] = False
    items: list[PreviewItem]


class PauseCommand(Command):
    scope: Literal["tenant", "global"] = "tenant"
    paused: bool


class PauseState(Command):
    scope: str
    paused: bool
    automation_enabled: bool
    version: int


class PolicyRecompute(Command):
    expected_version: int = Field(ge=1)
