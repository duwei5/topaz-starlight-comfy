# Pack topaz_engine into 1.9 GB volumes for upload (GitHub Releases caps a file at 2 GB).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\cloud\pack_engine.ps1 -EngineDir D:\ComfyUI\topaz_engine -OutDir D:\pack_out
# ASCII only on purpose: PowerShell 5.1 parses BOM-less .ps1 as GBK.

param(
  [string]$EngineDir = 'D:\ComfyUI\topaz_engine',
  [string]$OutDir = 'D:\pack_out',
  [int]$VolumeMB = 1900,
  [switch]$KeepOriginalZips
)

$ErrorActionPreference = 'Stop'

$sevenZip = @('C:\Program Files\7-Zip\7z.exe', 'C:\Program Files (x86)\7-Zip\7z.exe') |
  Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $sevenZip) { throw '7-Zip not found: winget install --id 7zip.7zip -e' }
if (-not (Test-Path $EngineDir)) { throw "engine dir not found: $EngineDir" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$sizeGB = [math]::Round(((Get-ChildItem $EngineDir -Recurse -File | Measure-Object Length -Sum).Sum / 1GB), 2)
Write-Host "[pack] engine: $EngineDir ($sizeGB GB)"
Write-Host "[pack] output: $OutDir  volumes: ${VolumeMB}MB"

# Exclude the original download archives unless asked - they are already unpacked.
$exArgs = @()
if (-not $KeepOriginalZips) {
  $exArgs += '-xr!*.zip'
  $exArgs += '-xr!*.7z'
}

$target = Join-Path $OutDir 'topaz_engine.7z'
if (Test-Path $target) { Remove-Item $target -Force }

Write-Host '[pack] compressing (store-ish: weights do not compress much, so this is fast)'
& $sevenZip a -t7z -mx=1 "-v${VolumeMB}m" $target $EngineDir\* @exArgs | Out-Null
if ($LASTEXITCODE -ge 2) { throw "7z failed ($LASTEXITCODE)" }

$parts = Get-ChildItem $OutDir -Filter 'topaz_engine.7z.*' | Sort-Object Name
Write-Host ("[pack] {0} volumes, {1} GB total" -f $parts.Count, [math]::Round(($parts | Measure-Object Length -Sum).Sum / 1GB, 2))
$parts | ForEach-Object { '  {0}  {1:N1} GB' -f $_.Name, ($_.Length / 1GB) }

Write-Host ''
Write-Host '[pack] next steps'
Write-Host '  a) object storage (ossutil / coscmd / rclone copy) then pull on the cloud host'
Write-Host '  b) GitHub Release asset (each volume < 2 GB, so the 1.9 GB split works):'
Write-Host '       gh release create engine-v1 --repo <owner>/<repo> ' + ($parts[0].FullName) + ' ...'
Write-Host '  c) direct RDP copy for small test sets only'
Write-Host ''
Write-Host '[pack] on the cloud host:'
Write-Host ('  7z x topaz_engine.7z.001 -o' + (Join-Path $EngineDir ''))