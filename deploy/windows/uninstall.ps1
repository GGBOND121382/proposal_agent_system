param(
    [string]$InstallRoot = "$env:ProgramData\ProposalAgent",
    [switch]$KeepData
)
$ErrorActionPreference = "Stop"
Unregister-ScheduledTask -TaskName "ProposalAgent" -Confirm:$false -ErrorAction SilentlyContinue
if (Test-Path "$InstallRoot\stop.ps1") { & "$InstallRoot\stop.ps1" -InstallRoot $InstallRoot }
Remove-Item $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
if (-not $KeepData) { Remove-Item "$env:ProgramData\ProposalAgentData" -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host "Proposal Agent removed."
