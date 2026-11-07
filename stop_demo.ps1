<#
    停止 start_demo.ps1 / START.cmd 启动的全部进程（Mosquitto 服务除外）。
    先按 PID 文件杀，再按命令行兜底，避免 PID 文件丢失时残留进程。
#>
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "data\demo.pids.json"

if (Test-Path $PidFile) {
    $pids = Get-Content $PidFile -Raw | ConvertFrom-Json
    foreach ($name in $pids.PSObject.Properties.Name) {
        $id = $pids.$name
        try {
            Stop-Process -Id $id -Force -ErrorAction Stop
            Write-Host ("  [停止] {0,-18} pid={1}" -f $name, $id) -ForegroundColor Green
        } catch {
            Write-Host ("  [跳过] {0,-18} pid={1} 已退出" -f $name, $id) -ForegroundColor DarkGray
        }
    }
    Remove-Item $PidFile -Force
} else {
    Write-Host "没有 PID 文件，按命令行兜底清理。" -ForegroundColor Yellow
}

# 兜底：按命令行匹配（PID 文件丢失或进程重启过）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "backend_server|rule_engine|sensor_simulator|modbus_bridge" } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ("  [停止] python pid={0}" -f $_.ProcessId) -ForegroundColor Green } catch { }
    }

Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "next" } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ("  [停止] node pid={0}" -f $_.ProcessId) -ForegroundColor Green } catch { }
    }

Write-Host "完成。Mosquitto 若是 Windows 服务仍在运行：Stop-Service mosquitto（需管理员）。" -ForegroundColor Cyan
