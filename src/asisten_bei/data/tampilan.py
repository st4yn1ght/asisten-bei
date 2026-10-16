"""`DataView` — penjaga look-ahead (SPEC §8.8, P3).

Strategi, skor, kondisi pasar, eligibilitas, dan sizing **hanya** menerima `DataView(as_of=d)`, yang
mengembalikan potongan data ≤ d. Indikator dan tanda kualitas data boleh dihitung tervektor atas
seluruh seri (lebih cepat), karena keduanya kausal (dibuktikan T-LA1/T-LA3); `DataView` lalu
memotongnya ≤ as_of dengan `searchsorted(side="right")` + `iloc` (cepat, tanpa salin).

Meminta tanggal > as_of (mis. `baris(kode, d)` dengan d > as_of, `sebelum(..., k < 0)`) memunculkan
`LookAheadError`. Satu-satunya data "sesudah as_of" yang sah dipakai adalah faktor split `F(d)`
(§8.4): ia hanya membatalkan penyesuaian split yang sudah tertanam di harga cache.

Konvensi `x_{t−k}` (§4.1): nilai pada bar valid ke-k sebelum t milik saham itu. Bar tanpa transaksi,
bar rusak, dan hari tanpa bar tidak dihitung.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from ..indikator import hhv_sejak as _hhv_sejak
from .provider import ke_ts
from .split import faktor_split as _faktor_split
from .split import harga_nominal as _harga_nominal

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..kalender import Kalender

KODE_IHSG = "IHSG"
ALASAN_DATA_BASI = "DATA_BASI"  # enum alasan_dikeluarkan §4.3: tanpa bar valid pada tanggal itu


class LookAheadError(Exception):
    """Akses ke data bertanggal sesudah `as_of` (pelanggaran P3)."""


@dataclass
class DataSaham:
    """Seluruh data satu simbol (saham atau IHSG) untuk satu run. Bingkai berindeks tanggal urut naik.

    - `mentah`: bar pada hari bursa (non-bursa sudah dibuang), termasuk bar rusak/tanpa transaksi
      (bertanda); `valid`: bar valid saja; `split`: seri split standar (§8.4).
    - `indikator`: `hitung_indikator(valid)` / `hitung_indikator_indeks(valid)`.
    - `tanda`: `kualitas.hitung_tanda_asof` (satu baris per hari bursa, kausal), dihitung sampai `akhir`
      run yang **sama untuk semua simbol** (t run harian / akhir backtest), bukan sampai bar terakhir
      simbol itu. Dengan begitu hari sesudah bar terakhir tetap punya baris (bar hilang / data basi)
      dan hasilnya identik dengan perhitungan ulang dari data ≤ t (T-LA1a). Ditegakkan `DataPasar`.
    - `alasan_seri`: alasan tingkat seri (mis. GAGAL_AMBIL, RIWAYAT_CSV_PENDEK) yang berlaku untuk
      semua tanggal dan mengalahkan alasan as-of.
    """

    kode: str
    adalah_indeks: bool
    status_ambil: str
    jenis_error: str | None
    pesan: str
    sumber: str
    mentah: pd.DataFrame
    valid: pd.DataFrame
    split: pd.Series
    indikator: pd.DataFrame
    tanda: pd.DataFrame
    alasan_seri: str | None = None
    csv_pengganti_penuh: bool = False


# ---------------------------------------------------------------------------
# Kunci tanggal (int64 ns) per bingkai, dihitung sekali per DataPasar
# ---------------------------------------------------------------------------


def _kunci_ns(index: pd.Index, nama: str, kode: str) -> np.ndarray:
    if len(index) == 0:
        return np.empty(0, dtype="int64")
    if not isinstance(index, pd.DatetimeIndex) or index.tz is not None:
        raise ValueError(f"{kode}: index {nama} harus DatetimeIndex tanggal tanpa zona waktu")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError(f"{kode}: index {nama} harus urut naik tanpa tanggal ganda")
    return index.as_unit("ns").asi8


def _ts_ns(d: date | pd.Timestamp | str) -> int:
    return int(ke_ts(d).as_unit("ns").value)


def _ke_date(d: date | pd.Timestamp | str) -> date:
    return ke_ts(d).date()


@dataclass(frozen=True)
class _Kunci:
    mentah: np.ndarray
    valid: np.ndarray
    indikator: np.ndarray
    tanda: np.ndarray
    split: np.ndarray

    @classmethod
    def dari(cls, ds: DataSaham) -> "_Kunci":
        return cls(
            mentah=_kunci_ns(ds.mentah.index, "mentah", ds.kode),
            valid=_kunci_ns(ds.valid.index, "valid", ds.kode),
            indikator=_kunci_ns(ds.indikator.index, "indikator", ds.kode),
            tanda=_kunci_ns(ds.tanda.index, "tanda", ds.kode),
            split=_kunci_ns(ds.split.index, "split", ds.kode),
        )


# ---------------------------------------------------------------------------
# DataPasar (seluruh data run) → DataView (potongan ≤ as_of)
# ---------------------------------------------------------------------------


class DataPasar:
    """Kumpulan `DataSaham` satu run. Satu-satunya jalan ke data adalah `tampilan(as_of)`."""

    def __init__(
        self,
        saham: dict[str, DataSaham],
        ihsg: DataSaham | None,
        kalender: "Kalender",
        cfg: "Config",
    ):
        self._saham = dict(saham)
        self._ihsg = ihsg
        self.kalender = kalender
        self.cfg = cfg
        self._kunci: dict[str, _Kunci] = {k: _Kunci.dari(ds) for k, ds in self._saham.items()}
        self._kunci_ihsg = _Kunci.dari(ihsg) if ihsg is not None else None
        self._cek_akhir_tanda_sama()

    def _cek_akhir_tanda_sama(self) -> None:
        """Semua bingkai `tanda` yang tidak kosong wajib berakhir pada hari bursa yang sama (akhir run).
        Akhir per simbol membuat `tanda()` bernilai None sesudah bar terakhir simbol itu, padahal
        perhitungan ulang dari data ≤ t punya baris pada t — pelanggaran kesetaraan T-LA1a."""
        akhir: dict[str, pd.Timestamp] = {}
        semua = list(self._saham.values()) + ([self._ihsg] if self._ihsg is not None else [])
        for ds in semua:
            if len(ds.tanda.index) > 0:
                akhir[ds.kode] = ds.tanda.index[-1]
        if len(set(akhir.values())) > 1:
            rincian = ", ".join(f"{k}={v.date().isoformat()}" for k, v in sorted(akhir.items()))
            raise ValueError(f"tanda harus dihitung sampai akhir run yang sama untuk semua simbol: {rincian}")

    def tampilan(self, as_of: date | pd.Timestamp | str) -> "DataView":
        return DataView(self, as_of)

    def _cari(self, kode: str) -> tuple[DataSaham, _Kunci] | None:
        ds = self._saham.get(kode)
        if ds is not None:
            return ds, self._kunci[kode]
        if kode == KODE_IHSG and self._ihsg is not None:
            return self._ihsg, self._kunci_ihsg
        return None


class TampilanSeri:
    """Satu simbol dilihat dari `as_of`: semua bingkai dipotong ≤ as_of."""

    __slots__ = ("_ds", "_kunci", "_as_of", "_as_of_ns", "_cfg", "_n")

    def __init__(self, ds: DataSaham, kunci: _Kunci, as_of: date, cfg: "Config"):
        self._ds = ds
        self._kunci = kunci
        self._as_of = as_of
        self._as_of_ns = _ts_ns(as_of)
        self._cfg = cfg
        self._n: dict[str, int] = {}

    # -- metadata seri (bukan data harga) ---------------------------------------

    @property
    def kode(self) -> str:
        return self._ds.kode

    @property
    def adalah_indeks(self) -> bool:
        return self._ds.adalah_indeks

    @property
    def as_of(self) -> date:
        return self._as_of

    @property
    def status_ambil(self) -> str:
        return self._ds.status_ambil

    @property
    def jenis_error(self) -> str | None:
        return self._ds.jenis_error

    @property
    def pesan(self) -> str:
        return self._ds.pesan

    @property
    def sumber(self) -> str:
        return self._ds.sumber

    @property
    def alasan_seri(self) -> str | None:
        return self._ds.alasan_seri

    @property
    def csv_pengganti_penuh(self) -> bool:
        return self._ds.csv_pengganti_penuh

    # -- pemotongan ---------------------------------------------------------------

    def _jumlah(self, nama: str) -> int:
        """Banyak baris bingkai `nama` bertanggal ≤ as_of."""
        n = self._n.get(nama)
        if n is None:
            n = int(np.searchsorted(getattr(self._kunci, nama), self._as_of_ns, side="right"))
            self._n[nama] = n
        return n

    def _cek_tanggal(self, d: date | pd.Timestamp | str) -> int:
        ns = _ts_ns(d)
        if ns > self._as_of_ns:
            raise LookAheadError(
                f"akses look-ahead ditolak: {self.kode} tanggal {_ke_date(d).isoformat()} "
                f"sesudah as_of {self._as_of.isoformat()}"
            )
        return ns

    def mentah(self) -> pd.DataFrame:
        return self._ds.mentah.iloc[: self._jumlah("mentah")]

    def valid(self) -> pd.DataFrame:
        return self._ds.valid.iloc[: self._jumlah("valid")]

    def indikator(self) -> pd.DataFrame:
        return self._ds.indikator.iloc[: self._jumlah("indikator")]

    def split(self) -> pd.Series:
        """Peristiwa split bertanggal ≤ as_of (split sesudah as_of belum diketahui pada as_of)."""
        return self._ds.split.iloc[: self._jumlah("split")]

    def baris(self, d: date | pd.Timestamp | str | None = None) -> pd.Series | None:
        """Baris indikator pada `d` (default as_of). None bila tidak ada bar valid pada d."""
        ns = self._as_of_ns if d is None else self._cek_tanggal(d)
        kunci = self._kunci.indikator
        n = self._jumlah("indikator")
        pos = int(np.searchsorted(kunci[:n], ns, side="left"))
        if pos < n and kunci[pos] == ns:
            return self._ds.indikator.iloc[pos]
        return None

    def sebelum(self, kolom: str, k: int) -> float:
        """`x_{t−k}` dengan t = as_of: nilai kolom indikator pada bar valid ke-k sebelum as_of.
        k = 0 → bar as_of sendiri (NaN bila as_of tanpa bar valid). NaN bila bar valid kurang.
        k < 0 meminta masa depan → LookAheadError."""
        if isinstance(k, bool) or not isinstance(k, (int, np.integer)):
            raise TypeError(f"k harus bilangan bulat (nilai: {k!r})")
        if k < 0:
            raise LookAheadError(f"akses look-ahead ditolak: {self.kode} {kolom} bar ke-{k} (k < 0)")
        nilai = self._ds.indikator[kolom]  # KeyError bila kolom tidak ada
        kunci = self._kunci.indikator
        n_le = self._jumlah("indikator")
        n_lt = int(np.searchsorted(kunci[:n_le], self._as_of_ns, side="left"))
        if k == 0:
            return float(nilai.iloc[n_lt]) if n_le > n_lt else math.nan
        pos = n_lt - k
        if pos < 0:
            return math.nan
        return float(nilai.iloc[pos])

    def hhv_sejak(self, mulai: date, sampai: date | None = None) -> float:
        """`HHV_sejak_entry(high)` atas bar valid mulai..sampai (inklusif; sampai default as_of)."""
        if sampai is None:
            sampai = self._as_of
        else:
            self._cek_tanggal(sampai)
        return _hhv_sejak(self.valid()["high"], mulai, sampai)

    def tanda(self) -> pd.Series | None:
        """Baris tanda kualitas data tepat pada as_of. None bila as_of di luar index tanda (sebelum bar
        mentah pertama, sesudah akhir perhitungan, atau bukan hari bursa)."""
        kunci = self._kunci.tanda
        n = self._jumlah("tanda")
        if n > 0 and kunci[n - 1] == self._as_of_ns:
            return self._ds.tanda.iloc[n - 1]
        return None

    def alasan_data(self) -> str | None:
        """Alasan dikeluarkan karena data, as-of (§8.5): `alasan_seri` bila ada; DATA_BASI bila tidak ada
        baris tanda pada as_of; selain itu `kualitas.alasan_data_asof`. None = lolos."""
        if self._ds.alasan_seri:
            return self._ds.alasan_seri
        baris = self.tanda()
        if baris is None:
            return ALASAN_DATA_BASI
        from . import kualitas  # impor malas: kualitas tidak perlu dimuat untuk pemotongan

        return kualitas.alasan_data_asof(
            baris, self._cfg, self._ds.adalah_indeks, self._ds.csv_pengganti_penuh
        )

    def faktor_split(self, d: date | pd.Timestamp | str | None = None) -> float:
        """`F(d)` (§8.4) dari data split terkini — sah walau memakai split sesudah as_of, karena hanya
        membatalkan penyesuaian. `d` sendiri tidak boleh sesudah as_of."""
        if d is None:
            d = self._as_of
        else:
            self._cek_tanggal(d)
        return _faktor_split(self._ds.split, d)

    def nominal(self, harga_adj: Any, d: date | pd.Timestamp | str | None = None) -> Any:
        """Harga nominal pada d = harga tersesuaikan × F(d). Decimal masuk → Decimal keluar."""
        return _harga_nominal(harga_adj, self.faktor_split(d))


class DataView:
    """Pandangan pasar pada `as_of`. Semua akses dipotong ≤ as_of; tanggal > as_of → LookAheadError."""

    __slots__ = ("_pasar", "_as_of", "_seri")

    def __init__(self, pasar: DataPasar, as_of: date | pd.Timestamp | str):
        self._pasar = pasar
        self._as_of = _ke_date(as_of)
        self._seri: dict[str, TampilanSeri] = {}

    @property
    def as_of(self) -> date:
        return self._as_of

    @property
    def kalender(self) -> "Kalender":
        return self._pasar.kalender

    @property
    def cfg(self) -> "Config":
        return self._pasar.cfg

    # -- daftar simbol --------------------------------------------------------------

    def kode(self) -> list[str]:
        """Kode saham yang dikenal run ini (tanpa IHSG), urut abjad."""
        return sorted(self._pasar._saham)

    def ada(self, kode: str) -> bool:
        """True bila kode dikenal run ini (saham, atau "IHSG" bila data IHSG ada)."""
        return self._pasar._cari(kode) is not None

    def seri(self, kode: str) -> TampilanSeri:
        tampil = self._seri.get(kode)
        if tampil is None:
            hasil = self._pasar._cari(kode)
            if hasil is None:
                raise KeyError(f"kode tidak dikenal di data pasar: {kode}")
            tampil = TampilanSeri(hasil[0], hasil[1], self._as_of, self._pasar.cfg)
            self._seri[kode] = tampil
        return tampil

    # -- pintasan per saham ---------------------------------------------------------

    def valid(self, kode: str) -> pd.DataFrame:
        return self.seri(kode).valid()

    def mentah(self, kode: str) -> pd.DataFrame:
        return self.seri(kode).mentah()

    def indikator(self, kode: str) -> pd.DataFrame:
        return self.seri(kode).indikator()

    def baris(self, kode: str, d: date | pd.Timestamp | str | None = None) -> pd.Series | None:
        return self.seri(kode).baris(d)

    def sebelum(self, kode: str, kolom: str, k: int) -> float:
        return self.seri(kode).sebelum(kolom, k)

    def hhv_sejak(self, kode: str, mulai: date, sampai: date | None = None) -> float:
        return self.seri(kode).hhv_sejak(mulai, sampai)

    def tanda(self, kode: str) -> pd.Series | None:
        return self.seri(kode).tanda()

    def alasan_data(self, kode: str) -> str | None:
        return self.seri(kode).alasan_data()

    def faktor_split(self, kode: str, d: date | pd.Timestamp | str | None = None) -> float:
        return self.seri(kode).faktor_split(d)

    def nominal(self, kode: str, harga_adj: Any, d: date | pd.Timestamp | str | None = None) -> Any:
        return self.seri(kode).nominal(harga_adj, d)

    # -- IHSG -----------------------------------------------------------------------

    def ihsg(self) -> TampilanSeri | None:
        if self._pasar._ihsg is None:
            return None
        return self.seri(KODE_IHSG)

    def ihsg_indikator(self) -> pd.DataFrame:
        """Indikator IHSG ≤ as_of; bingkai kosong bila data IHSG tidak ada."""
        tampil = self.ihsg()
        if tampil is None:
            return pd.DataFrame(index=pd.DatetimeIndex([], name="tanggal"))
        return tampil.indikator()

    def ihsg_baris(self, d: date | pd.Timestamp | str | None = None) -> pd.Series | None:
        tampil = self.ihsg()
        return None if tampil is None else tampil.baris(d)
