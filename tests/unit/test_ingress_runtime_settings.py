"""Synthetic capture is separately selected and never enabled by outbound flags."""

import pytest
from pydantic import SecretStr

from aco.core.local_runtime import local_settings
from aco.core.settings import Settings


def test_ingress_settings_are_explicit_private_and_local_only() -> None:
    default = local_settings({})
    assert not default.synthetic_email_ingress_enabled
    enabled = local_settings(
        {
            "SYNTHETIC_EMAIL_INGRESS_ENABLED": "true",
            "SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET": "synthetic-private-secret",
        }
    )
    assert enabled.synthetic_email_ingress_enabled
    assert enabled.synthetic_email_ingress_webhook_secret is not None
    assert enabled.synthetic_email_ingress_webhook_secret.get_secret_value() == "synthetic-private-secret"
    assert not enabled.synthetic_email_enabled and not enabled.outbound_enabled and enabled.kill_switch
    assert "synthetic-private-secret" not in repr(enabled)
    with pytest.raises(ValueError):
        Settings(synthetic_email_ingress_enabled=True)
    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            web_public_url="https://example.test",
            synthetic_email_ingress_enabled=True,
            synthetic_email_ingress_webhook_secret=SecretStr("synthetic-private-secret"),
        )
