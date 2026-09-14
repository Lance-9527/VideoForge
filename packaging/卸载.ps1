# VideoForge · 卸载程序
# 由「设置 → 应用」或安装目录下的卸载入口调用

param(
    [switch]$Silent,      # 静默卸载：不询问，保留用户数据
    [switch]$PurgeData    # 同时删除用户数据
)

$ErrorActionPreference = 'Continue'
$OutputEncoding = [System.Text.Encoding]::UTF8

$APP_NAME = 'VideoForge'
$regPath  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$APP_NAME"

function Write-Out { param([string]$Text, [ConsoleColor]$Color = 'Gray'); Write-Host $Text -ForegroundColor $Color }

Write-Out ""
Write-Out "============================================" Cyan
Write-Out "   $APP_NAME 卸载程序" Cyan
Write-Out "============================================" Cyan
Write-Out ""

$installDir = (Get-ItemProperty -Path $regPath -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
if (-not $installDir) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $installDir = $here
}
Write-Out "  安装目录 : $installDir"
Write-Out ""

if (-not $Silent) {
    $ans = Read-Host "  确认卸载 $APP_NAME ？(Y/N)"
    if ($ans -notmatch '^[Yy]') { Write-Out "  已取消。" Gray; exit 0 }
}

# 1. 结束正在运行的进程
$proc = Get-Process -Name $APP_NAME -ErrorAction SilentlyContinue
if ($proc) {
    Write-Out "  正在关闭运行中的程序..." Gray
    $proc | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# 2. 删除快捷方式
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
foreach ($lnk in @(
        (Join-Path $desktop "$APP_NAME.lnk"),
        (Join-Path $startMenu "$APP_NAME.lnk"),
        (Join-Path $startMenu "$APP_NAME 环境诊断.lnk"))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force -ErrorAction SilentlyContinue }
}
Write-Out "  OK   已删除快捷方式" Green

# 3. 删除注册表项
Remove-Item -Path $regPath -Recurse -Force -ErrorAction SilentlyContinue
Write-Out "  OK   已移除系统注册信息" Green

# 4. 删除程序文件
if (Test-Path $installDir) {
    try {
        Remove-Item -Path $installDir -Recurse -Force -ErrorAction Stop
        Write-Out "  OK   已删除程序文件" Green
    } catch {
        Write-Out "  [提示] 部分文件删除失败（可能被占用）：$($_.Exception.Message)" Yellow
        Write-Out "         可稍后手动删除：$installDir" Yellow
    }
}

# 5. 用户数据（默认保留）
$dataDir = Join-Path $env:LOCALAPPDATA "$APP_NAME\data"
if (Test-Path $dataDir) {
    Write-Out ""
    Write-Out "  用户数据目录：$dataDir" Gray
    Write-Out "  其中包含：项目、剧本、会话、生成的视频" Gray
    if ($PurgeData -or $Silent) { $d = if ($PurgeData) { 'Y' } else { 'N' } }
    else { $d = Read-Host "  是否一并删除用户数据？(Y/N，默认保留)" }
    if ($d -match '^[Yy]') {
        Remove-Item -Path $dataDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Out "  OK   已删除用户数据" Green
    } else {
        Write-Out "  已保留用户数据（重装后可继续使用）" Gray
    }
}

Write-Out ""
Write-Out "============================================" Green
Write-Out "   卸载完成" Green
Write-Out "============================================" Green
if (-not $Silent) {
    Write-Out ""
    Write-Out "按任意键退出..." Gray
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
}
