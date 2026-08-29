$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$environmentFile = Join-Path $projectRoot "runtime\cloud.env"

if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw "缺少 runtime\cloud.env，请先完成云端部署配置。"
}

foreach ($line in Get-Content -LiteralPath $environmentFile) {
    if ($line -match '^([^#=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
    }
}

$env:LM_CLOUD_URL = "https://nx.ybwlawa0.com/living-margins"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

& $python (Join-Path $projectRoot "cloud_relay.py")
