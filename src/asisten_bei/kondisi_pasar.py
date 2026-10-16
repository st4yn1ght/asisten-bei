"""Filter kondisi pasar (SPEC §10).

Dievaluasi pada close `t` lewat `DataView(as_of=t)` saja (P3); hasilnya berlaku untuk entry di `t+1`.
Logika identik di backtest, jurnal kertas, dan run harian.

Aturan dievaluasi berurutan, aturan pertama yang cocok menentukan status:

- K0 data tidak lengkap → JANGAN BELI
- K1 IHSG tren turun (close < SMA panjang dan SMA pendek < SMA panjang) → JANGAN BELI
- K2 breadth < `breadth_jangan_maks_pct` → JANGAN BELI
- K3 IHSG naik (close > SMA panjang, SMA pendek > SMA panjang, close > SMA pendek) dan
  breadth ≥ `breadth_boleh_min_pct` → BOLEH BELI
- K4 selain itu → HATI-HATI

Breadth (§10 "Masukan"):

- saham **tercatat** pada t: punya ≥ `breadth_sma` bar valid sebelum t (= sampai hari bursa sebelum t;
  bar non-bursa sudah dibuang sehingga keduanya sama);
- saham **terhitung**: tercatat, punya bar valid pada t, dan tidak beranomali as-of t dalam
  `breadth_sma` bar valid terakhir. Terhitung selalu subset tercatat;
- `Breadth_t` = persentase terhitung dengan close_t > SMA(close, breadth_sma)_t;
- `Cakupan_t` = terhitung ÷ tercatat.

Perbandingan persen memakai perkalian silang bilangan bulat × Decimal, sehingga tepat di batas
(mis. breadth tepat 50% lolos K3) tanpa galat pembulatan float.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import pandas as pd

from . import teks
from .indikator import k_sma

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .data.tampilan import DataView

StatusPasar = Literal["BOLEH_BELI", "HATI_HATI", "JANGAN_BELI"]
Aturan = Literal["K0", "K1", "K2", "K3", "K4"]

BOLEH_BELI: StatusPasar = "BOLEH_BELI"
HATI_HATI: StatusPasar = "HATI_HATI"
JANGAN_BELI: StatusPasar = "JANGAN_BELI"

TAMPILAN_STATUS: dict[str, str] = {
    BOLEH_BELI: "BOLEH BELI",
    HATI_HATI: "HATI-HATI",
    JANGAN_BELI: "JANGAN BELI",
}

KODE_IHSG = "IHSG"


@dataclass(frozen=True)
class Syarat:
    """Satu baris tabel syarat di laporan: kondisi, nilai aktual, ambang, dan apakah kondisinya benar."""

    aturan: str  # "K0" .. "K3"
    uraian: str
    nilai: str
    ambang: str
    terpenuhi: bool | None  # None = tidak dapat dinilai (data kurang)

    @property
    def tanda(self) -> str:
        return "—" if self.terpenuhi is None else ("✓" if self.terpenuhi else "✗")


@dataclass(frozen=True)
class RingkasanBreadth:
    n_tercatat: int
    n_terhitung: int
    n_di_atas_sma: int

    @property
    def breadth_pct(self) -> Decimal | None:
        if self.n_terhitung == 0:
            return None
        return Decimal(self.n_di_atas_sma) * 100 / Decimal(self.n_terhitung)

    @property
    def cakupan_pct(self) -> Decimal | None:
        if self.n_tercatat == 0:
            return None
        return Decimal(self.n_terhitung) * 100 / Decimal(self.n_tercatat)

    def breadth_kurang_dari(self, ambang_pct: Decimal) -> bool:
        """Breadth < ambang, tepat (perkalian silang). Hanya untuk n_terhitung > 0."""
        return Decimal(self.n_di_atas_sma) * 100 < Decimal(ambang_pct) * self.n_terhitung

    def cakupan_kurang_dari(self, ambang_pct: Decimal) -> bool:
        return Decimal(self.n_terhitung) * 100 < Decimal(ambang_pct) * self.n_tercatat


@dataclass(frozen=True)
class HasilKondisiPasar:
    tanggal: date
    status: StatusPasar
    aturan: Aturan
    alasan: str
    syarat: tuple[Syarat, ...]
    breadth: RingkasanBreadth
    ihsg_close: float | None
    ihsg_sma_pendek: float | None
    ihsg_sma_panjang: float | None
    kekurangan_data: tuple[str, ...]  # alasan K0 (kosong bila data lengkap)

    @property
    def tampilan(self) -> str:
        return TAMPILAN_STATUS[self.status]

    @property
    def data_lengkap(self) -> bool:
        return not self.kekurangan_data


@dataclass(frozen=True)
class ParameterEntry:
    """Akibat status pasar terhadap entry baru (§10 "Akibat status")."""

    boleh_entry: bool
    pengali_risiko: Decimal
    maks_entry_baru_per_hari: int | None  # None = hanya dibatasi slot


def parameter_entry(status: StatusPasar, cfg: "Config") -> ParameterEntry:
    r = cfg.risiko
    if status == BOLEH_BELI:
        return ParameterEntry(True, Decimal(1), r.maks_entry_baru_per_hari)
    if status == HATI_HATI:
        return ParameterEntry(True, Decimal(r.hati_hati.pengali_risiko), r.hati_hati.maks_entry_baru_per_hari)
    if status == JANGAN_BELI:
        return ParameterEntry(False, Decimal(0), 0)
    raise ValueError(f"status pasar tidak dikenal: {status!r}")


# ---------------------------------------------------------------------------
# Breadth
# ---------------------------------------------------------------------------


def _anomali_dalam_jendela(dv: "DataView", kode: str, jendela: int) -> bool:
    """Ada anomali (yang tidak tercatat di anomali_ok) as-of t dalam `jendela` bar valid terakhir ≤ t."""
    baris = dv.tanda(kode)
    if baris is None:
        return False
    a = baris.get("anomali_terakhir")
    if a is None or pd.isna(a):
        return False
    return dv.jumlah_bar_valid_sejak(kode, pd.Timestamp(a)) <= jendela


def hitung_breadth(dv: "DataView", kode_breadth: Iterable[str]) -> RingkasanBreadth:
    """Breadth dan cakupan pada `dv.as_of` (§10). Kode yang tidak ada di data run tidak tercatat."""
    n_sma = int(dv.cfg.kondisi_pasar.breadth_sma)
    kolom_sma = k_sma(n_sma)
    tercatat = terhitung = atas = 0
    for kode in dict.fromkeys(kode_breadth):  # urutan stabil, tanpa ganda
        if kode == KODE_IHSG or not dv.ada(kode):
            continue
        if dv.jumlah_bar_valid(kode, termasuk_as_of=False) < n_sma:
            continue  # belum tercatat (belum melantai / riwayat belum cukup)
        tercatat += 1
        if not dv.ada_bar_valid(kode):
            continue
        if _anomali_dalam_jendela(dv, kode, n_sma):
            continue
        terhitung += 1
        close = dv.sebelum(kode, "close", 0)
        sma = dv.sebelum(kode, kolom_sma, 0)
        if close > sma:  # NaN → False (tidak terjadi: terhitung menjamin ≥ n_sma + 1 bar valid)
            atas += 1
    return RingkasanBreadth(tercatat, terhitung, atas)


# ---------------------------------------------------------------------------
# Status pasar
# ---------------------------------------------------------------------------


def _nilai(baris: pd.Series | None, kolom: str) -> float | None:
    if baris is None or kolom not in baris.index:
        return None
    x = float(baris[kolom])
    return None if math.isnan(x) else x


def _cek_data_ihsg(dv: "DataView") -> list[str]:
    """Kekurangan data IHSG untuk K0 (§8.5 seri indeks, §10)."""
    cfg = dv.cfg
    tampil = dv.ihsg()
    if tampil is None:
        return ["data IHSG tidak tersedia"]
    kurang: list[str] = []
    if tampil.alasan_seri:
        kurang.append(f"data IHSG tidak dapat dipakai ({tampil.alasan_seri})")
    baris = tampil.tanda()
    if baris is None or not bool(baris.get("ada_bar_valid", False)):
        kurang.append(f"bar IHSG {dv.as_of.isoformat()} tidak ada")
    if baris is not None:
        hilang = int(baris.get("bar_hilang", 0))
        maks = cfg.data.validasi.maks_bar_hilang
        if hilang > maks:
            jendela = cfg.data.validasi.jendela_kelengkapan_bar
            kurang.append(f"data IHSG bolong ({hilang} bar hilang dalam {jendela} hari bursa terakhir > {maks})")
        if bool(baris.get("anomali_aktif", False)):
            kurang.append("anomali data IHSG")
    return kurang


def hitung_kondisi_pasar(dv: "DataView", kode_breadth: Iterable[str]) -> HasilKondisiPasar:
    """Status pasar pada `dv.as_of` beserta alasan dan tabel syarat lengkap (§10)."""
    cfg = dv.cfg
    kp = cfg.kondisi_pasar
    n_pendek, n_panjang = int(kp.sma_pendek), int(kp.sma_panjang)

    baris_ihsg = dv.ihsg_baris() if dv.ihsg() is not None else None
    close = _nilai(baris_ihsg, "close")
    sma_pendek = _nilai(baris_ihsg, k_sma(n_pendek))
    sma_panjang = _nilai(baris_ihsg, k_sma(n_panjang))
    br = hitung_breadth(dv, kode_breadth)

    # -- K0: data ----------------------------------------------------------------
    kurang = _cek_data_ihsg(dv)
    ihsg_ada_bar = baris_ihsg is not None
    if ihsg_ada_bar and sma_panjang is None:
        kurang.append(f"SMA{n_panjang} IHSG belum terdefinisi (riwayat kurang)")
    if ihsg_ada_bar and sma_pendek is None:
        kurang.append(f"SMA{n_pendek} IHSG belum terdefinisi (riwayat kurang)")
    if br.n_tercatat == 0:
        kurang.append("tidak ada saham breadth yang tercatat")
    elif br.cakupan_kurang_dari(kp.min_cakupan_breadth_pct):
        kurang.append(
            f"cakupan breadth {teks.persen(br.cakupan_pct)} < {teks.persen(kp.min_cakupan_breadth_pct)}"
            f" ({br.n_terhitung} dari {br.n_tercatat} saham)"
        )
    if br.n_terhitung < kp.min_saham_breadth:
        kurang.append(f"saham breadth terhitung {br.n_terhitung} < {kp.min_saham_breadth}")

    ihsg_lengkap = close is not None and sma_pendek is not None and sma_panjang is not None
    if not ihsg_lengkap and not kurang:
        kurang.append("nilai IHSG tidak lengkap")
    syarat = _tabel_syarat(dv, br, close, sma_pendek, sma_panjang)

    def hasil(status: StatusPasar, aturan: Aturan, alasan: str) -> HasilKondisiPasar:
        return HasilKondisiPasar(
            tanggal=dv.as_of, status=status, aturan=aturan, alasan=alasan, syarat=syarat, breadth=br,
            ihsg_close=close, ihsg_sma_pendek=sma_pendek, ihsg_sma_panjang=sma_panjang,
            kekurangan_data=tuple(kurang),
        )

    if kurang:
        return hasil(JANGAN_BELI, "K0", "Data tidak lengkap: " + "; ".join(kurang))
    assert close is not None and sma_pendek is not None and sma_panjang is not None

    # -- K1: tren IHSG turun ---------------------------------------------------------
    if close < sma_panjang and sma_pendek < sma_panjang:
        return hasil(JANGAN_BELI, "K1", "IHSG dalam tren turun")

    # -- K2: breadth lemah -------------------------------------------------------------
    if br.breadth_kurang_dari(kp.breadth_jangan_maks_pct):
        return hasil(
            JANGAN_BELI, "K2",
            f"Breadth lemah: {teks.persen(br.breadth_pct)} < {teks.persen(kp.breadth_jangan_maks_pct)}",
        )

    # -- K3: tren naik dan breadth sehat ---------------------------------------------------
    gagal_k3: list[str] = []
    if not close > sma_panjang:
        gagal_k3.append(f"close IHSG {teks.angka(close)} tidak di atas SMA{n_panjang} {teks.angka(sma_panjang)}")
    if not sma_pendek > sma_panjang:
        gagal_k3.append(
            f"SMA{n_pendek} {teks.angka(sma_pendek)} tidak di atas SMA{n_panjang} {teks.angka(sma_panjang)}"
        )
    if not close > sma_pendek:
        gagal_k3.append(f"close IHSG {teks.angka(close)} tidak di atas SMA{n_pendek} {teks.angka(sma_pendek)}")
    if br.breadth_kurang_dari(kp.breadth_boleh_min_pct):
        gagal_k3.append(
            f"breadth {teks.persen(br.breadth_pct)} < {teks.persen(kp.breadth_boleh_min_pct)}"
        )
    if not gagal_k3:
        return hasil(BOLEH_BELI, "K3", "Tren IHSG naik dan breadth sehat")

    # -- K4 ---------------------------------------------------------------------------------
    return hasil(HATI_HATI, "K4", "Syarat BOLEH BELI belum terpenuhi: " + "; ".join(gagal_k3))


def _tabel_syarat(
    dv: "DataView",
    br: RingkasanBreadth,
    close: float | None,
    sma_pendek: float | None,
    sma_panjang: float | None,
) -> tuple[Syarat, ...]:
    """Semua syarat K0–K3 dengan nilai aktual, ambang, dan ✓/✗ (§10: selalu ditampilkan lengkap)."""
    kp = dv.cfg.kondisi_pasar
    v = dv.cfg.data.validasi
    n_pendek, n_panjang = int(kp.sma_pendek), int(kp.sma_panjang)
    a = teks.angka
    p = teks.persen
    tampil = dv.ihsg()
    baris_tanda = tampil.tanda() if tampil is not None else None
    ada_bar = baris_tanda is not None and bool(baris_tanda.get("ada_bar_valid", False))
    hilang = int(baris_tanda.get("bar_hilang", 0)) if baris_tanda is not None else None
    anomali = bool(baris_tanda.get("anomali_aktif", False)) if baris_tanda is not None else None
    breadth = br.breadth_pct
    cakupan = br.cakupan_pct

    def banding(kiri: float | None, kanan: float | None, lebih_besar: bool) -> bool | None:
        if kiri is None or kanan is None:
            return None
        return kiri > kanan if lebih_besar else kiri < kanan

    def breadth_kurang(ambang: Decimal) -> bool | None:
        return None if breadth is None else br.breadth_kurang_dari(ambang)

    alasan_seri = tampil.alasan_seri if tampil is not None else None
    if tampil is None:
        data_dipakai, dapat = "tidak tersedia", False
    elif alasan_seri:
        data_dipakai, dapat = alasan_seri, False
    else:
        data_dipakai, dapat = "ya", True
    return (
        Syarat("K0", "Data IHSG tersedia dan dapat dipakai", data_dipakai, "ya", dapat),
        Syarat("K0", f"Bar IHSG {dv.as_of.isoformat()} tersedia", "ada" if ada_bar else "tidak ada", "ada",
               ada_bar),
        Syarat("K0", f"Bar IHSG hilang dalam {v.jendela_kelengkapan_bar} hari bursa terakhir",
               a(hilang, 0) if hilang is not None else teks.STRIP, f"≤ {v.maks_bar_hilang}",
               None if hilang is None else hilang <= v.maks_bar_hilang),
        Syarat("K0", "Tanpa anomali data IHSG", teks.STRIP if anomali is None else ("anomali" if anomali else "tidak ada"),
               "tidak ada", None if anomali is None else not anomali),
        Syarat("K0", f"SMA{n_panjang} IHSG terdefinisi", a(sma_panjang), "terdefinisi",
               None if not ada_bar else sma_panjang is not None),
        Syarat("K0", f"SMA{n_pendek} IHSG terdefinisi", a(sma_pendek), "terdefinisi",
               None if not ada_bar else sma_pendek is not None),
        Syarat("K0", f"Cakupan breadth ({br.n_terhitung}/{br.n_tercatat} saham)", p(cakupan),
               f"≥ {p(kp.min_cakupan_breadth_pct)}",
               None if cakupan is None else not br.cakupan_kurang_dari(kp.min_cakupan_breadth_pct)),
        Syarat("K0", "Saham breadth terhitung", a(br.n_terhitung, 0), f"≥ {kp.min_saham_breadth}",
               br.n_terhitung >= kp.min_saham_breadth),
        Syarat("K1", f"IHSG close < SMA{n_panjang}", f"{a(close)} vs {a(sma_panjang)}", "tren turun bila ✓",
               banding(close, sma_panjang, False)),
        Syarat("K1", f"SMA{n_pendek} < SMA{n_panjang}", f"{a(sma_pendek)} vs {a(sma_panjang)}", "tren turun bila ✓",
               banding(sma_pendek, sma_panjang, False)),
        Syarat("K2", f"Breadth (close > SMA{kp.breadth_sma})", p(breadth),
               f"< {p(kp.breadth_jangan_maks_pct)} → JANGAN BELI", breadth_kurang(kp.breadth_jangan_maks_pct)),
        Syarat("K3", f"IHSG close > SMA{n_panjang}", f"{a(close)} vs {a(sma_panjang)}", "wajib ✓",
               banding(close, sma_panjang, True)),
        Syarat("K3", f"SMA{n_pendek} > SMA{n_panjang}", f"{a(sma_pendek)} vs {a(sma_panjang)}", "wajib ✓",
               banding(sma_pendek, sma_panjang, True)),
        Syarat("K3", f"IHSG close > SMA{n_pendek}", f"{a(close)} vs {a(sma_pendek)}", "wajib ✓",
               banding(close, sma_pendek, True)),
        Syarat("K3", "Breadth", p(breadth), f"≥ {p(kp.breadth_boleh_min_pct)}",
               None if breadth is None else not br.breadth_kurang_dari(kp.breadth_boleh_min_pct)),
    )
