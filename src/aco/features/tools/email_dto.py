"""Untrusted email text is a draft, never destination, spend or dispatch authority."""

from uuid import UUID

from pydantic import Field, field_validator

from aco.core.types import StrictModel


class DraftCustomerEmail(StrictModel):
    invoice_id: UUID
    subject: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=20000)

    @field_validator("subject")
    @classmethod
    def header_text(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Plain nonblank single-line subject required")
        return value

    @field_validator("content")
    @classmethod
    def body_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Nonblank plain-text body required")
        return value
