param(
    [string]$OutputDir = "dist\proposal-agent-docker-offline",
    [ValidateSet("offline", "hybrid")][string]$Mode = "offline",
    [string]$SearxngImage = "searxng/searxng:latest"
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Out = Join-Path $Root $OutputDir
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Image = "proposal-agent:0.5.0-offline"
docker build -f "$Root\deploy\docker\Dockerfile.offline" -t $Image $Root
docker save -o "$Out\proposal-agent-image.tar" $Image
if ($Mode -eq "hybrid") {
    docker pull $SearxngImage
    docker save -o "$Out\searxng-image.tar" $SearxngImage
}
Copy-Item "$Root\deploy\docker\docker-compose.offline.yml" $Out
Copy-Item "$Root\deploy\docker\docker-compose.hybrid.yml" $Out
Copy-Item "$Root\deploy\docker\load_and_run.ps1" $Out
Copy-Item "$Root\deploy\common\verify_manifest.py" $Out
Copy-Item "$Root\prompt_pack" "$Out\prompt_pack" -Recurse
Copy-Item "$Root\.env.example" "$Out\proposal-agent.env.example"
@"
Application image: $Image
Bundle mode: $Mode
Built at: $([DateTime]::UtcNow.ToString("o"))
Application image inspect:
$(docker image inspect $Image --format '{{.Id}}')
"@ | Set-Content "$Out\BUNDLE_INFO.txt" -Encoding UTF8
py -3.12 "$Root\deploy\common\write_manifest.py" $Out
Compress-Archive -Path "$Out\*" -DestinationPath "$Out.zip" -CompressionLevel Optimal -Force
Write-Host "Created $Out.zip"
