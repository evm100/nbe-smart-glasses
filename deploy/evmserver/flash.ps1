# Flash a prebuilt NBE node image over USB on Windows 10/11, then show its log.
# Windows twin of flash.sh. Double-click flash.bat, or from a terminal in this folder:
#
#   .\flash.bat GLS03 [COM5]        the image GLS03.bin must be next to this script
#
# Needs Python 3 (python.org or 'winget install Python.Python.3.12'); the first run installs
# esptool + pyserial into %USERPROFILE%\.nbe-flash-venv.
# Board not found? Hold BOOT, plug in USB, release BOOT, run again.
# Keep this file plain ASCII: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.
param([string]$Id = '', [string]$Port = '')
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

function Fail([string]$msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

# --- which image -------------------------------------------------------------------------------
if (-not $Id) {
  # double-clicked: one .bin next to the script -> use it, otherwise ask
  $bins = @(Get-ChildItem -Path $here -Filter '*.bin' -File)
  if ($bins.Count -eq 1) { $Id = $bins[0].BaseName }
  else { $Id = Read-Host 'Node ID to flash (e.g. GLS03)' }
}
$Id = $Id.Trim().ToUpper()
if (-not $Id) { Fail 'usage: flash.bat <NODE_ID> [COMx]' }
$img = Join-Path $here "$Id.bin"
if (-not (Test-Path -LiteralPath $img -PathType Leaf)) { Fail "no $img - put $Id.bin next to flash.ps1" }

# --- which serial port -------------------------------------------------------------------------
function Find-Port {  # COM port of an ESP32 board: Espressif native USB first, then common USB-UART bridges
  $vids = 'VID_303A', 'VID_10C4', 'VID_1A86', 'VID_0403'   # Espressif, CP210x, CH34x, FTDI
  $devs = @(Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '\((COM\d+)\)' })
  foreach ($vid in $vids) {
    foreach ($d in $devs) {
      if ("$($d.PNPDeviceID)" -match $vid -and $d.Name -match '\((COM\d+)\)') { return $Matches[1] }
    }
  }
  return ''
}
if (-not $Port) { $Port = Find-Port }
if (-not $Port) {
  $all = [System.IO.Ports.SerialPort]::GetPortNames()
  Write-Host 'no board found - plug in the XIAO with a data USB-C cable (or give the port: flash.bat' $Id 'COM5)'
  Write-Host 'a new board that never shows up: hold BOOT, plug it in, release BOOT, run this again'
  if ($all) { Write-Host ('serial ports seen: ' + ($all -join ', ')) }
  exit 1
}
$Port = $Port.ToUpper()

# --- Python + esptool --------------------------------------------------------------------------
function Find-Python {  # a real Python 3 (skips the Microsoft Store 'python' stub, which isn't one)
  foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
    if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
    $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
    try { $v = & $exe @pre -c 'import sys; print(sys.version_info[0])' 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0 -and "$v".Trim() -eq '3') { return , $c }
  }
  return $null
}
$V = Join-Path $HOME '.nbe-flash-venv'
$py = Join-Path $V 'Scripts\python.exe'
$ready = $false
if (Test-Path -LiteralPath $py) {
  & $py -c 'import esptool, serial' 2>$null
  $ready = ($LASTEXITCODE -eq 0)
}
if (-not $ready) {
  $sys = Find-Python
  if (-not $sys) {
    Fail ("Python 3 is missing: install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')`n" +
          "or run: winget install Python.Python.3.12   - then open a new window and run this again")
  }
  $exe = $sys[0]; $pre = @($sys | Select-Object -Skip 1)
  Write-Host "first run: installing esptool into $V ..."
  if (-not (Test-Path -LiteralPath $py)) {
    & $exe @pre -m venv $V
    if ($LASTEXITCODE -ne 0) { Fail "could not create $V" }
  }
  & $py -m pip install -q --disable-pip-version-check 'esptool>=5' pyserial
  if ($LASTEXITCODE -ne 0) { Fail 'pip install failed (no internet?) - run this again' }
}

# --- flash -------------------------------------------------------------------------------------
Write-Host "flashing $Id -> $Port (erases the whole chip first)"
& $py -m esptool --chip esp32s3 -p $Port -b 460800 --before default-reset --after hard-reset `
    write-flash --erase-all 0x0 $img
if ($LASTEXITCODE -ne 0) {
  Fail ("flashing failed. Port busy? Close Arduino IDE / serial monitors / a running NBE gateway.`n" +
        "Still failing: hold BOOT, unplug and replug USB, release BOOT, run this again.")
}
Write-Host ''

# the camera task only exists in camera builds
$text = [System.Text.Encoding]::ASCII.GetString([System.IO.File]::ReadAllBytes($img))
if ($text.Contains('ppg_camera')) {
  # camera board: its USB port carries binary data for the NBE USB gateway, not a readable log
  Write-Host "done. $Id is a camera board: its USB port now talks to the NBE USB gateway." -ForegroundColor Green
  Write-Host "Next: download the gateway for $Id from the NBE server (Nodes page -> USB gateway), unzip it,"
  Write-Host "start it, and keep the glasses plugged in. $Id shows as online on the website when it works."
  exit 0
}
Write-Host "log of $Id (Wi-Fi / broker connection); Ctrl-] to quit. Look for 'got IP' and 'connected to broker'."
Start-Sleep -Seconds 2   # the board re-enumerates its USB port after the reset
& $py -m serial.tools.miniterm --raw $Port 115200
exit $LASTEXITCODE
