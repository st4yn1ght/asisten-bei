"""Periode, jendela, dan holdout (SPEC §16.4).

- **In-sample:** `backtest.mulai` … hari bursa terakhir sebelum `holdout_mulai` (`akhir_insample`).
- **Holdout:** `holdout_mulai` … akhir data backtest.
- **Penuh:** `backtest.mulai` … akhir data backtest.
- **Jendela:** setiap tahun kalender di dalam in-sample; tahun yang tidak tercakup penuh berlabel "parsial".

Titik awal setiap periode/jendela = close hari bursa terakhir sebelum periode itu (baris ekuitas terakhir
bertanggal < awal periode); untuk periode pertama: baris pertama simulasi (modal awal, `tanggal_awal`).

Transaksi masuk ke periode/jendela berdasarkan **tanggal entry**. Untuk statistik in-sample (termasuk
jendela), transaksi yang masih terbuka pada `akhir_insample` diperlakukan `TERBUKA`: dinilai pada close hari
itu (`pos.meta["nilai_pada"][akhir_insample]`, dicatat mesin bila `simulasikan(...,
tanggal_penilaian=tanggal_penilaian(...))`), tidak masuk PF/win rate, dan bertanda `MELEWATI_HOLDOUT`
dalam tampilan in-sample. Harga periode holdout tidak pernah masuk statistik in-sample.

Fungsi di modul ini tidak mengubah objek masukan, kecuali `tandai_melewati_holdout` (eksplisit, untuk
kolom `tanda_transaksi` berkas transaksi).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from .. import exit as ex
from . import metrik as mt

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..kalender import Kalender
    from .mesin import BarisEkuitas, HasilSimulasi, SinyalDilewati

IN_SAMPLE = "in_sample"
HOLDOUT = "holdout"
PENUH = "penuh"
JENDELA = "jendela"

LABEL_JENIS = {IN_SAMPLE: "In-sample", HOLDOUT: "Holdout", PENUH: "Penuh"}
LABEL_PARSIAL = "parsial"


# ---------------------------------------------------------------------------
# Periode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Periode:
    jenis: str  # in_sample | holdout | penuh | jendela
    mulai: date  # batas nominal, inklusif (tanggal entry ≥ mulai)
    akhir: date  # batas nominal, inklusif (tanggal entry ≤ akhir)
    tahun: int | None = None  # hanya jendela
    parsial: bool = False  # hanya jendela

    @property
    def kunci(self) -> str:
        """"in_sample", "holdout", "penuh", atau tahun ("2015") untuk jendela."""
        return str(self.tahun) if self.jenis == JENDELA else self.jenis

    @property
    def label(self) -> str:
        if self.jenis == JENDELA:
            return f"{self.tahun} ({LABEL_PARSIAL})" if self.parsial else str(self.tahun)
        return LABEL_JENIS[self.jenis]


def akhir_insample(kal: "Kalender", cfg: "Config") -> date:
    """Hari bursa terakhir sebelum `backtest.holdout_mulai` (§16.4)."""
    return kal.hari_bursa_sebelum(cfg.backtest.holdout_mulai)


def tanggal_penilaian(kal: "Kalender", cfg: "Config") -> tuple[date, ...]:
    """Tanggal yang wajib diteruskan ke `mesin.simulasikan(..., tanggal_penilaian=...)` agar posisi terbuka
    pada akhir in-sample bisa dinilai pada close hari itu."""
    return (akhir_insample(kal, cfg),)


def daftar_jendela(mulai: date, holdout_mulai: date) -> list[Periode]:
    """Satu jendela per tahun kalender di in-sample [mulai, holdout_mulai − 1 hari]. Parsial bila tahun itu
    tidak tercakup dari 1 Januari sampai 31 Desember."""
    akhir = holdout_mulai - timedelta(days=1)
    hasil = []
    for y in range(mulai.year, akhir.year + 1):
        a, b = max(mulai, date(y, 1, 1)), min(akhir, date(y, 12, 31))
        if a > b:
            continue
        hasil.append(Periode(JENDELA, a, b, y, a > date(y, 1, 1) or b < date(y, 12, 31)))
    return hasil


# ---------------------------------------------------------------------------
# Tampilan periode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TampilanPeriode:
    periode: Periode
    ekuitas: tuple["BarisEkuitas", ...]  # baris titik awal + hari simulasi dalam periode
    tertutup: tuple[ex.Posisi, ...]  # tertutup dalam tampilan periode ini (urut tanggal entry)
    terbuka: tuple[mt.TransaksiTerbuka, ...]  # TERBUKA pada tanggal penilaian periode
    dilewati: tuple["SinyalDilewati", ...]  # tanggal sinyal dalam periode

    @property
    def tanggal_awal(self) -> date | None:
        return self.ekuitas[0].tanggal if self.ekuitas else None

    @property
    def ada_titik_awal(self) -> bool:
        return bool(self.ekuitas)


@dataclass(frozen=True)
class PembagianPeriode:
    in_sample: TampilanPeriode
    holdout: TampilanPeriode
    penuh: TampilanPeriode
    jendela: tuple[TampilanPeriode, ...]
    akhir_insample: date
    akhir_data: date

    def semua(self) -> list[TampilanPeriode]:
        """Urutan tampilan: in-sample, holdout, penuh, lalu jendela per tahun."""
        return [self.in_sample, self.holdout, self.penuh, *self.jendela]


def _titik_awal(baris: Sequence["BarisEkuitas"], mulai: date) -> int | None:
    """Indeks baris terakhir bertanggal < mulai (close hari bursa terakhir sebelum periode)."""
    idx = None
    for i, b in enumerate(baris):
        if b.tanggal < mulai:
            idx = i
        else:
            break
    return idx


def _irisan(baris: Sequence["BarisEkuitas"], mulai: date, akhir: date) -> tuple["BarisEkuitas", ...]:
    i = _titik_awal(baris, mulai)
    if i is None:
        return ()
    return (baris[i], *(b for b in baris[i + 1 :] if b.tanggal <= akhir))


def _nilai_pada(pos: ex.Posisi, d: date, akhir_data: date) -> "object":
    catatan = pos.meta.get("nilai_pada", {})
    if d in catatan:
        return catatan[d]
    if d == akhir_data and pos.keluar is None:
        return pos.nilai()
    raise ValueError(
        f"{pos.kode} (entry {pos.tanggal_entry}): nilai posisi pada {d} tidak dicatat mesin; panggil "
        "mesin.simulasikan(..., tanggal_penilaian=jendela.tanggal_penilaian(kal, cfg))"
    )


def _tampilan(
    periode: Periode,
    hasil: "HasilSimulasi",
    *,
    batas_tutup: date,
    akhir_data: date,
    tanda_terbuka: tuple[str, ...] = (),
) -> TampilanPeriode:
    """Transaksi dengan tanggal entry dalam periode. Tertutup bila exit ≤ `batas_tutup`; selain itu TERBUKA,
    dinilai pada `batas_tutup` (close valid terakhir ≤ hari itu)."""
    tertutup, terbuka = [], []
    for p in hasil.transaksi:
        if not periode.mulai <= p.tanggal_entry <= periode.akhir:
            continue
        if p.keluar is not None and p.keluar.tanggal <= batas_tutup:
            tertutup.append(p)
        else:
            nilai = _nilai_pada(p, batas_tutup, akhir_data)
            # Tanda yang didapat sesudah batas_tutup (mis. ANOMALI di holdout) tidak ikut statistik periode ini.
            dasar = None if batas_tutup == akhir_data else p.meta.get("tanda_pada", {}).get(batas_tutup)
            terbuka.append(mt.transaksi_terbuka(p, batas_tutup, nilai, tanda_terbuka, dasar))
    dilewati = tuple(s for s in hasil.dilewati if periode.mulai <= s.tanggal <= periode.akhir)
    akhir_ekuitas = min(periode.akhir, batas_tutup)
    return TampilanPeriode(
        periode, _irisan(hasil.ekuitas, periode.mulai, akhir_ekuitas), tuple(tertutup), tuple(terbuka), dilewati
    )


def bagi_periode(hasil: "HasilSimulasi", cfg: "Config") -> PembagianPeriode:
    """Bagi hasil simulasi satu run × mode ke in-sample, holdout, penuh, dan jendela (§16.4)."""
    if not hasil.ekuitas:
        raise ValueError("hasil simulasi tanpa baris ekuitas")
    mulai, holdout_mulai = cfg.backtest.mulai, cfg.backtest.holdout_mulai
    baris = hasil.ekuitas
    akhir_data = baris[-1].tanggal
    i_is = _titik_awal(baris, holdout_mulai)
    akhir_is = baris[i_is].tanggal if i_is is not None else holdout_mulai - timedelta(days=1)
    ada_holdout = akhir_data >= holdout_mulai
    tanda_is = (ex.MELEWATI_HOLDOUT,) if ada_holdout else ()

    p_is = Periode(IN_SAMPLE, mulai, akhir_is)
    p_ho = Periode(HOLDOUT, holdout_mulai, max(akhir_data, holdout_mulai))
    p_pn = Periode(PENUH, mulai, akhir_data)
    in_sample = _tampilan(p_is, hasil, batas_tutup=akhir_is, akhir_data=akhir_data, tanda_terbuka=tanda_is)
    holdout = _tampilan(p_ho, hasil, batas_tutup=akhir_data, akhir_data=akhir_data)
    penuh = _tampilan(p_pn, hasil, batas_tutup=akhir_data, akhir_data=akhir_data)
    jendela = tuple(
        _tampilan(pj, hasil, batas_tutup=akhir_is, akhir_data=akhir_data, tanda_terbuka=tanda_is)
        for pj in daftar_jendela(mulai, holdout_mulai)
        if pj.mulai <= akhir_is
    )
    return PembagianPeriode(in_sample, holdout, penuh, jendela, akhir_is, akhir_data)


def tandai_melewati_holdout(hasil: "HasilSimulasi", cfg: "Config") -> list[ex.Posisi]:
    """Tambahkan tanda `MELEWATI_HOLDOUT` (mengubah `pos.tanda`) pada transaksi yang masuk di in-sample dan
    masih terbuka pada akhir in-sample, bila data mencapai holdout. Mengembalikan posisi yang ditandai."""
    pembagian = bagi_periode(hasil, cfg)
    if pembagian.akhir_data < cfg.backtest.holdout_mulai:
        return []
    hasil_tanda = []
    for t in pembagian.in_sample.terbuka:
        t.posisi.tanda.add(ex.MELEWATI_HOLDOUT)
        hasil_tanda.append(t.posisi)
    return hasil_tanda


# ---------------------------------------------------------------------------
# Metrik per periode
# ---------------------------------------------------------------------------


def memenuhi_syarat_jendela(m: mt.MetrikTransaksi, cfg: "Config") -> bool:
    """Jendela dihitung untuk G5 bila transaksi tertutupnya ≥ `gerbang.min_transaksi_per_jendela` (§16.4)."""
    return m.n_tertutup >= int(cfg.gerbang.min_transaksi_per_jendela)


@dataclass(frozen=True)
class MetrikPeriode:
    periode: Periode
    ekuitas: mt.MetrikEkuitas | None  # None bila periode tanpa titik awal
    transaksi: mt.MetrikTransaksi
    ihsg: mt.MetrikPembanding | None
    dilewati: dict[str, int]
    memenuhi_syarat_g5: bool  # hanya bermakna untuk jendela


def metrik_periode(
    tp: TampilanPeriode,
    kal: "Kalender",
    cfg: "Config",
    *,
    close_ihsg: dict[date, object] | None = None,
    hari_per_tahun: float | None = None,
) -> MetrikPeriode:
    ek = mt.metrik_ekuitas(tp.ekuitas, hari_per_tahun=hari_per_tahun) if tp.ekuitas else None
    tr = mt.metrik_transaksi(tp.tertutup, kal, terbuka=tp.terbuka)
    ih = None
    if close_ihsg is not None and tp.ekuitas:
        ih = mt.metrik_ihsg(tp.ekuitas, close_ihsg, ek.cagr if ek else None)
    syarat = tp.periode.jenis == JENDELA and memenuhi_syarat_jendela(tr, cfg)
    return MetrikPeriode(tp.periode, ek, tr, ih, mt.hitung_dilewati(tp.dilewati), syarat)


def metrik_semua_periode(
    pembagian: PembagianPeriode,
    kal: "Kalender",
    cfg: "Config",
    *,
    close_ihsg: dict[date, object] | None = None,
) -> list[MetrikPeriode]:
    """Metrik setiap periode (urutan `PembagianPeriode.semua`). Sharpe semua periode memakai
    `hari_bursa_per_tahun` periode backtest penuh (§16.5), dengan tahun penuh menurut `kal`."""
    pn = pembagian.penuh.ekuitas
    hpt = mt.hari_bursa_per_tahun(pn[0].tanggal, [b.tanggal for b in pn[1:]], kal=kal) if pn else None
    return [metrik_periode(tp, kal, cfg, close_ihsg=close_ihsg, hari_per_tahun=hpt) for tp in pembagian.semua()]
