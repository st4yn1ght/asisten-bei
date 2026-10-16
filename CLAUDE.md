# asisten-bei — catatan proyek

Alat bantu keputusan saham BEI untuk pemakaian pribadi. **SPEC.md adalah sumber kebenaran** dan mengikat (nama berkas, kolom, kunci config, enum). Setiap perubahan perilaku dimulai dengan memperbarui SPEC.md.

## Perintah

```bash
# Pasang (Linux/macOS; Windows: scripts\pasang.bat)
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e . --no-deps

# Jalankan (opsi global boleh sebelum/sesudah subperintah)
.venv/bin/python -m asisten_bei [--config config.yaml] [--akar .] cek [--silang KODE YYYY-MM-DD]
.venv/bin/python -m asisten_bei unduh [--penuh] [--simbol K1,K2]
.venv/bin/python -m asisten_bei backtest | harian | jurnal | uji-telegram   # tahap berikutnya

# Test (Windows: scripts\uji.bat)
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_indikator.py -q      # satu berkas

# Bangkitkan ulang data sintetis uji (deterministik)
.venv/bin/python tests/fixtures/buat_fixture.py
```

## Aturan penting

- **BUKAN SARAN INVESTASI** wajib ada di setiap keluaran (terminal, CSV baris pertama `# ...`, JSON kunci `catatan`, YAML komentar pertama). Konstanta di `asisten_bei/__init__.py`.
- **P1 tidak mengarang data:** tanpa forward-fill/interpolasi/tebakan. Data rusak/hilang → saham dikeluarkan + alasan (enum §4.3).
- **P3 tanpa look-ahead:** keputusan hari `t` hanya memakai bar ≤ `t`. Strategi/skor/kondisi pasar/eligibilitas/sizing hanya menerima `DataView(as_of=d)`. Indikator tervektor wajib kausal: jendela bergulir ke belakang, **tanpa `shift(-k)`**, tanpa normalisasi seluruh sampel. Uji `tests/test_tanpa_lookahead.py` harus lulus.
- **P4:** semua parameter dari `config.yaml`. Jangan pernah mengisi fraksi harga, ARA/ARB, atau jam sesi BEI dari ingatan; nilai rilis tetap `terverifikasi: false` dan kosong.
- **Uang pakai `decimal.Decimal`** (sizing, fee, P&L); lot dibulatkan ke bawah. Pembulatan harga: `harga_maks` & target ke **bawah**, stop & trailing ke **atas**.
- **Waktu** hanya dari `asisten_bei.waktu.sekarang()` (env `ASISTEN_BEI_WAKTU_SEKARANG` saat uji); zona selalu `Asia/Jakarta`. Tunggu/jeda lewat `waktu.tidur()`.
- **Data harian:** index `DatetimeIndex` tz-naive bernama `tanggal` (= tanggal WIB); kolom `open, high, low, close, volume` (+ `sumber`, `split` di cache). Harga di cache sudah disesuaikan split; sizing memakai harga nominal (`× F(d)`).
- **Path** di config relatif terhadap `--akar` (`KonteksApp.path()`), bukan direktori kerja.
- **Uji** offline & deterministik: tanpa jaringan (yfinance di-mock), tidak menyentuh berkas pengguna (salin fixture ke `tmp_path`), selalu set `ASISTEN_BEI_WAKTU_SEKARANG`.
- Enum hanya nilai §4.3. Teks untuk pengguna berbahasa Indonesia; kata "trade" tidak dipakai di laporan.
- Kode keluar §20.1, prioritas 3 > 1 > 5 > 2 > 6 > 4 > 0 (`operasional.gabung_kode_keluar`).
