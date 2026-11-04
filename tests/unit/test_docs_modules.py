"""The module guide must cover every module in the code (T074).

``docs/platform-core-modules.md`` explains each Python module in plain language. This
test keeps it honest: when a module is added and not documented, or a test file named in
the guide is renamed, this fails.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "deterrent"
GUIDE = REPO / "docs" / "platform-core-modules.md"

MODULES = sorted(
    p.relative_to(REPO).as_posix() for p in SOURCE.rglob("*.py") if p.name != "__init__.py"
)
PACKAGES = sorted(p.parent.relative_to(REPO).as_posix() + "/" for p in SOURCE.rglob("__init__.py"))


@pytest.fixture(scope="module")
def guide() -> str:
    assert GUIDE.is_file(), "docs/platform-core-modules.md is missing"
    return GUIDE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def paragraphs(guide) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", guide) if p.strip()]


def paragraph_for(paragraphs: list[str], path: str) -> str:
    """The paragraph that is about ``path``: it opens with the path in bold."""
    for paragraph in paragraphs:
        if paragraph.startswith(f"**`{path}`**"):
            return paragraph
    raise AssertionError(f"no paragraph opens with **`{path}`**")


def test_the_scan_finds_the_modules_it_should():
    assert len(MODULES) >= 30
    assert "src/deterrent/simulator/arbitration.py" in MODULES
    assert "src/deterrent/composition.py" in MODULES
    assert "src/deterrent/simulator/" in PACKAGES and "src/deterrent/" in PACKAGES


@pytest.mark.parametrize("path", MODULES)
def test_every_module_is_mentioned_by_path(guide, path):
    assert path in guide


@pytest.mark.parametrize("path", MODULES)
def test_every_module_has_its_own_paragraph_with_what_it_never_does_and_its_tests(paragraphs, path):
    paragraph = paragraph_for(paragraphs, path)
    assert "Never:" in paragraph, f"{path}: say what it never does"
    assert "Proven by:" in paragraph, f"{path}: say which tests prove it"
    assert re.search(r"tests/[\w/]+\.py", paragraph), f"{path}: name at least one test file"


@pytest.mark.parametrize("path", PACKAGES)
def test_every_package_is_described(paragraphs, path):
    assert paragraph_for(paragraphs, path)


def test_every_test_file_named_in_the_guide_exists(guide):
    named = set(re.findall(r"tests/[\w/]+\.py", guide))
    assert len(named) >= 30
    missing = sorted(p for p in named if not (REPO / p).is_file())
    assert missing == []


def test_every_module_in_the_guide_still_exists(guide):
    named = set(re.findall(r"src/deterrent/[\w/]+\.py", guide))
    missing = sorted(p for p in named if not (REPO / p).is_file())
    assert missing == []


def test_the_guide_has_the_authority_split_and_the_interview_list(guide):
    assert re.search(r"^## .*safety authority", guide, re.IGNORECASE | re.MULTILINE)
    assert re.search(r"^## .*interview", guide, re.IGNORECASE | re.MULTILINE)
    split = guide.split("safety authority", 1)[1].lower()
    for phrase in ("simulat", "laptop", "decide", "dashboard"):
        assert phrase in split


def test_the_guide_contains_no_code_blocks(guide):
    assert "```" not in guide
    assert "~~~" not in guide


def test_the_interview_list_has_at_least_five_numbered_points(guide):
    section = re.split(r"^## .*interview.*$", guide, flags=re.IGNORECASE | re.MULTILINE)[1]
    assert len(re.findall(r"^\d+\. ", section, re.MULTILINE)) >= 5
