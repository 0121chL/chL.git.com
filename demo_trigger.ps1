<#
    现场演示触发（PowerShell 版，等价于 demo_trigger.sh）。
        powershell -ExecutionPolicy Bypass -File .\demo_trigger.ps1
#>
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "[demo] 注入 ch4_rise（约 10 秒，ch4 0.30% -> 1.30%）..." -ForegroundColor Cyan
& python sensor_simulator.py --inject ch4_rise
Write-Host ""
Write-Host "浏览器 http://localhost:3000 应依次出现：" -ForegroundColor Green
Write-Host "  1) 左侧 R-CH4-RISE / 高危 事件卡片"
Write-Host "  2) 右侧 Agent 分析卡片（根因 / 规程依据 / 建议动作）"
Write-Host "  3) 点击「批准」后底部工单表新增一行"
Write-Host "未出现时先看 data\logs\backend.err 与 data\logs\rule_engine.log" -ForegroundColor Yellow
