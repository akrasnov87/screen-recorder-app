<#
.SYNOPSIS
    Build Screen Recorder & Transcriber for Windows 10/11.

.DESCRIPTION
    Analog of build_deb.sh, but for Windows:
      1. Creates a local venv (.venv).
      2. Installs dependencies from requirements.txt.
      3. Installs PyInstaller.
      4. Runs PyInstaller with the spec file.
      5. Puts the result into dist\ScreenRecorder\.

.PARAMETER Clean
    Remove build\ and dist\ before building.

.EXAMPLE
    .\packaging\windows\build_win.ps1
    .\packaging\windows\build_win.ps1 -Clean
#>

[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

# --- Move to project root ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..\..")
Push-Location $Root

try {
    # --- Read version from src/__init__.py ---
    $InitFile = Join-Path $Root "src\__init__.py"
    if (-not (Test-Path $InitFile)) {
        throw "File not found: $InitFile (cannot read version)."
    }

    $VersionMatch = Select-String -Path $InitFile `
        -Pattern '__version__\s*=\s*"([^"]+)"'
    if (-not $VersionMatch) {
        throw "Could not extract __version__ from src\__init__.py."
    }
    $Version = $VersionMatch.Matches[0].Groups[1].Value

    Write-Host ""
    Write-Host "Building ScreenRecorder version $Version" `
        -ForegroundColor Cyan
    Write-Host ""

    # --- Optional clean ---
    if ($Clean) {
        Write-Host "Cleaning build\ and dist\..." -ForegroundColor Yellow
        if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
        if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist" }
    }

    # --- venv ---
    $VenvDir = Join-Path $Root ".venv"
    $VenvPy  = Join-Path $VenvDir "Scripts\python.exe"

    if (-not (Test-Path $VenvPy)) {
        Write-Host "Creating virtual environment .venv..." `
            -ForegroundColor Yellow

        # Find Python: prefer py.exe (launcher), then python, then python3.
        $PythonCmd  = $null
        $PythonArgs = @()

        if (Get-Command py -ErrorAction SilentlyContinue) {
            $PythonCmd  = "py"
            $PythonArgs = @("-3")
            Write-Host "  Using: py -3" -ForegroundColor Gray
        }
        elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $PythonCmd  = "python"
            $PythonArgs = @()
            Write-Host "  Using: python" -ForegroundColor Gray
        }
        elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
            $PythonCmd  = "python3"
            $PythonArgs = @()
            Write-Host "  Using: python3" -ForegroundColor Gray
        }
        else {
            throw "Python not found. Install Python 3.11+ from " +
                  "python.org (check 'Add python.exe to PATH')."
        }

        & $PythonCmd @PythonArgs -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create venv. Check that Python 3.11+ " +
                  "is installed and works."
        }
    }

    # --- Dependencies ---
    Write-Host "Installing dependencies..." -ForegroundColor Yellow
    & $VenvPy -m pip install --upgrade pip wheel
    if ($LASTEXITCODE -ne 0) { throw "pip install --upgrade failed" }

    & $VenvPy -m pip install -r "requirements.txt"
    if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed" }

    # --- PyInstaller ---
    Write-Host "Installing PyInstaller..." -ForegroundColor Yellow
    & $VenvPy -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller install failed" }

    # --- Icon (optional) ---
    $Icon = Join-Path $Root "packaging\windows\app.ico"
    if (-not (Test-Path $Icon)) {
        Write-Host "WARNING: app.ico not found - default PyInstaller " `
            -ForegroundColor Yellow -NoNewline
        Write-Host "icon will be used." -ForegroundColor Yellow
        Write-Host "  To generate: see README, section 'Windows -> About icon'." `
            -ForegroundColor Gray
    }

    # --- Build ---
    Write-Host "Running PyInstaller..." -ForegroundColor Yellow
    & $VenvPy -m PyInstaller --clean --noconfirm `
        "packaging\windows\ScreenRecorder.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    # --- Result ---
    $ExePath = Join-Path $Root "dist\ScreenRecorder\ScreenRecorder.exe"
    if (-not (Test-Path $ExePath)) {
        throw "Build finished, but $ExePath not found."
    }

    $SizeMB = [Math]::Round(
        ((Get-ChildItem (Join-Path $Root "dist\ScreenRecorder") -Recurse |
          Measure-Object -Property Length -Sum).Sum / 1MB), 1
    )

    Write-Host ""
    Write-Host "Done!" -ForegroundColor Green
    Write-Host "  EXE:  $ExePath"
    Write-Host "  Folder: dist\ScreenRecorder\ ($SizeMB MB)"
    Write-Host ""
    Write-Host "For installer run Inno Setup on " `
        -NoNewline
    Write-Host "packaging\windows\installer.iss" -ForegroundColor Cyan
    Write-Host ""
}
finally {
    Pop-Location
}