"""Explicit server composition for a separately authorized bounded integration run."""

from collections.abc import Callable
from uuid import UUID

from aco.features.agent.live import BoundedLiveBudget
from aco.features.agent.models import AgentRun
from aco.features.agent.usage import RunModelGuard
from aco.integrations.openai.adapter import OpenAIModelAdapter
from aco.integrations.openai.settings import OpenAISettings
from aco.integrations.ports.llm import LLMPort


class OpenAIInvestigationRuntime:
    def __init__(self, settings: OpenAISettings, grant_id: UUID, kill_switch: Callable[[], bool]):
        self.settings, self.grant_id = settings, grant_id
        self.budget = BoundedLiveBudget(grant_id, kill_switch)

    def model_factory(self, run: AgentRun, guard: RunModelGuard) -> LLMPort:
        if run.model_id != self.settings.model_id or run.model_route != self.settings.model_route:
            raise ValueError("Configured model does not match the persisted run grant")
        return OpenAIModelAdapter(
            tenant_id=run.tenant_id,
            connection_id=run.provider_connection_id,
            settings=self.settings,
            execution_guard=guard,
        )
