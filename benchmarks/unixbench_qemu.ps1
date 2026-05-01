<#
Run the UnixBench comparison inside an Ubuntu guest booted by QEMU on Windows.

This is a Windows-native runner: no Git Bash, WSL, cloud-localds, or 9p mount is
required. It creates a NoCloud seed ISO with PowerShell, boots QEMU with WHPX,
copies the repo into the guest over SSH, and runs benchmarks/unixbench_compare.sh.

Run from the repo root:
  powershell -ExecutionPolicy Bypass -File benchmarks\unixbench_qemu_windows.ps1 `
      -Modes "workload_only,heuristic,classifier" -Full

NOTE: Quote the -Modes value. In powershell.exe -File mode, unquoted commas split
into separate positional arguments and cause parameter binding errors.

Useful parameters:
  -Modes "workload_only,heuristic"   (quoted, comma-separated)
  -Targeted                          run only eBPF-sensitive tests: pipe, context1, syscall, spawn, fsbuffer
  -Full                              run full UnixBench suite (-i 3); default is fast (-i 1 dhry2reg whetstone-double)
  -DryRunOnly                        run non-workload modes in dry-run only (no live tuning runs)
  -DryRun                            run each mode twice: once live, once with --dry-run
  -SshPort 52223
  -DiskGB 32
  -OpenAIApiKey "<key>"              (optional; defaults to host OPENAI_API_KEY or .env)
#>

[CmdletBinding()]
param(
    [string]$Modes = "",
    [int]$SshPort = 52222,
    [int]$DiskGB = 24,
    [int]$MemoryMB = 4096,
    [int]$Cpus = 6,
    [string]$CacheDir = "",
    [string]$OpenAIApiKey = "",
    [string]$UbuntuImageUrl = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    [string]$UnixBenchUrl = "https://github.com/kdlucas/byte-unixbench.git",
    [switch]$KeepVm,
    [switch]$Full,
    [switch]$Targeted,
    [switch]$DryRun,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Modes)) {
    throw "-Modes is required. Pass a quoted comma-separated list, e.g.: -Modes `"workload_only,heuristic,classifier`""
}

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

function Reset-HostKeyForPort {
    param([int]$Port)
    $knownHosts = Join-Path $env:USERPROFILE ".ssh\known_hosts"
    if (-not (Test-Path $knownHosts)) {
        return
    }
    & ssh-keygen -R "[127.0.0.1]:$Port" -f $knownHosts | Out-Null
}

function Get-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name
    )
    if (-not (Test-Path $Path)) {
        return $null
    }
    foreach ($rawLine in Get-Content $Path) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        if ($line -match '^\s*export\s+') {
            $line = $line -replace '^\s*export\s+', ''
        }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) {
            continue
        }
        $key = $parts[0].Trim()
        if ($key -ne $Name) {
            continue
        }
        $value = $parts[1].Trim()
        if ($value.Length -ge 2) {
            if (($value.StartsWith("'") -and $value.EndsWith("'")) -or ($value.StartsWith('"') -and $value.EndsWith('"'))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        return $value
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
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $out = & ssh -vv `
            -F NUL `
            -i $KeyPath `
            -p $Port `
            -o IdentitiesOnly=yes `
            -o StrictHostKeyChecking=accept-new `
            -o UserKnownHostsFile="$KnownHosts" `
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
            $out | Select-Object -Last 12 | ForEach-Object { Write-Host "  $_" }
        }
        Start-Sleep -Seconds 5
    }
    throw "SSH did not become ready on port $Port"
}

function Invoke-Guest {
    param([string]$Command)
    & ssh -i $Key -p $SshPort `
        -F NUL `
        -o IdentitiesOnly=yes ` `
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

if ([string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
    $OpenAIApiKey = $env:OPENAI_API_KEY
}
if ([string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
    $dotEnvPath = Join-Path $repoRoot ".env"
    $OpenAIApiKey = Get-DotEnvValue -Path $dotEnvPath -Name "OPENAI_API_KEY"
    if (-not [string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
        Write-Step "Loaded OPENAI_API_KEY from .env"
    }
}

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

$baseImg = Join-Path $CacheDir "noble-unixbench-deps-amd64.img"
$overlay = Join-Path $CacheDir "unixbench-overlay-$PID.qcow2"
$seedIso = Join-Path $CacheDir "unixbench-seed-$PID.iso"
$seedDir = Join-Path $CacheDir "seed-$PID"
$consoleLog = Join-Path $CacheDir "unixbench-console-$PID.log"
$qemuLog = Join-Path $CacheDir "unixbench-qemu-$PID.log"
$Key = Join-Path $CacheDir "id_ed25519"
$pub = "$Key.pub"
$KnownHosts = Join-Path $CacheDir "known_hosts.unixbench.$PID"
$repoTar = Join-Path $CacheDir "reflex-$PID.tar.gz"
$runRootFile = Join-Path $CacheDir "unixbench-run-root-$PID.txt"

Reset-HostKeyForPort -Port $SshPort

Write-Step "Generating SSH key: $Key"
Remove-Item -Force $Key, $pub -ErrorAction SilentlyContinue
& ssh-keygen -t ed25519 -f $Key -N '""' -q
if ($LASTEXITCODE -ne 0) {
    throw "ssh-keygen failed"
}
# Windows OpenSSH refuses to sign with world-readable private keys
icacls $Key /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null

if (-not (Test-Path $baseImg)) {
    $bakeScript = Join-Path $PSScriptRoot "bake_unixbench_image.ps1"
    if (Test-Path $bakeScript) {
        Write-Step "Prebaked image missing; building via bake_unixbench_image.ps1"
        $bakePort = $SshPort + 100
        if ($bakePort -gt 65535) { $bakePort = 52223 }
        & powershell -ExecutionPolicy Bypass -File $bakeScript -CacheDir $CacheDir -SourceImageUrl $UbuntuImageUrl -OutputImageName "noble-unixbench-deps-amd64.img" -SshPort $bakePort -MemoryMB $MemoryMB -Cpus $Cpus
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to bake image via $bakeScript"
        }
    } else {
        throw "Prebaked image missing and bake script not found: $bakeScript"
    }
    if (-not (Test-Path $baseImg)) {
        throw "Expected prebaked image not found after bake: $baseImg"
    }
}

Write-Step "Creating cloud-init seed ISO"
New-Item -ItemType Directory -Force -Path $seedDir | Out-Null
$pubLine = (Get-Content -Raw $pub).Trim()
[System.IO.File]::WriteAllText(
    (Join-Path $seedDir "meta-data"),
    @"
instance-id: iid-reflex-win-unixbench-$PID
local-hostname: reflex-win-unixbench
"@,
    (New-Object System.Text.UTF8Encoding($false))
)
[System.IO.File]::WriteAllText(
    (Join-Path $seedDir "user-data"),
    @"
#cloud-config
users:
  - default

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

if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
    Write-Step "Shutting down WSL before starting QEMU"
    wsl --shutdown | Out-Null
}

Write-Step "Creating VM overlay"
& $qemuImg create -f qcow2 -F qcow2 -b (Resolve-Path $baseImg) $overlay | Out-Host
& $qemuImg resize $overlay "${DiskGB}G" | Out-Host

Write-Step "Boosting power plan for benchmark"
powercfg -setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFBOOSTMODE 1
powercfg -setactive SCHEME_CURRENT

Write-Step "Starting QEMU on 127.0.0.1:$SshPort"
$qemuArgs = @(
    "-machine", "type=q35,accel=whpx",
    "-smbios", "type=1,serial=ds=nocloud",
    "-cpu", "qemu64",
    "-smp", "$Cpus",
    "-m", "$MemoryMB",
    "-display", "none",
    "-serial", "file:$consoleLog",
    "-drive", "file=$overlay,if=virtio,cache=writeback",
    "-cdrom", "$seedIso",
    "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:$SshPort-:22",
    "-device", "e1000,netdev=net0"
)
$qemuProc = Start-Process -FilePath $qemu -ArgumentList $qemuArgs -RedirectStandardError $qemuLog -PassThru -WindowStyle Hidden
try {
    # Prefer the highest practical priority for benchmark stability.
    $qemuProc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::RealTime
} catch {
    try {
        $qemuProc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::High
    } catch {
        Write-Step "Warning: Could not raise QEMU process priority."
    }
}
$qemuProc.ProcessorAffinity = [IntPtr]0xFF  # P-cores; adjust mask as needed

try {
    New-Item -ItemType File -Force -Path $KnownHosts | Out-Null
    Write-Step "Waiting for SSH"
    try {
        Wait-ForSsh -KeyPath $Key -Port $SshPort
    } catch {
        Write-Step "SSH did not become ready; dumping recent VM logs"
        if (Test-Path $consoleLog) {
            Write-Host "---- console log (last 80 lines) ----"
            Get-Content $consoleLog -Tail 80 | ForEach-Object { Write-Host $_ }
        }
        if (Test-Path $qemuLog) {
            Write-Host "---- qemu stderr log (last 80 lines) ----"
            Get-Content $qemuLog -Tail 80 | ForEach-Object { Write-Host $_ }
        }
        throw
    }

    Write-Step "Preparing repo archive"
    if (Test-Path $repoTar) { Remove-Item -Force $repoTar }
    $wslRoot = (wsl wslpath -u ($repoRoot.ToString().Replace('\', '/'))).Trim()
    $wslTar  = (wsl wslpath -u ($repoTar.Replace('\', '/'))).Trim()
    wsl bash -c "cd '$wslRoot' && tar -czf '$wslTar' --exclude='./.git' --exclude='./.venv' --exclude='./.uv-cache' --exclude='./.pytest_cache' --exclude='./.ruff_cache' --exclude='./.mypy_cache' --exclude='./.cache' --exclude='./data/qemu-windows' --exclude='./data/qemu' --exclude='./__pycache__' --exclude='./.worktrees' ."
    if ($LASTEXITCODE -ne 0) { throw "repo tar creation failed" }

    Write-Step "Copying repo archive to guest"
    & scp -i $Key -P $SshPort `
        -F NUL `
        -o IdentitiesOnly=yes ` `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHosts" `
        $repoTar ubuntu@127.0.0.1:/home/ubuntu/reflex.tar.gz
    if ($LASTEXITCODE -ne 0) {
        throw "scp repo archive failed"
    }

    Write-Step "Running guest setup and UnixBench comparison"
    $openAiExportLine = ""
    if (-not [string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
        # Avoid brittle quote escaping by passing the key as base64 and decoding in guest.
        $keyBytes = [Text.Encoding]::UTF8.GetBytes($OpenAIApiKey)
        $keyB64 = [Convert]::ToBase64String($keyBytes)
        $openAiExportLine = "export OPENAI_API_KEY=`$(printf '%s' '$keyB64' | base64 -d)"
    } else {
        Write-Step "OPENAI_API_KEY not provided; OpenAI controller runs will no-op."
    }

    $guestScript = @'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
__OPENAI_API_KEY_EXPORT__

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
  git make perl curl ca-certificates linux-tools-common >/dev/null
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
tar -xzf /home/ubuntu/reflex.tar.gz -C /home/ubuntu/reflex
find /home/ubuntu/reflex -name "*.sh" -exec sed -i 's/\r//' {} +
cd /home/ubuntu/reflex
uv venv --system-site-packages --allow-existing
uv sync --extra openai

BPFTOOL_BIN="$(command -v bpftool || true)"
if [[ -z "$BPFTOOL_BIN" ]]; then
  BPFTOOL_BIN="$(find /usr/lib/linux-tools -type f -name bpftool 2>/dev/null | head -1 || true)"
fi
if [[ -z "$BPFTOOL_BIN" ]]; then
  echo "error: bpftool not found" >&2
  exit 1
fi
"$BPFTOOL_BIN" btf dump file /sys/kernel/btf/vmlinux format c > src/vmlinux.h
make -C src/reflex/implementations/ebpf BPFTOOL="$BPFTOOL_BIN"

UNIXBENCH_DIR="$HOME/byte-unixbench"
if [[ ! -x "$UNIXBENCH_DIR/UnixBench/Run" ]]; then
  rm -rf "$UNIXBENCH_DIR"
  git clone --depth 1 "__UNIXBENCH_URL__" "$UNIXBENCH_DIR"
fi

RUN_ROOT="/home/ubuntu/reflex/data/runs/unixbench-$(date +%Y%m%d-%H%M%S)"
UNIXBENCH="$UNIXBENCH_DIR/UnixBench/Run" RUN_ROOT="$RUN_ROOT" bash benchmarks/unixbench_compare.sh --modes "__MODES__" __SUITE__ __DRYRUN__
printf '%s\n' "$RUN_ROOT" > /home/ubuntu/reflex/data/last_unixbench_run_root.txt
'@
    $suiteArg  = if ($Targeted) { "--targeted" } elseif ($Full) { "--full" } else { "--fast" }
    $dryRunArg = if ($DryRunOnly) { "--dry-run-only" } elseif ($DryRun) { "--dry-run" } else { "" }
    $guestScript = $guestScript.Replace("__UNIXBENCH_URL__", $UnixBenchUrl).Replace("__MODES__", $Modes).Replace("__SUITE__", $suiteArg).Replace("__DRYRUN__", $dryRunArg).Replace("__OPENAI_API_KEY_EXPORT__", $openAiExportLine)
    $guestScript = $guestScript -replace "`r", ""
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($guestScript))
    Invoke-Guest "echo $encoded | base64 -d > /home/ubuntu/run_reflex_unixbench.sh && bash /home/ubuntu/run_reflex_unixbench.sh"

    Write-Step "Copying results back to Windows host"
    & scp -r -i $Key -P $SshPort `
        -F NUL `
        -o IdentitiesOnly=yes ` `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHosts" `
        ubuntu@127.0.0.1:/home/ubuntu/reflex/data/last_unixbench_run_root.txt `
        $runRootFile
    if ($LASTEXITCODE -ne 0) {
        throw "scp last_unixbench_run_root.txt failed"
    }

    $guestRunRoot = (Get-Content -Raw $runRootFile).Trim()
    if ([string]::IsNullOrWhiteSpace($guestRunRoot)) {
        throw "Could not determine guest run root"
    }
    $runName = Split-Path -Leaf $guestRunRoot

    & scp -r -i $Key -P $SshPort `
        -F NUL `
        -o IdentitiesOnly=yes ` `
        -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile="$KnownHosts" `
        "ubuntu@127.0.0.1:$guestRunRoot" `
        (Join-Path $repoRoot "data\runs\")
    if ($LASTEXITCODE -ne 0) {
        throw "scp run dir failed: $guestRunRoot"
    }

    Write-Step "Done"
    Write-Host "Results:"
    Write-Host "  data\runs\$runName\"
}
finally {
    if (-not $KeepVm -and $qemuProc -and -not $qemuProc.HasExited) {
        Write-Step "Stopping QEMU"
        Stop-Process -Id $qemuProc.Id -Force
    }
    if (-not $KeepVm) {
        Remove-Item -Force $overlay, $seedIso, $repoTar, $KnownHosts, $runRootFile -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $seedDir -ErrorAction SilentlyContinue
    } else {
        Write-Step "Keeping VM artifacts in $CacheDir"
    }
    # sweep stale artifacts from aborted or old runs (keeps base image and SSH key)
    Get-ChildItem $CacheDir -File | Where-Object {
        $_.Name -match '^(unixbench-|reflex-|known_hosts\.)' -and
        $_.FullName -notin @($Key, $pub)
    } | Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem $CacheDir -Directory | Where-Object { $_.Name -match '^seed-' } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
