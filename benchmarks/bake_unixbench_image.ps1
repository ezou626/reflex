<#
Build a reusable UnixBench QEMU base image with dependencies preinstalled.

Run from repo root:
  powershell -ExecutionPolicy Bypass -File benchmarks\bake_unixbench_image.ps1

Optional parameters:
  -SshPort 52222
  -MemoryMB 4096
  -Cpus 4
  -CacheDir "data\qemu-windows"
  -OutputImageName "noble-unixbench-deps-amd64.img"
#>

[CmdletBinding()]
param(
    [int]$SshPort = 52222,
    [int]$MemoryMB = 4096,
    [int]$Cpus = 4,
    [int]$DiskGB = 24,
    [string]$CacheDir = "",
    [string]$SourceImageName = "noble-server-cloudimg-amd64.img",
    [string]$SourceImageUrl = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    [string]$OutputImageName = "noble-unixbench-deps-amd64.img"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[bake-unixbench-image] $Message"
}

function Resolve-CommandPath {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Reset-HostKeyForPort {
    param([int]$Port)
    $knownHosts = Join-Path $env:USERPROFILE ".ssh\known_hosts"
    if (-not (Test-Path $knownHosts)) {
        return
    }
    & ssh-keygen -R "[127.0.0.1]:$Port" -f $knownHosts | Out-Null
}

function Write-SeedIso {
    param(
        [string]$IsoPath,
        [string]$SourceDir
    )
    $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
    $fsi.FileSystemsToCreate = 1
    $fsi.VolumeName = "cidata"
    $fsi.Root.AddTree($SourceDir, $false)
    $result = $fsi.CreateResultImage()
    $stream = $result.ImageStream
    $out = New-Object -ComObject ADODB.Stream
    $out.Type = 1
    $out.Open()
    $bufferSize = 1MB
    while ($stream.Position -lt $stream.Size) {
        $remaining = $stream.Size - $stream.Position
        $chunkSize = [Math]::Min($bufferSize, $remaining)
        $out.Write($stream.Read($chunkSize))
    }
    if (Test-Path $IsoPath) { Remove-Item -Force $IsoPath }
    $out.SaveToFile($IsoPath, 2)
    $out.Close()
}

function Wait-ForSsh {
    param(
        [string]$KeyPath,
        [int]$Port,
        [string]$KnownHostsPath
    )
    for ($i = 0; $i -lt 120; $i++) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $out = & ssh -vv `
            -F NUL `
            -i $KeyPath `
            -p $Port `
            -o IdentitiesOnly=yes `
            -o PreferredAuthentications=publickey `
            -o StrictHostKeyChecking=accept-new `
            -o UserKnownHostsFile="$KnownHostsPath" `
            -o ConnectTimeout=5 `
            -o BatchMode=yes `
            ubuntu@127.0.0.1 "echo ok" 2>&1
        $ErrorActionPreference = $prevEap
        if ($LASTEXITCODE -eq 0) {
            Write-Step "SSH ready"
            return
        }
        if ($i % 6 -eq 0) {
            Write-Host "SSH attempt $i failed:"
            $out | Select-Object -Last 40 | ForEach-Object { Write-Host "  $_" }
        }
        Start-Sleep -Seconds 5
    }
    throw "SSH did not become ready on port $Port"
}

function Invoke-Guest {
    param(
        [string]$Command,
        [string]$KeyPath,
        [int]$Port,
        [string]$KnownHostsPath
    )
    & ssh -i $KeyPath -p $Port `
        -F NUL `
        -o IdentitiesOnly=yes `
        -o PreferredAuthentications=publickey `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHostsPath" `
        -o ConnectTimeout=30 `
        ubuntu@127.0.0.1 $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Guest command failed: $Command"
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

if ([string]::IsNullOrWhiteSpace($CacheDir)) {
    $CacheDir = Join-Path $repoRoot "data\qemu-windows"
}
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

$qemu = Resolve-CommandPath @("qemu-system-x86_64.exe", "qemu-system-x86_64")
$qemuImg = Resolve-CommandPath @("qemu-img.exe", "qemu-img")
if (-not $qemu -or -not $qemuImg) {
    throw "QEMU not found on PATH."
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { throw "ssh not found." }
if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) { throw "ssh-keygen not found." }

$baseImg = Join-Path $CacheDir $SourceImageName
$overlay = Join-Path $CacheDir "bake-overlay-$PID.qcow2"
$seedIso = Join-Path $CacheDir "bake-seed-$PID.iso"
$seedDir = Join-Path $CacheDir "bake-seed-$PID"
$consoleLog = Join-Path $CacheDir "bake-console-$PID.log"
$qemuLog = Join-Path $CacheDir "bake-qemu-$PID.log"
$Key = Join-Path $CacheDir "id_ed25519"
$pub = "$Key.pub"
$KnownHosts = Join-Path $CacheDir "known_hosts.bake.$PID"
$outputImg = Join-Path $CacheDir $OutputImageName

Reset-HostKeyForPort -Port $SshPort

if (-not (Test-Path $Key)) {
    Write-Step "Generating SSH key: $Key"
    & ssh-keygen -t ed25519 -f $Key -N '""' -q
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed" }
}
icacls $Key /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null

if (-not (Test-Path $baseImg)) {
    Write-Step "Downloading source image"
    Invoke-WebRequest -Uri $SourceImageUrl -OutFile "$baseImg.part"
    Move-Item -Force "$baseImg.part" $baseImg
}

Write-Step "Creating cloud-init seed ISO"
New-Item -ItemType Directory -Force -Path $seedDir | Out-Null
$pubLine = (Get-Content -Raw $pub).Trim()
[System.IO.File]::WriteAllText((Join-Path $seedDir "meta-data"), "instance-id: iid-reflex-bake-$PID`nlocal-hostname: reflex-bake`n", (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText(
    (Join-Path $seedDir "user-data"),
    @"
#cloud-config
users:
  - name: ubuntu
    shell: /bin/bash
    groups: [adm, cdrom, dip, lxd, sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - $pubLine
"@,
    (New-Object System.Text.UTF8Encoding($false))
)
$mkisofs = Resolve-CommandPath @("mkisofs.exe", "genisoimage.exe", "xorriso.exe", "oscdimg.exe")
if ($mkisofs) {
    if ((Split-Path $mkisofs -Leaf) -ieq "oscdimg.exe") {
        & $mkisofs -o -m -l $seedDir $seedIso
    } elseif ((Split-Path $mkisofs -Leaf) -ieq "xorriso.exe") {
        & $mkisofs -as mkisofs -output $seedIso -volid cidata -joliet -rock $seedDir
    } else {
        & $mkisofs -output $seedIso -volid cidata -joliet -rock $seedDir
    }
    if ($LASTEXITCODE -ne 0) { throw "seed ISO creation failed" }
} elseif (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
    $wslUserData = (wsl wslpath -u $seedDir.Replace('\', '/')) + "/user-data"
    $wslMetaData = (wsl wslpath -u $seedDir.Replace('\', '/')) + "/meta-data"
    $wslIso      = wsl wslpath -u $seedIso.Replace('\', '/')
    $hasCloudLocalds = (wsl which cloud-localds 2>$null).Trim()
    $hasGenisoimage  = (wsl which genisoimage  2>$null).Trim()
    if ($hasCloudLocalds) {
        wsl cloud-localds $wslIso $wslUserData $wslMetaData
    } elseif ($hasGenisoimage) {
        $wslDir = wsl wslpath -u $seedDir.Replace('\', '/')
        wsl genisoimage -output $wslIso -volid cidata -joliet -rock $wslDir
    } else {
        throw "WSL found but neither cloud-localds nor genisoimage available. Run: wsl sudo apt-get install cloud-image-utils"
    }
    if ($LASTEXITCODE -ne 0) { throw "seed ISO creation failed (WSL)" }
} else {
    throw "No ISO creation tool found. Install mkisofs/genisoimage/xorriso/oscdimg, or enable WSL with genisoimage."
}

Write-Step "Creating writable overlay"
& $qemuImg create -f qcow2 -F qcow2 -b (Resolve-Path $baseImg) $overlay | Out-Host
& $qemuImg resize $overlay "${DiskGB}G" | Out-Host

Write-Step "Boosting power plan for bake"
powercfg -setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFBOOSTMODE 1
powercfg -setactive SCHEME_CURRENT

Write-Step "Starting QEMU"
$qemuArgs = @(
    "-machine", "type=q35,accel=whpx",
    "-smbios", "type=1,serial=ds=nocloud",
    "-cpu", "qemu64",
    "-smp", "$Cpus",
    "-m", "$MemoryMB",
    "-boot", "order=c",
    "-display", "none",
    "-serial", "file:$consoleLog",
    "-drive", "file=$overlay,if=virtio,cache=writeback",
    "-cdrom", "$seedIso",
    "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:$SshPort-:22",
    "-device", "e1000,netdev=net0"
)
$qemuProc = Start-Process -FilePath $qemu -ArgumentList $qemuArgs -RedirectStandardError $qemuLog -PassThru -WindowStyle Hidden
try {
    $qemuProc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::RealTime
} catch {
    try {
        $qemuProc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::High
    } catch {
        Write-Step "Warning: Could not raise QEMU process priority."
    }
}
$qemuProc.ProcessorAffinity = [IntPtr]0xFF

try {
    New-Item -ItemType File -Force -Path $KnownHosts | Out-Null
    Write-Step "Waiting for SSH"
    try {
        Wait-ForSsh -KeyPath $Key -Port $SshPort -KnownHostsPath $KnownHosts
    } catch {
        Write-Step "SSH did not become ready; dumping recent VM logs"
        if (Test-Path $consoleLog) {
            Write-Host "---- bake console log (last 80 lines) ----"
            Get-Content $consoleLog -Tail 80 | ForEach-Object { Write-Host $_ }
        }
        if (Test-Path $qemuLog) {
            Write-Host "---- bake qemu stderr log (last 80 lines) ----"
            Get-Content $qemuLog -Tail 80 | ForEach-Object { Write-Host $_ }
        }
        throw
    }

    Write-Step "Installing dependency toolchain inside guest"
    $guestScript = @'
set -euo pipefail
if command -v cloud-init >/dev/null 2>&1; then
  sudo cloud-init status --wait 2>/dev/null || true
fi
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  build-essential clang libbpf-dev bpfcc-tools python3-bpfcc \
  git make perl curl ca-certificates unzip linux-tools-common >/dev/null
if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  "linux-headers-$(uname -r)" "linux-tools-$(uname -r)" >/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    linux-headers-generic linux-tools-generic >/dev/null
fi
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
echo "unixbench-baked" | sudo tee /etc/reflex-unixbench-image >/dev/null
sudo apt-get clean
sudo sync
'@
    $guestScript = $guestScript -replace "`r", ""
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($guestScript))
    Invoke-Guest -Command "echo $encoded | base64 -d > /home/ubuntu/bake_image.sh && bash /home/ubuntu/bake_image.sh" -KeyPath $Key -Port $SshPort -KnownHostsPath $KnownHosts

    Write-Step "Stopping VM to finalize image"
    if ($qemuProc -and -not $qemuProc.HasExited) {
        Stop-Process -Id $qemuProc.Id -Force
        $qemuProc.WaitForExit()
    }

    Write-Step "Committing overlay to $outputImg"
    if (Test-Path $outputImg) { Remove-Item -Force $outputImg }
    & $qemuImg convert -O qcow2 $overlay $outputImg | Out-Host
    Write-Step "Image ready: $outputImg"
}
finally {
    if ($qemuProc -and -not $qemuProc.HasExited) {
        Stop-Process -Id $qemuProc.Id -Force
    }
    Remove-Item -Force $overlay, $seedIso, $KnownHosts -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $seedDir -ErrorAction SilentlyContinue
}
