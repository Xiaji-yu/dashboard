# 把 8282 总控台装成 Windows 服务（开机自启、崩溃自动重启）
#
# 用法（**管理员身份**的 PowerShell）：
#   cd deploy
#   .\install-windows.ps1              # 安装并启动
#   .\install-windows.ps1 -Remove      # 卸载
#
# 说明与取舍：
# - 用系统自带的 sc.exe，不需要 NSSM 之类的第三方依赖；
# - 默认以 **LocalSystem** 运行（等价 Linux 的 root）：能读到全部进程与端口归属。
#   想降权就跑 .\install-windows.ps1 -User ".\某个普通用户"（会要求输入该用户的密码，
#   并需要「作为服务登录」权限：secpol.msc → 本地策略 → 用户权限分配）；
# - 崩溃后 60 秒内三次重启都失败会重置计数，不会无限重启；
# - 凭据文件 auth.json 就在项目目录（600 权限是 Linux 的概念，Windows 上请用 NTFS 权限控制）。

param(
    [switch]$Remove,
    [string]$User = "",
    [string]$Name = "dashboard",
    [string]$DisplayName = "8282 总控台（本机实时监控看板）",
    # 打包成 exe 时传它的路径，例如 .\install-windows.ps1 -ExePath "D:\dist\dashboard.exe"
    # 这样目标机器不需要装 Python；数据文件落在 exe 同目录（或 %LOCALAPPDATA%\dashboard）
    [string]$ExePath = ""
)

$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $PSScriptRoot
if ($ExePath) {
    if (-not (Test-Path $ExePath)) { throw "找不到 exe：$ExePath" }
    # 直接注册打包好的 exe：目标机器不需要 Python
    $BinPath = "`"$ExePath`""
} else {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $Python) {
        throw "找不到 python 命令；先装 Python 3.9+（安装时勾选 Add python.exe to PATH），或用 -ExePath 指向打包好的 exe"
    }
    python -c "import psutil" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "缺少依赖 psutil，请先执行：  python -m pip install psutil"
    }
    $BinPath = "`"$Python`" `"$Dir\server.py`""
}

if ($Remove) {
    sc.exe stop $Name | Out-Null
    Start-Sleep -Seconds 2
    sc.exe delete $Name | Out-Host
    Write-Host "已卸载服务 $Name"
    return
}

# 首次启动会生成随机初始密码并写进日志：用 sc.exe 启动的服务日志在「事件查看器」里，
# 更稳妥的做法是设好环境变量再装，或在装好后手动跑一次 .\run.ps1 start 看日志。
$env:DASHBOARD_HOST = "0.0.0.0"
$env:DASHBOARD_PORT = "8282"

$created = sc.exe create $Name binPath= "$BinPath" start= auto DisplayName= "$DisplayName"
$created | Out-Host
if ($User) {
    # 不带密码时 sc 会交互式询问；也可以改用 gMSA（域环境）
    sc.exe config $Name obj= "$User" password= | Out-Host
} else {
    sc.exe config $Name obj= LocalSystem | Out-Host
}

# 失败重启：失败后 60 秒重启，连续 3 次；86400 秒（1 天）后重置计数
sc.exe failure $Name reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Host
sc.exe description $Name "8282 总控台：本机实时监控看板（Python + psutil，账号鉴权）" | Out-Host
sc.exe start $Name | Out-Host

Write-Host ""
Write-Host "已安装并启动。常用命令："
Write-Host "  sc.exe query $Name            查看状态"
Write-Host "  sc.exe stop  $Name            停止"
Write-Host "  sc.exe delete $Name           卸载"
Write-Host "  事件查看器 → Windows 日志 → 应用程序：首次启动的随机初始账号密码就在这里"
Write-Host "  （或者先 .\run.ps1 fg 手动跑一次，把初始密码记下来再删掉 auth.json 重装）"
Write-Host ""
Write-Host "浏览器打开 http://127.0.0.1:8282/ ，用日志里的初始账号登录，然后立刻改密码。"
