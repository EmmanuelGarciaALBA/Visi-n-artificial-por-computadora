# Instala VisionServer para que arranque solo y abre los puertos en el firewall.
# Ejecutar en PowerShell COMO ADMINISTRADOR:
#   powershell -ExecutionPolicy Bypass -File .\instalar_servicio.ps1
# Para quitarlo:
#   powershell -ExecutionPolicy Bypass -File .\instalar_servicio.ps1 -Desinstalar

param([switch]$Desinstalar)

$ErrorActionPreference = "Stop"
$TaskName = "VisionServer"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Dir ".venv\Scripts\pythonw.exe"
$Cfg = Get-Content (Join-Path $Dir "config.json") -Raw | ConvertFrom-Json
$Ports = @($Cfg.server.port, $Cfg.opcua.port, $Cfg.tcp.port)

if ($Desinstalar) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName "VisionServer*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host "VisionServer desinstalado."
    return
}

if (-not (Test-Path $Python)) { throw "No existe $Python. Ejecuta primero iniciar.bat para crear el entorno." }

# Tarea programada: arranca al iniciar sesión, reinicia si falla, sin ventana.
$action = New-ScheduledTaskAction -Execute $Python -Argument "run.py" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Servidor de visión artificial (web + OPC UA + TCP)" -Force | Out-Null

# Firewall: permitir conexiones entrantes en la red privada (Wi-Fi/LAN de la planta)
Get-NetFirewallRule -DisplayName "VisionServer*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "VisionServer TCP $($Ports -join ',')" -Direction Inbound -Protocol TCP `
    -LocalPort $Ports -Action Allow -Profile Private,Domain | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "VisionServer instalado e iniciado. Puertos abiertos: $($Ports -join ', ')"
Write-Host "Nota: la red Wi-Fi debe estar marcada como 'Privada' en Windows para aceptar conexiones."
