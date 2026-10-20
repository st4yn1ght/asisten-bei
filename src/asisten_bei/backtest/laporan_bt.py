"""Laporan backtest (SPEC §16.1, §16.4–§16.10): semua berkas folder run, ringkasan md/html, ringkasan terminal.

Berkas di `output/backtest/<YYYYMMDD-HHMMSS>_<hash8>/` (§16.1):

- `transaksi_<run>_<mode>.csv`, `ekuitas_<run>_<mode>.csv`, `sinyal_dilewati_<run>_<mode>.csv`,
  `sinyal_r_<strategi>.csv` — kolom persis "Kolom berkas" §16.1, baris pertama disclaimer (`# ...`).
  Format mesin: titik desimal tanpa pemisah ribuan; Rupiah 2 desimal ROUND_HALF_UP; R 4 desimal; harga (dan
  lembar/skor) sampai 6 desimal tanpa nol di belakang; tanggal `YYYY-MM-DD`; kosong = tidak ada. Isinya hanya
  bergantung pada hasil simulasi, sehingga dua run yang sama byte-identik (§22.1).
- `config_snapshot.yaml` — diawali komentar disclaimer, lalu salinan apa adanya berkas config yang dipakai.
- `ringkasan.md`, `ringkasan.html` — diawali "BUKAN SARAN INVESTASI" dan diakhiri teks §18.4. Isi: banner
  survivorship, identitas run, status gerbang per syarat, tabel metrik per run × mode × periode berdampingan
  dengan IHSG beli-dan-tahan, tabel per tahun, grafik ekuitas vs IHSG dan drawdown (HTML: PNG base64 tersemat,
  CSS inline, tanpa JavaScript/URL eksternal; md: hanya catatan teks), diagnostik §16.7–§16.9, dan 15
  keterbatasan §16.10. Angka untuk dibaca pengguna memakai `teks` (gaya Indonesia).

`gerbang_run.json`, `gate_status.json`, dan `holdout_log.csv` ditulis lewat `gerbang` (dipanggil `jalankan`).
Modul ini tidak menghitung ulang simulasi; semua angka berasal dari `mesin`, `metrik`, `jendela`, `gerbang`.
"""

from __future__ import annotations

import base64
import html
import io
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from .. import DISCLAIMER, DISCLAIMER_SINGKAT, DISCLAIMER_YAML, teks
from .. import exit as ex
from ..kondisi_pasar import JANGAN_BELI, TAMPILAN_STATUS
from ..operasional import teks_csv, tulis_keluaran
from . import gerbang as gb
from . import jendela as jd
from . import metrik as mt
from . import mesin

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config, KonteksRisiko
    from ..kalender import Kalender

NAMA_RINGKASAN_MD = "ringkasan.md"
NAMA_RINGKASAN_HTML = "ringkasan.html"
NAMA_CONFIG_SNAPSHOT = "config_snapshot.yaml"

# Kolom berkas (§16.1 "Kolom berkas") — urutan mengikat.
KOLOM_TRANSAKSI: tuple[str, ...] = (
    "kode", "strategi", "sektor", "tanggal_sinyal", "tanggal_entry", "tanggal_exit", "alasan_exit",
    "tanda_transaksi", "lembar", "harga_beli", "harga_beli_nominal", "harga_exit", "harga_exit_nominal",
    "stop_awal", "stop_awal_nominal", "target", "target_nominal", "biaya_beli_idr", "fee_beli_idr",
    "fee_jual_idr", "slippage_idr", "hasil_jual_idr", "pnl_idr", "risiko_awal_idr", "r", "hari", "skor",
    "status_pasar_sinyal", "pembatas_lot",
)
KOLOM_EKUITAS: tuple[str, ...] = (
    "tanggal", "kas_idr", "nilai_posisi_idr", "ekuitas_idr", "n_posisi", "perkiraan", "status_pasar",
    "ihsg_close", "ekuitas_ihsg_idr",
)
KOLOM_SINYAL_DILEWATI: tuple[str, ...] = (
    "tanggal_sinyal", "kode", "strategi", "skor", "status_sinyal", "status_isi", "tanggal_isi", "alasan",
)
KOLOM_SINYAL_R: tuple[str, ...] = (
    "tanggal_sinyal", "kode", "strategi", "skor", "status_pasar", "status_isi", "tanggal_entry", "tanggal_exit",
    "alasan_exit", "r", "hari", "tanda_transaksi",
)

TEKS_SURVIVORSHIP = (
    "PERINGATAN SURVIVORSHIP BIAS: backtest memakai daftar universe saat ini untuk seluruh periode. "
    "Hasil cenderung terlalu optimis."
)
TEKS_BREADTH_BIAS = "breadth_universe selalu memakai daftar saat ini; ini juga sumber bias survivorship."
TEKS_ACAK = (
    "Pembanding acak tampak menguntungkan — periksa kemungkinan bug/look-ahead atau periode naik kuat"
)
TEKS_TERSELEKSI = "terseleksi dengan holdout — optimis"
TEKS_SUBSET_LEMAH = "keunggulan belum terbukti pada saham yang terjangkau modal Anda"

URUTAN_RUN: tuple[str, ...] = ("pb", "bo", "mr", "acak", "gabungan_aktif", "gabungan_lolos")
URUTAN_MODE: tuple[str, ...] = ("edge", "realistis")

# Warna grafik (palet kategorikal tervalidasi: slot 1 biru, slot 2 jingga; IHSG = tinta sekunder netral).
_WARNA_MODE = {"edge": "#2a78d6", "realistis": "#eb6834"}
_WARNA_IHSG = "#898781"
_PERMUKAAN = "#fcfcfb"
_TINTA = "#0b0b0b"
_TINTA_2 = "#52514e"
_GRID = "#e1e0d9"
_SUMBU = "#c3c2b7"


# ---------------------------------------------------------------------------
# Format mesin (CSV)
# ---------------------------------------------------------------------------


def _desimal(x: Any) -> Decimal | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return x if x.is_finite() else None
    if isinstance(x, bool):
        return Decimal(int(x))
    if isinstance(x, float):
        return Decimal(repr(x)) if math.isfinite(x) else None
    return Decimal(x)


def _kuantum(x: Any, desimal: int, buang_nol: bool = False) -> str:
    d = _desimal(x)
    if d is None:
        return ""
    q = d.quantize(Decimal(1).scaleb(-desimal), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # tanpa "-0.00"
    if buang_nol:
        n = q.normalize()
        return format(n, "f") if n != 0 else "0"
    return format(q, "f")


def csv_rupiah(x: Any) -> str:
    """Rupiah: 2 desimal ROUND_HALF_UP (`203304.5` → "203304.50"); None → ""."""
    return _kuantum(x, 2)


def csv_r(x: Any) -> str:
    """R: 4 desimal ROUND_HALF_UP."""
    return _kuantum(x, 4)


def csv_harga(x: Any) -> str:
    """Harga (juga lembar dan skor): sampai 6 desimal tanpa nol di belakang (`2314.000000` → "2314")."""
    return _kuantum(x, 6, buang_nol=True)


def csv_tanggal(d: date | None) -> str:
    return "" if d is None else d.isoformat()


def csv_tanda(tanda: Iterable[str]) -> str:
    """`tanda_transaksi` dipisah `;` dengan urutan enum §4.3."""
    ada = set(tanda)
    return ";".join(t for t in ex.URUTAN_TANDA if t in ada)


def _bingkai(baris: list[dict[str, str]], kolom: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(baris, columns=list(kolom), dtype=object)


# ---------------------------------------------------------------------------
# Model laporan
# ---------------------------------------------------------------------------


@dataclass
class RunMode:
    """Satu run × mode yang sudah disimulasikan dan dibagi ke periode (§16.3–§16.5)."""

    nama: str  # enum run_backtest
    mode: str  # enum mode
    strategi: tuple[str, ...]  # strategi yang sinyalnya ikut (PB/BO/MR/ACAK)
    hasil: mesin.HasilSimulasi
    pembagian: jd.PembagianPeriode
    metrik: list[jd.MetrikPeriode]

    @property
    def terseleksi_holdout(self) -> bool:
        return self.nama == "gabungan_lolos"

    def periode(self, kunci: str) -> jd.MetrikPeriode | None:
        return next((m for m in self.metrik if m.periode.kunci == kunci), None)

    @property
    def ada_holdout(self) -> bool:
        return self.pembagian.akhir_data >= self.pembagian.holdout.periode.mulai

    @property
    def berkas_akhiran(self) -> str:
        return f"{self.nama}_{self.mode}"


@dataclass(frozen=True)
class TahunBreadth:
    """Ringkasan breadth & K0 per tahun kalender (§10 baris terakhir)."""

    tahun: int
    hari: int
    hari_k0: int
    terhitung_rata: float
    terhitung_min: int
    terhitung_maks: int


def breadth_per_tahun(keputusan: Sequence[mesin.KeputusanHari]) -> list[TahunBreadth]:
    per: dict[int, list[mesin.KeputusanHari]] = {}
    for kh in keputusan:
        per.setdefault(kh.tanggal.year, []).append(kh)
    hasil = []
    for y in sorted(per):
        xs = per[y]
        n = [kh.kondisi.breadth.n_terhitung for kh in xs]
        hasil.append(TahunBreadth(
            y, len(xs), sum(1 for kh in xs if kh.kondisi.aturan == "K0"), sum(n) / len(n), min(n), max(n),
        ))
    return hasil


@dataclass
class LaporanBacktest:
    """Semua bahan laporan satu perintah backtest."""

    cfg: "Config"
    kalender: "Kalender"
    dibuat_pada: datetime
    versi_program: str
    t: date
    akhir: date
    tanggal_awal: date
    hash_aturan: str  # global
    hash_input: str
    hash8: str
    folder_run: str  # relatif terhadap --akar (posix)
    strategi_diminta: tuple[str, ...]  # PB/BO/MR/ACAK
    mode_diminta: tuple[str, ...]
    runs: list[RunMode]
    konteks: dict[str, "KonteksRisiko"]
    close_ihsg: dict[date, Decimal]  # close IHSG yang berlaku (bar valid terakhir ≤ d)
    close_ihsg_hari_itu: dict[date, Decimal]  # hanya hari dengan bar IHSG valid
    faktor: Callable[[str, date], Decimal]  # F(d) per kode (§8.4), untuk harga exit nominal TERBUKA
    teks_config: str
    nama_config: str
    gerbang: dict[str, gb.HasilGerbang] = field(default_factory=dict)
    entri_gate: dict[str, dict] = field(default_factory=dict)
    subset: dict[str, gb.SubsetTerjangkau] = field(default_factory=dict)
    jalur: dict[str, list[mesin.JalurSinyal]] = field(default_factory=dict)
    holdout_log: gb.RingkasanHoldoutLog | None = None
    gate_status_ditulis: bool = False
    riwayat_ada: bool = False
    riwayat_tanpa_data: tuple[str, ...] = ()
    breadth_tahunan: list[TahunBreadth] = field(default_factory=list)
    catatan: list[str] = field(default_factory=list)
    peringatan: list[str] = field(default_factory=list)
    path_kualitas: str = ""

    def run(self, nama: str, mode: str) -> RunMode | None:
        return next((r for r in self.runs if r.nama == nama and r.mode == mode), None)

    def nama_run(self) -> list[str]:
        ada = {r.nama for r in self.runs}
        return [n for n in URUTAN_RUN if n in ada]

    @property
    def akhir_insample(self) -> date:
        return self.kalender.hari_bursa_sebelum(self.cfg.backtest.holdout_mulai)


# ---------------------------------------------------------------------------
# Baris CSV
# ---------------------------------------------------------------------------


def baris_transaksi(rm: RunMode, lap: LaporanBacktest) -> pd.DataFrame:
    """`transaksi_<run>_<mode>.csv`: satu baris per transaksi (urut tanggal entry, seperti mesin). Posisi
    yang masih terbuka di akhir → `TERBUKA`: tanggal_exit = akhir data, harga exit = close valid terakhir,
    tanpa fee jual; harga exit nominal = × F(akhir data)."""
    cfg, kal = lap.cfg, lap.kalender
    akhir = rm.pembagian.akhir_data
    baris = []
    for pos in rm.hasil.transaksi:
        k = pos.keluar
        if k is None:
            k = ex.tutup_terbuka(pos, akhir, cfg, lap.faktor(pos.kode, akhir))
        tgl_exit = k.tanggal
        exit_nom = k.harga_nominal
        f = pos.faktor_entry
        baris.append({
            "kode": pos.kode,
            "strategi": pos.strategi,
            "sektor": pos.sektor,
            "tanggal_sinyal": csv_tanggal(pos.tanggal_sinyal),
            "tanggal_entry": csv_tanggal(pos.tanggal_entry),
            "tanggal_exit": csv_tanggal(tgl_exit),
            "alasan_exit": k.alasan,
            "tanda_transaksi": csv_tanda(pos.tanda),
            "lembar": csv_harga(pos.lembar / f),
            "harga_beli": csv_harga(pos.harga_beli),
            "harga_beli_nominal": csv_harga(pos.harga_beli * f),
            "harga_exit": csv_harga(k.harga),
            "harga_exit_nominal": csv_harga(exit_nom),
            "stop_awal": csv_harga(pos.stop_awal),
            "stop_awal_nominal": csv_harga(pos.stop_awal * f),
            "target": csv_harga(pos.target),
            "target_nominal": csv_harga(None if pos.target is None else pos.target * f),
            "biaya_beli_idr": csv_rupiah(pos.biaya_beli_idr),
            "fee_beli_idr": csv_rupiah(pos.fee_beli_idr),
            "fee_jual_idr": csv_rupiah(k.fee_jual_idr),
            "slippage_idr": csv_rupiah(pos.slippage_beli_idr + k.slippage_idr),
            "hasil_jual_idr": csv_rupiah(k.hasil_jual_idr),
            "pnl_idr": csv_rupiah(k.pnl_idr),
            "risiko_awal_idr": csv_rupiah(pos.risiko_awal_idr),
            "r": csv_r(k.r),
            "hari": str(kal.nomor_hari(pos.tanggal_entry, tgl_exit)),
            "skor": csv_harga(pos.meta.get("skor")),
            "status_pasar_sinyal": str(pos.meta.get("status_pasar") or ""),
            "pembatas_lot": str(pos.meta.get("pembatas") or ""),
        })
    return _bingkai(baris, KOLOM_TRANSAKSI)


def baris_ekuitas(rm: RunMode, lap: LaporanBacktest) -> pd.DataFrame:
    """`ekuitas_<run>_<mode>.csv`: baris pertama = titik awal (§16.4). `ihsg_close` hanya pada hari dengan bar
    IHSG valid; `ekuitas_ihsg_idr` = modal × close berlaku ÷ close titik awal (tanpa fee, §16.5)."""
    rows = rm.hasil.ekuitas
    tanggal = [b.tanggal for b in rows]
    ihsg = mt.ekuitas_ihsg(tanggal, lap.close_ihsg, rows[0].ekuitas) if rows else []
    baris = []
    for b, e_ihsg in zip(rows, ihsg):
        baris.append({
            "tanggal": csv_tanggal(b.tanggal),
            "kas_idr": csv_rupiah(b.kas),
            "nilai_posisi_idr": csv_rupiah(b.nilai_posisi),
            "ekuitas_idr": csv_rupiah(b.ekuitas),
            "n_posisi": str(int(b.n_posisi)),
            "perkiraan": "1" if b.perkiraan else "0",
            "status_pasar": b.status_pasar or "",
            "ihsg_close": csv_harga(lap.close_ihsg_hari_itu.get(b.tanggal)),
            "ekuitas_ihsg_idr": csv_rupiah(e_ihsg),
        })
    return _bingkai(baris, KOLOM_EKUITAS)


def baris_dilewati(rm: RunMode) -> pd.DataFrame:
    """`sinyal_dilewati_<run>_<mode>.csv`: sinyal yang tidak menjadi transaksi (urutan mesin)."""
    baris = [
        {
            "tanggal_sinyal": csv_tanggal(s.tanggal),
            "kode": s.kode,
            "strategi": s.strategi,
            "skor": csv_harga(s.skor),
            "status_sinyal": s.status_sinyal,
            "status_isi": s.status_isi or "",
            "tanggal_isi": csv_tanggal(s.tanggal_isi),
            "alasan": " ".join(str(s.alasan or "").split()),
        }
        for s in rm.hasil.dilewati
    ]
    return _bingkai(baris, KOLOM_SINYAL_DILEWATI)


def baris_sinyal_r(jalur: Sequence[mesin.JalurSinyal]) -> pd.DataFrame:
    """`sinyal_r_<strategi>.csv`: jalur per sinyal §16.7 (1 unit independen)."""
    baris = [
        {
            "tanggal_sinyal": csv_tanggal(j.tanggal_sinyal),
            "kode": j.kode,
            "strategi": j.strategi,
            "skor": csv_harga(j.skor),
            "status_pasar": j.status_pasar or "",
            "status_isi": j.status_isi or "",
            "tanggal_entry": csv_tanggal(j.tanggal_entry),
            "tanggal_exit": csv_tanggal(j.tanggal_exit),
            "alasan_exit": j.alasan_exit or "",
            "r": csv_r(j.r),
            "hari": "" if j.hari is None else str(int(j.hari)),
            "tanda_transaksi": csv_tanda(j.tanda),
        }
        for j in jalur
    ]
    return _bingkai(baris, KOLOM_SINYAL_R)


def teks_config_snapshot(lap: LaporanBacktest) -> str:
    """`config_snapshot.yaml`: komentar disclaimer + identitas, lalu salinan apa adanya config yang dipakai."""
    isi = lap.teks_config.lstrip("﻿").replace("\r\n", "\n")
    if isi and not isi.endswith("\n"):
        isi += "\n"
    kepala = [
        DISCLAIMER_YAML,
        f"# Salinan config yang dipakai backtest ini: {lap.nama_config}",
        f"# Dibuat {lap.dibuat_pada.isoformat(timespec='seconds')} (WIB), asisten-bei {lap.versi_program}",
        f"# hash_aturan (global): {lap.hash_aturan}",
        f"# hash_input: {lap.hash_input}",
        f"# hash8: {lap.hash8}",
    ]
    return "\n".join(kepala) + "\n" + isi


# ---------------------------------------------------------------------------
# Statistik turunan untuk ringkasan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatSinyal:
    """Statistik R jalur per sinyal (§16.7), tanpa hari JANGAN BELI."""

    strategi: str
    n_sinyal: int  # selain hari JANGAN BELI
    n_jangan_beli: int  # tidak dihitung
    n_terisi: int
    n_tertutup: int
    n_terbuka: int
    n_batal: int
    n_menunggu: int
    pf_r: mt.NilaiPF
    ekspektansi_r: Decimal | None
    win_rate_pct: float | None
    rata_menang_r: Decimal | None
    rata_kalah_r: Decimal | None


def stat_sinyal(strategi: str, jalur: Sequence[mesin.JalurSinyal]) -> StatSinyal:
    dihitung = [j for j in jalur if j.status_pasar != JANGAN_BELI]
    terisi = [j for j in dihitung if j.status_isi == ex.TERISI]
    tertutup = [j for j in terisi if j.alasan_exit not in (None, ex.TERBUKA) and j.r is not None]
    r = [j.r for j in tertutup]
    menang = [x for x in r if x > 0]
    kalah = [x for x in r if x < 0]

    def rata(xs: list[Decimal]) -> Decimal | None:
        return sum(xs, Decimal(0)) / len(xs) if xs else None

    return StatSinyal(
        strategi=strategi,
        n_sinyal=len(dihitung),
        n_jangan_beli=len(jalur) - len(dihitung),
        n_terisi=len(terisi),
        n_tertutup=len(tertutup),
        n_terbuka=sum(1 for j in terisi if j.alasan_exit == ex.TERBUKA),
        n_batal=sum(1 for j in dihitung if j.status_isi and j.status_isi.startswith("BATAL_")),
        n_menunggu=sum(1 for j in dihitung if j.status_isi == mesin.MENUNGGU),
        pf_r=mt.profit_factor(r),
        ekspektansi_r=rata(r),
        win_rate_pct=len(menang) * 100 / len(r) if r else None,
        rata_menang_r=rata(menang),
        rata_kalah_r=rata(kalah),
    )


@dataclass(frozen=True)
class BarisTahun:
    """Kinerja per tahun kalender dari kurva ekuitas penuh (tidak memakai harga sesudah 31 Desember tahun
    itu). Titik awal = close hari bursa terakhir tahun sebelumnya."""

    tahun: int
    parsial: bool
    memuat_holdout: bool
    seri: mt.RingkasanSeri
    ihsg: mt.MetrikPembanding | None
    n_masuk: int  # transaksi dengan tanggal entry di tahun itu
    exposure_pct: float | None


def per_tahun(rm: RunMode, lap: LaporanBacktest) -> list[BarisTahun]:
    baris = rm.hasil.ekuitas
    if len(baris) < 2:
        return []
    holdout = lap.cfg.backtest.holdout_mulai
    mulai = lap.cfg.backtest.mulai
    akhir = baris[-1].tanggal
    hasil = []
    for y in sorted({b.tanggal.year for b in baris[1:]}):
        # Titik awal: close hari bursa terakhir tahun sebelumnya; tahun pertama: titik awal backtest.
        i0 = max((i for i, b in enumerate(baris) if b.tanggal < date(y, 1, 1)), default=0)
        rows = [baris[i0], *(b for b in baris[i0 + 1 :] if b.tanggal.year == y)]
        hari = rows[1:]
        akhir_tahun = lap.kalender.hari_bursa_sebelum(date(y + 1, 1, 1))
        awal_tahun = lap.kalender.hari_bursa_berikut(date(y - 1, 12, 31))
        parsial = (y == mulai.year and mulai > awal_tahun) or (y == akhir.year and akhir < akhir_tahun)
        hasil.append(BarisTahun(
            tahun=y,
            parsial=parsial,
            memuat_holdout=date(y, 12, 31) >= holdout,
            seri=mt.ringkas_seri([(b.tanggal, b.ekuitas) for b in rows]),
            ihsg=mt.metrik_ihsg(rows, lap.close_ihsg),
            n_masuk=sum(1 for p in rm.hasil.transaksi if p.tanggal_entry.year == y),
            exposure_pct=sum(1 for b in hari if b.n_posisi >= 1) * 100 / len(hari) if hari else None,
        ))
    return hasil


def pf_r_holdout_acak(lap: LaporanBacktest) -> mt.NilaiPF | None:
    rm = lap.run("acak", "edge")
    if rm is None or not rm.ada_holdout:
        return None
    m = rm.periode(jd.HOLDOUT)
    return None if m is None else m.transaksi.pf_r


def acak_mencurigakan(lap: LaporanBacktest) -> bool:
    """§16.9: PF_R holdout ACAK (mode edge) > `backtest.maks_pf_r_acak` (n/a dengan transaksi menang = tak hingga)."""
    pf = pf_r_holdout_acak(lap)
    return pf is not None and pf.lebih_dari(lap.cfg.backtest.maks_pf_r_acak)


def jumlah_data_berakhir(rm: RunMode) -> int:
    return sum(1 for p in rm.hasil.tertutup if p.keluar.alasan == ex.DATA_BERAKHIR)


# ---------------------------------------------------------------------------
# Format tampilan (gaya Indonesia)
# ---------------------------------------------------------------------------

_STRIP = teks.STRIP


def _pct(pecahan: float | Decimal | None, desimal: int = 2) -> str:
    """Pecahan (0,1234) → "12,34%"."""
    if pecahan is None:
        return _STRIP
    return teks.angka(float(pecahan) * 100, desimal) + "%"


def _pct_angka(x: float | Decimal | None, desimal: int = 1) -> str:
    """Angka persen (52,345) → "52,3%"."""
    return _STRIP if x is None else teks.angka(float(x), desimal) + "%"


def _rp(x: Decimal | None) -> str:
    return _STRIP if x is None else teks.rupiah(x)


def _r(x: Decimal | None, desimal: int = 2) -> str:
    return _STRIP if x is None else teks.angka(x, desimal) + "R"


def _a(x: Any, desimal: int = 2) -> str:
    return teks.angka(x, desimal)


def _tgl(d: date | None) -> str:
    return _STRIP if d is None else d.isoformat()


def _hitungan(d: dict[str, int]) -> str:
    isi = [f"{k} {v}" for k, v in d.items() if v]
    return ", ".join(isi) if isi else "tidak ada"


def _status_pasar(s: str | None) -> str:
    return TAMPILAN_STATUS.get(s or "", s or _STRIP)


def _nilai_syarat(s: gb.HasilSyarat) -> str:
    v = s.nilai
    if v is None:
        return _STRIP
    if isinstance(v, mt.NilaiPF):
        return v.teks()
    if isinstance(v, bool):
        return "ya" if v else "tidak"
    if isinstance(v, int):
        return str(v)
    if s.kode == "G3":
        return _r(v, 4)
    if s.kode == "G5":
        return _pct_angka(v, 2)
    if s.kode == "G6":
        return _r(v)
    return _a(v)


def _ambang_syarat(s: gb.HasilSyarat) -> str:
    a = s.ambang
    if s.kode == "G1":
        return f"≥ {int(a)}"
    if s.kode == "G2":
        return f"≥ {_a(a)}"
    if s.kode == "G3":
        return f"> {_r(Decimal(repr(float(a))), 4)}"
    if s.kode == "G4":
        return f"≥ {_a(a)}"
    if s.kode == "G5":
        return f"≥ {_pct_angka(a, 2)}"
    if s.kode == "G6":
        return f"≤ {_r(Decimal(repr(float(a))))}"
    return _a(a)


# ---------------------------------------------------------------------------
# Dokumen (satu model, dua keluaran: md dan html)
# ---------------------------------------------------------------------------


@dataclass
class _Judul:
    level: int
    teks: str


@dataclass
class _Paragraf:
    teks: str
    gaya: str = ""  # "" | "banner" | "peringatan" | "catatan"


@dataclass
class _Tabel:
    kolom: list[str]
    baris: list[list[str]]
    judul: str = ""


@dataclass
class _Daftar:
    butir: list[str]
    bernomor: bool = False


@dataclass
class _Gambar:
    alt: str
    png: bytes
    keterangan: str


def _label_periode(rm: RunMode, m: jd.MetrikPeriode) -> str:
    label = m.periode.label
    if m.periode.jenis == jd.HOLDOUT and rm.terseleksi_holdout:
        label += f" ({TEKS_TERSELEKSI})"
    return label


TEKS_HOLDOUT_BELUM = "belum tercapai oleh data"


def _holdout_belum(rm: RunMode, m: jd.MetrikPeriode) -> bool:
    """Periode holdout yang belum dicapai data: tidak ada angka untuk ditampilkan (§16.4)."""
    return m.periode.jenis == jd.HOLDOUT and not rm.ada_holdout


def _baris_holdout_belum(rm: RunMode, m: jd.MetrikPeriode, n_kolom: int) -> list[str]:
    return [_label_periode(rm, m), TEKS_HOLDOUT_BELUM, *([_STRIP] * (n_kolom - 2))]


def _tabel_ekuitas(rm: RunMode) -> _Tabel:
    kolom = ["Periode", "Rentang", "Titik awal", "E awal", "E akhir", "Return", "CAGR", "Max DD",
             "IHSG return", "IHSG CAGR", "IHSG Max DD", "Selisih CAGR"]
    baris = []
    for m in rm.metrik:
        ek, ih = m.ekuitas, m.ihsg
        rentang = f"{m.periode.mulai.isoformat()} … {m.periode.akhir.isoformat()}"
        if _holdout_belum(rm, m):
            baris.append(_baris_holdout_belum(rm, m, len(kolom)))
            continue
        if ek is None:
            baris.append([_label_periode(rm, m), rentang, _STRIP, *([_STRIP] * 9)])
            continue
        baris.append([
            _label_periode(rm, m), rentang, ek.seri.tanggal_awal.isoformat(), _rp(ek.e_awal), _rp(ek.e_akhir),
            _pct(ek.seri.imbal_hasil), _pct(ek.cagr), _pct(ek.drawdown.maks),
            _pct(ih.imbal_hasil) if ih else _STRIP, _pct(ih.cagr) if ih else _STRIP,
            _pct(ih.drawdown.maks) if ih else _STRIP,
            (teks.angka(ih.selisih_cagr * 100, 2) + " poin %") if ih and ih.selisih_cagr is not None else _STRIP,
        ])
    return _Tabel(kolom, baris, "Kinerja ekuitas vs IHSG beli-dan-tahan (modal sama, tanpa fee)")


def _tabel_transaksi(rm: RunMode) -> _Tabel:
    kolom = ["Periode", "Tertutup", "Terbuka", "Win rate", "PF (Rp)", "PF_R", "Ekspektansi", "Ekspektansi R",
             "Rata menang / kalah", "Rata menang / kalah R", "Max DD R", "Kalah beruntun", "Lama menahan (hari)"]
    baris = []
    for m in rm.metrik:
        t = m.transaksi
        if _holdout_belum(rm, m):
            baris.append(_baris_holdout_belum(rm, m, len(kolom)))
            continue
        baris.append([
            _label_periode(rm, m) + (" ✓G5" if m.memenuhi_syarat_g5 else ""), str(t.n_tertutup), str(t.n_terbuka),
            _pct_angka(t.win_rate_pct), t.pf_idr.teks(), t.pf_r.teks(), _rp(t.ekspektansi_idr),
            _r(t.ekspektansi_r, 4), f"{_rp(t.rata_menang_idr)} / {_rp(t.rata_kalah_idr)}",
            f"{_r(t.rata_menang_r)} / {_r(t.rata_kalah_r)}", _r(t.max_drawdown_r),
            str(t.kerugian_beruntun_terpanjang), _a(t.rata_lama_menahan_hari, 1),
        ])
    return _Tabel(kolom, baris, "Transaksi (masuk menurut tanggal entry; TERBUKA tidak masuk PF/win rate; "
                                "✓G5 = jendela memenuhi syarat G5)")


def _tabel_dilewati_tanda(rm: RunMode) -> _Tabel:
    """Per periode: sinyal dilewati per status, transaksi bertanda, dan metrik tanpa transaksi ANOMALI (§16.5)."""
    kolom = ["Periode", "Sinyal dilewati per status", "Transaksi bertanda (tertutup + terbuka)",
             "Tanpa transaksi bertanda ANOMALI"]
    baris = []
    for m in rm.metrik:
        t, ta = m.transaksi, m.transaksi.tanpa_anomali
        if _holdout_belum(rm, m):
            baris.append(_baris_holdout_belum(rm, m, len(kolom)))
            continue
        tanpa = (f"tertutup {ta.n_tertutup}, win rate {_pct_angka(ta.win_rate_pct)}, PF_R {ta.pf_r.teks()}, "
                 f"ekspektansi {_r(ta.ekspektansi_r, 4)}, max DD {_r(ta.max_drawdown_r)}") if ta is not None else _STRIP
        baris.append([_label_periode(rm, m), _hitungan(m.dilewati), _hitungan(t.per_tanda), tanpa])
    return _Tabel(kolom, baris, "Sinyal dilewati, transaksi bertanda, dan metrik tanpa ANOMALI")


def _tabel_biaya(rm: RunMode) -> _Tabel:
    kolom = ["Periode", "Fee + slippage", "% laba kotor", "Pembatas lot (alokasi/kas/likuid)", "Risiko terealisasi",
             "Exposure", "Modal terpakai", "Sharpe (info)", "Calmar (info)", "Puncak → lembah → pulih",
             "Durasi di bawah puncak (hari bursa)"]
    baris = []
    for m in rm.metrik:
        t, ek = m.transaksi, m.ekuitas
        if _holdout_belum(rm, m):
            baris.append(_baris_holdout_belum(rm, m, len(kolom)))
            continue
        dd = ek.drawdown if ek else None
        jejak = (f"{_tgl(dd.tanggal_puncak)} → {_tgl(dd.tanggal_lembah)} → "
                 f"{_tgl(dd.tanggal_pulih) if dd.tanggal_pulih else 'belum pulih'}") if dd and dd.tanggal_lembah else _STRIP
        baris.append([
            _label_periode(rm, m), _rp(t.biaya_total_idr),
            _pct_angka(t.biaya_pct_laba_kotor) if t.biaya_pct_laba_kotor is not None else mt.TEKS_NA,
            _pct_angka(t.pembatas_lot_pct), _pct_angka(t.risiko_terealisasi_pct, 2),
            _pct_angka(ek.exposure_pct) if ek else _STRIP, _pct_angka(ek.modal_terpakai_pct) if ek else _STRIP,
            _a(ek.sharpe) if ek else _STRIP, _a(ek.calmar) if ek else _STRIP, jejak,
            str(dd.durasi_terpanjang) if dd else _STRIP,
        ])
    return _Tabel(kolom, baris, "Biaya, sizing, dan informasi tambahan")


def _tabel_rincian(rm: RunMode) -> _Tabel:
    m = rm.periode(jd.PENUH)
    t = m.transaksi
    ek = m.ekuitas
    baris = [
        ["Alasan exit (tertutup)", _hitungan(t.per_alasan_exit)],
        ["Keluar DATA_BERAKHIR (potongan konservatif)", str(jumlah_data_berakhir(rm))],
        ["Transaksi TERBUKA di akhir data", f"{t.n_terbuka} (P&L belum terealisasi {_rp(t.pnl_terbuka_idr)})"],
        ["Hari ekuitas \"≈\" (posisi tanpa close valid)", str(ek.hari_perkiraan) if ek else _STRIP],
        ["Laba kotor (P&L bersih + fee + slippage)", _rp(t.laba_kotor_idr)],
        ["Fee / slippage", f"{_rp(t.fee_idr)} / {_rp(t.slippage_idr)}"],
        ["Pembatas lot (jumlah transaksi)", _hitungan(t.pembatas_lot)],
    ]
    return _Tabel(["Rincian periode penuh", "Nilai"], baris)


def _tabel_tahun(rm: RunMode, lap: LaporanBacktest) -> _Tabel | None:
    xs = per_tahun(rm, lap)
    if not xs:
        return None
    kolom = ["Tahun", "Return strategi", "Return IHSG", "Selisih", "Max DD strategi", "Max DD IHSG",
             "Transaksi masuk", "Exposure"]
    baris = []
    for b in xs:
        label = str(b.tahun) + (" (parsial)" if b.parsial else "") + (" (memuat holdout)" if b.memuat_holdout else "")
        r_s, r_i = b.seri.imbal_hasil, (b.ihsg.imbal_hasil if b.ihsg else None)
        selisih = (teks.angka((r_s - r_i) * 100, 2) + " poin %") if r_s is not None and r_i is not None else _STRIP
        baris.append([
            label, _pct(r_s), _pct(r_i), selisih, _pct(b.seri.drawdown.maks),
            _pct(b.ihsg.drawdown.maks) if b.ihsg else _STRIP, str(b.n_masuk), _pct_angka(b.exposure_pct),
        ])
    return _Tabel(kolom, baris, "Per tahun kalender (kurva ekuitas penuh; titik awal = close akhir tahun sebelumnya)")


def _blok_gerbang(lap: LaporanBacktest) -> list:
    hasil: list = [_Judul(2, "Status gerbang (mode edge, run strategi itu sendiri)")]
    if not lap.gerbang:
        alasan = ("mode edge tidak dijalankan" if "edge" not in lap.mode_diminta
                  else "tidak ada strategi PB/BO/MR yang dijalankan")
        hasil.append(_Paragraf(f"Gerbang tidak dinilai pada run ini ({alasan}); gate_status.json tidak diubah "
                               "untuk strategi mana pun.", "catatan"))
        return hasil
    hasil.append(_Paragraf(
        "gate_status.json digabung per strategi: strategi yang tidak dijalankan mempertahankan entri lamanya."
        if lap.gate_status_ditulis else "gate_status.json tidak ditulis pada run ini.", "catatan"))
    for kode, g in lap.gerbang.items():
        status = "LOLOS" if g.lolos else "TIDAK LOLOS"
        hasil.append(_Judul(3, f"{kode} — {status}"))
        baris = [
            [s.kode, _nilai_syarat(s), _ambang_syarat(s), "✓ lolos" if s.lolos else "✗ gagal", s.uraian]
            for s in g.syarat
        ]
        hasil.append(_Tabel(["Syarat", "Nilai", "Ambang", "Hasil", "Uraian"], baris))
        sub = lap.subset.get(kode)
        if sub is not None:
            teks_sub = (f"Subset terjangkau modal realistis (§16.7): {sub.n} dari {sub.n_holdout} transaksi holdout, "
                        f"PF_R {sub.pf_r.teks()}, ekspektansi {_r(sub.ekspektansi_r, 4)}.")
            hasil.append(_Paragraf(teks_sub, ""))
            if sub.lemah:
                hasil.append(_Paragraf(f"PERINGATAN {kode}: {TEKS_SUBSET_LEMAH} (subset_terjangkau_lemah: true).",
                                       "peringatan"))
        entri = lap.entri_gate.get(kode)
        if entri:
            hasil.append(_Paragraf(f"hash_aturan[{kode}] {entri['hash_aturan']}; tanggal_data_terakhir "
                                   f"{entri['tanggal_data_terakhir']}; holdout_mulai {entri['holdout_mulai']}.",
                                   "catatan"))
    return hasil


def _blok_run(lap: LaporanBacktest, nama: str, grafik: dict[str, list[tuple[str, bytes, str]]]) -> list:
    runs = [r for r in lap.runs if r.nama == nama]
    if not runs:
        return []
    judul = f"Run {nama}" + (f" — {TEKS_TERSELEKSI}" if nama == "gabungan_lolos" else "")
    hasil: list = [_Judul(2, judul)]
    hasil.append(_Paragraf(f"Strategi: {', '.join(runs[0].strategi)}. "
                           + ("Berbagi slot dengan deduplikasi §13.2." if nama.startswith("gabungan")
                              else "Tanpa deduplikasi dan tanpa gerbang.")))
    if nama == "gabungan_lolos":
        hasil.append(_Paragraf(
            "Strategi run ini dipilih berdasarkan hasil holdout, sehingga metrik holdout-nya BUKAN out-of-sample "
            f"({TEKS_TERSELEKSI}). Ekspektasi live yang jujur hanya diperoleh dari jurnal kertas setelah tanggal gerbang.",
            "peringatan"))
    for g in grafik.get(nama, []):
        hasil.append(_Gambar(*g))
    for rm in sorted(runs, key=lambda r: URUTAN_MODE.index(r.mode)):
        k = lap.konteks.get(rm.mode)
        hasil.append(_Judul(3, f"{nama} — mode {rm.mode}"))
        if k is not None:
            hasil.append(_Paragraf(f"Konteks {k.uraian()}.", "catatan"))
        hasil += [_tabel_ekuitas(rm), _tabel_transaksi(rm), _tabel_biaya(rm), _tabel_dilewati_tanda(rm),
                  _tabel_rincian(rm)]
        tahun = _tabel_tahun(rm, lap)
        if tahun is not None:
            hasil.append(tahun)
    return hasil


def _blok_diagnostik(lap: LaporanBacktest) -> list:
    cfg = lap.cfg
    hasil: list = [_Judul(2, "Diagnostik (informasi, tidak mengubah gerbang)")]

    hasil.append(_Judul(3, "Jalur per sinyal (§16.7), tanpa hari JANGAN BELI"))
    if lap.jalur:
        baris = []
        for kode, jalur in lap.jalur.items():
            s = stat_sinyal(kode, jalur)
            baris.append([
                kode, str(s.n_sinyal), str(s.n_terisi), str(s.n_tertutup), _pct_angka(s.win_rate_pct), s.pf_r.teks(),
                _r(s.ekspektansi_r, 4), _r(s.rata_menang_r), _r(s.rata_kalah_r), str(s.n_terbuka), str(s.n_batal),
                str(s.n_menunggu), str(s.n_jangan_beli),
            ])
        hasil.append(_Tabel(
            ["Strategi", "Sinyal", "Terisi", "Tertutup", "Win rate", "PF_R", "Ekspektansi R", "Rata menang R",
             "Rata kalah R", "Terbuka", "Batal (data/gap/ARA)", "Menunggu", "Sinyal hari JANGAN BELI (tidak dihitung)"],
            baris))
        hasil.append(_Paragraf("Satu unit = 1 lembar dengan fee persentase saja, tanpa batas slot atau modal; "
                               "sinyal STOP_TERLALU_JAUH tidak disimulasikan. Rincian: sinyal_r_<strategi>.csv.",
                               "catatan"))
    else:
        hasil.append(_Paragraf("Tidak ada strategi per run yang dijalankan.", "catatan"))

    hasil.append(_Judul(3, "Subset terjangkau modal realistis (§16.7)"))
    if lap.subset:
        baris = [[k, f"{s.n} / {s.n_holdout}", s.pf_r.teks(), _r(s.ekspektansi_r, 4),
                  f"ya — {TEKS_SUBSET_LEMAH}" if s.lemah else "tidak"] for k, s in lap.subset.items()]
        hasil.append(_Tabel(["Strategi", "n terjangkau / n holdout edge", "PF_R", "Ekspektansi R", "Lemah"], baris))
    else:
        hasil.append(_Paragraf("Tidak dihitung (memerlukan run PB/BO/MR mode edge).", "catatan"))

    hasil.append(_Judul(3, "Disiplin holdout (§16.4)"))
    log = lap.holdout_log
    if log is not None and log.n_baris:
        hasil.append(_Paragraf(log.teks(), "" if log.murni else "peringatan"))
        if log.holdout_mulai_pertama and log.holdout_mulai_pertama != cfg.backtest.holdout_mulai.isoformat():
            hasil.append(_Paragraf(
                f"PERINGATAN: holdout_mulai {cfg.backtest.holdout_mulai.isoformat()} berbeda dari nilai pertama di "
                f"holdout_log.csv ({log.holdout_mulai_pertama}).", "peringatan"))
    else:
        hasil.append(_Paragraf("Holdout belum dihitung (akhir data sebelum holdout_mulai).", "catatan"))
    if "gabungan_lolos" in lap.nama_run():
        hasil.append(_Paragraf(f"Run gabungan_lolos: {TEKS_TERSELEKSI}.", "peringatan"))

    hasil.append(_Judul(3, "Uji kewarasan ACAK (§16.9)"))
    pf = pf_r_holdout_acak(lap)
    if pf is None:
        hasil.append(_Paragraf("Tidak dinilai: memerlukan run acak mode edge yang mencapai holdout.", "catatan"))
    elif pf.n_menang == 0 and pf.n_kalah == 0:
        hasil.append(_Paragraf("Tidak dinilai: ACAK tanpa transaksi tertutup di holdout.", "catatan"))
    elif acak_mencurigakan(lap):
        hasil.append(_Paragraf(f"{TEKS_ACAK} (PF_R holdout ACAK {pf.teks()} > {_a(cfg.backtest.maks_pf_r_acak)}).",
                               "banner"))
    else:
        hasil.append(_Paragraf(f"PF_R holdout ACAK {pf.teks()} ≤ {_a(cfg.backtest.maks_pf_r_acak)}: wajar.", ""))
    hasil.append(_Paragraf("Identitas akuntansi §16.9 lolos untuk setiap run × mode.", "catatan"))

    hasil.append(_Judul(3, "DATA_BERAKHIR"))
    baris = [[rm.nama, rm.mode, str(jumlah_data_berakhir(rm))] for rm in urut_runs(lap.runs)]
    hasil.append(_Tabel(["Run", "Mode", "Transaksi DATA_BERAKHIR"], baris))

    hasil.append(_Judul(3, "Survivorship (§16.8)"))
    if lap.riwayat_ada:
        tanpa = lap.riwayat_tanpa_data
        hasil.append(_Paragraf(
            f"universe_history.csv dipakai. Konstituen historis tanpa data yang dapat dipakai: {len(tanpa)}"
            + (f" ({', '.join(tanpa)}; alasannya di berkas kualitas)" if tanpa else "")
            + ". Ini sisa bias yang tidak bisa dihilangkan dengan yfinance.",
            "peringatan" if tanpa else ""))
    else:
        hasil.append(_Paragraf(TEKS_SURVIVORSHIP, "banner"))
    hasil.append(_Paragraf(TEKS_BREADTH_BIAS, "catatan"))

    hasil.append(_Judul(3, "Breadth dan K0 per tahun (§10)"))
    if lap.breadth_tahunan:
        baris = [[str(b.tahun), str(b.hari), str(b.hari_k0), _a(b.terhitung_rata, 1), str(b.terhitung_min),
                  str(b.terhitung_maks)] for b in lap.breadth_tahunan]
        hasil.append(_Tabel(["Tahun", "Hari bursa", "Hari K0 (data tidak lengkap)", "Saham breadth terhitung (rata-rata)",
                             "Minimum", "Maksimum"], baris))
    return hasil


def _keterbatasan(lap: LaporanBacktest) -> list[str]:
    """15 keterbatasan §16.10 (isi sama; angka dari config)."""
    cfg = lap.cfg
    surv = (f"universe_history.csv dipakai, tetapi {len(lap.riwayat_tanpa_data)} konstituen historis tidak punya data"
            if lap.riwayat_ada else TEKS_SURVIVORSHIP.rstrip("."))
    ar = cfg.pasar.auto_reject
    fr = cfg.pasar.fraksi_harga
    status_bei = (
        f"Status saat ini: fraksi {'terverifikasi' if fr.terverifikasi else 'belum terverifikasi (rupiah bulat)'}, "
        f"ARA/ARB {'terverifikasi' if ar.terverifikasi else 'belum terverifikasi (tidak dimodelkan)'}."
    )
    return [
        f"Survivorship bias: {surv}. {TEKS_BREADTH_BIAS} (§16.8)",
        "Kualitas data yfinance: bisa bolong, salah, atau direvisi. Rights issue dan sebagian aksi korporasi tidak "
        "disesuaikan.",
        "Dividen diabaikan, baik di strategi maupun di IHSG.",
        "Urutan intraday tidak diketahui: jika stop dan target tersentuh di hari yang sama, stop diasumsikan lebih dulu.",
        f"Order otomatis diasumsikan terisi di stop dikurangi slippage tetap ({_a(cfg.biaya.slippage_pct)}%). "
        "Dalam pasar cepat, kenyataannya bisa lebih buruk.",
        "Gap turun bisa membuat kerugian melebihi anggaran risiko; backtest menghitungnya, rencana harian tidak bisa "
        "mencegahnya.",
        "Order beli diasumsikan terisi penuh di harga pembukaan dan dibatalkan jika tidak terisi saat pembukaan. "
        "Pengisian sebagian dan pengisian belakangan di sesi berjalan tidak dimodelkan.",
        "Fraksi dan ARA/ARB hanya dimodelkan untuk tanggal ≥ berlaku_mulai setelah Anda memverifikasinya. Aturan masa "
        f"lalu yang berbeda tidak dimodelkan. {status_bei}",
        f"Likuiditas di pembukaan diasumsikan cukup, dengan batas {_a(cfg.likuiditas.maks_pct_order_dari_nilai_harian)}% "
        "rata-rata nilai harian.",
        "Settlement T+2 diabaikan.",
        "Pemilihan parameter oleh manusia yang mungkin sudah pernah melihat grafik periode yang sama; lihat juga log "
        "holdout.",
        "Sampel kecil, terutama mode realistis.",
        f"Asumsi konservatif DATA_BERAKHIR (potongan {_a(cfg.backtest.potongan_data_berakhir_pct, 0)}%) untuk saham "
        "yang datanya berhenti.",
        "Struktur pasar berubah: aturan perdagangan, jam sesi, dan fee berubah dari waktu ke waktu.",
        "Hasil masa lalu tidak menjamin hasil masa depan.",
    ]


def urut_runs(runs: Iterable[RunMode]) -> list[RunMode]:
    return sorted(runs, key=lambda r: (URUTAN_RUN.index(r.nama), URUTAN_MODE.index(r.mode)))


def dokumen(lap: LaporanBacktest, grafik: dict[str, list[tuple[str, bytes, str]]] | None = None) -> list:
    """Model dokumen ringkasan (blok berurutan), dirender ke md dan html."""
    grafik = grafik or {}
    cfg = lap.cfg
    d: list = [_Judul(1, "Ringkasan backtest asisten-bei")]
    if not lap.riwayat_ada:
        d.append(_Paragraf(TEKS_SURVIVORSHIP, "banner"))
    if acak_mencurigakan(lap):
        d.append(_Paragraf(TEKS_ACAK, "banner"))
    if lap.holdout_log is not None and lap.holdout_log.n_baris and not lap.holdout_log.murni:
        d.append(_Paragraf(lap.holdout_log.teks(), "banner"))
    for kode, s in lap.subset.items():
        if s.lemah:
            d.append(_Paragraf(f"{kode}: {TEKS_SUBSET_LEMAH}.", "peringatan"))

    d.append(_Judul(2, "Identitas run"))
    run_teks = ", ".join(f"{n} ({'/'.join(r.mode for r in urut_runs(lap.runs) if r.nama == n)})"
                         for n in lap.nama_run())
    d.append(_Tabel(["Item", "Nilai"], [
        ["Dibuat pada", f"{lap.dibuat_pada.isoformat(timespec='seconds')} WIB"],
        ["Versi program", lap.versi_program],
        ["hash8", lap.hash8],
        ["hash_aturan (global)", lap.hash_aturan],
        ["hash_input", lap.hash_input],
        ["Folder run", lap.folder_run],
        ["Tanggal data t", lap.t.isoformat()],
        ["Akhir data backtest", lap.akhir.isoformat()],
        ["Titik awal (close sebelum backtest.mulai)", lap.tanggal_awal.isoformat()],
        ["In-sample", f"{cfg.backtest.mulai.isoformat()} … {min(lap.akhir_insample, lap.akhir).isoformat()}"],
        ["Holdout", f"{cfg.backtest.holdout_mulai.isoformat()} … {lap.akhir.isoformat()}"
         if lap.akhir >= cfg.backtest.holdout_mulai else "belum tercapai oleh data"],
        ["Strategi diminta", ", ".join(s.lower() for s in lap.strategi_diminta)],
        ["Mode diminta", ", ".join(lap.mode_diminta)],
        ["Run dijalankan", run_teks or _STRIP],
        ["Universe backtest", "universe_history.csv" if lap.riwayat_ada else "universe.csv saat ini (seluruh periode)"],
        ["Berkas kualitas data", lap.path_kualitas or _STRIP],
    ]))
    if lap.catatan or lap.peringatan:
        d.append(_Judul(2, "Catatan dan peringatan"))
        d += [_Paragraf(c, "catatan") for c in lap.catatan]
        d += [_Paragraf(p, "peringatan") for p in lap.peringatan]

    d += _blok_gerbang(lap)
    for nama in lap.nama_run():
        d += _blok_run(lap, nama, grafik)
    d += _blok_diagnostik(lap)
    d.append(_Judul(2, "Keterbatasan (wajib dibaca)"))
    d.append(_Daftar(_keterbatasan(lap), bernomor=True))
    return d


# ---------------------------------------------------------------------------
# Render Markdown
# ---------------------------------------------------------------------------


def _sel_md(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def render_md(blok: Sequence) -> str:
    """Markdown: baris pertama "BUKAN SARAN INVESTASI.", baris terakhir teks §18.4. Tanpa rujukan eksternal."""
    keluaran = [DISCLAIMER_SINGKAT, ""]
    for b in blok:
        if isinstance(b, _Judul):
            keluaran += ["#" * b.level + " " + b.teks, ""]
        elif isinstance(b, _Paragraf):
            if b.gaya == "banner":
                keluaran.append(f"> **{b.teks}**")
            elif b.gaya == "peringatan":
                keluaran.append(f"> {b.teks}")
            elif b.gaya == "catatan":
                keluaran.append(f"_{b.teks}_")
            else:
                keluaran.append(b.teks)
            keluaran.append("")
        elif isinstance(b, _Tabel):
            if b.judul:
                keluaran += [f"**{b.judul}**", ""]
            keluaran.append("| " + " | ".join(_sel_md(k) for k in b.kolom) + " |")
            keluaran.append("|" + "|".join("---" for _ in b.kolom) + "|")
            for r in b.baris:
                keluaran.append("| " + " | ".join(_sel_md(x) for x in r) + " |")
            keluaran.append("")
        elif isinstance(b, _Daftar):
            for i, x in enumerate(b.butir, 1):
                keluaran.append(f"{i}. {x}" if b.bernomor else f"- {x}")
            keluaran.append("")
        elif isinstance(b, _Gambar):
            keluaran += [f"_Grafik: {b.alt}. {b.keterangan} Gambar tersemat di {NAMA_RINGKASAN_HTML}._", ""]
    keluaran.append(DISCLAIMER)
    return "\n".join(keluaran) + "\n"


# ---------------------------------------------------------------------------
# Render HTML (mandiri: CSS inline, PNG base64, tanpa JavaScript/URL eksternal)
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light; --latar: #f9f9f7; --permukaan: #fcfcfb; --tinta: #0b0b0b; --tinta-2: #52514e;
  --garis: #e1e0d9; --banner: #fdecea; --banner-tinta: #8a1c1c; --peringatan: #fff4dc; --peringatan-tinta: #5c4200; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { color-scheme: dark; --latar: #0d0d0d; --permukaan: #1a1a19; --tinta: #f0efec;
    --tinta-2: #c3c2b7; --garis: #2c2c2a; --banner: #3a1414; --banner-tinta: #ffd2d2; --peringatan: #33290f;
    --peringatan-tinta: #ffe7a8; }
}
:root[data-theme="dark"] { color-scheme: dark; --latar: #0d0d0d; --permukaan: #1a1a19; --tinta: #f0efec;
  --tinta-2: #c3c2b7; --garis: #2c2c2a; --banner: #3a1414; --banner-tinta: #ffd2d2; --peringatan: #33290f;
  --peringatan-tinta: #ffe7a8; }
body { background: var(--latar); color: var(--tinta); font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; line-height: 1.45; }
main { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.25rem; margin-top: 2rem; border-bottom: 1px solid var(--garis); }
h3 { font-size: 1.05rem; margin-top: 1.4rem; }
.disclaimer { font-weight: 600; }
.banner { background: var(--banner); color: var(--banner-tinta); padding: 10px 14px; border-radius: 6px; font-weight: 700; }
.peringatan { background: var(--peringatan); color: var(--peringatan-tinta); padding: 8px 12px; border-radius: 6px; }
.catatan { color: var(--tinta-2); font-size: 0.92rem; }
.gulir { overflow-x: auto; margin: 0.4rem 0 1rem; }
table { border-collapse: collapse; background: var(--permukaan); font-size: 0.85rem; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--garis); padding: 4px 8px; text-align: left; vertical-align: top; }
th { color: var(--tinta-2); font-weight: 600; }
caption { text-align: left; font-weight: 600; padding: 4px 0; }
figure { margin: 0.6rem 0 1.2rem; background: #fcfcfb; padding: 8px; border-radius: 6px; max-width: 100%; }
figure img { max-width: 100%; height: auto; display: block; }
figcaption { color: #52514e; font-size: 0.85rem; }
""".strip()


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def render_html(blok: Sequence) -> str:
    """HTML mandiri. Teks yang tampil diawali "BUKAN SARAN INVESTASI" dan diakhiri teks §18.4."""
    isi: list[str] = [f'<p class="disclaimer">{_e(DISCLAIMER_SINGKAT)}</p>']
    for b in blok:
        if isinstance(b, _Judul):
            lv = min(max(b.level, 1), 4)
            isi.append(f"<h{lv}>{_e(b.teks)}</h{lv}>")
        elif isinstance(b, _Paragraf):
            kelas = f' class="{b.gaya}"' if b.gaya else ""
            isi.append(f"<p{kelas}>{_e(b.teks)}</p>")
        elif isinstance(b, _Tabel):
            bagian = ['<div class="gulir"><table>']
            if b.judul:
                bagian.append(f"<caption>{_e(b.judul)}</caption>")
            bagian.append("<thead><tr>" + "".join(f"<th>{_e(k)}</th>" for k in b.kolom) + "</tr></thead><tbody>")
            for r in b.baris:
                bagian.append("<tr>" + "".join(f"<td>{_e(x)}</td>" for x in r) + "</tr>")
            bagian.append("</tbody></table></div>")
            isi.append("".join(bagian))
        elif isinstance(b, _Daftar):
            tag = "ol" if b.bernomor else "ul"
            isi.append(f"<{tag}>" + "".join(f"<li>{_e(x)}</li>" for x in b.butir) + f"</{tag}>")
        elif isinstance(b, _Gambar):
            data = base64.b64encode(b.png).decode("ascii")
            isi.append(f'<figure><img alt="{_e(b.alt)}" src="data:image/png;base64,{data}">'
                       f"<figcaption>{_e(b.keterangan)}</figcaption></figure>")
    isi.append(f'<p class="disclaimer">{_e(DISCLAIMER)}</p>')
    return (
        "<!DOCTYPE html>\n<html lang=\"id\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_e(DISCLAIMER_SINGKAT)} Ringkasan backtest asisten-bei</title>\n"
        f"<style>\n{_CSS}\n</style>\n</head>\n<body>\n<main>\n" + "\n".join(isi) + "\n</main>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# Grafik (matplotlib, backend Agg tanpa pyplot)
# ---------------------------------------------------------------------------


def _gambar_png(fig) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=_PERMUKAAN, metadata={"Software": None})
    return buf.getvalue()


def _sumbu(ax, judul: str) -> None:
    from matplotlib.ticker import FuncFormatter

    ax.set_facecolor(_PERMUKAAN)
    for sisi in ("top", "right"):
        ax.spines[sisi].set_visible(False)
    for sisi in ("left", "bottom"):
        ax.spines[sisi].set_color(_SUMBU)
        ax.spines[sisi].set_linewidth(0.8)
    ax.grid(True, color=_GRID, linewidth=0.6, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(colors=_TINTA_2, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: teks.angka(v, 0)))
    ax.set_title(judul, loc="left", fontsize=10, color=_TINTA)


def _legenda(ax) -> None:
    """Legenda satu baris di bawah area plot (tidak menutupi data atau judul)."""
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.1), ncol=3, fontsize=8, frameon=False, labelcolor=_TINTA_2,
              borderaxespad=0.0)


def _garis_holdout(ax, holdout: date, awal: date, akhir: date) -> None:
    if awal < holdout <= akhir:
        ax.axvline(pd.Timestamp(holdout), color=_TINTA_2, linewidth=0.8)
        ax.annotate("holdout", xy=(pd.Timestamp(holdout), 1), xycoords=("data", "axes fraction"),
                    xytext=(3, -10), textcoords="offset points", fontsize=8, color=_TINTA_2)


def _seri_dd(nilai: Sequence[float]) -> list[float]:
    puncak = -math.inf
    hasil = []
    for v in nilai:
        puncak = max(puncak, v)
        hasil.append((v / puncak - 1) * 100 if puncak > 0 else 0.0)
    return hasil


def grafik_run(lap: LaporanBacktest, nama: str) -> list[tuple[str, bytes, str]]:
    """Dua PNG per run: ekuitas vs IHSG (indeks = 100 pada titik awal, satu sumbu) dan drawdown (%)."""
    from matplotlib.figure import Figure

    runs = sorted((r for r in lap.runs if r.nama == nama), key=lambda r: URUTAN_MODE.index(r.mode))
    if not runs:
        return []
    holdout = lap.cfg.backtest.holdout_mulai
    rows0 = runs[0].hasil.ekuitas
    tanggal = [b.tanggal for b in rows0]
    x = [pd.Timestamp(d) for d in tanggal]
    ihsg = mt.ekuitas_ihsg(tanggal, lap.close_ihsg, Decimal(100))
    titik_ihsg = [(xi, float(v)) for xi, v in zip(x, ihsg) if v is not None]

    fig = Figure(figsize=(9, 3.6))
    ax = fig.add_subplot(1, 1, 1)
    _sumbu(ax, f"{nama}: ekuitas vs IHSG beli-dan-tahan (indeks 100 = titik awal {tanggal[0].isoformat()})")
    for rm in runs:
        rows = rm.hasil.ekuitas
        e0 = float(rows[0].ekuitas)
        ys = [float(b.ekuitas) / e0 * 100 for b in rows]
        ax.plot([pd.Timestamp(b.tanggal) for b in rows], ys, color=_WARNA_MODE[rm.mode], linewidth=1.5,
                solid_joinstyle="round", solid_capstyle="round", label=f"{rm.mode} (akhir {teks.angka(ys[-1], 1)})")
    if titik_ihsg:
        ax.plot([p[0] for p in titik_ihsg], [p[1] for p in titik_ihsg], color=_WARNA_IHSG, linewidth=1.0,
                label=f"IHSG (akhir {teks.angka(titik_ihsg[-1][1], 1)})")
    _garis_holdout(ax, holdout, tanggal[0], tanggal[-1])
    _legenda(ax)
    fig.tight_layout()
    png_ekuitas = _gambar_png(fig)

    fig = Figure(figsize=(9, 3.0))
    ax = fig.add_subplot(1, 1, 1)
    _sumbu(ax, f"{nama}: drawdown dari puncak (%)")
    for rm in runs:
        rows = rm.hasil.ekuitas
        ax.plot([pd.Timestamp(b.tanggal) for b in rows], _seri_dd([float(b.ekuitas) for b in rows]),
                color=_WARNA_MODE[rm.mode], linewidth=1.5, label=rm.mode)
    if titik_ihsg:
        ax.plot([p[0] for p in titik_ihsg], _seri_dd([p[1] for p in titik_ihsg]), color=_WARNA_IHSG, linewidth=1.0,
                label="IHSG")
    _garis_holdout(ax, holdout, tanggal[0], tanggal[-1])
    _legenda(ax)
    fig.tight_layout()
    png_dd = _gambar_png(fig)
    mode_teks = " dan ".join(r.mode for r in runs)
    return [
        (f"Ekuitas {nama} vs IHSG", png_ekuitas,
         f"Ekuitas mode {mode_teks} dan IHSG beli-dan-tahan, diindeks 100 pada titik awal; garis tegak = awal holdout."),
        (f"Drawdown {nama}", png_dd, f"Penurunan dari puncak ekuitas (%), mode {mode_teks} dan IHSG."),
    ]


# ---------------------------------------------------------------------------
# Penulisan berkas
# ---------------------------------------------------------------------------


def nama_berkas_run(jenis: str, rm: RunMode) -> str:
    return f"{jenis}_{rm.berkas_akhiran}.csv"


def tulis_semua(folder: Path, lap: LaporanBacktest) -> dict[str, Path]:
    """Tulis semua berkas laporan ke `folder` (kecuali berkas gerbang). Peringatan berkas terkunci masuk
    `lap.peringatan`. Mengembalikan nama → path yang benar-benar ditulis."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    ditulis: dict[str, Path] = {}

    def tulis(nama: str, isi: str) -> None:
        path, pesan = tulis_keluaran(folder / nama, isi)
        ditulis[nama] = path
        if pesan:
            lap.peringatan.append(pesan)

    for rm in urut_runs(lap.runs):
        tulis(nama_berkas_run("transaksi", rm), teks_csv(baris_transaksi(rm, lap)))
        tulis(nama_berkas_run("ekuitas", rm), teks_csv(baris_ekuitas(rm, lap)))
        tulis(nama_berkas_run("sinyal_dilewati", rm), teks_csv(baris_dilewati(rm)))
    for kode, jalur in lap.jalur.items():
        tulis(f"sinyal_r_{kode.lower()}.csv", teks_csv(baris_sinyal_r(jalur)))
    tulis(NAMA_CONFIG_SNAPSHOT, teks_config_snapshot(lap))
    grafik = {nama: grafik_run(lap, nama) for nama in lap.nama_run()}
    blok = dokumen(lap, grafik)
    tulis(NAMA_RINGKASAN_MD, render_md(blok))
    tulis(NAMA_RINGKASAN_HTML, render_html(blok))
    return ditulis


# ---------------------------------------------------------------------------
# Ringkasan terminal
# ---------------------------------------------------------------------------


def ringkasan_terminal(lap: LaporanBacktest, folder: str | Path | None = None) -> list[str]:
    """Baris ringkasan terminal; baris terakhir = disclaimer §18.4."""
    cfg = lap.cfg
    hasil = ["", f"Ringkasan backtest (akhir data {lap.akhir.isoformat()}, holdout mulai "
                 f"{cfg.backtest.holdout_mulai.isoformat()}, titik awal {lap.tanggal_awal.isoformat()})"]
    if not lap.riwayat_ada:
        hasil.append(TEKS_SURVIVORSHIP)
    else:
        hasil.append(f"Konstituen historis tanpa data: {len(lap.riwayat_tanpa_data)}")
    hasil.append(f"{'Run':<18}{'Mode':<11}{'Holdout n':>10}{'PF_R':>22}{'Eksp. R':>11}{'CAGR penuh':>12}{'MaxDD':>9}")
    for rm in urut_runs(lap.runs):
        ho, pn = rm.periode(jd.HOLDOUT), rm.periode(jd.PENUH)
        if rm.ada_holdout and ho is not None:
            n, pf, eks = str(ho.transaksi.n_tertutup), ho.transaksi.pf_r.teks(), _r(ho.transaksi.ekspektansi_r)
        else:
            n, pf, eks = _STRIP, _STRIP, _STRIP
        cagr = _pct(pn.ekuitas.cagr) if pn and pn.ekuitas else _STRIP
        dd = _pct(pn.ekuitas.drawdown.maks) if pn and pn.ekuitas else _STRIP
        nama = rm.nama + (" *" if rm.terseleksi_holdout else "")
        hasil.append(f"{nama:<18}{rm.mode:<11}{n:>10}{pf:>22}{eks:>11}{cagr:>12}{dd:>9}")
    if any(r.terseleksi_holdout for r in lap.runs):
        hasil.append(f"* gabungan_lolos: {TEKS_TERSELEKSI}")
    if lap.gerbang:
        hasil.append("Gerbang (mode edge):")
        for kode, g in lap.gerbang.items():
            hasil.append(f"  {kode}: {'LOLOS' if g.lolos else 'TIDAK LOLOS — ' + g.alasan()}")
            sub = lap.subset.get(kode)
            if sub is not None and sub.lemah:
                hasil.append(f"    PERINGATAN: {TEKS_SUBSET_LEMAH} (subset terjangkau n {sub.n}, PF_R {sub.pf_r.teks()})")
    else:
        hasil.append("Gerbang tidak dinilai pada run ini (memerlukan mode edge dan strategi PB/BO/MR).")
    if lap.holdout_log is not None and lap.holdout_log.n_baris:
        hasil.append(lap.holdout_log.teks())
    if acak_mencurigakan(lap):
        hasil.append(f"PERINGATAN: {TEKS_ACAK}")
    for c in lap.catatan:
        hasil.append(f"Catatan: {c}")
    for p in lap.peringatan:
        hasil.append(f"PERINGATAN: {p}")
    if folder is not None:
        hasil.append(f"Folder hasil: {folder}")
    if lap.path_kualitas:
        hasil.append(f"Berkas kualitas: {lap.path_kualitas}")
    hasil += ["", DISCLAIMER]
    return hasil
