"""Strict human task and persistent queue HTTP contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from aco.core.types import StrictModel

TaskStatus = Literal["open", "in_progress", "done"]


class TaskCreate(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=4000)
    assignee_id: UUID | None = None
    invoice_id: UUID | None = None
    priority: int = Field(default=1, ge=0, le=3)
    due_at: AwareDatetime | None = None

    @field_validator("title")
    @classmethod
    def title_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a task title")
        return value

    @field_validator("assignee_id", "invoice_id", mode="before")
    @classmethod
    def uuid_wire(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value

    @field_validator("due_at", mode="before")
    @classmethod
    def due_wire(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value


class TaskPatch(StrictModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    notes: str | None = Field(default=None, max_length=4000)
    assignee_id: UUID | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    due_at: AwareDatetime | None = None
    status: TaskStatus | None = None

    @field_validator("title", "notes", "priority", "status", mode="before")
    @classmethod
    def required_when_present(cls, value: object) -> object:
        if value is None:
            raise ValueError("This task field cannot be null")
        return value

    @field_validator("title")
    @classmethod
    def title_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Enter a task title")
        return value

    @field_validator("assignee_id", mode="before")
    @classmethod
    def uuid_wire(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value

    @field_validator("due_at", mode="before")
    @classmethod
    def due_wire(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value


class TaskDelete(StrictModel):
    expected_version: int = Field(ge=1)


class TaskView(StrictModel):
    id: UUID
    invoice_id: UUID | None
    source_action_id: UUID | None
    creator_id: UUID
    assignee_id: UUID | None
    title: str
    notes: str
    status: TaskStatus
    priority: int
    priority_rank: str
    due_at: datetime | None
    deleted_at: datetime | None
    version: int
    origin: Literal["human", "generated"]
    deadline_locked: bool


class TaskPage(StrictModel):
    items: list[TaskView]


class QueueReorder(StrictModel):
    ids: list[UUID] = Field(min_length=1, max_length=1000)
    expected_version: int = Field(ge=1)

    @field_validator("ids")
    @classmethod
    def unique_ids(cls, ids: list[UUID]) -> list[UUID]:
        if len(set(ids)) != len(ids):
            raise ValueError("Task IDs must be unique")
        return ids

    @field_validator("ids", mode="before")
    @classmethod
    def ids_wire(cls, value: object) -> object:
        if isinstance(value, list):
            return [UUID(item) if isinstance(item, str) else item for item in value]
        return value


class QueueView(StrictModel):
    version: int
    items: list[TaskView]


class TaskAssignee(StrictModel):
    user_id: UUID
    email: str
