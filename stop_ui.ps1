# Stop the D:\ComfyUI instance (and any leftover Topaz engine process).
# ASCII only on purpose: PowerShell 5.1 reads .ps1 without BOM as GBK.
param([int]$Port = 8189, [switch]$KeepEngine)

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'main\.py' -and $_.CommandLine -match "--port $Port" }
foreach ($p in $procs) {
  try { Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null; Write-Host "stopped python pid $($p.ProcessId)" }
  catch { Write-Host "could not stop $($p.ProcessId): $($_.Exception.Message)" }
}

if (-not $KeepEngine) {
  $eng = Get-Process neuroserver -ErrorAction SilentlyContinue
  foreach ($e in $eng) {
    try { Stop-Process -Id $e.Id -Force; Write-Host "stopped neuroserver pid $($e.Id)" } catch { }
  }
}

if (-not $procs) { Write-Host "nothing running on port $Port" }