"""Tests for structured logging (T046)."""

import json
import re

import pytest

from deterrent.observability.logging import (
    SEVERITIES,
    StructuredLogger,
    record_to_json,
    trace_context,
)
from deterrent.observability.store import LogRecord

HEX16 = re.compile(r"^[0-9a-f]{16}$")
HEX8 = re.compile(r"^[0-9a-f]{8}$")
TRACE = "0123456789abcdef"
SPAN = "89abcdef"


def make_logger(now: int = 1_700_000_000_000_000):
    submitted: list[LogRecord] = []
    counter = iter(range(1, 10_000))
    logger = StructuredLogger(submitted.append, next_id=lambda: next(counter), clock_us=lambda: now)
    return logger, submitted


def test_every_record_has_timestamp_component_and_severity():
    logger, submitted = make_logger()
    for severity in SEVERITIES:
        logger.log(severity, "api", f"a {severity} message")
    assert [r.severity for r in submitted] == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    for record in submitted:
        assert record.ts_us == 1_700_000_000_000_000
        assert record.component == "api"
        assert record.message


def test_the_shortcut_methods_use_the_right_severity():
    logger, submitted = make_logger()
    logger.debug("api", "d")
    logger.info("api", "i")
    logger.warning("api", "w")
    logger.error("api", "e")
    logger.critical("api", "c")
    assert [r.severity for r in submitted] == list(SEVERITIES)


def test_ids_increase_and_come_from_the_given_source():
    logger, submitted = make_logger()
    logger.info("api", "one")
    logger.info("api", "two")
    assert [r.id for r in submitted] == [1, 2]


def test_trace_and_span_ids_have_the_right_shape_when_given():
    logger, submitted = make_logger()
    logger.info("engagement", "with ids", trace_id=TRACE, span_id=SPAN)
    record = submitted[0]
    assert HEX16.match(record.trace_id) and HEX8.match(record.span_id)


@pytest.mark.parametrize("bad", ["", "xyz", "0123456789ABCDEF", "0123456789abcde", "0" * 17])
def test_a_malformed_trace_id_is_refused(bad):
    logger, submitted = make_logger()
    with pytest.raises(ValueError):
        logger.info("api", "bad", trace_id=bad)
    assert submitted == []


@pytest.mark.parametrize("bad", ["", "zz", "89ABCDEF", "89abcde", "0" * 9])
def test_a_malformed_span_id_is_refused(bad):
    logger, submitted = make_logger()
    with pytest.raises(ValueError):
        logger.info("api", "bad", trace_id=TRACE, span_id=bad)
    assert submitted == []


def test_the_component_is_required():
    logger, submitted = make_logger()
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            logger.info(bad, "no component")
    assert submitted == []


def test_the_severity_must_be_one_of_the_five():
    logger, _ = make_logger()
    with pytest.raises(ValueError):
        logger.log("LOUD", "api", "x")


def test_an_empty_message_is_refused():
    logger, _ = make_logger()
    with pytest.raises(ValueError):
        logger.info("api", "")


def test_trace_context_adds_the_ids_to_every_record_inside_it():
    logger, submitted = make_logger()
    logger.info("api", "before")
    with trace_context(TRACE, SPAN):
        logger.info("engagement", "inside")
        with trace_context(TRACE, "11111111"):
            logger.info("engagement", "nested")
    logger.info("api", "after")
    assert [(r.trace_id, r.span_id) for r in submitted] == [
        (None, None),
        (TRACE, SPAN),
        (TRACE, "11111111"),
        (None, None),
    ]


def test_explicit_ids_win_over_the_context():
    logger, submitted = make_logger()
    with trace_context(TRACE, SPAN):
        logger.info("engagement", "explicit", span_id="22222222")
    assert submitted[0].trace_id == TRACE
    assert submitted[0].span_id == "22222222"


def test_a_bytes_image_field_is_removed_and_the_removal_is_logged():
    logger, submitted = make_logger()
    logger.info("camera", "frame seen", frame_bytes=b"\xff\xd8\xff\xe0" + b"x" * 100, count=3)
    assert len(submitted) == 2
    kept, notice = submitted
    assert kept.message == "frame seen"
    assert kept.fields["count"] == 3
    assert kept.fields["frame_bytes"] == "[REMOVED]"
    assert notice.severity == "WARNING"
    assert notice.component == "logging"
    assert "frame_bytes" in notice.message
    assert "xxxx" not in json.dumps([r.fields for r in submitted]) + notice.message


@pytest.mark.parametrize(
    "name",
    ["image", "jpeg_data", "pixels", "snapshot_png", "footage", "video_clip", "thumbnail"],
)
def test_fields_named_like_image_data_are_removed(name):
    logger, submitted = make_logger()
    logger.info("camera", "x", **{name: "abc"})
    assert submitted[0].fields[name] == "[REMOVED]"


def test_a_data_uri_image_in_a_field_is_removed():
    logger, submitted = make_logger()
    logger.info("api", "x", note="data:image/png;base64,iVBORw0KGgo=")
    assert submitted[0].fields["note"] == "[REMOVED]"


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "PASSWORD",
        "db_passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "api_key",
        "apiKey",
        "authorization",
        "private_key",
        "credentials",
    ],
)
def test_fields_named_like_secrets_are_removed_and_never_stored(name):
    logger, submitted = make_logger()
    logger.info("api", "login", **{name: "hunter2-very-secret"})
    for record in submitted:
        assert "hunter2" not in json.dumps(record.fields)
        assert "hunter2" not in record.message
    assert submitted[0].fields[name] == "[REMOVED]"
    assert submitted[1].component == "logging"


def test_secret_fields_inside_nested_values_are_found():
    logger, submitted = make_logger()
    logger.info("api", "x", detail={"user": "a", "inner": {"password": "pw123"}})
    assert "pw123" not in json.dumps(submitted[0].fields)


def test_secrets_written_into_the_message_text_are_blanked():
    logger, submitted = make_logger()
    logger.info("api", "connecting with password=abc123 and token: xyz789 ok")
    message = submitted[0].message
    assert "abc123" not in message and "xyz789" not in message
    assert "connecting with" in message and "ok" in message


def test_a_message_that_is_image_data_drops_the_whole_record_and_logs_the_drop():
    logger, submitted = make_logger()
    result = logger.info("camera", "data:image/jpeg;base64," + "A" * 200)
    assert result is None
    assert len(submitted) == 1
    assert submitted[0].component == "logging"
    assert submitted[0].severity == "WARNING"
    assert "AAAA" not in submitted[0].message


def test_a_bytes_message_is_dropped():
    logger, submitted = make_logger()
    assert logger.info("camera", b"\xff\xd8binary") is None
    assert len(submitted) == 1 and submitted[0].component == "logging"


def test_ordinary_fields_are_kept_as_they_are():
    logger, submitted = make_logger()
    logger.info("simulator", "moved", pan_cdeg=6000, state="DISARMED", ok=True, ratio=0.5)
    assert submitted[0].fields == {"pan_cdeg": 6000, "state": "DISARMED", "ok": True, "ratio": 0.5}


def test_recent_returns_the_newest_records_oldest_first():
    logger, _ = make_logger()
    for index in range(60):
        logger.info("api", f"m{index}")
    recent = logger.recent(50)
    assert len(recent) == 50
    assert [r.message for r in recent][0] == "m10" and [r.message for r in recent][-1] == "m59"


def test_listeners_hear_every_accepted_record():
    logger, _ = make_logger()
    heard: list[LogRecord] = []
    logger.subscribe(heard.append)
    logger.info("api", "one")
    logger.info("camera", "x", password="p")
    assert [r.message for r in heard][0] == "one"
    assert len(heard) == 3  # the second record and its removal notice


def test_a_listener_that_fails_does_not_stop_logging():
    logger, submitted = make_logger()

    def broken(record: LogRecord) -> None:
        raise RuntimeError("listener bug")

    logger.subscribe(broken)
    logger.info("api", "still works")
    assert [r.message for r in submitted] == ["still works"]


def test_a_record_is_one_line_of_json_with_the_documented_keys():
    logger, submitted = make_logger()
    logger.info("engagement", "hello", trace_id=TRACE, span_id=SPAN, a=1)
    line = record_to_json(submitted[0])
    assert "\n" not in line
    data = json.loads(line)
    assert data == {
        "id": 1,
        "ts_us": 1_700_000_000_000_000,
        "component": "engagement",
        "severity": "INFO",
        "message": "hello",
        "trace_id": TRACE,
        "span_id": SPAN,
        "fields": {"a": 1},
    }


def test_the_module_does_not_shadow_or_need_the_standard_logging_package():
    import deterrent.observability.logging as ours

    assert ours.__name__ == "deterrent.observability.logging"


def test_remember_adds_a_record_made_elsewhere_to_the_live_list():
    logger, submitted = make_logger()
    heard: list[LogRecord] = []
    logger.subscribe(heard.append)
    outside = LogRecord(99, 5, "store", "ERROR", "storage fault")
    logger.remember(outside)
    assert logger.recent(1) == [outside]
    assert heard == [outside]
    assert submitted == []  # the store already queued it itself
