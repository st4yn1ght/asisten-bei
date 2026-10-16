"""Skor kandidat (SPEC §13.1).

    skor = b_sinyal × K_sinyal + b_rs × K_rs + b_likuid × K_likuid + b_stop × K_stop   (bobot `skor.bobot`, Σ = 100)

- `K_sinyal`: dihitung strategi (§13.1 per strategi), sudah di [0, 1].
- `K_rs`: peringkat persentil `Ret(skor.rs_lookback)_t` di antara saham eligible (§11 syarat 1–5) pada t:
  `(peringkat_rata2 − 1)/(n − 1)`, peringkat 1 = Ret terendah, nilai sama = rata-rata peringkat; n = 1 → 0,5.
  Saham eligible yang Ret-nya NaN tidak ikut diperingkat dan K_rs-nya 0. Tidak bergantung pada posisi,
  mode, atau strategi.
- `K_likuid = clip(log10(RataNilai_t / likuiditas.nilai_transaksi_min_idr), 0, 1)`.
- `K_stop = clip((jarak_stop_buruk_pct − jarak_pct)/(jarak_stop_buruk_pct − jarak_stop_ideal_pct), 0, 1)`,
  jarak_pct = (harga_maks − stop)/harga_maks × 100 setelah pembulatan.

Peringkat, pemecah nilai sama, dan deduplikasi memakai skor presisi penuh; `tampilan` (1 desimal) hanya
untuk laporan. Skor adalah heuristik pemeringkat, bukan peluang keberhasilan.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import teks
from .indikator import k_ret
from .strategi.dasar import Sinyal

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .data.tampilan import DataView

CATATAN_SKOR = "Skor adalah heuristik pemeringkat, bukan peluang keberhasilan."


def _clip01(x: float) -> float:
    if math.isnan(x):
        return 0.0
    return min(max(x, 0.0), 1.0)


@dataclass(frozen=True)
class KomponenSkor:
    k_sinyal: float
    k_rs: float
    k_likuid: float
    k_stop: float
    skor: float  # presisi penuh, 0..100

    @property
    def tampilan(self) -> str:
        return teks.angka(self.skor, 1)


@dataclass(frozen=True)
class SinyalBerskor:
    sinyal: Sinyal
    komponen: KomponenSkor

    @property
    def skor(self) -> float:
        return self.komponen.skor

    @property
    def kode(self) -> str:
        return self.sinyal.kode

    @property
    def strategi(self) -> str:
        return self.sinyal.strategi


def hitung_k_rs(dv: "DataView", kode_eligible: Iterable[str]) -> dict[str, float]:
    """K_rs untuk setiap kode eligible pada `dv.as_of`."""
    kolom = k_ret(int(dv.cfg.skor.rs_lookback))
    nilai: dict[str, float] = {}
    hasil: dict[str, float] = {}
    for kode in sorted(set(kode_eligible)):
        r = dv.sebelum(kode, kolom, 0)
        if math.isnan(r):
            hasil[kode] = 0.0
        else:
            nilai[kode] = r
    n = len(nilai)
    if n == 1:
        hasil.update({k: 0.5 for k in nilai})
        return hasil
    urut = sorted(nilai.items(), key=lambda kv: kv[1])
    i = 0
    while i < n:
        j = i
        while j + 1 < n and urut[j + 1][1] == urut[i][1]:
            j += 1
        peringkat = (i + 1 + j + 1) / 2  # rata-rata peringkat (1-based) untuk nilai sama
        for k in range(i, j + 1):
            hasil[urut[k][0]] = (peringkat - 1) / (n - 1)
        i = j + 1
    return hasil


def hitung_skor(sinyal: Sinyal, k_rs: float, cfg: "Config") -> KomponenSkor:
    b = cfg.skor.bobot
    s = cfg.skor
    ambang = float(cfg.likuiditas.nilai_transaksi_min_idr)
    if sinyal.rata_nilai > 0 and ambang > 0:
        k_likuid = _clip01(math.log10(sinyal.rata_nilai / ambang))
    elif ambang == 0 and sinyal.rata_nilai > 0:
        k_likuid = 1.0
    else:
        k_likuid = 0.0
    ideal, buruk = float(s.jarak_stop_ideal_pct), float(s.jarak_stop_buruk_pct)
    k_stop = _clip01((buruk - sinyal.jarak_stop_pct) / (buruk - ideal))
    k_sinyal = _clip01(sinyal.k_sinyal)
    k_rs = _clip01(k_rs)
    skor = (
        float(b.sinyal) * k_sinyal
        + float(b.kekuatan_relatif) * k_rs
        + float(b.likuiditas) * k_likuid
        + float(b.jarak_stop) * k_stop
    )
    return KomponenSkor(k_sinyal, k_rs, k_likuid, k_stop, skor)


def beri_skor(dv: "DataView", sinyal: Sequence[Sinyal], kode_eligible: Iterable[str]) -> list[SinyalBerskor]:
    """Skor untuk setiap sinyal; K_rs dihitung atas seluruh `kode_eligible` pada `dv.as_of`."""
    k_rs = hitung_k_rs(dv, kode_eligible)
    return [SinyalBerskor(s, hitung_skor(s, k_rs.get(s.kode, 0.0), dv.cfg)) for s in sinyal]
