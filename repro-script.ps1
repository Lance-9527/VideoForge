$ErrorActionPreference='Continue'
$exe='D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\.desktop-build\targets\win-x64\unsigned-artifacts\win-unpacked\resources\videoforge-sidecar\VideoForgeService.exe'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$env:VIDEOFORGE_PORT='8798'
$env:VIDEOFORGE_HOST='127.0.0.1'
$env:VIDEOFORGE_DATA_DIR="$env:LOCALAPPDATA\VideoForge\data"
$env:PYTHONUNBUFFERED='1'
$p = Start-Process -FilePath $exe -ArgumentList '--service' -WorkingDirectory (Split-Path $exe) -PassThru -WindowStyle Hidden -RedirectStandardOutput 'D:\VideoForge-dsh\sidecar-out.log' -RedirectStandardError 'D:\VideoForge-dsh\sidecar-err.log'
Write-Output "sidecar pid=$($p.Id) port=8798"
for($i=0;$i -lt 40;$i++){ Start-Sleep -Milliseconds 800; try { $r=Invoke-WebRequest 'http://127.0.0.1:8798/api/health' -TimeoutSec 2 -UseBasicParsing; if($r.StatusCode -eq 200){ Write-Output "healthy after $($i+1)"; break } } catch {} }
# 复现：带 idea 跑剧本
$body = '{"project_id":"114dcc06-8762-4b6a-8475-5700b717903d","stage":"script","params":{"idea":"雨夜便利店，12 秒，日系冷调"}}'
$run = Invoke-WebRequest -Uri 'http://127.0.0.1:8798/api/v2/run' -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 20
Write-Output "run = $($run.Content)"
$jid = ($run.Content | ConvertFrom-Json).data.job_id
for($i=0;$i -lt 40;$i++){
  Start-Sleep -Seconds 2
  try {
    $j = (Invoke-WebRequest "http://127.0.0.1:8798/api/v2/jobs/$jid" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
    $st = $j.data.status
    if($st -notin @('running','queued','pending')){ Write-Output "FINAL=$st msg=$($j.data.message)"; break }
  } catch { }
}
Write-Output "`n===== sidecar stderr tail ====="
Get-Content 'D:\VideoForge-dsh\sidecar-err.log' -Tail 60 -ErrorAction SilentlyContinue
Write-Output "`n===== sidecar stdout tail ====="
Get-Content 'D:\VideoForge-dsh\sidecar-out.log' -Tail 40 -ErrorAction SilentlyContinue
Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
