from enum import StrEnum
from uuid import UUID

from aco.core.types import StrictModel


class APIErrorCode(StrEnum):
    STALE_ACCOUNTING_DATA = "STALE_ACCOUNTING_DATA"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CONTACT_PROHIBITED = "CONTACT_PROHIBITED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    AMBIGUOUS_INVOICE_SCOPE = "AMBIGUOUS_INVOICE_SCOPE"


API_STATUS = {
    APIErrorCode.STALE_ACCOUNTING_DATA: 409,
    APIErrorCode.APPROVAL_REQUIRED: 409,
    APIErrorCode.CONTACT_PROHIBITED: 403,
    APIErrorCode.IDEMPOTENT_REPLAY: 409,
    APIErrorCode.PROVIDER_UNAVAILABLE: 503,
    APIErrorCode.AMBIGUOUS_INVOICE_SCOPE: 422,
}


class APIError(StrictModel):
    code: APIErrorCode
    correlation_id: UUID
    message: str


class ExternalEgressDenied(RuntimeError):
    pass
