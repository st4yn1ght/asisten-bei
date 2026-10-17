"""ACAK — pembanding acak, khusus backtest (SPEC §12.4; `K_sinyal` ACAK §13.1).

Untuk setiap saham pada `t` (= `dv.as_of`):

    u = int.from_bytes(sha256(f"{seed}|{t:%Y-%m-%d}|{kode}".encode()).digest()[:8], "big") / 2**64

Sinyal muncul bila `u < peluang_sinyal_pct/100` (satu cek "ACAK"). `u` hanya bergantung pada
(seed, tanggal, kode), sehingga identik antar run dan antar proses. Fungsi `hash()` bawaan Python
dilarang (diacak per proses lewat PYTHONHASHSEED).

Stop awal (ruang tersesuaikan) = `close_t − stop_atr × ATR_t`; ATR wajib sah (> 0, bukan NaN) dan stop
wajib > 0, bila tidak cek "STOP" gagal dan tidak ada sinyal. Target = `target_r` R (dihitung saat isi).
`K_sinyal` = 0,5. `harga_maks`, konversi nominal `× F(t)`, dan pembulatan dikerjakan
`Strategi._bangun_sinyal`. Stop waktu (`stop_waktu_hari`) ditangani mesin exit (§15).

Tidak pernah muncul di laporan harian, jurnal, atau run gabungan (`daftar_strategi(termasuk_acak=False)`).
"""

from __future__ import annotations

import hashlib
import math
from datetime import date
from typing import TYPE_CHECKING

from .dasar import CekAturan, EvaluasiStrategi, Strategi, a, atr_sah, nilai_baris

if TYPE_CHECKING:  # pragma: no cover
    from ..aturan_bei import AturanBEI
    from ..data.tampilan import DataView

K_SINYAL_ACAK = 0.5  # §13.1, tetap (bukan parameter config)


def nilai_u(seed: int, tanggal: date, kode: str) -> float:
    """`u` §12.4 dalam [0, 1): 8 byte pertama SHA-256 dari "seed|YYYY-MM-DD|KODE" dibagi 2**64."""
    pesan = f"{seed}|{tanggal:%Y-%m-%d}|{kode}".encode()
    return int.from_bytes(hashlib.sha256(pesan).digest()[:8], "big") / 2**64


class Acak(Strategi):
    """ACAK — sinyal acak deterministik berseed untuk memeriksa mesin backtest (SPEC §12.4)."""

    kode = "ACAK"

    def evaluasi(self, dv: "DataView", kode: str, bei: "AturanBEI") -> EvaluasiStrategi:
        p = self.cfg.strategi.acak
        baris = dv.baris(kode)
        if baris is None:
            return self.tanpa_sinyal(
                kode, [CekAturan("DATA", f"tidak ada bar valid pada {dv.as_of.isoformat()}", False)]
            )

        peluang_pct = float(p.peluang_sinyal_pct)
        batas = peluang_pct / 100
        u = nilai_u(p.seed, dv.as_of, kode)
        cek = [
            CekAturan(
                "ACAK",
                f"u = {a(u, 6)} < peluang {a(batas, 6)} ({a(peluang_pct)}%, seed {p.seed})",
                u < batas,
            )
        ]
        if not cek[0].lolos:
            return self.tanpa_sinyal(kode, cek)

        close = nilai_baris(baris, "close")
        atr = nilai_baris(baris, "atr")
        stop_atr = float(p.stop_atr)
        stop_adj = close - stop_atr * atr if atr_sah(atr) else math.nan
        if not stop_adj > 0:  # NaN (ATR tidak sah) atau ≤ 0
            alasan = "ATR tidak sah (≤ 0 atau tidak tersedia)" if not atr_sah(atr) else "≤ 0"
            cek.append(
                CekAturan(
                    "STOP",
                    f"stop = close {a(close)} − {a(stop_atr)} × ATR {a(atr)} tidak dapat dipakai: {alasan}",
                    False,
                )
            )
            return self.tanpa_sinyal(kode, cek)

        return self._bangun_sinyal(
            dv,
            kode,
            cek,
            close=close,
            atr=atr,
            stop_tersesuaikan=stop_adj,
            k_sinyal=K_SINYAL_ACAK,
            target_r=float(p.target_r),
            rata_nilai=nilai_baris(baris, "rata_nilai"),
        )
