"""Public review accepts references only, never client-supplied provider authority."""

from aco.api.app import create_app
from aco.core.settings import Settings


def test_provider_review_schema_exposes_only_current_review_references() -> None:
    schema = create_app(Settings()).openapi()
    operation = schema["paths"]["/api/workspaces/{workspace_id}/email-execution/provider"]["put"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    definition = schema["components"]["schemas"][body["$ref"].split("/")[-1]]
    assert set(definition["properties"]) == {
        "connection_id",
        "expected_activation_id",
        "evidence",
        "reviewed_hash",
    }
    assert definition["additionalProperties"] is False
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/EmailExecutionView"
    )
