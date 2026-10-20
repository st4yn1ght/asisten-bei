# Daftar cek fitur order broker (SPEC §24 item 11)

> **BUKAN SARAN INVESTASI.** Daftar ini membantu Anda mencocokkan asumsi program dengan aplikasi broker Anda.

Item ini tidak punya berkas input. Tanyakan ke broker atau periksa di aplikasinya, lalu tulis jawabannya di
kolom "Jawaban Anda" (simpan salinan berkas ini untuk catatan Anda). Kolom "Asumsi program" menunjukkan apa yang
dianggap benar oleh simulasi; kolom terakhir menjelaskan tindakan bila keadaan di broker Anda berbeda.
Kerjakan sebelum backtest pertama.

| # | Pertanyaan | Asumsi program | Jawaban Anda | Bila berbeda dari asumsi program |
|---|---|---|---|---|
| a1 | Bisakah memasang **order otomatis stop** (jual otomatis bila harga turun ke level tertentu)? | Bisa | | Catat, dan jangan memakai rekomendasi untuk uang sungguhan sampai program/SPEC diperbarui (§15.2). |
| a2 | Bisakah memasang **order otomatis target** (jual otomatis bila harga naik ke level tertentu)? | Bisa | | Sama seperti a1. |
| a3 | Bisakah order otomatis stop/target dipasang untuk saham yang **baru dibeli hari itu**? | Bisa | | Ubah `entry.stop_aktif_hari_entry: false` di `config.yaml` sebelum backtest pertama. |
| b1 | Bagaimana cara **menarik (withdraw) order beli** yang tidak terisi saat pembukaan? | Bisa ditarik segera setelah pembukaan | (tulis langkahnya) | Bila tidak bisa ditarik, sama seperti a1. |
| b2 | Apakah ada **masa berlaku order** yang otomatis membatalkan order (mis. hanya berlaku hari itu)? | Tidak diandalkan: Anda menarik sendiri order yang tidak terisi | (tulis jawabannya) | Tidak ada perubahan; tetap tarik sendiri order yang tidak terisi. |
| c | Bagaimana cara **menjual di harga pembukaan** (untuk jual terjadwal)? | Bisa | (tulis caranya) | Bila tidak bisa, sama seperti a1. |
| d | Apakah **hasil jual yang belum terjadi** (order jual yang menunggu) ikut dihitung dalam limit beli? | Tidak dipakai: `risiko.dana_jual_langsung_dipakai: false` | | Tidak ada perubahan untuk jawaban Ya maupun Tidak. Jangan mengubahnya ke `true` tanpa memperbarui SPEC (§25 butir 3). |

> **BUKAN SARAN INVESTASI.** Seluruh keputusan dan risiko transaksi menjadi tanggung jawab pengguna.
