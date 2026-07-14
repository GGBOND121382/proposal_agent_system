param(
    [string]$OutputDir = "dist\proposal-agent-windows-offline",
    [string]$Python = "py",
    [string]$PythonVersion = "3.12.10",
    [string]$PythonInstaller = "",
    [switch]$SkipBrowserBundle
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Out = Join-Path $Root $OutputDir
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Out, "$Out\source", "$Out\wheelhouse", "$Out\runtime" | Out-Null

$excludeDirs = @(".git", ".venv", "data", "dist", "__pycache__")
Get-ChildItem $Root -Force | Where-Object { $excludeDirs -notcontains $_.Name } | ForEach-Object {
    Copy-Item $_.FullName "$Out\source" -Recurse -Force
}

& $Python -3.12 -m pip download --only-binary=:all: --dest "$Out\wheelhouse" --requirement "$Root\requirements.txt"

# Bundle a signed official Python installer so the target machine does not need Python beforehand.
$PythonInstallerTarget = "$Out\runtime\python-$PythonVersion-amd64.exe"
if ($PythonInstaller) {
    Copy-Item $PythonInstaller $PythonInstallerTarget -Force
} else {
    $PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
    Write-Host "Downloading Python runtime: $PythonUrl"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonInstallerTarget -UseBasicParsing
}

# Bundle Playwright Chromium. Mermaid rendering never downloads a browser in the isolated environment.
if (-not $SkipBrowserBundle) {
    $BuildVenv = "$Out\runtime\builder-venv"
    & $Python -3.12 -m venv $BuildVenv
    & "$BuildVenv\Scripts\python.exe" -m pip install --upgrade pip
    & "$BuildVenv\Scripts\python.exe" -m pip install -r "$Root\requirements.txt"
    $env:PLAYWRIGHT_BROWSERS_PATH = "$Out\runtime\playwright-browsers"
    & "$BuildVenv\Scripts\python.exe" -m playwright install chromium
    Remove-Item $BuildVenv -Recurse -Force
}

Copy-Item "$Root\deploy\windows\install_offline.ps1" "$Out\install.ps1"
Copy-Item "$Root\deploy\windows\start.ps1" "$Out\start.ps1"
Copy-Item "$Root\deploy\windows\stop.ps1" "$Out\stop.ps1"
Copy-Item "$Root\deploy\windows\backup.ps1" "$Out\backup.ps1"
Copy-Item "$Root\deploy\windows\restore.ps1" "$Out\restore.ps1"
Copy-Item "$Root\deploy\windows\uninstall.ps1" "$Out\uninstall.ps1"
Copy-Item "$Root\deploy\common\verify_manifest.py" "$Out\verify_manifest.py"
@"
Proposal Agent Windows offline bundle
Built at: $([DateTime]::UtcNow.ToString("o"))
Python installer: python-$PythonVersion-amd64.exe
Playwright Chromium bundled: $(-not $SkipBrowserBundle)
Target: Windows 10/11 or Windows Server x64.
No network access is required by install.ps1.
"@ | Set-Content "$Out\BUNDLE_INFO.txt" -Encoding UTF8

& $Python -3.12 "$Root\deploy\common\write_manifest.py" $Out
$Zip = "$Out.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path "$Out\*" -DestinationPath $Zip -CompressionLevel Optimal
Write-Host "Created $Zip"
