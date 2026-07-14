param(
    [string]$InstallRoot = "$env:ProgramData\ProposalAgent",
    [string]$BackupDir = "$env:ProgramData\ProposalAgentBackups"
)
$ErrorActionPreference = "Stop"
$EnvFile = Join-Path $InstallRoot "proposal-agent.env"
if (-not (Test-Path $EnvFile)) { throw "Missing $EnvFile" }
$DataDir = (Get-Content $EnvFile | Where-Object { $_ -match '^APP_DATA_DIR=' } | Select-Object -First 1).Split('=',2)[1].Replace('/','\')
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Stage = Join-Path $env:TEMP "proposal-agent-backup-$Stamp"
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
Copy-Item $DataDir "$Stage\data" -Recurse -Force
Copy-Item $EnvFile "$Stage\proposal-agent.env" -Force
$Zip = Join-Path $BackupDir "proposal-agent-$Stamp.zip"
Compress-Archive -Path "$Stage\*" -DestinationPath $Zip -CompressionLevel Optimal
Remove-Item $Stage -Recurse -Force
Write-Host $Zip
