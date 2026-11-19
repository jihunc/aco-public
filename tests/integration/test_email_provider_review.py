"""Public company provider review exposes no credentials and performs no external work."""

from typing import NoReturn

import pytest
from identity_support import actor_client


@pytest.mark.parametrize("role", ["owner", "finance_manager", "operator", "viewer"])
def test_provider_review_requires_current_owner_and_default_is_unavailable(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("Reading disabled company email configuration cannot look up credentials or send")

    monkeypatch.setattr("aco.integrations.resend.application.local_email_runtime", forbidden)
    with actor_client(role) as (client, store, user, workspace):
        result = client.get(f"/api/workspaces/{workspace}/email-execution/provider")
        assert result.status_code == (200 if role == "owner" else 403), result.text
        if role == "owner":
            assert result.json() == {
                "available": False,
                "configurations": [],
                "reason": "PROVIDER_OUTBOUND_UNAVAILABLE",
            }
            execution = client.get(f"/api/workspaces/{workspace}/email-execution").json()
            assert execution["mode"] == "DISABLED"
            assert execution["synthetic"] is True


def test_saved_demo_activation_is_not_current_after_runtime_switch() -> None:
    from reminder_support import reminder_world

    from aco.features.communications.activation import EmailActivationService
    from aco.features.communications.provider_ingress import ProviderIngressRuntime
    from aco.features.communications.provider_outbound import ProviderOutboundRuntime

    def no_provider_material(*args: object, **kwargs: object) -> NoReturn:
        raise LookupError("No provider binding for the retained Demo activation")

    with reminder_world() as (client, db, ctx, _invoices, _saved):
        app = client.app
        original = EmailActivationService(db, app.state.settings, app.state.ports.email).view(ctx)
        assert original.activation is not None and original.activation.current
        retained = original.activation.id
        provider_ingress = ProviderIngressRuntime(
            app.state.business_sessions, no_provider_material, no_provider_material, enabled=True
        )
        provider = ProviderOutboundRuntime(
            app.state.business_sessions,
            app.state.settings,
            provider_ingress,
            no_provider_material,
            no_provider_material,
            enabled=True,
        )
        switched = EmailActivationService(db, app.state.settings, provider).view(ctx)
        assert switched.provider == "resend"
        assert switched.activation is not None and switched.activation.id == retained
        assert switched.activation.current is False
