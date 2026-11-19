"""Private provider wire validation; unknown vendor fields never enter canonical rules."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Wire(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class RawDownload(Wire):
    download_url: str
    expires_at: AwareDatetime


class Attachment(Wire):
    id: UUID
    filename: str | None = None
    content_type: str
    size: int | None = Field(default=None, ge=0)
    content_disposition: Literal["inline", "attachment"] | None = None
    content_id: str | None = None


class EmailEnvelope(Wire):
    id: UUID
    sender: str | None = Field(default=None, alias="from")
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str | None = None
    text: str | None = None
    html: str | None = None
    created_at: AwareDatetime
    message_id: str | None = None
    headers: dict[str, str] | None = None
    attachments: tuple[Attachment, ...] = ()
    raw: RawDownload | None = None
