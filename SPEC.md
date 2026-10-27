# SPEC — Asisten Keputusan Saham BEI (`asisten-bei`)

> **BUKAN SARAN INVESTASI.** Program ini alat bantu keputusan untuk pemakaian pribadi. Program tidak terhubung ke broker dan tidak pernah mengirim order.
>
> Status: spesifikasi hasil wawancara (15 Oktober 2026), revisi 2 setelah tinjauan menyeluruh. Belum ada kode. Semua nama berkas, kolom, kunci config, enum, dan aturan di dokumen ini mengikat. Setiap perubahan dimulai dengan memperbarui dokumen ini.

---

## Daftar isi

1. [Tujuan dan prinsip](#1-tujuan-dan-prinsip)
2. [Ringkasan keputusan wawancara](#2-ringkasan-keputusan-wawancara)
3. [Konsekuensi modal Rp 3 juta](#3-konsekuensi-modal-rp-3-juta)
4. [Konvensi, glosarium, dan enum](#4-konvensi-glosarium-dan-enum)
5. [Struktur folder](#5-struktur-folder)
6. [Berkas input](#6-berkas-input)
7. [config.yaml](#7-configyaml)
8. [Lapisan data](#8-lapisan-data)
9. [Definisi indikator](#9-definisi-indikator)
10. [Filter kondisi pasar](#10-filter-kondisi-pasar)
11. [Universe dan eligibilitas](#11-universe-dan-eligibilitas)
12. [Aturan strategi](#12-aturan-strategi)
13. [Skor dan seleksi](#13-skor-dan-seleksi)
14. [Ukuran posisi dan rencana transaksi](#14-ukuran-posisi-dan-rencana-transaksi)
15. [Mesin exit dan saran posisi yang dipegang](#15-mesin-exit-dan-saran-posisi-yang-dipegang)
16. [Backtest](#16-backtest)
17. [Jurnal kertas (paper trading)](#17-jurnal-kertas-paper-trading)
18. [Laporan harian dan notifikasi](#18-laporan-harian-dan-notifikasi)
19. [Operasional di Windows](#19-operasional-di-windows)
20. [Penanganan kegagalan dan kode keluar](#20-penanganan-kegagalan-dan-kode-keluar)
21. [Di luar cakupan](#21-di-luar-cakupan)
22. [Verifikasi end-to-end](#22-verifikasi-end-to-end)
23. [Urutan implementasi](#23-urutan-implementasi)
24. [Yang harus Anda cek dan isi sendiri](#24-yang-harus-anda-cek-dan-isi-sendiri)
25. [Keputusan default yang tidak ditanyakan](#25-keputusan-default-yang-tidak-ditanyakan)

---

## 1. Tujuan dan prinsip

Setiap sore setelah bursa tutup, program menjawab tiga pertanyaan untuk hari bursa berikutnya:

1. **Kondisi pasar:** BOLEH BELI, HATI-HATI, atau JANGAN BELI, beserta alasannya.
2. **Kandidat beli:** saham yang layak dibeli, lengkap dengan rencana transaksi (harga masuk maksimal, stop loss, target, jumlah lot).
3. **Posisi yang dipegang:** tahan atau jual, dan di level berapa order otomatis stop/target dipasang.

| Kode | Prinsip | Akibat praktis |
|---|---|---|
| P1 | **Tidak mengarang data.** | Tidak ada forward-fill, interpolasi, atau nilai tebakan untuk OHLCV. Data yang hilang atau rusak membuat saham dikeluarkan, dan alasannya dilaporkan. Bar tanpa transaksi tidak pernah dipakai sebagai harga eksekusi. |
| P2 | **Satu mesin aturan.** | Backtest, jurnal kertas, dan laporan harian memanggil kode sinyal, sizing, seleksi, dan exit yang sama. Tidak ada salinan logika. |
| P3 | **Tanpa look-ahead.** | Keputusan pada hari `t` hanya memakai bar ≤ `t`. Eksekusi beli terjadi di pembukaan `t+1`. Aturan yang memakai data mana pun (termasuk anomali dan cakupan) dievaluasi per tanggal. |
| P4 | **Semua parameter di `config.yaml`.** | Aturan BEI (fraksi harga, ARA/ARB, jam sesi) tidak diisi dari ingatan. Selama belum diverifikasi, fitur yang bergantung padanya dinonaktifkan dan setiap laporan memuat peringatan. |
| P5 | **Ragu berarti jangan beli.** | Data tidak lengkap, gerbang backtest belum lolos, atau konfigurasi tidak valid → nol rekomendasi beli, dengan alasan tertulis. Saran exit tetap diberikan selama datanya ada. |
| P6 | **Bukan robot.** | Tidak ada koneksi API broker. Semua order dipasang dan dibatalkan manual oleh pengguna. |

---

## 2. Ringkasan keputusan wawancara

| Topik | Keputusan |
|---|---|
| Modal | Rp 3.000.000 |
| Gaya / lama menahan | Bebas per strategi. PB: stop waktu dievaluasi pada close hari ke-10, jual di pembukaan hari ke-11. MR: 2–5 hari (keluar SMA5 mulai close hari ke-2, stop waktu close hari ke-5). BO: selama trailing stop belum tersentuh. |
| Risiko per transaksi | 1% ekuitas (Rp 30.000 pada ekuitas Rp 3 juta). Sudah termasuk fee dan asumsi slippage di sisi stop. |
| Maks posisi | 3 posisi bersamaan, maks 1 per sektor |
| Alokasi maks per posisi | Ekuitas ÷ maks posisi (≈ Rp 1 juta) |
| 1 lot melebihi batas risiko | Kandidat dilewati dan ditampilkan sebagai `TIDAK_TERJANGKAU`. Backtest menerapkan aturan yang sama. |
| Fee broker | Beli 0,15%, jual 0,25%, tanpa biaya minimum |
| Order beli | Limit di **harga masuk maksimal** (`harga_maks`), dipasang di pra-pembukaan `t+1`. Jika tidak terisi di harga pembukaan, order **ditarik manual**. |
| Gap naik | Batal jika open `t+1` > `harga_maks` (= close `t` + 0,5 × ATR). Lot dihitung dari `harga_maks` sehingga kerugian di level stop tidak melebihi batas risiko. Hanya gap turun yang melewati stop bisa membuat kerugian melebihi batas. |
| Aturan keluar | Per strategi. Stop dan target dipasang sebagai order otomatis intraday di broker, segera setelah order beli terisi. Jika stop dan target tersentuh di hari yang sama, stop dianggap lebih dulu. |
| Strategi | PB (pullback dalam tren naik), BO (breakout dengan lonjakan volume), MR (oversold dalam tren naik) |
| Market breadth | Dari `breadth_universe.csv` terpisah yang lebih luas |
| Saat HATI-HATI | Risiko × 0,5, maks 1 entry baru per hari |
| Saat JANGAN BELI | Saran exit + daftar pantau. Sinyal dicatat sebagai bayangan. |
| Survivorship bias | `universe_history.csv` opsional. Tanpa berkas itu, setiap ringkasan backtest memuat peringatan tegas. |
| Periode backtest | Sejak 2015-01-01. Parameter tetap, jendela tahunan, holdout mulai 2024-10-01 (± 2 tahun terakhir). |
| Mode backtest | `edge`: modal Rp 1 miliar, **semua aturan lain identik**; dipakai untuk gerbang. `realistis`: modal Rp 3 juta. |
| Gerbang | Strategi yang gagal dinonaktifkan otomatis. Sinyalnya tetap dicatat di jurnal sebagai bayangan. |
| Sumber data | yfinance, dengan cadangan CSV manual. Cache lokal. Lapisan data bisa diganti. |
| Kalender bursa | `holidays.csv` diisi manual dari idx.co.id |
| Jurnal | Diperbarui otomatis dari data dengan aturan yang sama dengan backtest. SQLite + ekspor CSV. |
| Portofolio | `portfolio.csv` lengkap + `akun.yaml` (saldo kas). Risiko dihitung dari ekuitas. |
| OS dan jadwal | Windows 10/11, Task Scheduler pukul 19:30 WIB, dijalankan susulan bila terlewat |
| Notifikasi | Telegram bot (token di `.env`) |
| Format laporan | Terminal, Markdown, HTML + grafik, CSV, Excel |
| Pengguna | Python tingkat dasar. Semua perintah tersedia sebagai `.bat`; README berisi panduan langkah demi langkah. |

---

## 3. Konsekuensi modal Rp 3 juta

Ringkasan bagian ini juga wajib dimuat di README.

**Risiko per lot.** Lot dihitung dari `harga_maks` (P). Jarak stop `d` diukur dari P, bukan dari close, sehingga sudah termasuk tambahan +0,5 ATR. Dengan fee 0,15%/0,25% dan slippage 0,10%:

`risiko per lembar = P × (0,0049975 + 0,9965025 × d)`

| Jarak stop `d` (dari harga_maks) | Harga maks agar 1 lot ≤ Rp 30.000 | Saat HATI-HATI (Rp 15.000) | Nilai posisi maks pada risiko 1% |
|---|---|---|---|
| 3% | ≈ Rp 8.600 | ≈ Rp 4.300 | ≈ Rp 860 ribu |
| 5% | ≈ Rp 5.470 | ≈ Rp 2.740 | ≈ Rp 547 ribu |
| 8% | ≈ Rp 3.540 | ≈ Rp 1.770 | ≈ Rp 354 ribu |
| 10% | ≈ Rp 2.870 | ≈ Rp 1.430 | ≈ Rp 287 ribu |
| 12% | ≈ Rp 2.410 | ≈ Rp 1.200 | ≈ Rp 241 ribu |

**Jarak stop tipikal**, dengan `a = ATR/close`:

| Strategi | Jarak stop dari harga_maks | Pada a = 2–3% |
|---|---|---|
| BO | `2,5a/(1 + 0,5a)` | 4,95–7,39% |
| MR | `3a/(1 + 0,5a)` | 5,94–8,87% |
| PB | `(0,7 ATR + close − LLV5)/(close + 0,5 ATR)` | Paling bervariasi; contoh A §14.2: 6,99% |

**Akibatnya:**

- Batas alokasi Rp 1 juta hanya mengikat bila `d` < ± 2,5%. Saham di atas ≈ Rp 9.985/lembar tidak bisa dibeli walau 1 lot.
- Nilai posisi maksimal (sebelum pembulatan lot) sekitar Rp 240–860 ribu. Setelah lot dibulatkan ke bawah, nilainya sering lebih kecil (contoh §14.2: Rp 203 ribu dan Rp 229 ribu). Dengan 3 posisi, biasanya hanya ± 20–55% modal terpakai.
- Pembulatan lot ke bawah membuat risiko terealisasi sering jauh di bawah 1%. Contoh A di §14.2 hanya memakai 0,51%.
- Banyak konstituen LQ45 akan sering `TIDAK_TERJANGKAU`. Wajar bila berhari-hari tidak ada rekomendasi beli.
- Saat HATI-HATI, hampir semua kandidat tidak terjangkau.
- **Ilustrasi** (bukan prediksi): 24 transaksi/tahun × ekspektansi 0,2R × risiko terealisasi rata-rata 0,8% ≈ 3,8%/tahun ≈ Rp 115 ribu/tahun.
- Mode `edge` memakai aturan identik (3 slot, 1 per sektor). Karena slot sedikit, syarat gerbang "≥ 30 transaksi di holdout" bisa sulit tercapai, dan strategi tersebut akan tetap nonaktif. Ini konsekuensi yang disengaja.
- Gerbang mengukur keunggulan aturan di modal besar. Ringkasan backtest juga menampilkan kinerja **subset transaksi yang terjangkau modal Anda** (§16.7). Jika subset ini lemah, laporan harian memberi peringatan.

---

## 4. Konvensi, glosarium, dan enum

### 4.1 Konvensi teknis

| Istilah | Arti |
|---|---|
| `t` | Tanggal data: hari bursa terakhir yang barnya sudah dianggap final (§8.6). |
| `t+1` | Hari bursa berikutnya menurut kalender §8.7. Rencana transaksi berlaku untuk hari ini. |
| Bar | Satu baris data harian: `open, high, low, close, volume`. |
| Bar valid | Bar pada hari bursa yang lolos pemeriksaan per bar §8.5 **dan** volumenya > 0. |
| Bar tanpa transaksi | Bar pada hari bursa dengan volume = 0. Tidak dipakai untuk indikator dan tidak pernah menjadi harga eksekusi. |
| `x_{t−k}` | Nilai `x` pada bar valid ke-k sebelum `t` milik saham tersebut. |
| Hari ke-N | Dihitung dalam **hari bursa kalender** (§8.7) sejak hari entry. Hari entry = hari ke-1. |
| WIB | Semua waktu memakai zona `Asia/Jakarta`, terlepas dari pengaturan jam Windows. |
| `harga_ref` | Close `t`, sebagai perkiraan harga masuk. |
| `harga_maks` | `harga_ref + entry.gap_maks_atr × ATR_t`, dibulatkan menurut §4.1 "Pembulatan harga". |
| `stop_awal` | Stop loss saat sinyal dibuat. Bisa dinaikkan oleh trailing (BO), tidak pernah diturunkan. |
| `harga_beli` | Live: harga isi aktual. Simulasi: `min(open × (1 + slip), harga_maks)`. |
| `R_harga` | `harga_beli − stop_awal` (per lembar, tanpa fee). Dipakai untuk menghitung target. |
| `risiko_lembar` | `harga_beli × (1 + fb) − stop × (1 − slip) × (1 − fj)`. Saat sizing, `harga_beli` = `harga_maks`. |
| `risiko_awal_idr` | **Risiko rencana:** `lembar × (harga_maks × (1 + fb) − stop_awal × (1 − slip) × (1 − fj))`, ditetapkan saat sizing, tidak berubah, dan selalu > 0. Satuan **R** = P&L bersih ÷ `risiko_awal_idr`. Untuk baris `portfolio.csv` (tanpa harga_maks), `harga_beli` dipakai sebagai pengganti `harga_maks`; jika hasilnya ≤ 0, R ditampilkan "n/a". |
| Ekuitas | `kas + Σ nilai posisi pada close t` (§14.1). |
| `fb`, `fj`, `slip` | `fee_beli_pct/100`, `fee_jual_pct/100`, `slippage_pct/100`. |
| Kode saham | Huruf besar tanpa akhiran (`ABCD`). Simbol provider (`ABCD.JK`) dibentuk oleh provider. |
| Persen di config | Angka persen dengan titik desimal (`1.0` = 1%, `0.15` = 0,15%). |
| `null` pada batas | Tanpa batas. Rujukan ke bagian lain ditulis eksplisit: `ikuti_akun`, `ikuti_risiko`, `ikuti_maks_posisi`. |
| Aritmetika uang | Sizing, fee, dan P&L dihitung dengan `decimal.Decimal` dari nilai config, bukan float. Fee tidak dibulatkan kecuali `biaya.pembulatan_fee` diisi. |
| Tampilan Rupiah | 2 desimal dengan `ROUND_HALF_UP` (mis. 66.841,625 → 66.841,63). Hanya untuk tampilan; perhitungan memakai presisi penuh. |
| Pembulatan lot | Selalu ke bawah (`floor`). |
| Pembulatan harga | Arahnya selalu menjaga risiko ≤ anggaran: `harga_maks` dan target ke **bawah**; stop dan trailing ke **atas** (lebih dekat ke harga). Jika fraksi terverifikasi dan tanggal ≥ `berlaku_mulai`, pembulatan dilakukan ke **harga valid terdekat** pada arah itu (harga valid = kelipatan fraksi rentang tempat harga itu berada; bila perlu melintasi batas rentang). Jika belum, ke rupiah bulat, dan laporan meminta Anda membulatkan ke fraksi valid dengan arah yang sama. Sizing, `STOP_TERLALU_JAUH`, dan skor dihitung **setelah** pembulatan. |
| Transaksi | Satu siklus beli lalu jual. Kata "trade" tidak dipakai dalam teks laporan. |
| Nama berkas | `portfolio.csv` tetap memakai ejaan Inggris. "Portofolio" adalah istilah dalam teks. |

### 4.2 Glosarium untuk pembaca laporan

Glosarium ini juga dimuat sebagai lampiran di laporan HTML dan di README.

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

### 4.3 Enum kanonik

Semua bagian, tabel jurnal, dan berkas CSV **hanya** memakai nilai-nilai berikut.

| Enum | Nilai |
|---|---|
| `status_pasar` | `BOLEH_BELI`, `HATI_HATI`, `JANGAN_BELI` (laporan menampilkannya dengan spasi) |
| `status_data_run` | `LENGKAP`, `TIDAK_LENGKAP`, `BELUM_TERSEDIA` |
| `status_sinyal` | `REKOMENDASI`, `SUDAH_DIPEGANG`, `DIBLOKIR_PORTOFOLIO` (hanya live), `SLOT_PENUH`, `BATAS_ENTRY_HARIAN`, `BATAS_SEKTOR`, `TIDAK_TERJANGKAU`, `TIDAK_LIKUID`, `KAS_KURANG`, `BAYANGAN_GERBANG`, `BAYANGAN_PASAR`, `STOP_TERLALU_JAUH`, `DUPLIKAT_STRATEGI` |
| `status_isi` | `MENUNGGU` (bar `t+1` belum tersedia), `TERISI`, `BATAL_GAP`, `BATAL_DATA`, `BATAL_ARA`, `BATAL_SLOT_SAAT_OPEN`, `BATAL_SEKTOR_SAAT_OPEN`, `BATAL_KAS_SAAT_OPEN` |
| `alasan_exit` | `STOP`, `STOP_GAP`, `STOP_GAP_HARI_ENTRY`, `TRAILING`, `TRAILING_GAP`, `TARGET`, `TARGET_GAP`, `KELUAR_SMA`, `STOP_WAKTU`, `PAKSA_JUAL_PASAR`, `DATA_BERAKHIR`, `TERBUKA` |
| `tanda_transaksi` (boleh lebih dari satu) | `ANOMALI`, `TERTUNDA_ARB`, `TERTUNDA_TANPA_TRANSAKSI`, `SUMBER_MANUAL`, `MELEWATI_HOLDOUT` |
| `status_ambil` | `OK`, `SEBAGIAN`, `GAGAL` |
| `jenis_error_ambil` | `BATAS_PERMINTAAN`, `TIDAK_ADA_DATA`, `FORMAT`, `JARINGAN` |
| `strategi` | `PB`, `BO`, `MR`, `ACAK` (khusus backtest), `MANUAL` (khusus `portfolio.csv`). Di nama berkas dan opsi CLI ditulis huruf kecil (`pb`, `bo`, `mr`, `acak`). |
| `run_backtest` | `pb`, `bo`, `mr`, `acak`, `gabungan_aktif`, `gabungan_lolos` |
| `mode` | `edge`, `realistis` |
| `tanda_bar` | `BAR_HARI_NONBURSA`, `BAR_RUSAK`, `TANPA_TRANSAKSI` |
| `alasan_dikeluarkan` | `GAGAL_AMBIL`, `DATA_BASI` (tanpa bar `t`), `BAR_HILANG`, `ANOMALI`, `SPLIT_BELUM_DISESUAIKAN`, `RIWAYAT_PENDEK`, `RIWAYAT_CSV_PENDEK`, `KEMUNGKINAN_SUSPENSI`, `TIDAK_LIKUID_RATA2`, `DIKECUALIKAN`, `BUKAN_KONSTITUEN` |
| `saran_posisi` | `TAHAN`, `JUAL`, `CEK_ORDER`, `TIDAK_DAPAT_DIEVALUASI`, `INFO_MANUAL` |
| Sektor khusus | `TIDAK_DIKETAHUI:<KODE>` (§6.3) |

Exit BO pada level trailing (termasuk trailing awal = `stop_awal`) berlabel `TRAILING`. PB, MR, dan ACAK memakai `STOP`.

---

## 5. Struktur folder

```
asisten-bei/
├── README.md                    # Panduan langkah demi langkah (Windows) + glosarium + rutinitas harian
├── SPEC.md
├── config.yaml                  # Semua parameter (§7); milik Anda setelah diisi (§24)
├── .env.example                 # TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
├── .env                         # Rahasia Telegram (tidak dibagikan/di-commit)
├── .gitignore                   # .env, .venv/, data/, output/
├── requirements.txt             # Versi dependensi dipin (§19.1)
├── pyproject.toml
│
├── input/                       # Berkas input utama yang Anda isi (§6)
│   ├── universe.csv             # dirilis berisi baris judul saja
│   ├── breadth_universe.csv     # dirilis berisi baris judul saja
│   ├── universe_history.csv     # opsional; tidak dirilis (berkas kosong = backtest tanpa konstituen)
│   ├── exclude.csv              # dirilis berisi baris judul saja
│   ├── portfolio.csv            # dirilis berisi baris judul saja
│   ├── akun.yaml                # tidak dirilis (Anda buat sendiri)
│   ├── holidays.csv             # dirilis berisi baris judul saja
│   └── anomali_ok.csv           # opsional; tidak dirilis
│
├── contoh/                      # Contoh terisi untuk §24; TIDAK PERNAH dibaca program
│   ├── PANDUAN_ISI.md           # letak, format, sumber, dan langkah setiap item §24
│   ├── input/                   # contoh terisi setiap berkas input/ (kode & harga fiktif, libur bertahun 2099)
│   ├── config_bei_contoh.yaml   # bentuk isian pasar.jadwal_sesi/fraksi_harga/auto_reject, biaya, data, backtest
│   │                            #   (semua aturan BEI FIKTIF, terverifikasi false, berlaku_mulai 2099)
│   ├── cek_fitur_broker.md      # daftar pertanyaan item 11
│   └── env.contoh               # bentuk isian .env (nilai fiktif)
│
├── data/                        # Dikelola program, kecuali data/manual/
│   ├── cache/
│   │   └── yfinance/            # <KODE>.parquet + <KODE>.meta.json (csv_manual tidak di-cache, §8.2)
│   ├── manual/                  # CSV OHLCV cadangan yang Anda siapkan sendiri (§6.9)
│   ├── jurnal.sqlite
│   ├── .kunci                   # berkas kunci proses (§19.3)
│   └── .kunci.info              # PID, perintah, waktu mulai pemegang kunci
│
├── output/
│   ├── laporan/YYYY-MM-DD/      # laporan.md/.html, kandidat.csv, rencana.xlsx, kualitas_data.csv (+ run_HHMMSS/)
│   ├── backtest/
│   │   ├── <YYYYMMDD-HHMMSS>_<hash8>/   # satu folder per run backtest
│   │   ├── gate_status.json     # status gerbang terbaru per strategi (dibaca run harian)
│   │   └── holdout_log.csv
│   ├── kualitas/                # <YYYYMMDD-HHMMSS>_<perintah>.csv dari unduh/backtest
│   ├── jurnal/                  # ekspor CSV jurnal
│   └── log/                     # YYYY-MM-DD.log + terjadwal.log
│
├── scripts/                     # Pintasan klik ganda untuk pengguna tingkat dasar
│   ├── pasang.bat               # buat .venv + pasang dependensi
│   ├── cek.bat
│   ├── unduh.bat
│   ├── backtest.bat
│   ├── harian.bat
│   ├── harian_terjadwal.bat     # dipanggil Task Scheduler (tanpa pause, log ke terjadwal.log)
│   ├── jurnal.bat
│   ├── uji_telegram.bat
│   ├── uji.bat                  # menjalankan pytest
│   ├── daftar_jadwal.bat        # memanggil daftar_jadwal.ps1 dengan -ExecutionPolicy Bypass
│   └── daftar_jadwal.ps1
│
├── src/asisten_bei/
│   ├── __init__.py              # __version__ = versi_program (sumber tunggal, dari pyproject)
│   ├── __main__.py              # CLI (argparse), §19.2; langkah pertama: pasang penyaring rahasia (§19.5)
│   ├── harian.py                # orkestrasi run harian: tunggu data, susulan, laporan, kode keluar
│   ├── cek.py                   # perintah cek (+ --silang, perbandingan kalender)
│   ├── unduh.py                 # perintah unduh
│   ├── config.py                # memuat, memvalidasi, dan meresolusi config (pydantic; kunci tak dikenal = error)
│   ├── inputs.py                # memuat & memvalidasi berkas input/
│   ├── waktu.py                 # "sekarang" (bisa diinjeksi untuk uji, §22.1)
│   ├── kalender.py              # hari bursa, t, t+1, keterlambatan
│   ├── aturan_bei.py            # fraksi & ARA/ARB dari config (nonaktif jika belum diverifikasi)
│   ├── biaya.py                 # fee, slippage, fee minimum (Decimal)
│   ├── data/
│   │   ├── provider.py          # antarmuka PriceProvider + HasilAmbil
│   │   ├── yfinance_provider.py
│   │   ├── csv_provider.py
│   │   ├── cache.py             # parquet + meta, inkremental, revisi, tulis atomik
│   │   ├── split.py             # faktor split F(d), konversi nominal ↔ tersesuaikan
│   │   ├── kualitas.py          # validasi & laporan kualitas
│   │   ├── sumber.py            # kebijakan coba ulang (§8.1), pemilihan sumber per simbol (§8.2), muat pasar dari cache
│   │   └── tampilan.py          # DataView(as_of=d)
│   ├── indikator.py
│   ├── kondisi_pasar.py
│   ├── universe.py
│   ├── strategi/
│   │   ├── dasar.py
│   │   ├── pullback.py          # PB
│   │   ├── breakout.py          # BO
│   │   ├── oversold.py          # MR
│   │   └── acak.py              # ACAK (pembanding acak, khusus backtest)
│   ├── skor.py
│   ├── sizing.py
│   ├── seleksi.py
│   ├── exit.py                  # mesin exit bersama
│   ├── portofolio.py            # baca portfolio.csv/akun.yaml, konversi split, saran posisi
│   ├── backtest/
│   │   ├── mesin.py             # keputusan harian, simulasi run × mode, jalur per sinyal
│   │   ├── metrik.py
│   │   ├── jendela.py
│   │   ├── gerbang.py           # G1–G6, hash, gate_status.json, holdout_log.csv, subset terjangkau
│   │   ├── laporan_bt.py
│   │   └── jalankan.py          # perintah backtest: muat data, run, gerbang, tulis keluaran
│   ├── jurnal.py                # termasuk --rekonsiliasi
│   ├── laporan/
│   │   ├── terminal.py          # rich
│   │   ├── markdown.py
│   │   ├── html.py              # jinja2 + grafik PNG base64 (satu berkas mandiri)
│   │   ├── tabel.py             # CSV + XLSX
│   │   ├── grafik.py            # matplotlib / mplfinance
│   │   └── templates/
│   ├── notifikasi/telegram.py
│   └── operasional.py           # kunci proses (filelock), tulis atomik, penyaring rahasia
│
└── tests/
    ├── fixtures/
    │   ├── buat_fixture.py      # pembangkit data sintetis deterministik (§22.2)
    │   ├── config_rilis.yaml    # salinan rilis config.yaml (nilai = blok §7); dasar semua uji
    │   ├── config_uji.yaml
    │   ├── config_uji_ujibo.yaml # sama, tetapi file_portfolio → input/portfolio_ujibo.csv
    │   ├── pasar_sintetis/      # hasil pembangkit dalam format cache §8.3
    │   ├── input_uji/           # termasuk portfolio.csv (kosong) dan portfolio_ujibo.csv
    │   └── acuan/               # keluaran acuan (golden) yang diharapkan
    ├── test_config.py   test_indikator.py   test_tanpa_lookahead.py   test_kalender.py
    ├── test_kualitas_data.py   test_cache.py   test_split.py   test_aturan_bei.py
    ├── test_kondisi_pasar.py   test_universe.py   test_strategi.py   test_skor_seleksi.py
    ├── test_sizing.py   test_biaya.py   test_exit.py   test_portofolio.py
    ├── test_mesin_backtest.py   test_metrik.py   test_gerbang.py   test_jurnal.py
    ├── test_laporan.py   test_telegram.py   test_operasional.py   test_inputs.py
    └── test_e2e.py
```

---

## 6. Berkas input

**Aturan umum untuk semua CSV:**

- Dibaca dengan `encoding="utf-8-sig"`, sehingga BOM dari Excel diterima.
- Pemisah koma, header di baris pertama.
- Angka memakai titik desimal tanpa pemisah ribuan. Pemisah ribuan bertitik dengan dua kelompok atau lebih (`1.000.000`) adalah ERROR. Satu kelompok (`2.850`) tidak bisa dibedakan dari angka desimal yang sah, jadi dibaca sebagai 2,85 dan diberi PERINGATAN pada `harga_beli`, `stop_awal`, dan `kas_idr` ("bila maksudnya ribuan, tulis tanpa titik").
- Tanggal **hanya** `YYYY-MM-DD`. Format lain adalah ERROR dan tidak pernah ditebak.
- Kode saham huruf besar tanpa `.JK`.
- Jika terdeteksi pemisah `;`, koma desimal, atau format tanggal lain, `cek` menolak berkas dengan pesan khusus: "Berkas tampaknya disimpan Excel dengan format regional Indonesia. Simpan ulang sebagai CSV UTF-8 (koma) atau edit dengan Notepad." README menganjurkan mengedit berkas input dengan Notepad dan menyertakan contoh siap pakai.
- Contoh di bawah memakai **kode dan harga fiktif**, hanya untuk menunjukkan format.
- Berkas wajib dirilis di `input/` berisi baris judul saja; contoh terisi ada di `contoh/input/` (§5). Berkas CSV input tidak mendukung baris komentar, jadi penjelasan ada di `contoh/PANDUAN_ISI.md`.

### 6.1 `input/universe.csv` — saham yang boleh direkomendasikan

```csv
kode,nama,sektor
AAAA,Contoh Satu Tbk,Keuangan
BBBB,Contoh Dua Tbk,Infrastruktur
```

- `sektor` wajib, karena berlaku batas 1 posisi per sektor. Penamaan bebas tetapi harus konsisten; disarankan memakai klasifikasi sektor resmi IDX.
- Kode duplikat adalah ERROR.

### 6.2 `input/breadth_universe.csv`

```csv
kode
AAAA
CCCC
```

Disarankan 100–200 saham likuid. Boleh tumpang tindih dengan `universe.csv`.

### 6.3 `input/universe_history.csv` — opsional

```csv
kode,tanggal_masuk,tanggal_keluar
AAAA,2015-01-01,
DDDD,2016-02-01,2019-07-31
```

- Saham eligible pada `d` jika `tanggal_masuk ≤ d ≤ tanggal_keluar`, atau `tanggal_keluar` kosong. Kedua batas inklusif.
- Satu kode boleh punya beberapa baris (keluar lalu masuk lagi).
- Jika berkas ada, backtest memakainya **sebagai pengganti** `universe.csv`.
- Sektor diambil dari `universe.csv`. Jika kode tidak ada di sana, sektornya `TIDAK_DIKETAHUI:<KODE>`, yaitu satu sektor tersendiri per kode.

### 6.4 `input/exclude.csv`

```csv
kode,alasan,tanggal_mulai,tanggal_akhir
EEEE,Suspensi,2026-10-01,
FFFF,Notasi khusus,,
```

- Tanggal kosong berarti tanpa batas; kedua batas inklusif.
- Berlaku untuk laporan harian dan jurnal kertas.
- Backtest mengabaikan berkas ini kecuali `backtest.pakai_exclude: true`.

### 6.5 `input/portfolio.csv` — posisi yang sedang dipegang

```csv
kode,tanggal_beli,harga_beli,lot,strategi,stop_awal,sektor,harga_per_tanggal,lembar,catatan
ABCD,2026-10-08,2850,2,PB,2720,,,,
```

| Kolom | Wajib | Aturan |
|---|---|---|
| `kode` | ya | Harus punya data. Boleh di luar `universe.csv`. |
| `tanggal_beli` | ya | Hari bursa ≤ `t`. |
| `harga_beli` | ya | Harga rata-rata per lembar sesuai trade confirmation, tanpa fee. |
| `lot` | ya | Bilangan bulat ≥ 1. |
| `strategi` | ya | `PB`, `BO`, `MR`, atau `MANUAL`. |
| `stop_awal` | ya | Isi dengan stop dari laporan, setelah Anda bulatkan ke fraksi. Biasanya < `harga_beli`. Jika ≥ `harga_beli` (terisi saat harga gap di bawah stop), `cek` memberi peringatan (bukan ERROR) dan saran posisinya JUAL. |
| `sektor` | jika kode tidak ada di `universe.csv` | `cek` memberi ERROR jika kosong. |
| `harga_per_tanggal` | tidak | Tanggal acuan nilai harga dan lot di baris ini. Kosong = `tanggal_beli`. Lihat §15.3 tentang aksi korporasi. |
| `lembar` | tidak | Isi hanya jika jumlah saham bukan kelipatan lot (mis. setelah aksi korporasi). Jika diisi, menggantikan `lot × lot_lembar`. |
| `catatan` | tidak | Bebas. |

- Satu kode = satu baris. Penambahan posisi digabung sendiri menjadi harga rata-rata.
- **Semua** baris, termasuk `MANUAL` dan saham di luar universe, dihitung ke slot posisi dan batas sektor.
- Contoh di atas: risiko = 200 × (2.850 × 1,0015 − 2.720 × 0,999 × 0,9975) = **Rp 28.757,64** (0,96% dari Rp 3 juta).

### 6.6 `input/akun.yaml` — saldo kas

```yaml
kas_idr: 2429145          # = 3.000.000 − 200 × 2.850 × 1,0015 (contoh setelah membeli posisi di §6.5)
diperbarui: 2026-10-15
```

- Perbarui setiap ada transaksi atau setoran (lihat rutinitas §19.6).
- Laporan memberi peringatan jika `diperbarui` lebih tua dari `akun.maks_umur_hari_bursa` hari bursa.
- Jika berkas tidak ada, `akun.modal_awal_idr` dipakai sebagai kas dengan peringatan mencolok.

### 6.7 `input/holidays.csv` — libur bursa

```csv
tanggal,keterangan
2026-12-25,Contoh format — cocokkan dengan kalender resmi
```

- Diisi dari kalender libur bursa resmi di idx.co.id, termasuk cuti bersama yang ditetapkan sebagai libur bursa.
- `cek` dan setiap laporan memberi peringatan jika tidak ada satu pun tanggal untuk **tahun berjalan atau tahun berikutnya**. Sejak Desember, peringatan untuk tahun berikutnya tampil sebagai banner.
- Jika `t+1` jatuh pada tahun tanpa entri, rencana transaksi diberi banner "Tanggal t+1 belum terverifikasi terhadap kalender libur resmi".

### 6.8 `input/anomali_ok.csv` — opsional

```csv
kode,tanggal,catatan
ABCD,2021-10-13,Lonjakan sudah dicek ke sumber resmi; data benar
```

Anomali data (§8.5) yang sudah Anda periksa dan nyatakan benar. Untuk IHSG, gunakan kode `IHSG`.

### 6.9 `data/manual/` — data cadangan

**Format `data/manual/<KODE>.csv`:**

```csv
tanggal,open,high,low,close,volume
2026-10-14,2850,2890,2830,2880,45231000
```

- Harga dan volume harus **disesuaikan untuk split** dengan konvensi yang sama seperti cache (§8.4). Volume dalam lembar.
- IHSG disimpan sebagai `IHSG.csv`; kolom volume opsional.
- Berkas opsional `data/manual/<KODE>_split.csv` (`tanggal,rasio`) mencatat split. Tanpa berkas ini, faktor split dianggap 1 dan laporan memberi peringatan.

**Dua cara pakai (§8.2):**

1. **Pengganti penuh:** riwayat sejak `data.mulai_unduh`, minimal `universe.min_bar_riwayat` bar. Jika lebih pendek, saham tidak dinilai dengan alasan "riwayat CSV manual terlalu pendek".
2. **Sambungan:** hanya bar terbaru setelah bar terakhir cache, ditambah minimal `data.min_bar_tumpang_tindih_sambungan` bar tumpang tindih yang OHLC-nya cocok dengan cache dalam `toleransi_revisi_pct`. Tanpa tumpang tindih yang cocok, sambungan ditolak.

---

## 7. `config.yaml`

Isi lengkap dengan nilai default. Salinan rilis ini disimpan di `tests/fixtures/config_rilis.yaml` (wajib identik nilainya dengan blok di bawah, dan dipakai semua uji). `config.yaml` di folder utama adalah config **Anda**: dirilis dengan nilai yang sama, lalu Anda isi (§24); uji tidak pernah membacanya. Semua kunci wajib ada, dan kunci yang tidak dikenal adalah error. Kunci ganda pada tingkat mana pun (mis. blok yang ditempel dua kali) adalah error; PyYAML biasa diam-diam memakai yang terakhir. Validasi tambahan: `pasar.jadwal_sesi`, `pasar.fraksi_harga`, dan `pasar.auto_reject` dengan `terverifikasi: true` tidak boleh ber-`sumber` yang masih memuat teks contoh "CONTOH FIKTIF" (dari `contoh/config_bei_contoh.yaml`); `skor.bobot` berjumlah 100 dan semuanya ≥ 0; tabel fraksi dan ARA/ARB tidak boleh tumpang tindih atau berlubang (harga di bawah `harga_min` baris pertama memakai baris pertama); persen berada di rentang wajar.

```yaml
versi_config: 1                        # harus 1; nilai lain = error "config dari versi program lain"

akun:
  modal_awal_idr: 3000000              # default modal mode 'realistis' (ikuti_akun) & jurnal kertas
  file_akun: input/akun.yaml
  maks_umur_hari_bursa: 5

risiko:
  risiko_per_transaksi_pct: 1.0        # % ekuitas; sudah termasuk fee & slippage sisi stop
  maks_posisi: 3
  alokasi_maks_pct: ikuti_maks_posisi  # = ekuitas ÷ maks_posisi milik konteks yang berjalan; atau angka persen
  maks_per_sektor: 1                   # null = tanpa batas
  maks_entry_baru_per_hari: null       # null = hanya dibatasi slot
  izinkan_satu_lot_melebihi_risiko: false
  dana_jual_langsung_dipakai: false    # true = estimasi hasil jual di open t+1 boleh membiayai beli di open t+1
  slot_bebas_dari_jual_terjadwal: true # posisi yang dijadwalkan jual di open t+1 tidak menempati slot
  hati_hati:
    pengali_risiko: 0.5
    maks_entry_baru_per_hari: 1

biaya:
  fee_beli_pct: 0.15
  fee_jual_pct: 0.25                   # termasuk pajak penjualan; cocokkan dengan trade confirmation
  fee_minimum_idr: 0                   # per transaksi beli atau jual; 0 = tidak ada
  pembulatan_fee: null                 # null = tidak dibulatkan; "rupiah_terdekat" (ROUND_HALF_UP) / "rupiah_ke_atas"
  slippage_pct: 0.10                   # per sisi; dipakai di sizing (sisi stop), backtest, dan jurnal
  terverifikasi: false                 # ubah ke true setelah dicocokkan dengan trade confirmation

entry:
  gap_maks_atr: 0.5                    # harga_maks = close_t + 0.5 × ATR_t
  stop_aktif_hari_entry: true          # false jika Anda tidak bisa memasang stop di hari pembelian

jadwal:
  jam_run: "19:30"                     # keputusan Anda; dipakai Task Scheduler & sebagai batas bar final (§8.6)
  kirim_pesan_saat_libur: false

pasar:
  lot_lembar: 100
  file_libur: input/holidays.csv

  # ======================================================================
  # ATURAN & JADWAL BEI — WAJIB DICEK DI idx.co.id. JANGAN DIISI DARI INGATAN.
  # Aturan perdagangan banyak berubah pada 2026. Selama 'terverifikasi: false':
  #   - jadwal_sesi : bar hari ini baru dianggap final setelah jadwal.jam_run; setiap run
  #                   pada tanggal t+1 diberi banner untuk memastikan masih sebelum pra-pembukaan;
  #   - fraksi_harga: harga dibulatkan ke rupiah bulat saja (arah aman, §4.1);
  #   - auto_reject : ARA/ARB tidak dimodelkan di backtest/jurnal;
  #   - setiap laporan menampilkan peringatan.
  # ======================================================================
  jadwal_sesi:
    terverifikasi: false
    sumber: ""                         # judul + URL halaman idx.co.id yang Anda baca
    tanggal_cek: null                  # YYYY-MM-DD
    jam_penutupan: null                # "HH:MM" WIB, hari bursa biasa
    jam_pra_pembukaan: null            # "HH:MM" WIB, awal sesi pra-pembukaan
  fraksi_harga:
    terverifikasi: false
    sumber: ""
    tanggal_cek: null
    berlaku_mulai: null                # YYYY-MM-DD; sebelum tanggal ini tidak ada pembulatan ke fraksi
    tabel: []
    # Format per baris (harga_maks null = tak terbatas):
    #   - {harga_min: <angka>, harga_maks: <angka|null>, fraksi: <angka>}
  auto_reject:
    terverifikasi: false
    sumber: ""
    tanggal_cek: null
    berlaku_mulai: null                # YYYY-MM-DD; ARA/ARB hanya dimodelkan untuk tanggal ≥ ini
    papan_berlaku: ""                  # informasi saja; ditampilkan di laporan bersama sumber
    harga_acuan: null                  # isi dari peraturan, mis. "close_kemarin" jika memang demikian
    ara: []                            # - {harga_min: <angka>, harga_maks: <angka|null>, jenis: pct|abs, batas: <angka>}
    arb: []                            # - {harga_min: <angka>, harga_maks: <angka|null>, jenis: pct|abs, batas: <angka>}
    # jenis pct: batas = persen dari harga acuan; jenis abs: batas = rupiah dari harga acuan (mis. ±1).
    # Jika aturan memakai satu persentase untuk semua harga: satu baris, harga_min 0, harga_maks null.

data:
  provider_urutan: [yfinance, csv_manual]
  folder_cache: data/cache
  folder_manual: data/manual
  mulai_unduh: "2013-01-01"
  akhiran_yfinance: ".JK"
  simbol_ihsg: {yfinance: "^JKSE", csv_manual: "IHSG"}
  pengali_volume: 1                    # volume harus dalam lembar; cek sekali (§22.3)
  pengali_volume_terverifikasi: false
  jeda_data_final_menit: 60            # dipakai hanya jika jadwal_sesi terverifikasi
  jeda_antar_permintaan_detik: 0.5
  jeda_batas_permintaan_detik: 60
  maks_simbol_kena_batas_berturut: 3
  coba_lagi_tidak_ada_data_hari: 7
  tunggu_data_hari_ini: {jumlah: 3, jeda_menit: 15}
  bar_cek_ulang: 10
  toleransi_revisi_pct: 0.5
  toleransi_revisi_volume_pct: 5.0
  min_bar_tumpang_tindih_sambungan: 5
  file_anomali_ok: input/anomali_ok.csv
  validasi:
    lonjakan_mencurigakan_pct: 40      # setelah ARA/ARB diverifikasi, pastikan di atas batas ARA pct tertinggi
    jendela_kelengkapan_bar: 60
    maks_bar_hilang: 2
    min_cakupan_universe_pct: 80
    min_hari_kalender_historis_pct: 50

likuiditas:
  periode_hari: 20
  nilai_transaksi_min_idr: 10000000000 # rata-rata (close × volume) 20 bar ≥ Rp 10 miliar
  maks_hari_volume_nol: 0
  maks_pct_order_dari_nilai_harian: 1.0

universe:
  file: input/universe.csv
  file_riwayat: input/universe_history.csv
  file_exclude: input/exclude.csv
  file_portfolio: input/portfolio.csv
  min_bar_riwayat: 250

indikator:
  atr_periode: 14

kondisi_pasar:
  sma_pendek: 50
  sma_panjang: 200
  breadth_sma: 50
  breadth_boleh_min_pct: 50
  breadth_jangan_maks_pct: 30
  min_cakupan_breadth_pct: 80
  min_saham_breadth: 30                # jumlah absolut minimum saham yang dihitung
  file_breadth_universe: input/breadth_universe.csv
  jangan_beli_paksa_jual: false

strategi:
  pb:
    aktif: true
    sma_cepat: 20
    sma_tren: 50
    sma_panjang: 200
    lookback_slope_sma_tren: 10
    lookback_sentuh_sma_cepat: 4
    maks_jarak_atas_sma_cepat_atr: 1.0
    lookback_swing_low: 5
    penyangga_stop_atr: 0.2
    maks_jarak_stop_atr: 3.0
    target_r: 2.0
    stop_waktu_hari: 10
  bo:
    aktif: true
    lookback_tertinggi: 20
    lookback_volume: 20
    min_rasio_volume: 1.5
    min_posisi_close: 0.6
    maks_lewat_level_atr: 1.0
    sma_tren: 50
    sma_panjang: 200
    stop_atr: 2.0
    trailing_atr: 3.0
    stop_waktu_hari: null              # null = tanpa stop waktu
  mr:
    aktif: true
    rsi_periode: 2
    rsi_maks: 10
    sma_tren: 200
    maks_turun_harian_atr: 3.0
    stop_atr: 2.5
    sma_keluar: 5
    min_hari_tahan: 2
    stop_waktu_hari: 5
  acak:                                # pembanding acak; tidak pernah di laporan harian atau run gabungan
    peluang_sinyal_pct: 2.0
    seed: 12345
    stop_atr: 2.0
    target_r: 2.0
    stop_waktu_hari: 10

skor:
  bobot: {sinyal: 40, kekuatan_relatif: 30, likuiditas: 15, jarak_stop: 15}
  rs_lookback: 60
  jarak_stop_ideal_pct: 3.0
  jarak_stop_buruk_pct: 10.0

backtest:
  mulai: "2015-01-01"
  selesai: null                        # null = bar terakhir yang tersedia
  holdout_mulai: "2024-10-01"          # TETAPKAN SEKALI. Jangan digeser setelah melihat hasil.
  pakai_exclude: false
  maks_hari_tanpa_bar: 60              # §15.1 "Seri berhenti"; juga dipakai jurnal kertas
  potongan_data_berakhir_pct: 50       # §15.1, asumsi konservatif bila seri saham berhenti
  maks_pf_r_acak: 1.3
  mode:
    edge:
      modal_idr: 1000000000
      maks_posisi: ikuti_risiko
      maks_per_sektor: ikuti_risiko
      alokasi_maks_pct: ikuti_risiko
    realistis:
      modal_idr: ikuti_akun
      maks_posisi: ikuti_risiko
      maks_per_sektor: ikuti_risiko
      alokasi_maks_pct: ikuti_risiko

gerbang:                               # selalu dinilai pada mode 'edge'
  min_transaksi_holdout: 30
  min_pf_r_holdout: 1.2
  min_ekspektansi_r_holdout: 0.0       # harus > nilai ini
  min_pf_r_insample: 1.1
  min_transaksi_per_jendela: 5
  min_pct_jendela_pf_di_atas_1: 50
  maks_drawdown_r: 15
  kedaluwarsa_hari: 90

laporan:
  folder: output/laporan
  format: [terminal, markdown, html, csv, xlsx]
  maks_kandidat_ditampilkan: 10
  maks_baris_dikeluarkan: 50
  bar_grafik: 120

notifikasi:
  telegram:
    aktif: true                        # false = tidak ada pengiriman; kode keluar 4 tidak pernah muncul
    lampirkan_html: true
    maks_lampiran_mb: 45               # token & chat id di .env, BUKAN di sini

jurnal:
  file: data/jurnal.sqlite
  folder_ekspor: output/jurnal
  tanggal_mulai_kertas: null           # awal periode kertas pertama; null = tanggal run harian pertama
  maks_hari_tunggu_data: 3
  min_transaksi_peringatan: 20
  persentil_peringatan: 5
  bootstrap_iterasi: 10000
  bootstrap_seed: 2026

operasional:
  simpan_log_hari: 90
  batas_waktu_run_jam: 2
```

**Batas ARA/ARB** (hanya bila `auto_reject` terverifikasi dan tanggal ≥ `berlaku_mulai`; baris dipilih menurut harga acuan dengan aturan rentang yang sama seperti `fraksi_harga`, harga di bawah baris pertama memakai baris pertama):

| `jenis` | Batas atas ARA | Batas bawah ARB |
|---|---|---|
| `pct` (`0 < batas ≤ 100`) | `acuan × (1 + batas/100)` | `acuan × (1 − batas/100)` |
| `abs` (`batas > 0`, Rupiah) | `acuan + batas` | `acuan − batas` |

Batas atas dibulatkan ke harga valid di **bawah**-nya, batas bawah ke harga valid di **atas**-nya (§4.1). Batas bawah ≤ 0 menjadi 0 (tidak ada bar yang terkunci ARB). Kunci lama `batas_pct` ditolak dengan pesan agar diganti `jenis: pct` + `batas`.

**Resolusi rujukan** (dilakukan `config.py` sekali per konteks; hasilnya dicetak oleh `cek`):

- `ikuti_akun` = `akun.modal_awal_idr`.
- `ikuti_risiko` = nilai kunci bernama sama di `risiko`. Jika nilai itu sendiri rujukan, rujukan tersebut diresolusi lagi **dalam konteks mode yang berjalan**.
- `ikuti_maks_posisi`: `alokasi_maks = ekuitas ÷ maks_posisi` milik konteks yang berjalan (live, atau mode backtest yang aktif). Dihitung sebagai pembagian langsung, bukan lewat persen, sehingga 3.000.000 ÷ 3 = 1.000.000 tepat.

Dengan default ini, mode `edge` dan `realistis` hanya berbeda pada modal.

**Lain-lain:**

- Zona waktu selalu `Asia/Jakarta` dan tidak dapat diubah.
- Simbol provider dibentuk dari `data.akhiran_yfinance` dan `data.simbol_ihsg`.
- `laporan.format` menentukan berkas yang ditulis; ringkasan terminal ditampilkan bila `terminal` ada di daftar.

---

## 8. Lapisan data

### 8.1 Antarmuka provider

```python
class PriceProvider(Protocol):
    nama: str
    def simbol(self, kode: str) -> str: ...        # "ABCD" -> "ABCD.JK"; "IHSG" -> "^JKSE"
    def ambil_harian(self, kode: str, mulai: date, akhir: date) -> HasilAmbil: ...   # akhir inklusif

@dataclass
class HasilAmbil:
    kode: str
    sumber: str
    status: Literal["OK", "SEBAGIAN", "GAGAL"]
    jenis_error: Literal["BATAS_PERMINTAAN", "TIDAK_ADA_DATA", "FORMAT", "JARINGAN"] | None
    pesan: str                     # teks error asli, setelah token rahasia disensor (§19.5)
    bar: pd.DataFrame | None       # index: tanggal WIB; kolom: open, high, low, close, volume
    split: pd.Series | None        # tanggal → rasio (lembar baru per lembar lama)
```

**`YFinanceProvider`:**

- Memanggil `yf.Ticker(simbol).history(start=mulai, end=akhir + 1 hari, interval="1d", auto_adjust=False, actions=True, repair=False)`. Parameter `end` di yfinance bersifat eksklusif, dan `repair` dilarang karena mengubah harga (P1).
- Kolom dinormalisasi ke huruf kecil. `Adj Close` dan `Dividends` dibuang. `Stock Splits` disimpan sebagai `split`.
- Tanggal = `index.tz_convert("Asia/Jakarta").date`, tidak pernah lewat UTC. Jika ada bar bertanggal Sabtu/Minggu, seri **GAGAL** dengan pesan "tanggal/zona waktu tidak dikenali".
- Simbol = kode + `data.akhiran_yfinance`; IHSG = `data.simbol_ihsg.yfinance`.
- Satu simbol per permintaan, dengan jeda `jeda_antar_permintaan_detik`. Versi yfinance dicatat di meta.

**Klasifikasi error:**

| Jenis | Perlakuan |
|---|---|
| `BATAS_PERMINTAAN` (HTTP 429 / error rate-limit yfinance) | Jeda global `jeda_batas_permintaan_detik`, lalu satu kali coba lagi. Setelah `maks_simbol_kena_batas_berturut` simbol berturut-turut kena batas, unduhan run ini dihentikan; sisa simbol memakai cache/CSV dengan catatan "diblokir sementara oleh Yahoo". |
| `TIDAK_ADA_DATA` (simbol tidak dikenal/delisting) | Tanpa retry. Dicatat di meta, dicoba lagi paling cepat `coba_lagi_tidak_ada_data_hari` hari kemudian. |
| `FORMAT` | GAGAL tanpa retry. Jika ≥ 90% simbol gagal dengan jenis ini, laporan menyarankan memeriksa versi yfinance (§22.3). |
| `JARINGAN` | Backoff 2 s, 4 s, 8 s. |

**`CsvManualProvider`** membaca `data/manual/` (§6.9) dengan validasi yang sama.

**Kebijakan per run** (satu objek untuk seluruh run, `data/sumber.py`):
- Jeda `jeda_antar_permintaan_detik` sebelum setiap permintaan ke provider jaringan, kecuali permintaan pertama.
- `JARINGAN`: dicoba lagi sampai 3 kali dengan jeda 2 s, 4 s, lalu 8 s.
- `BATAS_PERMINTAAN`: jeda `jeda_batas_permintaan_detik`, lalu satu kali coba lagi. Jika masih kena batas, penghitung "berturut-turut" naik. Hasil lain mengembalikan penghitung ke 0.
- Saat penghitung mencapai `maks_simbol_kena_batas_berturut`, tidak ada permintaan lagi ke provider itu selama run. Simbol sisanya memakai cache/CSV dengan pesan "diblokir sementara oleh Yahoo".
- Semua jeda lewat `waktu.tidur()`.

**Provider baru** cukup mengimplementasikan antarmuka di atas dan didaftarkan lewat namanya di `data.provider_urutan`.

### 8.2 Pemilihan sumber per simbol

1. Ambil dari provider pertama, lalu gabungkan dengan cache provider itu (§8.3).
2. Status seri gabungan:
   - **OK:** lolos validasi struktur dan memuat bar `t` yang diharapkan.
   - **SEBAGIAN:** lolos validasi tetapi bar terakhirnya < `t`.
   - **GAGAL:** tidak lolos validasi struktur.
3. Jika SEBAGIAN atau GAGAL, coba `csv_manual`:
   - sebagai **pengganti penuh** (dipakai jika berkas itu sendiri berstatus OK); atau
   - sebagai **sambungan** (§6.9) di atas seri yang SEBAGIAN. Hanya bar baru yang diambil, setelah tumpang tindih tervalidasi.
4. Setiap bar menyimpan kolom `sumber`. Rencana transaksi yang memakai bar dari CSV manual diberi peringatan, dan transaksi simulasinya diberi tanda `SUMBER_MANUAL`.
5. Cache tidak pernah memasok bar `t` yang belum pernah diambil dari provider.

**Urutan cadangan** (provider pertama = utama; provider berikutnya = cadangan, biasanya `csv_manual`):

| Seri utama | Cadangan | Hasil |
|---|---|---|
| OK | — | seri utama |
| SEBAGIAN | sambungan sah (tumpang tindih cocok, §6.9) | seri utama + bar cadangan sesudah bar terakhir utama; status OK bila memuat bar `t`, selain itu SEBAGIAN |
| SEBAGIAN | sambungan tidak sah, cadangan OK | cadangan sebagai **pengganti penuh** |
| SEBAGIAN | lainnya | seri utama (SEBAGIAN), dengan catatan alasan cadangan ditolak |
| GAGAL / kosong | cadangan OK | cadangan sebagai pengganti penuh |
| GAGAL / kosong | lainnya | GAGAL (`GAGAL_AMBIL`) |

- Seri pengganti penuh yang lebih pendek dari `universe.min_bar_riwayat` → `RIWAYAT_CSV_PENDEK`.
- Split seri gabungan = split seri utama, ditambah split cadangan sesudah bar terakhir utama.
- `csv_manual` **tidak di-cache**. Berkasnya sendiri adalah sumbernya dan dibaca ulang setiap kali dipakai, sehingga berkas yang dihapus atau diperbaiki langsung berlaku. Bar yang belum final (§8.6) dari berkas ini juga dibuang.

**Simbol yang diunduh:**

- `harian`: universe ∪ breadth ∪ portfolio ∪ IHSG.
- `unduh` dan `backtest`: juga semua simbol `universe_history.csv`.

### 8.3 Cache

- Lokasi: `data/cache/<provider>/<KODE>.parquet` + `<KODE>.meta.json`. Meta berisi `diambil_pada` (WIB), `bar_pertama`, `bar_terakhir`, `jumlah_bar`, `versi_provider`, `split_terakhir`, dan `tidak_ada_data_sejak`. Kunci terakhir ini berisi tanggal permintaan **terakhir** yang menghasilkan `TIDAK_ADA_DATA`. Setiap percobaan ulang yang gagal lagi memperbaruinya, sehingga jeda `coba_lagi_tidak_ada_data_hari` berlaku lagi.
- **Update inkremental:** unduh mulai `bar_cek_ulang` hari bursa sebelum `bar_terakhir`. Bar yang tumpang tindih **selalu ditimpa** dengan nilai terbaru.
  - Selisih close > `toleransi_revisi_pct`, atau split baru → unduh ulang penuh, dicatat "riwayat disesuaikan ulang".
  - Selisih volume > `toleransi_revisi_volume_pct` → dicatat di log dan berkas kualitas.
  - Bar yang ada di cache tetapi hilang dari unduhan tumpang tindih → dihapus dan dicatat.
- **Bar hari ini tidak pernah disimpan** jika waktu pengambilan sebelum batas bar final (§8.6).
- **Penulisan atomik:** berkas sementara + `os.replace`. Parquet atau meta yang tidak terbaca diperlakukan sebagai cache kosong, lalu diunduh ulang penuh.
- Mode `--offline` hanya membaca cache. Jika bar `t` tidak ada, berlaku §8.6.

### 8.4 Split dan harga nominal

- OHLC dan volume di cache **disesuaikan untuk split**: harga dibagi rasio, volume dikali rasio. Dividen **tidak** disesuaikan; penurunan harga saat ex-dividen tetap terlihat dan bisa memicu stop, seperti di dunia nyata.
- **Faktor split** `F(d)` = hasil kali rasio split yang terjadi **setelah** `d` (rasio = lembar baru per lembar lama). Hubungannya:
  - harga nominal = harga tersesuaikan × `F(d)`;
  - volume nominal = volume tersesuaikan ÷ `F(d)`;
  - lembar tersesuaikan = lembar nominal × `F(d)`.
- **Pemeriksaan split yang belum disesuaikan sumber.** Untuk setiap split tercatat dengan rasio `r` pada tanggal `s`, hitung `q = close_s / close_{s−1}`.
  - Jika `|ln q − ln(1/r)| < |ln q|` (q lebih dekat ke 1/r daripada ke 1), seri dianggap belum disesuaikan dan diberi status `SPLIT_BELUM_DISESUAIKAN`.
  - Saham itu dikeluarkan dan dilaporkan sejak bar valid pertama ≥ `s` (as-of). Status ini didahulukan dari `ANOMALI`, karena lompatan harga pada `s` juga tampak sebagai anomali. Setiap run berikutnya mengunduh ulang penuh simbol tersebut sampai lolos: sebelum memperbarui, `unduh` memeriksa seri di cache; bila pemeriksaan ini menemukan split yang belum disesuaikan, simbol itu diunduh ulang penuh (dicatat di berkas kualitas).
  - Split seperti ini **tidak ikut** dalam `F(d)`. Harga cache sebelum `s` belum pernah dibagi rasionya, jadi harga itu sudah nominal. Jika split ini ikut, backtest pada `d < s` akan memakai harga nominal `r` kali terlalu besar.
- **Pemakaian:**
  - Semua level harga (harga_maks, stop, target, trailing) dihitung di ruang tersesuaikan, dikonversi ke nominal (× `F(d)`) untuk pembulatan, lalu dikonversi kembali.
  - Semua sizing (§14.1) di backtest memakai harga nominal.
  - Posisi disimpan sebagai `lembar_adj`. Semua arus Rupiah = `lembar_adj × harga_adj`, yang nilainya sama dengan nominal.
  - Perbandingan eksekusi (gap terhadap `harga_maks`, stop, trailing, target) memakai harga nominal hari itu yang dibulatkan ke 6 desimal di kedua sisi, sehingga keputusan tidak bergantung pada `F(d)` yang desimalnya tak berhingga (mis. rasio 1,2 atau 3).
  - Di live, `F(t) = 1`.
- **Posisi dan order yang melewati split belum disesuaikan** (backtest, jurnal kertas, jalur per sinyal). Karena split itu tidak ikut `F(d)`, harga cache melompat dengan faktor `1/r` pada `s`. Agar arus Rupiah tetap sama dengan nominal:
  - Posisi yang entry-nya sebelum `s` dikonversi pada hari bursa pertama ≥ `s`, sebelum langkah 0 §15.1: `lembar_adj × r`; harga beli, `harga_maks`, stop awal, stop/trailing, target, dan close valid terakhir ÷ `r`; `faktor_entry × r`, sehingga semua nilai nominal pada tanggal entry tidak berubah. Puncak `HHV_sejak_entry` untuk trailing memakai high sebelum `s` ÷ `r`.
  - Order yang sinyalnya sebelum `s` dan diproses pada/sesudah `s` memakai `faktor_sinyal × r` (sama seperti split yang sudah disesuaikan).
  - `close kemarin` untuk ARA/ARB pada hari pertama ≥ `s` adalah close valid sebelumnya ÷ `r`.
  - Selama SMA keluar MR masih mencakup bar sebelum `s` (kurang dari `sma_keluar` bar valid sejak `s`), `KELUAR_SMA` tidak dievaluasi. ATR yang melonjak karena lompatan itu hanya menurunkan kandidat trailing; trailing tidak pernah turun, jadi tidak perlu aturan khusus.
  - Transaksi itu selalu diberi tanda `ANOMALI`, termasuk bila tanggal itu tercatat di `anomali_ok.csv`.
- Tanpa langkah ini, saham yang kemudian di-split akan tampak "murah" di masa lalu. Ini bentuk look-ahead.

### 8.5 Validasi kualitas data

**Per seri (struktur):**

| Pemeriksaan | Tindakan |
|---|---|
| Kolom wajib hilang / tipe salah | GAGAL |
| Tanggal duplikat pada tanggal terakhir | Jika semua kolom identik, simpan satu. Jika berbeda, bar tanggal itu dianggap belum final (seolah belum ada). |
| Tanggal duplikat di tempat lain | GAGAL |
| Bar bertanggal akhir pekan | GAGAL (§8.1) |

**Per bar:**

| Pemeriksaan | Tindakan |
|---|---|
| Bar pada tanggal yang bukan hari bursa menurut kalender §8.7 | Dibuang sebelum perhitungan apa pun, dicatat `BAR_HARI_NONBURSA` |
| OHLC NaN/kosong, `high < low`, open/close di luar [low, high], harga ≤ 0, volume NaN atau < 0 | Bar rusak → dianggap hilang |
| Volume = 0 pada hari bursa | Bar tanpa transaksi: tidak dipakai untuk indikator, dihitung sebagai bar hilang untuk aturan kelengkapan, dihitung untuk `maks_hari_volume_nol`, dan tidak pernah menjadi harga eksekusi |

**Per tanggal `d` (as-of; hanya memakai bar ≤ `d`):**

| Pemeriksaan | Tindakan |
|---|---|
| **Anomali:** \|close_a / close_{a−1} − 1\| > `lonjakan_mencurigakan_pct` pada tanggal `a` (di ruang tersesuaikan), tanpa entri `anomali_ok.csv` | Saham tidak eligible untuk sinyal baru pada setiap `d ≥ a` selama `a` masih berada dalam `min_bar_riwayat` bar valid terakhir ≤ `d`. Data sebelum `a` tetap dipakai. Posisi yang terbuka saat `a` disimulasikan apa adanya dan diberi tanda `ANOMALI`. Posisi di `portfolio.csv` tetap dievaluasi dengan peringatan mencolok. |
| Bar hilang dalam `jendela_kelengkapan_bar` bar kalender terakhir > `maks_bar_hilang` | Saham tidak dinilai pada `d` |
| Bar bervolume 0 (bar mentah, bukan bar valid) lebih dari `maks_hari_volume_nol` kali dalam `likuiditas.periode_hari` hari bursa kalender terakhir | Tidak dinilai; laporan menulis "kemungkinan suspensi — cek idx.co.id" |
| Bar valid < `universe.min_bar_riwayat` | Tidak dinilai |

**Seri indeks (IHSG):**

- Kolom volume opsional dan tidak divalidasi. Aturan volume 0, likuiditas, dan `min_bar_riwayat` saham tidak berlaku.
- Yang berlaku: struktur, OHLC konsisten, bar non-bursa, kelengkapan (→ K0 "data IHSG bolong"), dan anomali (→ K0 "anomali data IHSG" selama `a` berada dalam `kondisi_pasar.sma_panjang` bar terakhir, kecuali ada entri `IHSG` di `anomali_ok.csv`).
- IHSG tidak ikut perhitungan kalender turunan.

**Keluaran:**

- `unduh` dan `backtest` menulis `output/kualitas/<YYYYMMDD-HHMMSS>_<perintah>.csv`.
- Run harian menulis `output/laporan/<t>/kualitas_data.csv`.
- Kolom: `kode, sumber, status_ambil, jenis_error, pesan, bar_terakhir, bar_hilang_60, anomali_terakhir, split_belum_disesuaikan, alasan_dikeluarkan` (enum §4.3).
- Baris pertama setiap berkas kualitas: `# BUKAN SARAN INVESTASI ...`.

### 8.6 Kesegaran data (run harian)

**Batas bar final:**

- Jika `pasar.jadwal_sesi.terverifikasi` dan `jam_penutupan` terisi: `jam_penutupan + data.jeda_data_final_menit`.
- Jika tidak: `jadwal.jam_run` (19:30, keputusan Anda, bukan aturan BEI).

**Menentukan `t`:** hari ini jika hari ini hari bursa dan waktu WIB ≥ batas bar final; selain itu, hari bursa sebelumnya.

**Cakupan universe:** dari saham universe yang punya bar valid pada hari bursa sebelum `t` (penyebut), berapa yang juga punya bar valid `t` (pembilang; selalu subset penyebut).

**Mulai terlalu awal (mode `--jadwal`):** jika hari ini hari bursa tetapi waktu WIB masih sebelum batas bar final (mis. karena zona waktu Windows bukan WIB), program **menunggu** sampai batas itu, maksimal `operasional.batas_waktu_run_jam`, lalu menentukan `t`. Program tidak keluar dengan "tidak ada data baru".

**Kondisi tunggu (mode `--jadwal`):** bar IHSG `t` belum ada, **atau** cakupan universe < `min_cakupan_universe_pct`. Tunggu `data.tunggu_data_hari_ini.jeda_menit`, unduh ulang, maksimal `data.tunggu_data_hari_ini.jumlah` kali.

**Hasil akhir:**

| Kondisi setelah percobaan | `status_data_run` | Akibat | Kode keluar |
|---|---|---|---|
| Cakupan universe < ambang | `BELUM_TERSEDIA` | Banner DATA BELUM TERSEDIA. Nol rekomendasi, tidak ada sinyal dicatat. Saran exit untuk posisi yang punya bar `t` valid. Jika IHSG dan hampir semua saham tidak punya bar `t` padahal `t−1` lengkap, laporan menambahkan "mungkin hari libur yang belum ada di holidays.csv". | 2 |
| Cakupan cukup, tetapi bar IHSG `t` tidak ada atau K0 karena data | `TIDAK_LENGKAP` | Status pasar JANGAN BELI (K0). Saran exit dan daftar pantau tetap dibuat. Sinyal dicatat sementara (§17.3). | 2 |
| Cakupan cukup, IHSG ada, tetapi sebagian saham tanpa bar `t` | `TIDAK_LENGKAP` | Proses normal. Saham tanpa bar `t` dikeluarkan dan dilaporkan sebagai "data basi". | 0 |
| Semua bar yang diharapkan ada | `LENGKAP` | Normal | 0 |

### 8.7 Kalender bursa

- **Kalender manual:** Senin–Jumat dikurangi `holidays.csv`. Dipakai untuk menentukan `t` dan `t+1`, dan untuk semua tanggal di tahun yang punya ≥ 1 entri di `holidays.csv`.
- **Kalender turunan:** untuk tahun tanpa entri `holidays.csv` (umumnya riwayat backtest). Tanggal `d` adalah hari bursa jika ≥ `min_hari_kalender_historis_pct` dari saham `breadth_universe` yang bar pertamanya < `d` **dan bar terakhirnya ≥ `d`** memiliki bar bervolume > 0 pada `d`. Saham yang datanya sudah berakhir (delisting, cache belum diperbarui) tidak ikut menilai `d`. Dihitung dari bar mentah, sebelum validasi.
- `t` tidak pernah ditentukan oleh kalender turunan.
- `cek` membandingkan kedua kalender untuk tanggal lampau di tahun yang tercakup `holidays.csv`, dan memberi peringatan bila ada ketidakcocokan (libur tercatat yang ternyata ada transaksi, atau sebaliknya).
- **Keterlambatan:** dinilai bila run terjadi pada tanggal `t+1`.
  - Jika `jadwal_sesi` terverifikasi dan waktu ≥ `jam_pra_pembukaan`: banner **TERLAMBAT — rencana mungkin tidak bisa dieksekusi hari ini**.
  - Jika `jadwal_sesi` belum terverifikasi: setiap run pada tanggal `t+1` diberi banner **RUN PADA HARI t+1 — pastikan sendiri masih sebelum pra-pembukaan**.

### 8.8 `DataView` (penjaga look-ahead)

Strategi, skor, kondisi pasar, eligibilitas, dan sizing **hanya** menerima `DataView(as_of=d)`, yang mengembalikan potongan data ≤ `d`.

Indikator boleh dihitung tervektor atas seluruh seri demi kecepatan, dengan syarat kausal: hanya jendela bergulir ke belakang, tanpa `shift(-k)`, dan tanpa normalisasi atas seluruh sampel. Syarat ini dibuktikan oleh uji T-LA1–T-LA3 (§22.1).

**Pengecualian: metadata split.** `F(d)` dan klasifikasi split belum disesuaikan (§8.4) adalah metadata cache. Keduanya ditentukan dari seluruh seri cache (rasio split dan close pada tanggal split), bukan informasi pasar untuk keputusan. Fungsinya hanya mengembalikan harga cache ke harga nominal yang memang berlaku pada `d`. Keputusan tetap hanya membaca bar ≤ `d`.
- Uji yang mengganggu bar setelah `t` (T-LA2) memakai metadata split dari data asli yang tidak diganggu.
- Uji pemotongan (T-LA1b) memberi data terpotong metadata split as-of `t`: split ≤ `t`, ditambah split sesudah `t` yang tertanam di harga cache.

---

## 9. Definisi indikator

Semua indikator dihitung atas **bar valid** saham (§4.1). Jika bar valid belum cukup, nilainya `NaN`. Aturan yang bergantung pada nilai `NaN` dianggap **tidak terpenuhi**. Jika `ATR_t ≤ 0` atau `NaN`, setiap aturan yang memakai ATR (membagi atau mengalikan, mis. PB5, BO5, MR3) dianggap tidak terpenuhi.

| Nama | Definisi |
|---|---|
| `SMA(x, n)_t` | Rata-rata `x` pada `n` bar valid terakhir, termasuk `t`. |
| `HHV(x, n)_{t−1}` | Maksimum `x` pada `n` bar valid sebelum `t` (tidak termasuk `t`). |
| `LLV(x, n)_t` | Minimum `x` pada `n` bar valid terakhir, termasuk `t`. |
| `HHV_sejak_entry(high)_d` | Maksimum `high` atas bar valid dari hari entry (inklusif) sampai `d` (inklusif). Bar hari sinyal tidak termasuk. |
| `TR_t` | `max(high_t − low_t, |high_t − close_{t−1}|, |low_t − close_{t−1}|)`; untuk bar valid pertama: `high − low`. |
| `ATR_t` | Wilder dengan `n = indikator.atr_periode`. Nilai pertama = rata-rata `n` TR pertama; selanjutnya `ATR_t = (ATR_{t−1} × (n−1) + TR_t) / n`. |
| `RSI(n)_t` | Wilder. Selisih close dipisah menjadi naik/turun. Rata-rata awal = mean `n` selisih pertama; selanjutnya smoothing Wilder periode `n`. `RSI = 100 − 100/(1 + avg_naik/avg_turun)`. Jika `avg_turun = 0` dan `avg_naik > 0`: 100. Jika keduanya 0: 50. |
| `CLV_t` | `(close_t − low_t)/(high_t − low_t)`; jika `high_t = low_t`: 0,5. |
| `NilaiTx_t` | `close_t × volume_t × data.pengali_volume` (Rupiah; invarian terhadap split). |
| `RataNilai_t` | `SMA(NilaiTx, likuiditas.periode_hari)_t`. |
| `RasioVol(n)_t` | `volume_t / SMA(volume, n)_{t−1}`, dengan rata-rata atas `n` bar valid sebelum `t`. |
| `Ret(n)_t` | `close_t / close_{t−n} − 1`. |

Riwayat dari `data.mulai_unduh` (2013) memberi pemanasan lebih dari 400 bar sebelum `backtest.mulai`. Ini mencukupi `min_bar_riwayat` (250) dan menstabilkan smoothing Wilder.

---

## 10. Filter kondisi pasar

Dievaluasi pada close `t`; hasilnya berlaku untuk entry di `t+1`. Logikanya identik di backtest, jurnal, dan live.

**Masukan:**

- IHSG: `close`, `SMA50` (`kondisi_pasar.sma_pendek`), `SMA200` (`kondisi_pasar.sma_panjang`). Breadth memakai `SMA(close, kondisi_pasar.breadth_sma)`.
- Saham breadth **tercatat** pada `t`: punya ≥ `breadth_sma` bar valid sampai hari bursa sebelum `t`.
- Saham breadth **terhitung** pada `t`: saham tercatat yang juga punya bar valid di `t` dan tidak beranomali as-of `t` (§8.5) dalam `breadth_sma` bar terakhir. Terhitung selalu subset tercatat. Filter universe dan likuiditas tidak berlaku.
- `Breadth_t` = persentase saham terhitung dengan `close_t > SMA50_t`.
- `Cakupan_t` = jumlah terhitung ÷ jumlah tercatat. Saham yang belum melantai atau riwayatnya belum cukup tidak dihitung sebagai data hilang.

**Aturan (dievaluasi berurutan; aturan pertama yang cocok menentukan status):**

| # | Kondisi | Status | Alasan yang ditulis |
|---|---|---|---|
| K0 | Bar IHSG `t` tidak ada; `SMA200` IHSG `NaN`; data IHSG bolong/beranomali (§8.5); `Cakupan_t < min_cakupan_breadth_pct`; atau jumlah terhitung < `min_saham_breadth` | **JANGAN BELI** | "Data tidak lengkap: …" (sebutkan yang kurang) |
| K1 | IHSG `close < SMA200` **dan** `SMA50 < SMA200` | **JANGAN BELI** | "IHSG dalam tren turun" |
| K2 | `Breadth_t < breadth_jangan_maks_pct` | **JANGAN BELI** | "Breadth lemah: X% < 30%" |
| K3 | IHSG `close > SMA200` **dan** `SMA50 > SMA200` **dan** `close > SMA50` **dan** `Breadth_t ≥ breadth_boleh_min_pct` | **BOLEH BELI** | "Tren IHSG naik dan breadth sehat" |
| K4 | Selain itu | **HATI-HATI** | Sebutkan setiap syarat K3 yang tidak terpenuhi |

Laporan selalu menampilkan tabel semua syarat, beserta nilai aktual, ambang, dan ✓/✗.

**Akibat status:**

| Status | Entry baru | Risiko per transaksi | Maks entry baru per hari |
|---|---|---|---|
| BOLEH BELI | Ya | `risiko.risiko_per_transaksi_pct` | `risiko.maks_entry_baru_per_hari` (null = slot) |
| HATI-HATI | Ya | × `risiko.hati_hati.pengali_risiko` | `risiko.hati_hati.maks_entry_baru_per_hari` |
| JANGAN BELI | Tidak; sinyal menjadi `BAYANGAN_PASAR` (daftar pantau) | — | 0 |

Status pasar tidak memaksa penjualan posisi, kecuali `jangan_beli_paksa_jual: true` (berlaku identik di backtest).

Ringkasan backtest menampilkan, per tahun, jumlah saham breadth terhitung dan jumlah hari K0.

---

## 11. Universe dan eligibilitas

Saham **eligible** pada `t` (live, jurnal, dan backtest) jika:

1. Ada di `universe.csv`. Di backtest dengan `universe_history.csv`: konstituen pada `t`.
2. Tidak ada di `exclude.csv` pada `t` (backtest: hanya jika `pakai_exclude: true`).
3. Lolos validasi as-of `t` (§8.4–§8.5): bar valid `t` ada, kelengkapan terpenuhi, tidak beranomali, tidak `SPLIT_BELUM_DISESUAIKAN`, dan bar valid ≥ `min_bar_riwayat`.
4. `RataNilai_t ≥ nilai_transaksi_min_idr`; batas inklusif.
5. Jumlah bar mentah bervolume 0 dalam `likuiditas.periode_hari` hari bursa kalender terakhir ≤ `maks_hari_volume_nol`.

**Sinyal dan skor hanya bergantung pada syarat 1–5**, tidak pada isi portofolio. Saham yang sedang dipegang tetap bisa menghasilkan sinyal, tetapi pada seleksi (§13.2) statusnya menjadi `SUDAH_DIPEGANG`. Tidak ada penambahan posisi pada saham yang sama.

Saham yang tidak eligible ditampilkan di bagian "Dikeluarkan" pada laporan beserta alasannya, maksimal `maks_baris_dikeluarkan` baris (sisanya hanya di `kualitas_data.csv`).

---

## 12. Aturan strategi

Semua nilai dievaluasi pada close `t` atas saham eligible. ATR = `ATR_t` (§9). Sinyal pada `t` → order beli di pembukaan `t+1` (§14).

Angka di tabel aturan adalah **nilai default**. Implementasi wajib membaca kunci config yang tercantum di baris "Kunci" setiap strategi, dan uji T-CFG memastikan setiap kunci benar-benar berpengaruh.

**Pemeriksaan stop (`STOP`).** Setelah semua syarat lolos, level dikonversi ke nominal dan dibulatkan (§4.1, §8.4). Jika stop tidak terdefinisi (`NaN`, ATR tidak sah), stop ≤ 0, atau stop ≥ `harga_maks`, tidak ada sinyal. Evaluasi mencatat cek `STOP` yang gagal beserta nilainya.

**Stop waktu** "pada close hari ke-N" berarti pada close hari ke-N, atau pada close bar valid pertama sesudahnya jika hari ke-N tidak punya bar valid.

### 12.1 PB — Pullback dalam tren naik

**Syarat sinyal (semua harus benar):**

| # | Aturan |
|---|---|
| PB1 | `close_t > SMA50_t` dan `SMA50_t > SMA200_t` |
| PB2 | `SMA50_t > SMA50_{t−10}` (`lookback_slope_sma_tren`) |
| PB3 | Ada `k ∈ {0, 1, 2, 3}` (`lookback_sentuh_sma_cepat` = 4 bar) dengan `low_{t−k} ≤ SMA20_{t−k}`: dalam 4 bar terakhir, harga menyentuh atau menembus SMA20 pada hari yang sama |
| PB4 | `close_t > high_{t−1}` dan `close_t > open_t` (pemicu pantulan) |
| PB5 | `(close_t − SMA20_t) / ATR_t ≤ 1,0` |

**Stop awal:** `LLV(low, 5)_t − 0,2 × ATR_t`, dibulatkan ke atas (§4.1).

**Validasi:** jika `(harga_maks − stop) / ATR_t > maks_jarak_stop_atr (3,0)`, sinyal berstatus `STOP_TERLALU_JAUH`. Sinyal ini dicatat di jurnal tetapi tidak masuk antrean.

**Exit:**

| Jenis | Aturan | Eksekusi |
|---|---|---|
| Stop (intraday) | `stop_awal`, tetap | Order otomatis stop di broker |
| Target (intraday) | `harga_beli + target_r (2,0) × R_harga`, dibulatkan ke bawah | Order otomatis jual limit |
| Stop waktu | Masih terbuka pada close hari ke-10 | Jual di pembukaan hari ke-11 |

**Kunci (`strategi.pb`):** SMA20 = `sma_cepat`; SMA50 = `sma_tren`; SMA200 = `sma_panjang`; 10 (PB2) = `lookback_slope_sma_tren`; 4 (PB3) = `lookback_sentuh_sma_cepat`; 1,0 (PB5) = `maks_jarak_atas_sma_cepat_atr`; LLV 5 = `lookback_swing_low`; 0,2 = `penyangga_stop_atr`; 3,0 = `maks_jarak_stop_atr`; 2,0 = `target_r`; 10 hari = `stop_waktu_hari`.

### 12.2 BO — Breakout dengan lonjakan volume

**Syarat sinyal:**

| # | Aturan |
|---|---|
| BO1 | `close_t > HHV(high, 20)_{t−1}` |
| BO2 | `RasioVol(20)_t ≥ 1,5` |
| BO3 | `close_t > SMA50_t` dan `SMA50_t > SMA200_t` |
| BO4 | `CLV_t ≥ 0,6` |
| BO5 | `(close_t − HHV(high, 20)_{t−1}) / ATR_t ≤ 1,0` |

**Stop awal:** `close_t − 2,0 × ATR_t`, dibulatkan ke atas.

**Exit:**

| Jenis | Aturan | Eksekusi |
|---|---|---|
| Trailing (intraday) | Untuk setiap close `d` ≥ hari entry: `trail_d = max(trail_{d−1}, HHV_sejak_entry(high)_d − 3,0 × ATR_d)`, dengan trail awal = `stop_awal`. Dibulatkan ke atas dan tidak pernah turun. Level `trail_d` berlaku untuk hari `d+1`. | Order otomatis; Anda memperbarui levelnya setiap sore sesuai laporan |
| Stop waktu | `stop_waktu_hari` (default null = tidak ada) | Jual di pembukaan berikutnya |

Tidak ada target tetap.

**Kunci (`strategi.bo`):** 20 (BO1) = `lookback_tertinggi`; 20 (BO2) = `lookback_volume`; 1,5 = `min_rasio_volume`; SMA50/SMA200 = `sma_tren`/`sma_panjang`; 0,6 = `min_posisi_close`; 1,0 (BO5) = `maks_lewat_level_atr`; 2,0 = `stop_atr`; 3,0 = `trailing_atr`; stop waktu = `stop_waktu_hari`.

### 12.3 MR — Oversold dalam tren naik

**Syarat sinyal:**

| # | Aturan |
|---|---|
| MR1 | `close_t > SMA200_t` |
| MR2 | `RSI(2)_t < 10` |
| MR3 | `close_{t−1} − close_t ≤ 3,0 × ATR_t` (bukan jatuh ekstrem dalam sehari, yang sering terkait berita) |

**Stop awal:** `close_t − 2,5 × ATR_t`, dibulatkan ke atas.

**Exit:**

| Jenis | Aturan | Eksekusi |
|---|---|---|
| Stop (intraday) | `stop_awal`, tetap | Order otomatis |
| Keluar pantulan | `close_d > SMA5_d` pada close hari ke-`min_hari_tahan` (2) atau sesudahnya | Jual di pembukaan berikutnya |
| Stop waktu | Masih terbuka pada close hari ke-5 | Jual di pembukaan hari ke-6 |

MR menahan posisi sangat singkat, sehingga fee 0,40% pulang-pergi plus slippage menggerus porsi besar keuntungan per transaksi. Gerbang akan menilainya dengan jujur.

**Kunci (`strategi.mr`):** SMA200 = `sma_tren`; RSI(2) = `rsi_periode`; 10 = `rsi_maks`; 3,0 (MR3) = `maks_turun_harian_atr`; 2,5 = `stop_atr`; SMA5 = `sma_keluar`; hari ke-2 = `min_hari_tahan`; hari ke-5 = `stop_waktu_hari`.

### 12.4 ACAK — pembanding acak (khusus backtest)

- **Sinyal:** untuk setiap saham eligible pada `t`, hitung `u = int.from_bytes(sha256(f"{seed}|{t:%Y-%m-%d}|{kode}".encode()).digest()[:8], "big") / 2**64`. Sinyal muncul jika `u < peluang_sinyal_pct/100`. Fungsi `hash()` bawaan Python dilarang (tidak deterministik antar proses).
- **Stop:** `close − stop_atr × ATR`. Jika ATR tidak sah, berlaku pemeriksaan `STOP` (§12). **Target:** `target_r` R. **Stop waktu:** `stop_waktu_hari`. **`K_sinyal` = 0,5.**
- **Tujuan:** memeriksa mesin backtest. Setelah fee dan slippage, PF_R-nya harus berada di sekitar atau di bawah 1.
- Tidak pernah muncul di laporan harian, jurnal, atau run gabungan, dan tidak pernah dinilai gerbang.

---

## 13. Skor dan seleksi

### 13.1 Skor (0–100)

```
skor = 40 × K_sinyal + 30 × K_rs + 15 × K_likuid + 15 × K_stop        (bobot dari config, jumlah = 100)
```

Setiap komponen `K` dipotong ke [0, 1]. Peringkat, pemecah nilai sama, dan deduplikasi memakai skor **presisi penuh**. Pembulatan 1 desimal hanya untuk tampilan.

| Komponen | Definisi |
|---|---|
| `K_sinyal` PB | `0,5 × (1 − clip((close − SMA20)/ATR, 0, 1)) + 0,5 × CLV` |
| `K_sinyal` BO | `0,5 × clip((RasioVol − v_min)/v_min, 0, 1) + 0,5 × CLV`, dengan `v_min = min_rasio_volume` |
| `K_sinyal` MR | `clip((rsi_maks − RSI2)/rsi_maks, 0, 1)` |
| `K_sinyal` ACAK | 0,5 |
| `K_rs` | `(peringkat_rata2 − 1)/(n − 1)`. Peringkat 1 = `Ret(skor.rs_lookback)_t` terendah di antara `n` saham eligible (§11 syarat 1–5) pada `t`; nilai sama memakai rata-rata peringkat. Jika `n = 1`: 0,5. Tidak bergantung pada posisi, mode, atau strategi. |
| `K_likuid` | `clip(log10(RataNilai_t / nilai_transaksi_min_idr), 0, 1)` |
| `K_stop` | `clip((jarak_stop_buruk_pct − jarak_pct)/(jarak_stop_buruk_pct − jarak_stop_ideal_pct), 0, 1)` (kunci `skor.*`), dengan `jarak_pct = (harga_maks − stop)/harga_maks × 100` setelah pembulatan |

Skor adalah heuristik pemeringkat, **bukan** peluang keberhasilan, dan laporan harus menyatakannya. Karena backtest memakai skor yang sama untuk memilih kandidat saat slot terbatas, pemeringkatan ini ikut teruji.

### 13.2 Urutan seleksi (identik di live, jurnal kertas, dan backtest)

1. **Sinyal.** Hitung sinyal semua strategi `aktif` atas saham eligible. Backtest per strategi hanya memakai satu strategi.
2. **Validasi strategi.** Sinyal yang gagal (mis. jarak stop) → `STOP_TERLALU_JAUH`; keluar dari antrean.
3. **Gerbang.** Sinyal strategi yang tidak lolos gerbang (§16.6) → `BAYANGAN_GERBANG`; keluar dari antrean. Sinyal ini tidak ikut deduplikasi dan tidak pernah menggugurkan sinyal strategi yang lolos. Backtest per strategi tidak menerapkan langkah ini.
4. **Deduplikasi.** Jika satu saham masih punya lebih dari satu sinyal, sinyal dengan skor tertinggi dipertahankan; jika skor sama, urutan PB, BO, MR. Sinyal lainnya → `DUPLIKAT_STRATEGI`.
5. **Urutkan.** Skor menurun; jika sama, `RataNilai` menurun; lalu kode alfabetis.
6. **Sudah dipegang.** Saham yang sedang dipegang → `SUDAH_DIPEGANG`.
   - **6a. Portofolio tidak dapat dievaluasi (hanya live).** Jika ada baris `portfolio.csv` berstatus TIDAK DAPAT DIEVALUASI karena §15.3 (lonjakan tanpa data split), semua sinyal tersisa → `DIBLOKIR_PORTOFOLIO` dengan alasan yang menyebut kode saham tersebut, dan laporan diberi banner "PORTOFOLIO TIDAK DAPAT DIEVALUASI — tidak ada rekomendasi beli". Proses selesai (P5).
7. **JANGAN BELI.** Jika status pasar JANGAN BELI, semua sinyal tersisa → `BAYANGAN_PASAR`, dan daftar pantau mengikuti urutan langkah 5. Proses selesai.
8. **Slot dan kas.** `slot = maks_posisi − (posisi terbuka − posisi yang dijadwalkan jual di open t+1 jika slot_bebas_dari_jual_terjadwal)`.
   - Posisi berstatus CEK ORDER (§15.4) tetap dihitung terisi.
   - `kas_tersedia` = kas `akun.yaml` (live) atau kas simulasi. Jika `dana_jual_langsung_dipakai: true`, tambahkan estimasi `stop_aktif × lembar × (1 − slip) × (1 − fj)` dari jual terjadwal.
9. **Status per kandidat.** Untuk setiap kandidat sesuai urutan, tetapkan status **pertama** yang berlaku:

| Cek | Kondisi | Status |
|---|---|---|
| a | Slot habis | `SLOT_PENUH` |
| b | Batas entry harian tercapai | `BATAS_ENTRY_HARIAN` |
| c | Sektor sudah mencapai `maks_per_sektor`. Dihitung: posisi terbuka di sektor itu, dikurangi yang dijadwalkan jual di open `t+1` (hanya jika `slot_bebas_dari_jual_terjadwal`), ditambah kandidat yang sudah diterima. | `BATAS_SEKTOR` |
| d | `lot_risiko < 1` (§14.1) | `TIDAK_TERJANGKAU`, dengan alasan "risiko 1 lot = Rp X (Y% ekuitas) > anggaran Rp Z (W%, status pasar)" |
| e | `lot_alokasi < 1` | `TIDAK_TERJANGKAU`, dengan alasan "nilai 1 lot Rp X > alokasi maks Rp Z" |
| f | `lot_likuid < 1` | `TIDAK_LIKUID` |
| g | `lot_kas < 1` | `KAS_KURANG` |
| h | Selain itu | `REKOMENDASI`: kurangi slot, kas tersedia, kuota harian, dan catat sektornya |

Jika `izinkan_satu_lot_melebihi_risiko: true` dan `lot_risiko = 0`, maka `lot_risiko = 1`, dan rencana diberi tanda "risiko di atas anggaran". Batas e–g tetap berlaku.

**Ketergantungan pada jual terjadwal** (hanya jika `slot_bebas_dari_jual_terjadwal`). Untuk setiap `REKOMENDASI`, program menghitung jual terjadwal mana yang, bila tidak terisi di open, membatalkan order itu. Caranya: §16.2 langkah 2 (cek slot lalu sektor, urutan skor) disimulasikan untuk setiap himpunan jual terjadwal yang tidak terisi, dengan asumsi order beli lain tidak batal karena data, gap, ARA, atau kas.
- Hasilnya adalah daftar **himpunan minimal**. Contoh: `{A}` berarti "jika jual A tidak terisi, tarik"; `{A, B}` berarti "jika jual A dan B keduanya tidak terisi, tarik".
- Kandidat yang tidak pernah batal tidak punya ketergantungan.
- Mesin backtest tidak memerlukannya (ia mensimulasikan open langsung) dan boleh melewati perhitungan ini.

Laporan menampilkan semua `REKOMENDASI`, lalu kandidat lain hingga `maks_kandidat_ditampilkan` beserta status dan alasannya. **Semua** sinyal dicatat di jurnal.

---

## 14. Ukuran posisi dan rencana transaksi

### 14.1 Rumus

Di backtest, semua harga di rumus ini adalah **harga nominal** (§8.4).

```
ekuitas          = kas + Σ nilai posisi                     # nilai = lembar_adj × close valid terakhir ≤ t;
                                                            # jika close t tidak valid, ekuitas ditandai "≈"
                                                            # (aturan yang sama dipakai §15.1 langkah 0 dan §16.2)
anggaran_risiko  = ekuitas × risiko_per_transaksi_pct/100 × (pengali_risiko jika HATI-HATI, selain itu 1)
alokasi_maks     = ekuitas ÷ maks_posisi                    # jika alokasi_maks_pct = ikuti_maks_posisi (§7)
                 = ekuitas × alokasi_maks_pct/100           # jika diisi angka
P                = harga_maks (sudah dibulatkan)
S                = stop (sudah dibulatkan)
L                = lot_lembar
m                = fee_minimum_idr

biaya_beli(n)    = n·L·P + max(fb·n·L·P, m)
hasil_stop(n)    = n·L·S·(1−slip) − max(fj·n·L·S·(1−slip), m)
risiko(n)        = biaya_beli(n) − hasil_stop(n)

lot_risiko   = n terbesar dengan risiko(n) ≤ anggaran_risiko
lot_alokasi  = n terbesar dengan biaya_beli(n) ≤ alokasi_maks
lot_kas      = n terbesar dengan biaya_beli(n) ≤ kas_tersedia
lot_likuid   = floor(maks_pct_order_dari_nilai_harian/100 × RataNilai_t / (P × L))
lot          = min(lot_risiko, lot_alokasi, lot_kas, lot_likuid)
```

- Jika `m = 0`, `lot_risiko = floor(anggaran_risiko / (L × risiko_lembar))`, dengan `risiko_lembar = P(1+fb) − S(1−slip)(1−fj)`. Jika `m > 0`, cari menurun dari nilai tanpa minimum.
- Semua dihitung dengan `Decimal`.
- Jika `biaya.pembulatan_fee` diisi, yang dibulatkan adalah fee akhir, yaitu `max(f·nilai, m)`.
- `harga_maks` wajib > stop. Pemanggil dengan stop ≥ `harga_maks` adalah kesalahan program, bukan kasus 0 lot.
- **Jaminan:** jika open `t+1` ≤ `harga_maks`, kerugian saat stop tersentuh dengan slippage sesuai asumsi ≤ anggaran. Kerugian bisa lebih besar hanya bila harga gap turun melewati stop atau slippage aktual melebihi asumsi (§16.10).

### 14.2 Contoh kerja (wajib menjadi uji T-SZ)

Asumsi:

- Ekuitas Rp 3.000.000 (kas Rp 3.000.000, tanpa posisi), status BOLEH BELI → anggaran Rp 30.000, alokasi maks Rp 1.000.000.
- Fee 0,15%/0,25%, slippage 0,10%, `m = 0`. Fraksi belum diverifikasi; semua harga sudah bulat.
- `RataNilai_t` = Rp 20 miliar.
- Setiap kolom dihitung **terpisah** sebagai satu-satunya kandidat. Ini menguji fungsi sizing, bukan seleksi.

| | A (PB) | B (BO) | C (MR) |
|---|---|---|---|
| close_t; ATR_t | 2.000; 60 | 9.000; 250 | 450; 16 |
| harga_maks | 2.000 + 30 = **2.030** | 9.000 + 125 = **9.125** | 450 + 8 = **458** |
| stop | LLV5 1.900 − 12 = **1.888** | 9.000 − 500 = **8.500** | 450 − 40 = **410** |
| P × 1,0015 | 2.033,045 | 9.138,6875 | 458,687 |
| S × 0,999 × 0,9975 | 1.881,39672 | 8.470,27125 | 408,566025 |
| risiko_lembar | **151,64828** | **668,41625** | **50,120975** |
| risiko per lot | 15.164,828 | 66.841,625 | 5.012,0975 |
| lot_risiko | floor(1,978) = **1** | **0** | floor(5,985) = **5** |
| biaya_beli(1) | 203.304,50 | 913.868,75 | 45.868,70 |
| lot_alokasi / lot_kas / lot_likuid | 4 / 14 / 985 | 1 / 3 / 219 | 21 / 65 / 4.366 |
| **Hasil** | **1 lot**: nilai Rp 203.000, risiko Rp 15.164,83 (0,51%) | **TIDAK_TERJANGKAU**: risiko 1 lot Rp 66.841,63 (2,23%) > Rp 30.000 | **5 lot**: nilai Rp 229.000, risiko Rp 25.060,49 (0,84%) |
| Saat HATI-HATI (Rp 15.000) | TIDAK_TERJANGKAU (15.164,83 > 15.000) | TIDAK_TERJANGKAU | 2 lot (risiko Rp 10.024,20) |

**Uji tambahan T-SZ:**

- **Fee minimum fiktif** (khusus uji), `m` = Rp 5.000, kandidat A:
  - `n = 2`: biaya 411.000 − hasil stop 372.222,40 = 38.777,60 > 30.000.
  - `n = 1`: 208.000 − 183.611,20 = **24.388,80** → 1 lot.
- **Batas pembulatan:** P = 1.060, S = 964, slippage 0. Risiko per lot tepat 10.000,00, sehingga hasilnya **3 lot**. Ini membuktikan bahwa `Decimal` dipakai, bukan float.

**Target A (PB, 2R)** dari harga isi aktual:

| Harga isi | Target |
|---|---|
| 2.000 | 2.000 + 2 × 112 = **2.224** |
| 2.015 | 2.015 + 2 × 127 = **2.269** |
| 2.030 | 2.030 + 2 × 142 = **2.314** |

**Perkiraan R:R bersih** = `(target × (1 − fj) − harga_maks × (1 + fb)) / risiko_lembar`. Untuk A di 2.030: (2.308,215 − 2.033,045)/151,64828 = **1,81**. Target 2R bernilai ≈ 1,81R setelah fee dan asumsi slippage.

**Contoh fee (uji T-BIAYA, slippage 0):**

| Langkah | Nilai | Fee | Kas berubah |
|---|---|---|---|
| Beli 1 lot di 2.030 | 203.000 | 304,50 | −203.304,50 |
| Jual di 2.314 | 231.400 | 578,50 | +230.821,50 |
| **P&L bersih** | | | **Rp 27.517** |

### 14.3 Isi rencana per kandidat `REKOMENDASI`

| Field | Isi |
|---|---|
| Identitas | Kode, nama, sektor, strategi, skor (1 desimal) |
| Alasan sinyal | Setiap aturan dengan nilai aktual, mis. "PB4: close 2.000 > high kemarin 1.985 ✓" |
| Harga referensi | close `t` |
| **Instruksi masuk** | "Pasang order BELI limit di **harga_maks**, N lot, sebelum pra-pembukaan `t+1`. **Segera setelah harga pembukaan terbentuk, cek order Anda.** Jika belum terisi (open > harga_maks), **tarik (withdraw) order**; jangan biarkan antre sepanjang hari, karena bisa terisi belakangan di harga yang tidak diuji backtest. Jika hanya terisi sebagian, tarik sisanya dan catat lot yang terisi saja." Untuk setiap himpunan minimal ketergantungan jual terjadwal (§13.2): "Jika jual <kode> [dan <kode> …] di pembukaan tidak terisi, tarik order beli ini." |
| Stop loss | Level, metode, jarak %. Jika fraksi belum diverifikasi: "bulatkan stop **ke atas** ke fraksi valid". |
| Target | PB: tabel target untuk harga isi = harga_ref, tengah, dan harga_maks, plus rumus "target = harga isi + 2 × (harga isi − stop)". BO: "trailing, diperbarui setiap sore". MR: "jual di pembukaan setelah close > SMA5 (mulai hari ke-2)". |
| Lot, nilai, fee beli, risiko | Rp dan % ekuitas, beserta sisa anggaran risiko per lembar |
| R:R bersih | Hanya PB |
| **Langkah setelah terisi** | "Pasang order otomatis stop di X (dan target di Y) **hari itu juga**. Jika harga isi sudah ≤ stop, stop telah terlewati: jual segera. Perbarui portfolio.csv dan akun.yaml (§19.6)." Jika `stop_aktif_hari_entry: false`: "pasang mulai besok". |
| Peringatan | Harga belum dibulatkan ke fraksi ("bulatkan limit beli dan target ke BAWAH, stop ke ATAS"); bar dari CSV manual; risiko di atas anggaran (jika diizinkan); dan sebagainya |

---

## 15. Mesin exit dan saran posisi yang dipegang

### 15.1 Simulasi per hari bursa `d` (backtest, jurnal kertas, jalur per-sinyal)

`S` = level stop/trailing dan `T` = target yang berlaku untuk `d`; keduanya ditetapkan pada close `d−1`, atau saat sinyal untuk hari entry.

**Langkah, berurutan, untuk setiap posisi:**

0. **Bar `d` tidak ada atau tanpa transaksi.** Tidak ada eksekusi apa pun dan trailing tidak diperbarui. Jual terjadwal ditunda ke bar valid berikutnya (tanda `TERTUNDA_TANPA_TRANSAKSI`). Nilai posisi memakai close valid terakhir (§14.1). Hitungan hari tetap berjalan; stop waktu yang jatuh tempo dievaluasi pada close bar valid berikutnya (§12). Lanjut ke posisi berikutnya.
1. **Bar terkunci ARB.** Hanya jika `auto_reject` terverifikasi dan `d ≥ berlaku_mulai`. Kondisi: `open = high = low = close` dan `close ≤ batas_bawah_ARB(harga_acuan)`; `harga_acuan` = close valid terakhir sebelum `d`. Semua jual tidak dapat dieksekusi hari itu dan ditunda ke hari berikutnya (tanda `TERTUNDA_ARB`):
   - Jual terjadwal tetap terjadwal dengan label aslinya.
   - Stop yang terlewati pada bar itu (`open_d ≤ S`) dijadwalkan di open bar valid berikutnya dengan label `STOP_GAP`/`TRAILING_GAP` (di hari entry: `STOP_GAP_HARI_ENTRY`), walaupun open berikutnya kembali di atas stop.
   - Tanda `TERTUNDA_ARB` hanya diberikan bila memang ada jual yang tertunda.
   - Langkah 5–6 tidak berlaku pada bar itu. Langkah 7 tetap berlaku karena bar terkunci adalah bar valid.
2. **Jual terjadwal** (dari aturan berbasis close sebelumnya): keluar di `open_d × (1 − slip)`, dengan label alasan aslinya.
3. **Gap di bawah stop:** jika `open_d ≤ S`, keluar di `open_d × (1 − slip)`. Label: `STOP_GAP` atau `TRAILING_GAP`.
4. **Gap di atas target:** jika `T` ada dan `open_d ≥ T`, keluar di `open_d` (`TARGET_GAP`; limit, tanpa slippage).
5. **Stop intraday:** jika `low_d ≤ S`, keluar di `S × (1 − slip)`. Label: `STOP` atau `TRAILING`. Jika target juga tersentuh di hari yang sama, stop dianggap lebih dulu.
6. **Target intraday:** jika `high_d ≥ T`, keluar di `T` (`TARGET`).
7. **Pada close `d`:** perbarui trailing (BO), lalu evaluasi aturan berbasis close untuk dijadwalkan jual di open `d+1`. Jika beberapa aturan terpicu bersamaan, prioritas label: `PAKSA_JUAL_PASAR` > `KELUAR_SMA` > `STOP_WAKTU`.

**Hari entry:**

- Order beli dari sinyal `d−1` diproses di open `d` (§16.2). Harga isi = `min(open_d × (1 + slip), harga_maks)`.
- Jika `open_d ≤ S`, order tetap terisi (karena open ≤ limit), lalu langsung keluar di `open_d × (1 − slip)` dengan label `STOP_GAP_HARI_ENTRY`. Transaksi ini dihitung penuh di semua metrik. R-nya dihitung terhadap `risiko_awal_idr` rencana (§4.1). Karena beli dan jual sama-sama di sekitar open, kerugiannya biasanya hanya fee dan slippage, sehingga R berada antara −1 dan 0. Jika bar `d` terkunci ARB (langkah 1), jual ditunda seperti biasa.
- Langkah 5–6 berlaku di hari entry hanya jika `entry.stop_aktif_hari_entry: true`. Langkah 7 selalu berlaku.
- Posisi yang langsung keluar `STOP_GAP_HARI_ENTRY` tidak menempati slot atau sektor untuk order berikutnya di open yang sama.

**Seri berhenti (as-of, tanpa melihat masa depan):** jika posisi terbuka tidak punya bar valid selama `backtest.maks_hari_tanpa_bar` (60) hari bursa kalender berturut-turut sampai `d`, posisi ditutup **pada `d`** di close valid terakhir × (1 − `potongan_data_berakhir_pct`/100) dengan label `DATA_BERAKHIR`. Keputusan ini tidak dibatalkan walaupun bar muncul lagi kemudian. Transaksi ini dihitung di semua metrik, dan jumlahnya dilaporkan. Jurnal kertas memakai aturan yang sama.

### 15.2 Asumsi eksekusi

- Order otomatis stop diasumsikan terisi di `S × (1 − slip)`.
- Order beli diasumsikan terisi penuh. Pengisian sebagian tidak dimodelkan.

### 15.3 Posisi di `portfolio.csv` dan aksi korporasi

- `harga_beli`, `stop_awal`, `lot`/`lembar` adalah nilai **nominal per `harga_per_tanggal`**; jika kosong, per `tanggal_beli`.
- Sebelum evaluasi, program mengonversinya ke ruang cache:
  - `harga_adj = harga / F(harga_per_tanggal)`
  - `lembar_adj = lembar × F(harga_per_tanggal)`
  
  `F` dihitung dari data split terkini.
- Jika ada split antara `harga_per_tanggal` dan `t`, laporan menampilkan banner **SPLIT TERDETEKSI** berisi padanan pasca-split (lembar, harga beli, stop, target) untuk dicocokkan dengan aplikasi broker, dan meminta Anda memeriksa ulang order otomatis.
- Anda boleh memperbarui baris itu ke nilai pasca-split dengan `harga_per_tanggal` = tanggal pembaruan, atau membiarkannya. Keduanya benar, dan program tidak pernah menyesuaikan dua kali.
- Jika data split tidak tersedia untuk sumber yang dipakai sementara terdeteksi lonjakan > `lonjakan_mencurigakan_pct` sejak `tanggal_beli`, posisi berstatus **TIDAK DAPAT DIEVALUASI** dan tidak ada rekomendasi beli baru (P5).

### 15.4 Saran harian untuk setiap baris `portfolio.csv`

State posisi (trailing, hari ke-N, target, exit yang seharusnya terjadi) dihitung ulang secara deterministik dengan **memutar ulang §15.1** dari `tanggal_beli` sampai `t`, memakai `harga_beli` sebagai harga isi. Kondisi dievaluasi berurutan; yang pertama cocok menentukan saran:

| # | Kondisi | Saran |
|---|---|---|
| 1 | Data `t` tidak ada/rusak, atau §15.3 tidak bisa dievaluasi | **TIDAK DAPAT DIEVALUASI**, beserta alasannya; level stop terakhir yang valid ditampilkan |
| 2 | `strategi = MANUAL` | Info saja: jarak ke `stop_awal` dan P&L belum terealisasi. Tanpa aturan exit sistem. |
| 3 | `stop_awal ≥ harga_beli` (terisi saat harga gap di bawah stop) | **JUAL di pembukaan `t+1`** — "posisi sudah berada di bawah stop sejak dibeli" |
| 4 | Pemutaran ulang menunjukkan exit pada tanggal < `t` (alasan apa pun), **atau** jual terjadwal di pembukaan `t` (`KELUAR_SMA`, `STOP_WAKTU`, `PAKSA_JUAL_PASAR` dari close sebelumnya) | **JUAL di pembukaan `t+1`** — "menurut aturan, posisi seharusnya keluar pada <tanggal> (<alasan>). Jika sudah dijual, perbarui portfolio.csv dan akun.yaml." |
| 5 | Pemutaran ulang menunjukkan exit oleh order otomatis pada `t` (`STOP`, `STOP_GAP`, `TRAILING`, `TRAILING_GAP`, `TARGET`, `TARGET_GAP`, `STOP_GAP_HARI_ENTRY`). Pemutaran ulang menghormati `stop_aktif_hari_entry`. | **CEK_ORDER:** "order otomatis seharusnya terisi hari ini di <level>." Jika masih dipegang → JUAL di pembukaan `t+1`. |
| 6 | Pemutaran ulang: aturan berbasis close pada `t` menjadwalkan jual di `t+1` | **JUAL di pembukaan `t+1`**, dengan alasannya (mis. "MR: close 1.245 > SMA5 1.238") |
| 7 | Selain itu | **TAHAN**. Tampilkan level stop untuk besok (dan perubahannya dari kemarin), target, hari ke-N dari stop waktu, serta P&L belum terealisasi (Rp, %, R). |

Untuk slot (§13.2 langkah 8), saran JUAL (#3, #4, #6) dihitung sebagai "dijadwalkan jual". Saran CEK_ORDER (#5) tetap dihitung terisi. Hasil jual tidak masuk `kas_tersedia` sampai Anda memperbarui berkas.

**Pengingat konsistensi:**

- "REKOMENDASI kemarin menurut data seharusnya terisi (open ≤ harga_maks), tetapi tidak ada di portfolio.csv."
- "Posisi X menurut data seharusnya sudah keluar."

---

## 16. Backtest

### 16.1 Perintah dan keluaran

`scripts\backtest.bat` menjalankan `python -m asisten_bei backtest [--strategi pb,bo,mr,acak] [--mode edge,realistis]` (default: semua).

Keluaran di `output/backtest/<YYYYMMDD-HHMMSS>_<hash8>/`:

| Berkas | Isi |
|---|---|
| `ringkasan.md`, `ringkasan.html` | Diawali "BUKAN SARAN INVESTASI" dan diakhiri teks §18.4. Berisi tabel metrik, grafik ekuitas vs IHSG, grafik drawdown, tabel per tahun, status gerbang, diagnostik, dan keterbatasan. |
| `transaksi_<run>_<mode>.csv` | Satu baris per transaksi: tanggal sinyal, entry, exit, harga (tersesuaikan **dan** nominal pada tanggalnya, kolom `*_nominal`), lembar, fee, P&L Rp, R, `alasan_exit`, `tanda_transaksi`, hari |
| `ekuitas_<run>_<mode>.csv` | Kurva ekuitas harian |
| `sinyal_dilewati_<run>_<mode>.csv` | Sinyal yang tidak menjadi transaksi, dengan `status_sinyal` / `status_isi` |
| `sinyal_r_<strategi>.csv` | Jalur per sinyal (§16.7) |
| `gerbang_run.json` | Hasil gerbang run ini, sebelum digabung ke `output/backtest/gate_status.json` (satu-satunya berkas yang dibaca run harian) |
| `config_snapshot.yaml` | Salinan config yang dipakai |

`<run>` memakai enum `run_backtest` (§4.3).

**Run yang dijalankan:** per strategi yang diminta × mode yang diminta. `gabungan_aktif` dan `gabungan_lolos` hanya dijalankan bila `--strategi` tidak diisi. `gabungan_lolos` memerlukan mode `edge` dalam run yang sama, dan dilewati dengan catatan bila tidak ada strategi yang lolos.

**Simbol backtest:** universe ∪ `universe_history.csv` ∪ breadth, ditambah IHSG. Tanda kualitas dihitung sampai `t` untuk semua simbol (kausal, sehingga nilai ≤ akhir data identik); berkas kualitas `output/kualitas/<YYYYMMDD-HHMMSS>_backtest.csv` dievaluasi as-of akhir data.

**Akhir data backtest** = `backtest.selesai`, atau, bila null, hari bursa terakhir ≤ `t` (§8.6) yang punya bar IHSG valid.
- `backtest.selesai` yang bukan hari bursa → hari bursa terakhir ≤ `selesai`.
- `backtest.selesai` > `t` → memakai aturan null, dengan catatan di ringkasan.
- Tidak ada bar IHSG valid ≤ `t`, atau akhir data < `backtest.mulai` → "DATA BELUM TERSEDIA untuk backtest", kode keluar 2. Berkas kualitas tetap ditulis; tidak ada folder run.

**Folder run:** bila dua run memakai nama folder yang sama (detik yang sama), folder itu dipakai ulang dan berkasnya ditimpa. `gerbang_run.json` selalu ditulis; tanpa mode `edge` (atau hanya ACAK) isinya `{"catatan": ..., "strategi": {}}`. `config_snapshot.yaml` = komentar disclaimer, komentar hash, lalu salinan apa adanya berkas config yang dipakai.

**Tabel per tahun** (dari kurva ekuitas penuh, per tahun kalender, titik awal = close terakhir tahun sebelumnya): return strategi vs IHSG, max drawdown, jumlah entry, dan exposure. PF per tahun hanya ditampilkan lewat jendela in-sample (§16.4), agar harga sesudah akhir tahun tidak bocor.

**Kolom berkas** (angka dengan titik desimal tanpa pemisah ribuan; Rupiah 2 desimal ROUND_HALF_UP; R 4 desimal; harga sampai 6 desimal tanpa nol di belakang; tanggal `YYYY-MM-DD`; kosong = tidak ada):
- `transaksi_*`: `kode, strategi, sektor, tanggal_sinyal, tanggal_entry, tanggal_exit, alasan_exit, tanda_transaksi, lembar, harga_beli, harga_beli_nominal, harga_exit, harga_exit_nominal, stop_awal, stop_awal_nominal, target, target_nominal, biaya_beli_idr, fee_beli_idr, fee_jual_idr, slippage_idr, hasil_jual_idr, pnl_idr, risiko_awal_idr, r, hari, skor, status_pasar_sinyal, pembatas_lot`.
  - `lembar` = lembar nominal pada tanggal entry.
  - Harga beli, stop, dan target nominal pada tanggal entry; harga exit nominal pada tanggal exit.
  - `tanda_transaksi` dipisah `;` dengan urutan enum §4.3.
  - Posisi yang masih terbuka di akhir ditulis `TERBUKA`: `tanggal_exit` = akhir data, harga exit = close valid terakhir, tanpa fee jual.
- `ekuitas_*`: `tanggal, kas_idr, nilai_posisi_idr, ekuitas_idr, n_posisi, perkiraan, status_pasar, ihsg_close, ekuitas_ihsg_idr`. Baris pertama = titik awal (§16.4). `perkiraan` 1/0 = ekuitas "≈".
- `sinyal_dilewati_*`: `tanggal_sinyal, kode, strategi, skor, status_sinyal, status_isi, tanggal_isi, alasan`.
- `sinyal_r_*`: `tanggal_sinyal, kode, strategi, skor, status_pasar, status_isi, tanggal_entry, tanggal_exit, alasan_exit, r, hari, tanda_transaksi`. Unit `TERBUKA`: `tanggal_exit` = akhir data dan `hari` dihitung sampai akhir data, seperti `transaksi_*`. `tanda_transaksi` mengikuti aturan posisi (`ANOMALI`, `SUMBER_MANUAL`, `TERTUNDA_*`).

**Disclaimer di semua keluaran:**

- Setiap CSV diawali baris `# BUKAN SARAN INVESTASI ...` (dibaca dengan `comment="#"`). Untuk `holdout_log.csv`, baris ini ditulis saat berkas dibuat.
- Setiap JSON memuat kunci tetap `"catatan": "BUKAN SARAN INVESTASI ..."`. Nilainya konstan, sehingga hasil tetap byte-identik.
- Setiap YAML diawali komentar `# BUKAN SARAN INVESTASI ...`.
- Keluaran terminal semua perintah diakhiri baris disclaimer.

**Hash** (per strategi, agar perubahan pada satu strategi tidak membatalkan gerbang strategi lain):

- **Bagian config yang dikecualikan** dari semua hash, karena tidak mengubah hasil simulasi:
  - bagian `laporan`, `notifikasi`, `jurnal`, `operasional`, `jadwal`, dan `akun.maks_umur_hari_bursa`;
  - seluruh `pasar.jadwal_sesi`;
  - kunci `sumber`, `tanggal_cek`, dan `papan_berlaku` di `pasar.fraksi_harga`/`pasar.auto_reject`;
  - `biaya.terverifikasi` dan `data.pengali_volume_terverifikasi`;
  - parameter unduhan: `data.jeda_*`, `data.maks_simbol_kena_batas_berturut`, `data.coba_lagi_tidak_ada_data_hari`, `data.tunggu_data_hari_ini`;
  - `backtest.maks_pf_r_acak`, `gerbang.kedaluwarsa_hari`, dan semua kunci path berkas/folder.
  
  Kunci `terverifikasi` pada `fraksi_harga` dan `auto_reject` **ikut** di-hash, karena mengubah simulasi.
- `hash_aturan[X]` untuk strategi X = SHA-256 dari config yang tersisa, tetapi dari bagian `strategi` hanya `strategi.X`. Ditambah `versi_program` (`asisten_bei.__version__`).
- `hash_aturan` (global, untuk periode kertas dan log holdout) = SHA-256 dari config yang tersisa, dengan seluruh `strategi` kecuali `strategi.acak`.
- `hash_input` = SHA-256 isi `universe.csv`, `universe_history.csv` (jika ada), `breadth_universe.csv`, `anomali_ok.csv`, `holidays.csv`, dan `exclude.csv` (jika `pakai_exclude`).
- `hash8` = 8 karakter pertama SHA-256 dari gabungan `hash_aturan` dan `hash_input`.

### 16.2 Linimasa simulasi (per hari bursa historis `d`)

```
PEMBUKAAN d
  1. Langkah 0–4 §15.1 untuk posisi lama (bar hilang/ARB, jual terjadwal, gap stop, gap target).
  2. Order beli tertunda dari sinyal d−1, berurutan sesuai skor:
       bar d tidak ada, tanpa transaksi (volume 0), atau rusak (§8.5)     → BATAL_DATA
       open_d > harga_maks                                                → BATAL_GAP
       bar terkunci ARA: open = high = low = close dan close ≥ batas_atas_ARA(harga_acuan)
         (hanya bila auto_reject terverifikasi dan d ≥ berlaku_mulai)     → BATAL_ARA
       slot penuh setelah langkah 1                                       → BATAL_SLOT_SAAT_OPEN
       sektor penuh setelah langkah 1 (jual terjadwal sektor itu tertunda) → BATAL_SEKTOR_SAAT_OPEN
       kas < biaya_beli (dengan harga isi)                                → BATAL_KAS_SAAT_OPEN
       selain itu: beli lot yang ditetapkan pada close d−1 di min(open_d × (1+slip), harga_maks);
                   jika open_d ≤ stop → langsung keluar (STOP_GAP_HARI_ENTRY)
     Pembatalan di open hanya boleh memakai fakta ketersediaan transaksi pada d (bar ada, volume > 0,
     bar tidak rusak, pola terkunci ARA) dan harga open. Aturan keputusan yang dievaluasi pada close
     (anomali, likuiditas, kelengkapan, sinyal) TIDAK boleh membatalkan order di open d.
SEPANJANG HARI d
  3. Langkah 5–6 §15.1 untuk semua posisi terbuka (posisi baru: hanya jika stop_aktif_hari_entry).
PENUTUPAN d
  4. Ekuitas_d = kas + Σ nilai posisi (mark-to-market).
  5. Status pasar d.
  6. Langkah 7 §15.1 (trailing; exit berbasis close, termasuk paksa jual dengan status pasar d) → jadwal jual d+1.
  7. Sinyal d → skor → seleksi & sizing §13–§14 memakai Ekuitas_d dan status pasar d → order tertunda untuk d+1.
```

- Hasil jual intraday (stop/target) baru bisa dipakai keesokan harinya.
- Hasil jual di open dipakai di open yang sama hanya jika `dana_jual_langsung_dipakai: true`.
- Run harian dan jurnal memakai urutan yang sama.

### 16.3 Mode dan run

| Mode | Modal | Aturan lain |
|---|---|---|
| `edge` | Rp 1 miliar | Identik dengan live: 3 slot, 1 per sektor, alokasi 33,3%, sinyal, entry, exit, status pasar, fee, slippage, sizing |
| `realistis` | `backtest.mode.realistis.modal_idr` (default `ikuti_akun` = Rp 3 juta) | Identik dengan live |

**Run yang dijalankan:**

- **Per strategi** (`pb`, `bo`, `mr`, `acak`): masing-masing sendiri. Tanpa deduplikasi dan tanpa gerbang.
- **`gabungan_aktif`:** semua strategi `aktif` (tanpa ACAK) berbagi slot, dengan deduplikasi §13.2.
- **`gabungan_lolos`:** hanya strategi yang lolos gerbang **pada run yang sama**. Karena strategi dipilih berdasarkan holdout, metrik holdout `gabungan_lolos` **bukan out-of-sample**. Ringkasan wajib menandainya "terseleksi dengan holdout — optimis". Ekspektasi live yang jujur hanya diperoleh dari jurnal kertas setelah tanggal gerbang.

### 16.4 Periode, jendela, dan holdout

- **Pemanasan:** data sejak `data.mulai_unduh`. Tidak ada transaksi sebelum `backtest.mulai`.
- **In-sample:** `backtest.mulai` … hari bursa terakhir sebelum `holdout_mulai`.
- **Holdout:** `holdout_mulai` … akhir data backtest (§16.1).
- **Penugasan transaksi:**
  - Transaksi masuk ke periode/jendela berdasarkan **tanggal entry**.
  - Untuk statistik in-sample, transaksi yang masih terbuka pada hari bursa terakhir sebelum `holdout_mulai` diperlakukan `TERBUKA`: dinilai pada close hari itu, tidak masuk PF/win rate, dan diberi tanda `MELEWATI_HOLDOUT`. Tanda lainnya diambil sesuai keadaan hari itu (tanda yang baru muncul di holdout tidak dihitung). Ini mencegah harga dan peristiwa periode holdout bocor ke statistik in-sample.
- **Jendela:** setiap tahun kalender di in-sample, dipotong di akhir data bila holdout belum tercapai. Tahun yang tidak mencakup hari bursa pertama sampai hari bursa terakhirnya diberi label "parsial". Jendela hanya dihitung untuk G5 jika memiliki ≥ `min_transaksi_per_jendela` transaksi.
- **Sinyal dilewati** masuk periode menurut tanggal sinyal. Order dari sinyal hari terakhir in-sample yang baru diputuskan di open holdout pertama (BATAL_*) tidak ikut statistik in-sample, karena hasilnya memakai harga holdout.
- **Titik awal setiap periode/jendela:** `E_awal` strategi dan pembanding IHSG sama-sama diambil pada close hari bursa terakhir sebelum periode. Untuk periode pertama: modal awal dan close IHSG sebelum `backtest.mulai`. `hari_kalender` dihitung dari tanggal itu.
- **Parameter tidak dioptimasi.**
- **Disiplin holdout:**
  - Bila `holdout_log.csv` terkunci (dibuka di Excel), isinya ditulis ke `holdout_log_<HHMMSS>.csv` dengan peringatan; berkas alternatif ini tetap dibaca untuk N/M (baris yang sama dihitung sekali), jadi jangan dihapus. Berkas alternatif tidak pernah ditimpa (`_2`, `_3`, ...). Bila ada berkas log yang tidak terbaca, ringkasan menyebut N/M sebagai batas bawah ("sedikitnya") dengan peringatan.
  - Setiap run × mode yang menghitung holdout menambah satu baris ke `holdout_log.csv`: `waktu, holdout_mulai, hash_aturan, hash_input, strategi, mode, pf_r_holdout, n_holdout`. Kolom `strategi` berisi nama run (`run_backtest`). Semua baris dari satu perintah backtest memakai `waktu` yang sama.
  - Ringkasan menampilkan: "Holdout telah dilihat **N** kali dengan **M** konfigurasi aturan berbeda." N = banyaknya nilai `waktu` berbeda (perintah backtest), M = banyaknya `hash_aturan` berbeda. Jika M > 1, holdout tidak lagi murni dan ringkasan memberi peringatan.
  - `cek` memperingatkan jika `holdout_mulai` berbeda dari nilai pertama di log.

### 16.5 Metrik

Untuk setiap strategi × mode × {in-sample, holdout, penuh, setiap jendela}:

| Metrik | Rumus |
|---|---|
| CAGR | `(E_akhir / E_awal)^(365,25 / hari_kalender) − 1` |
| Max drawdown | `max_d (1 − E_d / max_{s≤d} E_s)` dari ekuitas close harian; plus tanggal puncak, lembah, dan pulih, serta durasi terpanjang di bawah puncak (hari bursa) |
| Win rate | Transaksi dengan P&L bersih > 0 ÷ transaksi tertutup |
| PF (Rupiah) | `Σ P&L bersih menang / |Σ P&L bersih kalah|` |
| **PF_R** | `Σ R menang / |Σ R kalah|`, dipakai gerbang karena tidak terdistorsi ukuran posisi. Tanpa kerugian: "n/a (tanpa kerugian)". |
| Max drawdown R | Drawdown terbesar pada kurva R kumulatif (transaksi diurutkan menurut tanggal exit) |
| Jumlah transaksi | Tertutup, terpisah dari yang `TERBUKA` |
| Ekspektansi | Rata-rata P&L bersih (Rp) dan rata-rata R |
| Rata-rata menang / kalah | Rp dan R |
| Kerugian beruntun terpanjang | Jumlah transaksi |
| Rata-rata lama menahan | Hari bursa |
| Exposure | % hari dengan ≥ 1 posisi |
| Fee + slippage | Rp dan % dari laba kotor |
| Sinyal dilewati | Jumlah per status |
| Pembatas lot | % transaksi yang lot-nya dibatasi alokasi, kas, atau likuiditas (bukan risiko) |
| Risiko terealisasi | Rata-rata `risiko_awal_idr` ÷ ekuitas (%) |
| Modal terpakai | Rata-rata Σ nilai posisi ÷ ekuitas (%) |
| Transaksi bertanda | Jumlah per `tanda_transaksi`; metrik juga dihitung **tanpa** transaksi bertanda `ANOMALI` |
| Sharpe (informasi) | `mean(r_harian) / std(r_harian) × √(hari_bursa_per_tahun)`, rf = 0, dengan `hari_bursa_per_tahun` = rata-rata jumlah hari bursa per tahun kalender penuh dalam periode backtest |
| Calmar (informasi) | `CAGR / MaxDD` |

**Definisi rinci:**
- *Slippage* per transaksi = lembar × |harga acuan − harga isi|. Acuannya: `min(open, harga_maks)` untuk beli; open untuk jual di open; level stop untuk stop intraday. Target dan `DATA_BERAKHIR` tanpa slippage.
- *Laba kotor* = Σ (P&L bersih + fee + slippage) transaksi tertutup. "% dari laba kotor" ditampilkan "n/a" bila laba kotor ≤ 0.
- *Exposure* dan *modal terpakai* dihitung atas hari simulasi, tanpa baris titik awal.
- *Risiko terealisasi* memakai ekuitas pada tanggal sinyal.
- *Pembatas lot* = `pembatas` sizing (§14.1) ∈ {alokasi, kas, likuid}.
- *Durasi di bawah puncak* dihitung dalam hari bursa.
- *Sharpe*: `hari_bursa_per_tahun` = rata-rata hari bursa per tahun kalender penuh dalam periode. Tanpa tahun penuh → 252.

**Pembanding beli-dan-tahan IHSG:** modal yang sama dinilai mengikuti close IHSG dari titik awal periode (§16.4), tanpa fee. Ditampilkan berdampingan: CAGR, MaxDD, return per jendela, dan selisih CAGR. IHSG adalah indeks harga tanpa dividen dan tidak bisa dibeli langsung. Strategi juga tidak memperhitungkan dividen; keduanya konsisten, tetapi sama-sama mengecilkan total return.

### 16.6 Gerbang

Strategi **lolos** jika semua syarat berikut terpenuhi pada mode `edge`, untuk run strategi itu sendiri:

| # | Syarat |
|---|---|
| G1 | Holdout: transaksi tertutup ≥ `min_transaksi_holdout` (30) |
| G2 | Holdout: PF_R ≥ `min_pf_r_holdout` (1,2). "n/a (tanpa kerugian)" dianggap lolos jika G1 lolos. |
| G3 | Holdout: ekspektansi R > `min_ekspektansi_r_holdout` (0) |
| G4 | In-sample: PF_R ≥ `min_pf_r_insample` (1,1). "n/a (tanpa kerugian)" dengan ≥ 1 transaksi menang dianggap lolos; tanpa transaksi gagal. |
| G5 | In-sample: ≥ `min_pct_jendela_pf_di_atas_1` (50%) jendela yang memenuhi syarat memiliki PF_R > 1. Jika tidak ada jendela yang memenuhi syarat, G5 **gagal**. PF_R jendela "n/a (tanpa kerugian)" dengan ≥ 1 transaksi menang dihitung > 1. |
| G6 | Penuh: max drawdown R ≤ `maks_drawdown_r` (15R) |

**Isi `output/backtest/gate_status.json`**: `{"catatan": <disclaimer>, "strategi": {"PB": {...}, ...}}`. Setiap entri strategi: `lolos`, `syarat` (per G1–G6: `nilai`, `ambang`, `lolos`, `uraian`), `hash_aturan` (= `hash_aturan[X]`), `hash_input`, `rincian_hash` (hash pendek per bagian config dan per berkas input, agar run harian dapat menyebut bagian yang berubah), `tanggal_data_terakhir`, `dibuat_pada`, `holdout_mulai`, `subset_terjangkau_lemah` (§16.7), dan `folder_run` (folder run asal, tempat `sinyal_r_<strategi>.csv` dibaca oleh §17.4). JSON deterministik (kunci terurut, indentasi 2), sehingga dua run identik hanya berbeda pada `dibuat_pada` dan `folder_run` (keduanya memuat waktu run).

**Penulisan:**

- Berkas hanya ditulis jika run memuat mode `edge`.
- Penulisan **menggabungkan per strategi**: strategi yang tidak dijalankan mempertahankan entri lamanya, termasuk hash dan tanggalnya sendiri.

**Run harian** menganggap strategi **tidak lolos** (→ `BAYANGAN_GERBANG`, dengan alasan tertulis) jika:

- berkas atau entri strategi tidak ada → "Jalankan backtest.bat terlebih dahulu";
- `hash_aturan[X]` atau `hash_input` berbeda dari kondisi saat ini → laporan menyebut bagian config atau berkas input yang berubah;
- `t − tanggal_data_terakhir` > `kedaluwarsa_hari` hari kalender; atau
- `lolos: false`.

### 16.7 Diagnostik tambahan (informasi, tidak mengubah gerbang)

- **Jalur per sinyal:** setiap sinyal strategi (semua status kecuali `STOP_TERLALU_JAUH`) disimulasikan sebagai 1 unit independen dengan §15.1, tanpa batas slot atau modal. Hasilnya disimpan di `sinyal_r_<strategi>.csv` dengan kolom status pasar. Ringkasan menampilkan statistik R per sinyal, tidak termasuk hari JANGAN BELI.
  - Satu unit = 1 lembar dengan fee persentase saja (tanpa fee minimum dan pembulatan fee), sehingga R tidak bergantung ukuran posisi.
  - Pembatalan di open berbasis data/harga (BATAL_DATA, BATAL_GAP, BATAL_ARA) tetap berlaku.
  - Sinyal hari terakhir berstatus `MENUNGGU`.
  - Unit yang masih terbuka di akhir dicatat `TERBUKA` dan tidak masuk statistik.
  - Statistik R per sinyal hanya memakai unit `TERISI` yang sudah tertutup; jumlah `BATAL_*`, `MENUNGGU`, dan sinyal hari JANGAN BELI ditampilkan terpisah.
- **Subset terjangkau:** dari transaksi holdout mode `edge`, ambil yang terjangkau menurut aturan `realistis` pada tanggal sinyal: harga nominal, 1 lot ≤ anggaran risiko dan ≤ alokasi maks yang dihitung dari modal mode `realistis` (`backtest.mode.realistis.modal_idr` yang sudah diresolusi), dengan status pasar hari itu. Sizing-nya sama dengan live (§13.2), termasuk `izinkan_satu_lot_melebihi_risiko`. Tampilkan n, PF_R, dan ekspektansi R. Jika PF_R subset < `min_pf_r_holdout` atau n < `min_transaksi_holdout`, `gate_status.json` mencatat `subset_terjangkau_lemah: true`, dan laporan harian menampilkan peringatan **"keunggulan belum terbukti pada saham yang terjangkau modal Anda"**.

### 16.8 Survivorship

- Dengan `universe_history.csv`, eligibilitas per tanggal mengikuti riwayat konstituen. Ringkasan mencantumkan jumlah konstituen historis yang **tidak punya data yang dapat dipakai** (tidak ada di sumber mana pun, atau ditolak validasi §8.2; alasannya di berkas kualitas). Umumnya ini saham delisting. Ini sisa bias yang tidak bisa dihilangkan dengan yfinance.
- Tanpa berkas itu, banner di atas ringkasan berbunyi: **"PERINGATAN SURVIVORSHIP BIAS: backtest memakai daftar universe saat ini untuk seluruh periode. Hasil cenderung terlalu optimis."**
- `breadth_universe` selalu memakai daftar saat ini. Ini juga dicatat sebagai bias.

### 16.9 Uji kewarasan di setiap run

- **ACAK:** jika PF_R holdout ACAK (mode `edge`) > `maks_pf_r_acak`, ringkasan menampilkan peringatan **"Pembanding acak tampak menguntungkan — periksa kemungkinan bug/look-ahead atau periode naik kuat"**.
- **Identitas akuntansi** (gagal = backtest berhenti dengan kode 1):
  - `E_akhir − E_awal = Σ P&L bersih transaksi tertutup + Σ (nilai posisi TERBUKA pada close akhir − biaya_beli-nya)`
  - `kas_akhir = kas_awal − Σ biaya_beli + Σ hasil jual bersih`
  
  P&L bersih = hasil jual setelah fee − biaya beli termasuk fee; slippage sudah tercermin di harga eksekusi. Toleransi: |selisih| ≤ Rp 0,01 × jumlah transaksi.

### 16.10 Keterbatasan (wajib tercetak di setiap ringkasan backtest)

1. **Survivorship bias:** §16.8.
2. **Kualitas data yfinance:** bisa bolong, salah, atau direvisi. Rights issue dan sebagian aksi korporasi tidak disesuaikan.
3. **Dividen diabaikan,** baik di strategi maupun di IHSG.
4. **Urutan intraday tidak diketahui:** jika stop dan target tersentuh di hari yang sama, stop diasumsikan lebih dulu.
5. **Order otomatis diasumsikan terisi** di stop dikurangi slippage tetap. Dalam pasar cepat, kenyataannya bisa lebih buruk.
6. **Gap turun** bisa membuat kerugian melebihi anggaran risiko; backtest menghitungnya, rencana harian tidak bisa mencegahnya.
7. **Order beli** diasumsikan terisi penuh di harga pembukaan dan dibatalkan jika tidak terisi saat pembukaan. Pengisian sebagian dan pengisian belakangan di sesi berjalan tidak dimodelkan.
8. **Fraksi dan ARA/ARB** hanya dimodelkan untuk tanggal ≥ `berlaku_mulai` setelah Anda memverifikasinya. Aturan masa lalu yang berbeda tidak dimodelkan.
9. **Likuiditas di pembukaan** diasumsikan cukup, dengan batas 1% rata-rata nilai harian.
10. **Settlement T+2** diabaikan.
11. **Pemilihan parameter oleh manusia** yang mungkin sudah pernah melihat grafik periode yang sama; lihat juga log holdout.
12. **Sampel kecil**, terutama mode `realistis`.
13. **Asumsi konservatif `DATA_BERAKHIR`** (potongan 50%) untuk saham yang datanya berhenti.
14. **Struktur pasar berubah:** aturan perdagangan, jam sesi, dan fee berubah dari waktu ke waktu.
15. **Hasil masa lalu tidak menjamin hasil masa depan.**

---

## 17. Jurnal kertas (paper trading)

### 17.1 Penyimpanan

SQLite `data/jurnal.sqlite`. Setiap run harian mengekspor setiap tabel ke `output/jurnal/<tabel>.csv` dengan baris pertama `# BUKAN SARAN INVESTASI ...`.

**Ruang harga:** semua harga di jurnal disimpan sebagai **harga nominal pada tanggalnya**. Saat dibandingkan dengan data terkini, nilai itu dikonversi ke ruang tersesuaikan dengan `nominal ÷ F_terkini(tanggal)` (§8.4). Dengan begitu, split yang terjadi kemudian tidak menimbulkan tanda revisi palsu.

| Tabel | Kolom utama |
|---|---|
| `run` | `run_id, waktu_run, tanggal_data, untuk_tanggal, status_data_run, status_pasar, alasan_pasar, hash_aturan, hash_input, versi_program, gerbang_snapshot (JSON per strategi: lolos, hash, tanggal_data_terakhir), snapshot_input (JSON: kode + sektor universe, baris exclude yang aktif), peringatan_data, terlambat, catatan` |
| `sinyal` | `sinyal_id, run_id, tanggal_sinyal, kode, strategi, skor, status_pasar (pada tanggal_sinyal), status_live, alasan_live, lot_live, risiko_live_idr, ditampilkan (0/1), harga_ref, harga_maks, stop_awal, target_harga, rincian_aturan (JSON), bar_saat_sinyal (JSON OHLCV nominal), hash_aturan, susulan (0/1), sementara (0/1), data_direvisi (0/1), tidak_muncul_di_data_final (0/1)`. Kunci unik `(tanggal_sinyal, kode, strategi)`. |
| `hasil_sinyal` | `sinyal_id, status_isi, tanggal_entry, harga_entry, alasan_exit, tanggal_exit, harga_exit, r_bersih, hari, tanda_transaksi` |
| `kertas_periode` | `periode_id, mulai, hash_aturan, modal_awal, selesai` |
| `kertas_transaksi` | `periode_id`, kolom transaksi seperti `transaksi_*.csv` (termasuk kolom `*_nominal`) |
| `kertas_ekuitas` | `periode_id, tanggal, kas, nilai_posisi, ekuitas` |

### 17.2 Tiga keluaran

1. **`sinyal` — catatan apa yang terjadi.**
   - `status_live` selalu berisi hasil §13.2 yang dihitung dengan `portfolio.csv` dan `akun.yaml` saat sinyal diproses.
   - `ditampilkan = 1` hanya untuk sinyal yang muncul di laporan yang benar-benar dibuat untuk tanggal itu. Sinyal susulan atau yang baru muncul setelah data dilengkapi bernilai 0.
   - Baris tidak pernah dihapus diam-diam.
2. **`hasil_sinyal` — jalur per sinyal.** Setiap sinyal (kecuali `STOP_TERLALU_JAUH`) disimulasikan sebagai 1 unit dengan §15.1, tanpa batas modal, dalam satuan R. Simulasi selalu membangkitkan level dari data terkini (deterministik), bukan dari harga tersimpan. Jalur ini mengevaluasi kualitas sinyal, termasuk sinyal bayangan.
3. **`kertas_*` — portofolio kertas.**
   - Mesin backtest `realistis` gabungan, dijalankan dari awal periode kertas sampai `t`, dengan modal mode `realistis`.
   - Eligibilitas per tanggal `d` memakai `snapshot_input` milik run untuk `d`, sama seperti `universe_history.csv` di backtest; `exclude.csv` diterapkan. Perubahan `universe.csv` atau `exclude.csv` di kemudian hari tidak mengubah transaksi kertas masa lalu.
   - Strategi yang diikutkan pada `d` = yang lolos menurut **gerbang yang berlaku pada `d`**: `gerbang_snapshot` dari run terakhir yang terjadi sebelum tanggal `d+1` 00:00 WIB. Jika tidak ada (mis. run pertama), dipakai snapshot run yang memproses `d`, dan diberi tanda.
   - Ini **bukan** portofolio nyata Anda.

**Periode kertas:**

- Periode pertama dimulai pada `jurnal.tanggal_mulai_kertas` (null = tanggal run harian pertama).
- Jika `hash_aturan` global berubah, periode berjalan ditutup dan dibekukan, lalu periode baru dimulai dari tanggal itu dengan modal awal.
- Statistik selalu dilaporkan per periode, sehingga hasil dari aturan yang berbeda tidak tercampur.

### 17.3 Aturan pembaruan

1. **Hari yang diproses.** Setiap run memproses setiap hari bursa `d` dalam (tanggal terakhir yang final, `t`].
   - Untuk `d < t` yang belum pernah diproses (PC mati): sinyal dibangkitkan dengan `DataView(as_of=d)`. Status seleksi (`status_live`) tetap dihitung, tetapi baris dicatat dengan `susulan = 1` dan `ditampilkan = 0`. Sinyal ini tidak pernah ditampilkan sebagai rekomendasi.
   - Laporan menulis "N hari dikejar; M sinyal susulan tidak pernah dilaporkan".
2. **Final atau sementara.** Himpunan sinyal tanggal `d` menjadi **final** jika diproses oleh run berstatus `LENGKAP`, atau setelah `maks_hari_tunggu_data` hari bursa berlalu. Sebelum itu barisnya bertanda `sementara = 1`.
   - Saat dibangkitkan ulang dengan data lebih lengkap, sinyal baru ditambahkan dengan `ditampilkan = 0`.
   - Sinyal yang sudah tercatat dipertahankan. Sinyal yang tidak muncul lagi diberi tanda `tidak_muncul_di_data_final`.
3. **Status `BELUM_TERSEDIA`** tidak menulis sinyal.
4. **Idempoten.** Run ulang untuk `t` yang sudah final tidak menambah baris, dan laporan menulis "sudah tercatat". Laporan run ulang disimpan di `output/laporan/<t>/run_<HHMMSS>/` tanpa menimpa versi sebelumnya; salinan terbaru ada di `output/laporan/<t>/`.
5. **Hitung ulang turunan.** `hasil_sinyal` dan `kertas_*` periode berjalan **dihitung ulang dari awal setiap run** dari data terkini (deterministik). Revisi data otomatis tercermin. Laporan menyebut "K transaksi kertas berubah karena revisi data" jika ada. Order kertas yang bar `d+1`-nya belum tersedia berstatus `MENUNGGU`.
6. **Revisi bar.** Jika `bar_saat_sinyal`, setelah dikonversi ke ruang tersesuaikan, berbeda dari data terkini melewati toleransi, baris diberi tanda `data_direvisi = 1`.
7. **`--catat-ulang`** menghapus baris `sinyal` untuk tanggal yang sedang diproses (`--tanggal`, atau `t`). Penghapusan dicatat di `run.catatan`, lalu sinyal dibangkitkan ulang.
8. **Replay `--tanggal`** tidak menulis ke jurnal, kecuali diberi `--catat`.
9. **`jurnal --rekonsiliasi`** membandingkan sinyal yang ditampilkan live (`ditampilkan = 1`) dengan sinyal yang dibangkitkan ulang dari data terkini dengan config yang sama.
   - Perbedaan (tambah/hilang/level berubah) dilaporkan per baris, dengan petunjuk penyebab: `sementara`, `data_direvisi`, atau hash berbeda.
   - Pada data yang final dan tidak direvisi, selisihnya harus nol.

### 17.4 Pemantauan penyimpangan

**Sampel** per strategi, dari `hasil_sinyal`:

- hanya sinyal dengan `hash_aturan` periode kertas berjalan;
- hanya `tanggal_sinyal` **setelah** `tanggal_data_terakhir` gerbang strategi itu, agar tidak tumpang tindih dengan holdout;
- tidak termasuk `status_pasar = JANGAN_BELI` dan `STOP_TERLALU_JAUH`.

Filter yang sama diterapkan pada `sinyal_r_<strategi>.csv` dari `folder_run` gerbang strategi itu. Kedua sisi tanpa deduplikasi.

**Uji**, jika sampel ≥ `min_transaksi_peringatan`:

1. Ambil `n_kertas` nilai R dengan pengembalian dari distribusi holdout tersebut, sebanyak `bootstrap_iterasi` kali dengan `bootstrap_seed`. Hitung rata-rata setiap sampel.
2. Peringatan muncul jika rata-rata R jurnal < persentil ke-`persentil_peringatan` dari rata-rata bootstrap tersebut.

---

## 18. Laporan harian dan notifikasi

### 18.1 Isi laporan (urutan bagian)

1. **Kepala:** "BUKAN SARAN INVESTASI", tanggal data `t`, untuk hari bursa `t+1`, waktu dibuat (WIB), `hash8`, versi program. Banner bila berlaku: DATA BELUM TERSEDIA, DATA TIDAK LENGKAP, TERLAMBAT / RUN PADA HARI t+1, SPLIT TERDETEKSI, kalender t+1 belum terverifikasi.
2. **Ringkasan aksi** (maksimal 8 baris), mis. "JUAL ABCD di pembukaan", "BELI AAAA 1 lot, limit 2.030 — tarik jika tidak terisi saat pembukaan", "Perbarui stop BO BBBB ke 1.450".
3. **Status pasar:** status + tabel syarat K0–K4.
4. **Status strategi:** lolos/tidak per strategi beserta alasan, tanggal backtest, PF_R holdout, dan peringatan subset terjangkau.
5. **Akun:** kas, nilai posisi, ekuitas, anggaran risiko, alokasi maks, slot kosong.
6. **Posisi dipegang:** saran §15.4 per posisi + level order otomatis untuk `t+1`.
7. **Rencana beli:** §14.3 per `REKOMENDASI`.
8. **Kandidat lain:** status dan alasannya.
9. **Daftar pantau** (saat JANGAN BELI).
10. **Kualitas data:** jumlah simbol OK/SEBAGIAN/GAGAL/basi/anomali/sumber manual, daftar yang dikeluarkan, dan revisi data.
11. **Item yang belum diverifikasi:** `jadwal_sesi`, `fraksi_harga`, `auto_reject`, `biaya`, `pengali_volume`, `holidays.csv` (tahun berjalan dan berikutnya), akun basi.
12. **Jurnal kertas:** posisi kertas terbuka, transaksi tertutup 30 hari terakhir, statistik per strategi vs holdout, peringatan penyimpangan, dan jumlah hari yang dikejar.
13. **Penutup:** disclaimer §18.4, keterbatasan singkat, dan glosarium (HTML).

### 18.2 Format

| Format | Lokasi | Catatan |
|---|---|---|
| Terminal | — | `rich`. Kepala, ringkasan aksi, status pasar, tabel rencana, peringatan. |
| Markdown | `laporan.md` | Semua bagian. |
| HTML | `laporan.html` | Berkas mandiri (CSS inline, grafik PNG base64; tanpa JavaScript/URL eksternal). Candlestick `bar_grafik` bar dengan SMA20/50/200 dan garis harga_maks/stop/target untuk setiap `REKOMENDASI` dan posisi dipegang; grafik IHSG + SMA50/200; breadth 120 hari. |
| CSV | `kandidat.csv` | Semua sinyal hari itu beserta status. Baris pertama: disclaimer. |
| Excel | `rencana.xlsx` | Sheet `Ringkasan`, `Rencana`, `Kandidat`, `Posisi`, `Pasar`, `Kualitas Data`, `Catatan`. Disclaimer di baris 1 setiap sheet. |

Jika berkas keluaran terkunci (sedang dibuka di Excel), program menulis ke `<nama>_<HHMMSS>.<ext>`, memberi peringatan di laporan dan Telegram, lalu melanjutkan run.

### 18.3 Telegram

**Rahasia dan isi:**

- Token dan chat ID dibaca dari `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) lewat `python-dotenv`.
- Pesan memakai parse mode HTML. Semua teks dinamis di-escape dengan `html.escape`, dan hanya tag `<b>`, `<i>`, `<code>`, `<pre>` yang dipakai.
- Isi pesan: status pasar + 1 baris alasan, ringkasan aksi (termasuk pengingat menarik order yang tidak terisi saat pembukaan), rencana beli singkat, perubahan level stop, dan peringatan penting.

**Pengiriman:**

- Pesan dipecah per baris setelah dirakit. Setiap bagian tag-seimbang, **diakhiri baris disclaimer**, dan ≤ 4.000 karakter termasuk disclaimer.
- Balasan 400 (parse) → kirim ulang sebagai teks polos (tag dibuang, entitas di-unescape). Balasan 429 → tunggu `retry_after`, lalu coba sekali lagi.
- `laporan.html` dilampirkan jika `lampirkan_html: true` dan ukurannya ≤ `maks_lampiran_mb`, dengan caption "Laporan <t> — BUKAN SARAN INVESTASI." Jika lebih besar, diganti pesan pemberitahuan.

**Wajib:**

- **Setiap** pesan, termasuk error, libur, DATA BELUM TERSEDIA, dan uji, diakhiri baris "BUKAN SARAN INVESTASI."
- Pesan error dikirim untuk kode keluar 1, 2, 3, 5, dan 6 dalam mode `--jadwal`.
- Kegagalan kirim dicatat di log dan laporan.
- `uji-telegram` mengirim pesan uji; `--cari-chat-id` mencetak chat ID dari `getUpdates`.

### 18.4 Teks disclaimer (wajib di semua keluaran)

> BUKAN SARAN INVESTASI. Laporan ini dibuat otomatis oleh program pribadi berdasarkan aturan mekanis dan data publik yang bisa salah, terlambat, atau tidak lengkap. Hasil backtest dan paper trading tidak menjamin hasil di masa depan. Seluruh keputusan dan risiko transaksi menjadi tanggung jawab pengguna.

---

## 19. Operasional di Windows

### 19.1 Instalasi (README memuat langkah dengan tangkapan layar)

1. Pasang Python 3.12 atau 3.13 (64-bit) dari python.org. Centang "Add python.exe to PATH" dan "py launcher".
2. Ekstrak `asisten-bei` ke folder dengan jalur sederhana, mis. `C:\asisten-bei`. **Jangan** di dalam OneDrive/Dropbox, karena sinkronisasi bisa mengunci `jurnal.sqlite` dan cache.
3. Klik ganda `scripts\pasang.bat`. Skrip ini memakai `py -3.12` (atau `-3.13`), mencetak versinya, berhenti dengan pesan jelas bila Python tidak ditemukan, lalu membuat `.venv` dan memasang `requirements.txt`.
4. Isi berkas di `input\` dengan Notepad (§6), lalu tinjau `config.yaml` (§24).
5. `scripts\cek.bat` → perbaiki semua ERROR.
6. `scripts\unduh.bat`. Unduhan pertama bisa lama.
7. `scripts\backtest.bat` → baca ringkasannya.
8. Siapkan Telegram (§19.4) → `scripts\uji_telegram.bat`.
9. `scripts\harian.bat` sekali secara manual.
10. Daftarkan jadwal dengan klik ganda `scripts\daftar_jadwal.bat` (§19.3).

**Dependensi:** pandas, numpy, yfinance, pyarrow, pyyaml, pydantic, python-dotenv, requests, jinja2, matplotlib, mplfinance, openpyxl, rich, filelock, **tzdata** (wajib; Windows tidak punya basis data zona waktu IANA), dan pytest.

- Versi dipin di `requirements.txt` ke versi yang diuji saat implementasi.
- Upgrade yfinance hanya setelah uji cepat §22.3.

**Konvensi berkas `.bat`:**

- Dimulai dengan `cd /d "%~dp0.."`.
- Memanggil `".venv\Scripts\python.exe" -m asisten_bei ...` langsung, tanpa `activate`, dengan `set PYTHONUTF8=1`.
- Menyimpan `set KODE=%ERRORLEVEL%`, lalu `pause` (hanya untuk mode manual), lalu `exit /b %KODE%`.
- `harian_terjadwal.bat` menjalankan `if not exist "output\log" mkdir "output\log"` sebelum apa pun, lalu menambahkan `>> "output\log\terjadwal.log" 2>&1` dan menampilkan "Jangan tutup jendela ini".
- Semua path dikutip.
- `config.py` menyelesaikan path relatif terhadap `--akar` (default: folder berisi `config.yaml`), bukan terhadap direktori kerja.

### 19.2 Perintah CLI

**Opsi global** (semua perintah): `--config PATH` (default `config.yaml` di folder kerja; berkas `.bat` selalu menjalankan dari folder proyek, §19.1) dan `--akar DIR` (dasar semua path relatif di config).

| Perintah | Fungsi |
|---|---|
| `cek [--silang KODE TANGGAL]` | Validasi config + semua input. Mencantumkan item belum diverifikasi, `holidays.csv`, `holdout_mulai`, `.env` (termasuk deteksi `.env.txt` dan PERINGATAN bila token masih nilai contoh `CONTOH_FIKTIF` dari `contoh/env.contoh`), dan perbandingan kalender. `--silang` mencetak bar cache, `F(d)`, dan nilai nominal untuk dicocokkan dengan sumber resmi. |
| `unduh [--penuh] [--simbol K1,K2]` | Memperbarui cache (§8.2). |
| `backtest [--strategi ...] [--mode ...]` | §16 |
| `harian [--jadwal] [--offline] [--tanggal D] [--tanpa-notifikasi] [--catat] [--catat-ulang]` | §18 |
| `jurnal [--ekspor] [--rekonsiliasi]` | §17 |
| `uji-telegram [--cari-chat-id]` | §18.3 |

**Semua keluaran terminal**, termasuk `--help`, `--versi`, dan pesan error, memuat "BUKAN SARAN INVESTASI". Bila konsol tidak memakai UTF-8, karakter yang tidak bisa ditampilkan diganti `?` dan program tidak berhenti.

**`cek`** hanya membaca: tidak mengunduh, tidak memegang kunci, dan tidak menulis apa pun kecuali log. Isinya, berurutan:
1. config (ERROR menghentikan pemeriksaan);
2. input `input/` (ERROR/PERINGATAN per baris), termasuk tanggal portofolio terhadap kalender dan umur `akun.yaml`;
3. item **BELUM DIVERIFIKASI** (`pasar.jadwal_sesi`, `pasar.fraksi_harga`, `pasar.auto_reject`, `biaya`, `data.pengali_volume`), beserta sumber dan tanggal berlaku item yang sudah diverifikasi;
4. `holidays.csv` (tahun berjalan/berikutnya) dan perbandingan kalender manual vs turunan dari cache breadth (dilewati dengan catatan bila cache belum ada);
5. `holdout_mulai` terhadap baris pertama `holdout_log.csv`;
6. `.env` (berkas yang bukan teks UTF-8 → ERROR yang menunjuk `.env`);
7. resolusi rujukan per konteks (live, edge, realistis).

Kode keluar 3 bila ada ERROR, selain itu 0.

**`cek --silang KODE TANGGAL`** mencetak bar seri terpilih (§8.2) pada TANGGAL:
- OHLCV cache (tersesuaikan), sumber, tanda bar;
- `F(d)` dan split sesudahnya;
- OHLC nominal (× `F`), volume nominal (÷ `F`) dikali `pengali_volume`, dan nilai transaksi.

TANGGAL harus ≤ `t`. Tanpa bar pada TANGGAL, `cek --silang` mengatakannya, tanpa mencari tanggal terdekat.

**`unduh`** memegang kunci (§19.3) dan memperbarui cache provider jaringan untuk semua simbol §8.2. `--simbol` membatasi ke kode tertentu; `IHSG` boleh disebut. Sesudahnya `unduh`:
- menulis berkas kualitas `output/kualitas/<YYYYMMDD-HHMMSS>_unduh.csv`, dinilai as-of `t` (§8.6) atas seri terpilih §8.2;
- mencetak ringkasan: jumlah per `status_ambil`; setiap simbol yang unduhannya gagal beserta pesan aslinya, termasuk yang tertolong cadangan; setiap seri yang tidak OK beserta bar terakhirnya; setiap unduh ulang penuh yang tidak diminta beserta alasannya (split belum disesuaikan, revisi, split baru); peringatan blokir Yahoo; dan saran versi yfinance (≥ 90% `FORMAT`).
- Kolom `pesan` berkas kualitas memuat pesan error asli dan catatan pembaruan run itu (revisi volume, unduh ulang penuh dan alasannya, bar yang dihapus) serta catatan sumber (cadangan, tanpa berkas split).
- Log mencatat setiap langkah beserta durasinya, dan sumber terpilih per simbol.

Kegagalan per simbol tidak mengubah kode keluar: 0, kecuali config/input tidak valid (3), kunci dipegang (5), atau error tak terduga (1).

Perintah `backtest`, `harian`, `jurnal`, dan `uji-telegram` baru tersedia pada tahap 5–7 (§23). Sebelum itu, perintah tersebut mencetak "belum tersedia" dan keluar dengan kode 1.

Kode keluar dan prioritasnya ada di §20.

### 19.3 Task Scheduler

`scripts\daftar_jadwal.bat` menjalankan `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0daftar_jadwal.ps1"`, tanpa mengubah kebijakan sistem. Skrip ini membuat tugas **"Asisten BEI Harian"**:

- Pemicu Senin–Jumat pada `jadwal.jam_run` (19:30) **WIB**. Skrip mengonversinya ke jam lokal Windows saat pendaftaran dan mencetak hasilnya. Jika zona waktu Windows bukan WIB, skrip memberi peringatan. Program sendiri tetap aman, karena §8.6 menunggu sampai batas bar final.
- `New-ScheduledTaskAction -Execute cmd.exe -Argument '/c "<path absolut>\scripts\harian_terjadwal.bat"' -WorkingDirectory <root proyek>`
- `New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours <batas_waktu_run_jam>) -MultipleInstances IgnoreNew`
- Principal `-LogonType Interactive` untuk pengguna saat ini, tanpa menyimpan kata sandi.
- Hasil diverifikasi dengan `Get-ScheduledTask`, lalu ringkasannya dicetak.
- README menjelaskan opsi "Wake the computer to run this task". Opsional: pemicu kedua pada jam yang Anda pilih sendiri sebelum pra-pembukaan, yang hanya memproses jika run untuk `t` belum `LENGKAP`.

**Perilaku `harian --jadwal`:**

- Tentukan `t` (§8.6). Jika tabel `run` sudah memuat run `LENGKAP` untuk `t`, keluar dengan kode 0 ("tidak ada data baru"; pesan hanya jika `kirim_pesan_saat_libur`).
- Jika belum, proses `t`, **walaupun hari ini akhir pekan atau libur** (run susulan), dengan banner keterlambatan sesuai §8.7. Tidak pernah ada analisis bertanggal hari libur.

**Kunci proses:**

- Semua perintah yang menulis (`unduh`, `backtest`, `harian`, `jurnal`) memegang kunci tingkat OS pada `data/.kunci` lewat paket `filelock`. Kunci ini lintas platform dan otomatis lepas jika proses mati.
- Setelah kunci didapat, PID, perintah, dan waktu mulai ditulis ke berkas terpisah `data/.kunci.info`, yang tidak dikunci sehingga bisa dibaca proses lain.
- Jika kunci sedang dipegang, run keluar dengan kode 5 ("proses lain sedang berjalan: <perintah> sejak <waktu>", dibaca dari `.kunci.info`), dan dalam mode `--jadwal` mengirim Telegram.

### 19.4 Telegram (README memuat langkah lengkap)

1. Buat bot lewat @BotFather dan salin token.
2. Kirim pesan apa pun ke bot Anda.
3. Jalankan `uji-telegram --cari-chat-id` untuk mendapatkan chat ID.
4. Isi `.env`. Simpan sebagai `.env`, bukan `.env.txt`; `cek` mendeteksi kesalahan ini.
5. Jalankan `uji_telegram.bat`.

### 19.5 Log

- `output/log/YYYY-MM-DD.log` mencatat setiap langkah, durasi, simbol yang gagal beserta pesan error aslinya, dan sumber data per simbol. `output/log/terjadwal.log` menangkap keluaran konsol run terjadwal.
- **Penyaring rahasia** mengganti nilai `TELEGRAM_BOT_TOKEN` dengan `***`, termasuk di teks exception yang memuat URL API. Penyaring dipasang di:
  - semua handler log, teks laporan, dan pesan Telegram;
  - `sys.stdout`, `sys.stderr`, dan `sys.excepthook`, sebagai langkah **pertama** di `__main__.py`, agar keluaran konsol yang tertangkap di `terjadwal.log` juga tersensor.
- Log lebih tua dari `simpan_log_hari` dihapus.

### 19.6 Rutinitas harian (juga di README)

| Kapan | Yang dilakukan |
|---|---|
| Malam (setelah laporan ±19:30) | Baca ringkasan aksi. Siapkan order untuk besok. |
| Pagi `t+1`, sebelum pra-pembukaan | Pasang order BELI limit di `harga_maks` sesuai laporan. Pasang order JUAL untuk saran "JUAL di pembukaan" dengan cara yang membuat order terisi di harga pembukaan (tergantung fitur broker, §24). |
| Segera setelah pembukaan | Cek setiap order beli. Tidak terisi → **tarik**. Terisi sebagian → tarik sisanya. Terisi → **pasang order otomatis stop (dan target untuk PB) hari itu juga**. Jika harga isi ≤ stop → jual segera. |
| Setelah ada transaksi | Perbarui `portfolio.csv` dan `akun.yaml` (contoh di bawah). |
| Setiap sore, posisi BO | Perbarui level order otomatis stop sesuai laporan. |

**Contoh dengan angka fiktif:**

- **Beli terisi:** 1 lot `AAAA` di 2.010, strategi PB, stop 1.888.
  - Tambah baris di `portfolio.csv`: `AAAA,2026-10-16,2010,1,PB,1888,,,,`
  - Kurangi kas di `akun.yaml` sebesar 2.010 × 100 × 1,0015 = **201.301,50**, lalu ubah `diperbarui`.
  - Target = 2.010 + 2 × (2.010 − 1.888) = **2.254**.
- **Jual 1 lot di 2.254:**
  - Hapus baris dari `portfolio.csv`.
  - Tambah kas sebesar 225.400 × (1 − 0,0025) = **224.836,50**.

Jika broker membulatkan fee berbeda, pakai angka dari trade confirmation.

---

## 20. Penanganan kegagalan dan kode keluar

### 20.1 Kode keluar

| Kode | Arti |
|---|---|
| 0 | OK, termasuk "tidak ada data baru" dan `cek` yang hanya berisi peringatan |
| 1 | Error tak terduga, atau identitas akuntansi backtest gagal |
| 2 | Data `BELUM_TERSEDIA`, atau `TIDAK_LENGKAP` karena IHSG/K0; backtest tanpa data (§16.1) |
| 3 | Config/input tidak valid (`cek` dengan ERROR) |
| 4 | Laporan dibuat, tetapi notifikasi gagal (tidak pernah muncul jika `notifikasi.telegram.aktif: false`) |
| 5 | Proses lain sedang berjalan (kunci) |
| 6 | Jurnal gagal (terkunci/rusak); laporan tetap dibuat dan dikirim |

**Prioritas jika beberapa kondisi terjadi bersamaan:** 3 > 1 > 5 > 2 > 6 > 4 > 0.

`jurnal --rekonsiliasi` yang menemukan selisih tetap keluar dengan kode 0; selisihnya dilaporkan sebagai informasi.

### 20.2 Situasi

| Situasi | Perilaku |
|---|---|
| Internet putus / semua simbol gagal | Coba CSV manual. Jika bar `t` tetap tidak ada: §8.6 (`BELUM_TERSEDIA`, kode 2, Telegram). **Tidak pernah** memakai data lama seolah data `t`. |
| Yahoo membatasi/memblokir | §8.1: hentikan setelah 3 simbol berturut-turut kena batas, pakai cache/CSV, laporan menulis "diblokir sementara oleh Yahoo". |
| Sebagian simbol gagal | Simbol tersebut dikeluarkan dan dilaporkan dengan pesan error aslinya. |
| Format yfinance berubah | Seri GAGAL (`FORMAT`) → CSV manual. Laporan menyarankan memeriksa versi yfinance. |
| Data IHSG gagal tetapi saham OK | `TIDAK_LENGKAP`, JANGAN BELI (K0), saran exit dan daftar pantau tetap ada, kode 2. |
| Kemungkinan libur yang tidak tercatat | §8.6: petunjuk "mungkin hari libur yang belum ada di holidays.csv". |
| Config/input tidak valid | Kode 3, pesan yang menunjuk baris/kunci, Telegram error. Tidak ada laporan parsial yang menyesatkan. |
| CSV disimpan Excel berlokal Indonesia | `cek` menolak dengan pesan khusus §6. |
| `gate_status.json` tidak ada / basi / hash berbeda | Strategi terkait `BAYANGAN_GERBANG`, instruksi "jalankan backtest.bat". |
| `holidays.csv` belum berisi tahun berjalan/berikutnya | Peringatan di setiap laporan (§6.7). |
| Saham di `portfolio.csv` tanpa data `t` | TIDAK DAPAT DIEVALUASI; nilai = close valid terakhir; ekuitas ditandai "≈". |
| Lonjakan sejak `tanggal_beli` tanpa data split (§15.3) | TIDAK DAPAT DIEVALUASI; semua sinyal `DIBLOKIR_PORTOFOLIO` (§13.2 langkah 6a). |
| Cache parquet rusak | Diunduh ulang penuh. |
| `jurnal.sqlite` terkunci/rusak | Laporan tetap dibuat dan dikirim; bagian jurnal diganti peringatan; kode 6. |
| Berkas keluaran terkunci (dibuka di Excel) | Tulis ke nama alternatif (§18.2), peringatan, run berlanjut. |
| Gagal impor / venv rusak sebelum logging aktif | Tercatat di `output\log\terjadwal.log` (§19.1). |
| Kunci dipegang proses lain | Kode 5 + Telegram (mode `--jadwal`). |
| Disk penuh | Kode 1 + Telegram jika memungkinkan. |

---

## 21. Di luar cakupan

- Eksekusi order otomatis atau integrasi API broker apa pun.
- Data intraday, sinyal intraday, dan peringatan real-time selama jam bursa.
- Pemodelan pengisian order sebagian atau pengisian belakangan di sesi berjalan.
- Short selling, margin, derivatif, waran, right, ETF, obligasi, reksa dana.
- Analisis fundamental, berita, sentimen, foreign flow, broker summary, bandarmologi.
- Pengambilan otomatis daftar LQ45, status suspensi/notasi khusus, kalender libur, jadwal sesi, dan aturan fraksi/ARA/ARB dari idx.co.id. Semuanya diisi manual.
- Optimasi parameter otomatis atau machine learning (keputusan: parameter tetap).
- Penyesuaian dividen dan rights issue di luar yang disediakan sumber data.
- Pelaporan pajak, rekonsiliasi dengan laporan broker, multi-akun/multi-pengguna.
- GUI atau aplikasi web. Antarmuka hanya CLI, berkas `.bat`, berkas laporan, dan Telegram.
- Sinkronisasi otomatis `portfolio.csv`/`akun.yaml` dengan akun broker.
- Skrip penjadwalan untuk macOS/Linux. Kode tetap lintas platform (pathlib, zoneinfo).

---

## 22. Verifikasi end-to-end

### 22.1 Uji otomatis (pytest, offline, deterministik)

`scripts\uji.bat` (= `pytest -q`) harus lulus 100% sebelum rilis dan setelah setiap perubahan.

**Isolasi dan determinisme:**

- **Waktu:** "sekarang" diambil dari `waktu.py`, yang membaca variabel lingkungan `ASISTEN_BEI_WAKTU_SEKARANG` (ISO, WIB) bila ada. Semua uji wajib menyetelnya. Tahun berjalan, umur akun, umur gerbang, dan keterlambatan dihitung terhadap waktu ini.
- **Berkas:** uji menyalin `tests/fixtures/` ke direktori sementara pytest dan menjalankan perintah dengan `--config <tmp>/config_uji.yaml --akar <tmp>`. Berkas pengguna tidak pernah disentuh.
- **Perbandingan acuan:**
  - md dan csv sebagai teks, setelah menormalkan stempel waktu;
  - xlsx berdasarkan nilai sel per sheet (openpyxl);
  - html berdasarkan teks setelah gambar base64 dibuang, ditambah jumlah gambar.
- **Byte-identik:** `transaksi_*.csv`, `ekuitas_*.csv`, dan `gate_status.json` (tanpa `dibuat_pada` dan `folder_run`) dari dua run yang sama harus byte-identik.

| Kode | Uji | Kriteria lulus |
|---|---|---|
| T-CFG | Config | Kunci hilang / tak dikenal → error; `skor.bobot` ≠ 100 → error; persen di luar rentang → error; `versi_config` ≠ 1 → error. Resolusi `ikuti_akun`/`ikuti_risiko`/`ikuti_maks_posisi` berantai per mode (edge: alokasi = ekuitas ÷ 3). Untuk setiap kunci di `strategi.*`, `skor.*`, dan `kondisi_pasar.*`, uji berparameter memastikan mengubah nilainya mengubah keluaran fixture. Ini menegakkan P4. |
| T-IN | Indikator | SMA, ATR Wilder (n dari config), RSI Wilder (termasuk avg_turun = 0 dan keduanya 0), HHV/LLV (batas inklusif/eksklusif), HHV_sejak_entry, CLV (high = low), RasioVol, dan ATR ≤ 0 cocok dengan hitung tangan hingga 1e-9. Bar tanpa transaksi tidak masuk perhitungan. |
| T-LA1 | Potong vs penuh | Untuk ≥ 200 pasangan (saham, tanggal) acak berseed: sinyal, skor, status pasar, eligibilitas, stop, dan harga_maks dari data dipotong ≤ `t` **identik** dengan hasil dari data penuh. Dibagi menjadi T-LA1a (indikator, eligibilitas, status pasar; tahap 3) dan T-LA1b (sinyal, skor, stop, harga_maks; tahap 4). |
| T-LA2 | Gangguan masa depan | Mengganti semua bar setelah `t` dengan derau acak, **termasuk lonjakan > `lonjakan_mencurigakan_pct` dan bar hilang**, tidak mengubah satu pun keputusan mesin ≤ `t` (order, fill, exit, ekuitas, eligibilitas, breadth). |
| T-LA3 | Uji jebakan | Indikator kanari tervektor yang memakai `shift(-1)` **harus** membuat T-LA1 gagal. Ini membuktikan T-LA1 bekerja. |
| T-KL | Kalender | Akhir pekan; libur di `holidays.csv`; libur panjang; `t` sebelum/sesudah batas bar final (terverifikasi dan tidak); banner TERLAMBAT / RUN PADA HARI t+1; peringatan tahun berjalan/berikutnya; banner t+1 tanpa kalender; kalender turunan dari breadth; prioritas kalender manual; run Jumat terlewat yang dijalankan Sabtu → laporan `t` = Jumat; run di hari libur saat `t` sudah `LENGKAP` → kode 0; `--jadwal` yang mulai sebelum batas bar final di hari bursa menunggu, bukan keluar. |
| T-DQ | Kualitas data | Fixture dengan NaN, duplikat (terakhir identik / berbeda / di tengah), high < low, bar hari libur historis bervolume 0 (dibuang, tidak memengaruhi indikator dan hitungan volume 0), bar akhir pekan → GAGAL, lonjakan 60% tanpa split, split yang belum disesuaikan sumber, volume 0 beruntun, bar hilang, dan IHSG tanpa volume. Semua terdeteksi sesuai §8.5. **Tidak ada baris yang dibuat atau diisi.** Mock yfinance memeriksa `end = akhir + 1 hari` dan `repair=False`; index `2026-10-12 00:00+07:00` → 2026-10-12. Klasifikasi error dan penghentian setelah 3× batas permintaan juga diuji. |
| T-CA | Cache | Update inkremental hanya mengunduh bar baru + 10 bar cek ulang. Bar tumpang tindih selalu ditimpa. Revisi close > toleransi → unduh ulang penuh. Revisi volume dicatat. Bar hari ini sebelum batas final tidak tersimpan. Penulisan atomik. Parquet rusak → unduh ulang. |
| T-SP | Split | `F(d)` benar. Sizing nominal pada saham fiktif dengan split 1:5. Posisi yang melewati split tidak mengubah ekuitas, P&L Rp sama dengan hitung tangan nominal, dan tidak memicu saran JUAL palsu (live). `harga_per_tanggal` mencegah penyesuaian ganda. |
| T-BEI | Aturan BEI | Belum terverifikasi → pembulatan rupiah bulat dengan arah §4.1 + peringatan. Tabel fiktif (berlabel "FIKTIF – KHUSUS UJI", bukan aturan nyata) → pembulatan benar di batas rentang dan hanya untuk tanggal ≥ `berlaku_mulai`. Tabel tumpang tindih/berlubang → error. ARB terkunci hanya dimodelkan bila terverifikasi. |
| T-KP | Kondisi pasar | Fixture untuk setiap cabang K0–K4, termasuk penyebut cakupan yang mengabaikan saham belum tercatat, `min_saham_breadth`, dan IHSG tanpa volume. |
| T-UN | Universe | Ambang likuiditas tepat di batas (≥ lolos); exclude dengan tanggal kosong/berbatas di tepi rentang; `universe_history` inklusif kedua ujung dan keluar-masuk ulang; sektor `TIDAK_DIKETAHUI:<KODE>`; backtest mengabaikan exclude kecuali `pakai_exclude`. |
| T-ST | Strategi | Untuk setiap aturan PB1–PB5, BO1–BO5, dan MR1–MR3: fixture yang lulus semua aturan, lalu satu varian per aturan yang hanya melanggar aturan itu → sinyal hilang dengan alasan yang benar. `STOP_TERLALU_JAUH`. ACAK deterministik dengan rumus §12.4. |
| T-SK | Skor & seleksi | Nilai skor cocok dengan hitung tangan, termasuk `K_rs` dengan n = 1 dan nilai sama. Urutan §13.2 langkah 1–9 lengkap: bayangan gerbang tidak menggugurkan sinyal yang lolos; `SUDAH_DIPEGANG`; slot dibebaskan jual terjadwal; CEK ORDER tetap terisi; sektor posisi `MANUAL` dan di luar universe; `TIDAK_LIKUID`; `izinkan_satu_lot_melebihi_risiko`. |
| T-SZ | Sizing | Contoh §14.2 persis: A = 1 lot (risiko 15.164,83); B = TIDAK_TERJANGKAU (alasan risiko, tampil "Rp 66.841,63"); C = 5 lot (risiko 25.060,49); HATI-HATI A = TIDAK_TERJANGKAU, C = 2 lot. Fee minimum fiktif → A = 1 lot (risiko 24.388,80). Batas pembulatan → 3 lot. Alokasi maks = 1.000.000 tepat. Kasus fiktif `m` = 5.000 dengan P = 9.950: biaya_beli(1) = 1.000.000 → `lot_alokasi` = 1. |
| T-BIAYA | Fee | Contoh §14.2: P&L bersih Rp 27.517 (`pembulatan_fee: null`). Dengan `rupiah_terdekat` (ROUND_HALF_UP) atau `rupiah_ke_atas`: fee 305 dan 579, P&L Rp 27.516. |
| T-EX | Exit | Setiap pembatalan di open: BATAL_DATA (bar tidak ada / volume 0 / rusak), BATAL_GAP, BATAL_ARA (terverifikasi, fixture fiktif), BATAL_SLOT_SAAT_OPEN dan BATAL_SEKTOR_SAAT_OPEN (jual terjadwal tertunda), BATAL_KAS_SAAT_OPEN; open ≤ stop di hari entry → `STOP_GAP_HARI_ENTRY` dengan R terhadap risiko rencana; open = harga_maks → harga isi = harga_maks dan `risiko_awal_idr ≤ anggaran`; low ≤ stop → keluar di stop × (1 − slip); gap di bawah stop posisi lama → open; stop dan target di hari yang sama → stop; open ≥ target → `TARGET_GAP`; trailing BO 5 hari cocok dengan hitung tangan dan tidak pernah turun; MR keluar SMA5 hanya mulai hari ke-2; stop waktu hari ke-10/ke-5; bar hilang/tanpa transaksi menunda jual; ARB terkunci (terverifikasi) menunda jual; stop waktu pada hari ke-N tanpa bar → dievaluasi pada bar valid berikutnya; `DATA_BERAKHIR` diputuskan as-of setelah 60 hari tanpa bar; prioritas label; `stop_aktif_hari_entry: false`. |
| T-PF | Saran posisi | Urutan §15.4 #1–#7, termasuk: exit sebelum `t`; jual terjadwal di pembukaan `t` (KELUAR_SMA dari close `t−1`) → #4, bukan TAHAN; exit order otomatis pada `t` → CEK_ORDER; hari entry dengan `stop_aktif_hari_entry: false` tidak memicu CEK_ORDER; `stop_awal ≥ harga_beli` → JUAL tanpa ERROR; MANUAL di bawah stop (tetap info); `DIBLOKIR_PORTOFOLIO`; split dengan dan tanpa `harga_per_tanggal`; pengingat konsistensi. |
| T-BT | Mesin backtest | Kas tidak pernah negatif; slot dan sektor tidak pernah terlampaui; harga isi beli ≤ harga_maks; identitas akuntansi §16.9; anomali as-of (tanda `ANOMALI`); `MELEWATI_HOLDOUT`; hasil byte-identik. |
| T-MT | Metrik | Kurva ekuitas sintetis dengan CAGR dan MaxDD yang diketahui; daftar transaksi dengan win rate, PF, PF_R, dan max drawdown R yang diketahui; PF tanpa kerugian → "n/a"; pembagian periode berdasarkan tanggal entry; titik awal pembanding IHSG. |
| T-GB | Gerbang | Setiap syarat G1–G6 gagal satu per satu → `lolos: false` dengan alasan tepat. G5 tanpa jendela yang memenuhi syarat → gagal. Penggabungan per strategi. Mengubah `indikator.atr_periode`, `gerbang.min_pf_r_holdout`, isi `universe.csv` atau `holidays.csv`, atau memverifikasi `auto_reject` → `BAYANGAN_GERBANG`. Mengubah `strategi.bo` tidak membatalkan gerbang PB/MR. Mengisi `pasar.jadwal_sesi`, `sumber`/`tanggal_cek`, atau `biaya.terverifikasi` tidak mengubah hash. Umur > kedaluwarsa. Log holdout dan peringatan M > 1. Subset terjangkau. |
| T-JR | Jurnal | Run ganda untuk `t` final idempoten. Hari terlewat dikejar dengan `susulan = 1`, `ditampilkan = 0`, dan `status_live` tetap terisi. Run `TIDAK_LENGKAP` lalu `LENGKAP` → sinyal baru ditambahkan, yang lama dipertahankan. Split setelah sinyal tidak memunculkan `data_direvisi` palsu. Perubahan `universe.csv` tidak mengubah transaksi kertas masa lalu (`snapshot_input`). Gerbang yang berlaku untuk hari susulan diambil dari run sebelum hari itu. `BELUM_TERSEDIA` tidak menulis. Hitung ulang turunan setelah revisi data. Periode kertas baru saat `hash_aturan` berubah. `--rekonsiliasi` pada fixture = nol selisih. Bootstrap deterministik. |
| T-LP | Laporan | Setiap keluaran (terminal semua perintah, md, html, csv, setiap sheet xlsx, setiap keluaran backtest termasuk JSON/YAML/`holdout_log.csv`, berkas kualitas, ekspor jurnal, setiap bagian pesan Telegram dan caption lampiran) memuat "BUKAN SARAN INVESTASI". HTML mandiri. Instruksi pembulatan dan instruksi menarik order muncul. Berkas terkunci → nama alternatif. |
| T-TG | Telegram | Alasan berisi `<`, `>`, `&` terkirim (mock memvalidasi HTML); pemecahan tag-seimbang ≤ 4.000; 400 → teks polos; 429 → tunggu lalu coba lagi; lampiran terlalu besar; exception mock dengan URL ber-token tidak memunculkan token di log. |
| T-OP | Operasional | Kunci OS: run kedua → kode 5 dengan pesan dari `.kunci.info`; kunci lepas setelah proses dimatikan. Prioritas kode keluar. `.env.txt` terdeteksi. Token tersensor di stdout/stderr/traceback. |
| T-INP | Input | BOM diterima; berkas berpemisah `;`, berkoma desimal, atau bertanggal `08/10/2026` → ERROR dengan pesan khusus; kolom `sektor` portfolio wajib bila di luar universe. |
| T-E2E | End-to-end offline | §22.2 |

### 22.2 Skenario end-to-end offline

**Fixture.** `tests/fixtures/buat_fixture.py` (berseed tetap, ikut di-commit) membangkitkan `pasar_sintetis/` dalam format cache §8.3. Konstanta tanggal `T1`, `T_TURUN`, `T_KOSONG` didefinisikan di skrip. `config_uji.yaml` memakai `holdout_mulai` dan ambang gerbang yang longgar (mis. `min_transaksi_holdout: 1`), dan skrip menjamin PB, BO, dan MR lolos gerbang pada data ini.

| Saham | Desain | Hasil yang diharapkan |
|---|---|---|
| `IHSG` | Periode naik (memuat `T1`), transisi, turun (memuat `T_TURUN`); tanpa volume | Ketiga status pasar muncul pada tanggal yang diketahui |
| ±40 saham breadth | Sebagian baru "melantai" di tengah periode | Penyebut cakupan diuji |
| `UJIPB` | Memicu PB pada `T1`, lalu kena target | `REKOMENDASI` → `TERISI` → `TARGET` |
| `UJIBO` | Breakout bervolume, lalu trailing; dipakai di `portfolio_ujibo.csv` | TAHAN + level trailing sesuai hitung tangan |
| `UJIMR` | RSI2 jatuh, lalu keluar SMA5 pada hari ke-3 | `KELUAR_SMA` |
| `UJIMAHAL` | Harga ± 9.000, sinyal PB pada `T1` dengan jarak stop ≥ 5% | `TIDAK_TERJANGKAU` (alasan risiko) |
| `UJIMAHAL2` | `harga_maks` tepat 10.500, sinyal pada `T1` dengan jarak stop 2% (risiko 1 lot 26.173,93 ≤ 30.000) | `TIDAK_TERJANGKAU` (alasan alokasi; 1 lot = 1.051.575) |
| `UJISEKTOR` | Sektor sama dengan `UJIPB`, sinyal pada `T1` dengan skor **lebih rendah** | `BATAS_SEKTOR` |
| `UJIGAP` | Sinyal pada `T1`, open `T1+1` > harga_maks | `BATAL_GAP` |
| `UJIBOLONG` | Bar hilang > `maks_bar_hilang` di sekitar `T1` | Dikeluarkan |
| `UJIANOMALI` | Lonjakan tanpa split **setelah** `T1` | Eligible pada `T1`; dikeluarkan setelah tanggal lonjakan |
| `UJISPLIT` | Split 1:5 di tengah periode, sudah disesuaikan | Sizing nominal benar di backtest |

**Langkah.** Semua langkah memakai `--config <tmp>/config_uji.yaml --akar <tmp>` dan `ASISTEN_BEI_WAKTU_SEKARANG` = pukul 20:00 WIB pada tanggal yang diproses. Langkah 2–9 memakai `harian --offline --tanpa-notifikasi --catat --tanggal <tanggal>` kecuali disebut lain.

1. `backtest` → `gate_status.json` dibuat. Ringkasan memuat semua bagian §16, termasuk keterbatasan, banner survivorship, dan diagnostik.
2. `harian --offline --tanggal T1 --tanpa-notifikasi --catat`, dengan portofolio kosong. Hasilnya:
   - status BOLEH BELI dengan alasan;
   - `UJIPB` = `REKOMENDASI` dengan lot/stop/target persis sesuai acuan;
   - `UJIMAHAL` dan `UJIMAHAL2` = `TIDAK_TERJANGKAU` dengan alasan masing-masing;
   - `UJISEKTOR` = `BATAS_SEKTOR`;
   - `UJIBOLONG` dikeluarkan;
   - kelima berkas laporan cocok dengan `tests/fixtures/acuan/`.
3. Ulangi langkah 2 → jurnal tidak berubah ("sudah tercatat").
4. `--tanggal T1+1` → `UJIPB` `TERISI` di open `T1+1`; `UJIGAP` `BATAL_GAP`.
5. Lompat ke `--tanggal T1+5` (simulasi PC mati 3 hari) → jurnal mengejar: sinyal susulan bertanda `susulan = 1`, `ditampilkan = 0`; `UJIPB` `TARGET` pada tanggal yang diharapkan.
6. Dengan `--config <tmp>/config_uji_ujibo.yaml` (memakai `portfolio_ujibo.csv`, `tanggal_beli` ≤ `t`), tanpa `--catat` → laporan menampilkan TAHAN + level trailing baru yang cocok dengan hitung tangan.
7. `--tanggal T_TURUN` → JANGAN BELI, nol `REKOMENDASI`, daftar pantau ada, sinyal `BAYANGAN_PASAR` tercatat.
8. Hapus `gate_status.json` → semua `BAYANGAN_GERBANG`, nol rekomendasi, instruksi menjalankan backtest.
9. `--tanggal T_KOSONG` (cache tanpa bar `T_KOSONG` untuk sebagian besar saham) → kode 2, banner DATA BELUM TERSEDIA, saran exit tetap ada untuk posisi yang barnya ada.
10. `jurnal --rekonsiliasi` → nol selisih.

### 22.3 Uji cepat dengan data nyata (manual; setelah instalasi dan setiap upgrade dependensi)

1. `cek.bat` → tidak ada ERROR. Setiap item "BELUM DIVERIFIKASI" sudah Anda putuskan, baik diisi maupun sengaja dibiarkan.
2. `unduh.bat` → periksa `output\kualitas\...csv`. Jumlah simbol OK mendekati total; telusuri setiap anomali dan split yang belum disesuaikan.
3. **Cek silang:**
   - Pilih 3 saham yang **tidak mengalami split sejak tanggal uji**, dengan tanggal yang sudah ≥ 2 hari bursa berlalu: satu di awal periode backtest, satu di tengah, satu baru.
   - Jalankan `cek --silang KODE TANGGAL`, lalu bandingkan dengan ringkasan perdagangan resmi (idx.co.id atau aplikasi broker). Harga harus sama.
   - Rasio volume menentukan `pengali_volume` (harus 1 jika yfinance memakai lembar). Jika rasionya tidak sama di ketiga periode, **jangan** memakai satu pengali; laporkan sebagai masalah data dan perbarui SPEC.
   - Setelah itu setel `pengali_volume_terverifikasi: true`.
4. `backtest.bat` → ringkasan terbuka. ACAK tidak tampak menguntungkan. Jumlah transaksi per tahun masuk akal. Grafik IHSG sesuai pengetahuan umum (mis. penurunan tajam 2020).
5. **Hitung tangan** satu transaksi dari `transaksi_pb_edge.csv`: sinyal tampak di grafik; harga entry = min(open hari berikutnya × 1,001, harga_maks); exit sesuai aturan; fee benar.
6. `harian.bat` → baca laporan dan hitung ulang sizing satu kandidat dengan kalkulator memakai rumus §14.1.
7. `uji_telegram.bat` → pesan diterima.
8. Daftarkan jadwal. Pada hari bursa berikutnya ±19:30, laporan dan pesan Telegram harus muncul. Opsional: matikan PC saat 19:30 → saat dinyalakan, tugas berjalan susulan dan laporan memakai `t` yang benar.

### 22.4 Masa paper trading sebelum uang sungguhan (anjuran, tidak ditegakkan program)

- **Minimal 3 bulan atau 30 transaksi tertutup di portofolio kertas** (mana yang lebih lama), tanpa transaksi nyata.
- Periksa peringatan penyimpangan (§17.4) dan peringatan subset terjangkau (§16.7).
- Pastikan Anda memahami setiap transaksi kertas yang tercatat.

### 22.5 Kriteria penerimaan rilis v1

- [ ] Semua uji §22.1 lulus di Windows 10/11 dengan Python yang dipin.
- [ ] Skenario §22.2 lulus, dan berkas acuan di-commit.
- [ ] Uji cepat §22.3 langkah 1–8 selesai dengan data nyata.
- [ ] Setiap keluaran memuat disclaimer.
- [ ] `config.yaml` rilis berisi `jadwal_sesi`, `fraksi_harga`, dan `auto_reject` dengan `terverifikasi: false` dan nilai kosong/null. **Tidak ada nilai aturan atau jam sesi BEI yang diisi pengembang.**
- [ ] README memuat §3 (ringkas), glosarium §4.2, instalasi §19.1, rutinitas §19.6, dan daftar cek §24 untuk pengguna tingkat dasar.

---

## 23. Urutan implementasi

| Tahap | Isi | Selesai jika |
|---|---|---|
| 1 | Kerangka proyek, `config.py`, `inputs.py`, `waktu.py`, `kalender.py`, `operasional.py`, perintah `cek` | T-CFG, T-KL (tanpa bagian run), T-INP, T-OP lulus |
| 2 | Lapisan data: provider, cache, split, kualitas, `DataView`, perintah `unduh` | T-DQ, T-CA, T-SP (bagian data) lulus; uji cepat §22.3 langkah 2–3 |
| 3 | `indikator.py`, `kondisi_pasar.py`, `universe.py` | T-IN, T-KP, T-UN, T-LA1a lulus |
| 4 | Strategi PB/BO/MR/ACAK, `skor.py`, `sizing.py`, `biaya.py`, `seleksi.py`, `aturan_bei.py` | T-ST, T-SK (tanpa bagian portofolio live), T-SZ, T-BIAYA, T-BEI (tanpa bagian ARB terkunci), T-LA1b lulus |
| 5 | `exit.py`, mesin backtest, metrik, jendela, gerbang (penulisan berkas), laporan backtest | T-EX, T-BT, T-MT, T-GB (bagian berkas), T-BEI (bagian ARB terkunci), T-LA2, T-LA3, T-SP (bagian mesin) lulus |
| 6 | `portofolio.py`, `harian.py`, jurnal, laporan (terminal/md/html/csv/xlsx) | T-PF, T-JR, T-SK (bagian portofolio live), T-SP (bagian live), T-GB (bagian run harian), T-LP (tanpa Telegram), T-KL (bagian run), T-E2E lulus |
| 7 | Telegram, berkas `.bat`, `daftar_jadwal.ps1`, README | T-TG, T-LP (bagian Telegram) lulus; uji cepat §22.3 langkah 1–8 |

---

## 24. Yang harus Anda cek dan isi sendiri

| # | Item | Di mana | Sumber |
|---|---|---|---|
| 1 | Konstituen universe (mis. LQ45 terbaru) + sektor | `input/universe.csv` | idx.co.id (pengumuman indeks) |
| 2 | Daftar saham breadth (100–200 saham likuid) | `input/breadth_universe.csv` | idx.co.id |
| 3 | Riwayat konstituen (opsional) | `input/universe_history.csv` | Arsip pengumuman evaluasi indeks |
| 4 | Saham suspensi / notasi khusus / papan pemantauan khusus | `input/exclude.csv` | idx.co.id; perbarui setiap ada pengumuman |
| 5 | Kalender libur tahun berjalan dan berikutnya | `input/holidays.csv` | idx.co.id |
| 6 | **Jadwal sesi** (jam penutupan, pra-pembukaan) | `config.yaml → pasar.jadwal_sesi` | Jadwal perdagangan terbaru di idx.co.id |
| 7 | **Tabel fraksi harga** + tanggal berlaku | `config.yaml → pasar.fraksi_harga` | Peraturan perdagangan terbaru di idx.co.id |
| 8 | **Batas ARA/ARB**, harga acuan, tanggal berlaku | `config.yaml → pasar.auto_reject` | Peraturan/pengumuman terbaru di idx.co.id |
| 9 | Fee beli/jual, biaya minimum, pembulatan fee → `biaya.terverifikasi: true` | `config.yaml → biaya` | Trade confirmation broker |
| 10 | Satuan volume → `pengali_volume_terverifikasi: true` | `config.yaml → data` | §22.3 langkah 3 |
| 11 | **Fitur order di broker Anda:** (a) order otomatis stop/target, termasuk atas saham yang dibeli hari itu; (b) cara menarik order yang tidak terisi saat pembukaan, dan apakah ada validitas order yang otomatis batal; (c) cara menjual di harga pembukaan; (d) apakah hasil jual yang belum terjadi dihitung dalam limit beli | — | Broker Anda. Jika (a) tidak tersedia di hari pembelian, setel `entry.stop_aktif_hari_entry: false` lalu jalankan ulang backtest. Jika asumsi lain tidak berlaku, perbarui SPEC. |
| 12 | Saldo kas dan posisi | `input/akun.yaml`, `input/portfolio.csv` | Akun broker, setiap ada transaksi (§19.6) |
| 13 | Token & chat ID Telegram | `.env` | @BotFather |
| 14 | `holdout_mulai` | `config.yaml → backtest` | Tetapkan sekali sebelum backtest pertama, lalu jangan diubah |

Item 6–8 sengaja dikosongkan. Program tetap berjalan tanpa nilai tersebut dengan perilaku aman (§7), dan setiap laporan akan mengingatkan Anda.

Letak, format, dan contoh isian setiap item ada di `contoh/PANDUAN_ISI.md` (§5). Semua nilai di `contoh/` fiktif; aturan BEI di `contoh/config_bei_contoh.yaml` sengaja dibuat tidak mirip aturan sungguhan dan tidak boleh disalin angkanya.

---

## 25. Keputusan default yang tidak ditanyakan

Keputusan berikut diambil saat menyusun spesifikasi. Semuanya bisa diubah lewat config atau revisi SPEC.

1. **Slippage 0,10% per sisi** dipakai di backtest, jurnal, **dan sizing sisi stop**, sehingga kerugian di level stop tetap ≤ 1% sesuai keputusan Anda. Akibatnya contoh A menjadi 1 lot, bukan 2.
2. **Gerbang memakai PF dan drawdown dalam satuan R** (PF_R, max drawdown 15R), agar tidak terdistorsi ukuran posisi. PF Rupiah tetap ditampilkan.
3. **Hasil jual di open tidak membiayai beli di open yang sama** (`dana_jual_langsung_dipakai: false`). Slot dari jual terjadwal tetap boleh dipakai.
4. **Dividen diabaikan;** harga hanya disesuaikan untuk split. Sizing backtest memakai harga nominal.
5. **Status JANGAN BELI tidak memaksa jual.**
6. **Tidak ada cooldown** setelah stop dan tidak ada penambahan posisi.
7. **Bobot skor 40/30/15/15** dan definisi komponennya adalah heuristik awal.
8. **Ambang:** likuiditas Rp 10 miliar/hari; breadth 50%/30%; `min_saham_breadth` 30; cakupan 80%; lonjakan mencurigakan 40%.
9. **Kalender historis diturunkan dari data breadth.** `holidays.csv` cukup diisi untuk tahun berjalan dan berikutnya.
10. **Grafik HTML memakai PNG tersemat**, sehingga bisa dibuka offline dan dilampirkan ke Telegram.
11. **Posisi tanpa bar valid selama 60 hari bursa** ditutup pada hari itu dengan potongan konservatif 50% (`DATA_BERAKHIR`), diputuskan as-of tanpa melihat masa depan.
12. **Anomali data berlaku ke depan** (as-of) selama `min_bar_riwayat` bar, bukan menghapus saham dari seluruh riwayat.
13. **Jurnal kertas dimulai ulang** setiap kali aturan (`hash_aturan` global) berubah. Gerbang dinilai ulang **per strategi**, sehingga mengubah satu strategi tidak menonaktifkan strategi lain. Metadata verifikasi (sumber, tanggal cek, flag `terverifikasi` biaya/volume, jadwal sesi) tidak memengaruhi hash.
14. **Masa paper trading yang dianjurkan:** 3 bulan / 30 transaksi kertas.
15. **MR `min_hari_tahan: 2`** untuk mewujudkan "ditahan 2–5 hari".
16. **Batas bar final = jam run Anda (19:30)** sampai jadwal sesi diverifikasi.
17. **Satuan R memakai risiko rencana** (dari `harga_maks` dan stop saat sizing), sehingga tetap bermakna pada gap turun di hari entry.
18. **Nilai posisi tanpa close valid** = close valid terakhir, dengan ekuitas ditandai "≈", dipakai seragam di live, backtest, dan jurnal.

---

> **BUKAN SARAN INVESTASI.** Dokumen ini adalah spesifikasi perangkat lunak untuk alat bantu keputusan pribadi. Tidak ada jaminan keuntungan. Seluruh keputusan dan risiko transaksi menjadi tanggung jawab pengguna.
