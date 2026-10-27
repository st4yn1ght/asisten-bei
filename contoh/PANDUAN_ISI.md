# Panduan mengisi berkas Anda sendiri (SPEC §24)

> **BUKAN SARAN INVESTASI.** Panduan ini hanya menjelaskan letak dan format berkas yang Anda isi sendiri.

Semua kode saham, harga, jam, dan persentase di folder `contoh/` **fiktif**. Gunanya hanya menunjukkan bentuk
isian yang benar. Jangan memakai angkanya; isi selalu dari sumber resmi yang disebut di setiap bagian.

## Di mana berkasnya?

```
asisten-bei\                      ← folder utama proyek
├── config.yaml                   ← item 6–11 dan 14 (sudah ada; ubah nilainya)
├── .env                          ← item 13 (Anda buat sendiri; contoh: contoh\env.contoh)
├── input\                        ← item 1–5 dan 12
│   ├── universe.csv              ← dirilis berisi baris judul saja; isi datanya
│   ├── breadth_universe.csv      ← dirilis berisi baris judul saja; isi datanya
│   ├── exclude.csv               ← dirilis berisi baris judul saja; isi datanya
│   ├── holidays.csv              ← dirilis berisi baris judul saja; isi datanya
│   ├── portfolio.csv             ← dirilis berisi baris judul saja; isi datanya
│   ├── akun.yaml                 ← Anda buat sendiri (contoh: contoh\input\akun.yaml)
│   ├── universe_history.csv      ← opsional; buat hanya bila Anda punya datanya
│   └── anomali_ok.csv            ← opsional; lihat bagian terakhir
└── contoh\                       ← berkas contoh terisi (tidak pernah dibaca program)
    ├── PANDUAN_ISI.md            ← berkas ini
    ├── input\…                   ← contoh terisi untuk setiap berkas di input\
    ├── config_bei_contoh.yaml    ← contoh terisi untuk bagian config.yaml (item 6–10, 14)
    ├── cek_fitur_broker.md       ← daftar pertanyaan untuk broker (item 11)
    └── env.contoh                ← contoh isi .env (item 13)
```

Program **hanya** membaca `input\`, `config.yaml`, dan `.env`. Isi di `contoh\` tidak pernah dipakai.

## Aturan umum untuk semua berkas CSV

- Edit dengan **Notepad**, bukan Excel (klik kanan berkas → *Buka dengan* → *Notepad*). Excel dengan format
  regional Indonesia mengganti koma menjadi `;` dan titik desimal menjadi koma, sehingga berkas ditolak.
- Baris pertama adalah **baris judul (header)**. Jangan diubah atau dihapus; tambahkan data mulai baris kedua.
- Pemisah kolom koma. Kolom kosong tetap ditulis dengan koma: `AAAA,2026-10-08,2850,2,PB,2720,,,,`
- Angka memakai titik desimal **tanpa pemisah ribuan**: `2850`, `0.5`.
  - `1.000.000` ditolak `cek` sebagai ERROR.
  - **Awas:** `2.850` **tidak** ditolak, karena juga sah sebagai angka desimal. Program membacanya sebagai
    2,85 rupiah dan `cek` hanya memberi PERINGATAN. Selalu tulis `2850`.
- Tanggal **hanya** `YYYY-MM-DD`, mis. `2026-10-08`. Format lain ditolak.
- Kode saham huruf besar **tanpa** `.JK`: `AAAA`, bukan `aaaa.JK`.
- Teks yang memuat koma diapit tanda kutip ganda: `DDDD,"Contoh Empat, Tbk",Barang Konsumen Primer`.
- Simpan dengan encoding **UTF-8** (Notepad: *Simpan sebagai* → *Encoding: UTF-8*).
- Saat membuat berkas **baru** (`akun.yaml`, `.env`, `universe_history.csv`, `anomali_ok.csv`), pilih
  *Simpan sebagai jenis: Semua berkas (\*.\*)* lalu ketik nama lengkapnya. Aktifkan juga *File name
  extensions* di File Explorer agar terlihat bila nama berkas berakhiran `.txt`.

Setelah mengisi, jalankan `cek` di folder proyek (Command Prompt yang dibuka di folder itu):

```
.venv\Scripts\python -m asisten_bei cek
```

`cek` menyebut berkas dan nomor baris yang salah. Perbaiki semua **ERROR**, lalu baca juga setiap
**PERINGATAN** (misalnya angka bertitik ribuan di atas).

---

## Item 1 — `input\universe.csv`: saham yang boleh direkomendasikan

| Kolom | Isi |
|---|---|
| `kode` | Kode saham, mis. `AAAA`. Tidak boleh ganda. |
| `nama` | Nama emiten (bebas; untuk dibaca di laporan). |
| `sektor` | Sektor emiten. Wajib, karena berlaku batas 1 posisi per sektor. Tulis nama sektor **persis sama** untuk saham sektor yang sama; disarankan memakai klasifikasi sektor resmi di profil emiten idx.co.id. |

Contoh: [`contoh/input/universe.csv`](input/universe.csv)

```csv
kode,nama,sektor
AAAA,Contoh Satu Tbk,Keuangan
BBBB,Contoh Dua Tbk,Infrastruktur
```

**Sumber:** daftar konstituen indeks terbaru (mis. LQ45) dari pengumuman indeks di idx.co.id.
Perbarui setiap ada pengumuman evaluasi indeks.

## Item 2 — `input\breadth_universe.csv`: saham untuk mengukur kondisi pasar

Satu kolom `kode`. Disarankan 100–200 saham likuid; boleh tumpang tindih dengan `universe.csv`.
Program butuh minimal `kondisi_pasar.min_saham_breadth` (30) saham yang datanya terhitung.

Contoh: [`contoh/input/breadth_universe.csv`](input/breadth_universe.csv)

```csv
kode
AAAA
CCCC
```

**Sumber:** daftar saham likuid dari idx.co.id (mis. konstituen indeks yang lebih luas).

## Item 3 — `input\universe_history.csv` (opsional): riwayat konstituen

Dipakai **hanya oleh backtest**, menggantikan `universe.csv`, agar backtest tidak hanya memakai saham yang
"bertahan" sampai hari ini.

| Kolom | Isi |
|---|---|
| `kode` | Kode saham. Boleh muncul di beberapa baris (keluar lalu masuk lagi). |
| `tanggal_masuk` | Tanggal mulai menjadi konstituen (ikut dihitung). |
| `tanggal_keluar` | Tanggal terakhir menjadi konstituen (ikut dihitung). Kosongkan bila masih konstituen. |

Contoh: [`contoh/input/universe_history.csv`](input/universe_history.csv)

```csv
kode,tanggal_masuk,tanggal_keluar
AAAA,2015-01-01,
BBBB,2015-01-01,2018-07-31
BBBB,2020-02-03,
```

- Sektor diambil dari `universe.csv`. Kode yang tidak ada di sana dianggap satu sektor tersendiri.
- **Jangan membuat berkas ini dalam keadaan kosong.** Bila berkas ada, backtest memakainya sebagai satu-satunya
  daftar saham. Bila belum punya datanya, jangan buat berkasnya sama sekali.

**Sumber:** arsip pengumuman evaluasi indeks di idx.co.id.

## Item 4 — `input\exclude.csv`: saham yang sedang tidak boleh direkomendasikan

| Kolom | Isi |
|---|---|
| `kode` | Kode saham. |
| `alasan` | Isi alasannya, mis. `Suspensi`, `Notasi khusus`, `Papan pemantauan khusus`. |
| `tanggal_mulai` | Mulai berlaku (ikut dihitung). Kosong = sejak dulu. |
| `tanggal_akhir` | Berakhir (ikut dihitung). Kosong = sampai dicabut. |

Contoh: [`contoh/input/exclude.csv`](input/exclude.csv)

```csv
kode,alasan,tanggal_mulai,tanggal_akhir
EEEE,Suspensi,2026-10-01,
FFFF,Notasi khusus,,
```

Berkas boleh hanya berisi baris judul bila tidak ada saham yang dikecualikan.
**Sumber:** pengumuman suspensi, notasi khusus, dan papan pemantauan khusus di idx.co.id. Perbarui setiap ada
pengumuman. Backtest mengabaikan berkas ini kecuali `backtest.pakai_exclude: true`.

## Item 5 — `input\holidays.csv`: libur bursa

| Kolom | Isi |
|---|---|
| `tanggal` | Tanggal libur bursa. |
| `keterangan` | Bebas, mis. nama hari libur. |

Contoh format: [`contoh/input/holidays.csv`](input/holidays.csv). Tanggal di contoh sengaja tahun 2099
agar tidak tertukar dengan kalender sungguhan.

```csv
tanggal,keterangan
2099-12-25,CONTOH FORMAT - ganti dengan tanggal libur resmi dari idx.co.id
```

- Isi **semua** hari libur bursa **tahun berjalan dan tahun berikutnya**, termasuk cuti bersama yang
  ditetapkan sebagai libur bursa.
- `cek` dan setiap laporan memberi peringatan bila belum ada tanggal untuk tahun berjalan atau tahun berikutnya.

**Sumber:** kalender libur bursa resmi di idx.co.id.

## Item 6–8 — Aturan BEI di `config.yaml` (bagian `pasar:`)

Contoh terisi: [`contoh/config_bei_contoh.yaml`](config_bei_contoh.yaml). Semua jam, tabel fraksi, dan
persentase ARA/ARB di contoh itu **fiktif** dan sengaja dibuat aneh.

| Item | Bagian di `config.yaml` | Yang diisi | Sumber |
|---|---|---|---|
| 6 | `pasar` → `jadwal_sesi` | `jam_penutupan`, `jam_pra_pembukaan` (`"HH:MM"` WIB), `sumber`, `tanggal_cek` | Jadwal perdagangan terbaru di idx.co.id |
| 7 | `pasar` → `fraksi_harga` | `tabel` (rentang harga → fraksi), `berlaku_mulai`, `sumber`, `tanggal_cek` | Peraturan perdagangan terbaru di idx.co.id |
| 8 | `pasar` → `auto_reject` | `ara`, `arb` (rentang harga → `jenis` `pct`/`abs` dan `batas`), `harga_acuan`, `berlaku_mulai`, `papan_berlaku`, `sumber`, `tanggal_cek` | Peraturan/pengumuman auto rejection terbaru di idx.co.id |

Langkah:

1. Buka `config.yaml` dengan Notepad, cari `jadwal_sesi:`, `fraksi_harga:`, dan `auto_reject:`.
2. **Ubah nilainya langsung di bagian yang sudah ada.** Jangan menempel (paste) blok dari berkas contoh:
   `config.yaml` sudah punya bagian yang sama, dan `cek` menolak bagian yang muncul dua kali.
3. Ganti `null` dan `[]` dengan nilai dari sumber resmi, mengikuti bentuk di contoh. Perhatikan indentasi
   (spasi, bukan tab) dan tanda kutip pada jam dan tanggal. Tulis angka **tanpa titik ribuan** (`5000`, bukan
   `5.000`): di `config.yaml`, `5.000` dibaca sebagai 5 tanpa pesan error.
4. Tabel rentang harga: `harga_min` ikut rentang itu, `harga_maks` tidak (sama dengan `harga_min` baris
   berikutnya). Baris harus bersambung tanpa celah, dan baris terakhir `harga_maks: null`. Untuk aturan yang
   ditulis "Rp 1 s.d. Rp 10" dan "Rp 11 s.d. Rp 14", tulis `harga_min: 1, harga_maks: 11` lalu
   `harga_min: 11, harga_maks: 15`.
5. Setiap baris `ara`/`arb` memilih satu `jenis`:
   - `jenis: pct` — `batas` dalam persen dari harga acuan (mis. `batas: 25` = naik/turun maks 25 %);
   - `jenis: abs` — `batas` dalam rupiah dari harga acuan (mis. `batas: 1` = naik/turun maks Rp 1).
6. Ubah `terverifikasi: true` **hanya** setelah semua nilai di blok itu diganti dan dicocokkan. `cek` menolak
   blok bertanda `terverifikasi: true` yang `sumber`-nya masih berisi teks contoh `CONTOH FIKTIF`.
7. Jalankan `cek`.

Item ini **boleh dibiarkan kosong** (`terverifikasi: false`). Program tetap berjalan dengan perilaku aman
(harga hanya dibulatkan ke rupiah bulat, ARA/ARB tidak dimodelkan), dan setiap laporan mengingatkan Anda.
Putuskan sebelum backtest pertama (lihat *Urutan yang disarankan*).

## Item 9 — Biaya broker di `config.yaml` (bagian `biaya:`)

| Kunci | Isi |
|---|---|
| `fee_beli_pct` | Persen biaya beli. Contoh hitung fiktif: biaya Rp 1234 untuk nilai beli Rp 1000000 → `0.1234`. |
| `fee_jual_pct` | Persen biaya jual, **termasuk pajak penjualan**. |
| `fee_minimum_idr` | Biaya minimum per transaksi, `0` bila tidak ada. |
| `pembulatan_fee` | `null` (tidak dibulatkan), `"rupiah_terdekat"`, atau `"rupiah_ke_atas"`. |
| `terverifikasi` | Ubah ke `true` setelah angka di atas cocok dengan trade confirmation. |

**Sumber:** trade confirmation (bukti transaksi) dari broker Anda. Cocokkan satu transaksi beli dan satu
transaksi jual. Nilai saat ini di `config.yaml` (0.15 dan 0.25) adalah keputusan awal Anda; ubah hanya bila
bukti transaksi menunjukkan angka lain.

## Item 10 — Satuan volume di `config.yaml` (bagian `data:`)

Dikerjakan **sesudah** data diunduh (`.venv\Scripts\python -m asisten_bei unduh`) dan **selagi**
`pengali_volume` masih `1`:

1. Pilih 3 pasangan saham + tanggal yang sudah lewat ≥ 2 hari bursa: satu tanggal di awal periode backtest,
   satu di tengah, satu yang baru. Pakai hanya saham yang tidak mengalami split sesudah tanggal itu
   (`cek --silang` menyebutkannya).
2. Jalankan untuk masing-masing: `.venv\Scripts\python -m asisten_bei cek --silang KODE YYYY-MM-DD`
3. Bandingkan harga dan volume dengan ringkasan perdagangan resmi di idx.co.id. Harga harus sama.
   - Bandingkan volume dalam **lembar**. Banyak aplikasi broker menampilkan **lot**; kalikan dulu dengan 100.
   - Pakai angka volume pada baris **`Bar cache`** dari `cek --silang` (baris `Nominal` sudah dikali
     `pengali_volume`).
   - Rasio volume resmi ÷ volume program menentukan `pengali_volume` (harus `1` bila data sudah dalam lembar).
4. Bila rasionya sama di ketiga pasangan, setel `pengali_volume` ke rasio itu dan
   `pengali_volume_terverifikasi: true`. Bila berbeda, **jangan** memakai satu pengali: biarkan
   `terverifikasi: false` dan anggap itu masalah data.

## Item 11 — Fitur order di broker Anda

Tidak ada berkas yang diisi. Jawab pertanyaan di [`contoh/cek_fitur_broker.md`](cek_fitur_broker.md).
Bila broker tidak bisa memasang order otomatis stop pada saham yang baru dibeli hari itu, setel
`entry.stop_aktif_hari_entry: false` di `config.yaml` sebelum backtest pertama.

## Item 12 — `input\akun.yaml` dan `input\portfolio.csv`

### `input\akun.yaml` — saldo kas

Buat berkas baru `input\akun.yaml` (contoh: [`contoh/input/akun.yaml`](input/akun.yaml)):

```yaml
kas_idr: 2268905
diperbarui: 2026-10-15
```

- `kas_idr`: uang tunai Anda **setelah** semua pembelian dan penjualan yang sudah terisi dikurangkan/ditambahkan,
  termasuk yang belum settle. **Bukan** limit beli/trading limit, dan tanpa dana pembiayaan/margin. Bila menyalin
  angka dari aplikasi broker, pastikan transaksi yang sudah tercermin di angka itu tidak Anda hitung lagi.
- Tulis **tanpa titik ribuan**: Rp 950.000 ditulis `950000`. (`950.000` dibaca 950 rupiah; `cek` memberi
  PERINGATAN.)
- `diperbarui`: tanggal saldo itu, format `YYYY-MM-DD`. Contoh di folder `contoh\` sengaja bertanggal 2099
  agar `cek` memperingatkan bila tersalin tanpa diganti.
- Perbarui setiap ada transaksi atau setoran. Laporan memberi peringatan bila `diperbarui` lebih tua dari
  `akun.maks_umur_hari_bursa` (5) hari bursa. Tanpa berkas ini, program memakai `akun.modal_awal_idr`
  (Rp 3.000.000) dengan peringatan mencolok.

### `input\portfolio.csv` — posisi yang sedang dipegang

| Kolom | Wajib | Isi |
|---|---|---|
| `kode` | ya | Kode saham. Boleh di luar `universe.csv`. Satu kode = satu baris. |
| `tanggal_beli` | ya | Tanggal beli (hari bursa). Tidak boleh sesudah tanggal data `t` yang dicetak `cek`: pembelian hari ini baru sah setelah `jadwal.jam_run` (19:30). Bila Anda mengisinya pagi/siang, `cek` memberi ERROR "setelah tanggal data t" — **jangan** mengubah tanggalnya; jalankan `cek` lagi setelah 19:30. |
| `harga_beli` | ya | Harga rata-rata per lembar sesuai trade confirmation, tanpa fee, tanpa titik ribuan. |
| `lot` | ya | Jumlah lot (1 lot = 100 lembar), bilangan bulat ≥ 1. Bila kolom `lembar` diisi, yang dipakai `lembar`, tetapi `lot` tetap wajib ≥ 1 (isi mis. `1`). |
| `strategi` | ya | `PB`, `BO`, `MR`, atau `MANUAL` (posisi yang bukan dari rekomendasi program). |
| `stop_awal` | ya | Stop dari laporan, setelah Anda bulatkan ke fraksi harga. Untuk `MANUAL`, isi level stop yang Anda tetapkan sendiri. |
| `sektor` | bila kode tidak ada di `universe.csv` | Nama sektor. |
| `harga_per_tanggal` | tidak | Tanggal acuan harga dan lot (untuk aksi korporasi). Kosong = `tanggal_beli`. |
| `lembar` | tidak | Isi hanya bila jumlah saham bukan kelipatan lot. |
| `catatan` | tidak | Bebas. |

Contoh: [`contoh/input/portfolio.csv`](input/portfolio.csv)

```csv
kode,tanggal_beli,harga_beli,lot,strategi,stop_awal,sektor,harga_per_tanggal,lembar,catatan
AAAA,2026-10-08,2850,2,PB,2720,,,,contoh: saham di universe.csv sehingga sektor boleh kosong
KKKK,2026-09-14,400,4,MANUAL,370,Properti,,,contoh: saham di luar universe.csv wajib diisi sektornya
```

- Bila tidak memegang saham, biarkan berkas hanya berisi baris judul.
- Bila membeli lagi saham yang sudah dipegang, gabungkan sendiri menjadi satu baris dengan harga rata-rata.
- Setelah jual habis, hapus barisnya dan tambahkan hasil jual ke `kas_idr` di `akun.yaml`.
- Semua baris, termasuk `MANUAL`, ikut dihitung ke slot posisi dan batas sektor.

**Contoh pembaruan dengan angka fiktif** (misalkan portofolio Anda masih kosong; fee beli 0,15 %, jual 0,25 %):

- Beli 1 lot `BBBB` di harga 2010, strategi PB, stop 1888 → tambah baris `BBBB,2026-10-16,2010,1,PB,1888,,,,`
  dan kurangi `kas_idr` sebesar `201301.5` (= 2010 × 100 × 1,0015), lalu ubah `diperbarui` ke tanggal itu.
- Jual 1 lot itu di harga 2254 → hapus barisnya dan tambah `kas_idr` sebesar `224836.5`
  (= 225400 × (1 − 0,0025)), lalu ubah `diperbarui`.
- Bila broker membulatkan fee berbeda, pakai angka dari trade confirmation.

## Item 13 — `.env`: token dan chat ID Telegram

Buat berkas bernama tepat `.env` (**bukan** `.env.txt`) di folder utama proyek, di sebelah `config.yaml`
(boleh menyalin `.env.example` yang sudah ada lalu mengganti namanya). Contoh isi: [`contoh/env.contoh`](env.contoh).

```
TELEGRAM_BOT_TOKEN=123456789:CONTOH_FIKTIF_ganti_dengan_token_dari_BotFather
TELEGRAM_CHAT_ID=987654321
```

1. Buat bot lewat @BotFather di Telegram (`/newbot`) dan salin tokennya.
2. Kirim satu pesan apa pun ke bot Anda.
3. Dapatkan chat ID dengan `.venv\Scripts\python -m asisten_bei uji-telegram --cari-chat-id` (tersedia mulai
   tahap 7, bersama notifikasi Telegram; sebelum itu item ini boleh ditunda).
4. Jangan bagikan berkas `.env` ke siapa pun. `cek` memberi peringatan bila yang ada justru `.env.txt`, atau
   bila token masih nilai contoh.

## Item 14 — `holdout_mulai` di `config.yaml` (bagian `backtest:`)

```yaml
backtest:
  holdout_mulai: "2024-10-01"
```

- Data sejak tanggal ini disisihkan untuk menguji strategi secara jujur (SPEC §16.4).
- **Tetapkan sekali sebelum backtest pertama, lalu jangan diubah.** Setiap kali holdout dilihat, program
  mencatatnya di `output\backtest\holdout_log.csv`, dan `cek` memperingatkan bila nilainya berubah.
- Pilih tanggal yang menyisakan cukup data sesudahnya agar holdout bisa memuat ≥ 30 transaksi
  (`gerbang.min_transaksi_holdout`). Nilai default di `config.yaml` adalah `"2024-10-01"`.

## `input\anomali_ok.csv` (opsional, bukan item §24)

Buat hanya bila berkas kualitas data atau laporan menandai **ANOMALI** (lonjakan harga mencurigakan) pada
saham + tanggal tertentu, dan Anda sudah mencocokkannya dengan ringkasan perdagangan resmi di idx.co.id lalu
ternyata datanya benar. Kolom `kode,tanggal,catatan`; untuk indeks pakai kode `IHSG`.
Contoh: [`contoh/input/anomali_ok.csv`](input/anomali_ok.csv). Tanpa anomali, jangan buat berkas ini.

---

## Urutan yang disarankan

1. Item 1, 2, dan 12, lalu item 4 dan 5. Jalankan `cek` sampai **0 ERROR**.
2. Unduh data: `.venv\Scripts\python -m asisten_bei unduh`, lalu kerjakan item 10.
3. **Sebelum backtest pertama**, putuskan item 6–9 dan 11 (diisi, atau sengaja dibiarkan kosong) dan
   tetapkan item 14. Fee, tabel fraksi/ARA, `pengali_volume`, `entry.stop_aktif_hari_entry`, dan
   `risiko.dana_jual_langsung_dipakai` ikut menentukan "konfigurasi aturan": mengubahnya sesudah holdout
   pernah dilihat dicatat sebagai konfigurasi baru di `holdout_log.csv`, sehingga holdout tidak lagi murni.
   Hanya `jadwal_sesi` (item 6) yang boleh diisi belakangan tanpa akibat itu.
4. Item 3 bila Anda punya data riwayat konstituen; item 13 saat notifikasi Telegram sudah tersedia.

> **BUKAN SARAN INVESTASI.** Laporan program ini dibuat otomatis berdasarkan aturan mekanis dan data publik yang
> bisa salah, terlambat, atau tidak lengkap. Seluruh keputusan dan risiko transaksi menjadi tanggung jawab
> pengguna.
