"""Smoke tests for the project skeleton (T001) and the tool configuration (T002)."""

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_FOLDERS = ("unit", "contract", "integration", "architecture")


def test_deterrent_package_imports():
    import deterrent

    assert deterrent.__name__ == "deterrent"


def test_database_files_are_git_ignored():
    # `git check-ignore -q` exits 0 only when the path IS ignored.
    for path in ("data/x.sqlite3", "data/device/zone_store.sqlite3", "x.sqlite3"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{path} is not git-ignored"


def _pytest_config() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]


def test_pytest_is_configured_to_collect_all_four_test_folders():
    config = _pytest_config()
    assert sorted(config["testpaths"]) == sorted(f"tests/{name}" for name in TEST_FOLDERS)
    for name in TEST_FOLDERS:
        assert (REPO_ROOT / "tests" / name).is_dir()


def test_pytest_async_loop_scope_is_explicit():
    # Removes the pytest-asyncio deprecation warning and keeps async tests predictable.
    assert _pytest_config()["asyncio_default_fixture_loop_scope"] == "function"


def _run_ruff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ruff_check_is_clean():
    result = _run_ruff("check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_ruff_format_check_is_clean():
    result = _run_ruff("format", "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_ci_workflow_runs_the_three_checks_on_linux():
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ubuntu-latest" in text
    assert "ruff check" in text
    assert "ruff format --check" in text
    assert "pytest" in text
    assert "push" in text
