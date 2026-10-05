# Direct NI-488.2 bus scan through ADLINK's gpib-32.dll (bypasses VISA entirely).
# Read-only: finds which GPIB addresses have a listener. Does not command any instrument.
$ErrorActionPreference = 'Stop'
$src = @'
using System;
using System.Runtime.InteropServices;
public static class Gpib {
    [DllImport("gpib-32.dll", CharSet = CharSet.Ansi)] public static extern int ibfind(string name);
    [DllImport("gpib-32.dll")] public static extern int ibln(int ud, int pad, int sad, ref short listen);
    [DllImport("gpib-32.dll")] public static extern int ibonl(int ud, int v);
    [DllImport("gpib-32.dll")] public static extern int ibsic(int ud);
    [DllImport("gpib-32.dll")] public static extern int ibask(int ud, int option, ref int value);
    [DllImport("gpib-32.dll")] public static extern int ThreadIbsta();
    [DllImport("gpib-32.dll")] public static extern int ThreadIberr();
}
'@
Add-Type -TypeDefinition $src -Language CSharp

$ERRBIT = 0x8000
$errNames = @('EDVR/system call','ECIC/not CIC','ENOL/no listener','EADR/addressing','EARG/bad arg',
              'ESAC/not sys ctrl','EABO/timeout','ENEB/no board','EDMA','?','EOIP','ECAP','EFSO','?',
              'EBUS/no devices','ESTB','ESRQ','?','?','?','ETAB')
function Err([int]$e) { if ($e -ge 0 -and $e -lt $errNames.Count) { $errNames[$e] } else { "err$e" } }

foreach ($boardName in @('GPIB0','GPIB1','GPIB2')) {
    $ud = [Gpib]::ibfind($boardName)                        
    if ($ud -lt 0) { Write-Output ("{0}: not found (ibfind={1})" -f $boardName, $ud); continue }

    Write-Output ("{0}: OPENED  ud={1}  ibsta=0x{2:X4}" -f $boardName, $ud, [Gpib]::ThreadIbsta())

    $pad = 0
    [void][Gpib]::ibask($ud, 1, [ref]$pad)      # IbaPAD = 1 -> this controller's own GPIB address
    Write-Output ("  controller primary address = {0}" -f $pad)

    [void][Gpib]::ibsic($ud)                    # assert IFC / become controller-in-charge
    $sta = [Gpib]::ThreadIbsta()
    if ($sta -band $ERRBIT) {
        Write-Output ("  ibsic FAILED ibsta=0x{0:X4} iberr={1}" -f $sta, (Err ([Gpib]::ThreadIberr())))
        [void][Gpib]::ibonl($ud, 0); continue
    }

    Write-Output "  scanning addresses 1..30 for listeners..."
    $found = @()
    for ($a = 1; $a -le 30; $a++) {
        if ($a -eq $pad) { continue }
        [int16]$listen = 0
        [void][Gpib]::ibln($ud, $a, 0, [ref]$listen)
        $s = [Gpib]::ThreadIbsta()
        if ($s -band $ERRBIT) {
            Write-Output ("    addr {0,2}: ibln error ibsta=0x{1:X4} iberr={2}" -f $a, $s, (Err ([Gpib]::ThreadIberr())))
            continue
        }
        if ($listen -ne 0) { $found += $a }
    }
    if ($found.Count -eq 0) { Write-Output "    NO listeners found on this board" }
    else { Write-Output ("  LISTENERS AT: " + ($found -join ', ')) }

    [void][Gpib]::ibonl($ud, 0)
}
