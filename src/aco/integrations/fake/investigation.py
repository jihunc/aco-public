"""Offline PydanticAI FunctionModel: decisions derive from actual scoped tool responses."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import UsageLimits

from aco.core.money import format_minor
from aco.core.types import CostQuality, ProviderCapabilities, ProviderCost, ProviderErrorCode, ProviderFailure
from aco.features.identity.models import now
from aco.integrations.ports.llm import (
    LLMInput,
    LLMRequestPolicy,
    LLMUsage,
    ReadTool,
    ToolReadEvidence,
    TypedResult,
)


def _major(minor: object, exponent: object) -> str:
    return format_minor(int(str(minor)), int(str(exponent)))


class InvestigationFake:
    def __init__(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        invoice_ids: tuple[UUID, ...],
        *,
        source_communication_id: UUID | None = None,
    ):
        self.tenant_id, self.connection_id, self.invoice_ids = tenant_id, connection_id, invoice_ids
        self.source_communication_id = source_communication_id

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset({"structured_output", "tool_calling"}), limits={"max_tool_calls": 8}
        )

    async def run_typed[OutputT: BaseModel](
        self,
        model_route: str,
        input_context: LLMInput,
        tools: tuple[ReadTool[Any, Any], ...],
        output_schema: type[OutputT],
        request_policy: LLMRequestPolicy,
    ) -> TypedResult[OutputT]:
        evidence: list[ToolReadEvidence] = []
        output = None
        error = None

        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            returns = [
                p
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
                if isinstance(p, ToolReturnPart)
            ]
            objective = input_context.text.split("User objective: ", 1)[-1]
            task_match = re.fullmatch(
                r"Update task ([0-9a-f-]{36}) version ([1-9][0-9]*) to (in_progress|done)",
                objective,
                re.IGNORECASE,
            )
            if objective.lower().startswith("update task") and task_match is None:
                raise ValueError("Explicit synthetic task update grammar required")
            if task_match and len(self.invoice_ids) != 1:
                raise ValueError("Explicit synthetic task update requires one selected invoice")
            case_match = re.search(
                r"\bcase ([0-9a-f-]{36}) version ([1-9][0-9]*)\b",
                input_context.text.split("User objective: ", 1)[-1],
                re.IGNORECASE,
            )
            if not returns:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            name, {"invoice_id": str(invoice_id)}, tool_call_id=f"{name}-{invoice_id}"
                        )
                        for invoice_id in self.invoice_ids
                        for name in ("get_invoice", "get_verified_payments")
                    ]
                    + (
                        [
                            ToolCallPart(
                                "find_existing_cases",
                                {"invoice_id": str(self.invoice_ids[0])},
                                tool_call_id="case-context",
                            )
                        ]
                        if case_match
                        else []
                    )
                    + (
                        [
                            ToolCallPart(
                                "list_collection_tasks",
                                {"invoice_id": str(self.invoice_ids[0])},
                                tool_call_id="task-context",
                            )
                        ]
                        if task_match
                        else []
                    )
                    + (
                        [
                            ToolCallPart(
                                "get_communication_history",
                                {"invoice_id": str(self.invoice_ids[0])},
                                tool_call_id="inbound-source",
                            )
                        ]
                        if self.source_communication_id is not None
                        else []
                    )
                )
            source_reads = [
                cast(dict[str, Any], p.content)
                for p in returns
                if p.tool_name == "get_communication_history" and isinstance(p.content, dict)
            ]
            if self.source_communication_id is not None and not any(
                fact.get("communication_id") == str(self.source_communication_id)
                and fact.get("direction") == "incoming"
                and fact.get("source_state") == "stored"
                for read in source_reads
                for fact in read.get("records", [])
            ):
                raise ValueError("Exact captured inbound source unavailable")
            case_reads = [
                cast(dict[str, Any], p.content)
                for p in returns
                if p.tool_name == "find_existing_cases" and isinstance(p.content, dict)
            ]
            task_reads = [
                cast(dict[str, Any], p.content)
                for p in returns
                if p.tool_name == "list_collection_tasks" and isinstance(p.content, dict)
            ]
            case_binding = None
            if case_match:
                exact = [
                    fact
                    for read in case_reads
                    for fact in read.get("records", [])
                    if fact.get("case_id") == case_match[1] and fact.get("version") == int(case_match[2])
                ]
                if len(exact) != 1:
                    raise ValueError("Exact requested case/version unavailable")
                case_binding = {"case_id": exact[0]["case_id"], "case_version": exact[0]["version"]}
            findings: list[dict[str, Any]] = []
            proposals: list[dict[str, Any]] = []
            for invoice_id in self.invoice_ids:
                reads = [
                    cast(dict[str, Any], p.content)
                    for p in returns
                    if p.tool_name in {"get_invoice", "get_verified_payments"}
                    and isinstance(p.content, dict)
                    and p.content.get("invoice_id") == str(invoice_id)
                ]
                invoices = [fact for r in reads for fact in r["records"] if fact["kind"] == "invoice"]
                payments = [fact for r in reads for fact in r["records"] if fact["kind"] == "payment"]
                if len(invoices) != 1 or len(reads) != 2:
                    raise ValueError("Scoped evidence incomplete")
                invoice = invoices[0]
                findings.append(
                    {
                        "invoice_id": str(invoice_id),
                        "assessment": (
                            f"{invoice['number']}: source snapshot balance {invoice['currency']} "
                            f"{_major(invoice['balance_minor'], invoice.get('exponent', 2))} "
                            f"({invoice['balance_minor']} minor units); "
                            f"financial status {invoice['financial_status']}; "
                            f"{len(payments)} imported payment allocation(s). "
                            "This is last-known evidence, not a fresh provider observation."
                        ),
                        "evidence_step_ids": [
                            r["step_id"]
                            for r in reads
                            + [
                                v
                                for v in source_reads + case_reads + task_reads
                                if v["invoice_id"] == str(invoice_id)
                            ]
                        ],
                    }
                )
                proposals.append(
                    {
                        "invoice_id": str(invoice_id),
                        "kind": "verify_accounting",
                        ("reason"): (
                            "Request human verification of source evidence before a collection decision; "
                            "no source refresh or outreach is requested."
                        ),
                        "executed": False,
                    }
                )
            if task_match is None and "task" in objective.lower():
                for proposal in proposals:
                    proposal["kind"] = (
                        "request_task_approval"
                        if "approval" in input_context.text.split("User objective: ", 1)[-1].lower()
                        else "create_task"
                    )
                    if case_binding is not None:
                        if proposal["invoice_id"] != str(self.invoice_ids[0]):
                            proposal["kind"] = "human_review"
                            continue
                        proposal["case_binding"] = case_binding
                    proposal["title"] = "Review imported invoice evidence"
                    proposal["reason"] = (
                        "Review source-stamped invoice and payment evidence; verify current accounti"
                        "ng before collection outreach."
                    )
                    if self.source_communication_id is not None:
                        proposal["reason"] = str(proposal["reason"]) + (
                            f" Review captured inbound communication {self.source_communication_id}; "
                            "sender authority remains unverified."
                        )
            if task_match:
                task_id = str(UUID(task_match[1]))
                exact_tasks = [
                    fact
                    for read in task_reads
                    if read.get("available") is True and read.get("invoice_id") == str(self.invoice_ids[0])
                    for fact in read.get("records", [])
                    if fact.get("kind") == "collection_task"
                    and fact.get("task_id") == task_id
                    and fact.get("invoice_id") == str(self.invoice_ids[0])
                    and fact.get("version") == int(task_match[2])
                ]
                if len(exact_tasks) != 1:
                    raise ValueError("Exact requested task/version unavailable")
                proposals = [
                    {
                        "invoice_id": str(self.invoice_ids[0]),
                        "kind": "update_collection_task",
                        "reason": "Explicit simulated task-status update using the actual current task read; "
                        "no Case, payment, urgency or outreach change.",
                        "executed": False,
                        "task_update": {
                            "task_id": exact_tasks[0]["task_id"],
                            "expected_version": exact_tasks[0]["version"],
                            "status": task_match[3].lower(),
                        },
                    }
                ]
            if (
                input_context.text.split("User objective: ", 1)[-1]
                == "Prepare a synthetic customer email and request independent approval"
            ):
                for proposal in proposals:
                    proposal["kind"] = "request_email_send"
                    proposal["reason"] = (
                        "Prepare fixed synthetic text for independent human review; no dispatch authority."
                    )
                    proposal["email_command"] = {
                        "invoice_id": proposal["invoice_id"],
                        "subject": "Synthetic invoice review",
                        "content": (
                            "SYNTHETIC DEMO: Please review the referenced invoice with our collections team."
                        ),
                    }
            if (
                input_context.text.split("User objective: ", 1)[-1]
                == "Request an independently reviewed synthetic browser voice session"
            ):
                proposals = [
                    {
                        "invoice_id": str(self.invoice_ids[0]),
                        "kind": "request_voice_call",
                        "reason": "Request independent review only; no call, capture or PSTN authority.",
                        "executed": False,
                        "voice_command": {
                            "invoice_id": str(self.invoice_ids[0]),
                            "invoice_ids": [str(key) for key in self.invoice_ids],
                            "objective": (
                                "Review the selected synthetic invoice evidence in a browser conversation"
                            ),
                        },
                    }
                ]
            # Deliberately narrow fixture recognition, never general interpretation or authority.
            # Its actual incoming original still passes the independent source/action guards.
            invoice_facts = [
                fact
                for part in returns
                if part.tool_name == "get_invoice" and isinstance(part.content, dict)
                for fact in cast(dict[str, Any], part.content).get("records", [])
                if fact.get("kind") == "invoice"
            ]
            expected_stop = (
                "SYNTHETIC DEMO SOURCE. This is a fixed application test scenario, "
                "not a customer statement. "
                "Invoice references: "
                + ", ".join(fact["number"] for fact in sorted(invoice_facts, key=lambda v: v["invoice_id"]))
                + ". For this synthetic example: Please stop all contact for this customer "
                "until a human reviews this request."
            )
            stop_sources = [
                fact
                for read in source_reads
                for fact in read.get("records", [])
                if fact.get("communication_id") == str(self.source_communication_id)
                and fact.get("synthetic") is True
                and fact.get("direction") == "incoming"
                and fact.get("source_state") == "stored"
                and fact.get("offset") == 0
                and fact.get("text_truncated") is False
                and fact.get("excerpt") == expected_stop
            ]
            if (
                len(stop_sources) == 1
                and len(invoice_facts) == len(self.invoice_ids)
                and all(fact["source"]["synthetic"] is True for fact in invoice_facts)
            ):
                primary = next(
                    fact for fact in invoice_facts if fact["invoice_id"] == str(self.invoice_ids[0])
                )
                source = stop_sources[0]
                proposals = [
                    {
                        "invoice_id": str(self.invoice_ids[0]),
                        "kind": "request_collection_hold",
                        "title": "Review synthetic customer contact-stop request",
                        "reason": (
                            "Fixed synthetic incoming stop request; sender remains unverified. "
                            "Human review required."
                        ),
                        "executed": False,
                        "hold_command": {
                            "invoice_id": str(self.invoice_ids[0]),
                            "customer_id": primary["customer_id"],
                            "evidence": {
                                "communication_id": source["communication_id"],
                                "excerpt": source["excerpt"],
                                "offset": source["offset"],
                            },
                        },
                    }
                ]
            # A fixed three-intent source uses one original, not three invented messages.
            # Actual selected-invoice reads above remain mandatory; the additional case
            # callback below is a real guarded read and the eighth tool call, never a
            # fictitious model/provider request or an increased request allowance.
            roles = {fact["number"]: fact for fact in invoice_facts}
            fixed_sources = [
                fact
                for read in source_reads
                for fact in read.get("records", [])
                if fact.get("communication_id") == str(self.source_communication_id)
                and fact.get("synthetic") is True
                and fact.get("direction") == "incoming"
                and fact.get("source_state") == "stored"
                and fact.get("offset") == 0
                and fact.get("text_truncated") is False
            ]
            if (
                case_match is None
                and len(invoice_facts) == 3
                and set(roles) == {"INV-101", "INV-107", "INV-113"}
                and all(fact["source"]["synthetic"] is True for fact in invoice_facts)
                and len(fixed_sources) == 1
            ):
                source = fixed_sources[0]
                due_date = (
                    (datetime.fromisoformat(source["event_at"]).astimezone(UTC) + timedelta(days=7))
                    .date()
                    .isoformat()
                )
                expected_three = (
                    "SYNTHETIC DEMO SOURCE. This is a fixed application test scenario, "
                    "not a customer statement. "
                    "Invoice references: "
                    + ", ".join(
                        fact["number"] for fact in sorted(invoice_facts, key=lambda v: v["invoice_id"])
                    )
                    + ". For this synthetic example: I commit to pay AUD 100.00 for INV-101 "
                    f"by {due_date} in UTC. "
                    "The amount on INV-107 is wrong; please investigate this dispute. "
                    "I claim INV-113 was already paid, but provide no payment evidence; "
                    "verify it against accounting."
                )
                if source["excerpt"] == expected_three:
                    if len(evidence) >= request_policy.max_tool_calls:
                        raise ValueError("Fixed fixture case read exceeds current tool allowance")
                    case_tool = next(tool for tool in tools if tool.name == "find_existing_cases")
                    arguments = case_tool.input_schema.model_validate_json(
                        json.dumps({"invoice_id": roles["INV-107"]["invoice_id"]}), strict=True
                    )
                    observed_case = await case_tool.invoke(arguments)
                    evidence.append(
                        ToolReadEvidence(name=case_tool.name, input=arguments, output=observed_case)
                    )
                    actual_cases = cast(dict[str, Any], observed_case.model_dump(mode="json"))
                    if actual_cases.get("available") is not True:
                        raise ValueError("Actual case read unavailable")
                    exact_case = [
                        fact
                        for fact in actual_cases["records"]
                        if fact.get("kind") == "case"
                        and fact.get("issue_type") == "wrong_amount"
                        and fact.get("claim_key", "").strip().casefold() == "synthetic-amount-dispute"
                    ]
                    if len(exact_case) > 1:
                        raise ValueError("Ambiguous existing synthetic issue")
                    span = {
                        "communication_id": source["communication_id"],
                        "excerpt": source["excerpt"],
                        "offset": source["offset"],
                    }
                    first, second, third = (roles[name] for name in ("INV-101", "INV-107", "INV-113"))
                    case_command = {
                        "customer_id": second["customer_id"],
                        "invoice_ids": [second["invoice_id"]],
                        "issue_type": "wrong_amount",
                        "claim_key": "synthetic-amount-dispute",
                        "substance": (
                            "Exact fixed synthetic source disputes this invoice amount; "
                            "accounting remains authoritative."
                        ),
                        "evidence": span,
                        "matching": "same" if exact_case else "new",
                        "matched_case_id": exact_case[0]["case_id"] if exact_case else None,
                        "expected_version": exact_case[0]["version"] if exact_case else None,
                    }
                    proposals = [
                        {
                            "invoice_id": first["invoice_id"],
                            "kind": "record_promise",
                            "executed": False,
                            "title": "Capture explicit synthetic INV-101 promise for human confirmation",
                            "reason": (
                                "Exact fixed incoming promise; no sender authority "
                                "or verified payment inferred."
                            ),
                            "promise_command": {
                                "customer_id": first["customer_id"],
                                "invoice_ids": [first["invoice_id"]],
                                "evidence": span,
                                "due_date": due_date,
                                "timezone": "UTC",
                                "amount": {"minor_units": 10000, "currency": "AUD", "exponent": 2},
                            },
                        },
                        {
                            "invoice_id": second["invoice_id"],
                            "kind": "upsert_case",
                            "executed": False,
                            "title": "Investigate synthetic INV-107 amount dispute",
                            "reason": (
                                "One stored original supports this selected invoice; "
                                "actual case lookup precedes the command."
                            ),
                            "case_command": case_command,
                        },
                        {
                            "invoice_id": third["invoice_id"],
                            "kind": "create_task",
                            "executed": False,
                            "title": "Review uncorroborated payment claim for INV-113",
                            "reason": "Check the actual imported payment read against the unverified claim "
                            "in original "
                            + source["communication_id"]
                            + "; do not infer paid or modify accounting.",
                        },
                    ]
                    for finding in findings:
                        finding["assessment"] += (
                            " Exact fixed synthetic three-intent source "
                            + source["communication_id"]
                            + "; human review remains required."
                        )
                        if finding["invoice_id"] == second["invoice_id"]:
                            finding["evidence_step_ids"].append(actual_cases["step_id"])
            result = {
                (
                    "summary"
                ): f"FAKE analysis of {len(findings)} selected invoice(s) using persisted source evidence.",
                "invoice_findings": findings,
                "proposals": proposals,
                "limitations": [
                    "Deterministic fake model; no external LLM request.",
                    "Last-known imported snapshots only.",
                    "Only independently guarded internal actions can execute.",
                ],
            }
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(result))])

        def wrap(tool: ReadTool[Any, Any]) -> Tool[Any]:
            async def invoke(**kwargs: Any) -> dict[str, Any]:
                arguments = tool.input_schema.model_validate_json(json.dumps(kwargs), strict=True)
                result = await tool.invoke(arguments)
                evidence.append(ToolReadEvidence(name=tool.name, input=arguments, output=result))
                return cast(dict[str, Any], result.model_dump(mode="json"))

            return Tool.from_schema(
                invoke,
                name=tool.name,
                description="Read authorized selected invoice evidence",
                json_schema=tool.input_schema.model_json_schema(),
                takes_ctx=False,
            )

        try:
            if (
                input_context.tenant_id != self.tenant_id
                or input_context.connection_id != self.connection_id
                or model_route != "investigation"
            ):
                raise ValueError("Scope mismatch")
            agent = Agent(
                FunctionModel(respond), output_type=output_schema, tools=[wrap(t) for t in tools], retries=0
            )
            result = await agent.run(
                input_context.text,
                usage_limits=UsageLimits(
                    request_limit=request_policy.max_requests, tool_calls_limit=request_policy.max_tool_calls
                ),
            )
            output = result.output
        except Exception:
            error = ProviderFailure(
                code=ProviderErrorCode.VALIDATION_FAILED, retryable=False, may_have_side_effect=False
            )
        return TypedResult[OutputT](
            tenant_id=input_context.tenant_id,
            connection_id=input_context.connection_id,
            actor_id=input_context.actor_id,
            correlation_id=input_context.correlation_id,
            attempt_id=uuid4(),
            occurred_at=now(),
            model_route=model_route,
            model_id="pydanticai-function-fake",
            prompt_version=input_context.prompt_version,
            schema_version=input_context.schema_version,
            authenticity="FAKE",
            provider_attempted=False,
            provider_reference=None,
            output=output,
            error=error,
            tools=tuple(evidence),
            usage=LLMUsage(requests=0),
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
        )
