<#
    一键启动：检查环境 → 启动 Mosquitto / backend_server / rule_engine / sensor_simulator / Next.js
    用法：
        powershell -ExecutionPolicy Bypass -File .\start_demo.ps1
        powershell -ExecutionPolicy Bypass -File .\start_demo.ps1 -Check      # 只体检，不启动
#>
param(
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

$results = @()
function Add-Result($name, $ok, $detail) {
    $script:results += [pscustomobject]@{ Check = $name; Status = $(if ($ok) { "PASS" } else { "FAIL" }); Detail = $detail }
    $color = if ($ok) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1,-26} {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $name, $detail) -ForegroundColor $color
}

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

Write-Host "`n=== 1. 环境体检 ===" -ForegroundColor Cyan

# Python
$pyOk = $false
try {
    $pyVer = & $Python --version 2>&1
    $pyOk = $LASTEXITCODE -eq 0
    Add-Result "Python" $pyOk "$pyVer"
} catch { Add-Result "Python" $false "找不到 python.exe" }

# Python 依赖
$ErrorActionPreference = "Continue"
$pyDeps = 'import fastapi, uvicorn, paho.mqtt.client, langgraph, pydantic'
& $Python -c $pyDeps 2>$null | Out-Null
$depsOk = $LASTEXITCODE -eq 0
Add-Result "Python 依赖" $depsOk $(if ($depsOk) { "fastapi / uvicorn / paho / langgraph" } else { "导入失败，检查 pip install -r requirements.txt" })

$ragDeps = 'import sentence_transformers, faiss'
& $Python -c $ragDeps 2>$null | Out-Null
$ragOk = $LASTEXITCODE -eq 0
Add-Result "RAG 依赖" $ragOk $(if ($ragOk) { "sentence-transformers + faiss" } else { "缺失：pip install -r requirements.txt" })
$ErrorActionPreference = "Stop"

# Mosquitto
$mqRunning = Test-Port 1883
Add-Result "Mosquitto :1883" $mqRunning $(if ($mqRunning) { "已监听" } else { "未运行，脚本会尝试启动" })

# Ollama
$ollamaOk = $false; $modelOk = $false
try {
    $tags = Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
    $ollamaOk = $true
    $modelOk = ($tags.models | ForEach-Object { $_.name }) -contains "qwen2.5:7b"
} catch { }
Add-Result "Ollama :11434" $ollamaOk $(if ($ollamaOk) { "在线" } else { "未运行 → 将回退离线模板" })
Add-Result "模型 qwen2.5:7b" $modelOk $(if ($modelOk) { "已拉取" } else { "缺失：ollama pull qwen2.5:7b" })

# Node
$nodeExe = Join-Path $NodeDir "node.exe"
if (-not (Test-Path $nodeExe)) { $nodeExe = "node" }
$nodeVer = & $nodeExe --version 2>&1
$nodeOk = $LASTEXITCODE -eq 0
Add-Result "Node.js" $nodeOk "$nodeVer"
$webModules = Test-Path (Join-Path $Root "web\node_modules\next")
Add-Result "前端依赖" $webModules $(if ($webModules) { "web/node_modules 就绪" } else { "缺失：cd web; npm install" })

# 端口
foreach ($p in 8000, 3000) {
    $busy = Test-Port $p
    Add-Result "端口 $p" (-not $busy) $(if ($busy) { "已被占用" } else { "空闲" })
}

$fails = @($results | Where-Object { $_.Status -eq "FAIL" -and $_.Check -notin @("Ollama :11434", "模型 qwen2.5:7b", "Mosquitto :1883", "端口 8000", "端口 3000") })
if ($Check) {
    $results | Format-Table -AutoSize
    if ($fails.Count -gt 0) { Write-Host "存在阻断项，未启动。" -ForegroundColor Red; exit 1 }
    Write-Host "体检通过（Ollama 缺失只影响分析质量，不阻断启动）。" -ForegroundColor Green
    exit 0
}

if ($fails.Count -gt 0) {
    Write-Host "存在阻断项，请按上面的提示处理后重试。" -ForegroundColor Red
    exit 1
}

# 端口被占时直接失败（避免启动到一半冲突）
if ((Test-Port 8000) -or (Test-Port 3000)) {
    Write-Host "端口 8000/3000 已被占用。先运行 .\stop_demo.ps1 或手动结束占用进程。" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== 2. 启动服务 ===" -ForegroundColor Cyan
$pids = @{}

# Mosquitto
if (-not (Test-Port 1883)) {
    $mosq = "C:\Program Files\mosquitto\mosquitto.exe"
    if (-not (Test-Path $mosq)) { Write-Host "找不到 $mosq，请先安装 Mosquitto 或手动启动 broker。" -ForegroundColor Red; exit 1 }
    $p = Start-Process -FilePath $mosq -ArgumentList "-c", (Join-Path $Root "config\mosquitto.conf") -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "mosquitto.log") -RedirectStandardError (Join-Path $LogDir "mosquitto.err")
    $pids["mosquitto"] = $p.Id
    Start-Sleep -Seconds 2
    Write-Host "  [启动] Mosquitto  :1883  pid=$($p.Id)" -ForegroundColor Green
} else {
    Write-Host "  [跳过] Mosquitto 已在运行 :1883" -ForegroundColor Yellow
}

# 后端
$backend = Start-Process -FilePath $Python -ArgumentList "-X","utf8","-u","backend_server.py" -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "backend.log") -RedirectStandardError (Join-Path $LogDir "backend.err")
$pids["backend"] = $backend.Id
Write-Host "  [启动] backend_server :8000  pid=$($backend.Id)" -ForegroundColor Green

if (-not (Wait-Http "http://127.0.0.1:8000/api/status" 30)) {
    Write-Host "后端启动失败，日志：data\logs\backend.err" -ForegroundColor Red
    Get-Content (Join-Path $LogDir "backend.err") -Tail 15 -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "  [就绪] http://127.0.0.1:8000/api/status" -ForegroundColor Green

# 规则引擎
$rule = Start-Process -FilePath $Python -ArgumentList "-X","utf8","-u","rule_engine.py" -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "rule_engine.log") -RedirectStandardError (Join-Path $LogDir "rule_engine.err")
$pids["rule_engine"] = $rule.Id
Write-Host "  [启动] rule_engine        pid=$($rule.Id)  → 订阅 line1/telemetry" -ForegroundColor Green

# 传感器（正常模式）
if (-not $NoSimulator) {
    $sim = Start-Process -FilePath $Python -ArgumentList "-X","utf8","-u","sensor_simulator.py" -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "simulator.log") -RedirectStandardError (Join-Path $LogDir "simulator.err")
    $pids["sensor_simulator"] = $sim.Id
    Write-Host "  [启动] sensor_simulator   pid=$($sim.Id)  → 1 Hz 发布基线" -ForegroundColor Green
}

# 前端
$env:PATH = "$NodeDir;$env:PATH"
$next = Start-Process -FilePath $nodeExe -ArgumentList "node_modules\next\dist\bin\next", "dev", "-p", "3000" `
    -WorkingDirectory (Join-Path $Root "web") -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "web.log") -RedirectStandardError (Join-Path $LogDir "web.err")
$pids["web"] = $next.Id
Write-Host "  [启动] next dev :3000  pid=$($next.Id)" -ForegroundColor Green

if (-not (Wait-Http "http://localhost:3000/" 90)) {
    Write-Host "前端启动超时，日志：data\logs\web.err" -ForegroundColor Red
} else {
    Write-Host "  [就绪] http://localhost:3000" -ForegroundColor Green
}

$pids | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

Write-Host "`n=== 3. 服务一览 ===" -ForegroundColor Cyan
Write-Host "  Mosquitto        mqtt://127.0.0.1:1883     topic: line1/telemetry, line1/events"
Write-Host "  backend_server   http://127.0.0.1:8000     REST /events /approve /reject /work_orders, WS /ws, SSE /api/stream"
Write-Host "  rule_engine      订阅 telemetry → events.jsonl + POST /events"
Write-Host "  sensor_simulator 正常模式 1 Hz"
Write-Host "  看板             http://localhost:3000"
Write-Host "  日志目录         data\logs\*.log"
Write-Host ""
Write-Host "  现场触发：python sensor_simulator.py --inject ch4_rise   （或 .\demo_trigger.ps1）" -ForegroundColor Yellow
Write-Host "  停止全部：powershell -ExecutionPolicy Bypass -File .\stop_demo.ps1" -ForegroundColor Yellow
Write-Host ""
exit 0
