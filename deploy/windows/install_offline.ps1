param(
    [string]$InstallRoot = "$env:ProgramData\ProposalAgent",
    [switch]$RegisterStartupTask = $true
)
$ErrorActionPreference = "Stop"
$BundleRoot = $PSScriptRoot

function Test-BundleManifest([string]$Root) {
    $ManifestPath = Join-Path $Root "manifest.json"
    if (-not (Test-Path $ManifestPath)) { throw "Missing manifest.json" }
    $Manifest = Get-Content $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($Item in $Manifest.files) {
        $Path = Join-Path $Root ($Item.path -replace '/', '\')
        if (-not (Test-Path $Path)) { throw "Bundle file missing: $($Item.path)" }
        $Hash = (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Hash -ne $Item.sha256.ToLowerInvariant()) { throw "Hash mismatch: $($Item.path)" }
    }
}
Test-BundleManifest $BundleRoot

$PythonExe = $null
$Py = Get-Command py -ErrorAction SilentlyContinue
if ($Py) {
    try { $PythonExe = (& py -3.12 -c "import sys; print(sys.executable)").Trim() } catch { $PythonExe = $null }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    $Installer = Get-ChildItem "$BundleRoot\runtime\python-*-amd64.exe" | Select-Object -First 1
    if (-not $Installer) { throw "Python 3.12 is absent and no bundled installer was found." }
    $PythonTarget = "$env:ProgramFiles\Python312"
    Start-Process -FilePath $Installer.FullName -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 TargetDir=`"$PythonTarget`"" -Wait -NoNewWindow
    $PythonExe = Join-Path $PythonTarget "python.exe"
}
if (-not (Test-Path $PythonExe)) { throw "Python installation failed: $PythonExe" }
& $PythonExe "$BundleRoot\verify_manifest.py" $BundleRoot

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
Get-ChildItem "$BundleRoot\source" -Force | ForEach-Object { Copy-Item $_.FullName $InstallRoot -Recurse -Force }

& $PythonExe -m venv "$InstallRoot\.venv"
& "$InstallRoot\.venv\Scripts\python.exe" -m pip install --no-index --find-links "$BundleRoot\wheelhouse" -r "$InstallRoot\requirements.txt"
Copy-Item "$BundleRoot\start.ps1" "$InstallRoot\start.ps1" -Force
Copy-Item "$BundleRoot\stop.ps1" "$InstallRoot\stop.ps1" -Force
Copy-Item "$BundleRoot\backup.ps1" "$InstallRoot\backup.ps1" -Force
Copy-Item "$BundleRoot\restore.ps1" "$InstallRoot\restore.ps1" -Force
Copy-Item "$BundleRoot\uninstall.ps1" "$InstallRoot\uninstall.ps1" -Force

$BrowserRoot = "$BundleRoot\runtime\playwright-browsers"
$BrowserExe = Get-ChildItem $BrowserRoot -Recurse -Filter chrome.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($BrowserExe) {
    $InstalledBrowserRoot = "$InstallRoot\runtime\playwright-browsers"
    New-Item -ItemType Directory -Path $InstalledBrowserRoot -Force | Out-Null
    Copy-Item "$BrowserRoot\*" $InstalledBrowserRoot -Recurse -Force
    $BrowserExe = Get-ChildItem $InstalledBrowserRoot -Recurse -Filter chrome.exe | Select-Object -First 1
} else {
    $Fallbacks = @("C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "C:\Program Files\Microsoft\Edge\Application\msedge.exe", "C:\Program Files\Google\Chrome\Application\chrome.exe")
    $BrowserExe = $Fallbacks | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $BrowserExe) { throw "No bundled Chromium or system Edge/Chrome was found." }
}
$BrowserPath = if ($BrowserExe -is [System.IO.FileInfo]) { $BrowserExe.FullName } else { [string]$BrowserExe }

$EnvFile = "$InstallRoot\proposal-agent.env"
if (-not (Test-Path $EnvFile)) {
    $DataRoot = "$env:ProgramData\ProposalAgentData"
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    $PromptPack = (Join-Path $InstallRoot "prompt_pack").Replace("\", "/")
    $DataPath = $DataRoot.Replace("\", "/")
    $MermaidJs = (Join-Path $InstallRoot "third_party\mermaid\mermaid.min.js").Replace("\", "/")
    $BrowserValue = $BrowserPath.Replace("\", "/")
    @"
APP_HOST=0.0.0.0
APP_PORT=8080
APP_DATA_DIR=$DataPath
PROMPT_PACK_DIR=$PromptPack
APP_WORKERS=1
APP_RELOAD=false
MODEL_RUNTIME_MODE=REPLAY
DENY_LIVE_CALLS_IN_CI=true
OFFLINE_LLM_ENABLED=false
OFFLINE_LLM_BASE_URL=http://127.0.0.1:8000/v1
OFFLINE_LLM_API_KEY=CHANGE_ME
OFFLINE_GENERAL_MODEL=CHANGE_ME
OFFLINE_CRITIC_MODEL=CHANGE_ME
ONLINE_LLM_ENABLED=false
ONLINE_LLM_BASE_URL=
ONLINE_LLM_API_KEY=
ONLINE_PUBLIC_MODEL=
PUBLIC_SEARCH_PROVIDER=disabled
PUBLIC_SEARCH_BASE_URL=
PUBLIC_RESEARCH_RECORD_FILE=
PUBLIC_RESEARCH_CONNECTOR_FILE=
PUBLIC_SEARCH_MAX_RESULTS=40
RESEARCH_FETCH_TIMEOUT_SECONDS=45
RESEARCH_MAX_SOURCE_BYTES=10485760
MERMAID_JS_PATH=$MermaidJs
MERMAID_BROWSER_EXECUTABLE=$BrowserValue
SKILL_TIMEOUT_SECONDS=60
MODEL_REQUEST_TIMEOUT_SECONDS=240
MAX_UPLOAD_MB=50
"@ | Set-Content $EnvFile -Encoding UTF8
    Write-Host "Created $EnvFile. Configure model endpoints before using LIVE mode."
}

if ($RegisterStartupTask) {
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallRoot\start.ps1`" -InstallRoot `"$InstallRoot`""
    $Trigger = New-ScheduledTaskTrigger -AtStartup
    $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName "ProposalAgent" -Action $Action -Trigger $Trigger -Principal $Principal -Force | Out-Null
    Start-ScheduledTask -TaskName "ProposalAgent"
    Start-Sleep -Seconds 5
}

$health = Invoke-RestMethod "http://127.0.0.1:8080/api/health" -TimeoutSec 15
$health | ConvertTo-Json -Depth 5
Write-Host "Proposal Agent installed at $InstallRoot"
