# Daftar cek fitur order broker (SPEC §24 item 11)

> **BUKAN SARAN INVESTASI.** Daftar ini membantu Anda mencocokkan asumsi program dengan aplikasi broker Anda.

Item ini tidak punya berkas input. Tanyakan ke broker atau periksa di aplikasinya, lalu tulis jawabannya di
kolom "Jawaban Anda" (simpan salinan berkas ini untuk catatan Anda). Simulasi program mengasumsikan semua
jawaban di bawah **Ya** (untuk pertanyaan d: lihat kolom terakhir). Bila ada jawaban yang berbeda, lakukan
tindakan di kolom terakhir **sebelum** memakai rekomendasi.

| # | Pertanyaan | Jawaban Anda | Bila jawabannya **Tidak** |
|---|---|---|---|
| a1 | Bisakah memasang **order otomatis stop** (jual otomatis bila harga turun ke level tertentu)? | Ya / Tidak | Asumsi program tidak berlaku: catat, lalu perbarui SPEC (§15.2). |
| a2 | Bisakah memasang **order otomatis target** (jual otomatis bila harga naik ke level tertentu)? | Ya / Tidak | Asumsi program tidak berlaku: catat, lalu perbarui SPEC (§15.2). |
| a3 | Bisakah order otomatis stop/target dipasang untuk saham yang **baru dibeli hari itu**? | Ya / Tidak | Ubah `entry.stop_aktif_hari_entry: false` di `config.yaml`, lalu jalankan ulang backtest. |
| b1 | Bagaimana cara **menarik (withdraw) order beli** yang tidak terisi saat pembukaan? | (tulis langkahnya) | Bila tidak bisa ditarik, asumsi program tidak berlaku: catat, lalu perbarui SPEC. |
| b2 | Apakah ada **masa berlaku order** yang otomatis membatalkan order (mis. hanya berlaku hari itu)? | Ya, berlaku … / Tidak | Catat jawabannya; laporan harian tetap meminta Anda menarik order yang tidak terisi. |
| c | Bagaimana cara **menjual di harga pembukaan** (untuk jual terjadwal)? | (tulis caranya) | Bila tidak bisa, asumsi program tidak berlaku: catat, lalu perbarui SPEC. |
| d | Apakah **hasil jual yang belum terjadi** (order jual yang menunggu) ikut dihitung dalam limit beli? | Ya / Tidak | Default program sudah menganggap **Tidak** (`risiko.dana_jual_langsung_dipakai: false`). Ubah ke `true` hanya bila jawabannya Ya, lalu jalankan ulang backtest. |

> **BUKAN SARAN INVESTASI.** Seluruh keputusan dan risiko transaksi menjadi tanggung jawab pengguna.
