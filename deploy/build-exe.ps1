# 把 8282 总控台打包成 Windows exe（PyInstaller）
#
# 用法（普通 PowerShell，不需要管理员）：
#   cd deploy
#   .\build-exe.ps1                # 单文件 exe（先试这个）
#   .\build-exe.ps1 -Onedir        # 目录版：启动更快、杀软误报更少
#   .\build-exe.ps1 -NoConsole     # 无控制台窗口（初始账号走文件 + 弹窗提示）
#   .\build-exe.ps1 -KeepConsole   # 明确保留控制台（默认）
#
# 产物：
#   onefile: dist\dashboard.exe
#   onedir : dist\dashboard\dashboard.exe
#
# 说明：
# - static/ 会以 --add-data 打进包里；auth.json / probes.json 运行时落在 **exe 同目录**
#   （不可写时自动退回 %LOCALAPPDATA%\dashboard），不会写进包内也不怕升级覆盖；
# - 目标机器**不需要**装 Python 或 psutil；但「设备页」仍会用系统自带的
#   PowerShell / netsh / arp，容器面板需要 Docker Desktop 在 PATH 里；
# - 单文件版每次启动会把自己解包到临时目录，首次启动慢 1~2 秒；目录版没有这个过程；
# - PyInstaller 打的包常被杀软误报，介意的话用目录版或做代码签名。

param(
    [switch]$Onedir,
    [switch]$NoConsole,
    [switch]$KeepConsole,
    [string]$Name = "dashboard"
)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectDir = Split-Path -Parent $DeployDir
Set-Location $ProjectDir

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "找不到 python 命令；先装 Python 3.9+（安装时勾选 Add python.exe to PATH）"
}

# PyInstaller 属于构建期依赖，不装进项目
$hasPyInstaller = $true
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    $hasPyInstaller = $false
}
if (-not $hasPyInstaller) {
    Write-Host "未检测到 PyInstaller，正在安装（只装到当前 Python 环境）..."
    python -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败，请检查网络后重试" }
}

$mode = if ($Onedir) { "--onedir" } else { "--onefile" }
# 默认保留控制台：源码运行时的初始账号密码是打印在控制台的
$window = if ($NoConsole -and -not $KeepConsole) { "--noconsole" } else { "--console" }

Write-Host "开始打包（$mode $window）..."

# 只排除确定用不到的库（http.server 等标准库正在用，不能排）
$buildArgs = @(
    "--noconfirm", "--clean", "--name", $Name,
    $mode, $window,
    "--add-data", "static;static",
    "--hidden-import", "psutil",
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    "--exclude-module", "pydoc",
    "--exclude-module", "test",
    "--exclude-module", "distutils",
    "server.py"
)
python -m PyInstaller @buildArgs
if ($LASTEXITCODE -ne 0) { throw "打包失败，请看上面的 PyInstaller 输出" }

# 产物路径
if ($Onedir) {
    $exe = Join-Path $ProjectDir "dist\$Name\$Name.exe"
    $targetDir = Join-Path $ProjectDir "dist\$Name"
} else {
    $exe = Join-Path $ProjectDir "dist\$Name.exe"
    $targetDir = Join-Path $ProjectDir "dist"
}
if (-not (Test-Path $exe)) { throw "没找到产物 $exe" }

# 顺带放一份探测目标样例与使用说明，方便直接分发
$probes = Join-Path $ProjectDir "probes.json"
if (Test-Path $probes) {
    Copy-Item $probes (Join-Path $targetDir "probes.json") -Force
}

@"
8282 总控台（打包版）
====================

1) 双击 $Name.exe（或 .\$(Split-Path -Leaf $exe)），
   首次启动会生成随机账号密码：
   - 若打包时保留了控制台：密码打印在窗口里；
   - 若是无控制台版本：密码写入"初始账号-登录后请删除.txt"并弹窗提示。
2) 浏览器打开 http://127.0.0.1:8282/ 登录，然后立刻在侧栏「账号」里改密码。
3) 数据文件（auth.json / probes.json / 日志）都在 exe 同目录；
   若该目录不可写（例如装在 Program Files），会自动落到 %LOCALAPPDATA%\dashboard。

常用环境变量（可在启动前设置）：
  DASHBOARD_PORT=8282        监听端口
  DASHBOARD_HOST=0.0.0.0     监听地址（想只本机访问就设 127.0.0.1）
  DASHBOARD_DISK=C:\         监控哪个盘
  DASHBOARD_TRUST_PROXY=1    放在反向代理后面时必须设

装成开机自启的服务（管理员 PowerShell）：
  .\install-windows.ps1 -ExePath "$exe"
"@ | Set-Content -Path (Join-Path $targetDir "使用说明.txt") -Encoding UTF8

Write-Host ""
Write-Host "打包完成：$exe"
Get-Item $exe | Select-Object Name, @{n = "大小MB"; e = { [math]::Round($_.Length / 1MB, 1) } } | Format-Table
Write-Host "同目录已生成 使用说明.txt$(if (Test-Path $probes) { ' 与 probes.json 样例' })"
Write-Host ""
Write-Host "下一步：直接运行 exe，或（管理员）装成服务："
Write-Host "  .\install-windows.ps1 -ExePath `"$exe`""
