$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$python = Get-Command 'python' -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python was not found. Install Python 3.10 or later and enable it in PATH.'
}
$versionText = & $python.Source --version 2>&1
if ($versionText -notmatch 'Python\s+(\d+)\.(\d+)') {
    throw "Could not read the Python version: $versionText"
}
if ([version]"$($Matches[1]).$($Matches[2])" -lt [version]'3.10') {
    throw "Python 3.10 or later is required; found $versionText."
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python.Source -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create virtual environment (exit code $LASTEXITCODE)."
    }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update pip (exit code $LASTEXITCODE)."
}
& $venvPython -m pip install -r 'requirements.txt'
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install application dependencies (exit code $LASTEXITCODE)."
}

$dist = Join-Path $projectRoot 'dist'
if (Test-Path -LiteralPath $dist) {
    Remove-Item -LiteralPath $dist -Recurse -Force
}
$workPath = Join-Path $projectRoot 'build'
if (Test-Path -LiteralPath $workPath) {
    Remove-Item -LiteralPath $workPath -Recurse -Force
}

& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --distpath 'dist' `
    --workpath 'build' `
    --specpath '.' `
    --name 'STM32KeilProjectCreator' `
    --add-data 'assets;assets' `
    'main.py'
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed (exit code $LASTEXITCODE)."
}

$exe = Join-Path $dist 'STM32KeilProjectCreator.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Build finished without creating the expected executable: $exe"
}
Write-Output "Executable created: $exe"
