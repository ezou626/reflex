<#
Start a Reflex daemon inside an Ubuntu guest booted by QEMU on Windows.

This is a Windows-native runner: no Git Bash, WSL, cloud-localds, or 9p mount is
required. It creates a NoCloud seed ISO with PowerShell, boots QEMU with WHPX,
copies the repo into the guest over SSH, builds the eBPF loader, and starts the
selected daemon.

Run from the repo root:
  powershell -ExecutionPolicy Bypass -File scripts\run_in_qemu.ps1 -Daemon "heuristic"

Optional parameters:
  -Port 52222            (default SSH port)
  -DiskGB 24             (VM overlay disk size)
  -MemoryMB 4096         (VM RAM)
  -Cpus 6                (VM vCPU count)
#>

[CmdletBinding()]
param(
	[string]$Daemon = "",
	[int]$Port = 52222,
	[int]$DiskGB = 24,
	[int]$MemoryMB = 4096,
	[int]$Cpus = 6,
	[switch]$Full,
	[switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Daemon)) {
	throw "-Daemon is required. Pass a daemon name, e.g.: -Daemon `"heuristic`""
}
if ($Daemon -notmatch '^[A-Za-z0-9_.-]+$') {
	throw "Daemon name must contain only letters, numbers, '.', '_', or '-'."
}

function Write-Step {
	param([string]$Message)
	Write-Host "[run-in-qemu] $Message"
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

function Wait-ForSsh {
	param(
		[string]$KeyPath,
		[int]$Port
	)
	for ($i = 0; $i -lt 120; $i++) {
		$ErrorActionPreference = "SilentlyContinue"
		$out = & ssh -vv `
			-i $KeyPath `
			-p $Port `
			-o StrictHostKeyChecking=accept-new `
			-o UserKnownHostsFile="$KnownHosts" `
			-o ConnectTimeout=5 `
			-o BatchMode=yes `
			ubuntu@127.0.0.1 "echo ok" 2>&1
		$ErrorActionPreference = "Stop"
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
	& ssh -i $Key -p $Port `
		-o StrictHostKeyChecking=yes `
		-o UserKnownHostsFile="$KnownHosts" `
		-o ConnectTimeout=30 `
		ubuntu@127.0.0.1 $Command
	if ($LASTEXITCODE -ne 0) {
		throw "Guest command failed: $Command"
	}
}

function New-RepoArchive {
	param(
		[string]$SourceRoot,
		[string]$ZipPath
	)
	if (Test-Path $ZipPath) {
		Remove-Item -Force $ZipPath
	}
	Add-Type -AssemblyName System.IO.Compression.FileSystem
	$root = (Resolve-Path $SourceRoot).Path.TrimEnd("\", "/")
	$excludedTop = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
	@(".git", ".venv", ".testvenv", ".uv-cache", ".pytest_cache", ".ruff_cache", ".worktrees", "__pycache__") |
		ForEach-Object { [void]$excludedTop.Add($_) }
	$excludedPrefixes = @("data\qemu-windows\", "data\qemu\")

	$stream = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::CreateNew)
	$zip = [System.IO.Compression.ZipArchive]::new($stream, [System.IO.Compression.ZipArchiveMode]::Create)
	try {
		Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
			$relative = $_.FullName.Substring($root.Length).TrimStart("\", "/")
			$parts = $relative -split "[\\/]"
			if ($parts.Count -gt 0 -and $excludedTop.Contains($parts[0])) {
				return
			}
			if ($parts -contains "__pycache__") {
				return
			}
			foreach ($prefix in $excludedPrefixes) {
				if ($relative.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
					return
				}
			}
			$entryName = $relative -replace "\\", "/"
			[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
				$zip,
				$_.FullName,
				$entryName,
				[System.IO.Compression.CompressionLevel]::Optimal
			) | Out-Null
		}
	} finally {
		$zip.Dispose()
		$stream.Dispose()
	}
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$CacheDir = Join-Path $repoRoot "data\qemu-windows"
$UbuntuImageUrl = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "data") | Out-Null

# Load OPENAI_API_KEY from env or .env
$OpenAIApiKey = $env:OPENAI_API_KEY
if ([string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
	$dotEnvPath = Join-Path $repoRoot ".env"
	$OpenAIApiKey = Get-DotEnvValue -Path $dotEnvPath -Name "OPENAI_API_KEY"
	if (-not [string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
		Write-Step "Loaded OPENAI_API_KEY from .env"
	}
}

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
$runRootFile = Join-Path $CacheDir "unixbench-run-root-$PID.txt"

if (-not (Test-Path $Key)) {
	Write-Step "Generating SSH key: $Key"
	& ssh-keygen -t ed25519 -f $Key -N "" -q
	if ($LASTEXITCODE -ne 0) {
		throw "ssh-keygen failed"
	}
}
# Windows OpenSSH refuses to sign with world-readable private keys
icacls $Key /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null

if (-not (Test-Path $baseImg)) {
	Write-Step "Downloading Ubuntu cloud image"
	Invoke-WebRequest -Uri $UbuntuImageUrl -OutFile "$baseImg.part"
	Move-Item -Force "$baseImg.part" $baseImg
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

Write-Step "Creating VM overlay"
& $qemuImg create -f qcow2 -F qcow2 -b (Resolve-Path $baseImg) $overlay | Out-Host
& $qemuImg resize $overlay "${DiskGB}G" | Out-Host

Write-Step "Boosting power plan for benchmark"
powercfg -setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFBOOSTMODE 1
powercfg -setactive SCHEME_CURRENT

Write-Step "Starting QEMU on 127.0.0.1:$Port"
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
	"-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:$Port-:22",
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
	Wait-ForSsh -KeyPath $Key -Port $Port

	Write-Step "Preparing repo archive"
	New-RepoArchive -SourceRoot $repoRoot -ZipPath $repoZip

	Write-Step "Copying repo archive to guest"
	& scp -i $Key -P $Port `
		-o StrictHostKeyChecking=yes `
		-o UserKnownHostsFile="$KnownHosts" `
		$repoZip ubuntu@127.0.0.1:/home/ubuntu/reflex.zip
	if ($LASTEXITCODE -ne 0) {
		throw "scp repo archive failed"
	}

	Write-Step "Running guest setup and starting daemon"
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
unzip -q -o /home/ubuntu/reflex.zip -d /home/ubuntu/reflex
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

# Start the configured daemon as a background service.
# Use sudo + env to ensure it has required privileges and receives OPENAI key.
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
DAEMON_LOG="/home/ubuntu/reflex/daemon.log"
DAEMON_PIDFILE="/home/ubuntu/reflex/daemon.pid"
nohup sudo env OPENAI_API_KEY="$OPENAI_API_KEY" uv run reflex --no-sudo __DRYRUN__ __DAEMON__ > "$DAEMON_LOG" 2>&1 &
echo $! > "$DAEMON_PIDFILE"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DAEMON_PIDFILE" > /home/ubuntu/reflex/daemon_started.txt
'@
	$dryRunArg = if ($DryRun) { "--dry-run" } else { "" }
	$guestScript = $guestScript.Replace("__OPENAI_API_KEY_EXPORT__", $openAiExportLine).Replace("__DAEMON__", $Daemon).Replace("__DRYRUN__", $dryRunArg)
	$guestScript = $guestScript -replace "`r", ""
	$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($guestScript))
	Invoke-Guest "echo $encoded | base64 -d > /home/ubuntu/run_reflex_daemon.sh && bash /home/ubuntu/run_reflex_daemon.sh"

	Write-Step "Copying daemon start marker back to Windows host"
	& scp -r -i $Key -P $Port `
		-o StrictHostKeyChecking=yes `
		-o UserKnownHostsFile="$KnownHosts" `
		ubuntu@127.0.0.1:/home/ubuntu/reflex/daemon_started.txt `
		$runRootFile
	if ($LASTEXITCODE -ne 0) {
		throw "scp daemon_started.txt failed"
	}

	$marker = (Get-Content -Raw $runRootFile).Trim()
	Write-Step "Daemon started on guest: $marker"
}
finally {
	if ($qemuProc -and -not $qemuProc.HasExited) {
		Write-Step "Stopping QEMU"
		Stop-Process -Id $qemuProc.Id -Force
	}
	Remove-Item -Force $overlay, $seedIso, $repoZip, $KnownHosts, $runRootFile -ErrorAction SilentlyContinue
	Remove-Item -Recurse -Force $seedDir -ErrorAction SilentlyContinue
	# sweep stale artifacts from aborted or old runs (keeps base image and SSH key)
	Get-ChildItem $CacheDir -File | Where-Object {
		$_.Name -match '^(unixbench-|reflex-|known_hosts\.)' -and
		$_.FullName -notin @($Key, $pub)
	} | Remove-Item -Force -ErrorAction SilentlyContinue
	Get-ChildItem $CacheDir -Directory | Where-Object { $_.Name -match '^seed-' } |
		Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}


