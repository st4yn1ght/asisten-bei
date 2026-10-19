"""Kalender bursa (SPEC §8.6–§8.7).

- Kalender manual: Senin–Jumat dikurangi `holidays.csv`. Dipakai untuk `t`, `t+1`, dan semua tanggal
  di tahun yang punya ≥ 1 entri libur.
- Kalender turunan: untuk tahun tanpa entri libur (riwayat backtest), diturunkan dari bar mentah
  `breadth_universe` (§8.7). `t` tidak pernah ditentukan oleh kalender turunan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from .config import Config, batas_bar_final_offset, parse_jam
from .waktu import ZONA, ke_wib


@dataclass(frozen=True)
class Peringatan:
    level: str  # "BANNER" | "PERINGATAN"
    teks: str


BANNER_TERLAMBAT = "TERLAMBAT — rencana mungkin tidak bisa dieksekusi hari ini"
BANNER_RUN_T1 = "RUN PADA HARI t+1 — pastikan sendiri masih sebelum pra-pembukaan"
BANNER_T1_TANPA_KALENDER = "Tanggal t+1 belum terverifikasi terhadap kalender libur resmi"


class Kalender:
    """Kalender gabungan manual + turunan.

    `libur`: tanggal → keterangan dari holidays.csv.
    `turunan`: himpunan hari bursa hasil `hitung_kalender_turunan` (boleh None), berlaku untuk tahun
    tanpa entri libur dan hanya di dalam rentang data turunan [`turunan_mulai`, `turunan_akhir`].
    Di luar rentang itu, tahun tanpa entri memakai aturan Senin–Jumat.
    """

    def __init__(
        self,
        libur: Mapping[date, str] | Iterable[date],
        turunan: Iterable[date] | None = None,
        turunan_rentang: tuple[date, date] | None = None,
    ):
        if isinstance(libur, Mapping):
            self.libur: dict[date, str] = dict(libur)
        else:
            self.libur = {d: "" for d in libur}
        self.tahun_manual: frozenset[int] = frozenset(d.year for d in self.libur)
        self._turunan: frozenset[date] | None = None
        self.turunan_mulai: date | None = None
        self.turunan_akhir: date | None = None
        if turunan is not None:
            self._turunan = frozenset(turunan)
            if turunan_rentang is not None:
                self.turunan_mulai, self.turunan_akhir = turunan_rentang
            elif self._turunan:
                self.turunan_mulai, self.turunan_akhir = min(self._turunan), max(self._turunan)

    # -- predikat -----------------------------------------------------------------

    def tercakup_manual(self, tahun: int) -> bool:
        return tahun in self.tahun_manual

    def adalah_hari_bursa_manual(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.libur

    def adalah_hari_bursa(self, d: date) -> bool:
        """Kalender efektif untuk data historis (validasi bar, hitungan hari)."""
        if d.year in self.tahun_manual or self._turunan is None:
            return self.adalah_hari_bursa_manual(d)
        if self.turunan_mulai is not None and self.turunan_mulai <= d <= self.turunan_akhir:
            return d in self._turunan
        return self.adalah_hari_bursa_manual(d)

    def _cek(self, manual: bool):
        return self.adalah_hari_bursa_manual if manual else self.adalah_hari_bursa

    # -- navigasi -----------------------------------------------------------------

    def hari_bursa_berikut(self, d: date, manual: bool = False) -> date:
        cek = self._cek(manual)
        x = d + timedelta(days=1)
        for _ in range(3660):
            if cek(x):
                return x
            x += timedelta(days=1)
        raise ValueError(f"tidak ada hari bursa setelah {d}")

    def hari_bursa_sebelum(self, d: date, manual: bool = False) -> date:
        cek = self._cek(manual)
        x = d - timedelta(days=1)
        for _ in range(3660):
            if cek(x):
                return x
            x -= timedelta(days=1)
        raise ValueError(f"tidak ada hari bursa sebelum {d}")

    def daftar_hari_bursa(self, mulai: date, akhir: date, manual: bool = False) -> list[date]:
        """Hari bursa dalam [mulai, akhir], inklusif."""
        cek = self._cek(manual)
        hasil, x = [], mulai
        while x <= akhir:
            if cek(x):
                hasil.append(x)
            x += timedelta(days=1)
        return hasil

    def indeks_hari_bursa(self, mulai: date, akhir: date, manual: bool = False) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(
            [pd.Timestamp(d) for d in self.daftar_hari_bursa(mulai, akhir, manual)], name="tanggal"
        )

    def nomor_hari(self, entry: date, d: date) -> int:
        """Hari ke-N sejak entry (hari entry = 1), dalam hari bursa kalender (§4.1)."""
        if d < entry:
            raise ValueError("d sebelum entry")
        return len(self.daftar_hari_bursa(entry, d))


# ---------------------------------------------------------------------------
# Kalender turunan (§8.7)
# ---------------------------------------------------------------------------


def hitung_kalender_turunan(
    bar_mentah: Mapping[str, pd.DataFrame],
    min_pct: float,
    tahun_dikecualikan: Iterable[int] = (),
) -> tuple[set[date], tuple[date, date] | None]:
    """Hari bursa turunan dari bar MENTAH saham breadth (sebelum validasi).

    Tanggal `d` (Senin–Jumat, tahunnya tidak dikecualikan) adalah hari bursa jika ≥ `min_pct` persen
    saham yang bar pertamanya < d DAN bar terakhirnya ≥ d memiliki bar bervolume > 0 pada d (saham yang
    datanya sudah berakhir — delisting atau cache belum diperbarui — tidak ikut menilai d). Jika tidak ada
    saham seperti itu (penyebut 0), d adalah hari bursa bila setidaknya satu saham bervolume > 0 pada d.

    Mengembalikan (himpunan hari bursa, (tanggal data pertama, tanggal data terakhir)) — rentang
    dipakai `Kalender` agar tanggal di luar data turunan jatuh ke aturan Senin–Jumat.
    """
    dikecualikan = set(tahun_dikecualikan)
    seri_aktif = {}
    pertama = {}
    terakhir = {}
    for kode, df in bar_mentah.items():
        if df is None or len(df) == 0:
            continue
        idx = pd.DatetimeIndex(df.index)
        vol = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else pd.Series(np.nan, index=idx)
        aktif = pd.Series((vol.to_numpy() > 0), index=idx)
        aktif = aktif.groupby(level=0).max()  # duplikat tanggal: cukup salah satu bervolume
        seri_aktif[kode] = aktif
        pertama[kode] = idx.min()
        terakhir[kode] = idx.max()
    if not seri_aktif:
        return set(), None
    semua = pd.DataFrame(seri_aktif).sort_index()
    tanggal = semua.index
    aktif = semua.fillna(False).astype(bool).to_numpy()
    pertama_arr = np.array([pertama[k] for k in semua.columns], dtype="datetime64[ns]")
    terakhir_arr = np.array([terakhir[k] for k in semua.columns], dtype="datetime64[ns]")
    hasil: set[date] = set()
    for i, ts in enumerate(tanggal):
        d = ts.date()
        if d.weekday() >= 5 or d.year in dikecualikan:
            continue
        sudah = (pertama_arr < np.datetime64(ts)) & (terakhir_arr >= np.datetime64(ts))
        penyebut = int(sudah.sum())
        if penyebut == 0:
            if aktif[i].any():
                hasil.add(d)
            continue
        pembilang = int((aktif[i] & sudah).sum())
        if pembilang * 100.0 >= float(min_pct) * penyebut:
            hasil.add(d)
    return hasil, (tanggal.min().date(), tanggal.max().date())


def bangun_kalender(
    libur: Mapping[date, str],
    bar_mentah_breadth: Mapping[str, pd.DataFrame] | None,
    min_pct: float,
) -> Kalender:
    """Kalender gabungan. Tanpa bar breadth, tahun tanpa entri libur memakai Senin–Jumat."""
    if not bar_mentah_breadth:
        return Kalender(libur)
    tahun_manual = {d.year for d in libur}
    turunan, rentang = hitung_kalender_turunan(bar_mentah_breadth, min_pct, tahun_manual)
    return Kalender(libur, turunan, rentang)


def bandingkan_kalender(
    kal: Kalender,
    bar_mentah_breadth: Mapping[str, pd.DataFrame],
    min_pct: float,
    sampai: date,
) -> list[str]:
    """Bandingkan kalender manual vs turunan untuk tanggal lampau (≤ sampai) di tahun yang
    tercakup holidays.csv (§8.7). Mengembalikan daftar teks peringatan."""
    if not kal.tahun_manual or not bar_mentah_breadth:
        return []
    turunan, rentang = hitung_kalender_turunan(bar_mentah_breadth, min_pct, ())
    if rentang is None:
        return []
    awal, akhir = rentang
    akhir = min(akhir, sampai)
    pesan = []
    x = awal
    while x <= akhir:
        if x.year in kal.tahun_manual and x.weekday() < 5:
            manual = kal.adalah_hari_bursa_manual(x)
            ada_tx = x in turunan
            if not manual and ada_tx:
                pesan.append(
                    f"{x.isoformat()}: tercatat libur di holidays.csv, tetapi data breadth menunjukkan ada transaksi"
                )
            elif manual and not ada_tx:
                pesan.append(
                    f"{x.isoformat()}: bukan libur menurut holidays.csv, tetapi data breadth hampir tanpa transaksi "
                    "(mungkin libur yang belum dicatat)"
                )
        x += timedelta(days=1)
    return pesan


# ---------------------------------------------------------------------------
# t, t+1, keterlambatan, peringatan libur (§8.6, §8.7, §6.7)
# ---------------------------------------------------------------------------


def batas_bar_final(cfg: Config, tanggal: date) -> datetime:
    """Waktu (WIB) setelah bar `tanggal` dianggap final. Bisa jatuh pada hari berikutnya bila
    jam penutupan + jeda melewati tengah malam."""
    return datetime.combine(tanggal, time(0), tzinfo=ZONA) + batas_bar_final_offset(cfg)


def tanggal_final_terakhir(cfg: Config, sekarang: datetime) -> date:
    """Tanggal kalender terbaru d ≤ hari ini yang barnya sudah final pada `sekarang`."""
    now = ke_wib(sekarang)
    d = now.date()
    while now < batas_bar_final(cfg, d):
        d -= timedelta(days=1)
    return d


def tentukan_t(cfg: Config, kal: Kalender, sekarang: datetime) -> date:
    """Hari bursa (manual) terbaru yang barnya sudah final pada `sekarang` (§8.6)."""
    final = tanggal_final_terakhir(cfg, sekarang)
    if kal.adalah_hari_bursa_manual(final):
        return final
    return kal.hari_bursa_sebelum(final, manual=True)


def tentukan_t1(kal: Kalender, t: date) -> date:
    return kal.hari_bursa_berikut(t, manual=True)


def banner_keterlambatan(cfg: Config, kal: Kalender, t: date, sekarang: datetime) -> str | None:
    """Banner keterlambatan bila run terjadi pada tanggal t+1 (§8.7)."""
    now = ke_wib(sekarang)
    if now.date() != tentukan_t1(kal, t):
        return None
    js = cfg.pasar.jadwal_sesi
    if js.terverifikasi and js.jam_pra_pembukaan:
        if now.time() >= parse_jam(js.jam_pra_pembukaan):
            return BANNER_TERLAMBAT
        return None
    return BANNER_RUN_T1


def peringatan_libur(kal: Kalender, sekarang: datetime) -> list[Peringatan]:
    """Peringatan bila holidays.csv tidak punya entri tahun berjalan/berikutnya (§6.7).
    Sejak Desember, peringatan tahun berikutnya tampil sebagai banner."""
    now = ke_wib(sekarang)
    tahun = now.year
    hasil = []
    if not kal.tercakup_manual(tahun):
        hasil.append(
            Peringatan("PERINGATAN", f"holidays.csv belum berisi tanggal libur tahun {tahun} — isi dari idx.co.id")
        )
    if not kal.tercakup_manual(tahun + 1):
        level = "BANNER" if now.month == 12 else "PERINGATAN"
        hasil.append(
            Peringatan(level, f"holidays.csv belum berisi tanggal libur tahun {tahun + 1} — isi dari idx.co.id")
        )
    return hasil


def banner_t1_tanpa_kalender(kal: Kalender, t1: date) -> str | None:
    if not kal.tercakup_manual(t1.year):
        return BANNER_T1_TANPA_KALENDER
    return None
