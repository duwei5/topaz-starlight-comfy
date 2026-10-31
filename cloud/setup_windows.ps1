# Cloud bootstrap for a Windows GPU instance (Topaz engine is Windows-only).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\cloud\setup_windows.ps1 -Root D:\ComfyUI -EngineArchive E:\topaz_engine.7z.001
# ASCII only on purpose: PowerShell 5.1 parses BOM-less .ps1 as GBK.

param(
  [string]$Root = 'D:\ComfyUI',
  [string]$Repo = (Split-Path $PSScriptRoot -Parent),
  [int]$Port = 8189,
  [string]$EngineArchive = '',
  [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host "[sr] $m" }
function Warn($m) { Write-Host "[sr] WARN: $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 1. hardware
Say 'checking GPU'
$smi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
if (-not (Test-Path $smi)) { throw 'nvidia-smi not found - this instance has no NVIDIA driver' }
$gpu = & $smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
Say ("GPU: " + ($gpu -join ' | '))
$vram = [int](($gpu -split ',')[1] -replace '[^0-9]', '')
if ($vram -lt 16000) { Warn ("only {0} MB VRAM; Starlight 2.6 (5B) wants 24GB+ for full residency" -f $vram) }

# ------------------------------------------------------------------- 2. disk
$drive = (Split-Path $Root -Qualifier)
$free = (Get-PSDrive ($drive -replace ':','')) .Free
$freeGB = [math]::Round($free / 1GB, 1)
Say ("free space on {0}: {1} GB" -f $drive, $freeGB)
if ($freeGB -lt 55) { Warn 'need about 55 GB (12.5 GB engine + 15 GB weights + scratch)' }

# --------------------------------------------------------- 3. ComfyUI present
$mainPy = Join-Path $Root 'main.py'
if (-not (Test-Path $mainPy)) {
  Warn "no main.py at $Root - install ComfyUI first"
  Say '  portable build: download ComfyUI_windows_portable_nvidia.7z from'
  Say '  https://github.com/comfyanonymous/ComfyUI/releases and extract it here'
  Say '  (that build ships its own python_embeded + .venv, set -Root to its ComfyUI subfolder)'
  throw 'aborted: ComfyUI core missing'
}

$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
  $py = Join-Path $Root 'python_embeded\python.exe'
}
if (-not (Test-Path $py)) { throw "no python found under $Root (looked for .venv and python_embeded)" }
Say ("python: " + $py)
& $py -c "import torch;print('[sr] torch',torch.__version__,'cuda',torch.version.cuda,'avail',torch.cuda.is_available())"
if ($LASTEXITCODE -ne 0) { throw 'torch is missing in that python' }

# ------------------------------------------------------------ 4. layout + node
foreach ($d in 'input', 'output', 'temp', 'custom_nodes') {
  $p = Join-Path $Root $d
  if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}
$nodeSrc = Join-Path $Repo 'nodes\ComfyUI-TopazStarlight'
$nodeDst = Join-Path $Root 'custom_nodes\ComfyUI-TopazStarlight'
if (-not (Test-Path $nodeSrc)) { throw "node source missing: $nodeSrc" }
Say "copying node -> $nodeDst"
& robocopy $nodeSrc $nodeDst /E /NFL /NDL /NJH /NJS /NP /XF *.pyc /R:1 /W:1 | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }

Copy-Item (Join-Path $Repo 'sr_run.py') (Join-Path $Root 'sr_run.py') -Force
Copy-Item (Join-Path $Repo 'start_ui.ps1') (Join-Path $Root 'start_ui.ps1') -Force
Copy-Item (Join-Path $Repo 'stop_ui.ps1') (Join-Path $Root 'stop_ui.ps1') -Force

# --------------------------------------------------------------- 5. engine
$eng = Join-Path $Root 'topaz_engine'
$engExe = Join-Path $eng 'neuroserver171\neuroserver.exe'
if ($EngineArchive) {
  if (-not (Test-Path $EngineArchive)) { throw "engine archive not found: $EngineArchive" }
  $sevenZip = @('C:\Program Files\7-Zip\7z.exe', 'C:\Program Files (x86)\7-Zip\7z.exe') |
    Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $sevenZip) {
    Say 'installing 7-Zip via winget'
    & winget install --id 7zip.7zip -e --accept-source-agreements --accept-package-agreements | Out-Host
    $sevenZip = 'C:\Program Files\7-Zip\7z.exe'
  }
  Say "extracting engine -> $eng"
  & $sevenZip x $EngineArchive "-o$eng" -y | Out-Null
}

if (-not (Test-Path $engExe)) { throw "engine missing: $engExe (pass -EngineArchive or copy topaz_engine manually)" }

# ------------------------------------------------------------ 6. env self-check
Say 'running self-check'
& $py (Join-Path $Root 'sr_run.py') --env --root $Root
$envOk = ($LASTEXITCODE -eq 0)

# ---------------------------------------------------------------- 7. start it
Say "starting ComfyUI on port $Port"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'start_ui.ps1') -Port $Port
if ($envOk -eq $false) { Warn 'self-check reported missing pieces; server may start but the node will fail' }

# ----------------------------------------------------------------- 8. smoke
if (-not $SkipSmoke) {
  $anyMp4 = Get-ChildItem (Join-Path $Root 'input') -Filter *.mp4 -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($anyMp4) {
    Say ("smoke test on " + $anyMp4.Name + " (10 frames)")
    & $py (Join-Path $Root 'sr_run.py') -v $anyMp4.FullName -c 10 -m 20 -o test --root $Root
  } else {
    Say "put an mp4 into $Root\input then run: python sr_run.py -v <file> -c 10 -m 20 -o test"
  }
}

Say ("done. GUI: http://127.0.0.1:" + $Port)