"""Can someone build the messages from the protocol document alone? (T043, SC-018)

``independent_encoder.py`` was written only from ``protocol/PROTOCOL.md``: its own COBS,
its own CRCs, its own message layouts, and it reads the type numbers and the reason and
reject codes out of the document's tables. It never imports ``deterrent.protocol``.

This file builds the messages of five named vectors with it and compares:

* the bytes with the bytes the reference encoder produced for the same vector,
* the replies the document predicts with the bytes the simulator really sends.

If the document and the reference encoder disagree on a single byte, a test here fails.
"""

import re
import zlib
from pathlib import Path

import independent_encoder as ind
import pytest
from hypothesis import given
from hypothesis import strategies as st
from simulator_adapter import SimulatorAdapter
from vector_runner import load_vector_files, run_vector

from deterrent.protocol.crc import crc16_ccitt_false
from deterrent.protocol.framing import build_frame, cobs_encode
from deterrent.protocol.messages import FireRequest, FireStop, Move, host_frame

DOCUMENT = Path(__file__).resolve().parents[2] / "protocol" / "PROTOCOL.md"
VECTORS = {vector["id"]: vector for _file, vector in load_vector_files()}


# ---- the independent pieces are right by themselves (published check values) --------------------


def test_crc16_ccitt_false_matches_the_published_check_value():
    assert ind.crc16(b"123456789") == 0x29B1  # the standard check value for this CRC


def test_crc32_matches_the_published_check_value():
    assert ind.crc32(b"123456789") == 0xCBF43926


@pytest.mark.parametrize(
    ("plain", "stuffed"),
    [
        (b"\x00", b"\x01\x01"),
        (b"\x00\x00", b"\x01\x01\x01"),
        (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"),
        (b"\x11\x22\x33\x44", b"\x05\x11\x22\x33\x44"),
        (b"\x11\x00\x00\x00", b"\x02\x11\x01\x01\x01"),
    ],
)
def test_cobs_matches_the_published_examples(plain, stuffed):
    assert ind.cobs_encode(plain) == stuffed
    assert ind.cobs_decode(stuffed) == plain


@given(st.binary(max_size=64))
def test_the_independent_cobs_and_crc_agree_with_the_reference_on_any_bytes(data):
    assert ind.cobs_encode(data) == cobs_encode(data)
    assert ind.cobs_decode(ind.cobs_encode(data)) == data
    assert ind.crc16(data) == crc16_ccitt_false(data)
    assert ind.crc32(data) == zlib.crc32(data)


@given(st.integers(0, 255), st.integers(0, 65535), st.binary(max_size=59))
def test_the_independent_frame_equals_the_reference_frame(msg_type, seq, payload):
    assert ind.frame(msg_type, seq, payload) == build_frame(msg_type, seq, payload)


# ---- the document's own worked example can be rebuilt from its description ----------------------


def _worked_example_hex() -> dict[str, str]:
    text = DOCUMENT.read_text(encoding="utf-8")
    example = text[text.index("## 11. Worked byte-level example") :]
    found = {}
    for label, key in (
        ("4. Raw frame", "request_raw"),
        ("5. COBS-encoded", "request_wire"),
        ("- Raw frame:", "reply_raw"),
        ("- On the wire:", "reply_wire"),
    ):
        line = next(line for line in example.splitlines() if label in line)
        found[key] = re.findall(r"`([0-9A-F ]+)`", line)[-1]
    return found


def test_the_worked_example_in_the_document_is_rebuilt_byte_for_byte():
    shown = _worked_example_hex()
    raw = ind.raw_frame(0x20, 7, ind.fire_request_payload(1000, 500, 12000, 4000, 500))
    assert ind.hexs(raw) == shown["request_raw"]
    assert (
        ind.hexs(ind.frame(0x20, 7, ind.fire_request_payload(1000, 500, 12000, 4000, 500)))
        == (shown["request_wire"])
    )
    reply = ind.predicted_denied(request_type=0x20, seq=7, reason="KEY_OFF")
    assert ind.hexs(reply) == shown["reply_wire"]
    assert ind.hexs(ind.cobs_decode(reply[:-1])) == shown["reply_raw"]


# ---- the document's tables are what the encoder reads -------------------------------------------


def test_message_types_and_codes_are_read_from_the_document():
    tables = ind.load_tables()
    assert tables.message_types["FIRE_REQUEST"] == 0x20
    assert tables.message_types["FIRE_STOP"] == 0x21
    assert tables.denial_codes["KEY_OFF"] == 5
    assert tables.denial_codes["COOLDOWN_ACTIVE"] == 15
    assert tables.denial_codes["PERSON_CLEAR_DELAY"] == 14
    assert tables.reject_codes["ZONE_NO_UPLOAD"] == 20
    assert tables.reject_codes["BAD_CRC"] == 2
    assert len(tables.denial_codes) == 15 and len(tables.message_types) == 15


def test_a_changed_document_changes_the_bytes_so_the_comparison_would_fail():
    text = DOCUMENT.read_text(encoding="utf-8")
    changed = ind.load_tables(text.replace("| 0x21 | FIRE_STOP |", "| 0x22 | FIRE_STOP |"))
    assert changed.message_types["FIRE_STOP"] == 0x22
    assert ind.fire_stop(1, changed) != host_frame(FireStop(), 1)
    assert ind.fire_stop(1) == host_frame(FireStop(), 1)  # the real document agrees

    changed = ind.load_tables(
        text.replace("| 5 | KEY_OFF | 5 | active |", "| 5 | KEY_OFF | 6 | active |")
    )
    assert ind.predicted_denied(0x20, 1, "KEY_OFF", tables=changed) != ind.predicted_denied(
        0x20, 1, "KEY_OFF"
    )


# ---- five named vectors: bytes and replies ------------------------------------------------------


class RecordingAdapter(SimulatorAdapter):
    """The simulator adapter that remembers the bytes the runner sent and what came back."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[bytes] = []

    def reset(self, config) -> None:
        super().reset(config)
        self.sent = []

    def send(self, data: bytes):
        self.sent.append(data)
        return super().send(data)


def reference_bytes(vector_id: str) -> list[bytes]:
    adapter = RecordingAdapter()
    result = run_vector(VECTORS[vector_id], adapter)
    assert result.passed
    return adapter.sent


def replay(vector_id: str, wires: list[tuple[int, bytes]]) -> list[list[bytes]]:
    """Put the vector's starting situation on a fresh simulator, then send the hand-built
    messages at their times. Returns, for each, every frame the device sent in answer."""
    vector = VECTORS[vector_id]
    adapter = SimulatorAdapter()
    adapter.reset(vector.get("config", {}))
    adapter.apply_initial(vector.get("initial", {}))
    answers = []
    for at_ms, wire in wires:
        adapter.advance_to(at_ms)
        answers.append(adapter.send(wire))
    return answers


def replies_only(frames: list[bytes]) -> list[bytes]:
    """Replies are everything except the unsolicited events (types 0xC0 to 0xC2)."""
    return [f for f in frames if not 0xC0 <= ind.decode(f)["type"] <= 0xC2]


def events_only(frames: list[bytes]) -> list[dict]:
    return [ind.decode(f) for f in frames if 0xC0 <= ind.decode(f)["type"] <= 0xC2]


def test_denial_vector_fire_deny_key_off():
    vector = VECTORS["fire.deny.key_off"]
    fields = vector["steps"][0]["message"]["fields"]
    mine = ind.fire_request(
        1, fields["issued_at_ms"], fields["ttl_ms"], 0, 0, fields["duration_ms"]
    )
    assert [mine] == reference_bytes("fire.deny.key_off")
    assert mine == host_frame(FireRequest(0, 500, 0, 0, 500), 1)
    [answer] = replay("fire.deny.key_off", [(0, mine)])
    assert replies_only(answer) == [ind.predicted_denied(0x20, 1, "KEY_OFF")]


def test_zone_upload_vector_zone_setup_exit1_commit_ok():
    vector_id = "zone.setup.exit1.commit_ok"
    points = [(3000, 1000), (4000, 1000), (4000, 2000), (3000, 2000)]
    crc = ind.zone_crc32(2, points)
    messages = [ind.zone_begin(1, 2, 4)]
    messages += [ind.zone_point(2 + i, i, pan, tilt) for i, (pan, tilt) in enumerate(points)]
    messages += [ind.zone_commit(6, crc), ind.zone_list(7), ind.zone_point(8, 0, 0, 0)]
    assert messages == reference_bytes(vector_id)
    steps = [s for s in VECTORS[vector_id]["steps"] if s["action"] == "send"]
    times = [s["at_ms"] for s in steps]
    assert len(times) == len(messages)
    answers = replay(vector_id, list(zip(times, messages, strict=True)))
    predicted = [
        ind.predicted_ok(t, i + 1) for i, t in enumerate([0x10, 0x11, 0x11, 0x11, 0x11, 0x12])
    ]
    predicted += [
        ind.predicted_ok(0x14, 7, ind.zone_list_data([(2, 4, crc)])),
        ind.predicted_rejected(0x11, 8, "ZONE_NO_UPLOAD"),
    ]
    assert [replies_only(a) for a in answers] == [[p] for p in predicted]


def test_duplicate_vector_seq_duplicate_replays_reply_acts_once():
    vector_id = "seq.duplicate_replays_reply_acts_once"
    first = ind.move(20, 1000, 0)
    second = ind.move(21, 2000, 0)
    duplicate = ind.move(20, 1000, 0)
    assert [first, second, duplicate] == reference_bytes(vector_id)
    assert first == host_frame(Move(1000, 0), 20)
    times = [s["at_ms"] for s in VECTORS[vector_id]["steps"]]
    answers = replay(vector_id, list(zip(times, [first, second, duplicate], strict=True)))
    reply_first = ind.predicted_ok(0x02, 20, ind.angles_data(1000, 0))
    reply_second = ind.predicted_ok(0x02, 21, ind.angles_data(2000, 0))
    # The repeat of number 20 is answered with the very same bytes as the first time,
    # not with a new reply about where the head is now.
    assert [replies_only(a) for a in answers] == [[reply_first], [reply_second], [reply_first]]


def test_fire_stop_vector_fire_stop_idle_ok_no_change():
    vector_id = "fire_stop.idle_ok_no_change"
    stop = ind.fire_stop(1)
    request = ind.fire_request(2, 0, 500, 0, 0, 500)
    assert [stop, request] == reference_bytes(vector_id)
    assert stop == host_frame(FireStop(), 1)
    answers = replay(vector_id, [(0, stop), (0, request)])
    assert replies_only(answers[0]) == [ind.predicted_ok(0x21, 1)]
    assert replies_only(answers[1]) == [ind.predicted_granted(2, granted_on_ms=0)]
    [event] = [e for e in events_only(answers[1]) if e["type"] == 0xC2]
    assert event["payload"] == bytes([1, 0])  # the effector came on; no cause


def test_corrupted_frame_vector_framing_bad_crc_not_acted_on():
    vector_id = "framing.bad_crc_not_acted_on"
    good = ind.move(5, 100, 0)
    bad = ind.with_wrong_crc(good)
    [recorded] = reference_bytes(vector_id)
    assert bad == recorded
    assert bad != good and ind.decode(good)["type"] == 0x02
    with pytest.raises(ValueError, match="CRC"):
        ind.decode(bad)
    [answer] = replay(vector_id, [(0, bad)])
    assert replies_only(answer) == []  # no reply to a frame that fails the checksum
    notices = [e for e in events_only(answer) if e["type"] == 0xC1]
    cause = ind.load_tables().reject_codes["BAD_CRC"]
    assert [n["payload"] for n in notices] == [bytes([cause, 1, 0])]  # cause, total count 1


def test_the_independent_encoder_imports_nothing_from_the_reference_code():
    source = Path(ind.__file__).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports and not any("deterrent" in line for line in imports)
