<# 
Set up a Windows host for Reflex development and QEMU-backed benchmark work.

Run from an elevated PowerShell when enabling Windows features:
  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -EnableWindowsFeatures

For package-only setup:
  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#>

[CmdletBinding()]
param(
    [switch]$EnableWindowsFeatures,
    [switch]$InstallWSL,
    [switch]$SkipPackageInstall,
    [switch]$SkipUvSync
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[reflex-win] $Message"
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Install-WingetCandidate {
    param(
        [string]$Name,
        [string[]]$Ids,
        [string[]]$CommandNames = @()
    )
    foreach ($commandName in $CommandNames) {
        if (Get-Command $commandName -ErrorAction SilentlyContinue) {
            Write-Step "$Name already available on PATH as $commandName; skipping winget install."
            return
        }
    }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget not found. Install App Installer from Microsoft Store, then rerun this script."
    }

    $installed = winget list --accept-source-agreements 2>$null
    foreach ($id in $Ids) {
        if ($installed -match [Regex]::Escape($id)) {
            Write-Step "$Name already installed according to winget id=$id; skipping install."
            return
        }
        Write-Step "Installing $Name via winget id=$id"
        winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            return
        }
        foreach ($commandName in $CommandNames) {
            if (Get-Command $commandName -ErrorAction SilentlyContinue) {
                Write-Step "$Name install reported failure, but $commandName is now available; continuing."
                return
            }
        }
        Write-Step "winget id=$id did not install cleanly; trying next candidate if available"
    }
    throw "Could not install $Name via winget. Tried: $($Ids -join ', ')"
}

function Enable-Feature {
    param([string]$FeatureName)
    Write-Step "Enabling Windows optional feature: $FeatureName"
    Enable-WindowsOptionalFeature -Online -FeatureName $FeatureName -All -NoRestart | Out-Null
}

function Show-VirtualizationStatus {
    Write-Step "Checking CPU virtualization exposure"
    $processors = Get-CimInstance Win32_Processor
    foreach ($cpu in $processors) {
        Write-Host "  $($cpu.Name)"
        Write-Host "  VirtualizationFirmwareEnabled=$($cpu.VirtualizationFirmwareEnabled)"
        Write-Host "  SecondLevelAddressTranslationExtensions=$($cpu.SecondLevelAddressTranslationExtensions)"
    }
    $computer = Get-CimInstance Win32_ComputerSystem
    Write-Host "  HypervisorPresent=$($computer.HypervisorPresent)"
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

Write-Step "Repo root: $repoRoot"
Show-VirtualizationStatus

if ($EnableWindowsFeatures) {
    if (-not (Test-Admin)) {
        throw "-EnableWindowsFeatures requires elevated PowerShell."
    }
    Enable-Feature -FeatureName "HypervisorPlatform"
    Enable-Feature -FeatureName "VirtualMachinePlatform"
    if ($InstallWSL) {
        Enable-Feature -FeatureName "Microsoft-Windows-Subsystem-Linux"
    }
    Write-Step "Feature changes may require a reboot before QEMU/WSL acceleration works."
}

if (-not $SkipPackageInstall) {
    Install-WingetCandidate -Name "Git" -Ids @("Git.Git") -CommandNames @("git")
    Install-WingetCandidate -Name "uv" -Ids @("astral-sh.uv") -CommandNames @("uv")
    Install-WingetCandidate -Name "QEMU" -Ids @(
        "SoftwareFreedomConservancy.QEMU",
        "QEMU.QEMU"
    ) -CommandNames @("qemu-system-x86_64", "qemu-system-x86_64.exe")

    if (Test-Admin) {
        Write-Step "Ensuring OpenSSH Client capability is installed"
        $sshClient = Get-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
        if ($sshClient.State -ne "Installed") {
            Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0 | Out-Null
        }
    } else {
        Write-Step "Skipping OpenSSH Client capability install because PowerShell is not elevated"
    }
}

if ($InstallWSL) {
    if (-not (Test-Admin)) {
        throw "-InstallWSL requires elevated PowerShell."
    }
    Write-Step "Installing Ubuntu for WSL if needed"
    wsl --install -d Ubuntu
    Write-Step "WSL install may require a reboot or first-launch user setup."
}

if (-not $SkipUvSync) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is not on PATH. Open a new PowerShell after winget install, or rerun with -SkipUvSync."
    }
    Write-Step "Syncing Python dependencies with uv"
    uv sync
}

Write-Host ""
Write-Step "Windows setup complete."
Write-Host "Next checks:"
Write-Host "  qemu-system-x86_64 --version"
Write-Host "  uv run ruff check"
Write-Host ""
Write-Host "For the current Linux guest benchmark flow, use WSL/Ubuntu or a Linux host to run:"
Write-Host "  scripts/setup_dev_env.sh"
Write-Host "  benchmarks/unixbench_qemu.sh"
Write-Host ""
Write-Host "Native Windows QEMU support requires a Windows-specific runner; this script installs host prerequisites."
