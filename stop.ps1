$ErrorActionPreference = 'Stop'
$pidPath = Join-Path $PSScriptRoot 'src\db\library.pid'
if (Test-Path -LiteralPath $pidPath) {
    $savedPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    $runPath = Join-Path $PSScriptRoot 'run.py'
    if ($process -and $process.CommandLine.Contains($runPath)) {
        Stop-Process -Id $savedPid
    }
    Remove-Item -LiteralPath $pidPath
}
