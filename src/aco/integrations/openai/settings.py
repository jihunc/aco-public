"""Explicit server-owned configuration; credentials do not grant execution authority."""

from typing import Annotated

from pydantic import Field, SecretStr

from aco.core.types import StrictModel


class OpenAISettings(StrictModel):
    api_key: SecretStr
    model_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")]
    model_route: Annotated[str, Field(min_length=1, max_length=100)]
    # Trusted composition supplies verified capabilities for this exact model.
    features: frozenset[str] = frozenset()
