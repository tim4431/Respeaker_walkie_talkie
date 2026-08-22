<#
.SYNOPSIS
  Auto-detect and flash both processors of a ReSpeaker Lite walkie-talkie unit.

  - XMOS XU316: reads the USB DFU revision; if it is not the 48 kHz v1.1.0
    I2S firmware, flashes it via dfu-util. Handles the one-time WinUSB
    driver setup by launching Zadig and waiting for you to click Install.
  - ESP32-S3 (XIAO): finds the Espressif USB-Serial-JTAG COM port and runs
    idf.py flash (building first if needed).

  Connect the ReSpeaker Lite's own USB-C for the XMOS step, and the XIAO's
  USB-C for the ESP32 step (both at once is fine).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\flash_all.ps1
  powershell -ExecutionPolicy Bypass -File tools\flash_all.ps1 -EspOnly -Monitor
#>
param(
    [switch]$XmosOnly,
    [switch]$EspOnly,
    [switch]$Monitor,
    [string]$Port,
    [int]$DriverWaitSec = 300
)

$ErrorActionPreference = 'Stop'
$RepoRoot    = Split-Path $PSScriptRoot -Parent
$FirmwareBin = Join-Path $PSScriptRoot 'firmware\respeaker_lite_i2s_dfu_firmware_48k_v1.1.0_ch0-asr_ch1-mww.bin'
$FirmwareUrl = 'https://raw.githubusercontent.com/respeaker/ReSpeaker_Lite/master/xmos_firmwares/respeaker_lite_i2s_dfu_firmware_48k_v1.1.0_ch0-asr_ch1-mww.bin'
$ZadigExe    = Join-Path $PSScriptRoot 'zadig-2.9.exe'
$ZadigUrl    = 'https://github.com/pbatard/libwdi/releases/download/v1.5.1/zadig-2.9.exe'
$TargetRev   = 0x0110   # bcdDevice of the 48k v1.1.0 firmware
$IdfExport   = "$env:USERPROFILE\esp\v5.4\esp-idf\export.ps1"

function Find-DfuUtil {
    $c = Get-Command dfu-util -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $tool = Get-ChildItem "$env:USERPROFILE\.espressif\tools\dfu-util" -Recurse -Filter dfu-util.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($tool) { return $tool.FullName }
    throw 'dfu-util not found (install ESP-IDF tools or add dfu-util to PATH)'
}

function Get-XmosRev {
    $dev = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -match 'VID_2886&PID_0019' } | Select-Object -First 1
    if (-not $dev) { return $null }
    $hw = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName DEVPKEY_Device_HardwareIds).Data
    foreach ($h in $hw) {
        if ($h -match 'REV_([0-9A-Fa-f]{4})') { return [Convert]::ToInt32($Matches[1], 16) }
    }
    return $null
}

function Test-DfuOpens([string]$DfuUtil) {
    # cmd /c so dfu-util's stderr can't become a terminating PS error
    $out = cmd /c "`"$DfuUtil`" -l 2>&1" | Out-String
    return ($out -match 'Found DFU')
}

function Invoke-XmosFlash {
    $dfu = Find-DfuUtil
    $rev = Get-XmosRev
    if ($null -eq $rev) {
        Write-Host '[XMOS] ReSpeaker Lite USB not detected - plug in the board''s own USB-C port. Skipping.' -ForegroundColor Yellow
        return
    }
    Write-Host ("[XMOS] found ReSpeaker Lite, firmware rev {0:x4}" -f $rev)
    if ($rev -eq $TargetRev) {
        Write-Host '[XMOS] already on 48k v1.1.0 - nothing to do.' -ForegroundColor Green
        return
    }
    if (-not (Test-Path $FirmwareBin)) {
        Write-Host "[XMOS] downloading firmware..."
        New-Item -ItemType Directory -Force (Split-Path $FirmwareBin) | Out-Null
        Invoke-WebRequest -Uri $FirmwareUrl -OutFile $FirmwareBin
    }
    if (-not (Test-DfuOpens $dfu)) {
        Write-Host '[XMOS] No WinUSB driver bound to the DFU interface (one-time setup).' -ForegroundColor Yellow
        if (-not (Test-Path $ZadigExe)) { Invoke-WebRequest -Uri $ZadigUrl -OutFile $ZadigExe }
        Write-Host '        Launching Zadig - in its window:'
        Write-Host '          1. select "ReSpeaker Lite" in the dropdown (Options > List All Devices if empty)'
        Write-Host '          2. make sure the target driver says WinUSB, then click "Install Driver"'
        Start-Process $ZadigExe
        $deadline = (Get-Date).AddSeconds($DriverWaitSec)
        while ((Get-Date) -lt $deadline -and -not (Test-DfuOpens $dfu)) { Start-Sleep -Seconds 5 }
        if (-not (Test-DfuOpens $dfu)) { throw "Driver still not usable after $DriverWaitSec s - finish the Zadig install and re-run." }
        Write-Host '[XMOS] driver OK.' -ForegroundColor Green
    }
    Write-Host '[XMOS] flashing 48k v1.1.0 (takes ~30 s)...'
    cmd /c "`"$dfu`" -R -e -a 1 -D `"$FirmwareBin`" 2>&1" | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw "dfu-util failed with exit code $LASTEXITCODE" }
    Write-Host '[XMOS] waiting for the board to re-enumerate...'
    Start-Sleep -Seconds 5
    for ($i = 0; $i -lt 12; $i++) {
        $rev = Get-XmosRev
        if ($rev -eq $TargetRev) { break }
        Start-Sleep -Seconds 5
    }
    if ($rev -eq $TargetRev) {
        Write-Host '[XMOS] verified: firmware rev 0110 (48k v1.1.0).' -ForegroundColor Green
    } else {
        Write-Host ("[XMOS] flashed, but current rev reads {0} - power-cycle the board and re-run to verify." -f $(if ($null -eq $rev) { 'n/a' } else { '{0:x4}' -f $rev })) -ForegroundColor Yellow
    }
}

function Find-EspPort {
    if ($Port) { return $Port }
    # XIAO ESP32-S3 built-in USB-Serial-JTAG: VID 303A PID 1001
    $dev = Get-PnpDevice -PresentOnly -Class Ports -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -match 'VID_303A' } | Select-Object -First 1
    if ($dev -and $dev.FriendlyName -match '\((COM\d+)\)') { return $Matches[1] }
    # fallback: any non-COM1 serial port
    $any = Get-CimInstance Win32_SerialPort | Where-Object { $_.DeviceID -ne 'COM1' } | Select-Object -First 1
    if ($any) { return $any.DeviceID }
    return $null
}

function Update-WifiConfig {
    # Reads wifi.conf (WIFI_SSID= / WIFI_PASSWORD=) and patches sdkconfig.
    # Returns $true if anything changed. Run inside the IDF env, repo root cwd.
    $wifiConf = Join-Path $RepoRoot 'wifi.conf'
    if (-not (Test-Path $wifiConf)) { return $false }
    $cfg = @{}
    Get-Content $wifiConf | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_]+)\s*=\s*(.+?)\s*$') { $cfg[$Matches[1]] = $Matches[2] }
    }
    if (-not $cfg.ContainsKey('WIFI_SSID') -or -not $cfg.ContainsKey('WIFI_PASSWORD')) {
        Write-Host '[ESP32] wifi.conf found but missing WIFI_SSID/WIFI_PASSWORD - ignoring.' -ForegroundColor Yellow
        return $false
    }
    $sdk = Join-Path $RepoRoot 'sdkconfig'
    if (-not (Test-Path $sdk)) {
        Write-Host '[ESP32] generating sdkconfig...'
        idf.py reconfigure | Out-Null
    }
    $content = [IO.File]::ReadAllText($sdk)
    $new = $content -replace '(?m)^CONFIG_WT_WIFI_SSID=".*"', "CONFIG_WT_WIFI_SSID=`"$($cfg['WIFI_SSID'])`""
    $new = $new -replace '(?m)^CONFIG_WT_WIFI_PASSWORD=".*"', "CONFIG_WT_WIFI_PASSWORD=`"$($cfg['WIFI_PASSWORD'])`""
    if ($new -ne $content) {
        [IO.File]::WriteAllText($sdk, $new)
        Write-Host ("[ESP32] applied WiFi credentials from wifi.conf (SSID: {0})" -f $cfg['WIFI_SSID'])
        return $true
    }
    Write-Host ("[ESP32] WiFi credentials already current (SSID: {0})" -f $cfg['WIFI_SSID'])
    return $false
}

function Invoke-EspFlash {
    $p = Find-EspPort
    if (-not $p) {
        Write-Host '[ESP32] no Espressif COM port found - plug in the XIAO''s USB-C port. Skipping.' -ForegroundColor Yellow
        return
    }
    Write-Host "[ESP32] flashing via $p ..."
    # export.ps1 and idf.py write progress to stderr; don't let strict mode kill us
    $ErrorActionPreference = 'Continue'
    . $IdfExport *> $null
    Push-Location $RepoRoot
    try {
        Update-WifiConfig | Out-Null
        idf.py -p $p flash   # rebuilds first if sources or sdkconfig changed
        if ($LASTEXITCODE -ne 0) { throw "idf.py flash failed with exit code $LASTEXITCODE" }
        Write-Host '[ESP32] flashed.' -ForegroundColor Green
        if ($Monitor) { idf.py -p $p monitor }
    } finally {
        Pop-Location
    }
}

if (-not $EspOnly)  { Invoke-XmosFlash }
if (-not $XmosOnly) { Invoke-EspFlash }
Write-Host 'Done.'
