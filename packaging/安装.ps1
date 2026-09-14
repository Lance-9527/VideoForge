# VideoForge · 安装程序
# 特点：零依赖（仅用 Windows 自带 PowerShell）、免管理员权限（装到用户目录）、
#       自动创建快捷方式、注册到"应用和功能"、安装后自动环境自检
# 静默安装（批量部署/自动化）：
#   powershell -ExecutionPolicy Bypass -File 安装.ps1 -Silent -InstallDir "D:\Apps\VideoForge" -SkipLaunch

param(
    [switch]$Silent,                    # 静默模式：使用默认值，无交互
    [string]$InstallDir = '',           # 指定安装目录
    [switch]$SkipLaunch,                # 安装后不启动
    [switch]$Force                      # 已安装时直接覆盖
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8

$APP_NAME    = 'VideoForge'
$APP_VERSION = '1.1.0'
$PUBLISHER   = 'VideoForge'
$HERE        = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Out { param([string]$Text, [ConsoleColor]$Color = 'Gray'); Write-Host $Text -ForegroundColor $Color }
function End-IfInteractive {
    if (-not $Silent) {
        Write-Out ""
        Write-Out "按任意键退出..." Gray
        $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    }
}

Write-Out ""
Write-Out "============================================" Cyan
Write-Out "   $APP_NAME 安装程序  v$APP_VERSION" Cyan
Write-Out "============================================" Cyan
Write-Out ""

# ──────────── 1. 定位程序源文件 ────────────
$sourceDir = Join-Path $HERE 'app'
if (-not (Test-Path (Join-Path $sourceDir 'VideoForge.exe'))) {
    if (Test-Path (Join-Path $HERE 'VideoForge.exe')) {
        $sourceDir = $HERE
    } else {
        Write-Out "[错误] 未找到程序文件 VideoForge.exe" Red
        Write-Out "       请确认 app\ 子目录存在且包含 VideoForge.exe 与 _internal\" Yellow
        End-IfInteractive
        exit 1
    }
}
$exePath = Join-Path $sourceDir 'VideoForge.exe'
$sizeMB = [Math]::Round((Get-ChildItem -Path $sourceDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Out "  程序源目录 : $sourceDir"
Write-Out "  程序体积   : $sizeMB MB"
Write-Out ""

# ──────────── 2. 环境预检 ────────────
Write-Out "[1/5] 环境预检..." Gray
$build = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuildNumber
if ($build -lt 17763) {
    Write-Out "      [警告] 系统版本偏低（build $build），需要 Windows 10 1809 或更高" Yellow
} else {
    Write-Out "      OK   Windows build $build" Green
}
$wv2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$wv2 = $null
foreach ($p in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$wv2Guid",
                 "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wv2Guid",
                 "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wv2Guid")) {
    $v = (Get-ItemProperty -Path $p -Name pv -ErrorAction SilentlyContinue).pv
    if ($v -and $v -ne '0.0.0.0') { $wv2 = $v; break }
}
if ($wv2) {
    Write-Out "      OK   WebView2 已安装（$wv2）—— 将使用桌面窗口" Green
} else {
    Write-Out "      [提示] 未检测到 WebView2 —— 程序会自动用浏览器模式打开（功能一致）" Yellow
    Write-Out "             如需桌面窗口体验，稍后可安装：https://developer.microsoft.com/microsoft-edge/webview2/" Gray
}
Write-Out ""

# ──────────── 3. 选择安装目录 ────────────
$defaultDir = Join-Path $env:LOCALAPPDATA "Programs\$APP_NAME"
Write-Out "[2/5] 安装位置" Gray
if ($InstallDir) {
    $installDir = $InstallDir.Trim('"')
    Write-Out "      指定位置: $installDir"
} elseif ($Silent) {
    $installDir = $defaultDir
    Write-Out "      默认位置（静默）: $installDir"
} else {
    Write-Out "      默认: $defaultDir"
    $inputPath = Read-Host "      直接回车使用默认位置，或输入其他路径"
    $installDir = if ([string]::IsNullOrWhiteSpace($inputPath)) { $defaultDir } else { $inputPath.Trim('"') }
    Write-Out "      安装到: $installDir"
}
Write-Out ""

# 已安装检测
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$APP_NAME"
if ((Test-Path $regPath) -and -not ($Force -or $Silent)) {
    $old = (Get-ItemProperty -Path $regPath -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
    Write-Out "      [提示] 检测到已安装版本：$old" Yellow
    $ans = Read-Host "      是否覆盖安装？(Y/N)"
    if ($ans -notmatch '^[Yy]') { Write-Out "      已取消。" Gray; exit 0 }
} elseif (Test-Path $regPath) {
    Write-Out "      [提示] 检测到已安装版本，将直接覆盖（静默模式）" Yellow
}

# ──────────── 4. 复制文件 ────────────
Write-Out "[3/5] 复制程序文件（约 $sizeMB MB，请稍候）..." Gray
if (Test-Path $installDir) {
    # 保留用户数据（数据在 %LOCALAPPDATA%\VideoForge\data，与安装目录分离）
    Remove-Item -Path (Join-Path $installDir '_internal') -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$robo = Start-Process robocopy -ArgumentList "`"$sourceDir`" `"$installDir`" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1" -Wait -PassThru -NoNewWindow
if ($robo.ExitCode -ge 8) {
    Write-Out "      [错误] 文件复制失败（robocopy 退出码 $($robo.ExitCode)）" Red
    Write-Out "             请检查目标目录权限或磁盘空间" Yellow
    Write-Out ""
    Write-Out "按任意键退出..." Gray
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    exit 1
}
# 复制诊断工具，便于排查
foreach ($f in @('环境诊断.ps1', '环境诊断.bat')) {
    $src = Join-Path $HERE $f
    if (Test-Path $src) { Copy-Item $src -Destination $installDir -Force }
}
Write-Out "      OK   已复制到 $installDir" Green
Write-Out ""

# ──────────── 5. 创建快捷方式 ────────────
Write-Out "[4/5] 创建快捷方式..." Gray
$target = Join-Path $installDir 'VideoForge.exe'
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param([string]$Path, [string]$TargetPath, [string]$WorkDir, [string]$Desc)
    $sc = $shell.CreateShortcut($Path)
    $sc.TargetPath = $TargetPath
    $sc.WorkingDirectory = $WorkDir
    $sc.Description = $Desc
    $sc.IconLocation = $TargetPath
    $sc.Save()
}

$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
try {
    New-Shortcut -Path (Join-Path $desktop "$APP_NAME.lnk") -TargetPath $target -WorkDir $installDir -Desc "$APP_NAME · 私人 AI 短片 Agent"
    Write-Out "      OK   桌面快捷方式" Green
} catch { Write-Out "      [提示] 桌面快捷方式创建失败：$($_.Exception.Message)" Yellow }
try {
    New-Shortcut -Path (Join-Path $startMenu "$APP_NAME.lnk") -TargetPath $target -WorkDir $installDir -Desc "$APP_NAME · 私人 AI 短片 Agent"
    Write-Out "      OK   开始菜单快捷方式" Green
} catch { Write-Out "      [提示] 开始菜单快捷方式创建失败：$($_.Exception.Message)" Yellow }

# 环境诊断快捷方式（开始菜单）
try {
    $diag = Join-Path $installDir '环境诊断.bat'
    if (Test-Path $diag) {
        New-Shortcut -Path (Join-Path $startMenu "$APP_NAME 环境诊断.lnk") -TargetPath $diag -WorkDir $installDir -Desc "$APP_NAME 环境诊断工具"
    }
} catch { }
Write-Out ""

# ──────────── 6. 注册卸载信息 ────────────
Write-Out "[5/5] 注册到系统..." Gray
try {
    New-Item -Path $regPath -Force | Out-Null
    $uninstallCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$installDir\卸载.ps1`""
    $estSize = [int]((Get-ChildItem -Path $installDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1KB)
    Set-ItemProperty -Path $regPath -Name DisplayName      -Value "$APP_NAME · 私人 AI 短片 Agent"
    Set-ItemProperty -Path $regPath -Name DisplayVersion   -Value $APP_VERSION
    Set-ItemProperty -Path $regPath -Name Publisher        -Value $PUBLISHER
    Set-ItemProperty -Path $regPath -Name InstallLocation  -Value $installDir
    Set-ItemProperty -Path $regPath -Name DisplayIcon      -Value $target
    Set-ItemProperty -Path $regPath -Name UninstallString  -Value $uninstallCmd
    Set-ItemProperty -Path $regPath -Name EstimatedSize    -Value $estSize -Type DWord
    Set-ItemProperty -Path $regPath -Name NoModify         -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name NoRepair         -Value 1 -Type DWord
    Write-Out "      OK   已注册（可在「设置 → 应用 → 已安装的应用」中卸载）" Green
} catch {
    Write-Out "      [提示] 注册卸载信息失败：$($_.Exception.Message)" Yellow
}

# 复制卸载脚本到安装目录（供注册表调用）
$un = Join-Path $HERE '卸载.ps1'
if (Test-Path $un) { Copy-Item $un -Destination $installDir -Force }

Write-Out ""
Write-Out "============================================" Green
Write-Out "   安装完成！" Green
Write-Out "============================================" Green
Write-Out "  安装目录 : $installDir"
Write-Out "  数据目录 : $env:LOCALAPPDATA\VideoForge\data（卸载不会删除，除非你选择删除）"
Write-Out "  日志目录 : $env:USERPROFILE\.videoforge"
Write-Out ""
if ($SkipLaunch -or $Silent) {
    Write-Out "  可从桌面或开始菜单启动。" Gray
} else {
    $ans = Read-Host "  现在启动 VideoForge？(Y/N)"
    if ($ans -match '^[Yy]') {
        Start-Process -FilePath $target -WorkingDirectory $installDir
        Write-Out "  已启动。首次启动约需 5-15 秒。" Gray
    } else {
        Write-Out "  可从桌面或开始菜单启动。" Gray
    }
}
End-IfInteractive
