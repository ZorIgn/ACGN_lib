param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$libraryRoot = $PSScriptRoot
$runPath = Join-Path $libraryRoot 'run.py'
$pythonPath = Join-Path $libraryRoot 'runtime\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = Join-Path $libraryRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python runtime is missing. Extract the entire application package first.' }
$dataPath = Join-Path $libraryRoot 'src\db'
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$pidPath = Join-Path $dataPath 'library.pid'
$running = $null
if (Test-Path -LiteralPath $pidPath) {
    $savedPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $running = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    if ($running -and ((-not $running.CommandLine) -or (-not $running.CommandLine.Contains($runPath)))) { $running = $null }
}
if (-not $running) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
    if ($listener) { throw 'Port 8000 is already in use. Stop the application using it before starting this library.' }
    $process = Start-Process -FilePath $pythonPath -ArgumentList @('-X', 'utf8', '-u', ('"' + $runPath + '"')) -WorkingDirectory $libraryRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataPath 'runtime.log') -RedirectStandardError (Join-Path $dataPath 'runtime-error.log') -PassThru
    Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii
}
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $null = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/accounts/login/' -UseBasicParsing -TimeoutSec 2
        $ready = $true
        break
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $ready) { throw 'Startup failed. See src\db\runtime-error.log.' }
if ($OpenBrowser) { Start-Process 'http://127.0.0.1:8000/library/' }
Write-Output 'http://127.0.0.1:8000/library/'
Get-NetIPConfiguration | Where-Object { $_.NetAdapter.HardwareInterface -and $_.IPv4DefaultGateway } | ForEach-Object {
    foreach ($address in $_.IPv4Address) { Write-Output ("Phone: http://" + $address.IPAddress + ":8000/library/") }
}
