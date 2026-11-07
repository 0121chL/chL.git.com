<#
    一键启动：体检 → 起 Mosquitto / backend / rule_engine / simulator / Next → 打开浏览器
    用法：
        START.cmd                     # 双击或命令行，默认离线模板（快）
        START.cmd -Backend local      # 用本地 Qwen2.5-3B（CPU，慢）
        START.cmd -Check              # 只体检，不启动
        START.cmd -NoSimulator        # 不起传感器
#>
param(
    [ValidateSet("offline", "local", "ollama", "auto")]
    [string]$Backend = "offline",
    [switch]$Check,
    [switch]$NoSimulator
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root "data\logs"
$PidFile = Join-Path $Root "data\demo.pids.json"
$NodeDir = Join-Path $Root ".tools\node-v22.20.0-win-x64"
$Python = "python"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "data") | Out-Null

function Test-Port($port) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $port)
        $c.Close()
        return $true
    } catch { return $false }
}

function Wait-Http($url, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true } catch { Start-Sleep -Milliseconds 800 }
    }
    return $false
}

$results = @()
function Add-Result($name, $ok, $detail) {
    $script:results += [pscustomobject]@{ Check = $name; Status = $(if ($ok) { "PASS" } else { "FAIL" }); Detail = $detail }
    Write-Host ("  [{0}] {1,-22} {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $name, $detail) `
        -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
}

Write-Host "`n=== 1. 环境体检 ===" -ForegroundColor Cyan

$pyVer = & $Python --version 2>&1
Add-Result "Python" ($LASTEXITCODE -eq 0) "$pyVer"

$ErrorActionPreference = "Continue"
& $Python -c 'import fastapi, uvicorn, paho.mqtt.client, langgraph' 2>$null | Out-Null
Add-Result "Python 依赖" ($LASTEXITCODE -eq 0) "fastapi / uvicorn / paho / langgraph"
$ErrorActionPreference = "Stop"

$nodeExe = Join-Path $NodeDir "node.exe"
if (-not (Test-Path $nodeExe)) { $nodeExe = "node" }
$nodeVer = & $nodeExe --version 2>&1
Add-Result "Node.js" ($LASTEXITCODE -eq 0) "$nodeVer"
Add-Result "前端依赖" (Test-Path (Join-Path $Root "web\node_modules\next")) "web/node_modules"

$mqRunning = Test-Port 1883
Add-Result "Mosquitto :1883" $mqRunning $(if ($mqRunning) { "已监听" } else { "未运行，启动时自动拉起" })

$localModel = Test-Path (Join-Path $Root ".models\Qwen2.5-3B-Instruct\config.json")
Add-Result "本地 Qwen2.5-3B" $localModel $(if ($localModel) { "已就绪" } else { "未下载（可用离线模板）" })

$busy = @()
foreach ($p in 8000, 3000) { if (Test-Port $p) { $busy += $p } }
Add-Result "端口 8000/3000" ($busy.Count -eq 0) $(if ($busy.Count -eq 0) { "空闲" } else { "被占用：$($busy -join ',')" })

$blocking = @($results | Where-Object { $_.Status -eq "FAIL" -and $_.Check -notin @("Mosquitto :1883", "本地 Qwen2.5-3B") })
if ($Check) {
    if ($blocking.Count -gt 0) { Write-Host "存在阻断项。" -ForegroundColor Red; exit 1 }
    Write-Host "体检通过。" -ForegroundColor Green
    exit 0
}
if ($blocking.Count -gt 0) { Write-Host "存在阻断项，请先处理上面的 FAIL 项。" -ForegroundColor Red; exit 1 }
if ($busy.Count -gt 0) {
    Write-Host "端口 8000/3000 被占用，先运行 STOP.cmd 或结束占用进程。" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== 2. 启动服务 ===" -ForegroundColor Cyan
$pids = @{}

# 1) Mosquitto
if (-not (Test-Port 1883)) {
    Write-Host "  [启动] Mosquitto ..." -ForegroundColor Yellow
    try { Start-Service mosquitto -ErrorAction Stop } catch {
        $mosq = "C:\Program Files\mosquitto\mosquitto.exe"
        if (-not (Test-Path $mosq)) { Write-Host "  找不到 mosquitto.exe，请先安装。" -ForegroundColor Red; exit 1 }
        $p = Start-Process -FilePath $mosq -ArgumentList "-c", (Join-Path $Root "config\mosquitto-filelog.conf") `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "mosquitto.out") `
            -RedirectStandardError (Join-Path $LogDir "mosquitto.err")
        $pids["mosquitto"] = $p.Id
        Start-Sleep -Seconds 2
    }
    if (-not (Test-Port 1883)) { Write-Host "  broker 未能监听 1883。" -ForegroundColor Red; exit 1 }
}
Write-Host "  [就绪] Mosquitto :1883" -ForegroundColor Green

# 2) 后端
$env:LLM_BACKEND = $Backend
$backendProc = Start-Process -FilePath $Python -ArgumentList "-X", "utf8", "-u", "backend_server.py" `
    -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "backend.log") -RedirectStandardError (Join-Path $LogDir "backend.err")
$pids["backend"] = $backendProc.Id
Write-Host "  [启动] backend_server :8000  (LLM_BACKEND=$Backend)" -ForegroundColor Green
if (-not (Wait-Http "http://127.0.0.1:8000/api/status" 40)) {
    Write-Host "  后端启动失败，看 data\logs\backend.err" -ForegroundColor Red
    Get-Content (Join-Path $LogDir "backend.err") -Tail 15 -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "  [就绪] http://127.0.0.1:8000" -ForegroundColor Green

# 3) 规则引擎
$rule = Start-Process -FilePath $Python -ArgumentList "-X", "utf8", "-u", "rule_engine.py" `
    -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "rule_engine.log") -RedirectStandardError (Join-Path $LogDir "rule_engine.err")
$pids["rule_engine"] = $rule.Id
Write-Host "  [启动] rule_engine（订阅 line1/telemetry）" -ForegroundColor Green

# 4) 传感器
if (-not $NoSimulator) {
    $sim = Start-Process -FilePath $Python -ArgumentList "-X", "utf8", "-u", "sensor_simulator.py" `
        -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "simulator.log") -RedirectStandardError (Join-Path $LogDir "simulator.err")
    $pids["sensor_simulator"] = $sim.Id
    Write-Host "  [启动] sensor_simulator（1 Hz + 心跳）" -ForegroundColor Green
}

# 5) 前端
$env:PATH = "$NodeDir;$env:PATH"
$next = Start-Process -FilePath $nodeExe -ArgumentList "node_modules\next\dist\bin\next", "dev", "-p", "3000" `
    -WorkingDirectory (Join-Path $Root "web") -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "web.log") -RedirectStandardError (Join-Path $LogDir "web.err")
$pids["web"] = $next.Id
Write-Host "  [启动] next dev :3000" -ForegroundColor Green

if (-not (Wait-Http "http://localhost:3000/" 120)) {
    Write-Host "  前端启动超时，看 data\logs\web.err" -ForegroundColor Red
} else {
    Write-Host "  [就绪] http://localhost:3000" -ForegroundColor Green
}

$pids | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

Write-Host "`n=== 3. 服务一览 ===" -ForegroundColor Cyan
Write-Host "  Mosquitto        mqtt://127.0.0.1:1883"
Write-Host "  backend_server   http://127.0.0.1:8000   (LLM_BACKEND=$Backend)"
Write-Host "  rule_engine      订阅 telemetry → events.jsonl + POST /events"
Write-Host "  sensor_simulator 1 Hz + 心跳"
Write-Host "  看板             http://localhost:3000"
Write-Host ""
Write-Host "  现场触发：demo_trigger.cmd   （注入 ch4_rise）" -ForegroundColor Yellow
Write-Host "  停止全部：STOP.cmd" -ForegroundColor Yellow
Write-Host ""

Start-Process "http://localhost:3000"
exit 0
