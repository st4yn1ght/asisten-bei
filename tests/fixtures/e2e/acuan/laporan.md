BUKAN SARAN INVESTASI

# Laporan harian 2025-09-10

**BUKAN SARAN INVESTASI**

Tanggal data t: 2025-09-10 — untuk hari bursa t+1: 2025-09-11

Dibuat: 2025-09-10 20:00:00 WIB — hash8 44b5deb5 — versi 0.1.0 — REPLAY (--tanggal)

Status data run: LENGKAP

> **REPLAY --tanggal 2025-09-10: laporan dibuat ulang dari data ≤ 2025-09-10.**

## Ringkasan aksi

- BELI UJIPB 3 lot, limit 1.724 — tarik jika tidak terisi saat pembukaan
- BELI UJIGAP 4 lot, limit 1.363 — tarik jika tidak terisi saat pembukaan

## Status pasar

**BOLEH BELI (K3): Tren IHSG naik dan breadth sehat**

| Syarat | Kondisi | Nilai | Ambang | ✓/✗ |
|---|---|---|---|---|
| K0 | Data IHSG tersedia dan dapat dipakai | ya | ya | ✓ |
| K0 | Bar IHSG 2025-09-10 tersedia | ada | ada | ✓ |
| K0 | Bar IHSG hilang dalam 60 hari bursa terakhir | 0 | ≤ 2 | ✓ |
| K0 | Tanpa anomali data IHSG | tidak ada | tidak ada | ✓ |
| K0 | SMA200 IHSG terdefinisi | 10.377,56 | terdefinisi | ✓ |
| K0 | SMA50 IHSG terdefinisi | 11.082,53 | terdefinisi | ✓ |
| K0 | Cakupan breadth (40/40 saham) | 100% | ≥ 80% | ✓ |
| K0 | Saham breadth terhitung | 40 | ≥ 30 | ✓ |
| K1 | IHSG close < SMA200 | 11.289,61 vs 10.377,56 | tren turun bila ✓ | ✗ |
| K1 | SMA50 < SMA200 | 11.082,53 vs 10.377,56 | tren turun bila ✓ | ✗ |
| K2 | Breadth (close > SMA50) | 65% | < 30% → JANGAN BELI | ✗ |
| K3 | IHSG close > SMA200 | 11.289,61 vs 10.377,56 | wajib ✓ | ✓ |
| K3 | SMA50 > SMA200 | 11.082,53 vs 10.377,56 | wajib ✓ | ✓ |
| K3 | IHSG close > SMA50 | 11.289,61 vs 11.082,53 | wajib ✓ | ✓ |
| K3 | Breadth | 65% | ≥ 50% | ✓ |

_Breadth: 26/40 di atas SMA50 (65%); cakupan 40/40 (100%)._

_(Grafik IHSG dengan SMA pendek/panjang: lihat laporan.html)_

_(Grafik Breadth 120 hari bursa: lihat laporan.html)_

## Status strategi

| Strategi | Status | Data backtest s/d | PF_R holdout | Subset terjangkau lemah | Alasan |
|---|---|---|---|---|---|
| PB | LOLOS | 2025-11-28 | n/a (tanpa kerugian) | tidak | — |
| BO | LOLOS | 2025-11-28 | 0,00 | tidak | — |
| MR | LOLOS | 2025-11-28 | 0,70 | tidak | — |

## Akun

| Item | Nilai |
|---|---|
| Kas (akun.yaml) | Rp 3.000.000 |
| Nilai posisi | Rp 0 |
| Ekuitas | Rp 3.000.000 |
| Anggaran risiko per transaksi | Rp 30.000 (1% ekuitas, status BOLEH BELI) |
| Alokasi maks per posisi | Rp 1.000.000 |
| Slot kosong | 3 |
| akun.yaml diperbarui | 2025-09-10 |

## Posisi dipegang

Tidak ada posisi di portfolio.csv.

## Rencana beli

**UJIPB — Ujipb Tbk (FIKTIF – KHUSUS UJI) (Sektor Fiktif A), PB, skor 75,2**

| Item | Isi |
|---|---|
| Alasan sinyal | PB1: close 1.710,49 > SMA50 1.700,73 dan SMA50 1.700,73 > SMA200 1.573,79 ✓; PB2: SMA50 1.700,73 > SMA50 10 bar lalu 1.685,53 ✓; PB3: low ≤ SMA20 dalam 4 bar terakhir: hari ini low 1.667,06 ≤ SMA20 1.723,77; 1 bar lalu low 1.660,38 ≤ SMA20 1.723,98; 2 bar lalu low 1.678,85 ≤ SMA20 1.726,10; 3 bar lalu low 1.697,52 ≤ SMA20 1.727,19 ✓; PB4: close 1.710,49 > high kemarin 1.699,12 dan close 1.710,49 > open 1.670,40 ✓; PB5: (close 1.710,49 − SMA20 1.723,77) / ATR 28,38 = -0,47 ≤ 1,00 ✓ |
| Harga referensi (close t) | 1.710,49 |
| Instruksi masuk | Pasang order BELI limit di 1.724, 3 lot, sebelum pra-pembukaan t+1. Segera setelah harga pembukaan terbentuk, cek order Anda. Jika belum terisi (open > harga_maks), tarik (withdraw) order; jangan biarkan antre sepanjang hari, karena bisa terisi belakangan di harga yang tidak diuji backtest. Jika hanya terisi sebagian, tarik sisanya dan catat lot yang terisi saja. |
| Stop loss | 1.655 (LLV5 − 0,2 × ATR14); jarak 4,00% |
| Target | target = harga isi + 2 × (harga isi − stop), dibulatkan ke bawah; isi 1.710,49 → target 1.821; isi 1.717 → target 1.841; isi 1.724 → target 1.862 |
| Lot / nilai / fee beli | 3 lot (300 lembar) / Rp 517.200 / Rp 775,80 |
| Risiko | Rp 23.212,31 (0,77% ekuitas); sisa anggaran per lembar Rp 22,63 |
| R:R bersih (isi di harga_maks) | 1,69 |
| Langkah setelah terisi | Pasang order otomatis stop di 1.655 (dan target di 1.862) hari itu juga. Jika harga isi sudah ≤ stop, stop telah terlewati: jual segera. Perbarui portfolio.csv dan akun.yaml (§19.6). |
| Peringatan | Harga belum dibulatkan ke fraksi: bulatkan limit beli dan target ke BAWAH, stop ke ATAS ke fraksi harga valid. |

_(Grafik UJIPB (PB, rencana): lihat laporan.html)_

**UJIGAP — Ujigap Tbk (FIKTIF – KHUSUS UJI) (Sektor Fiktif F), PB, skor 68,2**

| Item | Isi |
|---|---|
| Alasan sinyal | PB1: close 1.352,76 > SMA50 1.345,04 dan SMA50 1.345,04 > SMA200 1.234,81 ✓; PB2: SMA50 1.345,04 > SMA50 10 bar lalu 1.333,02 ✓; PB3: low ≤ SMA20 dalam 4 bar terakhir: hari ini low 1.318,41 ≤ SMA20 1.363,26; 1 bar lalu low 1.313,13 ≤ SMA20 1.363,43; 2 bar lalu low 1.327,73 ≤ SMA20 1.365,11; 3 bar lalu low 1.342,50 ≤ SMA20 1.365,97 ✓; PB4: close 1.352,76 > high kemarin 1.343,76 dan close 1.352,76 > open 1.321,05 ✓; PB5: (close 1.352,76 − SMA20 1.363,26) / ATR 22,44 = -0,47 ≤ 1,00 ✓ |
| Harga referensi (close t) | 1.352,76 |
| Instruksi masuk | Pasang order BELI limit di 1.363, 4 lot, sebelum pra-pembukaan t+1. Segera setelah harga pembukaan terbentuk, cek order Anda. Jika belum terisi (open > harga_maks), tarik (withdraw) order; jangan biarkan antre sepanjang hari, karena bisa terisi belakangan di harga yang tidak diuji backtest. Jika hanya terisi sebagian, tarik sisanya dan catat lot yang terisi saja. |
| Stop loss | 1.309 (LLV5 − 0,2 × ATR14); jarak 3,96% |
| Target | target = harga isi + 2 × (harga isi − stop), dibulatkan ke bawah; isi 1.352,76 → target 1.440; isi 1.357 → target 1.453; isi 1.363 → target 1.471 |
| Lot / nilai / fee beli | 4 lot (400 lembar) / Rp 545.200 / Rp 817,80 |
| Risiko | Rp 24.249,09 (0,81% ekuitas); sisa anggaran per lembar Rp 14,38 |
| R:R bersih (isi di harga_maks) | 1,69 |
| Langkah setelah terisi | Pasang order otomatis stop di 1.309 (dan target di 1.471) hari itu juga. Jika harga isi sudah ≤ stop, stop telah terlewati: jual segera. Perbarui portfolio.csv dan akun.yaml (§19.6). |
| Peringatan | Harga belum dibulatkan ke fraksi: bulatkan limit beli dan target ke BAWAH, stop ke ATAS ke fraksi harga valid. |

_(Grafik UJIGAP (PB, rencana): lihat laporan.html)_

_Skor adalah heuristik pemeringkat, bukan peluang keberhasilan._

## Kandidat lain

| Kode | Strategi | Skor | Status | Alasan |
|---|---|---|---|---|
| UJIMAHAL | PB | 74,5 | TIDAK_TERJANGKAU | risiko 1 lot = Rp 63.694,30 (2,12% ekuitas) > anggaran Rp 30.000 (1%, status BOLEH BELI) |
| UJIMAHAL2 | PB | 64,3 | TIDAK_TERJANGKAU | nilai 1 lot Rp 1.051.575 > alokasi maks Rp 1.000.000 |
| UJISEKTOR | PB | 60,3 | BATAS_SEKTOR | sektor Sektor Fiktif A sudah mencapai 1 posisi |

## Daftar pantau

Tidak ada (daftar pantau hanya saat JANGAN BELI).

## Kualitas data

Seri: OK 51, SEBAGIAN 0, GAGAL 0; data basi 0; anomali 0; sumber CSV manual 0. Rincian per simbol: kualitas_data.csv.

| Kode | Alasan | Rincian |
|---|---|---|
| UJIBOLONG | BAR_HILANG | 3 bar hilang dalam 60 hari bursa terakhir > 2 |

## Item yang belum diverifikasi

- pasar.jadwal_sesi — batas bar final = jadwal.jam_run 19:30 WIB; setiap run pada tanggal t+1 diberi banner. Cek jadwal perdagangan di idx.co.id.
- pasar.fraksi_harga — harga hanya dibulatkan ke rupiah bulat (harga limit & target ke BAWAH, stop ke ATAS). Cek peraturan fraksi harga di idx.co.id.
- pasar.auto_reject — batas ARA/ARB tidak dimodelkan di backtest/jurnal. Cek peraturan auto rejection di idx.co.id.
- biaya — fee beli 0,15%, jual 0,25%, minimum Rp 0, pembulatan tidak ada: cocokkan dengan trade confirmation broker.
- data.pengali_volume = 1 — cocokkan volume dengan sumber resmi lewat cek --silang (SPEC §22.3 langkah 3).

## Jurnal kertas

0 hari dikejar; 0 sinyal susulan tidak pernah dilaporkan.

Periode kertas 1 mulai 2025-09-10, modal Rp 3.000.000; ekuitas Rp 3.000.000 pada 2025-09-10. Ini BUKAN portofolio nyata Anda.

Order kertas MENUNGGU (bar t+1 belum ada): UJIGAP PB, UJIPB PB

| Strategi | n jurnal | Rata-rata R | PF_R | n holdout | Rata-rata R holdout | PF_R holdout |
|---|---|---|---|---|---|---|
| PB | 0 | — | n/a (tanpa transaksi) | 6 | 0,31 | 657,68 |
| BO | 0 | — | n/a (tanpa transaksi) | 1 | -0,31 | 0,00 |
| MR | 0 | — | n/a (tanpa transaksi) | 25 | 0,01 | 1,04 |

_Penyimpangan PB: sampel jurnal 0 < 20: belum diuji_

_Penyimpangan BO: sampel jurnal 0 < 20: belum diuji_

_Penyimpangan MR: sampel jurnal 0 < 20: belum diuji_

## Peringatan

Tidak ada peringatan.

## Penutup

**BUKAN SARAN INVESTASI. Laporan ini dibuat otomatis oleh program pribadi berdasarkan aturan mekanis dan data publik yang bisa salah, terlambat, atau tidak lengkap. Hasil backtest dan paper trading tidak menjamin hasil di masa depan. Seluruh keputusan dan risiko transaksi menjadi tanggung jawab pengguna.**

Keterbatasan singkat:

- Data publik (yfinance/CSV) bisa bolong, salah, terlambat, atau direvisi; dividen dan sebagian aksi korporasi tidak disesuaikan.
- Rencana mengasumsikan order beli terisi di pembukaan (atau ditarik bila tidak) dan order otomatis stop terisi di level stop dikurangi slippage; gap turun bisa membuat kerugian melebihi anggaran risiko.
- Skor adalah heuristik pemeringkat, bukan peluang keberhasilan. Hasil backtest dan jurnal kertas tidak menjamin hasil di masa depan.
- Aturan BEI (fraksi, ARA/ARB, jadwal sesi) hanya dipakai setelah Anda verifikasi sendiri dari idx.co.id.

## Glosarium

| Istilah | Penjelasan |
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

BUKAN SARAN INVESTASI. Laporan ini dibuat otomatis oleh program pribadi berdasarkan aturan mekanis dan data publik yang bisa salah, terlambat, atau tidak lengkap. Hasil backtest dan paper trading tidak menjamin hasil di masa depan. Seluruh keputusan dan risiko transaksi menjadi tanggung jawab pengguna.
