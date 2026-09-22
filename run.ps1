# 8282 总控台 —— Windows 启动脚本
#
# 用法（PowerShell）：
#   .\run.ps1 start      启动（后台，日志写 server.log）
#   .\run.ps1 stop       停止
#   .\run.ps1 restart    重启
#   .\run.ps1 status     看状态与接口自检
#   .\run.ps1 log 50     看最后 50 行日志
#   .\run.ps1 fg         前台运行（Ctrl+C 结束，适合先跑起来看看）
#
# 需要 Python 3.9+ 和 psutil：  python -m pip install psutil
# 生产环境建议装成 Windows 服务，见 deploy\install-windows.ps1

param(
    [ValidateSet("start", "stop", "restart", "status", "log", "fg")]
    [string]$Action = "status",
    [int]$Lines = 40
)

$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Dir "server.pid"
$LogFile = Join-Path $Dir "server.log"
$Host_ = if ($env:DASHBOARD_HOST) { $env:DASHBOARD_HOST } else { "0.0.0.0" }
$Port = if ($env:DASHBOARD_PORT) { $env:DASHBOARD_PORT } else { "8282" }

function Get-DashboardPid {
    if (Test-Path $PidFile) {
        $recorded = (Get-Content $PidFile -Raw).Trim()
        if ($recorded -and (Get-Process -Id $recorded -ErrorAction SilentlyContinue)) {
            return [int]$recorded
        }
    }
    # 兜底：只认 Name 为 python* 且命令行以本目录的 server.py 结尾的进程，
    # 免得把「命令行里恰好含这段路径」的终端当成看板（Linux 版踩过这个坑）。
    $matches = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($item in $matches) {
        if ($item.CommandLine -and $item.CommandLine.TrimEnd('"') -like "*$Dir\server.py") {
            return $item.ProcessId
        }
    }
    return $null
}

switch ($Action) {
    "start" {
        $pid = Get-DashboardPid
        if ($pid) { Write-Host "总控台已在运行（PID $pid，端口 $Port）"; return }
        if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
            throw "找不到 python 命令。先装 Python 3.9+，并 pip install psutil"
        }
        $env:DASHBOARD_HOST = $Host_
        $env:DASHBOARD_PORT = $Port
        # 进程脱离当前终端，日志重定向到文件
        Start-Process -FilePath "python" -ArgumentList "`"$Dir\server.py`"" `
            -WorkingDirectory $Dir -WindowStyle Hidden `
            -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err"
        Start-Sleep -Seconds 2
        $pid = Get-DashboardPid
        if ($pid) {
            Set-Content -Path $PidFile -Value $pid
            Write-Host "已启动：http://127.0.0.1:$Port/  （PID $pid，日志 $LogFile）"
        } else {
            Write-Host "启动失败，日志末尾："
            if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 }
        }
    }
    "stop" {
        $pid = Get-DashboardPid
        if ($pid) {
            Stop-Process -Id $pid -Force
            Start-Sleep -Milliseconds 500
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            Write-Host "已停止（PID $pid）"
        } else {
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            Write-Host "没有在运行"
        }
    }
    "restart" {
        & $MyInvocation.MyCommand.Path stop
        Start-Sleep -Seconds 1
        & $MyInvocation.MyCommand.Path start
    }
    "status" {
        $pid = Get-DashboardPid
        if (-not $pid) { Write-Host "未运行"; return }
        Write-Host "运行中（PID $pid，端口 $Port）"
        try {
            $reply = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/auth" `
                -TimeoutSec 5 -UseBasicParsing
            Write-Host ("接口自检: " + $reply.Content)
        } catch {
            Write-Host "接口自检失败: $($_.Exception.Message)"
        }
    }
    "log" {
        if (Test-Path $LogFile) { Get-Content $LogFile -Tail $Lines } else { Write-Host "还没有日志" }
    }
    "fg" {
        & python "$Dir\server.py"
    }
}
