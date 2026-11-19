"""Catch ordinary structured logs retaining sensitive exception/message contents."""

import json
import logging

from aco.core.logging import SafeJSONFormatter


def test_default_structured_logs_discard_sensitive_message_and_exception() -> None:
    record = logging.LogRecord(
        "aco", logging.ERROR, "", 0, "secret-token synthetic@example.invalid invoice-body", (), None
    )
    result = SafeJSONFormatter().format(record)
    assert json.loads(result) == {"event": "runtime.log", "level": "ERROR"}
    assert "secret-token" not in result


def _format(msg: object, args: tuple[object, ...] = ()) -> dict[str, str]:
    record = logging.LogRecord("aco", logging.INFO, "", 0, msg, args, None)
    return json.loads(SafeJSONFormatter().format(record))  # type: ignore[no-any-return]


def test_constant_event_message_is_kept_for_diagnosis() -> None:
    assert _format("dependency check completed") == {
        "event": "dependency check completed",
        "level": "INFO",
    }


def test_interpolated_or_data_bearing_messages_stay_redacted() -> None:
    assert _format("user %s signed in", ("synthetic@example.invalid",))["event"] == "runtime.log"
    assert _format("invoice 0dbd6dff-868b-4c81 balance 10000")["event"] == "runtime.log"
    assert _format("Login for Synthetic@Example.invalid")["event"] == "runtime.log"
    assert _format(ValueError("secret"))["event"] == "runtime.log"
    assert _format("x" * 81)["event"] == "runtime.log"
