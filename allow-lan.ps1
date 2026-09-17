$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot 'runtime\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Extract the entire application package first.' }
$interfaces = @(Get-NetIPConfiguration | Where-Object { $_.NetAdapter.HardwareInterface -and $_.IPv4DefaultGateway } | Select-Object -ExpandProperty InterfaceAlias)
if (-not $interfaces.Count) { throw 'No active local network interface was found.' }
$ruleName = 'ACGLib Local Network'
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) { $existing | Remove-NetFirewallRule }
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Program $pythonPath -InterfaceAlias $interfaces -RemoteAddress LocalSubnet -Profile Any -EdgeTraversalPolicy Block | Out-Null
Write-Output 'Local network access is ready. Connect your phone to the same Wi-Fi.'
