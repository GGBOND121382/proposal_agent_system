param([ValidateSet("offline", "hybrid")][string]$Mode = "offline")
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Manifest = Get-Content "$Root\manifest.json" -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($Item in $Manifest.files) {
    $Path = Join-Path $Root ($Item.path -replace '/', '\')
    if (-not (Test-Path $Path)) { throw "Bundle file missing: $($Item.path)" }
    $Hash = (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Hash -ne $Item.sha256.ToLowerInvariant()) { throw "Hash mismatch: $($Item.path)" }
}
docker load -i "$Root\proposal-agent-image.tar"
if (Test-Path "$Root\searxng-image.tar") { docker load -i "$Root\searxng-image.tar" }
if ($Mode -eq "hybrid" -and -not (Test-Path "$Root\searxng-image.tar")) { throw "Hybrid bundle does not contain searxng-image.tar" }
New-Item -ItemType Directory -Force -Path "$Root\data", "$Root\searxng" | Out-Null
if (-not (Test-Path "$Root\proposal-agent.env")) {
    Copy-Item "$Root\proposal-agent.env.example" "$Root\proposal-agent.env"
    (Get-Content "$Root\proposal-agent.env") `
        -replace '^APP_DATA_DIR=.*','APP_DATA_DIR=/var/lib/proposal-agent' `
        -replace '^PROMPT_PACK_DIR=.*','PROMPT_PACK_DIR=/app/prompt_pack' `
        -replace '^MERMAID_BROWSER_EXECUTABLE=.*','MERMAID_BROWSER_EXECUTABLE=/usr/bin/chromium' |
        Set-Content "$Root\proposal-agent.env" -Encoding UTF8
    Write-Warning "Edit $Root\proposal-agent.env, then rerun."
    exit 2
}
$Compose = if ($Mode -eq "hybrid") { "docker-compose.hybrid.yml" } else { "docker-compose.offline.yml" }
docker compose -f "$Root\$Compose" up -d
Start-Sleep -Seconds 8
Invoke-RestMethod "http://127.0.0.1:8080/api/health" -TimeoutSec 15 | ConvertTo-Json -Depth 5
