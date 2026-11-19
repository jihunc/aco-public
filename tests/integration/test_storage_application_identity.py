"""Native APP identity checks, distinct from the proxy's bucket-path restriction."""

import json
import os
from collections.abc import Callable
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError


def denied[**P](call: Callable[P, object], *args: P.args, **kwargs: P.kwargs) -> None:
    with pytest.raises(ClientError) as error:
        call(*args, **kwargs)
    assert error.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    assert error.value.response["Error"]["Code"] in {"AccessDenied", "403"}


def test_application_key_cannot_administer_configured_bucket() -> None:
    from artifact_support import storage_adapter

    adapter, _ = storage_adapter()
    client, bucket = adapter.client, adapter.bucket
    encryption = client.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]
    assert any(
        rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256" for rule in encryption["Rules"]
    )
    # Same bucket and actual supported route: a denial must come from native identity policy.
    # If unexpectedly permitted, this writes the identical safe encryption configuration.
    denied(client.put_bucket_encryption, Bucket=bucket, ServerSideEncryptionConfiguration=encryption)
    denied(client.get_bucket_acl, Bucket=bucket)
    denied(client.get_bucket_policy, Bucket=bucket)
    denied(client.list_objects_v2, Bucket=bucket)
    # This missing key cannot acquire a public ACL even if the policy is broken.
    denied(client.put_object_acl, Bucket=bucket, Key="tenants/t09-missing-" + uuid4().hex, ACL="public-read")
    denied(client.head_object, Bucket=bucket, Key="outside-tenants/t09-missing-" + uuid4().hex)


def test_application_key_denies_mutating_admin_operations_on_temporary_bucket() -> None:
    """Root owns creation, exact routing, temporary scope, zero-object proof and cleanup."""
    from artifact_support import storage_adapter

    bucket = os.environ.get("ACO_T09_EMPTY_BUCKET")
    if not bucket:
        pytest.skip("Requires Root's exact-routed temporary empty-bucket maintenance lane")
    assert bucket.startswith("aco-t09-empty-") and len(bucket) <= 63
    adapter, _ = storage_adapter()
    client = adapter.client
    # The temporary bucket is deliberately unencrypted and empty: an unexpected
    # successful delete cannot disable encryption on the normal bucket or erase content.
    denied(client.delete_bucket_encryption, Bucket=bucket)
    denied(client.put_bucket_acl, Bucket=bucket, ACL="private")
    # An unexpectedly applied deny-only policy cannot expose data or deny bucket cleanup.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/nonexistent-policy-proof",
            }
        ],
    }
    denied(client.put_bucket_policy, Bucket=bucket, Policy=json.dumps(policy))
    denied(client.delete_bucket_policy, Bucket=bucket)
    denied(client.delete_bucket, Bucket=bucket)


def test_application_key_cannot_put_outside_tenant_prefix() -> None:
    from artifact_support import storage_adapter

    adapter, _ = storage_adapter()
    key = "outside-tenants/t09-synthetic-" + uuid4().hex
    written = False
    try:
        try:
            adapter.client.put_object(
                Bucket=adapter.bucket,
                Key=key,
                Body=b"Synthetic scope rejection",
                ServerSideEncryption="AES256",
            )
            written = True
        except ClientError as error:
            assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 403
            assert error.response["Error"]["Code"] == "AccessDenied"
        assert not written, "Application identity unexpectedly wrote outside tenant prefix"
    finally:
        if written:
            adapter.client.delete_object(Bucket=adapter.bucket, Key=key)
