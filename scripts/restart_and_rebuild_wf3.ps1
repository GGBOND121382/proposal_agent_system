<#
.SYNOPSIS
Restart the local proposal-agent service and rebuild WF-3 without executing it.

.DESCRIPTION
The script performs these operations in order:
1. Ensures a local SearXNG JSON API is running when hybrid/SearXNG search is configured.
2. Finds the process listening on the configured application port.
3. Refuses to stop it unless its command line identifies this Uvicorn app.
4. Starts a fresh hidden service process from the repository root.
5. Waits for /api/health to report status=ok and prompt_pack_status=PASS.
6. Calls scripts/rebuild_wf3.py, which cancels the latest active WF-3 and
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
    [ValidateRange(10, 600)]
    [int]$SearxngStartupTimeoutSeconds = 180,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$SearxngImage = "docker.io/searxng/searxng:latest",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$SearxngContainerName = "proposal-agent-searxng-local",

    [Parameter()]
    [switch]$SkipSearxng,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$PythonLauncher = "py"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$rebuildScript = Join-Path $repositoryRoot "scripts\rebuild_wf3.py"
$environmentFile = Join-Path $repositoryRoot ".env"
$searxngSettingsFile = Join-Path $repositoryRoot "deploy\local\searxng\settings.yml"
$serviceUrl = "http://${BindAddress}:$Port"
$healthUrl = "$serviceUrl/api/health"

if (-not (Test-Path -LiteralPath $rebuildScript -PathType Leaf)) {
    throw "WF-3 rebuild script not found: $rebuildScript"
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw "Environment file not found: $environmentFile"
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$DefaultValue = ""
    )

    foreach ($line in Get-Content -LiteralPath $environmentFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim('"').Trim("'")
        }
    }
    return $DefaultValue
}

function Test-SearxngJsonApi {
    param([Parameter(Mandatory = $true)][string]$BaseUrl)

    try {
        $probe = Invoke-RestMethod `
            -Uri "$($BaseUrl.TrimEnd('/'))/search?q=proposal-agent-healthcheck&format=json" `
            -Method Get `
            -TimeoutSec 15
        return $null -ne $probe.PSObject.Properties["results"]
    } catch {
        return $false
    }
}

function Test-DockerDaemon {
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    $serverVersion = (& docker info --format "{{.ServerVersion}}" 2>$null | Out-String).Trim()
    return $LASTEXITCODE -eq 0 -and [bool]$serverVersion
}

function Wait-DockerDaemon {
    param([Parameter(Mandatory = $true)][int]$TimeoutSeconds)

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-DockerDaemon) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Docker daemon did not become ready within $TimeoutSeconds seconds. Start Docker Desktop and rerun."
}

function Ensure-Searxng {
    if ($SkipSearxng) {
        Write-Host "Skipping SearXNG startup because -SkipSearxng was specified."
        return $null
    }

    $provider = (Get-DotEnvValue -Name "PUBLIC_SEARCH_PROVIDER" -DefaultValue "disabled").ToLowerInvariant()
    if ($provider -notin @("searxng", "hybrid")) {
        Write-Host "PUBLIC_SEARCH_PROVIDER=$provider does not require SearXNG; skipping it."
        return $null
    }

    $baseUrl = Get-DotEnvValue -Name "PUBLIC_SEARCH_BASE_URL"
    if (-not $baseUrl) {
        throw "PUBLIC_SEARCH_PROVIDER=$provider requires PUBLIC_SEARCH_BASE_URL."
    }
    try {
        $uri = [Uri]$baseUrl
    } catch {
        throw "PUBLIC_SEARCH_BASE_URL is not a valid URL: $baseUrl"
    }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne "http") {
        throw "Automated local SearXNG startup requires an absolute http URL, got: $baseUrl"
    }

    if (Test-SearxngJsonApi -BaseUrl $baseUrl) {
        Write-Host "SearXNG JSON API is already healthy at $baseUrl."
        return $baseUrl
    }

    if ($uri.Host -notin @("127.0.0.1", "localhost", "::1")) {
        throw "External SearXNG is unreachable at $baseUrl; the script will not replace an external service."
    }
    if (-not (Test-Path -LiteralPath $searxngSettingsFile -PathType Leaf)) {
        throw "Local SearXNG settings file not found: $searxngSettingsFile"
    }
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI is required to start local SearXNG but was not found."
    }

    if (-not (Test-DockerDaemon)) {
        $programFiles = [Environment]::GetFolderPath("ProgramFiles")
        $dockerDesktop = Join-Path $programFiles "Docker\Docker\Docker Desktop.exe"
        if (-not (Test-Path -LiteralPath $dockerDesktop -PathType Leaf)) {
            throw "Docker daemon is unavailable and Docker Desktop was not found at $dockerDesktop."
        }
        Write-Host "Starting Docker Desktop for local SearXNG ..."
        Start-Process -FilePath $dockerDesktop -WindowStyle Hidden | Out-Null
        Wait-DockerDaemon -TimeoutSeconds $SearxngStartupTimeoutSeconds
    }

    $existingContainerId = (
        & docker container ls --all --quiet --filter "name=^/$SearxngContainerName$" 2>&1 |
            Out-String
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect Docker containers: $existingContainerId"
    }

    if ($existingContainerId) {
        $managedLabel = (& docker inspect --format '{{index .Config.Labels "proposal-agent.component"}}' $existingContainerId 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect ownership of SearXNG container ${SearxngContainerName}: $managedLabel"
        }
        if ($managedLabel -ne "searxng-local") {
            throw (
                "Container $SearxngContainerName already exists but is not managed by this script. " +
                "Refusing to alter it. Use a different -SearxngContainerName or restore its JSON API."
            )
        }
        $running = (& docker inspect --format "{{.State.Running}}" $existingContainerId 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect SearXNG container ${SearxngContainerName}: $running"
        }
        if ($running -ne "true") {
            Write-Host "Starting existing SearXNG container $SearxngContainerName ..."
            $startOutput = (& docker start $existingContainerId 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) {
                throw "Unable to start SearXNG container ${SearxngContainerName}: $startOutput"
            }
        } else {
            # The probe already failed, so restart only the container carrying
            # our ownership label to reload its mounted configuration.
            Write-Host "Restarting unhealthy managed SearXNG container $SearxngContainerName ..."
            $restartOutput = (& docker restart $existingContainerId 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) {
                throw "Unable to restart SearXNG container ${SearxngContainerName}: $restartOutput"
            }
        }
    } else {
        $searxngPort = if ($uri.IsDefaultPort) { 80 } else { $uri.Port }
        $portOwner = @(
            Get-NetTCPConnection -State Listen -LocalPort $searxngPort -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
        if ($portOwner.Count -gt 0) {
            throw "Port $searxngPort is occupied by PID(s) $($portOwner -join ', '), but no working SearXNG JSON API was found."
        }

        $publish = "127.0.0.1:${searxngPort}:8080"
        $settingsMount = "type=bind,source=$searxngSettingsFile,target=/etc/searxng/settings.yml,readonly"
        $secret = [Guid]::NewGuid().ToString("N")
        Write-Host "Creating local SearXNG container $SearxngContainerName on $baseUrl ..."
        $runOutput = (
            & docker run --detach `
                --name $SearxngContainerName `
                --label "proposal-agent.component=searxng-local" `
                --publish $publish `
                --mount $settingsMount `
                --env "SEARXNG_SECRET=$secret" `
                --restart unless-stopped `
                $SearxngImage 2>&1 |
                Out-String
        ).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to create local SearXNG container: $runOutput"
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($SearxngStartupTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-SearxngJsonApi -BaseUrl $baseUrl) {
            Write-Host "SearXNG JSON API is healthy at $baseUrl."
            return $baseUrl
        }
        Start-Sleep -Seconds 1
    }

    $logTail = (& docker logs --tail 80 $SearxngContainerName 2>&1 | Out-String).Trim()
    throw "SearXNG did not expose a working JSON API within $SearxngStartupTimeoutSeconds seconds.`n$logTail"
}

$searxngUrl = Ensure-Searxng

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
    searxng_url = $searxngUrl
    searxng_container = if ($searxngUrl) { $SearxngContainerName } else { $null }
    cancelled_workflow_id = $rebuildResult.cancelled_workflow_id
    cancelled_open_gates = $rebuildResult.cancelled_open_gates
    new_workflow_id = $rebuildResult.new_workflow_id
    workflow_status = $rebuildResult.status
    current_step = $rebuildResult.current_step
    auto_advanced = $rebuildResult.auto_advanced
    stdout_log = $stdoutLog
    stderr_log = $stderrLog
} | ConvertTo-Json -Depth 5
