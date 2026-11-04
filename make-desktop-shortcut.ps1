<#
.SYNOPSIS
Creates a desktop shortcut named "Pigeon Deterrent" that runs start-dashboard.bat.

.DESCRIPTION
Run this once, yourself:   powershell -ExecutionPolicy Bypass -File .\make-desktop-shortcut.ps1
It writes a single file, "Pigeon Deterrent.lnk", on your desktop. It does not delete or change
anything else. If a shortcut with that name already exists it stops and says so; add -Force to
replace that one shortcut file.
#>
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $repo "start-dashboard.bat"
if (-not (Test-Path -LiteralPath $target)) {
    throw "Cannot find $target. Run this script from the repository it belongs to."
}

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "Pigeon Deterrent.lnk"
if ((Test-Path -LiteralPath $linkPath) -and -not $Force) {
    Write-Host "A shortcut named 'Pigeon Deterrent' already exists on your desktop. Nothing was changed."
    Write-Host "Run again with -Force to replace that one shortcut."
    exit 0
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $repo
$shortcut.Description = "Start the Pigeon Deterrent dashboard server and open it in the browser"
$shortcut.Save()

Write-Host "Created the desktop shortcut: $linkPath"
Write-Host "It runs: $target"
