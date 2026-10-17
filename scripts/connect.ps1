param(
    [Parameter(Mandatory = $true)][string]$Server,
    [ValidateRange(1024, 65535)][int]$LocalPort = 8088
)
$ErrorActionPreference = 'Stop'
Write-Host "浏览器打开 http://127.0.0.1:$LocalPort，保持此终端运行。"
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L "127.0.0.1:${LocalPort}:127.0.0.1:8088" -L "127.0.0.1:8089:127.0.0.1:8089" $Server
