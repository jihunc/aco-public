"""Real local S3 infrastructure proofs; no application tenant/retention acceptance.

Run with `scripts/test-storage-admin` after `docker compose up -d --pull never storage`.
Only fixed loopback endpoints and explicit local Docker control are permitted by that protected lane.
Ordinary offline tests skip this lane and forbid Docker control.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
import psycopg
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from dotenv import dotenv_values

SEAWEED_IMAGE = (
    "chrislusf/seaweedfs:4.54@sha256:4b42c212543a0acd4c0e675215ecdcaf62a186ef98def76c7e12701871222d15"
)
REPOSITORY = Path(__file__).resolve().parents[2]
RAW_SCAN_LOOP = (
    'for f do grep -aFq -- "$0" "$f"; scan_status=$?; '
    'case "$scan_status" in 0) exit 3 ;; 1) ;; *) exit "$scan_status" ;; esac; done'
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ACO_RUN_STORAGE_INTEGRATION") != "1",
        reason="explicit local storage infrastructure lane; no external provider traffic",
    ),
]


@dataclass(frozen=True)
class StorageSettings:
    endpoint: str
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


@pytest.fixture(scope="module")
def storage_settings() -> StorageSettings:
    env_file = Path(os.environ.get("ACO_STORAGE_TEST_ENV", ".env"))
    values = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    endpoint = os.environ.get("ACO_STORAGE_TEST_ENDPOINT", "http://127.0.0.1:59000")
    parsed = urlsplit(endpoint)
    assert parsed.hostname in {"127.0.0.1", "localhost"}, "storage proof is loopback-only"
    assert parsed.scheme == "http" and parsed.port in {59000, 58333}
    return StorageSettings(
        endpoint=endpoint,
        bucket=values.get("STORAGE_BUCKET", values.get("S3_BUCKET", "aco-artifacts")),
        access_key=values.get("STORAGE_ACCESS_KEY", values.get("AWS_ACCESS_KEY_ID", "")),
        secret_key=values.get("STORAGE_SECRET_KEY", values.get("AWS_SECRET_ACCESS_KEY", "")),
    )


@pytest.fixture(scope="module")
def s3(storage_settings: StorageSettings) -> Any:
    assert storage_settings.access_key and storage_settings.secret_key
    return boto3.client(
        "s3",
        endpoint_url=storage_settings.endpoint,
        aws_access_key_id=storage_settings.access_key,
        aws_secret_access_key=storage_settings.secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=3,
            retries={"mode": "standard", "max_attempts": 2},
        ),
    )


@pytest.fixture
def object_key(s3: Any, storage_settings: StorageSettings) -> Iterator[str]:
    key = f"infrastructure-proof/{uuid.uuid4()}/synthetic + % #? café.eml"
    yield key
    s3.delete_object(Bucket=storage_settings.bucket, Key=key)


def docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], cwd=REPOSITORY, capture_output=True, text=True, timeout=timeout, check=False
    )


def core_container() -> str:
    if override := os.environ.get("ACO_STORAGE_TEST_CONTAINER"):
        return override
    result = docker("compose", "ps", "-q", "storage-core")
    assert result.returncode == 0 and result.stdout.strip(), "local storage-core must be running"
    return result.stdout.strip()


def scan_raw_files(marker: str, *paths: str) -> subprocess.CompletedProcess[str]:
    script = RAW_SCAN_LOOP
    if not paths:
        script = 'set -- /data/*.dat; [ -f "$1" ] || exit 4; ' + script
    return docker("exec", core_container(), "sh", "-c", script, marker, *paths)


def test_unqualified_upload_is_encrypted_by_bucket_default(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    """Removing default encryption must stop an ordinary SDK upload passing this proof."""
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=b"synthetic encryption test")
    metadata = s3.head_object(Bucket=storage_settings.bucket, Key=object_key)
    assert metadata.get("ServerSideEncryption") == "AES256", "ordinary upload must be encrypted by default"


def test_encrypted_object_plaintext_is_absent_from_raw_volume(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    """Catch a provider claiming encryption while writing recognizable plaintext chunks."""
    marker = "ACO_SYNTHETIC_ENCRYPTION_PROBE_" + uuid.uuid4().hex
    # Random incompressible bytes prevent compression from being mistaken for encryption.
    body = marker.encode() + secrets.token_bytes(65536)
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=body)
    assert s3.get_object(Bucket=storage_settings.bucket, Key=object_key)["Body"].read() == body
    scan = scan_raw_files(marker)
    assert scan.returncode == 0, "plaintext marker must be absent from existing raw .dat volume files"


def test_raw_volume_scan_read_error_is_not_plaintext_absence() -> None:
    """A failed grep read must never make the encryption proof green."""
    scan = scan_raw_files("synthetic-marker", f"/data/missing-scan-input-{uuid.uuid4().hex}.dat")
    assert scan.returncode == 2, "a native grep read error must propagate instead of being counted as absence"


def test_failed_encryption_initialization_blocks_exposed_endpoint(
    storage_settings: StorageSettings, tmp_path: Path
) -> None:
    """A fresh project's rejected encryption config must block proxy startup and every write."""
    project = "aco-storage-gate-" + uuid.uuid4().hex
    port = 59101  # Explicitly allowed by the separate protected storage-admin profile.
    with socket.socket() as listener:
        listener.bind(
            ("127.0.0.1", port)
        )  # A collision fails instead of touching another project's listener.
    bad_xml = tmp_path / "invalid-encryption.xml"
    bad_xml.write_text("<invalid-encryption-config/>")
    override = tmp_path / "storage-failure.override.yaml"
    override.write_text(
        "services:\n"
        "  storage:\n"
        f'    ports: !override ["127.0.0.1:{port}:8333"]\n'
        "  storage-init:\n"
        "    volumes:\n"
        f"      - {json.dumps(str(bad_xml) + ':/storage/default-encryption.xml:ro')}\n"
    )
    compose_args = (
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(REPOSITORY / ".env"),
        "-f",
        str(REPOSITORY / "compose.yaml"),
        "-f",
        str(override),
    )
    configured = docker(*compose_args, "config", "--format", "json")
    assert configured.returncode == 0
    configuration = json.loads(configured.stdout)
    assert configuration["networks"]["default"]["internal"] is True
    assert configuration["networks"]["storage_internal"]["internal"] is True
    for service, definition in configuration["services"].items():
        for network in definition.get("networks", {"default": None}):
            if not configuration["networks"][network].get("internal", False):
                assert service in {"storage", "local-ingress"} and network == "ingress", (
                    "only trusted fixed ingress proxies may provision a non-internal interface"
                )
    try:
        started = docker(
            *compose_args, "up", "--detach", "--pull", "never", "storage", "storage-init", timeout=120
        )
        deadline = time.monotonic() + 30
        init_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            init_id = docker(*compose_args, "ps", "--all", "--quiet", "storage-init").stdout.strip()
            if init_id:
                inspected = docker("inspect", "--format", "{{json .State}}", init_id)
                if inspected.returncode == 0:
                    init_state = json.loads(inspected.stdout)
                    if init_state.get("Status") == "exited":
                        break
            time.sleep(0.25)
        assert init_state.get("Status") == "exited", "rejected encryption setup must finish with failure"
        assert init_state["ExitCode"] == 22, "native S3 encryption PUT must actually reject the invalid XML"
        proxy_id = docker(*compose_args, "ps", "--all", "--quiet", "storage").stdout.strip()
        proxy_running = False
        if proxy_id:
            state = docker("inspect", "--format", "{{.State.Running}}", proxy_id)
            assert state.returncode == 0
            proxy_running = state.stdout.strip() == "true"
        connected = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                connected = True
        except ConnectionRefusedError:
            pass
        # A kernel-policy denial must fail the test, not masquerade as a blocked service.
        client = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=storage_settings.access_key,
            aws_secret_access_key=storage_settings.secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 0},
                connect_timeout=1,
                read_timeout=1,
            ),
        )
        write_status: int | None = None
        encrypted: bool | None = None
        try:
            written = client.put_object(
                Bucket=storage_settings.bucket,
                Key="synthetic-failed-init-proof",
                Body=b"synthetic startup gate proof",
            )
            write_status = written["ResponseMetadata"]["HTTPStatusCode"]
            metadata = client.head_object(Bucket=storage_settings.bucket, Key="synthetic-failed-init-proof")
            encrypted = metadata.get("ServerSideEncryption") == "AES256"
        except EndpointConnectionError:
            pass
        print(
            f"isolated startup result: init_exit=22 proxy_running={proxy_running} "
            f"tcp_connected={connected} write_http={write_status} encrypted={encrypted}"
        )
        assert started.returncode != 0, (
            "Compose must reject exposed service startup when encryption init fails"
        )
        assert not proxy_running and not connected and write_status is None, (
            "failed initialization must leave no exposed write endpoint"
        )
    finally:
        # Never run teardown against the main project or any pre-existing named resource.
        assert project.startswith("aco-storage-gate-") and len(project) == len("aco-storage-gate-") + 32
        cleaned = docker(*compose_args, "down", "--volumes", "--remove-orphans", timeout=90)
        assert cleaned.returncode == 0, "cleanup must remove only this unique failure project's resources"
        for resource, flags in (
            ("ps", ("--all", "--quiet")),
            ("volume", ("ls", "--quiet")),
            ("network", ("ls", "--quiet")),
        ):
            remaining = docker(resource, *flags, "--filter", f"label=com.docker.compose.project={project}")
            assert remaining.returncode == 0 and not remaining.stdout.strip(), (
                "unique failure project's containers, networks and volumes must be removed"
            )


def test_initializer_replay_preserves_existing_encrypted_artifact(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    body = b"synthetic encrypted initializer replay proof"
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=body)
    replayed = docker("compose", "run", "--pull", "never", "--rm", "--no-deps", "storage-init", timeout=45)
    assert replayed.returncode == 0, "authenticated initializer replay must succeed"
    assert s3.get_object(Bucket=storage_settings.bucket, Key=object_key)["Body"].read() == body
    assert s3.head_object(Bucket=storage_settings.bucket, Key=object_key)["ServerSideEncryption"] == "AES256"


def test_only_s3_is_reachable_from_application_network(storage_settings: StorageSettings) -> None:
    """Catch accidental exposure of unauthenticated filer/master/S3 gRPC controls to app peers."""
    network_state = docker("network", "inspect", "aco_default", "--format", "{{.Internal}}")
    assert network_state.returncode == 0 and network_state.stdout.strip() == "true", (
        "the audited peer must only join an internal application network"
    )
    inspected = docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", core_container())
    assert inspected.returncode == 0
    private_networks = json.loads(inspected.stdout)
    assert len(private_networks) == 1, "raw core must only join its dedicated storage network"
    private_ip = next(iter(private_networks.values()))["IPAddress"]
    proxy_id = docker("compose", "ps", "--quiet", "storage").stdout.strip()
    proxy_networks = json.loads(
        docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", proxy_id).stdout
    )
    ingress_ip = proxy_networks["aco_ingress"]["IPAddress"]
    result = docker(
        "run",
        "--pull",
        "never",
        "--rm",
        "--network",
        "aco_default",
        "--entrypoint",
        "/bin/sh",
        SEAWEED_IMAGE,
        "-c",
        "for port in 7333 8888 9333 9340 12535 18333 18888 19333 19340 22535 23646 33646; do "
        'if nc -z -w 1 storage "$port"; then '
        'printf "unexpectedly reachable control port %s\\n" "$port"; exit 1; fi; done; '
        'test "$(curl --silent --output /dev/null --write-out "%{http_code}" '
        'http://storage:8333/"$1")" = 403 || exit 1; '
        "for port in 8333 8888 9333 9340 18333 18888 19333 19340; do "
        'if nc -z -w 1 "$0" "$port"; then '
        'printf "raw core reachable from app network on port %s\\n" "$port"; exit 1; fi; done; '
        'if nc -z -w 1 "$2" 8333; then echo "nonlocal ingress interface reachable"; exit 1; fi; '
        'if ip route | grep -q "^default"; then echo "unexpected default route"; exit 1; fi',
        private_ip,
        storage_settings.bucket,
        ingress_ip,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_fixed_ingress_keeps_providers_internal_and_local_services_working() -> None:
    """Catch Internet-capable provider interfaces or broken fixed PG/OIDC host ingress."""
    for service in ("postgres", "identity", "storage-core", "storage-init"):
        container = docker("compose", "ps", "--all", "--quiet", service).stdout.strip()
        assert container, "required local provider service must exist"
        networks = json.loads(
            docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container).stdout
        )
        for network in networks:
            state = docker("network", "inspect", network, "--format", "{{.Internal}}")
            assert state.returncode == 0 and state.stdout.strip() == "true", (
                "provider services must remain internal-only"
            )
    checked = docker("compose", "exec", "-T", "local-ingress", "nginx", "-t")
    assert checked.returncode == 0, "pinned nginx must actually load the fixed stream ingress configuration"
    values = dotenv_values(REPOSITORY / ".env")
    with psycopg.connect(
        host="127.0.0.1",
        port=55432,
        dbname="aco",
        user="aco",
        password=values["DATABASE_PASSWORD"],
        connect_timeout=3,
    ) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    status, discovery = http_request("http://127.0.0.1:58080/realms/aco/.well-known/openid-configuration")
    assert status == 200 and json.loads(discovery)["issuer"] == "http://127.0.0.1:58080/realms/aco"


def test_ingress_cannot_forward_connect_absolute_urls_or_changed_signed_host(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    """A trusted ingress must never become an arbitrary HTTP/CONNECT forward proxy."""
    for raw_request in (
        b"CONNECT storage-core:8888 HTTP/1.1\r\nHost: storage-core:8888\r\n\r\n",
        b"GET http://storage-core:8888/ HTTP/1.1\r\nHost: storage-core:8888\r\nConnection: close\r\n\r\n",
    ):
        with socket.create_connection(("127.0.0.1", 59000), timeout=3) as connection:
            connection.sendall(raw_request)
            response = connection.recv(4096)
        assert response.split(b" ")[1] in {b"400", b"403", b"405"}, (
            "ingress must refuse forwarding to an arbitrary authority or control surface"
        )
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=b"synthetic signed host proof")
    signed = s3.generate_presigned_url(
        "get_object", Params={"Bucket": storage_settings.bucket, "Key": object_key}, ExpiresIn=30
    )
    request = urllib.request.Request(signed, headers={"Host": "storage-core:8888"})
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(request, timeout=3)
    assert denied.value.code == 403


def http_request(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method=method, data=body)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_private_access_and_invalid_signature_fail_closed(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=b"synthetic private content")
    # A signed URL gives a correctly escaped path without manual key encoding.
    signed = s3.generate_presigned_url(
        "get_object", Params={"Bucket": storage_settings.bucket, "Key": object_key}, ExpiresIn=30
    )
    unsigned = signed.split("?", 1)[0]
    assert http_request(unsigned)[0] == 403
    assert http_request(unsigned + ".anonymous", "PUT", b"unauthorized synthetic write")[0] == 403
    assert http_request(f"{storage_settings.endpoint}/{storage_settings.bucket}?list-type=2")[0] == 403
    tampered = signed.replace("X-Amz-Signature=", "X-Amz-Signature=0", 1)
    assert http_request(tampered)[0] == 403
    # The proxy cannot be used to reach non-S3 controls, even with an HTTP path.
    for path in ("/", "/admin/", "/cluster/status", "/dir/assign", "/status"):
        assert http_request(storage_settings.endpoint + path)[0] == 403


def test_signed_read_preserves_escaped_path_query_and_expires(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    body = b"synthetic signed read through proxy"
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=body)
    signed = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": storage_settings.bucket,
            "Key": object_key,
            "ResponseContentDisposition": 'attachment; filename="synthetic proof.txt"',
        },
        ExpiresIn=2,
    )
    status, returned = http_request(signed)
    assert status == 200 and returned == body
    time.sleep(3)
    assert http_request(signed)[0] == 403
    logs = docker("compose", "logs", "--no-color", "storage")
    assert logs.returncode == 0
    contains_sensitive_request = any(
        value in logs.stdout for value in ("X-Amz-Signature", storage_settings.access_key, body.decode())
    )
    assert not contains_sensitive_request, "proxy logs must omit signed URL credentials and object contents"


def test_metadata_integrity_and_same_key_replay(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    body = b"From: synthetic@example.invalid\r\n\r\nSynthetic MIME preservation proof."
    digest = hashlib.sha256(body).hexdigest()
    for _ in range(2):
        s3.put_object(
            Bucket=storage_settings.bucket,
            Key=object_key,
            Body=body,
            ContentType="message/rfc822",
            Metadata={"sha256": digest, "source-id": "synthetic-001"},
        )
    metadata = s3.head_object(Bucket=storage_settings.bucket, Key=object_key)
    returned = s3.get_object(Bucket=storage_settings.bucket, Key=object_key)["Body"].read()
    assert metadata["ServerSideEncryption"] == "AES256"
    assert metadata["ContentType"] == "message/rfc822" and metadata["ContentLength"] == len(body)
    assert metadata["Metadata"] == {"sha256": digest, "source-id": "synthetic-001"}
    assert metadata["LastModified"].tzinfo is not None
    assert hashlib.sha256(returned).hexdigest() == digest
    listed = s3.list_objects_v2(Bucket=storage_settings.bucket, Prefix=object_key)
    assert listed["KeyCount"] == 1


def test_deletion_and_replay_revoke_object_access(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=b"synthetic authorized deletion")
    signed = s3.generate_presigned_url(
        "get_object", Params={"Bucket": storage_settings.bucket, "Key": object_key}, ExpiresIn=60
    )
    for _ in range(2):
        result = s3.delete_object(Bucket=storage_settings.bucket, Key=object_key)
        assert result["ResponseMetadata"]["HTTPStatusCode"] == 204
    for operation in (s3.get_object, s3.head_object):
        with pytest.raises(ClientError) as denied:
            operation(Bucket=storage_settings.bucket, Key=object_key)
        assert denied.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
    assert http_request(signed)[0] == 404


def test_restart_recovers_encrypted_bytes_metadata_and_bucket_default(
    s3: Any, storage_settings: StorageSettings, object_key: str
) -> None:
    body = b"synthetic durable encrypted artifact " + secrets.token_bytes(4096)
    digest = hashlib.sha256(body).hexdigest()
    s3.put_object(Bucket=storage_settings.bucket, Key=object_key, Body=body, Metadata={"sha256": digest})
    restarted = docker("compose", "restart", "storage-core", "storage", timeout=90)
    assert restarted.returncode == 0, "dedicated storage services must restart successfully"
    deadline = time.monotonic() + 45
    recovered = False
    while time.monotonic() < deadline:
        try:
            returned = s3.get_object(Bucket=storage_settings.bucket, Key=object_key)["Body"].read()
            if returned == body:
                recovered = True
                break
        except (BotoCoreError, ClientError):
            pass
        time.sleep(0.5)
    assert recovered, "encrypted object must remain readable after actual storage service restart"
    metadata = s3.head_object(Bucket=storage_settings.bucket, Key=object_key)
    assert metadata["ServerSideEncryption"] == "AES256" and metadata["Metadata"]["sha256"] == digest
    assert hashlib.sha256(returned).hexdigest() == digest
    rules = s3.get_bucket_encryption(Bucket=storage_settings.bucket)["ServerSideEncryptionConfiguration"][
        "Rules"
    ]
    assert rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_application_artifact_plaintext_absent_from_raw_volume() -> None:
    """Root-controlled seam: scan the exact artifact created by the T09 app lane."""
    value = os.environ.get("ACO_STORAGE_APP_PROOF")
    if not value:
        pytest.skip("no active isolated T09 application maintenance proof")
    proof_path = Path(value).resolve()
    proof = json.loads(proof_path.read_text())
    tenant, artifact = uuid.UUID(proof["tenant_id"]), uuid.UUID(proof["artifact_id"])
    assert proof["object_key"] == f"tenants/{tenant}/artifacts/{artifact}/{proof['sha256']}"
    assert len(proof["sha256"]) == 64 and all(c in "0123456789abcdef" for c in proof["sha256"])
    marker = proof["marker"]
    assert marker.startswith("ACO_T09_APPLICATION_SYNTHETIC_") and len(marker) == 62
    scan = scan_raw_files(marker)
    assert scan.returncode == 0, "exact application marker must be absent from actual raw volume files"
