# Start the D:\ComfyUI instance (Topaz Starlight) on port 8189.
# ASCII only on purpose: PowerShell 5.1 reads .ps1 without BOM as GBK.
param([int]$Port = 8189)

$root = 'D:\ComfyUI'
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv python missing: $py" }

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'main\.py' -and $_.CommandLine -match "--port $Port" }
if ($running) {
  Write-Host "already running on port $Port (pid $($running.ProcessId -join ','))"
  Write-Host "GUI: http://127.0.0.1:$Port"
  return
}

$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
Start-Process -FilePath $py `
  -ArgumentList @('main.py', '--port', "$Port", '--listen', '127.0.0.1',
                  '--input-directory', (Join-Path $root 'input'),
                  '--output-directory', (Join-Path $root 'output')) `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $root "log_out_$ts.txt") `
  -RedirectStandardError  (Join-Path $root "log_err_$ts.txt")

Write-Host "starting... logs: log_out_$ts.txt / log_err_$ts.txt"
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 4
  try {
    $null = Invoke-RestMethod "http://127.0.0.1:$Port/object_info" -TimeoutSec 8
    Write-Host "ready: http://127.0.0.1:$Port"
    return
  } catch { }
}
Write-Host "not ready after 2 min - check log_err_$ts.txt"