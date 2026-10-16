"""T-LA1a (indikator & tanda data), T-LA3 (uji jebakan), dan penjaga `DataView` (SPEC §8.8, §22.1).

- `periksa_kausal(fungsi, df, tanggal_list)`: untuk setiap t, hasil `fungsi(df ≤ t)` pada baris t harus
  IDENTIK (toleransi 0; NaN/NaT dianggap sama) dengan baris t dari `fungsi(df)` (data penuh).
- T-LA1a: ≥ 200 pasangan (saham, tanggal) acak berseed atas saham sintetis (random walk ±600 bar, bar
  volume 0, bar hilang, bar rusak, bar hari libur, lonjakan anomali, split) → tidak ada ketidakcocokan
  untuk `hitung_indikator`, `hitung_indikator_indeks`, dan `kualitas.hitung_tanda_asof`.
- T-LA3: indikator kanari yang memakai `shift(-1)` (dan bentuk look-ahead lain) WAJIB membuat
  pemeriksa gagal — bukti bahwa T-LA1 benar-benar bekerja.
- DataView: akses tanggal > as_of → LookAheadError; potongan tidak memuat tanggal > as_of.
"""

from __future__ import annotations

import copy
import math
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from asisten_bei import indikator as ind
from asisten_bei.config import Config, baca_yaml, validasi_dict
from asisten_bei.data import kualitas
from asisten_bei.data.provider import ke_ts
from asisten_bei.data.tampilan import DataPasar, DataSaham, DataView, LookAheadError
from asisten_bei.kalender import Kalender

AKAR_PROYEK = Path(__file__).resolve().parents[1]
SEED = 20261015

# ---------------------------------------------------------------------------
# Config & kalender uji
# ---------------------------------------------------------------------------

_DASAR = baca_yaml(AKAR_PROYEK / "config.yaml")  # hanya dibaca; parameter yang relevan di-set eksplisit
_UBAH = {
    "indikator.atr_periode": 14,
    "likuiditas.periode_hari": 20,
    "likuiditas.maks_hari_volume_nol": 0,
    "data.pengali_volume": 1,
    "data.validasi.lonjakan_mencurigakan_pct": 40,
    "data.validasi.jendela_kelengkapan_bar": 60,
    "data.validasi.maks_bar_hilang": 2,
    "universe.min_bar_riwayat": 120,  # lebih kecil dari 250 agar jendela anomali sempat kedaluwarsa
    "strategi.pb.sma_cepat": 20,
    "strategi.pb.sma_tren": 50,
    "strategi.pb.sma_panjang": 200,
    "strategi.pb.lookback_swing_low": 5,
    "strategi.bo.lookback_tertinggi": 20,
    "strategi.bo.lookback_volume": 20,
    "strategi.bo.sma_tren": 50,
    "strategi.bo.sma_panjang": 200,
    "strategi.mr.rsi_periode": 2,
    "strategi.mr.sma_tren": 200,
    "strategi.mr.sma_keluar": 5,
    "kondisi_pasar.sma_pendek": 50,
    "kondisi_pasar.sma_panjang": 200,
    "kondisi_pasar.breadth_sma": 50,
    "skor.rs_lookback": 60,
}


def _cfg(ubah: dict | None = None) -> Config:
    data = copy.deepcopy(_DASAR)
    for kunci, nilai in {**_UBAH, **(ubah or {})}.items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = nilai
    return validasi_dict(data)


CFG = _cfg()
# Libur FIKTIF – KHUSUS UJI (bukan kalender BEI nyata).
LIBUR = {
    date(2023, 4, 7): "FIKTIF – KHUSUS UJI",
    date(2023, 12, 25): "FIKTIF – KHUSUS UJI",
    date(2024, 5, 1): "FIKTIF – KHUSUS UJI",
    date(2024, 8, 16): "FIKTIF – KHUSUS UJI",
    date(2025, 3, 31): "FIKTIF – KHUSUS UJI",
}
KAL = Kalender(LIBUR)
HARI = KAL.indeks_hari_bursa(date(2023, 1, 2), date(2026, 12, 31))

# ---------------------------------------------------------------------------
# Pemeriksa kausal (pembantu umum T-LA1/T-LA3)
# ---------------------------------------------------------------------------


def _nilai_sama(a, b) -> bool:
    na, nb = bool(pd.isna(a)), bool(pd.isna(b))
    if na or nb:
        return na and nb
    return bool(a == b)  # tanpa toleransi


def _sebagai_bingkai(hasil) -> pd.DataFrame:
    if isinstance(hasil, pd.Series):
        return hasil.to_frame(name=hasil.name if hasil.name is not None else "nilai")
    return hasil


def periksa_kausal(fungsi, df: pd.DataFrame, tanggal_list, *, pakai_akhir: bool = False) -> list[str]:
    """Bandingkan baris t dari `fungsi(df ≤ t)` dengan baris t dari `fungsi(df)` untuk setiap t.

    `fungsi(df)` → Series/DataFrame berindeks tanggal. Dengan `pakai_akhir=True` dipanggil
    `fungsi(df, akhir)` (akhir = t untuk data dipotong; tanggal terbesar untuk data penuh).
    Mengembalikan daftar ketidakcocokan (kosong = kausal pada tanggal-tanggal itu)."""
    tanggal = [ke_ts(t) for t in tanggal_list]
    if not tanggal:
        return []
    akhir_penuh = max([ke_ts(df.index.max()), *tanggal])
    penuh = _sebagai_bingkai(fungsi(df, akhir_penuh) if pakai_akhir else fungsi(df))
    idx = pd.DatetimeIndex(df.index)
    salah: list[str] = []
    for t in tanggal:
        potong = df.iloc[: int(idx.searchsorted(t, side="right"))]
        sebagian = _sebagai_bingkai(fungsi(potong, t) if pakai_akhir else fungsi(potong))
        teks_t = t.strftime("%Y-%m-%d")
        if len(sebagian) and pd.DatetimeIndex(sebagian.index).max() > t:
            salah.append(f"{teks_t}: hasil data dipotong memuat tanggal > t")
        ada_p, ada_s = t in penuh.index, t in sebagian.index
        if not ada_p and not ada_s:
            continue
        if ada_p != ada_s:
            salah.append(f"{teks_t}: baris hanya ada di {'data penuh' if ada_p else 'data dipotong'}")
            continue
        if list(penuh.columns) != list(sebagian.columns):
            salah.append(f"{teks_t}: kolom beda {list(sebagian.columns)} vs {list(penuh.columns)}")
            continue
        bp, bs = penuh.loc[t], sebagian.loc[t]
        for k in penuh.columns:
            if penuh[k].dtype != sebagian[k].dtype:
                salah.append(f"{teks_t} {k}: dtype {sebagian[k].dtype} vs {penuh[k].dtype}")
            elif not _nilai_sama(bs[k], bp[k]):
                salah.append(f"{teks_t} {k}: dipotong={bs[k]!r} penuh={bp[k]!r}")
    return salah


# ---------------------------------------------------------------------------
# Pasar sintetis berseed
# ---------------------------------------------------------------------------


def _buat_mentah(
    rng: np.random.Generator,
    *,
    n: int = 600,
    harga: float = 1000.0,
    adalah_indeks: bool = False,
    lonjakan_pada: int | None = None,
    nol_beruntun: tuple[int, int] | None = None,
    bolong: tuple[int, int] | None = None,
    rusak_pada: int | None = None,
    split_pada: int | None = None,
    split_disesuaikan: bool = True,
    bar_libur: date | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """Bar mentah (cache, ruang tersesuaikan) + seri split. Random walk log-normal; ±3% bar volume 0;
    ±2% bar hilang. Tidak ada baris yang diisi; hari tanpa bar memang tidak punya baris."""
    hari = HARI[:n]
    log_r = rng.normal(0.0004, 0.018, n)
    if lonjakan_pada is not None:
        log_r[lonjakan_pada] = math.log(1.6)  # +60% tanpa split → anomali
    close = harga * np.exp(np.cumsum(log_r))
    if split_pada is not None and not split_disesuaikan:
        close[:split_pada] *= 5.0  # sumber lupa menyesuaikan split 1:5 → q ≈ 1/5
    open_ = close * np.exp(rng.normal(0.0, 0.006, n))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.008, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.008, n)))
    if adalah_indeks:
        volume = np.full(n, np.nan)
    else:
        volume = rng.integers(50_000, 3_000_000, n).astype("float64")
        volume[rng.random(n) < 0.03] = 0.0
        if nol_beruntun is not None:
            volume[nol_beruntun[0] : nol_beruntun[1]] = 0.0
    if rusak_pada is not None:
        high[rusak_pada], low[rusak_pada] = low[rusak_pada], high[rusak_pada]  # high < low → BAR_RUSAK
    simpan = rng.random(n) >= 0.02
    if bolong is not None:
        simpan[bolong[0] : bolong[1]] = False
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=hari
    )[simpan]
    if bar_libur is not None:
        ekstra = pd.DataFrame(
            {"open": [100.0], "high": [100.0], "low": [100.0], "close": [100.0], "volume": [0.0]},
            index=pd.DatetimeIndex([ke_ts(bar_libur)]),
        )
        df = pd.concat([df, ekstra]).sort_index()
    df.index = pd.DatetimeIndex(df.index, name="tanggal").as_unit("ns")
    df["sumber"] = "yfinance"
    if split_pada is not None:
        split = pd.Series([5.0], index=pd.DatetimeIndex([hari[split_pada]], name="tanggal"), name="split")
    else:
        split = pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="tanggal"), name="split")
    return df, split


def _bangun_pasar():
    rng = np.random.default_rng(SEED)
    spesifikasi = {
        "AAAA": dict(lonjakan_pada=300, harga=1500.0),
        "BBBB": dict(lonjakan_pada=200, nol_beruntun=(400, 404), bolong=(450, 455), harga=600.0),
        "CCCC": dict(split_pada=350, rusak_pada=120, bar_libur=date(2024, 5, 1), harga=4000.0),
        "DDDD": dict(split_pada=380, split_disesuaikan=False, harga=250.0),
        "IHSG": dict(adalah_indeks=True, harga=7000.0, bolong=(500, 504)),
    }
    mentah, split = {}, {}
    for kode, opsi in spesifikasi.items():
        mentah[kode], split[kode] = _buat_mentah(rng, **opsi)
    # Anomali BBBB tercatat di anomali_ok.csv → tidak boleh menandai.
    anomali_ok = {("BBBB", HARI[200].date())}
    return mentah, split, anomali_ok


MENTAH, SPLIT, ANOMALI_OK = _bangun_pasar()
SAHAM = ["AAAA", "BBBB", "CCCC", "DDDD"]


def _indeks(kode: str) -> bool:
    return kode == "IHSG"


def _validasi(mentah: pd.DataFrame, split: pd.Series, kode: str):
    return kualitas.validasi_bar(mentah, split, kode, _indeks(kode), KAL)


def _fungsi_indikator(kode: str):
    if _indeks(kode):
        return lambda valid: ind.hitung_indikator_indeks(valid, CFG)
    return lambda valid: ind.hitung_indikator(valid, CFG)


def _fungsi_tanda(kode: str):
    def f(mentah: pd.DataFrame, akhir: pd.Timestamp) -> pd.DataFrame:
        split = SPLIT[kode]
        split = split[split.index <= akhir]  # split sesudah t belum diketahui pada t
        seri = _validasi(mentah, split, kode)
        return kualitas.hitung_tanda_asof(seri, KAL, CFG, ANOMALI_OK, akhir.date())

    return f


def _data_saham(kode: str) -> DataSaham:
    seri = _validasi(MENTAH[kode], SPLIT[kode], kode)
    akhir = MENTAH[kode].index.max().date()
    return DataSaham(
        kode=kode,
        adalah_indeks=_indeks(kode),
        status_ambil="OK",
        jenis_error=None,
        pesan="",
        sumber="yfinance",
        mentah=seri.mentah,
        valid=seri.valid,
        split=seri.split,
        indikator=_fungsi_indikator(kode)(seri.valid),
        tanda=kualitas.hitung_tanda_asof(seri, KAL, CFG, ANOMALI_OK, akhir),
    )


@pytest.fixture(scope="module")
def pasar() -> DataPasar:
    return DataPasar({k: _data_saham(k) for k in SAHAM}, _data_saham("IHSG"), KAL, CFG)


def _pasangan(sumber: dict[str, pd.DatetimeIndex], jumlah: int, seed: int) -> dict[str, list[pd.Timestamp]]:
    """`jumlah` pasangan (kode, tanggal) acak berseed, dikelompokkan per kode."""
    rng = np.random.default_rng(seed)
    kode = sorted(sumber)
    hasil: dict[str, list[pd.Timestamp]] = {k: [] for k in kode}
    for _ in range(jumlah):
        k = kode[int(rng.integers(len(kode)))]
        idx = sumber[k]
        hasil[k].append(idx[int(rng.integers(len(idx)))])
    return hasil


# ---------------------------------------------------------------------------
# Data sintetis benar-benar menguji kasus sulit (uji tidak hampa)
# ---------------------------------------------------------------------------


def test_data_sintetis_memuat_kasus_sulit(pasar):
    dv = pasar.tampilan(date(2026, 12, 31))
    for kode in SAHAM:
        assert len(MENTAH[kode]) >= 550
    tanda = {k: dv.seri(k)._ds.tanda for k in [*SAHAM, "IHSG"]}
    assert tanda["AAAA"]["anomali_aktif"].any()
    assert not tanda["BBBB"]["anomali_aktif"].any()  # anomali_ok
    assert (tanda["BBBB"]["bar_hilang"] > CFG.data.validasi.maks_bar_hilang).any()
    assert (tanda["BBBB"]["volume_nol"] > 3).any()
    assert tanda["DDDD"]["split_belum_disesuaikan"].any()
    assert not tanda["CCCC"]["split_belum_disesuaikan"].any()
    assert (tanda["IHSG"]["bar_hilang"] > 0).any()
    ccc = _validasi(MENTAH["CCCC"], SPLIT["CCCC"], "CCCC")
    assert ccc.dibuang_nonbursa == [date(2024, 5, 1)]
    assert (ccc.mentah["tanda_bar"] == kualitas.TANDA_BAR_RUSAK).sum() == 1
    # Bar tanpa transaksi ada di mentah tetapi tidak di valid/indikator.
    for kode in SAHAM:
        m, v = dv.mentah(kode), dv.valid(kode)
        assert (m["volume"] == 0).any() and (v["volume"] > 0).all()
        assert dv.indikator(kode).index.equals(v.index)
        assert len(v) <= len(m) <= len(MENTAH[kode])  # tidak ada baris yang dibuat


# ---------------------------------------------------------------------------
# T-LA1a: potong vs penuh
# ---------------------------------------------------------------------------


def test_t_la1a_indikator_potong_sama_dengan_penuh():
    valid = {k: _validasi(MENTAH[k], SPLIT[k], k).valid for k in [*SAHAM, "IHSG"]}
    pasangan = _pasangan({k: v.index for k, v in valid.items()}, 240, SEED + 1)
    assert sum(len(x) for x in pasangan.values()) >= 200
    salah = []
    for kode, tanggal in pasangan.items():
        salah += [f"{kode} {s}" for s in periksa_kausal(_fungsi_indikator(kode), valid[kode], tanggal)]
    assert salah == []


def test_t_la1a_tanda_data_potong_sama_dengan_penuh():
    pasangan = {}
    for kode in [*SAHAM, "IHSG"]:
        akhir = MENTAH[kode].index.max().date()
        pasangan[kode] = KAL.indeks_hari_bursa(MENTAH[kode].index.min().date(), akhir)
    pilihan = _pasangan(pasangan, 240, SEED + 2)
    assert sum(len(x) for x in pilihan.values()) >= 200
    salah = []
    for kode, tanggal in pilihan.items():
        hasil = periksa_kausal(_fungsi_tanda(kode), MENTAH[kode], tanggal, pakai_akhir=True)
        salah += [f"{kode} {s}" for s in hasil]
    assert salah == []


def test_t_la1a_tanda_pada_tanggal_kritis():
    """Tanggal di sekitar lonjakan, split, bar hilang beruntun, dan volume 0 beruntun."""
    kasus = {
        "AAAA": [298, 299, 300, 301, 302, 300 + 119, 300 + 122, 300 + 126],
        "BBBB": [199, 200, 201, 399, 400, 403, 404, 449, 450, 455, 456, 520],
        "CCCC": [119, 120, 121, 349, 350, 351],
        "DDDD": [378, 379, 380, 381, 382, 599],
        "IHSG": [499, 500, 503, 504, 505],
    }
    salah = []
    for kode, posisi in kasus.items():
        tanggal = [HARI[p] for p in posisi]
        salah += [f"{kode} {s}" for s in periksa_kausal(_fungsi_tanda(kode), MENTAH[kode], tanggal, pakai_akhir=True)]
        if kode != "IHSG":
            valid = _validasi(MENTAH[kode], SPLIT[kode], kode).valid
            ada = [t for t in tanggal if t in valid.index]
            salah += [f"{kode} {s}" for s in periksa_kausal(_fungsi_indikator(kode), valid, ada)]
    assert salah == []


def test_t_la1a_lewat_dataview(pasar):
    """DataView dari data penuh = perhitungan ulang dari data ≤ t (seluruh potongan, bukan hanya baris t)."""
    pilihan = _pasangan({k: KAL.indeks_hari_bursa(date(2023, 3, 1), date(2025, 6, 30)) for k in [*SAHAM, "IHSG"]},
                        200, SEED + 3)
    for kode, tanggal in pilihan.items():
        for t in tanggal:
            dv = pasar.tampilan(t.date())
            mentah_t = MENTAH[kode].iloc[: int(MENTAH[kode].index.searchsorted(t, side="right"))]
            split_t = SPLIT[kode][SPLIT[kode].index <= t]
            seri_t = _validasi(mentah_t, split_t, kode)
            indikator_t = _fungsi_indikator(kode)(seri_t.valid)
            pd.testing.assert_frame_equal(dv.indikator(kode), indikator_t, check_exact=True, check_freq=False)
            pd.testing.assert_frame_equal(dv.valid(kode), seri_t.valid, check_exact=True, check_freq=False)
            pd.testing.assert_frame_equal(dv.mentah(kode), seri_t.mentah, check_exact=True, check_freq=False)
            tanda_t = kualitas.hitung_tanda_asof(seri_t, KAL, CFG, ANOMALI_OK, t.date())
            baris = dv.tanda(kode)
            if t in tanda_t.index:
                assert baris is not None
                for k in tanda_t.columns:
                    assert _nilai_sama(baris[k], tanda_t.loc[t, k]), (kode, t, k)
                harapan = kualitas.alasan_data_asof(tanda_t.loc[t], CFG, _indeks(kode))
            else:
                assert baris is None
                harapan = "DATA_BASI"
            assert dv.alasan_data(kode) == harapan, (kode, t)
            b = dv.baris(kode)
            if t in indikator_t.index:
                pd.testing.assert_series_equal(b, indikator_t.loc[t], check_exact=True)
            else:
                assert b is None


# ---------------------------------------------------------------------------
# T-LA3: uji jebakan — pemeriksa WAJIB menangkap look-ahead
# ---------------------------------------------------------------------------

KANARI = {
    "shift(-1) close": lambda df: df["close"].shift(-1),
    "rolling mean shift(-1)": lambda df: df["close"].rolling(5).mean().shift(-1),
    "normalisasi seluruh sampel": lambda df: (df["close"] - df["close"].mean()) / df["close"].std(),
    "rolling terbalik (jendela ke depan)": lambda df: df["close"].iloc[::-1].rolling(3).max().iloc[::-1],
    "maksimum seluruh seri": lambda df: df["close"] / df["close"].max(),
}


@pytest.mark.parametrize("nama", list(KANARI))
def test_t_la3_kanari_terdeteksi(nama):
    valid = _validasi(MENTAH["AAAA"], SPLIT["AAAA"], "AAAA").valid
    tanggal = list(valid.index[50:-50:7])
    salah = periksa_kausal(KANARI[nama], valid, tanggal)
    # Hampir setiap tanggal harus ketahuan, bukan hanya satu-dua.
    assert len(salah) >= 0.9 * len(tanggal), (nama, len(salah), len(tanggal))


def test_t_la3_pembanding_kausal_lolos():
    valid = _validasi(MENTAH["AAAA"], SPLIT["AAAA"], "AAAA").valid
    tanggal = list(valid.index[50:-50:7])
    for f in (
        lambda df: df["close"].shift(1),
        lambda df: df["close"].rolling(5).mean(),
        lambda df: df["close"].expanding().max(),
    ):
        assert periksa_kausal(f, valid, tanggal) == []


def test_t_la3_kanari_di_dalam_hitung_indikator_menggagalkan_t_la1(monkeypatch):
    """Menyuntikkan `shift(-1)` ke `indikator.sma` harus membuat uji T-LA1a indikator gagal."""
    valid = _validasi(MENTAH["AAAA"], SPLIT["AAAA"], "AAAA").valid
    pilihan = _pasangan({"AAAA": valid.index}, 60, SEED + 4)["AAAA"]
    assert periksa_kausal(_fungsi_indikator("AAAA"), valid, pilihan) == []
    asli = ind.sma
    monkeypatch.setattr(ind, "sma", lambda x, n: asli(x, n).shift(-1))
    salah = periksa_kausal(_fungsi_indikator("AAAA"), valid, pilihan)
    assert salah, "kanari shift(-1) di dalam hitung_indikator tidak terdeteksi"
    assert any(" sma_20:" in s for s in salah) and any(" rata_nilai:" in s for s in salah)


def test_t_la3_kanari_tanda_terdeteksi():
    """Tanda yang 'mengintip' bar berikutnya juga harus ketahuan oleh pemeriksa yang sama."""
    asli = _fungsi_tanda("BBBB")

    def kanari(mentah, akhir):
        t = asli(mentah, akhir)
        return t.assign(ada_bar_valid=t["ada_bar_valid"].shift(-1, fill_value=False))

    hari = KAL.indeks_hari_bursa(MENTAH["BBBB"].index.min().date(), MENTAH["BBBB"].index.max().date())
    tanggal = [t for t in hari[100:500:9]]
    salah = periksa_kausal(kanari, MENTAH["BBBB"], tanggal, pakai_akhir=True)
    assert salah


# ---------------------------------------------------------------------------
# DataView: penjaga look-ahead
# ---------------------------------------------------------------------------


def test_dataview_potongan_tidak_memuat_masa_depan(pasar):
    rng = np.random.default_rng(SEED + 5)
    kandidat = pd.date_range("2022-12-20", "2025-06-30", freq="D")  # termasuk akhir pekan & sebelum data
    for _ in range(60):
        as_of = kandidat[int(rng.integers(len(kandidat)))]
        dv = pasar.tampilan(as_of.date())
        assert dv.as_of == as_of.date()
        for kode in [*SAHAM, "IHSG"]:
            sumber = dv.seri(kode)._ds
            for nama, potong, penuh in (
                ("valid", dv.valid(kode), sumber.valid),
                ("mentah", dv.mentah(kode), sumber.mentah),
                ("indikator", dv.indikator(kode), sumber.indikator),
                ("split", dv.seri(kode).split(), sumber.split),
            ):
                if len(potong):
                    assert potong.index.max() <= as_of, (kode, nama)
                # tidak ada data ≤ as_of yang hilang
                assert len(potong) == int((penuh.index <= as_of).sum()), (kode, nama)
        ih = dv.ihsg_indikator()
        assert len(ih) == 0 or ih.index.max() <= as_of


@pytest.fixture
def pasar_kecil() -> DataPasar:
    """Satu saham buatan tangan + IHSG.

    mentah: 03-02, 03-03, 03-04, 03-05 (volume 0), 03-06, 03-09, 03-10, [03-11 tanpa bar], 03-12, 03-13
    split 1:5 pada 2026-03-09.
    """
    tgl = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10",
           "2026-03-12", "2026-03-13"]
    close = [10.0, 11.0, 11.0, 50.0, 13.5, 12.5, 10.0, 10.5, 11.5]
    high = [11.0, 12.0, 11.5, 50.0, 14.0, 13.0, 12.0, 11.0, 12.0]
    volume = [1000.0, 1000.0, 1000.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0]
    idx = pd.DatetimeIndex(tgl, name="tanggal")
    mentah = pd.DataFrame(
        {"open": close, "high": high, "low": [min(c, h) - 1 for c, h in zip(close, high)], "close": close,
         "volume": volume, "sumber": "yfinance"},
        index=idx,
    )
    mentah["tanda_bar"] = np.where(mentah["volume"] == 0, "TANPA_TRANSAKSI", "")
    valid = mentah[mentah["volume"] > 0].drop(columns="tanda_bar")
    indikator = pd.DataFrame({"close": valid["close"], "sma_3": ind.sma(valid["close"], 3)})
    hari = KAL.indeks_hari_bursa(date(2026, 3, 2), date(2026, 3, 13))
    ada = hari.isin(valid.index)
    tanda = pd.DataFrame(
        {
            "ada_bar_valid": ada,
            "jumlah_bar_valid": np.cumsum(ada).astype("int64"),
            "bar_hilang": np.zeros(len(hari), dtype="int64"),
            "volume_nol": np.zeros(len(hari), dtype="int64"),
            "anomali_aktif": np.zeros(len(hari), dtype=bool),
            "anomali_terakhir": pd.Series(pd.NaT, index=hari, dtype="datetime64[ns]").to_numpy(),
            "split_belum_disesuaikan": np.zeros(len(hari), dtype=bool),
        },
        index=hari,
    )
    split = pd.Series([5.0], index=pd.DatetimeIndex(["2026-03-09"], name="tanggal"), name="split")
    uji = DataSaham("UJI", False, "OK", None, "", "yfinance", mentah, valid, split, indikator, tanda)
    ih_valid = valid.assign(volume=np.nan)
    ihsg = DataSaham(
        "IHSG", True, "OK", None, "", "yfinance", ih_valid, ih_valid,
        pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="tanggal"), name="split"),
        pd.DataFrame({"close": ih_valid["close"]}), tanda,
    )
    return DataPasar({"UJI": uji}, ihsg, KAL, _cfg({"universe.min_bar_riwayat": 3}))


def D(s: str) -> date:
    return date.fromisoformat(s)


class TestDataView:
    def test_akses_masa_depan_ditolak(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        with pytest.raises(LookAheadError):
            dv.baris("UJI", D("2026-03-11"))
        with pytest.raises(LookAheadError):
            dv.baris("UJI", D("2026-03-12"))
        with pytest.raises(LookAheadError):
            dv.sebelum("UJI", "close", -1)
        with pytest.raises(LookAheadError):
            dv.faktor_split("UJI", D("2026-03-11"))
        with pytest.raises(LookAheadError):
            dv.nominal("UJI", 100.0, D("2026-03-13"))
        with pytest.raises(LookAheadError):
            dv.hhv_sejak("UJI", D("2026-03-03"), D("2026-03-12"))
        with pytest.raises(LookAheadError):
            dv.ihsg_baris(D("2026-03-12"))
        # Pesan berbahasa Indonesia menyebut kedua tanggal.
        with pytest.raises(LookAheadError, match="2026-03-12.*as_of 2026-03-10"):
            dv.baris("UJI", "2026-03-12")

    def test_potongan(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        assert list(dv.valid("UJI").index.strftime("%m-%d")) == ["03-02", "03-03", "03-04", "03-06", "03-09", "03-10"]
        assert "2026-03-05" in dv.mentah("UJI").index.strftime("%Y-%m-%d")  # bar volume 0 tetap di mentah
        assert dv.indikator("UJI").index.max() == pd.Timestamp("2026-03-10")
        assert list(dv.ihsg_indikator().index.strftime("%m-%d"))[-1] == "03-10"
        assert dv.ihsg_baris()["close"] == 10.0

    def test_baris(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        assert dv.baris("UJI")["close"] == 10.0
        assert dv.baris("UJI", D("2026-03-06"))["sma_3"] == pytest.approx((11 + 11 + 13.5) / 3)
        assert dv.baris("UJI", D("2026-03-05")) is None  # bar tanpa transaksi = tidak ada bar valid
        assert dv.baris("UJI", D("2026-03-07")) is None  # Sabtu
        assert pasar_kecil.tampilan(D("2026-03-11")).baris("UJI") is None  # hari bursa tanpa bar

    def test_sebelum_x_t_minus_k(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        # bar valid ≤ 03-10: 02(10) 03(11) 04(11) 06(13.5) 09(12.5) 10(10); bar volume 0 (03-05) dilewati
        assert dv.sebelum("UJI", "close", 0) == 10.0
        assert dv.sebelum("UJI", "close", 1) == 12.5
        assert dv.sebelum("UJI", "close", 2) == 13.5
        assert dv.sebelum("UJI", "close", 3) == 11.0
        assert dv.sebelum("UJI", "close", 5) == 10.0
        assert math.isnan(dv.sebelum("UJI", "close", 6))
        assert math.isnan(dv.sebelum("UJI", "sma_3", 4))  # sma_3 bar valid ke-2: NaN (belum cukup bar)
        # as_of tanpa bar valid (03-11): k=0 → NaN; k=1 → bar valid terakhir sebelum as_of (03-10)
        dv11 = pasar_kecil.tampilan(D("2026-03-11"))
        assert math.isnan(dv11.sebelum("UJI", "close", 0))
        assert dv11.sebelum("UJI", "close", 1) == 10.0
        assert dv11.sebelum("UJI", "close", 2) == 12.5
        with pytest.raises(KeyError):
            dv.sebelum("UJI", "kolom_tidak_ada", 0)
        with pytest.raises(TypeError):
            dv.sebelum("UJI", "close", 1.0)

    def test_sebelum_data_awal(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-02-27"))  # sebelum bar pertama
        assert len(dv.valid("UJI")) == 0 and len(dv.mentah("UJI")) == 0
        assert math.isnan(dv.sebelum("UJI", "close", 0)) and math.isnan(dv.sebelum("UJI", "close", 1))
        assert dv.baris("UJI") is None
        assert dv.tanda("UJI") is None
        assert dv.alasan_data("UJI") == "DATA_BASI"

    def test_tanda_dan_alasan_data(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        t = dv.tanda("UJI")
        assert t.name == pd.Timestamp("2026-03-10") and bool(t["ada_bar_valid"]) and t["jumlah_bar_valid"] == 6
        assert dv.alasan_data("UJI") is None  # min_bar_riwayat 3 di config pasar_kecil
        # Hari bursa tanpa bar valid → tanda ada, alasan DATA_BASI (dari kualitas.alasan_data_asof)
        dv11 = pasar_kecil.tampilan(D("2026-03-11"))
        assert not bool(dv11.tanda("UJI")["ada_bar_valid"])
        assert dv11.alasan_data("UJI") == "DATA_BASI"
        dv05 = pasar_kecil.tampilan(D("2026-03-05"))
        assert dv05.alasan_data("UJI") == "DATA_BASI"  # bar tanpa transaksi
        # Akhir pekan / sesudah data: tidak ada baris tanda tepat pada as_of → DATA_BASI
        assert pasar_kecil.tampilan(D("2026-03-07")).tanda("UJI") is None
        assert pasar_kecil.tampilan(D("2026-03-07")).alasan_data("UJI") == "DATA_BASI"
        assert pasar_kecil.tampilan(D("2026-03-16")).alasan_data("UJI") == "DATA_BASI"
        # Riwayat pendek: 2 bar valid pada 03-03 < min_bar_riwayat 3
        assert pasar_kecil.tampilan(D("2026-03-03")).alasan_data("UJI") == "RIWAYAT_PENDEK"

    def test_alasan_data_meneruskan_baris_as_of(self, pasar_kecil, monkeypatch):
        tercatat = []

        def palsu(baris, cfg, adalah_indeks, csv_pengganti_penuh=False):
            tercatat.append((baris.name, adalah_indeks, csv_pengganti_penuh))
            return "BAR_HILANG"

        monkeypatch.setattr(kualitas, "alasan_data_asof", palsu)
        ds = pasar_kecil._saham["UJI"]
        ds.csv_pengganti_penuh = True
        dv = pasar_kecil.tampilan(D("2026-03-09"))
        assert dv.alasan_data("UJI") == "BAR_HILANG"
        assert dv.seri("IHSG").alasan_data() == "BAR_HILANG"
        assert tercatat == [(pd.Timestamp("2026-03-09"), False, True), (pd.Timestamp("2026-03-09"), True, False)]

    def test_alasan_seri_mengalahkan_as_of(self, pasar_kecil):
        pasar_kecil._saham["UJI"].alasan_seri = "GAGAL_AMBIL"
        assert pasar_kecil.tampilan(D("2026-03-10")).alasan_data("UJI") == "GAGAL_AMBIL"
        assert pasar_kecil.tampilan(D("2026-02-27")).alasan_data("UJI") == "GAGAL_AMBIL"

    def test_faktor_split_dan_nominal(self, pasar_kecil):
        # F(d) = Π rasio split bertanggal > d. Split 5.0 pada 03-09.
        dv06 = pasar_kecil.tampilan(D("2026-03-06"))
        assert dv06.faktor_split("UJI") == 5.0  # sah: split sesudah as_of hanya membatalkan penyesuaian
        assert dv06.faktor_split("UJI", D("2026-03-02")) == 5.0
        assert dv06.nominal("UJI", 13.5) == pytest.approx(67.5)
        assert dv06.nominal("UJI", Decimal("13.5")) == Decimal("67.50")
        assert len(dv06.seri("UJI").split()) == 0  # peristiwa split sendiri belum terjadi pada as_of
        dv10 = pasar_kecil.tampilan(D("2026-03-10"))
        assert dv10.faktor_split("UJI", D("2026-03-09")) == 1.0  # split pada d sendiri tidak ikut
        assert dv10.faktor_split("UJI", D("2026-03-06")) == 5.0
        assert dv10.nominal("UJI", 10.0) == 10.0
        assert list(dv10.seri("UJI").split().index.strftime("%Y-%m-%d")) == ["2026-03-09"]

    def test_hhv_sejak(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        # high valid: 03-03=12, 03-04=11.5, 03-06=14, 03-09=13, 03-10=12 (03-05 high 50 = bar volume 0)
        assert dv.hhv_sejak("UJI", D("2026-03-03")) == 14.0
        assert dv.hhv_sejak("UJI", D("2026-03-09")) == 13.0
        assert dv.hhv_sejak("UJI", D("2026-03-04"), D("2026-03-05")) == 11.5
        assert math.isnan(dv.hhv_sejak("UJI", D("2026-03-11")))

    def test_daftar_kode_dan_ihsg(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        assert dv.kode() == ["UJI"]
        assert dv.ada("UJI") and dv.ada("IHSG") and not dv.ada("ZZZZ")
        with pytest.raises(KeyError):
            dv.valid("ZZZZ")
        assert dv.ihsg().adalah_indeks and dv.ihsg().kode == "IHSG"
        tanpa = DataPasar(pasar_kecil._saham, None, KAL, pasar_kecil.cfg).tampilan(D("2026-03-10"))
        assert tanpa.ihsg() is None and tanpa.ihsg_baris() is None and len(tanpa.ihsg_indikator()) == 0
        assert not tanpa.ada("IHSG")

    def test_as_of_baca_saja_dan_format_masukan(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        with pytest.raises(AttributeError):
            dv.as_of = D("2026-03-13")
        assert pasar_kecil.tampilan("2026-03-10").as_of == D("2026-03-10")
        assert pasar_kecil.tampilan(pd.Timestamp("2026-03-10")).as_of == D("2026-03-10")
        assert isinstance(dv, DataView) and dv.kalender is KAL

    def test_mengubah_potongan_tidak_mengubah_sumber(self, pasar_kecil):
        dv = pasar_kecil.tampilan(D("2026-03-10"))
        v = dv.valid("UJI")
        v.loc[v.index[0], "close"] = -1.0
        assert pasar_kecil._saham["UJI"].valid["close"].iloc[0] == 10.0
        assert dv.valid("UJI")["close"].iloc[0] == 10.0

    def test_index_tidak_urut_ditolak(self, pasar_kecil):
        ds = pasar_kecil._saham["UJI"]
        rusak = DataSaham(**{**ds.__dict__, "valid": ds.valid.iloc[::-1]})
        with pytest.raises(ValueError, match="urut naik"):
            DataPasar({"UJI": rusak}, None, KAL, pasar_kecil.cfg)


def test_dataview_cepat_untuk_banyak_tanggal(pasar):
    """Pemotongan per tanggal tidak menyalin data: ±600 tampilan × 5 seri tetap ringan."""
    hari = KAL.indeks_hari_bursa(date(2023, 1, 2), date(2025, 5, 30))
    total = 0
    for t in hari:
        dv = pasar.tampilan(t.date())
        for kode in SAHAM:
            b = dv.baris(kode)
            total += 0 if b is None else 1
            dv.alasan_data(kode)
    assert total > 0.9 * len(hari) * len(SAHAM) * 0.9
