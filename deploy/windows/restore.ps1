param(
    [Parameter(Mandatory=$true)][string]$BackupZip,
    [string]$InstallRoot = "$env:ProgramData\ProposalAgent"
)
$ErrorActionPreference = "Stop"
& "$InstallRoot\stop.ps1" -InstallRoot $InstallRoot
$Temp = Join-Path $env:TEMP ("proposal-agent-restore-" + [guid]::NewGuid())
Expand-Archive $BackupZip -DestinationPath $Temp -Force
$EnvFile = "$Temp\proposal-agent.env"
$DataDir = (Get-Content $EnvFile | Where-Object { $_ -match '^APP_DATA_DIR=' } | Select-Object -First 1).Split('=',2)[1].Replace('/','\')
if (Test-Path $DataDir) { Remove-Item $DataDir -Recurse -Force }
Copy-Item "$Temp\data" $DataDir -Recurse -Force
Copy-Item $EnvFile "$InstallRoot\proposal-agent.env" -Force
Remove-Item $Temp -Recurse -Force
Start-ScheduledTask -TaskName "ProposalAgent" -ErrorAction SilentlyContinue
Write-Host "Restore completed: $BackupZip"
