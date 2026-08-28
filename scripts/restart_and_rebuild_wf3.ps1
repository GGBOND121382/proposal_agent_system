<#
.SYNOPSIS
Restart the local proposal-agent service and rebuild WF-3 without executing it.

.DESCRIPTION
The script performs these operations in order:
1. Finds the process listening on the configured port.
2. Refuses to stop it unless its command line identifies this Uvicorn app.
3. Starts a fresh hidden service process from the repository root.
4. Waits for /api/health to report status=ok and prompt_pack_status=PASS.
5. Calls scripts/rebuild_wf3.py, which cancels the latest active WF-3 and
   creates a replacement with auto_advance=false.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts/restart_and_rebuild_wf3.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts/restart_and_rebuild_wf3.ps1 `
  -ProjectId project-8595abee7b9c4047 -Port 8080
#>

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^project-[0-9a-fA-F]{16}$')]
    [string]$ProjectId = "project-8595abee7b9c4047",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$BindAddress = "127.0.0.1",

    [Parameter()]
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,

    [Parameter()]
    [ValidateRange(5, 300)]
    [int]$StartupTimeoutSeconds = 60,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$PythonLauncher = "py"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$rebuildScript = Join-Path $repositoryRoot "scripts\rebuild_wf3.py"
$environmentFile = Join-Path $repositoryRoot ".env"
$serviceUrl = "http://${BindAddress}:$Port"
$healthUrl = "$serviceUrl/api/health"

if (-not (Test-Path -LiteralPath $rebuildScript -PathType Leaf)) {
    throw "WF-3 rebuild script not found: $rebuildScript"
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw "Environment file not found: $environmentFile"
}

# Resolve the actual interpreter instead of starting py.exe. This makes the
# returned PID and the listener PID refer to the long-running Python process.
$pythonExecutable = (& $PythonLauncher -c "import sys; print(sys.executable)" 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $pythonExecutable) {
    throw "Unable to resolve Python through launcher '$PythonLauncher': $pythonExecutable"
}
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Resolved Python executable does not exist: $pythonExecutable"
}
$pythonExecutable = [System.IO.Path]::GetFullPath($pythonExecutable)

function Get-PortListeners {
    @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-ProcessCommandLine {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId"
    if ($null -eq $processInfo) {
        return $null
    }
    [string]$processInfo.CommandLine
}

$listenerProcessIds = @(Get-PortListeners)
foreach ($listenerProcessId in $listenerProcessIds) {
    $commandLine = Get-ProcessCommandLine -ProcessId $listenerProcessId
    $isProposalService = $commandLine -and
        $commandLine -match "(?i)(?:-m\s+uvicorn|uvicorn(?:\.exe)?)" -and
        $commandLine -match "(?i)app\.main:app"
    if (-not $isProposalService) {
        throw (
            "Port $Port is occupied by PID $listenerProcessId, but it is not the expected " +
            "proposal-agent Uvicorn service. Refusing to stop it. Command line: $commandLine"
        )
    }

    Write-Host "Stopping proposal-agent service PID $listenerProcessId ..."
    Stop-Process -Id $listenerProcessId -Force
}

$portReleaseDeadline = [DateTime]::UtcNow.AddSeconds(15)
# PowerShell unwraps an empty function result to $null. Force the result back
# into an array before reading Count so this remains valid under StrictMode.
while (@(Get-PortListeners).Count -gt 0) {
    if ([DateTime]::UtcNow -ge $portReleaseDeadline) {
        throw "Port $Port did not become available after stopping the old service."
    }
    Start-Sleep -Milliseconds 250
}

$timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$stdoutLog = Join-Path $repositoryRoot "data\service-$timestamp.stdout.log"
$stderrLog = Join-Path $repositoryRoot "data\service-$timestamp.stderr.log"

Write-Host "Starting proposal-agent service from $repositoryRoot ..."
$serviceProcess = Start-Process `
    -FilePath $pythonExecutable `
    -ArgumentList @(
        "-m", "uvicorn", "app.main:app",
        "--env-file", ".env",
        "--host", $BindAddress,
        "--port", [string]$Port
    ) `
    -WorkingDirectory $repositoryRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

$health = $null
$startupDeadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
while ([DateTime]::UtcNow -lt $startupDeadline) {
    $serviceProcess.Refresh()
    if ($serviceProcess.HasExited) {
        $stderrTail = if (Test-Path -LiteralPath $stderrLog) {
            (Get-Content -LiteralPath $stderrLog -Tail 80 | Out-String).Trim()
        } else {
            "(stderr log was not created)"
        }
        throw "Service exited during startup with code $($serviceProcess.ExitCode).`n$stderrTail"
    }

    try {
        $candidateHealth = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 3
        if (
            $candidateHealth.status -eq "ok" -and
            $candidateHealth.prompt_pack_status -eq "PASS"
        ) {
            $health = $candidateHealth
            break
        }
    } catch {
        # Connection failures are expected while Uvicorn is starting.
    }
    Start-Sleep -Milliseconds 500
}

if ($null -eq $health) {
    $stderrTail = if (Test-Path -LiteralPath $stderrLog) {
        (Get-Content -LiteralPath $stderrLog -Tail 80 | Out-String).Trim()
    } else {
        "(stderr log was not created)"
    }
    throw "Service did not become healthy within $StartupTimeoutSeconds seconds.`n$stderrTail"
}

Write-Host "Service is healthy (PID $($serviceProcess.Id)); rebuilding WF-3 ..."
$rebuildOutput = @(
    & $pythonExecutable $rebuildScript `
        --project-id $ProjectId `
        --service-url $serviceUrl 2>&1
)
if ($LASTEXITCODE -ne 0) {
    throw "WF-3 rebuild failed:`n$($rebuildOutput -join [Environment]::NewLine)"
}

$rebuildText = ($rebuildOutput -join [Environment]::NewLine).Trim()
try {
    $rebuildResult = $rebuildText | ConvertFrom-Json
} catch {
    throw "WF-3 rebuild returned non-JSON output:`n$rebuildText"
}

if ($rebuildResult.auto_advanced -ne $false -or $rebuildResult.current_step -ne 0) {
    throw "WF-3 was rebuilt in an unexpected state:`n$rebuildText"
}

[ordered]@{
    service_pid = $serviceProcess.Id
    service_url = $serviceUrl
    runtime_mode = $health.runtime_mode
    prompt_pack_status = $health.prompt_pack_status
    cancelled_workflow_id = $rebuildResult.cancelled_workflow_id
    cancelled_open_gates = $rebuildResult.cancelled_open_gates
    new_workflow_id = $rebuildResult.new_workflow_id
    workflow_status = $rebuildResult.status
    current_step = $rebuildResult.current_step
    auto_advanced = $rebuildResult.auto_advanced
    stdout_log = $stdoutLog
    stderr_log = $stderrLog
} | ConvertTo-Json -Depth 5
