"""Smoke tests for the T001 project skeleton."""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        )
        assert result.returncode == 0, f"{path} is not git-ignored"
