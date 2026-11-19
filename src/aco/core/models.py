"""Explicit complete ORM registration for every database entrypoint.

Imports only model declarations; no app/provider configuration or credential reads.
"""

from importlib import import_module


def register_models() -> None:
    for name in (
        "aco.features.identity.models",
        "aco.features.accounting.models",
        "aco.features.actions.models",
        "aco.features.agent.models",
        "aco.features.agent.live",
        "aco.features.artifacts.models",
        "aco.features.artifacts.consent_models",
        "aco.features.cases.models",
        "aco.features.communications.models",
        "aco.features.communications.email_live",
        "aco.features.impact.models",
        "aco.features.policies.models",
        "aco.features.promises.models",
        "aco.features.review.models",
        "aco.features.usage.models",
        "aco.features.voice.models",
        "aco.features.voice.live",
        "aco.integrations.demo_email.models",
    ):
        import_module(name)
