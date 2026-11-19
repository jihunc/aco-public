"""Fresh explicit fake-only product setup reaches real runtime selectors; no Docker or wires."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values
from dotenv.variables import parse_variables
from yaml.nodes import MappingNode, Node, ScalarNode

from aco.cli import runtime_settings


def mapping_value(node: Node, key: str) -> Node:
    assert isinstance(node, MappingNode)
    return next(value for name, value in node.value if name.value == key)


@pytest.mark.parametrize("enabled", [False, True])
def test_fresh_product_synthetic_workflows_are_explicit_and_cannot_select_live(
    tmp_path: Path, enabled: bool
) -> None:
    target = tmp_path / "fresh-product"
    command = [sys.executable, "scripts/prepare-product.py", "--output", str(target)]
    if enabled:
        command.append("--synthetic-workflows")
    result = subprocess.run(command, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    private = target / "product.env"
    assert private.stat().st_mode & 0o777 == 0o600
    values = dotenv_values(private)
    names = ("SYNTHETIC_EMAIL_ENABLED", "SYNTHETIC_VOICE_ENABLED", "SYNTHETIC_EMAIL_INGRESS_ENABLED")
    observed_flags = {name: values.get(name) for name in names}
    assert observed_flags == {name: str(enabled).lower() for name in names}
    # Read actual public Compose nodes and use maintained dotenv interpolation,
    # including :- defaults. No custom YAML constructor or resolved-secret output.
    document = yaml.compose(Path("compose.product.yaml").read_text())
    assert document is not None
    environment = mapping_value(mapping_value(document, "x-product-app"), "environment")
    assert isinstance(environment, MappingNode)
    resolved = {}
    for key, value in environment.value:
        assert isinstance(key, ScalarNode) and isinstance(value, ScalarNode)
        resolved[key.value] = "".join(atom.resolve(values) for atom in parse_variables(value.value))
    try:
        settings = runtime_settings(resolved)
    except ValueError:
        raise AssertionError("Generated fake-only product configuration was refused") from None
    assert settings.synthetic_email_enabled is enabled
    assert settings.synthetic_voice_enabled is enabled
    assert settings.synthetic_email_ingress_enabled is enabled
    assert settings.outbound_enabled is enabled
    assert settings.kill_switch is not enabled
    assert settings.external_providers == settings.llm_provider == "fake"
    assert settings.email_adapter == settings.voice_adapter == "mock"
    assert not settings.recording_enabled and not settings.transcription_enabled
    assert not any(name.startswith("ACO_") and "LIVE" in name for name in resolved)
    assert not {"OPENAI_API_KEY", "RESEND_API_KEY", "VAPI_API_KEY"} & resolved.keys()
    leaked_fixture = bool(values["DEMO_PASSWORD"] and values["DEMO_PASSWORD"] in result.stdout)
    assert not leaked_fixture


def test_fresh_product_seeds_four_identities_without_application_authority(tmp_path: Path) -> None:
    target = tmp_path / "fresh-identities"
    command = [sys.executable, "scripts/prepare-product.py", "--output", str(target)]
    result = subprocess.run(command, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    realm_path = target / "realm.json"
    realm_bytes = realm_path.read_bytes()
    realm = json.loads(realm_bytes)
    assert {user["username"] for user in realm["users"]} == {
        "owner@example.test", "finance@example.test", "operator@example.test", "viewer@example.test",
    }
    assert realm_path.stat().st_mode & 0o777 == 0o600
    for user in realm["users"]:
        assert user["email"] == user["username"]
        assert user["emailVerified"] is True and user["enabled"] is True
        assert not any(user.get(key) for key in ("realmRoles", "clientRoles", "groups", "attributes"))
        assert user["credentials"] == [{
            "type": "password", "value": "${ACO_DEMO_PASSWORD}", "temporary": False,
        }]
    # Existing output is rejected, never replaced/reseeded by setup.
    refused = subprocess.run(command, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert refused.returncode == 2
    assert realm_path.read_bytes() == realm_bytes
