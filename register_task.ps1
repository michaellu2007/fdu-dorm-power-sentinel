# Register a Windows scheduled task that runs main.py --once every 30 minutes.
# Usage: right-click -> Run with PowerShell, or: powershell -ExecutionPolicy Bypass -File register_task.ps1
# Uses schtasks (works without admin rights for the current user's own tasks).
# ASCII only on purpose (PowerShell 5.1 misreads UTF-8 .ps1 without BOM).

$ErrorActionPreference = "Stop"

$py = (Get-Command python -ErrorAction Stop).Source
# Prefer pythonw.exe: no console window pops up every 30 minutes.
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
$here = $PSScriptRoot
$taskName = "FDU-DormPowerSentinel"

$tr = '"' + $pyw + '" "' + (Join-Path $here 'main.py') + '" --once'

schtasks /Create /TN $taskName /TR $tr /SC MINUTE /MO 30 /F
if ($LASTEXITCODE -ne 0) { throw "schtasks /Create failed with $LASTEXITCODE" }

Write-Host "Scheduled task '$taskName' registered (every 30 min)."
Write-Host "Remove it with: schtasks /Delete /TN $taskName /F"

schtasks /Run /TN $taskName
Write-Host "First run started. Check monitor.log for the result."
