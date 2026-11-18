<#
.SYNOPSIS
    编译 / 烧录 ESP32-S3 现场网关固件。

.DESCRIPTION
    为什么要专门写这个脚本：
    这台机器的 Windows 用户名是中文（机械革命），而 ESP32 工具链里的 MinGW 链接器
    打不开「含中文的 -L 库路径」，于是链接阶段会报一堆
        ld.exe: cannot find -lbootloader_support: No such file or directory
    编译（gcc）能过，链接（ld）必挂，Arduino IDE 里点「验证/上传」也是同样的错。

    解决办法：把「核心 + 工具链 + 库」复制到纯 ASCII 路径（默认 C:\esptoolchain），
    用 --config-file 指向它。本脚本就是这件事的自动化，首次运行会自动搭建。

.EXAMPLE
    .\build.ps1                 # 只编译
    .\build.ps1 -Setup          # 首次：从 Arduino IDE 复制一套 ASCII 工具链（约 1.7 GB）
    .\build.ps1 -Upload -Port COM5
    .\build.ps1 -Upload         # 自动找串口
#>
[CmdletBinding()]
param(
    # 首次/修复：从 Arduino IDE 的数据目录重建 ASCII 工具链
    [switch]$Setup,
    # 编译后烧录
    [switch]$Upload,
    # 串口号，例如 COM5；不填则自动探测
    [string]$Port = "",
    # ASCII 工具链根目录（不要带中文）
    [string]$Toolchain = "C:\esptoolchain",
    # 目标板型
    [string]$Fqbn = "esp32:esp32:esp32s3"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Sketch = Join-Path $Here "esp32_s3_gateway"
$Ino = Join-Path $Sketch "esp32_s3_gateway.ino"
$ConfigH = Join-Path $Sketch "config.h"
$CfgFile = Join-Path $Toolchain "arduino-cli.yaml"
$BuildDir = Join-Path $Toolchain "build"
$IdeData = Join-Path $env:LOCALAPPDATA "Arduino15"
$IdeUser = Join-Path $env:USERPROFILE "Documents\Arduino"
$RequiredLibs = @("PubSubClient", "OneWire", "DallasTemperature")

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Write-Warn2($text) { Write-Host "  [!] $text" -ForegroundColor Yellow }
function Write-Ok($text) { Write-Host "  [OK] $text" -ForegroundColor Green }

function Test-AsciiPath([string]$path) {
    return ($path -notmatch '[^\x00-\x7F]')
}

function Resolve-ArduinoCli {
    $candidates = @(
        "E:\Arduino\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"),
        (Join-Path $env:ProgramFiles "Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command arduino-cli -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "找不到 arduino-cli（请确认 Arduino IDE 已安装，或把 arduino-cli 放进 PATH）"
}

function Get-FirstChild($path) {
    if (-not (Test-Path $path)) { return $null }
    $child = Get-ChildItem $path -Directory | Sort-Object Name -Descending | Select-Object -First 1
    if ($child) { return $child.Name }
    return $null
}

function Invoke-Setup {
    param($Toolchain, $IdeData, $IdeUser)

    if (-not (Test-Path $IdeData)) { throw "找不到 Arduino 数据目录：$IdeData" }
    $espPkg = Join-Path $IdeData "packages\esp32"
    if (-not (Test-Path $espPkg)) {
        throw "Arduino 里还没装 ESP32 核心。请先打开 Arduino IDE → 开发板管理器 → 搜索 esp32 安装。"
    }

    $coreVer = Get-FirstChild (Join-Path $espPkg "hardware\esp32")
    if (-not $coreVer) { throw "找不到 esp32 核心版本目录" }
    $needTools = @(
        @{ Name = "esp-x32"; Ver = (Get-FirstChild (Join-Path $espPkg "tools\esp-x32")) },
        @{ Name = "esp32s3-libs"; Ver = (Get-FirstChild (Join-Path $espPkg "tools\esp32s3-libs")) },
        @{ Name = "esptool_py"; Ver = (Get-FirstChild (Join-Path $espPkg "tools\esptool_py")) }
    )
    foreach ($t in $needTools) {
        if (-not $t.Ver) { throw "找不到工具 $($t.Name)（esp32 核心没装全？）" }
    }

    Write-Host "  核心版本: $coreVer"
    foreach ($t in $needTools) { Write-Host "  $($t.Name): $($t.Ver)" }

    foreach ($d in @("data\packages\esp32\hardware\esp32", "data\packages\esp32\tools", "data\staging", "user\libraries", "build")) {
        New-Item -ItemType Directory -Path (Join-Path $Toolchain $d) -Force | Out-Null
    }

    Write-Host "  复制核心（约 69 MB）..."
    Copy-Item (Join-Path $espPkg "hardware\esp32\$coreVer") (Join-Path $Toolchain "data\packages\esp32\hardware\esp32\") -Recurse -Force
    foreach ($t in $needTools) {
        Write-Host "  复制 $($t.Name) ..."
        Copy-Item (Join-Path $espPkg "tools\$($t.Name)\$($t.Ver)") (Join-Path $Toolchain "data\packages\esp32\tools\$($t.Name)\") -Recurse -Force
    }

    # 索引：复制一份，避免首次编译又去联网下载（慢且可能失败）
    foreach ($f in @("package_index.json", "package_index.json.sig", "library_index.json", "library_index.json.sig", "package_esp32_index_cn.json")) {
        $src = Join-Path $IdeData $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $Toolchain "data") -Force }
    }

    # 依赖库
    if (Test-Path (Join-Path $IdeUser "libraries")) {
        foreach ($lib in $RequiredLibs) {
            $src = Join-Path $IdeUser "libraries\$lib"
            if (-not (Test-Path $src)) { Write-Warn2 "IDE 里没装库 $lib，稍后用 arduino-cli lib install 补"; continue }
            Copy-Item $src (Join-Path $Toolchain "user\libraries\") -Recurse -Force
        }
    }

    $yaml = @"
directories:
  data: $($Toolchain -replace '\\','/')/data
  user: $($Toolchain -replace '\\','/')/user
  downloads: $($Toolchain -replace '\\','/')/data/staging
"@
    [System.IO.File]::WriteAllText($CfgFile, $yaml, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "ASCII 工具链已就绪：$Toolchain"
}

# ---------------------------------------------------------------------------
Write-Step "检查环境"
if (-not (Test-AsciiPath $Toolchain)) {
    throw "工具链路径含非 ASCII 字符（$Toolchain），换一个纯英文路径，例如 C:\esptoolchain"
}
$Cli = Resolve-ArduinoCli
Write-Ok "arduino-cli: $Cli"

if (-not (Test-Path $Ino)) { throw "找不到固件源码：$Ino" }

if ($Setup -or -not (Test-Path $CfgFile)) {
    Write-Step "搭建 ASCII 工具链 → $Toolchain"
    Invoke-Setup -Toolchain $Toolchain -IdeData $IdeData -IdeUser $IdeUser
}

if (-not (Test-Path $ConfigH)) {
    Write-Host ""
    Write-Host "缺少 config.h —— 先执行：" -ForegroundColor Yellow
    Write-Host "    Copy-Item `"$Here\esp32_s3_gateway\config.example.h`" `"$ConfigH`""
    Write-Host "  然后填 WIFI_SSID / WIFI_PASS / MQTT_HOST / DEVICE_ID。" -ForegroundColor Yellow
    exit 2
}

# 中文路径的自检：核心/库都在 ASCII 目录里才可能链接成功
Write-Step "编译 $Fqbn"
$compileArgs = @("--config-file", $CfgFile, "compile", "--fqbn", $Fqbn, "--build-path", $BuildDir, $Sketch)
if ($Upload) {
    if (-not $Port) {
        try {
            $json = & $Cli --config-file $CfgFile board list --format json 2>$null | Out-String
            $detected = ($json | ConvertFrom-Json).detected_ports
            $serial = $detected | Where-Object { $_.port.protocol -eq "serial" } | Select-Object -First 1
            if ($serial) {
                $Port = $serial.port.address
                Write-Ok "自动探测到串口 $Port（$($serial.port.properties.vid):$($serial.port.properties.pid)）"
            }
        } catch { }
        if (-not $Port) { throw "没有探测到串口：插上板子、装好 USB 驱动，或显式传 -Port COM5" }
    }
    $compileArgs += @("--upload", "--port", $Port)
}

$output = & $Cli @compileArgs 2>&1
$code = $LASTEXITCODE
$output | Where-Object { $_ -match "Sketch uses|Global variables|error|Error|Low memory" } | ForEach-Object { "  $_" }

if ($code -ne 0) {
    Write-Host "`n编译失败。" -ForegroundColor Red
    if (($output | Out-String) -match "cannot find -l") {
        Write-Warn2 "看到 cannot find -l 基本就是中文路径问题：先删掉 $Toolchain，再用 -Setup 重建到纯英文路径。"
    }
    exit 1
}

$bin = Get-ChildItem $BuildDir -Filter "esp32_s3_gateway.ino.bin" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($bin) {
    Write-Ok ("固件: {0}  ({1:N0} KB)" -f $bin.FullName, ($bin.Length / 1KB))
}
if ($Upload) {
    Write-Ok "已烧录到 $Port"
} else {
    Write-Host "`n  烧录： .\build.ps1 -Upload -Port COM5" -ForegroundColor Yellow
}
exit 0
