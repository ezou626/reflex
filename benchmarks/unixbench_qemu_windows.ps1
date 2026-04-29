<#
Run the UnixBench comparison inside an Ubuntu guest booted by QEMU on Windows.

This is a Windows-native runner: no Git Bash, WSL, cloud-localds, or 9p mount is
required. It creates a NoCloud seed ISO with PowerShell, boots QEMU with WHPX,
copies the repo into the guest over SSH, and runs benchmarks/unixbench_compare.sh.

Run from the repo root:
  powershell -ExecutionPolicy Bypass -File benchmarks\unixbench_qemu_windows.ps1

Useful parameters:
  -Modes workload_only,heuristic
  -SshPort 52223
  -DiskGB 32
#>

[CmdletBinding()]
param(
    [string]$Modes = "workload_only,heuristic,classifier",
    [int]$SshPort = 52222,
    [int]$DiskGB = 24,
    [int]$MemoryMB = 4096,
    [int]$Cpus = 2,
    [string]$CacheDir = "",
    [string]$UbuntuImageUrl = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    [string]$UnixBenchUrl = "https://github.com/kdlucas/byte-unixbench.git",
    [switch]$KeepVm
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[unixbench-win-qemu] $Message"
}

function Resolve-CommandPath {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            return $cmd.Source
        }
    }
    return $null
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
    if (Test-Path $IsoPath) {
        Remove-Item -Force $IsoPath
    }
    $out.SaveToFile($IsoPath, 2)
    $out.Close()
}

function Wait-ForSsh {
    param(
        [string]$KeyPath,
        [int]$Port
    )
    for ($i = 0; $i -lt 120; $i++) {
        & ssh -i $KeyPath -p $Port `
            -o StrictHostKeyChecking=accept-new `
            -o UserKnownHostsFile="$KnownHosts" `
            -o ConnectTimeout=5 `
            -o BatchMode=yes `
            ubuntu@127.0.0.1 "echo ok" *> $null
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Start-Sleep -Seconds 5
    }
    throw "SSH did not become ready on port $Port"
}

function Invoke-Guest {
    param([string]$Command)
    & ssh -i $Key -p $SshPort `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHosts" `
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
New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "data") | Out-Null

$qemu = Resolve-CommandPath @("qemu-system-x86_64.exe", "qemu-system-x86_64")
$qemuImg = Resolve-CommandPath @("qemu-img.exe", "qemu-img")
if (-not $qemu -or -not $qemuImg) {
    throw "QEMU not found on PATH. Run scripts\setup_windows.ps1, then open a new PowerShell."
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh not found. Install OpenSSH Client or rerun scripts\setup_windows.ps1 elevated."
}
if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw "scp not found. Install OpenSSH Client or rerun scripts\setup_windows.ps1 elevated."
}
if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) {
    throw "ssh-keygen not found. Install OpenSSH Client."
}

$baseImg = Join-Path $CacheDir "noble-server-cloudimg-amd64.img"
$overlay = Join-Path $CacheDir "unixbench-overlay-$PID.qcow2"
$seedIso = Join-Path $CacheDir "unixbench-seed-$PID.iso"
$seedDir = Join-Path $CacheDir "seed-$PID"
$consoleLog = Join-Path $CacheDir "unixbench-console-$PID.log"
$qemuLog = Join-Path $CacheDir "unixbench-qemu-$PID.log"
$Key = Join-Path $CacheDir "id_ed25519"
$pub = "$Key.pub"
$KnownHosts = Join-Path $CacheDir "known_hosts.unixbench.$PID"
$repoZip = Join-Path $CacheDir "reflex-$PID.zip"

if (-not (Test-Path $Key)) {
    Write-Step "Generating SSH key: $Key"
    & ssh-keygen -t ed25519 -f $Key -N "" -q
    if ($LASTEXITCODE -ne 0) {
        throw "ssh-keygen failed"
    }
}

if (-not (Test-Path $baseImg)) {
    Write-Step "Downloading Ubuntu cloud image"
    Invoke-WebRequest -Uri $UbuntuImageUrl -OutFile "$baseImg.part"
    Move-Item -Force "$baseImg.part" $baseImg
}

Write-Step "Creating cloud-init seed ISO"
New-Item -ItemType Directory -Force -Path $seedDir | Out-Null
$pubLine = Get-Content -Raw $pub
@"
instance-id: reflex-win-unixbench-$PID
local-hostname: reflex-win-unixbench
"@ | Set-Content -Encoding ascii (Join-Path $seedDir "meta-data")
@"
#cloud-config
package_update: false
growpart:
  mode: auto
  devices: ["/"]
  ignore_growroot_disabled: false
resize_rootfs: true
ssh_authorized_keys:
  - $pubLine
"@ | Set-Content -Encoding ascii (Join-Path $seedDir "user-data")
Write-SeedIso -IsoPath $seedIso -SourceDir $seedDir

Write-Step "Creating VM overlay"
& $qemuImg create -f qcow2 -F qcow2 -b (Resolve-Path $baseImg) $overlay | Out-Host
& $qemuImg resize $overlay "${DiskGB}G" | Out-Host

Write-Step "Starting QEMU on 127.0.0.1:$SshPort"
$qemuArgs = @(
    "-machine", "type=q35,accel=whpx",
    "-cpu", "max",
    "-smp", "$Cpus",
    "-m", "$MemoryMB",
    "-display", "none",
    "-serial", "file:$consoleLog",
    "-drive", "file=$overlay,if=virtio,cache=writeback",
    "-drive", "file=$seedIso,if=virtio,format=raw",
    "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:$SshPort-:22",
    "-device", "virtio-net-pci,netdev=net0"
)
$qemuProc = Start-Process -FilePath $qemu -ArgumentList $qemuArgs -RedirectStandardError $qemuLog -PassThru -WindowStyle Hidden

try {
    New-Item -ItemType File -Force -Path $KnownHosts | Out-Null
    Write-Step "Waiting for SSH"
    Wait-ForSsh -KeyPath $Key -Port $SshPort

    Write-Step "Preparing repo archive"
    if (Test-Path $repoZip) {
        Remove-Item -Force $repoZip
    }
    $exclude = @("\.git\", "\.venv\", "\data\qemu", "\data\qemu-windows", "\__pycache__\")
    $files = Get-ChildItem -Path $repoRoot -Recurse -File | Where-Object {
        $full = $_.FullName
        -not ($exclude | Where-Object { $full -like "*$_*" })
    }
    Compress-Archive -Path $files.FullName -DestinationPath $repoZip -Force

    Write-Step "Copying repo archive to guest"
    & scp -i $Key -P $SshPort `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHosts" `
        $repoZip ubuntu@127.0.0.1:/home/ubuntu/reflex.zip
    if ($LASTEXITCODE -ne 0) {
        throw "scp repo archive failed"
    }

    Write-Step "Running guest setup and UnixBench comparison"
    $guestScript = @'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

wait_for_apt() {
  if command -v cloud-init >/dev/null 2>&1; then
    sudo cloud-init status --wait 2>/dev/null || true
  fi
  local n=0
  while sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
    n=$((n + 1))
    if [[ "$n" -gt 90 ]]; then
      echo "error: apt/dpkg locks still held" >&2
      exit 1
    fi
    sleep 2
  done
}

wait_for_apt
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  build-essential clang libbpf-dev bpfcc-tools python3-bpfcc \
  git make perl curl ca-certificates unzip linux-tools-common >/dev/null
if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  "linux-headers-$(uname -r)" "linux-tools-$(uname -r)" >/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    linux-headers-generic linux-tools-generic >/dev/null
fi
sudo apt-get clean

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

rm -rf /home/ubuntu/reflex
mkdir -p /home/ubuntu/reflex
unzip -q /home/ubuntu/reflex.zip -d /home/ubuntu/reflex
cd /home/ubuntu/reflex
uv venv --system-site-packages --allow-existing
uv sync

BPFTOOL_BIN="$(command -v bpftool || true)"
if [[ -z "$BPFTOOL_BIN" ]]; then
  BPFTOOL_BIN="$(find /usr/lib/linux-tools -type f -name bpftool 2>/dev/null | head -1 || true)"
fi
if [[ -z "$BPFTOOL_BIN" ]]; then
  echo "error: bpftool not found" >&2
  exit 1
fi
make -C implementations/ebpf BPFTOOL="$BPFTOOL_BIN"

UNIXBENCH_DIR="$HOME/byte-unixbench"
if [[ ! -x "$UNIXBENCH_DIR/UnixBench/Run" ]]; then
  rm -rf "$UNIXBENCH_DIR"
  git clone --depth 1 "__UNIXBENCH_URL__" "$UNIXBENCH_DIR"
fi

UNIXBENCH="$UNIXBENCH_DIR/UnixBench/Run" MODES="__MODES__" bash benchmarks/unixbench_compare.sh
'@
    $guestScript = $guestScript.Replace("__UNIXBENCH_URL__", $UnixBenchUrl).Replace("__MODES__", $Modes)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($guestScript))
    Invoke-Guest "echo $encoded | base64 -d > /home/ubuntu/run_reflex_unixbench.sh && bash /home/ubuntu/run_reflex_unixbench.sh"

    Write-Step "Copying results back to Windows host"
    & scp -r -i $Key -P $SshPort `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHosts" `
        ubuntu@127.0.0.1:/home/ubuntu/reflex/data/unixbench_results.csv `
        (Join-Path $repoRoot "data\unixbench_results.csv")
    if ($LASTEXITCODE -ne 0) {
        throw "scp unixbench_results.csv failed"
    }
    & scp -r -i $Key -P $SshPort `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHosts" `
        ubuntu@127.0.0.1:/home/ubuntu/reflex/data/runs/* `
        (Join-Path $repoRoot "data\runs\")
    if ($LASTEXITCODE -ne 0) {
        throw "scp data/runs failed"
    }

    Write-Step "Done"
    Write-Host "Results:"
    Write-Host "  data\unixbench_results.csv"
    Write-Host "  data\runs\unixbench-<timestamp>\"
}
finally {
    if (-not $KeepVm -and $qemuProc -and -not $qemuProc.HasExited) {
        Write-Step "Stopping QEMU"
        Stop-Process -Id $qemuProc.Id -Force
    }
    if (-not $KeepVm) {
        Remove-Item -Force $overlay, $seedIso, $repoZip, $KnownHosts -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $seedDir -ErrorAction SilentlyContinue
    } else {
        Write-Step "Keeping VM artifacts in $CacheDir"
    }
}
