"""Validasi & laporan kualitas data (SPEC §8.5, §8.4, §11 syarat 3 & 5).

Tiga lapis pemeriksaan:
- per seri (struktur): `validasi_struktur` — kolom, tipe, index tanggal, akhir pekan, duplikat;
- per bar: `validasi_bar` — bar non-bursa dibuang, bar rusak & bar tanpa transaksi ditandai;
- per tanggal `d` (as-of, hanya bar ≤ d): `hitung_tanda_asof` lalu `alasan_data_asof`.

P1: tidak ada baris yang dibuat atau diisi. Bar rusak/tanpa transaksi tetap ada di `mentah` (bertanda)
dan tidak masuk `valid`; tanggal tanpa bar tetap tanpa bar (dihitung sebagai bar hilang).
P3: semua kolom `hitung_tanda_asof` kausal — jumlah kumulatif & jendela bergulir ke belakang, tanpa
`shift(-k)`; nilai baris `d` dari data penuh identik dengan nilai dari data yang dipotong ≤ d.
"""

from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from .provider import KOLOM_BAR, NAMA_INDEX, ke_ts
from .split import normalisasi_split, split_belum_disesuaikan

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..kalender import Kalender

# ---------------------------------------------------------------------------
# Enum (§4.3)
# ---------------------------------------------------------------------------

# tanda_bar
TANDA_HARI_NONBURSA = "BAR_HARI_NONBURSA"
TANDA_BAR_RUSAK = "BAR_RUSAK"
TANDA_TANPA_TRANSAKSI = "TANPA_TRANSAKSI"
TANDA_BAR: tuple[str, ...] = (TANDA_HARI_NONBURSA, TANDA_BAR_RUSAK, TANDA_TANPA_TRANSAKSI)

# alasan_dikeluarkan (urutan sama dengan §4.3)
ALASAN_DIKELUARKAN: tuple[str, ...] = (
    "GAGAL_AMBIL",
    "DATA_BASI",
    "BAR_HILANG",
    "ANOMALI",
    "SPLIT_BELUM_DISESUAIKAN",
    "RIWAYAT_PENDEK",
    "RIWAYAT_CSV_PENDEK",
    "KEMUNGKINAN_SUSPENSI",
    "TIDAK_LIKUID_RATA2",
    "DIKECUALIKAN",
    "BUKAN_KONSTITUEN",
)

# Uraian singkat untuk laporan (kode enum tetap ditulis apa adanya di CSV).
TEKS_ALASAN: dict[str, str] = {
    "GAGAL_AMBIL": "data gagal diambil",
    "DATA_BASI": "data basi (tidak ada bar valid pada tanggal ini)",
    "BAR_HILANG": "terlalu banyak bar hilang dalam jendela kelengkapan",
    "ANOMALI": "anomali data (lonjakan harga mencurigakan) — cek ke sumber resmi; bila benar, catat di anomali_ok.csv",
    "SPLIT_BELUM_DISESUAIKAN": "split belum disesuaikan sumber data (diunduh ulang penuh pada run berikutnya)",
    "RIWAYAT_PENDEK": "riwayat bar valid terlalu pendek",
    "RIWAYAT_CSV_PENDEK": "riwayat CSV manual terlalu pendek",
    "KEMUNGKINAN_SUSPENSI": "kemungkinan suspensi — cek idx.co.id",
    "TIDAK_LIKUID_RATA2": "rata-rata nilai transaksi di bawah ambang likuiditas",
    "DIKECUALIKAN": "tercantum di exclude.csv",
    "BUKAN_KONSTITUEN": "bukan konstituen universe pada tanggal ini",
}

_KOLOM_HARGA: tuple[str, ...] = ("open", "high", "low", "close")
_MAKS_CONTOH = 5  # jumlah tanggal contoh di catatan

log = logging.getLogger("asisten_bei")


def _tgl(ts: Any) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _contoh(idx: pd.Index) -> str:
    teks = ", ".join(_tgl(t) for t in idx[:_MAKS_CONTOH])
    return teks + (", …" if len(idx) > _MAKS_CONTOH else "")


# ---------------------------------------------------------------------------
# Validasi per seri (struktur)
# ---------------------------------------------------------------------------


@dataclass
class HasilStruktur:
    ok: bool
    bar: pd.DataFrame | None  # sudah dibersihkan (urut, tanpa duplikat), None jika gagal
    pesan: str = ""  # alasan GAGAL
    catatan: list[str] = field(default_factory=list)


def validasi_struktur(bar: pd.DataFrame | None, adalah_indeks: bool = False) -> HasilStruktur:
    """Pemeriksaan struktur per seri (§8.5).

    - kolom wajib hilang / tipe salah → GAGAL
    - index bukan tanggal tengah malam tz-naive (atau memuat NaT) → GAGAL ("tanggal/zona waktu tidak dikenali")
    - bar bertanggal akhir pekan → GAGAL
    - tanggal duplikat pada tanggal terakhir: identik → simpan satu; berbeda → tanggal itu dibuang
      (dianggap belum final)
    - tanggal duplikat di tempat lain → GAGAL
    Untuk seri indeks (IHSG), kolom volume opsional (diisi NaN bila tidak ada).
    Kolom tambahan (mis. `sumber`, `split`) dipertahankan apa adanya. Index keluaran datetime64[ns].
    """
    if bar is None:
        return HasilStruktur(False, None, "tidak ada data")
    df = bar.copy()
    wajib = [k for k in KOLOM_BAR if not (adalah_indeks and k == "volume")]
    hilang = [k for k in wajib if k not in df.columns]
    if hilang:
        return HasilStruktur(False, None, f"kolom wajib hilang: {', '.join(hilang)}")
    if adalah_indeks and "volume" not in df.columns:
        df["volume"] = np.nan

    if not isinstance(df.index, pd.DatetimeIndex):
        return HasilStruktur(False, None, "tanggal/zona waktu tidak dikenali (index bukan tanggal)")
    if df.index.tz is not None:
        return HasilStruktur(False, None, "tanggal/zona waktu tidak dikenali (index ber-zona waktu)")
    if df.index.hasnans:
        return HasilStruktur(False, None, "tanggal/zona waktu tidak dikenali (index memuat tanggal kosong)")
    if len(df) and not (df.index == df.index.normalize()).all():
        return HasilStruktur(False, None, "tanggal/zona waktu tidak dikenali (index memuat jam)")

    for k in KOLOM_BAR:
        kolom = df[k]
        if pd.api.types.is_bool_dtype(kolom):
            return HasilStruktur(False, None, f"tipe kolom {k} salah (bukan angka)")
        if not pd.api.types.is_numeric_dtype(kolom):
            if not (pd.api.types.is_object_dtype(kolom) or pd.api.types.is_string_dtype(kolom)):
                return HasilStruktur(False, None, f"tipe kolom {k} salah (bukan angka)")
            angka = pd.to_numeric(kolom, errors="coerce")
            if (angka.isna() & kolom.notna()).any():
                return HasilStruktur(False, None, f"tipe kolom {k} salah (bukan angka)")
            df[k] = angka
        df[k] = df[k].astype("float64")

    akhir_pekan = df.index[df.index.dayofweek >= 5]
    if len(akhir_pekan):
        contoh = ", ".join(d.strftime("%Y-%m-%d") for d in akhir_pekan[:3])
        return HasilStruktur(
            False, None, f"tanggal/zona waktu tidak dikenali (bar bertanggal akhir pekan: {contoh})"
        )

    df = df.sort_index(kind="mergesort")
    df.index = df.index.as_unit("ns")
    df.index.name = NAMA_INDEX
    catatan: list[str] = []
    ganda = df.index[df.index.duplicated(keep=False)].unique()
    if len(ganda):
        terakhir = df.index.max()
        lain = [d for d in ganda if d != terakhir]
        if lain:
            contoh = ", ".join(d.strftime("%Y-%m-%d") for d in lain[:3])
            return HasilStruktur(False, None, f"tanggal duplikat: {contoh}")
        baris = df.loc[[terakhir], list(KOLOM_BAR)]
        identik = all(baris.iloc[i].equals(baris.iloc[0]) for i in range(1, len(baris)))
        if identik:
            df = df[~df.index.duplicated(keep="first")]
        else:
            df = df[df.index != terakhir]
            catatan.append(
                f"bar {terakhir.strftime('%Y-%m-%d')} ganda dengan nilai berbeda: dianggap belum final"
            )
    return HasilStruktur(True, df, "", catatan)


# ---------------------------------------------------------------------------
# Hari bursa (kalender efektif), di-cache per objek Kalender
# ---------------------------------------------------------------------------

# Kalender → (awal, akhir, tanggal hari bursa dalam ns). Dihitung sekali per rentang terluas yang pernah
# diminta; ±500 saham dalam satu run memakai kalender yang sama sehingga cukup satu loop Python.
_CACHE_HARI_BURSA: "weakref.WeakKeyDictionary[Any, tuple[date, date, np.ndarray]]" = weakref.WeakKeyDictionary()


def _hari_bursa_antara(kal: "Kalender", mulai: date, akhir: date) -> pd.DatetimeIndex:
    """Hari bursa kalender efektif (`kal.adalah_hari_bursa`) dalam [mulai, akhir], index ns."""
    if akhir < mulai:
        return pd.DatetimeIndex([], dtype="datetime64[ns]", name=NAMA_INDEX)
    simpan = _CACHE_HARI_BURSA.get(kal)
    if simpan is None or mulai < simpan[0] or akhir > simpan[1]:
        awal_b = mulai if simpan is None else min(mulai, simpan[0])
        akhir_b = akhir if simpan is None else max(akhir, simpan[1])
        nilai = np.array(
            [ke_ts(d).as_unit("ns").value for d in kal.daftar_hari_bursa(awal_b, akhir_b)], dtype="int64"
        )
        simpan = (awal_b, akhir_b, nilai)
        _CACHE_HARI_BURSA[kal] = simpan
    nilai = simpan[2]
    i0 = np.searchsorted(nilai, ke_ts(mulai).as_unit("ns").value, side="left")
    i1 = np.searchsorted(nilai, ke_ts(akhir).as_unit("ns").value, side="right")
    return pd.DatetimeIndex(nilai[i0:i1].view("datetime64[ns]"), name=NAMA_INDEX)


# ---------------------------------------------------------------------------
# Validasi per bar
# ---------------------------------------------------------------------------


@dataclass
class SeriTervalidasi:
    kode: str
    adalah_indeks: bool
    mentah: pd.DataFrame  # bar pada hari bursa (non-bursa sudah dibuang): open..volume, sumber, tanda_bar
    valid: pd.DataFrame  # bar valid saja: open..volume, sumber
    split: pd.Series  # normalisasi_split
    dibuang_nonbursa: list[date] = field(default_factory=list)
    catatan: list[str] = field(default_factory=list)


def _mask_rusak(df: pd.DataFrame, adalah_indeks: bool) -> np.ndarray:
    """Bar rusak (§8.5): OHLC NaN/tak hingga, high < low, open/close di luar [low, high], harga ≤ 0,
    volume NaN/tak hingga atau < 0 (indeks: volume tidak diperiksa)."""
    o, h, l, c = (df[k].to_numpy(dtype="float64") for k in _KOLOM_HARGA)
    harga = np.vstack([o, h, l, c])
    with np.errstate(invalid="ignore"):
        rusak = ~np.isfinite(harga).all(axis=0)
        rusak |= (harga <= 0).any(axis=0)
        rusak |= h < l
        rusak |= (o < l) | (o > h) | (c < l) | (c > h)
        if not adalah_indeks:
            v = df["volume"].to_numpy(dtype="float64")
            rusak |= ~np.isfinite(v) | (v < 0)
    return rusak


def validasi_bar(
    bar: pd.DataFrame,
    split: pd.Series | None,
    kode: str,
    adalah_indeks: bool,
    kal: "Kalender",
) -> SeriTervalidasi:
    """Pemeriksaan per bar (§8.5) atas seri yang sudah lolos `validasi_struktur`.

    1. Bar pada tanggal bukan hari bursa (kalender efektif `kal.adalah_hari_bursa`) dibuang sebelum
       perhitungan apa pun dan dicatat (`BAR_HARI_NONBURSA`).
    2. Bar rusak → `tanda_bar = BAR_RUSAK` (dianggap hilang). Saham: volume = 0 → `TANPA_TRANSAKSI`
       (bila bar juga rusak, tanda BAR_RUSAK yang dipakai). Indeks: volume diabaikan.
    3. `valid` = bar bertanda "" (saham: tidak rusak & volume > 0; indeks: tidak rusak).

    Tidak ada baris yang dibuat atau nilainya diubah (P1). `split` None → diambil dari kolom `split`
    DataFrame cache bila ada (0.0 = tidak ada split). Kolom `sumber` dipertahankan; bila tidak ada, diisi ""
    (metadata, bukan data harga).
    """
    df = bar.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("validasi_bar: index bar harus DatetimeIndex (jalankan validasi_struktur dulu)")
    df.index = df.index.as_unit("ns")
    df.index.name = NAMA_INDEX
    if adalah_indeks and "volume" not in df.columns:
        df["volume"] = np.nan
    if split is None and "split" in df.columns:
        split = df["split"]
    seri_split = normalisasi_split(split)
    if "sumber" not in df.columns:
        df["sumber"] = ""
    df = df.loc[:, [*KOLOM_BAR, "sumber"]]
    catatan: list[str] = []

    # 1. Bar non-bursa dibuang (sebelum perhitungan apa pun).
    dibuang: list[date] = []
    if len(df):
        hari_bursa = _hari_bursa_antara(kal, df.index[0].date(), df.index[-1].date())
        di_bursa = df.index.isin(hari_bursa)
        if not di_bursa.all():
            idx_buang = df.index[~di_bursa]
            dibuang = [t.date() for t in idx_buang]
            catatan.append(
                f"{len(idx_buang)} bar bukan hari bursa dibuang ({TANDA_HARI_NONBURSA}): {_contoh(idx_buang)}"
            )
            df = df[di_bursa]

    # 2. Tanda per bar.
    rusak = _mask_rusak(df, adalah_indeks)
    if adalah_indeks:
        nol = np.zeros(len(df), dtype=bool)
    else:
        nol = ~rusak & (df["volume"].to_numpy(dtype="float64") == 0)
    tanda = np.where(rusak, TANDA_BAR_RUSAK, np.where(nol, TANDA_TANPA_TRANSAKSI, ""))
    mentah = df.copy()
    mentah["tanda_bar"] = pd.Series(tanda, index=df.index, dtype=object).astype(str)
    if rusak.any():
        catatan.append(
            f"{int(rusak.sum())} bar rusak dianggap hilang ({TANDA_BAR_RUSAK}): {_contoh(df.index[rusak])}"
        )
    if nol.any():
        catatan.append(
            f"{int(nol.sum())} bar tanpa transaksi ({TANDA_TANPA_TRANSAKSI}): {_contoh(df.index[nol])}"
        )

    # 3. Bar valid.
    valid = df[(tanda == "")]
    return SeriTervalidasi(
        kode=kode,
        adalah_indeks=adalah_indeks,
        mentah=mentah,
        valid=valid,
        split=seri_split,
        dibuang_nonbursa=dibuang,
        catatan=catatan,
    )


# ---------------------------------------------------------------------------
# Anomali (§8.5)
# ---------------------------------------------------------------------------


def _mask_anomali(valid: pd.DataFrame, ambang_pct: Any, tanggal_ok: set) -> np.ndarray:
    """Mask (panjang = len(valid)) bar valid `a` dengan |close_a / close_{a−1} − 1| × 100 > ambang.

    Dihitung sebagai |close_a − close_{a−1}| × 100 > ambang × close_{a−1} (setara untuk close > 0, tanpa
    pembagian) agar batas tepat (mis. +40% pada ambang 40) tidak terbalik karena galat pembulatan.
    Hanya membandingkan dua bar valid berurutan → kausal."""
    n = len(valid)
    mask = np.zeros(n, dtype=bool)
    if n < 2:
        return mask
    c = valid["close"].to_numpy(dtype="float64")
    ambang = float(ambang_pct)
    with np.errstate(invalid="ignore"):
        mask[1:] = np.abs(c[1:] - c[:-1]) * 100.0 > ambang * c[:-1]
    if tanggal_ok and mask.any():
        ok = pd.DatetimeIndex([ke_ts(d) for d in tanggal_ok]).as_unit("ns")
        mask &= ~pd.DatetimeIndex(valid.index).as_unit("ns").isin(ok)
    return mask


def deteksi_anomali(valid: pd.DataFrame, ambang_pct: float, tanggal_ok: set[date]) -> list[date]:
    """Tanggal anomali `a` (§8.5): |close_a / close_{a−1} − 1| × 100 > `ambang_pct` antara dua bar valid
    berurutan (ruang tersesuaikan), kecuali tanggal di `tanggal_ok` (anomali_ok.csv)."""
    mask = _mask_anomali(valid, ambang_pct, tanggal_ok)
    return [t.date() for t in valid.index[mask]]


# ---------------------------------------------------------------------------
# Tanda per tanggal (as-of)
# ---------------------------------------------------------------------------

KOLOM_TANDA: tuple[str, ...] = (
    "ada_bar_valid",
    "jumlah_bar_valid",
    "bar_hilang",
    "volume_nol",
    "anomali_aktif",
    "anomali_terakhir",
    "split_belum_disesuaikan",
)


def _tanda_kosong() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], dtype="datetime64[ns]", name=NAMA_INDEX)
    return pd.DataFrame(
        {
            "ada_bar_valid": pd.Series(dtype=bool),
            "jumlah_bar_valid": pd.Series(dtype="int64"),
            "bar_hilang": pd.Series(dtype="int64"),
            "volume_nol": pd.Series(dtype="int64"),
            "anomali_aktif": pd.Series(dtype=bool),
            "anomali_terakhir": pd.Series(dtype="datetime64[ns]"),
            "split_belum_disesuaikan": pd.Series(dtype=bool),
        },
        index=idx,
    )


def _jumlah_bergulir(x: np.ndarray, n: int) -> np.ndarray:
    """Jumlah `x` atas `n` elemen terakhir termasuk posisi ini (jendela ke belakang; awal seri: lebih
    pendek). Lewat jumlah kumulatif → bilangan bulat eksak."""
    c = np.cumsum(x, dtype="int64")
    hasil = c.copy()
    if n < len(c):
        hasil[n:] -= c[:-n]
    return hasil


def _potong_sampai(df: pd.DataFrame, akhir_ts: pd.Timestamp) -> pd.DataFrame:
    return df.iloc[: int(df.index.searchsorted(akhir_ts, side="right"))]


def hitung_tanda_asof(
    seri: SeriTervalidasi,
    kal: "Kalender",
    cfg: "Config",
    anomali_ok: set[tuple[str, date]],
    akhir: date,
) -> pd.DataFrame:
    """Tanda kualitas data per hari bursa `d` (as-of: hanya memakai bar ≤ d), tervektor & kausal.

    Index: hari bursa kalender efektif dari bar mentah pertama s/d `akhir` (inklusif); bar sesudah
    `akhir` diabaikan. Kolom:
    - ada_bar_valid (bool): ada bar valid pada d
    - jumlah_bar_valid (int): jumlah bar valid ≤ d
    - bar_hilang (int): hari bursa tanpa bar valid dalam `data.validasi.jendela_kelengkapan_bar` hari bursa
      terakhir ≤ d (hanya hari ≥ bar mentah pertama). Bar rusak & bar tanpa transaksi dihitung hilang.
    - volume_nol (int): bar mentah bervolume 0 dalam `likuiditas.periode_hari` hari bursa terakhir ≤ d
      (bar non-bursa sudah dibuang sehingga tidak ikut; indeks: selalu 0)
    - anomali_aktif (bool): ada anomali a ≤ d yang masih berada dalam N bar valid terakhir ≤ d
      (N = `universe.min_bar_riwayat` untuk saham, `kondisi_pasar.sma_panjang` untuk indeks)
    - anomali_terakhir (datetime64, NaT bila tidak ada): anomali terbaru ≤ d
    - split_belum_disesuaikan (bool): ada split bermasalah (§8.4) yang buktinya sudah tersedia ≤ d. Bukti
      split s = bar valid pertama ≥ s (biasanya s sendiri); memakai tanggal bukti, bukan s, menjaga
      kausalitas bila tidak ada bar valid pada s.
    """
    mentah = seri.mentah
    if mentah is None or len(mentah) == 0:
        return _tanda_kosong()
    akhir_ts = ke_ts(akhir).as_unit("ns")
    awal = mentah.index[0].date()
    hari = _hari_bursa_antara(kal, awal, akhir_ts.date())
    if len(hari) == 0:
        return _tanda_kosong()

    v = cfg.data.validasi
    valid = _potong_sampai(seri.valid, akhir_ts)
    mentah = _potong_sampai(mentah, akhir_ts)
    idx_valid = pd.DatetimeIndex(valid.index).as_unit("ns")

    # Kelengkapan.
    ada = hari.isin(idx_valid)
    jumlah = np.cumsum(ada, dtype="int64")
    w = int(v.jendela_kelengkapan_bar)
    n_hari = np.minimum(np.arange(1, len(hari) + 1, dtype="int64"), w)
    bar_hilang = n_hari - _jumlah_bergulir(ada.astype("int64"), w)

    # Volume 0 (bar mentah, saham saja).
    if seri.adalah_indeks:
        volume_nol = np.zeros(len(hari), dtype="int64")
    else:
        vol = mentah["volume"].to_numpy(dtype="float64")
        idx_nol = pd.DatetimeIndex(mentah.index[vol == 0]).as_unit("ns")
        volume_nol = _jumlah_bergulir(hari.isin(idx_nol).astype("int64"), int(cfg.likuiditas.periode_hari))

    # Anomali: posisi (urutan bar valid, mulai 1) anomali terbaru ≤ d via maksimum kumulatif.
    tanggal_ok = {d for (k, d) in anomali_ok if k == seri.kode}
    mask_an = _mask_anomali(valid, v.lonjakan_mencurigakan_pct, tanggal_ok)
    di_anomali = hari.isin(idx_valid[mask_an])
    posisi_hari = np.arange(len(hari), dtype="int64")
    hari_an_terakhir = np.maximum.accumulate(np.where(di_anomali, posisi_hari, -1))
    ada_an = hari_an_terakhir >= 0
    urutan_an = np.where(ada_an, jumlah[np.maximum(hari_an_terakhir, 0)], 0)
    n_aktif = int(cfg.kondisi_pasar.sma_panjang if seri.adalah_indeks else cfg.universe.min_bar_riwayat)
    anomali_aktif = ada_an & ((jumlah - urutan_an) < n_aktif)
    nilai_hari = hari.to_numpy(dtype="datetime64[ns]")
    anomali_terakhir = np.full(len(hari), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    anomali_terakhir[ada_an] = nilai_hari[hari_an_terakhir[ada_an]]

    # Split belum disesuaikan (§8.4) — permanen sejak tanggal bukti pertama.
    split_flag = np.zeros(len(hari), dtype=bool)
    if not seri.adalah_indeks and len(seri.split):
        bermasalah = split_belum_disesuaikan(valid, seri.split)
        if bermasalah:
            pos = idx_valid.searchsorted(pd.DatetimeIndex([ke_ts(s) for s in bermasalah]).as_unit("ns"), side="left")
            pos = pos[pos < len(idx_valid)]
            if len(pos):
                split_flag = nilai_hari >= idx_valid[int(pos.min())].to_datetime64()

    return pd.DataFrame(
        {
            "ada_bar_valid": ada.astype(bool),
            "jumlah_bar_valid": jumlah,
            "bar_hilang": bar_hilang.astype("int64"),
            "volume_nol": volume_nol.astype("int64"),
            "anomali_aktif": anomali_aktif.astype(bool),
            "anomali_terakhir": anomali_terakhir,
            "split_belum_disesuaikan": split_flag.astype(bool),
        },
        index=hari,
    )


def _bool(x: Any) -> bool:
    try:
        if pd.isna(x):
            return False
    except (TypeError, ValueError):
        pass
    return bool(x)


def _int(x: Any) -> int:
    try:
        if pd.isna(x):
            return 0
    except (TypeError, ValueError):
        pass
    return int(x)


def alasan_data_asof(
    baris: pd.Series | None,
    cfg: "Config",
    adalah_indeks: bool,
    csv_pengganti_penuh: bool = False,
) -> str | None:
    """Alasan data saham tidak dinilai pada d (§8.5, §11 syarat 3 & 5); None = lolos.

    Urutan: DATA_BASI → BAR_HILANG → ANOMALI → SPLIT_BELUM_DISESUAIKAN → RIWAYAT_PENDEK /
    RIWAYAT_CSV_PENDEK → KEMUNGKINAN_SUSPENSI. Indeks: hanya DATA_BASI, BAR_HILANG, ANOMALI.
    Batas: bar_hilang ≤ maks lolos; jumlah_bar_valid ≥ min_bar_riwayat lolos; volume_nol ≤ maks lolos.
    `baris` None (tidak ada baris tanda as-of d) diperlakukan sebagai tanpa bar valid → DATA_BASI.
    """
    if baris is None or not _bool(baris.get("ada_bar_valid", False)):
        return "DATA_BASI"
    if _int(baris.get("bar_hilang", 0)) > cfg.data.validasi.maks_bar_hilang:
        return "BAR_HILANG"
    if _bool(baris.get("anomali_aktif", False)):
        return "ANOMALI"
    if adalah_indeks:
        return None
    if _bool(baris.get("split_belum_disesuaikan", False)):
        return "SPLIT_BELUM_DISESUAIKAN"
    if _int(baris.get("jumlah_bar_valid", 0)) < cfg.universe.min_bar_riwayat:
        return "RIWAYAT_CSV_PENDEK" if csv_pengganti_penuh else "RIWAYAT_PENDEK"
    if _int(baris.get("volume_nol", 0)) > cfg.likuiditas.maks_hari_volume_nol:
        return "KEMUNGKINAN_SUSPENSI"
    return None


# ---------------------------------------------------------------------------
# Berkas kualitas (§8.5 "Keluaran")
# ---------------------------------------------------------------------------

KOLOM_KUALITAS: list[str] = [
    "kode",
    "sumber",
    "status_ambil",
    "jenis_error",
    "pesan",
    "bar_terakhir",
    "bar_hilang_60",
    "anomali_terakhir",
    "split_belum_disesuaikan",
    "alasan_dikeluarkan",
]


@dataclass
class BarisKualitas:
    """Satu baris berkas kualitas. Tanggal sebagai teks YYYY-MM-DD atau "" (tidak ada/tidak diketahui).

    `bar_hilang_60` = kolom `bar_hilang` as-of (jendela `jendela_kelengkapan_bar`; nama kolom tetap §8.5).
    `split_belum_disesuaikan` ditulis 1/0 (konvensi bendera CSV §17.1), "" bila tidak diketahui.
    """

    kode: str
    sumber: str = ""
    status_ambil: str = ""
    jenis_error: str | None = ""
    pesan: str = ""
    bar_terakhir: str = ""
    bar_hilang_60: int | None = None
    anomali_terakhir: str = ""
    split_belum_disesuaikan: bool | None = None
    alasan_dikeluarkan: str | None = ""

    def sebagai_dict(self) -> dict[str, str]:
        from ..operasional import PENYARING

        def teks(x: Any) -> str:
            return "" if x is None else str(x)

        pesan = " ".join(PENYARING.sensor(teks(self.pesan)).split())  # satu baris, tanpa rahasia
        split = "" if self.split_belum_disesuaikan is None else ("1" if self.split_belum_disesuaikan else "0")
        return {
            "kode": teks(self.kode),
            "sumber": teks(self.sumber),
            "status_ambil": teks(self.status_ambil),
            "jenis_error": teks(self.jenis_error),
            "pesan": pesan,
            "bar_terakhir": teks(self.bar_terakhir),
            "bar_hilang_60": "" if self.bar_hilang_60 is None else str(int(self.bar_hilang_60)),
            "anomali_terakhir": teks(self.anomali_terakhir),
            "split_belum_disesuaikan": split,
            "alasan_dikeluarkan": teks(self.alasan_dikeluarkan),
        }


def baris_kualitas_dari_tanda(
    kode: str,
    *,
    sumber: str = "",
    status_ambil: str = "",
    jenis_error: str | None = None,
    pesan: str = "",
    tanda: pd.DataFrame | None = None,
    d: date | None = None,
    alasan: str | None = None,
) -> BarisKualitas:
    """Bantuan: `BarisKualitas` dari tanda as-of `d` (default: baris tanda terakhir).

    `bar_terakhir` = bar valid terakhir ≤ d. Tanpa tanda (mis. GAGAL_AMBIL) kolom data dibiarkan kosong.
    `alasan` diisi pemanggil (alasan seri seperti GAGAL_AMBIL atau hasil `alasan_data_asof`).
    """
    baris = BarisKualitas(
        kode=kode, sumber=sumber, status_ambil=status_ambil, jenis_error=jenis_error or "", pesan=pesan,
        alasan_dikeluarkan=alasan or "",
    )
    if tanda is None or len(tanda) == 0:
        return baris
    sub = tanda if d is None else _potong_sampai(tanda, ke_ts(d).as_unit("ns"))
    if len(sub) == 0:
        return baris
    akhir = sub.iloc[-1]
    ada = sub["ada_bar_valid"].to_numpy(dtype=bool)
    if ada.any():
        baris.bar_terakhir = _tgl(sub.index[ada][-1])
    baris.bar_hilang_60 = _int(akhir["bar_hilang"])
    if not pd.isna(akhir["anomali_terakhir"]):
        baris.anomali_terakhir = _tgl(akhir["anomali_terakhir"])
    baris.split_belum_disesuaikan = _bool(akhir["split_belum_disesuaikan"])
    return baris


def path_berkas_kualitas(akar: Path, perintah: str, saat: datetime | None = None) -> Path:
    """`output/kualitas/<YYYYMMDD-HHMMSS>_<perintah>.csv` (§5) untuk `unduh`/`backtest`; jam WIB dari
    `waktu.sekarang()`."""
    from .. import waktu

    saat = waktu.sekarang() if saat is None else waktu.ke_wib(saat)
    return Path(akar) / "output" / "kualitas" / f"{saat:%Y%m%d-%H%M%S}_{perintah}.csv"


def tulis_berkas_kualitas(
    path: Path, baris: list[BarisKualitas], peringatan: list[str] | None = None
) -> Path:
    """Tulis berkas kualitas (§8.5): baris pertama disclaimer `# BUKAN SARAN INVESTASI ...`, lalu header
    `KOLOM_KUALITAS`. Atomik (`operasional.tulis_csv`). Bila berkas tujuan terkunci (mis. dibuka di Excel),
    ditulis ke nama alternatif; teks peringatannya ditambahkan ke `peringatan` (bila diberikan) dan ke log.
    Mengembalikan path yang benar-benar ditulis."""
    from ..operasional import tulis_csv

    df = pd.DataFrame([b.sebagai_dict() for b in baris], columns=KOLOM_KUALITAS, dtype=object)
    hasil, pesan = tulis_csv(Path(path), df, index=False)
    if pesan:
        log.warning(pesan)
        if peringatan is not None:
            peringatan.append(pesan)
    return hasil
