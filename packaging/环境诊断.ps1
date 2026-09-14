# VideoForge · 环境诊断工具
# 用途：在任意 Windows 电脑上检测运行 VideoForge 所需的运行环境，并给出修复建议
# 特点：零依赖（仅用 Windows 自带 PowerShell），可离线运行，不修改系统任何设置

$ErrorActionPreference = 'SilentlyContinue'
$OutputEncoding = [System.Text.Encoding]::UTF8

# 若在 windowed/无控制台环境，重定向输出到文件
$logPath = Join-Path $env:USERPROFILE '.videoforge\环境诊断报告.txt'
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null

$results = New-Object System.Collections.ArrayList

function Add-Check {
    param([string]$Name, [string]$Status, [string]$Detail, [string]$Advice = '')
    [void]$results.Add([PSCustomObject]@{ 项目 = $Name; 状态 = $Status; 说明 = $Detail; 建议 = $Advice })
}

function Write-Out {
    param([string]$Text, [ConsoleColor]$Color = 'Gray')
    Write-Host $Text -ForegroundColor $Color
}

Write-Out "============================================" Cyan
Write-Out "  VideoForge · 环境诊断工具" Cyan
Write-Out "============================================" Cyan
Write-Out ""

# ──────────── 1. Windows 版本 ────────────
Write-Out "[1/8] 检查 Windows 版本..." Gray
$os = Get-CimInstance Win32_OperatingSystem
$build = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuildNumber
$caption = $os.Caption
$minBuild = 17763   # Windows 10 1809
if ($build -ge $minBuild) {
    Add-Check 'Windows 版本' 'OK' "$caption (build $build)" ''
    Write-Out "      OK   $caption (build $build)" Green
} else {
    Add-Check 'Windows 版本' '不通过' "$caption (build $build)" "需要 Windows 10 1809（build $minBuild）或更高版本"
    Write-Out "      警告 $caption (build $build) —— 版本偏低" Yellow
}

# ──────────── 2. WebView2 Runtime（桌面窗口必需）────────────
Write-Out "[2/8] 检查 WebView2 运行库..." Gray
$wv2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$wv2Ver = $null
foreach ($p in @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$wv2Guid",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wv2Guid",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wv2Guid")) {
    $v = (Get-ItemProperty -Path $p -Name pv -ErrorAction SilentlyContinue).pv
    if ($v -and $v -ne '0.0.0.0') { $wv2Ver = $v; break }
}
if ($wv2Ver) {
    Add-Check 'WebView2 运行库' 'OK' "已安装（版本 $wv2Ver）" ''
    Write-Out "      OK   WebView2 已安装（版本 $wv2Ver）" Green
} else {
    Add-Check 'WebView2 运行库' '缺失' '未检测到' '程序会自动改用浏览器模式；如需桌面窗口，安装：https://developer.microsoft.com/microsoft-edge/webview2/'
    Write-Out "      缺失 WebView2 —— 程序将使用浏览器模式（功能一致）" Yellow
    Write-Out "           安装地址: https://developer.microsoft.com/microsoft-edge/webview2/" Gray
}

# ──────────── 3. .NET Framework ────────────
Write-Out "[3/8] 检查 .NET Framework..." Gray
$rel = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -Name Release -ErrorAction SilentlyContinue).Release
if ($rel -ge 528040)      { $netTxt = "4.8 或更高（Release=$rel）"; $netOk = $true }
elseif ($rel -ge 461808)  { $netTxt = "4.7.2（Release=$rel）"; $netOk = $true }
elseif ($rel -gt 0)       { $netTxt = "版本偏低（Release=$rel）"; $netOk = $false }
else                      { $netTxt = "未检测到"; $netOk = $false }
if ($netOk) {
    Add-Check '.NET Framework' 'OK' $netTxt ''
    Write-Out "      OK   .NET Framework $netTxt" Green
} else {
    Add-Check '.NET Framework' '偏低' $netTxt '桌面窗口可能不可用，可改用浏览器模式；建议安装 .NET Framework 4.8'
    Write-Out "      警告 .NET Framework $netTxt" Yellow
}

# ──────────── 4. VC++ 运行库（程序已内置，此处仅作信息）────────────
Write-Out "[4/8] 检查 VC++ 运行库..." Gray
$vc = $false
foreach ($p in @('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64')) {
    if ((Get-ItemProperty -Path $p -ErrorAction SilentlyContinue).Installed -eq 1) { $vc = $true; break }
}
if ($vc) {
    Add-Check 'VC++ 运行库' 'OK' '已安装' ''
    Write-Out "      OK   VC++ 2015-2022 x64 已安装" Green
} else {
    Add-Check 'VC++ 运行库' '未检测到' '系统未安装（程序已内置所需 DLL，通常不影响）' '若程序报 DLL 缺失，安装：https://aka.ms/vs/17/release/vc_redist.x64.exe'
    Write-Out "      提示 未检测到系统级 VC++（程序已内置所需 DLL）" Gray
}

# ──────────── 5. 数据目录可写性 ────────────
Write-Out "[5/8] 检查数据目录可写性..." Gray
$dataDir = Join-Path $env:LOCALAPPDATA 'VideoForge\data'
try {
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $probe = Join-Path $dataDir '.write-test'
    'ok' | Out-File -FilePath $probe -Encoding utf8
    Remove-Item $probe -Force
    $free = [Math]::Round((Get-PSDrive ($dataDir.Substring(0,1))).Free / 1GB, 1)
    Add-Check '数据目录' 'OK' "$dataDir（可用空间 ${free} GB）" ''
    Write-Out "      OK   $dataDir（可用空间 ${free} GB）" Green
} catch {
    Add-Check '数据目录' '不通过' "$dataDir 不可写：$($_.Exception.Message)" '检查磁盘空间/权限，或设置环境变量 VIDEOFORGE_DATA_DIR 指向可写目录'
    Write-Out "      失败 数据目录不可写: $dataDir" Red
}

# ──────────── 6. 程序文件完整性 ────────────
Write-Out "[6/8] 检查程序文件完整性..." Gray
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$exeCandidates = @(
    (Join-Path $here 'VideoForge.exe'),
    (Join-Path $here 'app\VideoForge.exe'),
    (Join-Path (Split-Path $here -Parent) 'VideoForge.exe')
)
$exe = $exeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($exe) {
    $exeDir = Split-Path -Parent $exe
    $hasInternal = Test-Path (Join-Path $exeDir '_internal')
    $sizeMB = [Math]::Round((Get-Item $exe).Length / 1MB, 1)
    if ($hasInternal) {
        Add-Check '程序文件' 'OK' "$exe（$sizeMB MB，_internal 完整）" ''
        Write-Out "      OK   $exe（$sizeMB MB）" Green
    } else {
        Add-Check '程序文件' '不完整' "$exe 缺少 _internal 目录" '请重新解压/安装完整程序包'
        Write-Out "      警告 缺少 _internal 目录（不完整）" Yellow
    }
} else {
    Add-Check '程序文件' '未找到' '当前目录及 app\ 下都没有 VideoForge.exe' '请把本工具放到程序目录下运行'
    Write-Out "      提示 未在当前目录找到 VideoForge.exe" Gray
}

# ──────────── 7. 进程与端口 ────────────
Write-Out "[7/8] 检查运行状态..." Gray
$running = Get-Process -Name 'VideoForge' -ErrorAction SilentlyContinue
if ($running) {
    Add-Check '运行状态' '运行中' "PID $($running.Id -join ',')" ''
    Write-Out "      运行中 PID $($running.Id -join ',')" Green
} else {
    Add-Check '运行状态' '未运行' '当前没有 VideoForge 进程' ''
    Write-Out "      未运行" Gray
}

# ──────────── 8. 安全软件（常见拦截源）────────────
Write-Out "[8/8] 检查安全软件..." Gray
$av = Get-CimInstance -Namespace 'root\SecurityCenter2' -ClassName AntiVirusProduct -ErrorAction SilentlyContinue
if ($av) {
    $names = ($av | ForEach-Object { $_.displayName }) -join '、'
    Add-Check '安全软件' '信息' $names '若程序被拦截/删除，请将安装目录加入白名单'
    Write-Out "      检测到: $names" Gray
} else {
    Add-Check '安全软件' '信息' '未检测到（或权限不足）' ''
    Write-Out "      未检测到安全软件信息" Gray
}

# ──────────── 报告汇总 ────────────
Write-Out ""
Write-Out "============================================" Cyan
Write-Out "  诊断结论" Cyan
Write-Out "============================================" Cyan
$results | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

$bad = $results | Where-Object { $_.状态 -in @('不通过', '缺失', '不完整') }
if ($bad.Count -eq 0) {
    Write-Out "结论：环境满足要求，可以正常使用 VideoForge。" Green
} else {
    Write-Out "结论：存在 $($bad.Count) 项需要关注（多数情况下程序仍可运行，会自动降级）。" Yellow
    foreach ($b in $bad) { Write-Out "  · $($b.项目)：$($b.说明)  → $($b.建议)" Yellow }
}

# 保存报告
$results | Format-Table -AutoSize | Out-String -Width 200 | Out-File -FilePath $logPath -Encoding utf8
Write-Out ""
Write-Out "报告已保存：$logPath" Gray
Write-Out ""
Write-Out "按任意键退出..." Gray
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
