"""Strategi PB — pullback dalam tren naik (SPEC §12.1; `K_sinyal` PB §13.1).

Semua aturan dievaluasi pada close `t` (= `dv.as_of`) dan selalu dicantumkan, juga sesudah satu aturan
gagal, dengan nilai aktual di uraiannya:

| # | Aturan (kunci `strategi.pb.*`) |
|---|---|
| PB1 | `close_t > SMA(sma_tren)_t` dan `SMA(sma_tren)_t > SMA(sma_panjang)_t` |
| PB2 | `SMA(sma_tren)_t > SMA(sma_tren)_{t−lookback_slope_sma_tren}` |
| PB3 | ada `k ∈ {0, …, lookback_sentuh_sma_cepat − 1}` dengan `low_{t−k} ≤ SMA(sma_cepat)_{t−k}` |
| PB4 | `close_t > high_{t−1}` dan `close_t > open_t` |
| PB5 | `(close_t − SMA(sma_cepat)_t) / ATR_t ≤ maks_jarak_atas_sma_cepat_atr` |

Stop awal (ruang tersesuaikan) = `LLV(low, lookback_swing_low)_t − penyangga_stop_atr × ATR_t`;
`harga_maks`, konversi nominal `× F(t)`, pembulatan, dan validasi `STOP_TERLALU_JAUH`
(`maks_jarak_stop_atr`) dikerjakan `Strategi._bangun_sinyal`. Target = `target_r` R (dihitung saat isi).

`K_sinyal` PB = `0,5 × (1 − clip((close − SMA_cepat)/ATR, 0, 1)) + 0,5 × CLV`.

Nilai NaN (riwayat kurang) membuat aturan tidak terpenuhi; ATR ≤ 0 atau NaN membuat PB5 (yang membagi
dengan ATR) tidak terpenuhi (§9). `x_{t−k}` dibaca lewat `dv.sebelum` (bar valid ke-k sebelum t). Semua
angka aturan berasal dari config (P4).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..indikator import k_llv_low, k_sma
from .dasar import CekAturan, EvaluasiStrategi, Strategi, a, atr_sah, nilai_baris

if TYPE_CHECKING:  # pragma: no cover
    from ..aturan_bei import AturanBEI
    from ..data.tampilan import DataView


def _ada(*nilai: float) -> bool:
    """Semua nilai tersedia (bukan NaN)."""
    return all(not math.isnan(x) for x in nilai)


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def _bar_ke(k: int) -> str:
    return "hari ini" if k == 0 else f"{k} bar lalu"


class Pullback(Strategi):
    """PB — pullback ke SMA cepat dalam tren naik, dipicu pantulan (SPEC §12.1)."""

    kode = "PB"

    def evaluasi(self, dv: "DataView", kode: str, bei: "AturanBEI") -> EvaluasiStrategi:
        p = self.cfg.strategi.pb
        baris = dv.baris(kode)
        if baris is None:
            return self.tanpa_sinyal(
                kode, [CekAturan("DATA", f"tidak ada bar valid pada {dv.as_of.isoformat()}", False)]
            )

        n_cepat, n_tren, n_panjang = p.sma_cepat, p.sma_tren, p.sma_panjang
        close = nilai_baris(baris, "close")
        buka = nilai_baris(baris, "open")
        atr = nilai_baris(baris, "atr")
        clv = nilai_baris(baris, "clv")
        sma_cepat = nilai_baris(baris, k_sma(n_cepat))
        sma_tren = nilai_baris(baris, k_sma(n_tren))
        sma_panjang = nilai_baris(baris, k_sma(n_panjang))

        # PB1 — tren naik: close di atas SMA tren, SMA tren di atas SMA panjang.
        pb1 = _ada(close, sma_tren, sma_panjang) and close > sma_tren and sma_tren > sma_panjang
        cek = [
            CekAturan(
                "PB1",
                f"close {a(close)} > SMA{n_tren} {a(sma_tren)} dan "
                f"SMA{n_tren} {a(sma_tren)} > SMA{n_panjang} {a(sma_panjang)}",
                pb1,
            )
        ]

        # PB2 — SMA tren menanjak dibanding `lookback_slope_sma_tren` bar valid sebelumnya.
        j = p.lookback_slope_sma_tren
        sma_tren_lalu = dv.sebelum(kode, k_sma(n_tren), j)
        pb2 = _ada(sma_tren, sma_tren_lalu) and sma_tren > sma_tren_lalu
        cek.append(
            CekAturan("PB2", f"SMA{n_tren} {a(sma_tren)} > SMA{n_tren} {j} bar lalu {a(sma_tren_lalu)}", pb2)
        )

        # PB3 — dalam `lookback_sentuh_sma_cepat` bar terakhir (termasuk t), low menyentuh/menembus
        # SMA cepat pada bar yang sama.
        m = p.lookback_sentuh_sma_cepat
        pb3 = False
        rincian = []
        for k in range(m):
            low_k = dv.sebelum(kode, "low", k)
            sma_k = dv.sebelum(kode, k_sma(n_cepat), k)
            if _ada(low_k, sma_k):
                sentuh = low_k <= sma_k
                tanda = "≤" if sentuh else ">"
            else:
                sentuh, tanda = False, "vs"
            pb3 = pb3 or sentuh
            rincian.append(f"{_bar_ke(k)} low {a(low_k)} {tanda} SMA{n_cepat} {a(sma_k)}")
        cek.append(
            CekAturan("PB3", f"low ≤ SMA{n_cepat} dalam {m} bar terakhir: " + "; ".join(rincian), pb3)
        )

        # PB4 — pemicu pantulan.
        high_kemarin = dv.sebelum(kode, "high", 1)
        pb4 = _ada(close, high_kemarin, buka) and close > high_kemarin and close > buka
        cek.append(
            CekAturan(
                "PB4",
                f"close {a(close)} > high kemarin {a(high_kemarin)} dan close {a(close)} > open {a(buka)}",
                pb4,
            )
        )

        # PB5 — close belum terlalu jauh di atas SMA cepat (dalam satuan ATR).
        batas = float(p.maks_jarak_atas_sma_cepat_atr)
        if atr_sah(atr) and _ada(close, sma_cepat):
            jarak_atr = (close - sma_cepat) / atr
            pb5 = jarak_atr <= batas
            uraian5 = (
                f"(close {a(close)} − SMA{n_cepat} {a(sma_cepat)}) / ATR {a(atr)} = {a(jarak_atr)} ≤ {a(batas)}"
            )
        else:
            jarak_atr, pb5 = math.nan, False
            uraian5 = (
                f"(close {a(close)} − SMA{n_cepat} {a(sma_cepat)}) / ATR {a(atr)} tidak dapat dihitung "
                f"(ATR ≤ 0 atau data kurang); batas {a(batas)}"
            )
        cek.append(CekAturan("PB5", uraian5, pb5))

        if not all(c.lolos for c in cek):
            return self.tanpa_sinyal(kode, cek)

        # Stop awal di ruang tersesuaikan; konversi nominal & pembulatan di `_bangun_sinyal`.
        n_swing = p.lookback_swing_low
        llv_low = nilai_baris(baris, k_llv_low(n_swing))
        stop_adj = llv_low - float(p.penyangga_stop_atr) * atr
        if not _ada(stop_adj) or stop_adj <= 0:
            cek.append(
                CekAturan(
                    "STOP",
                    f"stop = LLV low {n_swing} bar {a(llv_low)} − {a(float(p.penyangga_stop_atr))} × "
                    f"ATR {a(atr)} = {a(stop_adj)} tidak dapat dipakai (data kurang atau ≤ 0)",
                    False,
                )
            )
            return self.tanpa_sinyal(kode, cek)

        k_sinyal = 0.5 * (1.0 - _clip01(jarak_atr)) + 0.5 * clv
        return self._bangun_sinyal(
            dv,
            kode,
            cek,
            close=close,
            atr=atr,
            stop_tersesuaikan=stop_adj,
            k_sinyal=k_sinyal,
            target_r=float(p.target_r),
            rata_nilai=nilai_baris(baris, "rata_nilai"),
            maks_jarak_stop_atr=float(p.maks_jarak_stop_atr),
        )
