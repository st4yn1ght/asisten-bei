"""Kebijakan coba ulang (SPEC §8.1), pemilihan sumber per simbol (§8.2), dan pemuatan pasar dari cache.

- `KebijakanAmbil`: satu objek per run. Jeda antar-permintaan, coba lagi `JARINGAN` (2 s, 4 s, 8 s),
  `BATAS_PERMINTAAN` (jeda global lalu satu kali coba lagi), dan penghentian setelah
  `maks_simbol_kena_batas_berturut` simbol berturut-turut kena batas. Semua jeda lewat `waktu.tidur()`.
- `pilih_seri`: seri satu simbol dari cache provider utama, dengan cadangan (biasanya `csv_manual`)
  sebagai sambungan atau pengganti penuh menurut tabel §8.2. `csv_manual` tidak di-cache: berkasnya
  dibaca ulang setiap kali, dan bar yang belum final (§8.6) dibuang.
- `muat_pasar`: `DataPasar` untuk sekumpulan simbol as-of `akhir`, dengan kalender gabungan dari bar
  mentah breadth (§8.7).

Tidak ada bar yang dibuat atau diisi (P1); seri cadangan hanya menambah bar yang benar-benar ada di
berkasnya, setelah tumpang tindihnya cocok.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from .. import waktu
from ..inputs import KODE_IHSG
from ..kalender import Kalender, bangun_kalender, tanggal_final_terakhir
from .cache import CacheProvider, IsiCache
from .csv_provider import CsvManualProvider
from .kualitas import validasi_struktur
from .provider import KOLOM_BAR, NAMA_INDEX, HasilAmbil, JenisErrorAmbil, PriceProvider, StatusAmbil, gagal, ke_ts
from .split import normalisasi_split, split_dari_kolom
from .tampilan import DataPasar, DataSaham, bangun_data_saham

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config, KonteksApp
    from ..inputs import Masukan
    from .cache import HasilPembaruan

log = logging.getLogger("asisten_bei.data.sumber")

PROVIDER_LOKAL = frozenset({"csv_manual"})  # dibaca langsung dari berkas, tidak di-cache (§8.2)
JEDA_JARINGAN_DETIK: tuple[float, ...] = (2.0, 4.0, 8.0)  # §8.1
PESAN_DIBLOKIR = "diblokir sementara oleh Yahoo"
ALASAN_GAGAL_AMBIL = "GAGAL_AMBIL"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def buat_provider(nama: str, ctx: "KonteksApp", pabrik_ticker: Callable | None = None) -> PriceProvider:
    """Provider bernama `nama` (`data.provider_urutan`). `pabrik_ticker` hanya untuk yfinance (uji: mock)."""
    if nama == "yfinance":
        from .yfinance_provider import YFinanceProvider

        return YFinanceProvider(ctx.cfg, pabrik_ticker=pabrik_ticker)
    if nama == "csv_manual":
        return CsvManualProvider(ctx.cfg, ctx.path(ctx.cfg.data.folder_manual))
    raise ValueError(f"provider tidak dikenal: {nama}")


def cache_provider(ctx: "KonteksApp", nama: str) -> CacheProvider:
    return CacheProvider(ctx.path(ctx.cfg.data.folder_cache) / nama)


# ---------------------------------------------------------------------------
# Kebijakan coba ulang (§8.1)
# ---------------------------------------------------------------------------


@dataclass
class KebijakanAmbil:
    """Status satu run terhadap satu provider jaringan. Pakai satu objek untuk seluruh simbol run itu."""

    cfg: "Config"
    tidur: Callable[[float], None] = waktu.tidur
    berturut_kena_batas: int = 0
    diblokir: bool = False
    jumlah_permintaan: int = 0
    kena_batas: list[str] = field(default_factory=list)  # kode yang kena batas (setelah coba lagi)

    def _minta(self, provider: PriceProvider, kode: str, mulai: date, akhir: date) -> HasilAmbil:
        if self.jumlah_permintaan > 0:
            self.tidur(float(self.cfg.data.jeda_antar_permintaan_detik))
        self.jumlah_permintaan += 1
        return provider.ambil_harian(kode, mulai, akhir)

    def ambil(self, provider: PriceProvider, kode: str, mulai: date, akhir: date) -> HasilAmbil:
        if self.diblokir:
            return gagal(
                kode, provider.nama, "BATAS_PERMINTAAN",
                f"{kode}: tidak diminta — {PESAN_DIBLOKIR} (setelah "
                f"{self.cfg.data.maks_simbol_kena_batas_berturut} simbol berturut-turut kena batas)",
                getattr(provider, "versi_provider", ""),
            )
        hasil = self._minta(provider, kode, mulai, akhir)
        for jeda in JEDA_JARINGAN_DETIK:
            if hasil.jenis_error != "JARINGAN":
                break
            self.tidur(jeda)
            hasil = provider.ambil_harian(kode, mulai, akhir)
        if hasil.jenis_error == "BATAS_PERMINTAAN":
            self.tidur(float(self.cfg.data.jeda_batas_permintaan_detik))
            hasil = provider.ambil_harian(kode, mulai, akhir)
        if hasil.jenis_error == "BATAS_PERMINTAAN":
            self.berturut_kena_batas += 1
            self.kena_batas.append(kode)
            if self.berturut_kena_batas >= int(self.cfg.data.maks_simbol_kena_batas_berturut):
                self.diblokir = True
                log.warning("%s: unduhan dihentikan untuk sisa run (%s)", provider.nama, PESAN_DIBLOKIR)
        else:
            self.berturut_kena_batas = 0
        return hasil


# ---------------------------------------------------------------------------
# Pemilihan sumber per simbol (§8.2)
# ---------------------------------------------------------------------------


@dataclass
class SeriTerpilih:
    """Seri satu simbol hasil §8.2. `bar`: kolom open..volume + `sumber` (per bar); None bila tidak ada."""

    kode: str
    adalah_indeks: bool
    status: StatusAmbil
    sumber: str  # sumber seri (nama provider; sambungan: "<utama>+<cadangan>"); "" bila tidak ada data
    bar: pd.DataFrame | None
    split: pd.Series
    jenis_error: JenisErrorAmbil | None = None
    pesan: str = ""
    csv_pengganti_penuh: bool = False
    catatan: list[str] = field(default_factory=list)

    @property
    def ada_data(self) -> bool:
        return self.bar is not None and len(self.bar) > 0


@dataclass
class _Kandidat:
    nama: str
    bar: pd.DataFrame | None  # open..volume + sumber, sudah lolos struktur; None bila tidak ada/gagal
    split: pd.Series
    pesan: str = ""  # alasan tidak ada / gagal
    jenis_error: JenisErrorAmbil | None = None


def _status(bar: pd.DataFrame | None, t: date) -> StatusAmbil:
    if bar is None or len(bar) == 0:
        return "GAGAL"
    return "OK" if ke_ts(t) in bar.index else "SEBAGIAN"


def _batas_final(cfg: "Config", sekarang: datetime) -> pd.Timestamp:
    """Bar bertanggal ≥ nilai ini belum final (sama dengan `cache.perbarui_cache` langkah 6)."""
    return ke_ts(tanggal_final_terakhir(cfg, sekarang)) + pd.Timedelta(days=1)


def _dari_cache(isi: IsiCache, nama: str, adalah_indeks: bool, info: "HasilPembaruan | None") -> _Kandidat:
    jenis = info.jenis_error if info is not None and info.status == "GAGAL" else None
    pesan_ambil = info.pesan if info is not None and info.status == "GAGAL" else ""
    if isi.rusak or isi.bar is None or len(isi.bar) == 0:
        alasan = isi.pesan if isi.rusak else "tidak ada data di cache"
        pesan = "; ".join(x for x in (pesan_ambil, f"cache {nama}: {alasan}") if x)
        return _Kandidat(nama, None, normalisasi_split(None), pesan, jenis)
    st = validasi_struktur(isi.bar, adalah_indeks=adalah_indeks)
    if not st.ok:
        return _Kandidat(nama, None, normalisasi_split(None), f"cache {nama}: struktur tidak valid: {st.pesan}", "FORMAT")
    bar = st.bar.loc[:, [*KOLOM_BAR, "sumber"]] if "sumber" in st.bar.columns else st.bar.loc[:, list(KOLOM_BAR)]
    if "sumber" not in bar.columns:
        bar = bar.assign(sumber=nama)
    return _Kandidat(nama, bar, split_dari_kolom(isi.bar), pesan_ambil, jenis)


def _dari_csv(ctx: "KonteksApp", kode: str, adalah_indeks: bool, sekarang: datetime) -> _Kandidat:
    cfg = ctx.cfg
    prov = CsvManualProvider(cfg, ctx.path(cfg.data.folder_manual))
    if not prov.tersedia(kode):
        return _Kandidat(prov.nama, None, normalisasi_split(None), f"{prov.path_berkas(kode).name} tidak ada")
    hasil = prov.ambil_harian(kode, cfg.data.mulai_unduh, waktu.ke_wib(sekarang).date())
    if hasil.status == "GAGAL":
        return _Kandidat(prov.nama, None, normalisasi_split(None), hasil.pesan, hasil.jenis_error)
    st = validasi_struktur(hasil.bar, adalah_indeks=adalah_indeks)
    if not st.ok:
        return _Kandidat(prov.nama, None, normalisasi_split(None), f"{prov.nama}: {st.pesan}", "FORMAT")
    bar = st.bar.loc[:, list(KOLOM_BAR)]
    batas = _batas_final(cfg, sekarang)
    bar = bar[bar.index < batas].assign(sumber=prov.nama)
    split = normalisasi_split(hasil.split)
    split = split[split.index < batas]
    if len(bar) == 0:
        return _Kandidat(prov.nama, None, normalisasi_split(None), f"{prov.nama}: tidak ada bar final")
    return _Kandidat(prov.nama, bar, split)


def _kandidat(
    ctx: "KonteksApp", nama: str, kode: str, adalah_indeks: bool, sekarang: datetime,
    info: "Mapping[str, HasilPembaruan] | None",
) -> _Kandidat:
    if nama in PROVIDER_LOKAL:
        return _dari_csv(ctx, kode, adalah_indeks, sekarang)
    return _dari_cache(cache_provider(ctx, nama).baca(kode), nama, adalah_indeks, (info or {}).get(nama))


def _cocok(a: np.ndarray, b: np.ndarray, toleransi_pct: float) -> np.ndarray:
    """|b − a| × 100 ≤ tol × |a| per elemen (tanpa pembagian, sama dengan cache `_melebihi`)."""
    with np.errstate(invalid="ignore"):
        return np.abs(b - a) * 100.0 <= toleransi_pct * np.abs(a)


def _coba_sambungan(utama: _Kandidat, cad: _Kandidat, cfg: "Config") -> tuple[pd.DataFrame, pd.Series] | str:
    """Seri utama + bar cadangan sesudah bar terakhir utama (§6.9 cara 2), atau teks alasan ditolak."""
    akhir = utama.bar.index[-1]
    baru = cad.bar[cad.bar.index > akhir]
    if len(baru) == 0:
        return f"{cad.nama} tidak punya bar sesudah {akhir.date().isoformat()}"
    bersama = utama.bar.index.intersection(cad.bar.index[cad.bar.index <= akhir])
    n_min = int(cfg.data.min_bar_tumpang_tindih_sambungan)
    if len(bersama) < n_min:
        return f"tumpang tindih {cad.nama} {len(bersama)} bar < minimal {n_min}"
    tol = float(cfg.data.toleransi_revisi_pct)
    for k in ("open", "high", "low", "close"):
        a = utama.bar.loc[bersama, k].to_numpy(dtype="float64")
        b = cad.bar.loc[bersama, k].to_numpy(dtype="float64")
        beda = ~_cocok(a, b, tol)
        if beda.any():
            d = bersama[int(np.flatnonzero(beda)[0])].date().isoformat()
            return f"tumpang tindih {cad.nama} tidak cocok pada {d} ({k} {a[beda][0]:g} vs {b[beda][0]:g}, toleransi {tol:g}%)"
    gabungan = pd.concat([utama.bar, baru])
    split = pd.concat([utama.split, cad.split[cad.split.index > akhir]])
    return gabungan, normalisasi_split(split)


def pilih_seri(
    ctx: "KonteksApp",
    kode: str,
    *,
    t: date,
    sekarang: datetime | None = None,
    info: "Mapping[str, HasilPembaruan] | None" = None,
) -> SeriTerpilih:
    """Seri terpilih untuk `kode` menurut tabel §8.2, dinilai terhadap `t`. Hanya membaca (cache dan
    berkas manual); `info` = hasil pembaruan cache run ini per provider (untuk pesan error asli)."""
    cfg = ctx.cfg
    sekarang = waktu.sekarang() if sekarang is None else sekarang
    adalah_indeks = kode == KODE_IHSG
    urutan = list(cfg.data.provider_urutan)
    utama = _kandidat(ctx, urutan[0], kode, adalah_indeks, sekarang, info)
    st_utama = _status(utama.bar, t)
    catatan: list[str] = []

    def hasil(status, sumber, bar, split, *, pengganti=False, jenis=None, pesan="") -> SeriTerpilih:
        return SeriTerpilih(
            kode, adalah_indeks, status, sumber, bar, split, jenis, pesan, pengganti, catatan,
        )

    if st_utama == "OK":
        return hasil("OK", utama.nama, utama.bar, utama.split, jenis=utama.jenis_error, pesan=utama.pesan)

    for nama in urutan[1:]:
        cad = _kandidat(ctx, nama, kode, adalah_indeks, sekarang, info)
        if cad.bar is None:
            catatan.append(f"cadangan {nama}: {cad.pesan}")
            continue
        if st_utama == "SEBAGIAN":
            sambung = _coba_sambungan(utama, cad, cfg)
            if not isinstance(sambung, str):
                bar, split = sambung
                catatan.append(
                    f"sambungan {nama}: {int((bar.index > utama.bar.index[-1]).sum())} bar sesudah "
                    f"{utama.bar.index[-1].date().isoformat()}"
                )
                return hasil(_status(bar, t), f"{utama.nama}+{nama}", bar, split,
                             jenis=utama.jenis_error, pesan=utama.pesan)
            catatan.append(f"sambungan {nama} ditolak: {sambung}")
        if _status(cad.bar, t) == "OK":
            catatan.append(f"pengganti penuh {nama}")
            return hasil("OK", nama, cad.bar, cad.split, pengganti=(nama in PROVIDER_LOKAL),
                         jenis=utama.jenis_error, pesan=utama.pesan)
        catatan.append(f"cadangan {nama} tanpa bar {t.isoformat()}: tidak dipakai sebagai pengganti penuh")

    if st_utama == "SEBAGIAN":
        return hasil("SEBAGIAN", utama.nama, utama.bar, utama.split, jenis=utama.jenis_error, pesan=utama.pesan)
    pesan = utama.pesan or "tidak ada data"
    return hasil("GAGAL", "", None, normalisasi_split(None), jenis=utama.jenis_error, pesan=pesan)


# ---------------------------------------------------------------------------
# Pasar dari cache
# ---------------------------------------------------------------------------


@dataclass
class PasarTermuat:
    pasar: DataPasar
    seri: dict[str, SeriTerpilih]  # termasuk IHSG dan saham breadth yang dimuat untuk kalender
    data: dict[str, DataSaham]  # kode yang diminta + IHSG
    kalender: Kalender


def _bar_kosong() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], name=NAMA_INDEX).as_unit("ns")
    df = pd.DataFrame({k: pd.Series(dtype="float64") for k in KOLOM_BAR}, index=idx)
    df["sumber"] = pd.Series(dtype=object)
    return df


def data_saham_dari_seri(
    s: SeriTerpilih, *, kalender: Kalender, cfg: "Config", anomali_ok: set[tuple[str, date]], akhir: date
) -> DataSaham:
    """`DataSaham` dari seri terpilih. Tanpa data → alasan seri `GAGAL_AMBIL` (§4.3)."""
    return bangun_data_saham(
        s.kode,
        s.bar if s.ada_data else _bar_kosong(),
        s.split,
        adalah_indeks=s.adalah_indeks,
        kalender=kalender,
        cfg=cfg,
        anomali_ok=anomali_ok,
        akhir=akhir,
        status_ambil=s.status,
        jenis_error=s.jenis_error,
        pesan=s.pesan,
        sumber=s.sumber,
        alasan_seri=None if s.ada_data else ALASAN_GAGAL_AMBIL,
        csv_pengganti_penuh=s.csv_pengganti_penuh,
    )


def muat_pasar(
    ctx: "KonteksApp",
    masukan: "Masukan",
    kode: Iterable[str],
    *,
    akhir: date,
    t: date | None = None,
    sekarang: datetime | None = None,
    info: "Mapping[str, Mapping[str, HasilPembaruan]] | None" = None,
) -> PasarTermuat:
    """`DataPasar` untuk `kode` (+ IHSG) as-of `akhir`; status seri dinilai terhadap `t` (default `akhir`).

    Kalender = holidays.csv + kalender turunan dari bar mentah seri breadth (§8.7); seri breadth dimuat
    walau tidak diminta. `info`: kode → (provider → hasil pembaruan run ini)."""
    cfg = ctx.cfg
    sekarang = waktu.sekarang() if sekarang is None else sekarang
    t = akhir if t is None else t
    info = info or {}
    diminta = sorted({k for k in kode if k != KODE_IHSG})
    perlu = sorted(set(diminta) | set(masukan.breadth))
    seri = {k: pilih_seri(ctx, k, t=t, sekarang=sekarang, info=info.get(k)) for k in perlu}
    seri[KODE_IHSG] = pilih_seri(ctx, KODE_IHSG, t=t, sekarang=sekarang, info=info.get(KODE_IHSG))
    breadth_mentah = {k: seri[k].bar for k in masukan.breadth if seri[k].ada_data}
    kal = bangun_kalender(masukan.libur, breadth_mentah, float(cfg.data.validasi.min_hari_kalender_historis_pct))
    ok = masukan.anomali_ok_set()
    saham = {
        k: data_saham_dari_seri(seri[k], kalender=kal, cfg=cfg, anomali_ok=ok, akhir=akhir) for k in diminta
    }
    ihsg = data_saham_dari_seri(seri[KODE_IHSG], kalender=kal, cfg=cfg, anomali_ok=ok, akhir=akhir)
    return PasarTermuat(DataPasar(saham, ihsg, kal, cfg), seri, {**saham, KODE_IHSG: ihsg}, kal)


def baris_kualitas(termuat: PasarTermuat, kode: Sequence[str], d: date) -> list:
    """Baris berkas kualitas (§8.5) untuk `kode` as-of `d`, dengan alasan dikeluarkan karena data."""
    from .kualitas import baris_kualitas_dari_tanda

    dv = termuat.pasar.tampilan(d)
    hasil = []
    for k in kode:
        s = termuat.seri[k]
        hasil.append(
            baris_kualitas_dari_tanda(
                k,
                sumber=s.sumber,
                status_ambil=s.status,
                jenis_error=s.jenis_error,
                pesan="; ".join(x for x in (s.pesan, *s.catatan) if x),
                tanda=termuat.data[k].tanda if s.ada_data else None,
                d=d,
                alasan=dv.alasan_data(k),
            )
        )
    return hasil
