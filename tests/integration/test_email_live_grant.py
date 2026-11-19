"""Operator grant checks under offline native isolation; never a live authorization."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aco.features.communications.email_live import ApprovedEmailRetention, EmailLiveConfig
from aco.features.identity.models import now


def retention() -> ApprovedEmailRetention:
    return ApprovedEmailRetention(
        approval_ref="offline-fixture-retention-approval",
        jurisdiction="offline-fixture-jurisdiction",
        subject="consenting synthetic test participants only",
        purpose="bounded synthetic round-trip validation",
        reviewer_id=uuid4(),
        approved_at=now(),
        effective_until=now() + timedelta(hours=1),
        retention_days=1,
        max_bytes=1048576,
    )


def test_live_material_requires_explicit_participant_retention_price_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")
    base = dict(
        authorization_ref="offline-test-not-a-live-approval",
        source_commit="a" * 40,
        sender="sender@example.test",
        recipient="recipient@example.test",
        reply_mailbox="sender@example.test",
        participant_approval_ref="offline-consenting-test-participant",
        sender_verification_ref="offline-domain-verification",
        expires_at=now() + timedelta(minutes=10),
        max_requests=3,
        max_messages=1,
        max_usd=Decimal("0.10"),
        request_usd_ceiling=Decimal("0.01"),
        price_source_ref="offline-ceiling-evidence",
        price_source_at=now(),
    )
    valid = EmailLiveConfig.model_validate(base)
    assert valid.recipient == "recipient@example.test"
    outbound = {
        **base,
        "mode": "OUTBOUND_TEST_ONLY",
        "sender": "onboarding@resend.dev",
        "reply_mailbox": None,
        "max_received_messages": 0,
        "provider_test_account_email": "recipient@example.test",
    }
    assert EmailLiveConfig.model_validate(outbound).reply_mailbox is None
    final_scope = {
        **outbound,
        "authorization_ref": "user-final-local-delivery-2026-11-19",
        "recipient": "delivery-recipient@synthetic.test",
        "provider_test_account_email": "delivery-recipient@synthetic.test",
    }
    assert EmailLiveConfig.model_validate(final_scope).recipient == "delivery-recipient@synthetic.test"
    with pytest.raises(ValidationError, match="sole approved"):
        EmailLiveConfig.model_validate(
            {
                **final_scope,
                "recipient": "other@example.test",
                "provider_test_account_email": "other@example.test",
            }
        )
    for changed in (
        {"reply_mailbox": "onboarding@resend.dev"},
        {"max_received_messages": 1},
        {"sender": "arbitrary@example.test"},
        {"provider_test_account_email": "other@example.test"},
    ):
        with pytest.raises(ValidationError):
            EmailLiveConfig.model_validate({**outbound, **changed})
    for key in (
        "authorization_ref",
        "source_commit",
        "participant_approval_ref",
        "sender_verification_ref",
        "price_source_ref",
    ):
        with pytest.raises(ValidationError):
            EmailLiveConfig.model_validate({**base, key: ""})
    with pytest.raises(ValidationError):
        EmailLiveConfig.model_validate({**base, "request_usd_ceiling": Decimal("0")})
    with pytest.raises(ValidationError):
        retention().model_validate({**retention().model_dump(), "purpose": ""})


def test_persisted_grant_current_scope_retained_budget_and_immutable_approval() -> None:
    from action_support import action_world
    from fastapi import HTTPException
    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError

    from aco.features.communications.email_live import EmailLiveAuthorization, install_email_live_grant
    from aco.features.identity.models import ProviderConnection
    from aco.features.usage.models import BudgetReservation, ProviderUsageEvent, UsageAttempt

    with action_world() as world:
        db, ctx = world.store, world.ctx
        email = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            external_account_id="offline-account",
            encrypted_credentials_ref="offline-revision",
        )
        storage = ProviderConnection(
            tenant_id=ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
        )
        db.add_all([email, storage])
        db.commit()
        config = EmailLiveConfig(
            authorization_ref="OFFLINE-FIXTURE-NOT-LIVE-APPROVAL",
            source_commit="a" * 40,
            sender="sender@example.test",
            recipient="recipient@example.test",
            reply_mailbox="sender@example.test",
            participant_approval_ref="offline-participant",
            sender_verification_ref="offline-domain",
            expires_at=now() + timedelta(minutes=10),
            max_requests=3,
            max_messages=1,
            max_usd=Decimal(".03"),
            request_usd_ceiling=Decimal(".01"),
            price_source_ref="offline-price",
            price_source_at=now(),
        )
        approval = retention().model_copy(update={"reviewer_id": ctx.actor_id})
        grant = install_email_live_grant(
            db,
            tenant_id=ctx.tenant_id,
            initiator_id=ctx.actor_id,
            connection_id=email.id,
            storage_connection_id=storage.id,
            account_id="offline-account",
            credential_revision="offline-revision",
            config=config,
            retention=approval,
        )
        db.commit()
        killed = False
        guard = EmailLiveAuthorization(
            grant.id,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=lambda: killed,
        )
        assert guard.validate(db, ctx.tenant_id, email.id).id == grant.id
        for tenant_id, connection_id in [(uuid4(), email.id), (ctx.tenant_id, uuid4())]:
            with pytest.raises(HTTPException, match="EMAIL_LIVE_GRANT_REQUIRED"):
                guard.validate(db, tenant_id, connection_id)
        assert guard.retention(db, ctx.tenant_id, storage.id).id == grant.retention_policy_id
        with pytest.raises(HTTPException, match="EMAIL_LIVE_STORAGE_SCOPE"):
            guard.retention(db, ctx.tenant_id, uuid4())
        killed = True
        with pytest.raises(HTTPException, match="EMAIL_LIVE_KILL_SWITCH"):
            guard.validate(db, ctx.tenant_id, email.id)
        killed = False
        wrong_source = EmailLiveAuthorization(
            grant.id,
            authorization_ref=config.authorization_ref,
            source_commit="b" * 40,
            kill_switch=lambda: False,
        )
        with pytest.raises(HTTPException, match="EMAIL_LIVE_CONFIGURATION_CHANGED"):
            wrong_source.validate(db, ctx.tenant_id, email.id)

        def reserve(operation: str) -> BudgetReservation:
            attempt = UsageAttempt(
                tenant_id=ctx.tenant_id,
                provider_connection_id=email.id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.correlation_id,
                operation="send_email" if operation == "send_email" else f"email_ingress_{operation}",
                outcome="PENDING",
            )
            db.add(attempt)
            db.flush()
            return guard.reserve(db, ctx, email.id, operation, attempt_id=attempt.id)

        with pytest.raises(HTTPException, match="EMAIL_LIVE_ATTEMPT_REQUIRED"):
            guard.reserve(db, ctx, email.id, "send_email", attempt_id=uuid4())
        first = reserve("send_email")
        db.commit()
        with pytest.raises(HTTPException, match="EMAIL_LIVE_REQUEST_ALREADY_RESERVED"):
            guard.reserve(
                db, ctx, email.id, "send_email", attempt_id=UUID(first.operation_key.rsplit(":", 1)[1])
            )
        with db.begin_nested(), pytest.raises(HTTPException, match="EMAIL_LIVE_BUDGET_EXHAUSTED"):
            reserve("send_email")
        # Three real request slots share the same grant; unknown costs never release exposure.
        reserve("get_message")
        reserve("get_attachment")
        db.commit()
        with db.begin_nested(), pytest.raises(HTTPException, match="EMAIL_LIVE_BUDGET_EXHAUSTED"):
            reserve("get_message")
        rows = list(
            db.scalars(select(BudgetReservation).where(BudgetReservation.status == "UNKNOWN_SIDE_EFFECT"))
        )
        assert len(rows) == 3 and sum(r.retained_amount for r in rows) == Decimal(".03")
        assert first.spent_amount == 0
        assert (
            list(db.scalars(select(ProviderUsageEvent).where(ProviderUsageEvent.provider == "resend"))) == []
        )
        for statement in (
            "UPDATE email_live_grants SET material='{}' WHERE id=:key",
            "DELETE FROM email_live_grants WHERE id=:key",
        ):
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(text(statement), {"key": grant.id})
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("UPDATE artifact_retention_policies SET authority='other' WHERE id=:key"),
                {"key": grant.retention_policy_id},
            )
        email.encrypted_credentials_ref = "rotated"
        db.commit()
        with pytest.raises(HTTPException, match="EMAIL_LIVE_GRANT_REVOKED"):
            guard.validate(db, ctx.tenant_id, email.id)


@pytest.mark.parametrize(
    "outbound_only,startup_rotation,delivery_campaign",
    [(False, False, False), (True, False, False), (False, True, False), (True, False, True)],
)
def test_application_selection_uses_real_grant_before_private_credential_lookup(
    tmp_path: "Path",
    monkeypatch: pytest.MonkeyPatch,
    outbound_only: bool,
    startup_rotation: bool,
    delivery_campaign: bool,
) -> None:
    import json
    from hashlib import sha256

    if delivery_campaign:
        monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")

    from action_support import action_world
    from artifact_support import storage_configuration
    from fastapi import HTTPException
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.features.communications.email_live import install_email_live_grant
    from aco.features.identity.models import ProviderConnection
    from aco.integrations.resend import application
    from aco.integrations.s3.adapter import StorageRuntime

    with action_world() as world:
        db, ctx = world.store, world.ctx
        email = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            external_account_id="offline-account",
            encrypted_credentials_ref="offline-key-reference",
        )
        storage = ProviderConnection(
            tenant_id=ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
        )
        db.add_all([email, storage])
        db.commit()
        config = EmailLiveConfig(
            authorization_ref="user-final-local-delivery-2026-11-19"
            if delivery_campaign
            else "OFFLINE-LOADER-NOT-LIVE-APPROVAL",
            source_commit="a" * 40,
            mode="OUTBOUND_TEST_ONLY" if outbound_only else "ROUNDTRIP_TEST",
            sender="onboarding@resend.dev" if outbound_only else "sender@example.test",
            recipient="delivery-recipient@synthetic.test"
            if delivery_campaign
            else "participant@example.test",
            reply_mailbox=None if outbound_only else "sender@example.test",
            max_received_messages=0 if outbound_only else 1,
            provider_test_account_email=(
                "delivery-recipient@synthetic.test" if delivery_campaign else "participant@example.test"
            )
            if outbound_only
            else None,
            participant_approval_ref="offline-participant",
            sender_verification_ref="offline-sender",
            expires_at=now() + timedelta(minutes=10),
            max_requests=3,
            max_messages=1,
            max_usd=Decimal(".03"),
            request_usd_ceiling=Decimal(".01"),
            price_source_ref="offline-price",
            price_source_at=now(),
        )
        grant = install_email_live_grant(
            db,
            tenant_id=ctx.tenant_id,
            initiator_id=ctx.actor_id,
            connection_id=email.id,
            storage_connection_id=storage.id,
            account_id="offline-account",
            credential_revision="offline-key-reference",
            config=config,
            retention=retention().model_copy(update={"reviewer_id": ctx.actor_id}),
        )
        db.commit()
        credentials = tmp_path / "credentials.json"
        credentials.write_text(
            json.dumps(
                {
                    "api_key": "offline-placeholder",
                    **({} if outbound_only else {"webhook_secret": "offline-placeholder"}),
                }
            )
        )
        credentials.chmod(0o600)
        selected = tmp_path / "email-selection.json"
        values = {
            "grant_id": str(grant.id),
            "tenant_id": str(ctx.tenant_id),
            "connection_id": str(email.id),
            "authorization_ref": config.authorization_ref,
            "source_commit": config.source_commit,
            "credential_file": str(credentials),
            "credential_sha256": sha256(credentials.read_bytes()).hexdigest(),
            "download_hosts": [] if outbound_only else ["files.example.test"],
            "webhook_endpoint_id": None if outbound_only else str(uuid4()),
            "kill_switch": False,
        }
        selected.write_text(json.dumps({**values, "grant_id": str(uuid4())}))
        selected.chmod(0o600)
        seen: list[Path] = []
        read = application.read_private

        rotate_after_read = False

        def tracked(path: Path) -> bytes:
            nonlocal rotate_after_read
            seen.append(path)
            raw = read(path)
            if rotate_after_read and path == selected:
                rotate_after_read = False
                selected.write_text(json.dumps({**values, "kill_switch": True}))
            return raw

        monkeypatch.setattr(application, "read_private", tracked)
        # Only source-build identity is fixed for this offline fixture; DB authorization is genuine.
        monkeypatch.setattr(application, "current_source_commit", lambda: "a" * 40)
        settings = Settings(
            database_url=SecretStr(world.url),
            synthetic_email_enabled=True,
            outbound_enabled=True,
            kill_switch=False,
        )
        storage_runtime = StorageRuntime(**storage_configuration())
        with pytest.raises(RuntimeError, match="BOUNDED_EMAIL_CONFIGURATION_BLOCKED"):
            application.local_email_runtime(
                settings, world.client.app.state.business_sessions, storage_runtime, config_path=selected
            )
        assert credentials not in seen
        if delivery_campaign:
            from aco.integrations.provider_smoke.campaign import (
                AUTHORIZATION_REF,
                CampaignAllocation,
                LiveCampaign,
            )

            selected.write_text(json.dumps(values))
            with pytest.raises(RuntimeError, match="BOUNDED_EMAIL_CONFIGURATION_BLOCKED"):
                application.local_email_runtime(
                    settings, world.client.app.state.business_sessions, storage_runtime, config_path=selected
                )
            assert credentials not in seen
            ledger = tmp_path / "campaign.sqlite3"
            monkeypatch.setattr(
                "aco.integrations.provider_smoke.campaign.canonical_campaign_path", lambda: ledger
            )
            campaign = LiveCampaign.create(ledger, authorization_ref=AUTHORIZATION_REF)
            allocation = CampaignAllocation(
                id=uuid4(),
                grant_id=grant.id,
                provider="resend",
                source_commit=config.source_commit,
                max_usd=config.max_usd,
                max_requests=config.max_requests,
                max_messages=config.max_messages,
            )
            campaign.allocate(allocation)
            values.update({"campaign_file": str(ledger), "allocation_id": str(allocation.id)})
        selected.write_text(json.dumps(values))
        if delivery_campaign:
            monkeypatch.delenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT")
            with pytest.raises(RuntimeError, match="BOUNDED_EMAIL_CONFIGURATION_BLOCKED"):
                application.local_email_runtime(
                    settings, world.client.app.state.business_sessions, storage_runtime, config_path=selected
                )
            assert credentials not in seen
            monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")
        rotate_after_read = startup_rotation
        bundle = application.local_email_runtime(
            settings, world.client.app.state.business_sessions, storage_runtime, config_path=selected
        )
        assert bundle is not None
        if startup_rotation:
            assert json.loads(selected.read_text())["kill_switch"] is True
            assert bundle.outbound_runtime.live_authorization is not None
            with pytest.raises(HTTPException, match="EMAIL_LIVE_KILL_SWITCH"):
                bundle.outbound_runtime.live_authorization.validate(db, ctx.tenant_id, email.id)
            return
        if outbound_only:
            assert bundle.ingress_runtime is None and bundle.shared_webhooks == {}
            cap = bundle.outbound_runtime.capability(ctx.tenant_id, email.id)
            assert cap.ingress_activation_id is None and cap.mode == "OUTBOUND_TEST_ONLY"
            from aco.features.communications.provider_ingress import (
                ProviderIngressCapability,
                validate_material,
            )

            forbidden_receiving = ProviderIngressCapability(
                tenant_id=ctx.tenant_id,
                connection_id=email.id,
                credential_revision=grant.credential_revision,
                inbox_addresses=("onboarding@resend.dev",),
                webhook_secret_sha256="b" * 64,
                retention_policy_id=grant.retention_policy_id,
                retention_policy_version=grant.retention_policy_version,
                capture_purpose="approved_live_test_capture",
                evidence_mode="LIVE",
                live_grant_id=grant.id,
            )
            with pytest.raises(HTTPException, match="PROVIDER_INGRESS_LIVE_SCOPE"):
                validate_material(db, forbidden_receiving)
        else:
            assert bundle.ingress_runtime is not None
            ingress_cap = bundle.ingress_runtime.capability(ctx.tenant_id, email.id)
            assert ingress_cap.evidence_mode == "LIVE" and ingress_cap.live_grant_id == grant.id
        if outbound_only:
            assert cap.evidence_mode == "LIVE" and cap.live_grant_id == grant.id
        if delivery_campaign:
            # Current DB-backed capability resolution repeats approval, not only startup.
            monkeypatch.delenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT")
            with pytest.raises(ValidationError, match="private operator"):
                bundle.outbound_runtime.capability(ctx.tenant_id, email.id)
            monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "other@synthetic.test")
            with pytest.raises(ValidationError, match="sole approved"):
                bundle.outbound_runtime.capability(ctx.tenant_id, email.id)
            monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")
            assert bundle.outbound_runtime.capability(ctx.tenant_id, email.id).destinations == (
                "delivery-recipient@synthetic.test",
            )
            ledger.chmod(0o644)
            with pytest.raises(HTTPException, match="EMAIL_LIVE_KILL_SWITCH"):
                bundle.outbound_runtime.capability(ctx.tenant_id, email.id)
            ledger.chmod(0o600)
        selected.write_text(json.dumps({**values, "kill_switch": True}))
        with pytest.raises(HTTPException, match="EMAIL_LIVE_KILL_SWITCH"):
            if outbound_only:
                bundle.outbound_runtime.capability(ctx.tenant_id, email.id)
            else:
                assert bundle.ingress_runtime is not None
                bundle.ingress_runtime.capability(ctx.tenant_id, email.id)


def delivery_material() -> dict[str, object]:
    return dict(
        authorization_ref="user-final-local-delivery-2026-11-19",
        source_commit="a" * 40,
        mode="OUTBOUND_TEST_ONLY",
        sender="onboarding@resend.dev",
        recipient="delivery-recipient@synthetic.test",
        provider_test_account_email="delivery-recipient@synthetic.test",
        reply_mailbox=None,
        max_received_messages=0,
        participant_approval_ref="offline-participant",
        sender_verification_ref="offline-sender",
        expires_at=now() + timedelta(minutes=10),
        max_requests=3,
        max_messages=1,
        max_usd=Decimal(".03"),
        request_usd_ceiling=Decimal(".01"),
        price_source_ref="offline-price",
        price_source_at=now(),
    )


@pytest.mark.parametrize(
    "approved",
    [
        None,
        "",
        "not-a-mailbox",
        "person <delivery-recipient@synthetic.test>",
        "delivery-recipient@synthetic.test\n",
        "other@synthetic.test",
    ],
)
def test_delivery_recipient_missing_malformed_or_mismatched_operator_approval_is_closed(
    monkeypatch: pytest.MonkeyPatch, approved: str | None
) -> None:
    if approved is None:
        monkeypatch.delenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", raising=False)
    else:
        monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", approved)
    with pytest.raises(ValidationError, match="private operator|sole approved"):
        EmailLiveConfig.model_validate(delivery_material())


def test_delivery_recipient_is_exact_private_approval_and_rechecked_from_retained_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aco.features.communications.email_live import EmailLiveGrant, config_for

    monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "Delivery-Recipient@Synthetic.Test")
    config = EmailLiveConfig.model_validate(delivery_material())
    grant = EmailLiveGrant(material=config.model_dump(mode="json"))
    assert config_for(grant).recipient == "delivery-recipient@synthetic.test"
    assert "ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT" not in config.model_dump_json()
    monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "other@synthetic.test")
    with pytest.raises(ValidationError, match="sole approved"):
        config_for(grant)
    monkeypatch.delenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT")
    with pytest.raises(ValidationError, match="private operator"):
        config_for(grant)
