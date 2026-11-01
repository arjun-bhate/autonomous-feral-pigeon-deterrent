"""Tests for the written protocol document (T030).

``protocol/PROTOCOL.md`` is the one copy of the protocol. These tests keep it honest:
it is in the right place, nothing still links to the old place, its tables agree with
the machine-readable files, and its worked example is exactly what the code produces.
"""

import json
import re
import tomllib
from pathlib import Path

from deterrent.protocol.example import (
    BEGIN_MARKER,
    END_MARKER,
    fire_request_example,
    render_markdown,
)
from deterrent.protocol.framing import Frame, parse_frame
from deterrent.protocol.messages import FireRequest, decode_host_message

ROOT = Path(__file__).resolve().parents[2]
NEW_PATH = ROOT / "protocol" / "PROTOCOL.md"
OLD_PATH = ROOT / "specs" / "001-platform-core" / "contracts" / "protocol.md"
SKIPPED_DIRECTORIES = {
    ".git",
    ".venv",
    ".hypothesis",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _markdown_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.md")
        if not SKIPPED_DIRECTORIES & set(path.relative_to(ROOT).parts)
    ]


def _section(text: str, heading: str) -> str:
    """The text from ``heading`` up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading)
    following = re.search(rf"^#{{1,{level}}} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def _table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not set(line) <= set("|-: "):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def test_document_is_in_protocol_folder_and_old_copy_is_gone():
    assert NEW_PATH.is_file()
    assert not OLD_PATH.exists()


def test_no_markdown_link_points_to_the_old_path():
    broken = []
    for path in _markdown_files():
        for target in _LINK.findall(path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith(("mailto:", "#")):
                continue
            resolved = (path.parent / target.split("#")[0]).resolve()
            if resolved == OLD_PATH.resolve():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert broken == []


def test_input_line_of_tasks_names_the_new_path():
    first_lines = (ROOT / "specs/001-platform-core/tasks.md").read_text(encoding="utf-8")[:1500]
    assert "protocol/PROTOCOL.md" in first_lines
    assert "contracts/protocol.md" not in first_lines


def test_links_inside_the_moved_document_still_resolve():
    text = NEW_PATH.read_text(encoding="utf-8")
    for target in _LINK.findall(text):
        if "://" in target or target.startswith("#"):
            continue
        assert (NEW_PATH.parent / target.split("#")[0]).resolve().exists(), target


def test_reason_table_equals_reason_codes_json():
    text = NEW_PATH.read_text(encoding="utf-8")
    table = _section(text, "## 6. Fire denial reasons: priority and permanent codes")
    document = set()
    for row in _table_rows(table):
        if len(row) >= 4 and re.fullmatch(r"[A-Z_]+", row[1]) and row[2].isdigit():
            document.add((row[1], int(row[2]), row[3].strip("*")))
    file = json.loads((ROOT / "protocol/reason-codes.json").read_text(encoding="utf-8"))
    from_file = {(r["name"], r["code"], r["status"]) for r in file["reasons"]}
    assert len(document) == 15
    assert document == from_file


def test_every_default_in_section_8_equals_config_default_toml():
    text = NEW_PATH.read_text(encoding="utf-8")
    table = _section(text, "## 8. Timing rules and defaults")
    config = tomllib.loads((ROOT / "config/default.toml").read_text(encoding="utf-8"))
    checked = set()
    for row in _table_rows(table):
        names = [n.strip() for n in row[0].replace("`", "").split("/")]
        if not all(re.fullmatch(r"[a-z_]+", n) for n in names) or names[0] not in config:
            continue
        values = [int(v) for v in re.findall(r"\d+", row[1].split("(")[0])]
        assert values == [config[n] for n in names], row
        checked.update(names)
    # Every configured value except the elevation limits (section 6) and dev_tools is in
    # section 8, and each one was compared.
    assert checked == set(config) - {"elevation_min_cdeg", "elevation_max_cdeg", "dev_tools"}


def test_worked_example_in_document_is_what_the_code_produces_now():
    text = NEW_PATH.read_text(encoding="utf-8")
    assert text.count(BEGIN_MARKER) == 1 and text.count(END_MARKER) == 1
    inside = text.split(BEGIN_MARKER)[1].split(END_MARKER)[0].strip("\n")
    assert inside == render_markdown().strip("\n")


def test_example_frame_decodes_to_the_message_it_describes():
    example = fire_request_example()
    frame = parse_frame(example.wire[:-1])
    assert isinstance(frame, Frame)
    assert frame.seq == example.seq
    message = decode_host_message(frame.type, frame.payload)
    assert message == example.message
    assert isinstance(message, FireRequest)
    assert b"\x00" not in example.wire[:-1]  # COBS leaves no zero inside the body
    assert example.wire[-1] == 0
