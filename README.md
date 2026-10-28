# asisten-bei

> **BUKAN SARAN INVESTASI.** Program pribadi yang membuat laporan otomatis dari aturan mekanis dan data publik yang
> bisa salah, terlambat, atau tidak lengkap. Hasil backtest dan paper trading tidak menjamin hasil di masa depan.
> Seluruh keputusan dan risiko transaksi menjadi tanggung jawab Anda.

Setiap sore setelah bursa tutup, program menjawab tiga pertanyaan untuk hari bursa berikutnya:

1. **Kondisi pasar:** BOLEH BELI, HATI-HATI, atau JANGAN BELI, beserta alasannya.
2. **Kandidat beli:** saham yang layak dibeli, dengan rencana lengkap (harga limit maksimal, stop loss, target, lot).
3. **Posisi yang Anda pegang:** tahan atau jual, dan di level berapa order otomatis stop/target dipasang.

Program **tidak** terhubung ke broker dan tidak pernah mengirim order. Semua order Anda pasang dan tarik sendiri.
Spesifikasi lengkap ada di `SPEC.md`; panduan mengisi berkas ada di `contoh/PANDUAN_ISI.md`.

---

## Daftar isi

1. [Konsekuensi modal Rp 3 juta](#1-konsekuensi-modal-rp-3-juta)
2. [Instalasi](#2-instalasi)
3. [Berkas yang Anda isi sendiri](#3-berkas-yang-anda-isi-sendiri)
4. [Telegram](#4-telegram)
5. [Penjadwalan otomatis](#5-penjadwalan-otomatis)
6. [Rutinitas harian](#6-rutinitas-harian)
7. [Perintah dan berkas .bat](#7-perintah-dan-berkas-bat)
8. [Kode keluar dan masalah umum](#8-kode-keluar-dan-masalah-umum)
9. [Uji cepat dengan data nyata](#9-uji-cepat-dengan-data-nyata)
10. [Glosarium](#10-glosarium)

---

## 1. Konsekuensi modal Rp 3 juta

Ringkasan SPEC §3. Aturan bawaan: risiko per transaksi 1% ekuitas (Rp 30.000; setengahnya saat HATI-HATI),
maksimal 3 posisi, 1 posisi per sektor, alokasi per posisi maksimal ekuitas ÷ 3 (≈ Rp 1 juta). Fee beli 0,15%,
fee jual 0,25%, dan slippage 0,10% sudah dihitung. Lot dihitung dari `harga_maks`, dan jarak stop `d` diukur dari
`harga_maks`.

| Jarak stop `d` dari harga_maks | Harga maks agar risiko 1 lot ≤ Rp 30.000 | Saat HATI-HATI (Rp 15.000) | Nilai posisi maks pada risiko 1% |
|---|---|---|---|
| 3% | ≈ Rp 8.600 | ≈ Rp 4.300 | ≈ Rp 860 ribu |
| 5% | ≈ Rp 5.470 | ≈ Rp 2.740 | ≈ Rp 547 ribu |
| 8% | ≈ Rp 3.540 | ≈ Rp 1.770 | ≈ Rp 354 ribu |
| 10% | ≈ Rp 2.870 | ≈ Rp 1.430 | ≈ Rp 287 ribu |
| 12% | ≈ Rp 2.410 | ≈ Rp 1.200 | ≈ Rp 241 ribu |

Jarak stop tipikal pada saham dengan ATR 2–3% dari harga: BO ≈ 5–7,4%, MR ≈ 6–8,9%; PB paling bervariasi.

Akibatnya:

- Saham di atas ≈ Rp 9.985 per lembar tidak bisa dibeli walau 1 lot (batas alokasi Rp 1 juta). Batas alokasi
  ini hanya mengikat bila `d` < ± 2,5%.
- Banyak saham LQ45 akan sering berstatus `TIDAK_TERJANGKAU`. Wajar bila berhari-hari tidak ada rekomendasi beli.
  Saat HATI-HATI, hampir semua kandidat tidak terjangkau.
- Lot dibulatkan ke bawah, jadi nilai posisi sering lebih kecil dari tabel dan risiko sebenarnya sering jauh di
  bawah 1%. Dengan 3 posisi, biasanya hanya ± 20–55% modal terpakai.
- **Ilustrasi (bukan prediksi):** 24 transaksi/tahun × ekspektansi 0,2R × risiko rata-rata 0,8% ≈ 3,8%/tahun
  ≈ Rp 115 ribu/tahun.
- Gerbang backtest menilai aturan pada modal besar (mode `edge`, dengan aturan slot yang sama). Karena slot sedikit,
  syarat "≥ 30 transaksi di holdout" bisa sulit tercapai sehingga strategi tetap nonaktif; ini disengaja.
- Ringkasan backtest juga menampilkan kinerja subset transaksi yang terjangkau modal Anda. Jika subset ini lemah,
  laporan harian memberi peringatan.

---

## 2. Instalasi

Windows 10/11. Ikuti berurutan; setiap langkah memakai klik ganda pada berkas di folder `scripts\`.

1. **Pasang Python 3.12 atau 3.13 (64-bit)** dari python.org. Di layar pertama pemasang, centang
   **"Add python.exe to PATH"** dan **"py launcher"**, lalu klik *Install Now*.
2. **Ekstrak folder `asisten-bei`** ke jalur sederhana, mis. `C:\asisten-bei`. **Jangan** di dalam OneDrive atau
   Dropbox: sinkronisasi bisa mengunci `data\jurnal.sqlite` dan cache.
3. **Klik ganda `scripts\pasang.bat`.** Jendela hitam menampilkan versi Python, membuat folder `.venv`, lalu memasang
   semua paket (beberapa menit; perlu internet). Bila muncul "Python 3.12 atau 3.13 tidak ditemukan", ulangi
   langkah 1.
4. **Isi berkas di `input\` dengan Notepad dan tinjau `config.yaml`** — lihat
   [bagian 3](#3-berkas-yang-anda-isi-sendiri).
5. **Klik ganda `scripts\cek.bat`** dan perbaiki setiap baris ERROR (pesannya menyebut berkas, baris, dan kolom).
   Baca juga setiap PERINGATAN dan item "BELUM DIVERIFIKASI".
6. **Klik ganda `scripts\unduh.bat`.** Unduhan pertama (riwayat sejak 2013) bisa lama. Periksa ringkasannya.
7. **Klik ganda `scripts\backtest.bat`**, lalu buka `ringkasan.html` di folder run terbaru
   `output\backtest\<YYYYMMDD-HHMMSS>_<kode>\`.
8. **Siapkan Telegram** ([bagian 4](#4-telegram)), lalu klik ganda `scripts\uji_telegram.bat`.
9. **Klik ganda `scripts\harian.bat`** sekali secara manual dan baca `output\laporan\<tanggal>\laporan.html`.
10. **Daftarkan jadwal** dengan klik ganda `scripts\daftar_jadwal.bat` ([bagian 5](#5-penjadwalan-otomatis)).

Setiap jendela `.bat` manual menampilkan kode keluar ([bagian 8](#8-kode-keluar-dan-masalah-umum)) dan menunggu
Anda menekan tombol sebelum menutup.

Untuk pengembang (Linux/macOS):

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e . --no-deps
.venv/bin/python -m pytest -q
```

---

## 3. Berkas yang Anda isi sendiri

Letak, format, sumber, dan contoh terisi setiap berkas ada di **`contoh/PANDUAN_ISI.md`**. Edit CSV dengan
**Notepad**, bukan Excel; angka tanpa pemisah ribuan (`2850`, bukan `2.850`); tanggal `YYYY-MM-DD`. Program hanya
membaca `input\`, `config.yaml`, dan `.env`; isi folder `contoh\` tidak pernah dipakai.

**Daftar cek (SPEC §24):**

| # | Item | Di mana | Sumber |
|---|---|---|---|
| 1 | Konstituen universe (mis. LQ45 terbaru) + sektor | `input\universe.csv` | idx.co.id (pengumuman indeks) |
| 2 | Daftar saham breadth (100–200 saham likuid) | `input\breadth_universe.csv` | idx.co.id |
| 3 | Riwayat konstituen (opsional) | `input\universe_history.csv` | Arsip pengumuman evaluasi indeks |
| 4 | Saham suspensi / notasi khusus / papan pemantauan khusus | `input\exclude.csv` | idx.co.id; perbarui setiap ada pengumuman |
| 5 | Kalender libur tahun berjalan **dan** tahun berikutnya | `input\holidays.csv` | idx.co.id |
| 6 | Jadwal sesi (jam penutupan, pra-pembukaan) | `config.yaml → pasar.jadwal_sesi` | Jadwal perdagangan terbaru di idx.co.id |
| 7 | Tabel fraksi harga + tanggal berlaku | `config.yaml → pasar.fraksi_harga` | Peraturan perdagangan terbaru di idx.co.id |
| 8 | Batas ARA/ARB, harga acuan, tanggal berlaku | `config.yaml → pasar.auto_reject` | Peraturan/pengumuman terbaru di idx.co.id |
| 9 | Fee beli/jual, biaya minimum, pembulatan fee → `biaya.terverifikasi: true` | `config.yaml → biaya` | Trade confirmation broker |
| 10 | Satuan volume → `pengali_volume_terverifikasi: true` | `config.yaml → data` | [Uji cepat](#9-uji-cepat-dengan-data-nyata) langkah 3 |
| 11 | Fitur order broker (order otomatis stop/target, menarik order, jual di harga pembukaan, limit beli) | `contoh\cek_fitur_broker.md` | Broker Anda |
| 12 | Saldo kas dan posisi | `input\akun.yaml`, `input\portfolio.csv` | Akun broker, setiap ada transaksi |
| 13 | Token & chat ID Telegram | `.env` | @BotFather ([bagian 4](#4-telegram)) |
| 14 | `holdout_mulai` | `config.yaml → backtest` | Tetapkan sekali sebelum backtest pertama, lalu jangan diubah |

Item 6–8 sengaja kosong saat rilis. **Jangan mengisinya dari ingatan** — hanya dari idx.co.id. Program tetap
berjalan tanpa nilai itu: selama `terverifikasi: false`, harga dibulatkan ke rupiah bulat dan setiap laporan
meminta Anda membulatkan sendiri ke fraksi valid (limit beli dan target ke **bawah**, stop ke **atas**).

Akibat beberapa isian:

- **Item 11:** bila broker tidak bisa memasang order otomatis stop pada hari saham dibeli, setel
  `entry.stop_aktif_hari_entry: false` lalu jalankan ulang `backtest.bat`. Program tidak memakai hasil jual yang
  belum terjadi untuk membiayai beli (`risiko.dana_jual_langsung_dipakai: false`), walaupun limit beli di aplikasi
  broker bisa terlihat lebih besar.
- **`input\holidays.csv`** harus berisi tahun berjalan dan tahun berikutnya. Tanpa itu `cek` dan setiap laporan
  memberi peringatan, dan rencana bisa diberi banner "Tanggal t+1 belum terverifikasi terhadap kalender libur
  resmi". Tanggal Sabtu/Minggu tidak perlu ditulis (tidak berpengaruh; `cek` memberi PERINGATAN).
- **`input\akun.yaml`** yang `diperbarui`-nya lebih tua dari 5 hari bursa memicu peringatan "akun basi".
- Mengubah aturan di `config.yaml` (bukan bagian laporan, notifikasi, jurnal, jadwal, atau metadata verifikasi)
  atau berkas universe/breadth/holidays/anomali membuat strategi berstatus `BAYANGAN_GERBANG` sampai
  `backtest.bat` dijalankan ulang. Laporan menyebut bagian yang berubah. Hasil backtest juga dianggap kedaluwarsa
  setelah `gerbang.kedaluwarsa_hari` (90 hari).

---

## 4. Telegram

1. Di aplikasi Telegram, buka **@BotFather**, kirim `/newbot`, ikuti langkahnya, lalu salin **token** yang diberikan.
2. Kirim pesan apa pun (mis. "halo") ke bot baru Anda.
3. Salin `.env.example` (di folder utama, di sebelah `config.yaml`) menjadi berkas bernama tepat **`.env`**, lalu
   isi `TELEGRAM_BOT_TOKEN` dengan token Anda (contoh isian: `contoh\env.contoh`). Di Notepad pilih *Simpan sebagai
   jenis: Semua berkas* agar namanya `.env`, **bukan** `.env.txt` (`cek` mendeteksi kesalahan ini). Jangan bagikan
   berkas ini.
4. Buka Command Prompt di folder proyek dan jalankan `scripts\uji_telegram.bat --cari-chat-id`. Salin angka chat ID
   yang dicetak ke `TELEGRAM_CHAT_ID` di `.env`.
5. Klik ganda `scripts\uji_telegram.bat`. Pesan uji harus muncul di Telegram (kode keluar 0). Kode 3 berarti `.env`
   belum disiapkan (pesan di layar menyebut apa yang kurang); kode 4 berarti pengiriman gagal.

Setiap pesan diakhiri "BUKAN SARAN INVESTASI." Pesan harian berisi status pasar, ringkasan aksi (termasuk
pengingat menarik order beli yang tidak terisi saat pembukaan), rencana beli singkat, perubahan level stop, dan
peringatan penting. `laporan.html` dilampirkan bila ukurannya ≤ `notifikasi.telegram.maks_lampiran_mb`; bila lebih
besar, pesan memberi tahu agar Anda membukanya di folder `output\laporan`. Untuk mematikan pengiriman, setel
`notifikasi.telegram.aktif: false`. Token tidak pernah tercetak di log, layar, laporan, atau pesan (diganti `***`).

---

## 5. Penjadwalan otomatis

Klik ganda **`scripts\daftar_jadwal.bat`**. Skrip ini membuat (atau menimpa) tugas Task Scheduler
**"Asisten BEI Harian"**:

- Senin–Jumat pada `jadwal.jam_run` (default 19:30 **WIB**). Jam ini dikonversi ke jam lokal Windows dan dicetak;
  bila zona waktu Windows bukan WIB, skrip memberi peringatan. Program tetap aman karena run harian menunggu
  sendiri sampai data hari itu final. Bila Anda mengubah `jam_run`, jalankan `daftar_jadwal.bat` lagi.
- Bila PC mati atau tidur pada jam itu, tugas dijalankan **susulan** saat PC menyala, dan laporan tetap memakai
  tanggal data yang benar. Tidak pernah ada dua run bersamaan; batas waktu satu run
  `operasional.batas_waktu_run_jam`.
- Tugas hanya berjalan saat Anda login; kata sandi tidak disimpan.
- Keluaran setiap run terjadwal tercatat di `output\log\terjadwal.log`. Jangan tutup jendela hitam yang muncul.

**Opsional — bangunkan PC:** buka *Task Scheduler* → *Task Scheduler Library* → klik kanan **Asisten BEI Harian** →
*Properties* → tab **Conditions** → centang **"Wake the computer to run this task"** → OK. Pastikan *wake timers*
diizinkan di *Power Options* Windows.

**Opsional — pemicu kedua:** di *Properties* → tab **Triggers** → *New*, tambahkan jam pilihan Anda sendiri sebelum
pra-pembukaan. Run ini hanya memproses bila run untuk tanggal data terakhir belum `LENGKAP`.

Menjalankan `daftar_jadwal.bat` lagi menimpa tugas itu, sehingga kedua pengaturan opsional di atas perlu diulang.

Mode terjadwal (`harian --jadwal`): bila tabel `run` di jurnal sudah memuat run `LENGKAP` untuk tanggal data itu,
program keluar dengan "tidak ada data baru" (pesan Telegram hanya bila `jadwal.kirim_pesan_saat_libur: true`). Bila
belum, tanggal itu diproses walaupun hari ini akhir pekan atau libur (run susulan, dengan banner keterlambatan).
Bila data hari itu belum lengkap, program menunggu dan mencoba lagi (`data.tunggu_data_hari_ini`). Kegagalan
(kode 1, 2, 3, 5, 6) dikabarkan lewat Telegram.

---

## 6. Rutinitas harian

| Kapan | Yang dilakukan |
|---|---|
| Malam (setelah laporan ± 19:30) | Baca ringkasan aksi di Telegram atau `laporan.html`. Siapkan order untuk besok. |
| Pagi `t+1`, sebelum pra-pembukaan | Pasang order **BELI limit di harga_maks** sesuai laporan. Pasang order JUAL untuk saran "JUAL di pembukaan" dengan cara yang membuat order terisi di harga pembukaan (sesuai fitur broker Anda). |
| Segera setelah pembukaan | Cek setiap order beli. Tidak terisi → **tarik**. Terisi sebagian → tarik sisanya. Terisi → **pasang order otomatis stop (dan target untuk PB) hari itu juga**. Bila harga isi ≤ stop → jual segera. |
| Setelah ada transaksi | Perbarui `input\portfolio.csv` dan `input\akun.yaml` (contoh di bawah). |
| Setiap sore, posisi BO | Perbarui level order otomatis stop (trailing) sesuai laporan. |

**Contoh dengan angka fiktif:**

- **Beli terisi:** 1 lot `AAAA` di 2.010, strategi PB, stop 1.888.
  - Tambah baris di `portfolio.csv`: `AAAA,2026-10-16,2010,1,PB,1888,,,,`
  - Kurangi kas di `akun.yaml` sebesar 2.010 × 100 × 1,0015 = **201.301,50**, lalu ubah `diperbarui`.
  - Target = 2.010 + 2 × (2.010 − 1.888) = **2.254**.
- **Jual 1 lot di 2.254:**
  - Hapus baris dari `portfolio.csv`.
  - Tambah kas sebesar 225.400 × (1 − 0,0025) = **224.836,50**.

Bila broker membulatkan fee berbeda, pakai angka dari trade confirmation.

Saran per posisi di laporan:

- **TAHAN** — dengan level stop untuk besok, target, hari ke-N, dan P&L belum terealisasi.
- **JUAL di pembukaan** — beserta alasannya.
- **CEK ORDER** — order otomatis seharusnya sudah terisi hari ini; bila masih dipegang, jual di pembukaan.
- **INFO** — untuk posisi `MANUAL`.
- **TIDAK DAPAT DIEVALUASI** — data hari itu tidak ada, atau ada lonjakan harga tanpa data split. Lonjakan tanpa
  data split juga menahan semua rekomendasi beli sampai diperbaiki.

Laporan juga mengingatkan bila rekomendasi kemarin menurut data seharusnya terisi tetapi belum ada di
`portfolio.csv`.

---

## 7. Perintah dan berkas .bat

| Berkas | Perintah | Fungsi |
|---|---|---|
| `pasang.bat` | — | Membuat `.venv` dan memasang dependensi |
| `cek.bat` | `cek [--silang KODE TANGGAL]` | Validasi config + semua input (hanya membaca); `--silang` mencetak satu bar untuk dicocokkan dengan sumber resmi |
| `unduh.bat` | `unduh [--penuh] [--simbol K1,K2]` | Memperbarui cache harga |
| `backtest.bat` | `backtest [--strategi pb,bo,mr,acak] [--mode edge,realistis]` | Backtest dan gerbang |
| `harian.bat` | `harian [--offline] [--tanggal D] [--tanpa-notifikasi] [--catat] [--catat-ulang]` | Laporan harian (+ Telegram) |
| `harian_terjadwal.bat` | `harian --jadwal` | Dipanggil Task Scheduler |
| `jurnal.bat` | `jurnal [--ekspor] [--rekonsiliasi]` | Ringkasan jurnal kertas (paper trading), ekspor CSV, rekonsiliasi |
| `uji_telegram.bat` | `uji-telegram [--cari-chat-id]` | Pesan uji / mencari chat ID |
| `uji.bat` | `pytest -q` | Semua uji otomatis |
| `daftar_jadwal.bat` | — | Mendaftarkan tugas Task Scheduler |

Opsi bisa ditambahkan lewat Command Prompt di folder proyek, mis. `scripts\harian.bat --tanggal 2026-10-15`
(replay hari bursa itu; tidak mencatat ke jurnal kecuali `--catat`). Opsi global `--config PATH` dan `--akar DIR`
berlaku untuk semua perintah.

Laporan harian ada di `output\laporan\<tanggal>\` (`laporan.html`, `laporan.md`, `kandidat.csv`, `rencana.xlsx`,
`kualitas_data.csv`, sesuai `laporan.format`); setiap run juga tersimpan di subfolder `run_<jam>\`. Log harian ada
di `output\log\`.

---

## 8. Kode keluar dan masalah umum

| Kode | Arti |
|---|---|
| 0 | OK, termasuk "tidak ada data baru" dan `cek` yang hanya berisi peringatan |
| 1 | Error tak terduga (rincian di `output\log\`), atau pemeriksaan akuntansi backtest gagal |
| 2 | Data belum tersedia, atau tidak lengkap karena IHSG; backtest tanpa data |
| 3 | Config atau berkas input tidak valid (jalankan `cek.bat`); untuk `uji-telegram`: `.env` belum disiapkan |
| 4 | Laporan dibuat, tetapi notifikasi Telegram gagal (pada run harian tidak pernah muncul bila `notifikasi.telegram.aktif: false`); juga pesan uji `uji-telegram` yang gagal terkirim |
| 5 | Proses lain sedang berjalan |
| 6 | Jurnal gagal (terkunci/rusak); laporan tetap dibuat dan dikirim |

Bila beberapa kondisi terjadi bersamaan, kode yang dipakai mengikuti prioritas 3 > 1 > 5 > 2 > 6 > 4 > 0.

| Situasi | Yang terjadi / yang Anda lakukan |
|---|---|
| Internet putus, Yahoo membatasi | Data hari itu belum tersedia → kode 2, tanpa rekomendasi. Data lama tidak pernah dipakai seolah data hari itu. Coba lagi nanti, atau siapkan CSV cadangan di `data\manual\` (SPEC §6.9). |
| "mungkin hari libur yang belum ada di holidays.csv" | Tambahkan tanggal libur itu ke `input\holidays.csv`. |
| Strategi berstatus `BAYANGAN_GERBANG` | Jalankan `backtest.bat` (config/input berubah, hasil kedaluwarsa, atau gerbang tidak lolos; alasannya tertulis di laporan). |
| Muncul `rencana_<jam>.xlsx` | Berkas lama sedang dibuka di Excel; tutup Excel. |
| `jurnal.sqlite` terkunci | Tutup aplikasi yang membukanya; jangan letakkan proyek di OneDrive/Dropbox. |
| CSV ditolak karena `;` atau koma desimal | Berkas pernah disimpan Excel berlokal Indonesia; edit ulang dengan Notepad. |
| Run terjadwal tidak menghasilkan laporan | Baca `output\log\terjadwal.log`; bila `.venv` rusak, jalankan `pasang.bat` lagi. |

---

## 9. Uji cepat dengan data nyata

Setelah instalasi dan setiap upgrade dependensi (SPEC §22.3):

1. `cek.bat` → tidak ada ERROR. Setiap item "BELUM DIVERIFIKASI" sudah Anda putuskan, baik diisi maupun sengaja
   dibiarkan.
2. `unduh.bat` → periksa `output\kualitas\<waktu>_unduh.csv`. Jumlah simbol OK mendekati total; telusuri setiap
   anomali dan split yang belum disesuaikan.
3. **Cek silang:**
   - Pilih 3 saham yang **tidak mengalami split sejak tanggal uji**, dengan tanggal yang sudah ≥ 2 hari bursa
     berlalu: satu di awal periode backtest, satu di tengah, satu baru.
   - Jalankan `scripts\cek.bat --silang KODE TANGGAL`, lalu bandingkan dengan ringkasan perdagangan resmi
     (idx.co.id atau aplikasi broker). Harga harus sama.
   - Rasio volume menentukan `data.pengali_volume` (1 bila yfinance memakai lembar). Bila rasionya tidak sama di
     ketiga periode, **jangan** memakai satu pengali; itu masalah data.
   - Setelah itu setel `pengali_volume_terverifikasi: true`.
4. `backtest.bat` → ringkasan terbuka. ACAK tidak tampak menguntungkan. Jumlah transaksi per tahun masuk akal.
   Grafik IHSG sesuai pengetahuan umum (mis. penurunan tajam 2020).
5. **Hitung tangan** satu transaksi dari `transaksi_pb_edge.csv`: sinyal tampak di grafik; harga entry =
   min(open hari berikutnya × 1,001, harga_maks); exit sesuai aturan; fee benar.
6. `harian.bat` → baca laporan dan hitung ulang sizing satu kandidat dengan kalkulator (rumus SPEC §14.1).
7. `uji_telegram.bat` → pesan diterima.
8. Daftarkan jadwal. Pada hari bursa berikutnya ± 19:30, laporan dan pesan Telegram harus muncul. Opsional: matikan
   PC saat 19:30 → saat dinyalakan, tugas berjalan susulan dan laporan memakai tanggal data yang benar.

Sebelum memakai uang sungguhan (anjuran, tidak ditegakkan program): jalankan jurnal kertas **minimal 3 bulan atau
30 transaksi tertutup** (mana yang lebih lama), perhatikan peringatan penyimpangan dan subset terjangkau, dan
pastikan Anda memahami setiap transaksi kertas yang tercatat.

---

## 10. Glosarium

| Istilah | Penjelasan singkat |
|---|---|
| R | Kerugian yang direncanakan bila stop tersentuh. +2R berarti untung dua kali risiko awal; −1R berarti rugi sebesar rencana. |
| ATR | Rata-rata rentang gerak harian saham. Dipakai untuk menentukan jarak stop. |
| SMA20/50/200 | Rata-rata harga penutupan 20/50/200 hari bursa terakhir. |
| RSI(2) | Indikator jenuh jual/beli jangka sangat pendek (0–100). Nilai di bawah 10 berarti sangat jenuh jual. |
| Breadth | Persentase saham dalam daftar breadth yang harganya di atas SMA50. Menggambarkan seberapa luas pasar naik. |
| Drawdown | Penurunan dari nilai puncak ekuitas ke titik terendah setelahnya. |
| CAGR | Pertumbuhan tahunan rata-rata yang dimajemukkan. |
| Win rate | Persentase transaksi yang untung setelah fee. |
| Profit factor (PF) | Total untung ÷ total rugi. Di atas 1 berarti untung bersih. PF_R menghitungnya dalam satuan R. |
| Ekspektansi | Rata-rata hasil per transaksi, dalam Rupiah atau R. |
| In-sample / holdout | Periode yang dipakai untuk menilai aturan, dan periode terakhir yang disimpan untuk pengujian akhir. |
| Slippage | Selisih antara harga yang diharapkan dan harga eksekusi sebenarnya. |
| Fraksi harga | Kelipatan harga yang diizinkan bursa. Nilainya Anda isi sendiri dari aturan resmi. |
| ARA/ARB | Batas atas/bawah perubahan harga harian. Nilainya Anda isi sendiri dari aturan resmi. |
| Gerbang | Syarat backtest yang harus dipenuhi sebelum strategi boleh menghasilkan rekomendasi beli. |
| Bayangan | Sinyal yang dicatat untuk evaluasi tetapi tidak direkomendasikan (strategi belum lolos gerbang atau pasar JANGAN BELI). |
| Jurnal kertas | Simulasi transaksi tanpa uang sungguhan (paper trading), memakai aturan yang sama dengan backtest. |
| Stop waktu | Jual bila posisi belum keluar setelah N hari bursa. |
| Trailing stop | Stop yang naik mengikuti harga dan tidak pernah turun. |

---

> **BUKAN SARAN INVESTASI.** Laporan ini dibuat otomatis oleh program pribadi berdasarkan aturan mekanis dan data
> publik yang bisa salah, terlambat, atau tidak lengkap. Hasil backtest dan paper trading tidak menjamin hasil di
> masa depan. Seluruh keputusan dan risiko transaksi menjadi tanggung jawab pengguna.
