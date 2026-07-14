param(
    [string]$InstallRoot = "$env:ProgramData\ProposalAgent"
)
$ErrorActionPreference = "Stop"
$EnvFile = Join-Path $InstallRoot "proposal-agent.env"
if (-not (Test-Path $EnvFile)) { throw "Missing $EnvFile" }
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $name, $value = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
    }
}
Set-Location $InstallRoot
$Python = Join-Path $InstallRoot ".venv\Scripts\python.exe"
& $Python -m uvicorn app.main:app --host $env:APP_HOST --port $env:APP_PORT --workers 1
