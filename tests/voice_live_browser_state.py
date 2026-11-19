"""Explicit synthetic LIVE-code browser fixture; never a real provider grant/consent.

Runs only in the contained local profile and a disposable non-public schema. Actual
OIDC memberships, canonical source imports, policy and grant services remain authority.
"""

import argparse
import hashlib
import hmac
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from approval_browser_state import local_issuer, require_local_profile
from cryptography.fernet import Fernet
from pydantic import Field, SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.local_runtime import local_settings
from aco.core.models import register_models
from aco.core.settings import Settings
from aco.core.types import StrictModel
from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.identity.models import (
    Actor,
    LoginSession,
    Membership,
    ProviderConnection,
    Tenant,
    User,
    now,
)
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.dto import PolicyDefinition, PolicyRule, PolicySave, RecipientSave
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion, RecipientControl
from aco.features.policies.service import PolicyService
from aco.features.voice.live import VoiceLiveConfig, VoiceLiveGrant, install_voice_live_grant
from aco.features.voice.models import VoiceSession
from aco.features.voice.runtime import VoiceRuntime
from aco.integrations.ports.identity import AuthenticatedSubject
from aco.integrations.provider_smoke.source import current_source_commit
from aco.integrations.vapi.application import read_private
from aco.integrations.vapi.settings import VapiBinding

PROFILE = "voice-live-code"
PREFIX = "Voice live-code "
MANIFEST = Path(__file__).resolve().parents[1] / ".tooling" / "voice-live-browser-fixture.json"
RUNTIME_RECEIPT = MANIFEST.with_name("voice-live-browser-runtime.json")
SIGNING = "explicit-synthetic-live-browser-signing-not-real"
CAPABILITY = "explicit-synthetic-live-browser-capability-not-real"


class BrowserRuntimeReceipt(StrictModel):
    schema_name: str = Field(pattern=r"^voice_live_browser_[a-f0-9]{32}$")
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    pid: int = Field(ge=1)


def fixture_settings(settings: Settings) -> Settings:
    require_profile(settings)
    receipt = BrowserRuntimeReceipt.model_validate_json(read_private(RUNTIME_RECEIPT))
    if receipt.source_commit != current_source_commit():
        raise ValueError("Current owned browser runtime receipt required")
    os.kill(receipt.pid, 0)
    if settings.database_url is None:
        raise ValueError("Owned local database required")
    url = make_url(settings.database_url.get_secret_value()).update_query_dict(
        {"options": f"-csearch_path={receipt.schema_name}"}
    )
    return Settings.model_validate(
        settings.model_dump() | {"database_url": SecretStr(url.render_as_string(hide_password=False))}
    )


class BrowserFixture(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    grant_id: UUID
    assistant_id: UUID
    credential_id: UUID
    native_id: UUID
    invoice_id: UUID
    source_commit: str
    participant_email: str
    join_encryption_key: str
    profile: dict[str, object]
    credential: dict[str, object]

    def binding(self) -> VapiBinding:
        return VapiBinding(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            assistant_id=self.assistant_id,
            server_credential_id=self.credential_id,
            credential_revision="synthetic-live-browser-only",
            api_key=SecretStr("synthetic-no-provider-key"),
            webhook_secret=SecretStr(SIGNING),
            control_api_key=SecretStr("synthetic-no-control-key"),
            control_hosts=("control.vapi.ai",),
            evidence_mode="LIVE",
        )


def require_profile(settings: Settings) -> None:
    if os.getenv("ACO_TEST_PROFILE") != PROFILE:
        raise ValueError("Explicit contained LIVE-code mock-boundary profile required")
    require_local_profile("mock", settings)
    if settings.database_url is None:
        raise ValueError("Explicit local database required")
    if any(
        os.getenv(key)
        for key in (
            "ACO_AGENT_LIVE_CONFIG_PATH",
            "ACO_AGENT_LIVE_GRANT_ID",
            "ACO_EMAIL_LIVE_CONFIG_PATH",
            "ACO_VOICE_LIVE_CONFIG_PATH",
        )
    ):
        raise ValueError("Real provider selectors must remain absent")


def read_fixture() -> BrowserFixture:
    return BrowserFixture.model_validate_json(read_private(MANIFEST))


def owner_context(store: Session, workspace: UUID, settings: Settings) -> TenantContext:
    if (
        not local_issuer(settings.identity_issuer)
        or store.scalar(text("select current_schema()")) == "public"
    ):
        raise ValueError("Actual local OIDC and disposable non-public schema required")
    tenant = store.get(Tenant, workspace)
    if tenant is None:
        raise ValueError("Fresh actual workspace required")
    try:
        nonce = UUID(tenant.name.removeprefix(PREFIX))
    except ValueError:
        raise ValueError("Unique synthetic browser workspace required") from None
    if (
        not tenant.name.startswith(PREFIX)
        or nonce.version != 4
        or not tenant.is_demo
        or tenant.automation_enabled
        or not tenant.outbound_paused
        or not timedelta(0) <= now() - tenant.created_at <= timedelta(minutes=15)
    ):
        raise ValueError("Fresh paused synthetic browser workspace required")
    users = list(
        store.scalars(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.tenant_id == workspace,
                Membership.role == "owner",
                Membership.active.is_(True),
                User.issuer == settings.identity_issuer,
            )
        )
    )
    if len(users) != 1:
        raise ValueError("One actual current OIDC Owner required")
    user = users[0]
    login = store.scalar(
        select(LoginSession)
        .where(
            LoginSession.user_id == user.id, LoginSession.revoked.is_(False), LoginSession.expires_at > now()
        )
        .order_by(LoginSession.created_at.desc())
        .limit(1)
    )
    actor = store.get(Actor, user.id)
    if login is None or actor is None or actor.kind != "human" or not actor.active:
        raise ValueError("Current genuine browser login required")
    ctx = IdentityService(store).resolve_context(
        AuthenticatedSubject(
            user_id=user.id,
            issuer=user.issuer,
            subject=user.subject,
            session_id=login.id,
            authenticated_at=login.created_at,
            correlation_id=uuid4(),
        ),
        workspace,
    )
    IdentityService(store).authorize(ctx, Permission.SETTINGS)
    return ctx


def install_fixture(store: Session, workspace: UUID, settings: Settings) -> BrowserFixture:
    ctx = owner_context(store, workspace, settings)
    if store.scalar(select(func.count()).select_from(VoiceLiveGrant)):
        raise ValueError("Fixture requires a disposable schema without an existing Voice grant")
    participants = list(
        store.scalars(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.tenant_id == workspace,
                Membership.role == "finance_manager",
                Membership.active.is_(True),
                User.issuer == settings.identity_issuer,
                User.email_verified.is_(True),
                User.id != ctx.actor_id,
            )
        )
    )
    if len(participants) != 1:
        raise ValueError("One actual independently signed-in Finance Manager required")
    participant = participants[0]
    if (
        store.scalar(
            select(LoginSession.id)
            .where(
                LoginSession.user_id == participant.id,
                LoginSession.revoked.is_(False),
                LoginSession.expires_at > now(),
            )
            .limit(1)
        )
        is None
    ):
        raise ValueError("Actual participant browser login required")
    invoice = store.scalar(
        select(InvoiceSnapshot).where(
            InvoiceSnapshot.tenant_id == workspace,
            InvoiceSnapshot.number == "INV-101",
            InvoiceSnapshot.synthetic.is_(True),
        )
    )
    old = store.scalar(
        select(CollectionPolicyVersion)
        .where(CollectionPolicyVersion.tenant_id == workspace)
        .order_by(CollectionPolicyVersion.created_at.desc())
        .limit(1)
    )
    policy = store.get(CollectionPolicy, old.policy_id) if old else None
    if invoice is None or old is None or policy is None or now().weekday() >= 5:
        raise ValueError("Genuine seeded source and weekday policy fixture required")
    saved = PolicyService(store).save_policy(
        ctx,
        PolicySave(
            name=policy.name,
            definition=PolicyDefinition(
                rules=[PolicyRule(id=uuid4(), offset_days=0, channel="voice")],
                voice_enabled=True,
                voice_after_days=0,
            ),
            expected_version=policy.version,
            activate=True,
            native_reminders_disabled=True,
            configuration_reviewed=True,
        ),
        policy.version,
        policy.id,
    )
    contact = store.scalar(
        select(CustomerContact).where(
            CustomerContact.tenant_id == workspace, CustomerContact.customer_id == invoice.customer_id
        )
    )
    if contact is None:
        raise ValueError("Actual imported customer contact required")
    current = store.scalar(select(RecipientControl).where(RecipientControl.contact_id == contact.id))
    offset = 12 - now().hour
    zone = "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
    PolicyService(store).save_recipient(
        ctx,
        contact.id,
        RecipientSave(
            expected_version=current.version if current else 0,
            expected_contact_revision=contact.source_revision,
            expected_destination=contact.email,
            timezone=zone,
            timezone_verified=True,
            destination_verified=True,
            allowed_channels=["voice"],
            evidence="Explicit synthetic browser fixture recipient and local business-hour review",
        ),
    )
    PolicyService(store).set_outbound_pause(ctx, "tenant", False)
    connection = ProviderConnection(
        tenant_id=workspace,
        provider="vapi",
        provider_type="voice",
        status="NOT_CONFIGURED",
        external_account_id="synthetic-live-browser-account",
        encrypted_credentials_ref="synthetic-live-browser-only",
    )
    store.add(connection)
    store.flush()
    fixture = BrowserFixture(
        tenant_id=workspace,
        connection_id=connection.id,
        grant_id=uuid4(),
        assistant_id=uuid4(),
        credential_id=uuid4(),
        native_id=uuid4(),
        invoice_id=invoice.id,
        source_commit=current_source_commit(),
        participant_email=participant.email,
        join_encryption_key=Fernet.generate_key().decode(),
        profile={},
        credential={},
    )
    binding = fixture.binding()
    fixture.profile.update(
        {
            "id": str(binding.assistant_id),
            "orgId": connection.external_account_id,
            "server": {
                "url": "https://voice.example.test/api/voice/callback",
                "credentialId": str(binding.server_credential_id),
            },
            "artifactPlan": {
                "recordingEnabled": False,
                "loggingEnabled": False,
                "videoRecordingEnabled": False,
                "transcriptPlan": {"enabled": False},
            },
            "maxDurationSeconds": 300,
            "monitorPlan": {"controlAuthenticationEnabled": True},
        }
    )
    fixture.credential.update(
        {
            "id": str(binding.server_credential_id),
            "orgId": connection.external_account_id,
            "provider": "custom-credential",
            "authenticationPlan": {
                "type": "hmac",
                "algorithm": "sha256",
                "secretKey": SIGNING,
                "signatureHeader": "x-signature",
                "timestampHeader": "x-timestamp",
                "payloadFormat": "{timestamp}.{body}",
                "signatureEncoding": "hex",
                "includeTimestamp": True,
            },
        }
    )
    assert connection.external_account_id is not None
    config = VoiceLiveConfig(
        authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
        source_commit=fixture.source_commit,
        account_id=connection.external_account_id,
        assistant_id=binding.assistant_id,
        participant_email=participant.email,
        participant_timezone=zone,
        participant_approval_ref="Explicit synthetic fixture, no real participant/microphone consent",
        no_capture_approval_ref="Explicit mocked HTTP privacy profile, no real provider verification",
        callback_url="https://voice.example.test/api/voice/callback",
        server_credential_id=fixture.credential_id,
        assistant_profile_sha256=hashlib.sha256(
            json.dumps(fixture.profile, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        binding_sha256=VoiceRuntime.binding_hash(binding),
        policy_version_id=saved.id,
        invoice_ids=[invoice.id],
        expires_at=now() + timedelta(minutes=15),
        max_calls=1,
        max_call_seconds=300,
        max_total_seconds=300,
        max_requests=6,
        max_usd=Decimal("1"),
        all_component_usd_per_second=Decimal("0.001"),
        api_request_usd_ceiling=Decimal("0.01"),
        price_source_ref="Explicit synthetic bounded test rate fixture",
        price_source_at=now(),
    )
    grant = install_voice_live_grant(store, ctx, connection.id, config)
    store.commit()
    return fixture.model_copy(update={"grant_id": grant.id})


def emit_terminal(store: Session, fixture: BrowserFixture) -> None:
    row = store.scalar(
        select(VoiceSession).where(
            VoiceSession.tenant_id == fixture.tenant_id, VoiceSession.live_grant_id == fixture.grant_id
        )
    )
    if row is None or row.provider_call_id != str(fixture.native_id):
        raise ValueError("Actual accepted native fixture call required")
    from itsdangerous import URLSafeSerializer

    token = URLSafeSerializer(CAPABILITY, salt="aco-voice-call-v1").dumps(
        {
            "tenant_id": str(row.tenant_id),
            "session_id": str(row.id),
            "expires_at": int(row.expires_at.timestamp()),
            "binding_hash": row.binding_hash,
        }
    )
    body = json.dumps(
        {
            "message": {
                "type": "status-update",
                "status": "ended",
                "call": {
                    "id": row.provider_call_id,
                    "assistantId": str(fixture.assistant_id),
                    "assistantOverrides": {"variableValues": {"acoCallCapability": token}},
                },
            }
        }
    ).encode()
    store.rollback()
    stamp = str(int(now().timestamp()))
    signature = hmac.new(SIGNING.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    with httpx.Client(timeout=15, trust_env=False) as client:
        for _ in range(2):
            response = client.post(
                "http://127.0.0.1:58000/api/voice/callback",
                content=body,
                headers={"x-timestamp": stamp, "x-signature": signature, "content-type": "application/json"},
            )
            if response.status_code != 200:
                raise ValueError("Signed synthetic terminal receipt was not accepted")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["install", "terminal"])
    parser.add_argument("workspace", type=UUID)
    args = parser.parse_args()
    settings = fixture_settings(local_settings())
    assert settings.database_url is not None
    register_models()
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with Session(engine) as store:
            if args.command == "install":
                fixture = install_fixture(store, args.workspace, settings)
                MANIFEST.parent.mkdir(exist_ok=True)
                descriptor = os.open(MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as handle:
                    handle.write(fixture.model_dump_json())
                os.chmod(MANIFEST, 0o600)
                print(
                    json.dumps(
                        {"connection_id": str(fixture.connection_id), "invoice_id": str(fixture.invoice_id)}
                    )
                )
            else:
                fixture = read_fixture()
                if fixture.tenant_id != args.workspace or fixture.source_commit != current_source_commit():
                    raise ValueError("Exact current synthetic fixture required")
                emit_terminal(store, fixture)
                print('{"terminal_submitted":true}')
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
