<#
    停止 start_demo.ps1 启动的全部进程（Mosquitto 服务除外）。
#>
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "data\demo.pids.json"

if (-not (Test-Path $PidFile)) {
    Write-Host "没有找到 $PidFile，可能没通过 start_demo.ps1 启动。" -ForegroundColor Yellow
    exit 0
}

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
Write-Host "完成。Mosquitto 若是 Windows 服务仍在运行，可用 Get-Service mosquitto 查看。" -ForegroundColor Cyan
