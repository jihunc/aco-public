"""Conservative local logging. Raw messages/exceptions/payloads are never serialized.

A constant, lowercase, digit-free event phrase (for example "dependency check completed")
is kept so operators can diagnose runtime behavior; anything interpolated or data-bearing
collapses to the generic "runtime.log" event.
"""

import json
import logging
import re
from uuid import UUID

_EVENT = re.compile(r"[a-z][a-z_.: -]{0,79}")


def _event(record: logging.LogRecord) -> str:
    message = record.msg
    if isinstance(message, str) and not record.args and _EVENT.fullmatch(message):
        return message
    return "runtime.log"


class SafeJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        row = {"event": _event(record), "level": record.levelname}
        for key in ("tenant_id", "correlation_id"):
            value = getattr(record, key, None)
            if isinstance(value, UUID):
                row[key] = str(value)
        return json.dumps(row)


def runtime_logger() -> logging.Logger:
    logger = logging.getLogger("aco")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(SafeJSONFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger
