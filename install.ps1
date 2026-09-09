<#
.SYNOPSIS
  CodeOrbit installer for Windows.

.DESCRIPTION
  Installs CodeOrbit into an isolated virtualenv and puts a `codeorbit` command
  on your PATH. Re-running upgrades in place.

    irm https://raw.githubusercontent.com/Anilll-coder/codeorbit/main/install.ps1 | iex
    .\install.ps1                  # from a clone
    .\install.ps1 -Uninstall

.PARAMETER InstallDir
  Where the virtualenv lives. Default: $env:LOCALAPPDATA\CodeOrbit

.PARAMETER BinDir
  Where the launcher goes. Default: $env:LOCALAPPDATA\Programs\CodeOrbit\bin

.PARAMETER NoModel
  Skip downloading the Ollama model.

.PARAMETER Uninstall
  Remove CodeOrbit. Per-project .codeorbit\ indexes are left alone.
#>
[CmdletBinding()]
param(
    [string] $InstallDir = "$env:LOCALAPPDATA\CodeOrbit",
    [string] $BinDir     = "$env:LOCALAPPDATA\Programs\CodeOrbit\bin",
    [string] $Repo       = 'https://github.com/Anilll-coder/codeorbit.git',
    [string] $Ref        = 'main',
    [switch] $NoModel,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
$Model      = 'phi4-mini'
$MinPyMinor = 10

function Step($m) { Write-Host '==> ' -ForegroundColor Cyan -NoNewline; Write-Host $m }
function Ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function Note($m) { Write-Host "  $m" -ForegroundColor DarkGray }
function Warn($m) { Write-Host ' warn ' -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Die($m)  { Write-Host 'error ' -ForegroundColor Red -NoNewline; Write-Host $m; exit 1 }

# Run a native exe without letting PowerShell 5.1 turn its stderr into a
# terminating NativeCommandError, and with stdin closed so a mis-invoked
# interpreter can never sit waiting at an interactive prompt.
function Invoke-Exe {
    param([string] $Exe, [string[]] $Arguments, [switch] $Capture)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Capture) { $out = ($null | & $Exe @Arguments | Out-String) }
        else          { $null | & $Exe @Arguments | Out-Null; $out = '' }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Out = $out.Trim() }
    } finally {
        $ErrorActionPreference = $prev
    }
}

# ---------- uninstall -------------------------------------------------------
if ($Uninstall) {
    Step 'Removing CodeOrbit'
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir; Note "removed $InstallDir" }
    $launcher = Join-Path $BinDir 'codeorbit.cmd'
    if (Test-Path $launcher) { Remove-Item -Force $launcher; Note "removed $launcher" }

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -and $userPath.Split(';') -contains $BinDir) {
        $kept = ($userPath.Split(';') | Where-Object { $_ -and $_ -ne $BinDir }) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $kept, 'User')
        Note 'removed from PATH'
    }
    Write-Host ''
    Ok 'Done. Per-project .codeorbit\ indexes were left alone.'
    exit 0
}

# ---------- python ----------------------------------------------------------
Step 'Checking Python'
$probe = "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,$MinPyMinor) else 1)"
$py = $null
foreach ($name in @('python', 'py', 'python3')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    # Windows ships stub python.exe shims that just open the Store; they exit
    # non-zero here, so the probe rejects them for us.
    if ((Invoke-Exe -Exe $cmd.Source -Arguments @('-c', $probe)).Code -eq 0) {
        $py = $cmd.Source
        break
    }
}
if (-not $py) {
    Die "Python 3.$MinPyMinor or newer is required but was not found.`n  Install it from https://python.org (tick 'Add Python to PATH') and re-run."
}
# No quote characters inside the -c payload: PowerShell strips embedded double
# quotes when building a native command line, which corrupts the source.
$verOut = Invoke-Exe -Exe $py -Capture `
    -Arguments @('-c', 'import sys; v=sys.version_info; print(v[0], v[1], v[2])')
$pretty = if ($verOut.Out) { 'Python ' + ($verOut.Out -replace '\s+', '.') } else { 'Python ?' }
Ok "$pretty  ($py)"

if ((Invoke-Exe -Exe $py -Arguments @('-c', 'import venv')).Code -ne 0) {
    Die 'This Python is missing the venv module. Reinstall Python from python.org.'
}

# ---------- source ----------------------------------------------------------
# Piped through `irm ... | iex` there is no script file on disk, so both
# $PSScriptRoot and $MyInvocation.MyCommand.Path are null. Passing that null to
# Split-Path throws "Cannot bind argument to parameter 'Path'", which is what
# the documented one-liner used to do. A null $selfDir simply means "not run
# from a checkout", and the clone path below handles it.
$selfDir = if ($PSScriptRoot) {
    $PSScriptRoot
} elseif ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $null
}
$tempSrc = $null
if ($selfDir -and (Test-Path (Join-Path $selfDir 'pyproject.toml')) -and
    (Test-Path (Join-Path $selfDir 'codeorbit'))) {
    $src = $selfDir
    Step 'Installing from this checkout'
    Note $src
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Die 'git is required to fetch the source. Install it from https://git-scm.com'
    }
    $tempSrc = Join-Path ([System.IO.Path]::GetTempPath()) ('codeorbit-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $src = $tempSrc
    Step 'Fetching source'
    Note "$Repo ($Ref)"
    # --quiet: git writes clone progress to stderr, which PowerShell surfaces as
    # NativeCommandError noise even on success.
    if ((Invoke-Exe -Exe 'git' -Arguments @('clone', '--quiet', '--depth', '1',
                                            '--branch', $Ref, $Repo, $src)).Code -ne 0) {
        Die "Could not clone $Repo (ref: $Ref)."
    }
}

try {
    # ---------- venv --------------------------------------------------------
    Step 'Creating isolated environment'
    Note $InstallDir
    $vpy = Join-Path $InstallDir 'Scripts\python.exe'
    if (Test-Path $vpy) {
        Note 'existing install found - upgrading'
    } elseif ((Invoke-Exe -Exe $py -Arguments @('-m', 'venv', $InstallDir)).Code -ne 0) {
        Die "Could not create a virtualenv at $InstallDir"
    }
    if (-not (Test-Path $vpy)) { Die "The virtualenv at $InstallDir looks broken. Delete it and re-run." }

    Step 'Installing CodeOrbit and its dependencies'
    Invoke-Exe -Exe $vpy -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip') | Out-Null
    $pipRes = Invoke-Exe -Exe $vpy -Arguments @('-m', 'pip', 'install', '--upgrade', $src)
    if ($pipRes.Code -ne 0) {
        Die "Installation failed. Re-run this to see why:`n  & '$vpy' -m pip install --upgrade '$src'"
    }
    Ok 'ok'

    # Record where this came from: pip does not keep the source of a
    # non-editable install, so `codeorbit upgrade` would otherwise have
    # to guess at the public repo even when installed from a checkout.
    # WriteAllText, not Set-Content -Encoding utf8: PowerShell 5.1 writes a
    # BOM, and a leading U+FEFF makes the recorded path fail to resolve, so
    # `codeorbit upgrade` could not find its own source.
    try { [System.IO.File]::WriteAllText(
        (Join-Path $InstallDir '.codeorbit-source'), $src) } catch {}

    # ---------- launcher ----------------------------------------------------
    Step 'Putting codeorbit on your PATH'
    $target = Join-Path $InstallDir 'Scripts\codeorbit.exe'
    if (-not (Test-Path $target)) { Die 'The codeorbit entry point was not installed. This is a bug.' }

    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $launcher = Join-Path $BinDir 'codeorbit.cmd'
    "@echo off`r`n`"$target`" %*`r`n" | Set-Content -Path $launcher -Encoding ASCII
    Note $launcher

    $pathAdded = $false
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    if ($userPath.Split(';') -notcontains $BinDir) {
        $trimmed = $userPath.TrimEnd(';')
        $newPath = if ($trimmed) { "$trimmed;$BinDir" } else { $BinDir }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        $env:Path = "$env:Path;$BinDir"
        Note 'added to your user PATH'
        $pathAdded = $true
    }

    # ---------- ollama ------------------------------------------------------
    Step "Checking Ollama (needed for 'codeorbit ask')"
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        $listed = Invoke-Exe -Exe 'ollama' -Arguments @('list') -Capture
        if ($listed.Out -match [regex]::Escape($Model)) {
            Ok "ollama ok, model $Model present"
        } elseif ($NoModel) {
            Note 'skipping model download (-NoModel)'
        } else {
            Note "pulling $Model (~2.5 GB, one time)"
            if ((Invoke-Exe -Exe 'ollama' -Arguments @('pull', $Model)).Code -ne 0) {
                Warn "Model pull failed. Run later: ollama pull $Model"
            }
        }
    } else {
        Warn "Ollama not found - graph commands work, but 'codeorbit ask' will not."
        Note "Install from https://ollama.com, then: ollama pull $Model"
    }

    # ---------- done --------------------------------------------------------
    $verRes = Invoke-Exe -Exe $vpy -Capture `
        -Arguments @('-c', "import importlib.metadata as m; print(m.version('codeorbit'))")
    $version = if ($verRes.Out) { $verRes.Out } else { '?' }

    Write-Host ''
    Write-Host "CodeOrbit $version installed." -ForegroundColor Green
    Write-Host ''
    if ($pathAdded) {
        Warn 'PATH was updated - open a NEW terminal before using codeorbit.'
        Write-Host ''
    }
    Write-Host '  cd your-project'
    Write-Host '  codeorbit index .                  build the graph'
    Write-Host '  codeorbit ask "how does X work?"   ask the local model'
    Write-Host ''
    Note 'codeorbit --help             all commands'
    Note '.\install.ps1 -Uninstall     remove it'
}
finally {
    if ($tempSrc -and (Test-Path $tempSrc)) {
        Remove-Item -Recurse -Force $tempSrc -ErrorAction SilentlyContinue
    }
}
