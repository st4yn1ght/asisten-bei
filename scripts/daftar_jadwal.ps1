# BUKAN SARAN INVESTASI. Mendaftarkan tugas Task Scheduler "Asisten BEI Harian" (SPEC bagian 19.3).
# Dipanggil oleh scripts\daftar_jadwal.bat dengan -ExecutionPolicy Bypass; kebijakan sistem tidak diubah.
# Pemicu: Senin-Jumat WIB pada jadwal.jam_run dari config.yaml, dikonversi ke jam (dan hari) lokal Windows.
$ErrorActionPreference = "Stop"
$NamaTugas = "Asisten BEI Harian"
$Akar = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Akar ".venv\Scripts\python.exe"
$Bat = Join-Path $Akar "scripts\harian_terjadwal.bat"
$Pembantu = Join-Path $Akar "scripts\info_jadwal.py"

function Selesai([int]$Kode) {
    Write-Host "BUKAN SARAN INVESTASI."
    exit $Kode
}

Write-Host "BUKAN SARAN INVESTASI."
Write-Host "Mendaftarkan tugas '$NamaTugas' untuk folder $Akar"
if (-not (Test-Path $Python)) {
    Write-Host "ERROR: .venv belum ada. Jalankan scripts\pasang.bat terlebih dahulu."
    Selesai 1
}

# jadwal.jam_run dan operasional.batas_waktu_run_jam dibaca lewat program (sekaligus memvalidasi config.yaml).
# info_jadwal.py hanya menulis ke stdout; selama pemanggilan ini, keluaran program tidak menghentikan skrip.
Push-Location $Akar
$ErrorActionPreference = "Continue"
try {
    $env:PYTHONUTF8 = "1"
    $Keluaran = @(& $Python $Pembantu "config.yaml")
    $KodeKeluar = $LASTEXITCODE
} finally {
    $ErrorActionPreference = "Stop"
    Pop-Location
}
$Baris = @($Keluaran | ForEach-Object { "$_".Trim() } | Where-Object { $_ -ne "" })
if ($KodeKeluar -ne 0 -or $Baris.Count -lt 2 -or $Baris[0] -notmatch '^\d{1,2}:\d{2}$') {
    Write-Host "ERROR: config.yaml tidak bisa dibaca. Jalankan scripts\cek.bat untuk rinciannya."
    $Keluaran | ForEach-Object { Write-Host "  $_" }
    Selesai 3
}
$JamRun = $Baris[0]
$BatasJam = [double]::Parse($Baris[1], [Globalization.CultureInfo]::InvariantCulture)

# WIB = "SE Asia Standard Time" (UTC+7). Konversi jam_run WIB hari ini ke jam lokal Windows; bila konversi
# melewati tengah malam, hari pemicu ikut digeser agar tetap Senin-Jumat WIB.
$ZonaWib = [TimeZoneInfo]::FindSystemTimeZoneById("SE Asia Standard Time")
$ZonaLokal = [TimeZoneInfo]::Local
$Bagian = $JamRun.Split(":")
$Hari = [DateTime]::Today
$Wib = New-Object DateTime ($Hari.Year, $Hari.Month, $Hari.Day, [int]$Bagian[0], [int]$Bagian[1], 0, ([DateTimeKind]::Unspecified))
$Lokal = [TimeZoneInfo]::ConvertTime($Wib, $ZonaWib, $ZonaLokal)
$Geser = ($Lokal.Date - $Wib.Date).Days
$HariBursa = @([DayOfWeek]::Monday, [DayOfWeek]::Tuesday, [DayOfWeek]::Wednesday, [DayOfWeek]::Thursday, [DayOfWeek]::Friday)
$HariLokal = @($HariBursa | ForEach-Object { [DayOfWeek]((([int]$_) + $Geser + 7) % 7) })
Write-Host ("Jam run {0} WIB = {1:HH:mm} waktu lokal Windows ({2})." -f $JamRun, $Lokal, $ZonaLokal.Id)
if ($ZonaLokal.Id -ne $ZonaWib.Id) {
    Write-Host "PERINGATAN: zona waktu Windows bukan WIB (SE Asia Standard Time). Pemicu memakai jam lokal hasil konversi."
    Write-Host "  Program tetap aman: run harian menunggu sampai bar hari itu final (SPEC bagian 8.6)."
    if ($Geser -ne 0) {
        Write-Host ("PERINGATAN: konversi melewati tengah malam; hari pemicu lokal: {0}." -f ($HariLokal -join ", "))
    }
}

try {
    $Aksi = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c "{0}"' -f $Bat) -WorkingDirectory $Akar
    $Pemicu = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $HariLokal -At $Lokal
    $Batas = New-TimeSpan -Minutes ([int][Math]::Ceiling($BatasJam * 60))
    $Pengaturan = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit $Batas -MultipleInstances IgnoreNew
    $Pengguna = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Prinsipal = New-ScheduledTaskPrincipal -UserId $Pengguna -LogonType Interactive
    Register-ScheduledTask -TaskName $NamaTugas -Action $Aksi -Trigger $Pemicu -Settings $Pengaturan `
        -Principal $Prinsipal -Description "BUKAN SARAN INVESTASI. Run harian asisten-bei (harian --jadwal)." -Force | Out-Null

    # Verifikasi.
    $Tugas = Get-ScheduledTask -TaskName $NamaTugas
    $InfoTugas = $Tugas | Get-ScheduledTaskInfo
} catch {
    Write-Host ("ERROR: tugas gagal didaftarkan: {0}" -f $_.Exception.Message)
    Write-Host "  Jalankan daftar_jadwal.bat sekali lagi. Bila tetap gagal, buat tugas secara manual di Task Scheduler."
    Selesai 1
}
Write-Host ""
Write-Host "Tugas terdaftar:"
Write-Host ("  Nama        : {0}" -f $Tugas.TaskName)
Write-Host ("  Status      : {0}" -f $Tugas.State)
Write-Host ("  Perintah    : {0} {1}" -f $Tugas.Actions[0].Execute, $Tugas.Actions[0].Arguments)
Write-Host ("  Folder kerja: {0}" -f $Tugas.Actions[0].WorkingDirectory)
Write-Host ("  Hari        : {0}, pukul {1:HH:mm} waktu lokal" -f ($HariLokal -join ", "), $Lokal)
Write-Host ("  Run berikut : {0}" -f $InfoTugas.NextRunTime)
Write-Host ("  Batas waktu : {0} jam; mulai susulan bila terlewat; tidak ada dua run bersamaan" -f $BatasJam)
Write-Host ("  Pengguna    : {0} (hanya saat Anda login; kata sandi tidak disimpan)" -f $Pengguna)
Write-Host ""
Write-Host "Opsional: agar PC yang tertidur bangun untuk run ini, buka Task Scheduler > '$NamaTugas' > Properties >"
Write-Host "  tab Conditions, lalu centang 'Wake the computer to run this task' (lihat README)."
Write-Host "Menjalankan daftar_jadwal.bat lagi menimpa tugas ini; pengaturan manual seperti di atas perlu diulang."
Write-Host "Keluaran setiap run terjadwal: output\log\terjadwal.log"
Selesai 0
