"""Executable T01 characterizations, not completed ACO feature acceptance.

These tests use real local PostgreSQL/Keycloak and a deterministic FunctionModel.
They cannot establish real-provider access, production auth, SSE, or outreach safety.
"""

import asyncio
import base64
import json
import multiprocessing
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from uuid import uuid4

import httpx
import psycopg
import pytest
from authlib.common.security import generate_token
from authlib.integrations.base_client import OAuthError
from authlib.integrations.httpx_client import OAuth2Client
from authlib.oauth2.rfc6749.errors import MismatchingStateException
from authlib.oidc.core import CodeIDToken
from dotenv import dotenv_values
from joserfc import jwt
from joserfc.errors import BadSignatureError, InvalidClaimError
from joserfc.jwk import KeySet
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from playwright.sync_api import Route, sync_playwright
from procrastinate import App, PsycopgConnector
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import UsageLimits
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


@dataclass(repr=False)
class LocalStack:
    conninfo: str = field(repr=False)
    engine: Engine = field(repr=False)
    schema: str
    oidc_secret: str = field(repr=False)
    demo_password: str = field(repr=False)


@pytest.fixture
def local_stack() -> Iterator[LocalStack]:
    values = {**dotenv_values(Path(__file__).resolve().parents[2] / ".env"), **os.environ}
    required = ("DATABASE_PASSWORD", "OIDC_CLIENT_SECRET", "DEMO_PASSWORD")
    if any(not values.get(name) for name in required):
        pytest.fail("T01 requires configured local service secrets in ignored .env")
    oidc_secret, demo_password = values["OIDC_CLIENT_SECRET"], values["DEMO_PASSWORD"]
    assert oidc_secret is not None and demo_password is not None
    schema = f"compat_{uuid4().hex}"
    base = make_conninfo(
        host="127.0.0.1", port=55432, dbname="aco", user="aco", password=values["DATABASE_PASSWORD"]
    )
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    url = URL.create(
        "postgresql+psycopg",
        username="aco",
        password=values["DATABASE_PASSWORD"],
        host="127.0.0.1",
        port=55432,
        database="aco",
    )
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield LocalStack(scoped, engine, schema, oidc_secret, demo_password)
    finally:
        engine.dispose()
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


class GraphState(TypedDict):
    run_id: str
    tenant_id: str
    invoice_id: str
    balance_minor: NotRequired[int]
    approved: NotRequired[bool]


class InvoiceRead(BaseModel):
    invoice_id: str
    balance_minor: int
    currency: str


@dataclass
class ReadDependencies:
    engine: Engine
    tenant_id: str


def test_selected_stack_rolls_back_outbox_and_resumes_checkpoint(
    local_stack: LocalStack,
    record_property: Any,
) -> None:
    """Catch split business/outbox commits or loss/replay of a persisted graph run."""
    engine = local_stack.engine
    tenant_id, invoice_id, run_id = (str(uuid4()) for _ in range(3))
    with engine.begin() as db:
        db.execute(
            text("CREATE TABLE business (tenant_id uuid, invoice_id uuid PRIMARY KEY, balance_minor int)")
        )
        db.execute(text("CREATE TABLE outbox (operation_key text PRIMARY KEY, run_id uuid NOT NULL)"))
        db.execute(text("CREATE TABLE effects (operation_key text PRIMARY KEY, run_id uuid NOT NULL)"))
        db.execute(text("CREATE TABLE reads (tenant_id uuid, invoice_id uuid)"))

    params = {"tenant": tenant_id, "invoice": invoice_id, "run": run_id, "key": "synthetic-reminder-1"}

    async def prove_async_transaction() -> None:
        async_engine = create_async_engine(
            engine.url,
            connect_args={"options": f"-csearch_path={local_stack.schema}"},
        )
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        try:
            with pytest.raises(RuntimeError, match="force rollback"):
                async with sessions.begin() as session:
                    await session.execute(
                        text("INSERT INTO business VALUES (:tenant, :invoice, 12500)"), params
                    )
                    await session.execute(text("INSERT INTO outbox VALUES (:key, :run)"), params)
                    raise RuntimeError("force rollback")
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM business")) == 0
                assert await session.scalar(text("SELECT count(*) FROM outbox")) == 0
            async with sessions.begin() as session:
                await session.execute(text("INSERT INTO business VALUES (:tenant, :invoice, 12500)"), params)
                await session.execute(text("INSERT INTO outbox VALUES (:key, :run)"), params)
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM business")) == 1
                assert await session.scalar(text("SELECT count(*) FROM outbox")) == 1
        finally:
            await async_engine.dispose()

    asyncio.run(prove_async_transaction())

    def model_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "get_invoice"
        ]
        if not results:
            return ModelResponse(parts=[ToolCallPart("get_invoice", {"invoice_id": invoice_id})])
        content = InvoiceRead.model_validate(results[-1].content).model_dump()
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, content)])

    agent = Agent(FunctionModel(model_response), deps_type=ReadDependencies, output_type=InvoiceRead)

    @agent.tool
    def get_invoice(ctx: RunContext[ReadDependencies], invoice_id: str) -> dict[str, Any]:
        """Read one synthetic invoice only within the server-provided tenant."""
        with ctx.deps.engine.begin() as db:
            row = db.execute(
                text(
                    "SELECT invoice_id::text, balance_minor FROM business "
                    "WHERE tenant_id=:tenant AND invoice_id=:invoice"
                ),
                {"tenant": ctx.deps.tenant_id, "invoice": invoice_id},
            ).one()
            # Tool-invocation evidence is separate from the financial read-only data.
            db.execute(
                text("INSERT INTO reads VALUES (:tenant, :invoice)"),
                {"tenant": ctx.deps.tenant_id, "invoice": invoice_id},
            )
        return {"invoice_id": row.invoice_id, "balance_minor": row.balance_minor, "currency": "AUD"}

    def investigate(state: GraphState) -> dict[str, Any]:
        result = agent.run_sync(
            "Read the scoped synthetic invoice",
            deps=ReadDependencies(engine, state["tenant_id"]),
            usage_limits=UsageLimits(request_limit=3, tool_calls_limit=1),
        )
        assert result.output.invoice_id == state["invoice_id"]
        return {"balance_minor": result.output.balance_minor}

    def approval(state: GraphState) -> dict[str, bool]:
        return {"approved": interrupt({"run_id": state["run_id"], "kind": "synthetic_internal_effect"})}

    def execute(state: GraphState) -> dict[str, Any]:
        assert state["approved"] is True
        with engine.begin() as db:
            db.execute(text("INSERT INTO effects VALUES (:key,:run) ON CONFLICT DO NOTHING"), params)
        return {}

    builder = StateGraph(GraphState)
    builder.add_node("investigate", investigate)
    builder.add_node("approval", approval)
    builder.add_node("execute", execute)
    builder.add_edge(START, "investigate")
    builder.add_edge("investigate", "approval")
    builder.add_edge("approval", "execute")
    builder.add_edge("execute", END)
    config: RunnableConfig = {"configurable": {"thread_id": run_id}}
    with PostgresSaver.from_conn_string(local_stack.conninfo) as saver:
        saver.setup()
        graph = builder.compile(checkpointer=saver)
        paused = graph.invoke({"run_id": run_id, "tenant_id": tenant_id, "invoice_id": invoice_id}, config)
        assert paused["__interrupt__"][0].value["run_id"] == run_id
        checkpoint = saver.get_tuple(config)
        assert checkpoint is not None
        assert checkpoint.checkpoint["channel_values"]["balance_minor"] == 12500
    # Close the original checkpointer connection and compile against a new one.
    with PostgresSaver.from_conn_string(local_stack.conninfo) as saver:
        restored = builder.compile(checkpointer=saver)
        assert restored.get_state(config).next == ("approval",)
        resumed = restored.invoke(Command(resume=True), config)
        assert resumed["run_id"] == run_id
        assert resumed["balance_minor"] == 12500
        assert restored.get_state(config).next == ()
        restored.invoke(None, config)  # completed-checkpoint replay must not execute effects again
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM business")) == 1
        assert db.scalar(text("SELECT count(*) FROM outbox")) == 1
        assert db.scalar(text("SELECT count(*) FROM effects")) == 1
        assert db.scalar(text("SELECT count(*) FROM reads")) == 1
        assert str(db.scalar(text("SELECT run_id FROM effects"))) == run_id
        assert db.scalar(text("SELECT count(*) FROM checkpoints WHERE thread_id=:run"), params) >= 3
    record_property("persisted_run_id", run_id)
    record_property("isolated_schema", local_stack.schema)
    record_property("observed_rows", "rollback_business=0;rollback_outbox=0;committed_effects=1;read_calls=1")


def test_identity_library_round_trip(local_stack: LocalStack, record_property: Any) -> None:
    """Catch unusable code/PKCE wiring and unverified state, nonce, issuer or signature."""
    issuer = "http://localhost:58080/realms/aco"
    with httpx.Client(trust_env=False) as browser:
        discovery = browser.get(f"{issuer}/.well-known/openid-configuration")
        discovery.raise_for_status()
        metadata = discovery.json()
        assert metadata["issuer"] == issuer
        with OAuth2Client(
            "aco",
            local_stack.oidc_secret,
            scope="openid email profile",
            redirect_uri="http://localhost:8000/api/auth/callback",
            code_challenge_method="S256",
            trust_env=False,
        ) as client:
            nonce, verifier = generate_token(32), generate_token(64)
            authorize_url, state = client.create_authorization_url(
                metadata["authorization_endpoint"],
                code_verifier=verifier,
                nonce=nonce,
            )
            assert parse_qs(urlparse(authorize_url).query)["code_challenge_method"] == ["S256"]
            callbacks: list[str] = []
            denied_origins: list[str] = []

            def local_identity_route(route: Route) -> None:
                target = urlparse(route.request.url)
                if (target.hostname, target.port, target.path) == ("localhost", 8000, "/api/auth/callback"):
                    callbacks.append(route.request.url)
                    route.fulfill(status=200, body="Local OIDC callback captured for Authlib exchange")
                elif target.scheme == "http" and target.hostname == "localhost" and target.port == 58080:
                    route.continue_()
                else:
                    denied_origins.append(f"{target.scheme}://{target.hostname}:{target.port}")
                    route.abort("blockedbyclient")

            with sync_playwright() as playwright:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                actual_browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=str(chrome) if chrome.exists() else None,
                )
                context = actual_browser.new_context(service_workers="block")
                context.route("**/*", local_identity_route)
                page = context.new_page()
                page.goto(authorize_url)
                page.get_by_label("Username or email").fill("owner@example.test")
                page.get_by_label("Password", exact=True).fill(local_stack.demo_password)
                page.get_by_role("button", name="Sign In", exact=True).click()
                page.wait_for_url("http://localhost:8000/api/auth/callback?**")
                context.close()
                actual_browser.close()
            assert denied_origins == []
            assert len(callbacks) == 1
            callback = callbacks[0]
            parsed = urlparse(callback)
            assert (parsed.hostname, parsed.port, parsed.path) == ("localhost", 8000, "/api/auth/callback")
            bad_query = parse_qs(parsed.query)
            bad_query["state"] = ["wrong-state"]
            bad_callback = urlunparse(parsed._replace(query=urlencode(bad_query, doseq=True)))
            with pytest.raises(MismatchingStateException):
                client.fetch_token(
                    metadata["token_endpoint"], authorization_response=bad_callback, state=state
                )
            token = client.fetch_token(
                metadata["token_endpoint"],
                authorization_response=callback,
                state=state,
                code_verifier=verifier,
            )
            jwks_response = browser.get(metadata["jwks_uri"])
            jwks_response.raise_for_status()
            keys = KeySet.import_key_set(jwks_response.json())
            decoded = jwt.decode(token["id_token"], keys, algorithms=["RS256"])
            claims = CodeIDToken(
                decoded.claims,
                decoded.header,
                {"iss": {"essential": True, "value": issuer}, "aud": {"essential": True, "value": "aco"}},
                {"nonce": nonce, "client_id": "aco", "access_token": token["access_token"]},
            )
            claims.validate(leeway=0)
            userinfo = client.get(metadata["userinfo_endpoint"])
            userinfo.raise_for_status()
            assert claims["sub"] == userinfo.json()["sub"]
            assert claims["email"] == userinfo.json()["email"] == "owner@example.test"
            assert claims["email_verified"] is True
            record_property("verified_local_subject", claims["sub"])
            wrong_nonce = CodeIDToken(
                decoded.claims, decoded.header, params={"nonce": "wrong-nonce", "client_id": "aco"}
            )
            with pytest.raises(InvalidClaimError):
                wrong_nonce.validate()
            wrong_issuer = CodeIDToken(
                decoded.claims, decoded.header, {"iss": {"value": "https://untrusted.example.test"}}
            )
            with pytest.raises(InvalidClaimError):
                wrong_issuer.validate()
            wrong_audience = CodeIDToken(decoded.claims, decoded.header, {"aud": {"value": "other-client"}})
            with pytest.raises(InvalidClaimError):
                wrong_audience.validate()
            head, payload, signature = token["id_token"].split(".")
            body = json.loads(base64.urlsafe_b64decode(payload + "=="))
            body["sub"] = "forged-subject"
            forged_payload = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
            with pytest.raises(BadSignatureError):
                jwt.decode(f"{head}.{forged_payload}.{signature}", keys, algorithms=["RS256"])
            with pytest.raises(OAuthError) as replay:
                client.fetch_token(
                    metadata["token_endpoint"],
                    authorization_response=callback,
                    state=state,
                    code_verifier=verifier,
                )
            assert replay.value.error == "invalid_grant"


def _worker_process(conninfo: str, crash_after_effect: bool) -> None:
    """Actual child worker; hard exit emulates death after commit, before job acknowledgement."""
    app = App(connector=PsycopgConnector(conninfo=conninfo))

    @app.task(name="compat.synthetic_effect")
    async def synthetic_effect(operation_key: str) -> None:
        with psycopg.connect(conninfo) as db:
            db.execute("INSERT INTO attempts (operation_key) VALUES (%s)", (operation_key,))
            db.execute("INSERT INTO effects VALUES (%s) ON CONFLICT DO NOTHING", (operation_key,))
        if crash_after_effect:
            os._exit(71)

    async def run() -> None:
        async with app.open_async():
            await app.run_worker_async(
                wait=False,
                concurrency=1,
                listen_notify=False,
                install_signal_handlers=False,
                update_heartbeat_interval=0.05,
                stalled_worker_timeout=0.1,
            )

    asyncio.run(run())


def _run_worker(conninfo: str, *, crash: bool) -> int | None:
    worker = multiprocessing.get_context("spawn").Process(target=_worker_process, args=(conninfo, crash))
    worker.start()
    worker.join(timeout=20)
    if worker.is_alive():
        worker.kill()
        worker.join(timeout=5)
        pytest.fail("Local Procrastinate worker did not finish within its compatibility-test bound")
    return worker.exitcode


def test_worker_recovers_stalled_job(local_stack: LocalStack, record_property: Any) -> None:
    """Catch durable job loss or duplicate local effect when a worker dies before acknowledgement."""
    conninfo, engine = local_stack.conninfo, local_stack.engine
    app = App(connector=PsycopgConnector(conninfo=conninfo))
    with app.open():
        app.schema_manager.apply_schema()
    with engine.begin() as db:
        db.execute(text("CREATE TABLE effects (operation_key text PRIMARY KEY)"))
        db.execute(text("CREATE TABLE attempts (operation_key text)"))
    job = app.configure_task(name="compat.synthetic_effect")
    with app.open():
        job_id = job.defer(operation_key="synthetic-effect-1")
    assert _run_worker(conninfo, crash=True) == 71
    with engine.connect() as db:
        assert (
            db.scalar(text("SELECT status::text FROM procrastinate_jobs WHERE id=:id"), {"id": job_id})
            == "doing"
        )
        assert db.scalar(text("SELECT count(*) FROM effects")) == 1
        assert db.scalar(text("SELECT count(*) FROM attempts")) == 1

    async def recover() -> None:
        async with app.open_async():
            deadline = time.monotonic() + 5
            while True:
                stalled = list(await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=0.1))
                if stalled:
                    break
                assert time.monotonic() < deadline, (
                    "Persisted doing job was not discoverable after worker death"
                )
                await asyncio.sleep(0.02)
            assert [item.id for item in stalled] == [job_id]
            await app.job_manager.retry_job(stalled[0], retry_at=datetime.now(UTC))

    asyncio.run(recover())
    assert _run_worker(conninfo, crash=False) == 0
    with engine.connect() as db:
        assert (
            db.scalar(text("SELECT status::text FROM procrastinate_jobs WHERE id=:id"), {"id": job_id})
            == "succeeded"
        )
        assert db.scalar(text("SELECT count(*) FROM effects")) == 1
        assert db.scalar(text("SELECT count(*) FROM attempts")) == 2
    with app.open():
        duplicate_id = job.defer(operation_key="synthetic-effect-1")
    assert duplicate_id != job_id
    assert _run_worker(conninfo, crash=False) == 0
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM effects")) == 1
        assert db.scalar(text("SELECT count(*) FROM attempts")) == 3
        assert db.scalar(text("SELECT count(*) FROM procrastinate_jobs WHERE status='succeeded'")) == 2
    record_property("isolated_schema", local_stack.schema)
    record_property("persisted_job_ids", f"{job_id},{duplicate_id}")
    record_property("observed_recovery", "crash_exit=71;effect_count=1;attempt_count=3;succeeded_jobs=2")
