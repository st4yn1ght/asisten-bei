"""BO — Breakout dengan lonjakan volume (SPEC §12.2, §13.1 K_sinyal BO).

Syarat sinyal pada close `t` (semua harus benar; angka default ada di `strategi.bo`, P4):

- BO1 `close_t > HHV(high, lookback_tertinggi)_{t−1}` (kolom `k_hhv_high`, sudah tanpa bar t)
- BO2 `RasioVol(lookback_volume)_t ≥ min_rasio_volume`
- BO3 `close_t > SMA(sma_tren)_t` dan `SMA(sma_tren)_t > SMA(sma_panjang)_t`
- BO4 `CLV_t ≥ min_posisi_close`
- BO5 `(close_t − HHV(high, lookback_tertinggi)_{t−1}) / ATR_t ≤ maks_lewat_level_atr`

Stop awal (ruang tersesuaikan) = `close_t − stop_atr × ATR_t`; konversi ke nominal dengan F(t) dan
pembulatan ke atas dilakukan `Strategi._bangun_sinyal`. Tidak ada target tetap (`target_r = None`);
exit memakai trailing (`trailing_atr`, mesin exit §15).

`K_sinyal = 0,5 × clip((RasioVol − v_min)/v_min, 0, 1) + 0,5 × CLV`, dengan `v_min = min_rasio_volume`.

Nilai NaN membuat aturan tidak terpenuhi; ATR ≤ 0/NaN membuat BO5 (yang membagi dengan ATR) tidak
terpenuhi (§9). Semua aturan selalu dievaluasi dan dicantumkan, walau aturan sebelumnya gagal.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..indikator import k_hhv_high, k_rasio_vol, k_sma
from .dasar import CekAturan, EvaluasiStrategi, Strategi, a, atr_sah, nilai_baris

if TYPE_CHECKING:  # pragma: no cover
    from ..aturan_bei import AturanBEI
    from ..data.tampilan import DataView


def _ada(*nilai: float) -> bool:
    """Semua nilai tersedia (bukan NaN)."""
    return not any(math.isnan(x) for x in nilai)


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


class Breakout(Strategi):
    kode = "BO"

    def evaluasi(self, dv: "DataView", kode: str, bei: "AturanBEI") -> EvaluasiStrategi:
        p = self.cfg.strategi.bo
        baris = dv.baris(kode)
        if baris is None:
            return self.tanpa_sinyal(
                kode, [CekAturan("DATA", f"tidak ada bar valid pada {dv.as_of.isoformat()}", False)]
            )

        close = nilai_baris(baris, "close")
        hhv = nilai_baris(baris, k_hhv_high(p.lookback_tertinggi))
        rasio = nilai_baris(baris, k_rasio_vol(p.lookback_volume))
        sma_tren = nilai_baris(baris, k_sma(p.sma_tren))
        sma_panjang = nilai_baris(baris, k_sma(p.sma_panjang))
        clv = nilai_baris(baris, "clv")
        atr = nilai_baris(baris, "atr")
        v_min = float(p.min_rasio_volume)
        min_clv = float(p.min_posisi_close)
        maks_lewat = float(p.maks_lewat_level_atr)
        n_hhv, n_vol = p.lookback_tertinggi, p.lookback_volume

        cek: list[CekAturan] = []

        # BO1 — close menembus high tertinggi n bar sebelum t.
        cek.append(
            CekAturan(
                "BO1",
                f"close {a(close)} > high tertinggi {n_hhv} bar sebelumnya {a(hhv)}",
                _ada(close, hhv) and close > hhv,
            )
        )

        # BO2 — lonjakan volume terhadap rata-rata n bar sebelum t.
        cek.append(
            CekAturan(
                "BO2",
                f"rasio volume ({n_vol} bar sebelumnya) {a(rasio)} ≥ {a(v_min)}",
                _ada(rasio) and rasio >= v_min,
            )
        )

        # BO3 — tren naik: close > SMA tren dan SMA tren > SMA panjang.
        cek.append(
            CekAturan(
                "BO3",
                f"close {a(close)} > SMA{p.sma_tren} {a(sma_tren)} dan "
                f"SMA{p.sma_tren} {a(sma_tren)} > SMA{p.sma_panjang} {a(sma_panjang)}",
                _ada(close, sma_tren, sma_panjang) and close > sma_tren and sma_tren > sma_panjang,
            )
        )

        # BO4 — close di dekat high hari itu.
        cek.append(
            CekAturan(
                "BO4",
                f"posisi close dalam rentang hari ini (CLV) {a(clv)} ≥ {a(min_clv)}",
                _ada(clv) and clv >= min_clv,
            )
        )

        # BO5 — belum terlalu jauh di atas level tembus (dalam satuan ATR).
        dasar_bo5 = f"(close {a(close)} − high tertinggi {a(hhv)}) / ATR {a(atr)}"
        if not atr_sah(atr):
            cek.append(CekAturan("BO5", f"{dasar_bo5}: ATR tidak sah (≤ 0 atau tidak tersedia)", False))
        else:
            lewat = (close - hhv) / atr if _ada(close, hhv) else math.nan
            cek.append(
                CekAturan("BO5", f"{dasar_bo5} = {a(lewat)} ≤ {a(maks_lewat)}", _ada(lewat) and lewat <= maks_lewat)
            )

        if not all(c.lolos for c in cek):
            return self.tanpa_sinyal(kode, cek)

        k_sinyal = 0.5 * _clip01((rasio - v_min) / v_min) + 0.5 * clv
        return self._bangun_sinyal(
            dv,
            kode,
            cek,
            close=close,
            atr=atr,
            stop_tersesuaikan=close - float(p.stop_atr) * atr,
            k_sinyal=k_sinyal,
            target_r=None,
            rata_nilai=nilai_baris(baris, "rata_nilai"),
        )
