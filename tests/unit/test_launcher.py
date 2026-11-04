"""Tests for the double-click launcher and the desktop-shortcut script (T075 addition).

``start-dashboard.bat`` starts the server in its own window and opens the dashboard.
``make-desktop-shortcut.ps1`` makes a desktop shortcut that runs it; it is run only by the
owner, never by a test. These tests check what the files say, and (on Windows) that the
launcher refuses to start a second server when port 8000 is taken. They never start a
server or open a browser.
"""

import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BAT = REPO / "start-dashboard.bat"
SHORTCUT = REPO / "make-desktop-shortcut.ps1"
EXPECTED_SERVER_COMMAND = (
    "uv run uvicorn deterrent.api.app:create_app --factory --host 127.0.0.1 --port 8000"
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs cmd.exe / PowerShell")


def commands(text: str) -> list[str]:
    """The non-comment, non-empty lines of a batch file."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().lower().startswith("rem ")
    ]


@pytest.fixture(scope="module")
def bat() -> str:
    return BAT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ps1() -> str:
    return SHORTCUT.read_text(encoding="utf-8")


# ---- start-dashboard.bat -----------------------------------------------------------------------


def test_the_launcher_lives_in_the_repository_root():
    assert BAT.parent == REPO and BAT.is_file()


def test_it_changes_to_its_own_folder_first(bat):
    lines = commands(bat)
    assert 'cd /d "%~dp0"' in lines
    # nothing that depends on the current folder comes before it
    assert lines.index('cd /d "%~dp0"') <= 1


def test_it_starts_the_exact_server_command_in_a_window_titled_pigeon_deterrent_server(bat):
    starts = [line for line in commands(bat) if line.lower().startswith("start ")]
    server = [line for line in starts if "uvicorn" in line]
    assert len(server) == 1
    assert server[0].startswith('start "Pigeon Deterrent server" ')
    assert server[0].endswith(EXPECTED_SERVER_COMMAND)
    assert "cmd /k" in server[0]  # the window stays open so errors can be read


def test_it_waits_about_four_seconds_then_opens_the_dashboard_in_the_default_browser(bat):
    lines = commands(bat)
    server = next(i for i, line in enumerate(lines) if "uvicorn" in line)
    wait = next(i for i, line in enumerate(lines) if line.lower().startswith("timeout /t 4"))
    browser = next(i for i, line in enumerate(lines) if line.startswith('start ""'))
    assert server < wait < browser
    assert lines[browser] == 'start "" http://127.0.0.1:8000/'  # empty title: default browser


def test_it_checks_the_port_and_stops_with_a_message_before_starting_anything(bat):
    lines = commands(bat)
    check = next(i for i, line in enumerate(lines) if "netstat" in line and ":8000" in line)
    server = next(i for i, line in enumerate(lines) if "uvicorn" in line)
    assert check < server
    between = " ".join(lines[check:server])
    assert "already in use" in between
    assert "exit /b 1" in between
    assert "NOT started" in between
    assert "pause" in between  # a double-clicked window must not vanish before it is read


def test_it_never_binds_to_other_addresses_or_changes_the_computer(bat):
    executed = commands(bat)
    starts = {line.split()[0].lower() for line in executed}
    assert starts <= {
        "@echo",
        "cd",
        "where",
        "if",
        "echo",
        "pause",
        "exit",
        "netstat",
        "start",
        "timeout",
        ")",
    }
    assert "0.0.0.0" not in bat
    assert "--host 127.0.0.1" in bat


def test_the_file_uses_windows_line_endings():
    assert b"\r\n" in BAT.read_bytes()
    assert b"\n" not in BAT.read_bytes().replace(b"\r\n", b"")


# ---- make-desktop-shortcut.ps1 -----------------------------------------------------------------


def test_the_shortcut_script_makes_a_desktop_shortcut_named_pigeon_deterrent(ps1):
    assert '[Environment]::GetFolderPath("Desktop")' in ps1
    assert '"Pigeon Deterrent.lnk"' in ps1
    assert "CreateShortcut(" in ps1 and ".Save()" in ps1


def test_the_shortcut_points_at_the_launcher_in_this_repository(ps1):
    assert 'Join-Path $repo "start-dashboard.bat"' in ps1
    assert "$shortcut.TargetPath = $target" in ps1
    assert "$shortcut.WorkingDirectory = $repo" in ps1
    assert "Split-Path -Parent $MyInvocation.MyCommand.Path" in ps1


def test_the_shortcut_script_deletes_nothing_and_will_not_overwrite_without_force(ps1):
    lowered = ps1.lower()
    for forbidden in ("remove-item", "del ", "rmdir", "rm ", "-recurse", "set-executionpolicy"):
        assert forbidden not in lowered
    assert "[switch]$Force" in ps1
    assert re.search(r"Test-Path -LiteralPath \$linkPath\) -and -not \$Force", ps1)


@windows_only
def test_the_shortcut_script_is_valid_powershell_without_running_it():
    parse = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SHORTCUT}', "
        "[ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", parse],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---- behaviour: a taken port stops the launcher ------------------------------------------------


@windows_only
def test_with_port_8000_taken_the_launcher_says_so_and_starts_no_second_server():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            holder.bind(("127.0.0.1", 8000))
            holder.listen(1)
        except OSError:
            pass  # something else already holds the port, which is just as good for this test
        result = subprocess.run(
            ["cmd", "/c", str(BAT)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        holder.close()
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Port 8000 is already in use" in output
    assert "NOT started" in output
    assert "uvicorn" not in output.replace("http://127.0.0.1:8000/", "")
