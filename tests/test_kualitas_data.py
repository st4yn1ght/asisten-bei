"""Uji kualitas data (SPEC §8.5, §8.4 pemeriksaan split, §11 syarat 3 & 5; T-DQ §22.1).

Semua data dibangun langsung di uji (fixture kecil, deterministik, tanpa jaringan). Kalender memakai
libur FIKTIF – KHUSUS UJI. Config = config.yaml rilis (hanya dibaca) dengan beberapa parameter
diperkecil agar hitung tangan pendek.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from asisten_bei import DISCLAIMER_CSV
from asisten_bei.config import Config, validasi_dict
from asisten_bei.data import kualitas as kq
from asisten_bei.data.kualitas import (
    ALASAN_DIKELUARKAN,
    KOLOM_KUALITAS,
    KOLOM_TANDA,
    TANDA_BAR,
    TANDA_BAR_RUSAK,
    TANDA_HARI_NONBURSA,
    TANDA_TANPA_TRANSAKSI,
    BarisKualitas,
    alasan_data_asof,
    baris_kualitas_dari_tanda,
    deteksi_anomali,
    hitung_tanda_asof,
    path_berkas_kualitas,
    tulis_berkas_kualitas,
    validasi_bar,
    validasi_struktur,
)
from asisten_bei.kalender import Kalender

AKAR_PROYEK = Path(__file__).resolve().parents[1]
LIBUR_UJI = {date(2026, 8, 17): "FIKTIF – KHUSUS UJI"}

# Parameter kecil untuk hitung tangan.
UBAH_KECIL = {
    "data.validasi.jendela_kelengkapan_bar": 5,
    "data.validasi.maks_bar_hilang": 1,
    "data.validasi.lonjakan_mencurigakan_pct": 40,
    "likuiditas.periode_hari": 5,
    "likuiditas.maks_hari_volume_nol": 1,
    "universe.min_bar_riwayat": 4,
    "kondisi_pasar.sma_panjang": 3,
}


# ---------------------------------------------------------------------------
# Pembantu
# ---------------------------------------------------------------------------


def buat_cfg(ubah: dict | None = None) -> Config:
    with open(AKAR_PROYEK / "config.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for kunci, nilai in (ubah or {}).items():
        simpul = data
        *jalur, akhir = kunci.split(".")
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = nilai
    return validasi_dict(data)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return buat_cfg(UBAH_KECIL)


@pytest.fixture
def kal() -> Kalender:
    return Kalender(LIBUR_UJI)


def hari(kal: Kalender, mulai: date, n: int) -> list[date]:
    """n hari bursa pertama mulai `mulai` (inklusif)."""
    hasil = kal.daftar_hari_bursa(mulai, mulai + timedelta(days=n * 2 + 14))
    assert len(hasil) >= n
    return hasil[:n]


def buat_bar(tanggal, close, volume=1_000_000.0, sumber=None) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t in tanggal], name="tanggal").as_unit("ns")
    c = np.asarray(close, dtype="float64")
    vol = np.broadcast_to(np.asarray(volume, dtype="float64"), c.shape).copy()
    df = pd.DataFrame(
        {"open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": vol}, index=idx
    )
    if sumber is not None:
        df["sumber"] = sumber
    return df


def proses(bar, cfg, kal, kode="UJI", indeks=False, split=None, ok=(), akhir=None):
    st = validasi_struktur(bar, adalah_indeks=indeks)
    assert st.ok, st.pesan
    seri = validasi_bar(st.bar, split, kode, indeks, kal)
    if akhir is None:
        akhir = seri.mentah.index[-1].date()
    return seri, hitung_tanda_asof(seri, kal, cfg, set(ok), akhir)


def ts(d) -> pd.Timestamp:
    return pd.Timestamp(d)


# ---------------------------------------------------------------------------
# Enum (§4.3)
# ---------------------------------------------------------------------------


def _enum_spec(nama: str) -> list[str]:
    teks = (AKAR_PROYEK / "SPEC.md").read_text(encoding="utf-8")
    baris = next(b for b in teks.splitlines() if b.startswith(f"| `{nama}`"))
    sel = baris.split("|")[2]
    return re.findall(r"`([A-Z0-9_]+)`", sel)


def test_enum_sesuai_spec():
    assert list(TANDA_BAR) == _enum_spec("tanda_bar")
    assert list(ALASAN_DIKELUARKAN) == _enum_spec("alasan_dikeluarkan")
    assert set(kq.TEKS_ALASAN) == set(ALASAN_DIKELUARKAN)
    assert "kemungkinan suspensi — cek idx.co.id" in kq.TEKS_ALASAN["KEMUNGKINAN_SUSPENSI"]


# ---------------------------------------------------------------------------
# Struktur per seri
# ---------------------------------------------------------------------------


def test_struktur_none_gagal():
    h = validasi_struktur(None)
    assert not h.ok and h.bar is None and "tidak ada data" in h.pesan


def test_struktur_kolom_wajib_hilang():
    bar = buat_bar(["2026-10-12", "2026-10-13"], [100, 101]).drop(columns=["close"])
    h = validasi_struktur(bar)
    assert not h.ok and "close" in h.pesan


def test_struktur_ihsg_tanpa_volume_lolos_saham_tidak():
    bar = buat_bar(["2026-10-12", "2026-10-13"], [7000, 7010]).drop(columns=["volume"])
    h = validasi_struktur(bar, adalah_indeks=True)
    assert h.ok, h.pesan
    assert h.bar["volume"].isna().all() and len(h.bar) == 2
    h2 = validasi_struktur(bar, adalah_indeks=False)
    assert not h2.ok and "volume" in h2.pesan


def test_struktur_tipe_salah_gagal():
    bar = buat_bar(["2026-10-12", "2026-10-13"], [100, 101])
    teks = bar.astype({"close": object})
    teks.loc[ts("2026-10-13"), "close"] = "1,5"
    h = validasi_struktur(teks)
    assert not h.ok and "tipe kolom close" in h.pesan

    boolean = bar.copy()
    boolean["volume"] = [True, False]
    assert not validasi_struktur(boolean).ok

    waktu_kol = bar.copy()
    waktu_kol["open"] = pd.to_datetime(["2026-01-01", "2026-01-02"])
    assert not validasi_struktur(waktu_kol).ok


def test_struktur_angka_teks_dikonversi():
    bar = buat_bar(["2026-10-12", "2026-10-13"], [100, 101]).astype({"close": object})
    bar["close"] = ["100", "101.5"]
    h = validasi_struktur(bar)
    assert h.ok, h.pesan
    assert h.bar["close"].dtype == "float64"
    assert h.bar["close"].tolist() == [100.0, 101.5]


@pytest.mark.parametrize(
    "index, potongan",
    [
        (pd.RangeIndex(2), "index bukan tanggal"),
        (pd.DatetimeIndex(["2026-10-12", "2026-10-13"]).tz_localize("Asia/Jakarta"), "ber-zona waktu"),
        (pd.DatetimeIndex(["2026-10-12 00:00", "2026-10-13 09:00"]), "memuat jam"),
        (pd.DatetimeIndex(["2026-10-12", None]), "kosong"),
    ],
)
def test_struktur_index_tidak_dikenali(index, potongan):
    bar = buat_bar(["2026-10-12", "2026-10-13"], [100, 101])
    bar.index = index
    h = validasi_struktur(bar)
    assert not h.ok
    assert "tanggal/zona waktu tidak dikenali" in h.pesan and potongan in h.pesan


def test_struktur_bar_akhir_pekan_gagal():
    bar = buat_bar(["2026-10-09", "2026-10-10", "2026-10-12"], [100, 101, 102])  # 10 Okt = Sabtu
    h = validasi_struktur(bar)
    assert not h.ok
    assert "akhir pekan" in h.pesan and "2026-10-10" in h.pesan


def test_duplikat_terakhir_identik_disimpan_satu():
    bar = buat_bar(["2026-10-12", "2026-10-13", "2026-10-13"], [100, 101, 101])
    h = validasi_struktur(bar)
    assert h.ok and len(h.bar) == 2 and h.catatan == []
    assert h.bar.loc[ts("2026-10-13"), "close"] == 101


def test_duplikat_terakhir_identik_indeks_volume_nan():
    bar = buat_bar(["2026-10-12", "2026-10-13", "2026-10-13"], [7000, 7001, 7001]).drop(columns=["volume"])
    h = validasi_struktur(bar, adalah_indeks=True)
    assert h.ok and len(h.bar) == 2


def test_duplikat_terakhir_berbeda_dianggap_belum_final():
    bar = buat_bar(["2026-10-12", "2026-10-13", "2026-10-13"], [100, 101, 102])
    h = validasi_struktur(bar)
    assert h.ok and len(h.bar) == 1
    assert list(h.bar.index) == [ts("2026-10-12")]
    assert any("2026-10-13" in c and "belum final" in c for c in h.catatan)


def test_duplikat_di_tengah_gagal():
    bar = buat_bar(["2026-10-12", "2026-10-12", "2026-10-13"], [100, 100, 101])
    h = validasi_struktur(bar)
    assert not h.ok and "tanggal duplikat" in h.pesan and "2026-10-12" in h.pesan


def test_struktur_mengurutkan_dan_mempertahankan_kolom_tambahan():
    bar = buat_bar(["2026-10-13", "2026-10-12"], [101, 100], sumber=["csv_manual", "yfinance"])
    bar["split"] = [0.0, 0.0]
    bar.index = bar.index.as_unit("us")
    h = validasi_struktur(bar)
    assert h.ok
    assert list(h.bar.index) == [ts("2026-10-12"), ts("2026-10-13")]
    assert h.bar.index.name == "tanggal" and str(h.bar.index.dtype) == "datetime64[ns]"
    assert h.bar["sumber"].tolist() == ["yfinance", "csv_manual"]
    assert "split" in h.bar.columns


# ---------------------------------------------------------------------------
# Per bar
# ---------------------------------------------------------------------------

_HARI_RUSAK = ["2026-10-12", "2026-10-13", "2026-10-14"]


@pytest.mark.parametrize(
    "kolom, nilai",
    [
        ("close", np.nan),
        ("open", np.nan),
        ("high", np.nan),
        ("low", np.nan),
        ("high", 50.0),  # high < low (low = 98)
        ("open", 200.0),  # open > high
        ("close", 10.0),  # close < low
        ("low", 0.0),  # harga ≤ 0
        ("close", -1.0),
        ("high", np.inf),
        ("volume", np.nan),
        ("volume", -5.0),
    ],
)
def test_bar_rusak_setiap_jenis(kal, kolom, nilai):
    bar = buat_bar(_HARI_RUSAK, [100, 100, 100])
    bar.loc[ts("2026-10-13"), kolom] = nilai
    seri = validasi_bar(bar, None, "UJI", False, kal)
    assert seri.mentah["tanda_bar"].tolist() == ["", TANDA_BAR_RUSAK, ""]
    assert ts("2026-10-13") not in seri.valid.index
    assert len(seri.valid) == 2 and len(seri.mentah) == 3
    # nilai mentah tidak diubah/diisi
    asli = bar.loc[ts("2026-10-13"), kolom]
    tersimpan = seri.mentah.loc[ts("2026-10-13"), kolom]
    assert (np.isnan(asli) and np.isnan(tersimpan)) or asli == tersimpan
    assert any(TANDA_BAR_RUSAK in c for c in seri.catatan)


def test_volume_nol_tanpa_transaksi(kal):
    bar = buat_bar(_HARI_RUSAK, [100, 100, 100], volume=[1e6, 0, 1e6])
    seri = validasi_bar(bar, None, "UJI", False, kal)
    assert seri.mentah["tanda_bar"].tolist() == ["", TANDA_TANPA_TRANSAKSI, ""]
    assert list(seri.valid.index) == [ts("2026-10-12"), ts("2026-10-14")]


def test_bar_rusak_dan_volume_nol_bertanda_rusak(kal):
    bar = buat_bar(_HARI_RUSAK, [100, 100, 100], volume=[1e6, 0, 1e6])
    bar.loc[ts("2026-10-13"), "high"] = 1.0
    seri = validasi_bar(bar, None, "UJI", False, kal)
    assert seri.mentah["tanda_bar"].tolist() == ["", TANDA_BAR_RUSAK, ""]


def test_indeks_volume_tidak_diperiksa_ohlc_tetap(kal):
    bar = buat_bar(["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"], [7000, 7010, 7020, 7030])
    bar["volume"] = [np.nan, 0.0, -1.0, np.nan]
    bar.loc[ts("2026-10-15"), "low"] = 8000.0  # low > high → rusak walau indeks
    seri = validasi_bar(bar, None, "IHSG", True, kal)
    assert seri.mentah["tanda_bar"].tolist() == ["", "", "", TANDA_BAR_RUSAK]
    assert len(seri.valid) == 3


def test_bar_hari_nonbursa_dibuang_sebelum_apa_pun(kal):
    # 2026-08-17 libur (FIKTIF – KHUSUS UJI); bar bervolume 0 di hari itu dibuang, bukan TANPA_TRANSAKSI.
    tgl = ["2026-08-13", "2026-08-14", "2026-08-17", "2026-08-18"]
    bar = buat_bar(tgl, [100, 101, 101, 102], volume=[1e6, 1e6, 0, 1e6])
    seri = validasi_bar(bar, None, "BR01", False, kal)
    assert seri.dibuang_nonbursa == [date(2026, 8, 17)]
    assert ts("2026-08-17") not in seri.mentah.index and ts("2026-08-17") not in seri.valid.index
    assert TANDA_TANPA_TRANSAKSI not in seri.mentah["tanda_bar"].tolist()
    assert any(TANDA_HARI_NONBURSA in c and "2026-08-17" in c for c in seri.catatan)


def test_bar_nonbursa_kalender_turunan(kal):
    # Tahun tanpa entri libur (2022) → kalender turunan; 2022-03-03 (Kamis) bukan hari bursa turunan.
    turunan = {date(2022, 3, 1), date(2022, 3, 2), date(2022, 3, 4)}
    kal_t = Kalender(LIBUR_UJI, turunan, (date(2022, 3, 1), date(2022, 3, 4)))
    bar = buat_bar(["2022-03-01", "2022-03-02", "2022-03-03", "2022-03-04"], [10, 11, 12, 13])
    seri = validasi_bar(bar, None, "BR02", False, kal_t)
    assert seri.dibuang_nonbursa == [date(2022, 3, 3)]
    assert len(seri.mentah) == 3


def test_tidak_ada_baris_dibuat_atau_diisi(kal):
    tgl = hari(kal, date(2026, 8, 3), 15)
    close = np.linspace(100, 115, len(tgl))
    bar = buat_bar(tgl, close, volume=1e6)
    bar.loc[ts(tgl[3]), "close"] = np.nan
    bar.loc[ts(tgl[5]), "volume"] = 0.0
    bar.loc[ts(tgl[8]), "high"] = 1.0
    bar = bar.drop(index=[ts(tgl[10]), ts(tgl[11])])  # bar hilang
    libur = buat_bar(["2026-08-17"], [107.0], volume=0.0)
    masukan = pd.concat([bar, libur]).sort_index()
    st = validasi_struktur(masukan)
    assert st.ok
    seri = validasi_bar(st.bar, None, "UJI", False, kal)
    assert len(seri.valid) <= len(seri.mentah) <= len(masukan)
    assert set(seri.valid.index) <= set(seri.mentah.index) <= set(masukan.index)
    assert len(seri.valid) == 15 - 2 - 3 and len(seri.mentah) == 13
    # nilai setiap bar yang tersisa identik dengan masukan (NaN tetap NaN, tidak diisi)
    pd.testing.assert_frame_equal(
        seri.mentah.loc[:, ["open", "high", "low", "close", "volume"]],
        masukan.loc[seri.mentah.index, ["open", "high", "low", "close", "volume"]],
    )
    assert seri.mentah["close"].isna().sum() == 1
    assert not seri.valid[["open", "high", "low", "close", "volume"]].isna().any().any()
    # tanda hanya membaca, tidak menambah bar
    tanda = hitung_tanda_asof(seri, kal, buat_cfg(UBAH_KECIL), set(), tgl[-1])
    assert int(tanda["ada_bar_valid"].sum()) == len(seri.valid)


def test_split_dari_kolom_cache_dan_parameter(kal):
    tgl = hari(kal, date(2026, 8, 3), 4)
    bar = buat_bar(tgl, [100, 100, 100, 100])
    bar["split"] = [0.0, 0.0, 5.0, 0.0]
    seri = validasi_bar(bar, None, "UJISPLIT", False, kal)
    assert seri.split.to_dict() == {ts(tgl[2]): 5.0}
    assert "split" not in seri.mentah.columns
    # parameter eksplisit diutamakan; 0, NaN, dan 1.0 dibuang
    param = pd.Series([0.0, np.nan, 1.0, 2.0], index=pd.DatetimeIndex([ts(t) for t in tgl]))
    seri2 = validasi_bar(bar, param, "UJISPLIT", False, kal)
    assert seri2.split.to_dict() == {ts(tgl[3]): 2.0}
    assert len(validasi_bar(bar.drop(columns=["split"]), None, "X", False, kal).split) == 0


def test_sumber_dipertahankan_atau_kosong(kal):
    tgl = hari(kal, date(2026, 8, 3), 3)
    bar = buat_bar(tgl, [100, 101, 102], volume=[1e6, 0, 1e6], sumber=["yfinance", "yfinance", "csv_manual"])
    seri = validasi_bar(bar, None, "UJI", False, kal)
    assert seri.mentah["sumber"].tolist() == ["yfinance", "yfinance", "csv_manual"]
    assert seri.valid["sumber"].tolist() == ["yfinance", "csv_manual"]
    assert list(seri.valid.columns) == ["open", "high", "low", "close", "volume", "sumber"]
    assert list(seri.mentah.columns) == ["open", "high", "low", "close", "volume", "sumber", "tanda_bar"]
    tanpa = validasi_bar(buat_bar(tgl, [1, 2, 3]), None, "UJI", False, kal)
    assert tanpa.mentah["sumber"].tolist() == ["", "", ""]


def test_cache_hari_bursa_konsisten_saat_rentang_diperluas(kal):
    a = kq._hari_bursa_antara(kal, date(2026, 8, 10), date(2026, 8, 20))
    b = kq._hari_bursa_antara(kal, date(2026, 7, 1), date(2026, 9, 30))
    c = kq._hari_bursa_antara(kal, date(2026, 8, 10), date(2026, 8, 20))
    assert list(a) == list(c)
    assert [t.date() for t in b] == kal.daftar_hari_bursa(date(2026, 7, 1), date(2026, 9, 30))
    assert ts("2026-08-17") not in a
    assert len(kq._hari_bursa_antara(kal, date(2026, 8, 20), date(2026, 8, 10))) == 0


# ---------------------------------------------------------------------------
# Anomali
# ---------------------------------------------------------------------------


def test_anomali_ambang_tepat_dan_lewat(kal):
    tgl = hari(kal, date(2026, 8, 3), 6)
    # +40% tepat dan −40% tepat tidak anomali (> ambang); +40,01% dan −40,06% anomali; +60% anomali.
    close = [100.0, 140.0, 84.0, 117.61, 70.5, 112.8]
    valid = buat_bar(tgl, close)
    assert deteksi_anomali(valid, 40, set()) == [tgl[3], tgl[4], tgl[5]]
    from decimal import Decimal

    assert deteksi_anomali(valid, Decimal("40"), set()) == [tgl[3], tgl[4], tgl[5]]
    assert deteksi_anomali(valid, 70, set()) == []


def test_anomali_ok_dikecualikan(kal):
    tgl = hari(kal, date(2026, 8, 3), 4)
    valid = buat_bar(tgl, [100, 160, 160, 60])
    assert deteksi_anomali(valid, 40, set()) == [tgl[1], tgl[3]]
    assert deteksi_anomali(valid, 40, {tgl[1]}) == [tgl[3]]
    assert deteksi_anomali(valid.iloc[:1], 40, set()) == []
    assert deteksi_anomali(valid.iloc[:0], 40, set()) == []


def test_anomali_antar_bar_valid_berurutan(kal):
    tgl = hari(kal, date(2026, 8, 3), 6)
    # Bar tanpa transaksi (200, 50) bukan bar valid: lonjakan diukur terhadap bar valid sebelumnya.
    bar = buat_bar(tgl, [100, 200, 101, 50, 160, 161], volume=[1e6, 0, 1e6, 0, 1e6, 1e6])
    seri = validasi_bar(bar, None, "UJI", False, kal)
    assert deteksi_anomali(seri.valid, 40, set()) == [tgl[4]]  # 101 → 160 (+58%)


# ---------------------------------------------------------------------------
# Tanda as-of: hitung tangan
# ---------------------------------------------------------------------------


def _bar_kelengkapan(kal):
    d = hari(kal, date(2026, 8, 3), 12)  # 3–14 Agt, 18–19 Agt (17 Agt libur fiktif)
    bar = buat_bar(d, [100.0] * 12)
    bar.loc[ts(d[3]), "close"] = np.nan  # rusak
    bar.loc[ts(d[4]), "volume"] = 0.0  # tanpa transaksi
    bar = bar.drop(index=[ts(d[2]), ts(d[10])])  # tidak ada bar
    libur = buat_bar(["2026-08-17"], [100.0], volume=0.0)  # bar hari libur bervolume 0
    return d, pd.concat([bar, libur]).sort_index()


def test_tanda_kelengkapan_dan_volume_nol_hitung_tangan(kal):
    cfg = buat_cfg(UBAH_KECIL)  # jendela 5, periode likuiditas 5
    d, bar = _bar_kelengkapan(kal)
    seri, t = proses(bar, cfg, kal, akhir=d[-1])
    assert seri.dibuang_nonbursa == [date(2026, 8, 17)]
    assert list(t.columns) == list(KOLOM_TANDA)
    assert [x.date() for x in t.index] == d
    assert t["ada_bar_valid"].tolist() == [True, True, False, False, False, True, True, True, True, True, False, True]
    assert t["jumlah_bar_valid"].tolist() == [1, 2, 2, 2, 2, 3, 4, 5, 6, 7, 7, 8]
    assert t["bar_hilang"].tolist() == [0, 0, 1, 2, 3, 3, 3, 2, 1, 0, 1, 1]
    # hanya bar d[4] bervolume 0 yang dihitung; bar libur 17 Agt dibuang & tidak dihitung
    assert t["volume_nol"].tolist() == [0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 0]
    assert t["anomali_aktif"].sum() == 0 and t["anomali_terakhir"].isna().all()
    assert not t["split_belum_disesuaikan"].any()
    assert str(t.index.dtype) == "datetime64[ns]" and t.index.name == "tanggal"


def test_tanda_berlanjut_sampai_akhir_tanpa_bar(kal):
    cfg = buat_cfg(UBAH_KECIL)
    d, bar = _bar_kelengkapan(kal)
    _, t = proses(bar, cfg, kal, akhir=date(2026, 8, 21))
    assert [x.date() for x in t.index[-2:]] == [date(2026, 8, 20), date(2026, 8, 21)]
    assert t["ada_bar_valid"].tolist()[-2:] == [False, False]
    assert t["jumlah_bar_valid"].tolist()[-2:] == [8, 8]
    assert t["bar_hilang"].tolist()[-2:] == [2, 3]
    assert alasan_data_asof(t.iloc[-1], cfg, False) == "DATA_BASI"


def test_tanda_mulai_dari_bar_mentah_pertama(kal):
    cfg = buat_cfg(UBAH_KECIL)
    # Bar pertama di hari libur → dibuang; index tanda mulai dari bar mentah (hari bursa) pertama.
    bar = buat_bar(["2026-08-17", "2026-08-19", "2026-08-20"], [100, 100, 100])
    seri, t = proses(bar, cfg, kal, akhir=date(2026, 8, 20))
    assert [x.date() for x in t.index] == [date(2026, 8, 19), date(2026, 8, 20)]
    assert t["bar_hilang"].tolist() == [0, 0]  # hari sebelum bar pertama tidak dihitung hilang


def test_tanda_kosong(kal, cfg):
    kosong = buat_bar([], [])
    seri = validasi_bar(kosong, None, "UJI", False, kal)
    t = hitung_tanda_asof(seri, kal, cfg, set(), date(2026, 10, 15))
    assert len(t) == 0 and list(t.columns) == list(KOLOM_TANDA)
    seri2 = validasi_bar(buat_bar(["2026-10-14"], [100]), None, "UJI", False, kal)
    t2 = hitung_tanda_asof(seri2, kal, cfg, set(), date(2026, 10, 13))
    assert len(t2) == 0 and list(t2.columns) == list(KOLOM_TANDA)


def _bar_anomali(kal):
    d = hari(kal, date(2026, 8, 3), 8)
    bar = buat_bar(d, [100, 100, 160, 161, 161, 162, 163, 164], volume=[1e6] * 4 + [0] + [1e6] * 3)
    return d, bar


def test_anomali_aktif_selama_min_bar_riwayat_bar_valid(kal):
    cfg = buat_cfg(UBAH_KECIL)  # min_bar_riwayat = 4
    d, bar = _bar_anomali(kal)
    _, t = proses(bar, cfg, kal, kode="UJIANOMALI")
    # Anomali di d[2] (urutan valid ke-3). Aktif sampai a keluar dari 4 bar valid terakhir: d[2]..d[6]
    # (d[4] tanpa transaksi tidak memajukan hitungan); d[7] = bar valid ke-7 → 7 − 3 = 4 → tidak aktif.
    assert t["anomali_aktif"].tolist() == [False, False, True, True, True, True, True, False]
    assert t["anomali_terakhir"].isna().tolist()[:2] == [True, True]
    assert (t["anomali_terakhir"].iloc[2:] == ts(d[2])).all()
    alasan = [alasan_data_asof(t.loc[ts(x)], cfg, False) for x in d]
    assert alasan == [
        "RIWAYAT_PENDEK", "RIWAYAT_PENDEK", "ANOMALI", "ANOMALI", "DATA_BASI", "ANOMALI", "ANOMALI", None,
    ]


def test_anomali_ok_menonaktifkan(kal):
    cfg = buat_cfg(UBAH_KECIL)
    d, bar = _bar_anomali(kal)
    _, t = proses(bar, cfg, kal, kode="UJIANOMALI", ok={("UJIANOMALI", d[2])})
    assert not t["anomali_aktif"].any() and t["anomali_terakhir"].isna().all()
    # entri anomali_ok untuk kode lain tidak berpengaruh
    _, t2 = proses(bar, cfg, kal, kode="UJIANOMALI", ok={("LAIN", d[2]), ("UJIANOMALI", d[3])})
    assert t2["anomali_aktif"].tolist() == [False, False, True, True, True, True, True, False]


def test_anomali_terakhir_diperbarui_anomali_baru(kal):
    cfg = buat_cfg(UBAH_KECIL)
    d = hari(kal, date(2026, 8, 3), 10)
    bar = buat_bar(d, [100, 160, 161, 162, 163, 164, 80, 81, 82, 83])
    _, t = proses(bar, cfg, kal)
    assert t["anomali_aktif"].tolist() == [False, True, True, True, True, False, True, True, True, True]
    assert t.loc[ts(d[5]), "anomali_terakhir"] == ts(d[1])
    assert t.loc[ts(d[6]), "anomali_terakhir"] == ts(d[6])


def test_anomali_indeks_memakai_sma_panjang_dan_tanpa_volume(kal):
    cfg = buat_cfg(UBAH_KECIL)  # sma_panjang = 3
    d = hari(kal, date(2026, 8, 3), 8)
    bar = buat_bar(d, [7000, 7000, 11200, 11210, 11220, 11230, 11240, 11250]).drop(columns=["volume"])
    _, t = proses(bar, cfg, kal, kode="IHSG", indeks=True)
    # anomali = bar valid ke-3; aktif selama 3 bar valid terakhir: d[2], d[3], d[4]
    assert t["anomali_aktif"].tolist() == [False, False, True, True, True, False, False, False]
    assert (t["volume_nol"] == 0).all() and t["ada_bar_valid"].all()
    alasan = [alasan_data_asof(t.loc[ts(x)], cfg, True) for x in d]
    assert alasan == [None, None, "ANOMALI", "ANOMALI", "ANOMALI", None, None, None]
    _, t_ok = proses(bar, cfg, kal, kode="IHSG", indeks=True, ok={("IHSG", d[2])})
    assert not t_ok["anomali_aktif"].any()


# ---------------------------------------------------------------------------
# Split belum disesuaikan (§8.4)
# ---------------------------------------------------------------------------


def test_split_belum_disesuaikan_terdeteksi_sejak_tanggal_split(kal):
    cfg = buat_cfg(UBAH_KECIL)
    d = hari(kal, date(2026, 8, 3), 8)
    split = pd.Series([5.0], index=pd.DatetimeIndex([ts(d[5])]))
    mentah = buat_bar(d, [5000] * 5 + [1000] * 3)  # harga sebelum split belum dibagi 5
    _, t = proses(mentah, cfg, kal, kode="UJISPLIT", split=split)
    assert t["split_belum_disesuaikan"].tolist() == [False] * 5 + [True] * 3
    # lompatan −80% juga anomali; urutan alasan: ANOMALI sebelum SPLIT_BELUM_DISESUAIKAN
    assert alasan_data_asof(t.iloc[-1], cfg, False) == "ANOMALI"
    _, t_ok = proses(mentah, cfg, kal, kode="UJISPLIT", split=split, ok={("UJISPLIT", d[5])})
    assert alasan_data_asof(t_ok.iloc[-1], cfg, False) == "SPLIT_BELUM_DISESUAIKAN"
    assert alasan_data_asof(t_ok.iloc[4], cfg, False) is None  # sebelum split: data nominal konsisten

    disesuaikan = buat_bar(d, [1000] * 8)
    _, t2 = proses(disesuaikan, cfg, kal, kode="UJISPLIT", split=split)
    assert not t2["split_belum_disesuaikan"].any()
    assert alasan_data_asof(t2.iloc[-1], cfg, False) is None


def test_split_belum_disesuaikan_dari_kolom_cache(kal):
    cfg = buat_cfg(UBAH_KECIL)
    d = hari(kal, date(2026, 8, 3), 8)
    bar = buat_bar(d, [5000] * 5 + [1000] * 3)
    bar["split"] = [0.0] * 5 + [5.0, 0.0, 0.0]
    _, t = proses(bar, cfg, kal, kode="UJISPLIT")
    assert t["split_belum_disesuaikan"].tolist() == [False] * 5 + [True] * 3


def test_split_tanpa_bar_valid_pada_tanggal_split_kausal(kal):
    cfg = buat_cfg(UBAH_KECIL)
    d = hari(kal, date(2026, 8, 3), 8)
    split = pd.Series([5.0], index=pd.DatetimeIndex([ts(d[5])]))
    bar = buat_bar(d, [5000] * 5 + [1000] * 3, volume=[1e6] * 5 + [0] + [1e6] * 2)
    _, t = proses(bar, cfg, kal, kode="UJISPLIT", split=split)
    # bukti baru ada pada bar valid pertama ≥ s (d[6]); d[5] belum boleh ditandai
    assert t["split_belum_disesuaikan"].tolist() == [False] * 6 + [True] * 2
    _, t_potong = proses(bar[bar.index <= ts(d[5])], cfg, kal, kode="UJISPLIT", split=split, akhir=d[5])
    assert not t_potong["split_belum_disesuaikan"].any()


def test_split_indeks_diabaikan_dan_di_luar_data(kal):
    cfg = buat_cfg(UBAH_KECIL)
    d = hari(kal, date(2026, 8, 3), 6)
    bar = buat_bar(d, [1000] * 6)
    sebelum = pd.Series([5.0], index=pd.DatetimeIndex(["2026-07-01"]))
    sesudah = pd.Series([5.0], index=pd.DatetimeIndex(["2026-09-30"]))
    for sp in (sebelum, sesudah):
        _, t = proses(bar, cfg, kal, split=sp)
        assert not t["split_belum_disesuaikan"].any()


# ---------------------------------------------------------------------------
# alasan_data_asof (§8.5, §11 syarat 3 & 5)
# ---------------------------------------------------------------------------


def _baris(**ubah) -> pd.Series:
    dasar = dict(
        ada_bar_valid=True, jumlah_bar_valid=4, bar_hilang=1, volume_nol=1, anomali_aktif=False,
        anomali_terakhir=pd.NaT, split_belum_disesuaikan=False,
    )
    dasar.update(ubah)
    return pd.Series(dasar)


@pytest.mark.parametrize(
    "ubah, csv, harapan",
    [
        ({}, False, None),  # tepat di batas: bar_hilang = maks, jumlah = min, volume_nol = maks → lolos
        (dict(ada_bar_valid=False, bar_hilang=9, anomali_aktif=True, split_belum_disesuaikan=True,
              jumlah_bar_valid=0, volume_nol=9), False, "DATA_BASI"),
        (dict(bar_hilang=2, anomali_aktif=True, split_belum_disesuaikan=True, jumlah_bar_valid=0,
              volume_nol=9), False, "BAR_HILANG"),
        (dict(anomali_aktif=True, split_belum_disesuaikan=True, jumlah_bar_valid=0, volume_nol=9),
         False, "ANOMALI"),
        (dict(split_belum_disesuaikan=True, jumlah_bar_valid=3, volume_nol=9), False, "SPLIT_BELUM_DISESUAIKAN"),
        (dict(jumlah_bar_valid=3, volume_nol=9), False, "RIWAYAT_PENDEK"),
        (dict(jumlah_bar_valid=3, volume_nol=9), True, "RIWAYAT_CSV_PENDEK"),
        (dict(volume_nol=2), False, "KEMUNGKINAN_SUSPENSI"),
        (dict(volume_nol=2), True, "KEMUNGKINAN_SUSPENSI"),
        (dict(bar_hilang=0, volume_nol=0, jumlah_bar_valid=500), False, None),
    ],
)
def test_alasan_urutan_dan_batas_saham(cfg, ubah, csv, harapan):
    assert alasan_data_asof(_baris(**ubah), cfg, False, csv_pengganti_penuh=csv) == harapan


def test_alasan_indeks_hanya_basi_hilang_anomali(cfg):
    lain_buruk = dict(split_belum_disesuaikan=True, jumlah_bar_valid=0, volume_nol=99)
    assert alasan_data_asof(_baris(**lain_buruk), cfg, True) is None
    assert alasan_data_asof(_baris(anomali_aktif=True, **lain_buruk), cfg, True) == "ANOMALI"
    assert alasan_data_asof(_baris(bar_hilang=2, anomali_aktif=True), cfg, True) == "BAR_HILANG"
    assert alasan_data_asof(_baris(ada_bar_valid=False), cfg, True) == "DATA_BASI"


def test_alasan_tanpa_baris_data_basi(cfg):
    assert alasan_data_asof(None, cfg, False) == "DATA_BASI"
    assert alasan_data_asof(None, cfg, True) == "DATA_BASI"


def test_alasan_selalu_enum(cfg):
    kombinasi = [
        _baris(ada_bar_valid=a, bar_hilang=h, anomali_aktif=an, split_belum_disesuaikan=s, jumlah_bar_valid=j,
               volume_nol=v)
        for a in (True, False) for h in (0, 5) for an in (True, False) for s in (True, False)
        for j in (0, 10) for v in (0, 5)
    ]
    for b in kombinasi:
        for indeks in (True, False):
            for csv in (True, False):
                hasil = alasan_data_asof(b, cfg, indeks, csv)
                assert hasil is None or hasil in ALASAN_DIKELUARKAN


# ---------------------------------------------------------------------------
# Skenario T-DQ: struktur → bar → tanda → alasan
# ---------------------------------------------------------------------------

UBAH_SKENARIO = {
    "data.validasi.jendela_kelengkapan_bar": 10,
    "data.validasi.maks_bar_hilang": 2,
    "data.validasi.lonjakan_mencurigakan_pct": 40,
    "likuiditas.periode_hari": 5,
    "likuiditas.maks_hari_volume_nol": 1,
    "universe.min_bar_riwayat": 5,
    "kondisi_pasar.sma_panjang": 5,
}


@pytest.fixture
def skenario(kal):
    d = hari(kal, date(2026, 8, 3), 30)
    n = len(d)
    rata = np.full(n, 1000.0)
    anomali = rata.copy()
    anomali[12:] = 1600.0  # lonjakan +60% tanpa split pada d[12]
    nol = buat_bar(d, rata, volume=[0.0 if i in (10, 11) else 1e6 for i in range(n)])
    bolong = buat_bar(d, rata).drop(index=[ts(d[i]) for i in (10, 11, 12)])
    ihsg = buat_bar(d, 7000 + np.arange(n, dtype=float)).drop(columns=["volume"])
    libur = pd.concat([buat_bar(d, rata), buat_bar(["2026-08-17"], [1000.0], volume=0.0)]).sort_index()
    return d, {
        "UJIANOMALI": (buat_bar(d, anomali), False),
        "UJINOL": (nol, False),
        "UJIBOLONG": (bolong, False),
        "IHSG": (ihsg, True),
        "UJILIBUR": (libur, False),
    }


def _alasan_per_hari(kode, bar, indeks, cfg, kal, d, ok=()):
    _, t = proses(bar, cfg, kal, kode=kode, indeks=indeks, ok=ok, akhir=d[-1])
    return [alasan_data_asof(t.loc[ts(x)], cfg, indeks) for x in d], t


def test_skenario_lonjakan_60_persen(kal, skenario):
    cfg = buat_cfg(UBAH_SKENARIO)
    d, data = skenario
    alasan, t = _alasan_per_hari("UJIANOMALI", *data["UJIANOMALI"], cfg, kal, d)
    assert alasan[:4] == ["RIWAYAT_PENDEK"] * 4
    assert alasan[4:12] == [None] * 8  # sebelum lonjakan: as-of tidak tahu masa depan
    assert alasan[12:17] == ["ANOMALI"] * 5  # dari tanggal lonjakan, selama 5 bar valid
    assert alasan[17:] == [None] * (len(d) - 17)
    alasan_ok, _ = _alasan_per_hari("UJIANOMALI", *data["UJIANOMALI"], cfg, kal, d, ok={("UJIANOMALI", d[12])})
    assert "ANOMALI" not in alasan_ok


def test_skenario_volume_nol_beruntun_kemungkinan_suspensi(kal, skenario):
    cfg = buat_cfg(UBAH_SKENARIO)
    d, data = skenario
    alasan, t = _alasan_per_hari("UJINOL", *data["UJINOL"], cfg, kal, d)
    assert alasan[10:12] == ["DATA_BASI"] * 2
    assert t.loc[ts(d[12]), "volume_nol"] == 2 and t.loc[ts(d[12]), "bar_hilang"] == 2
    assert alasan[12:15] == ["KEMUNGKINAN_SUSPENSI"] * 3
    assert alasan[15:] == [None] * (len(d) - 15)
    # tiga hari beruntun → bar hilang 3 > 2 lebih dulu
    bar3 = data["UJINOL"][0].copy()
    bar3.loc[ts(d[12]), "volume"] = 0.0
    alasan3, _ = _alasan_per_hari("UJINOL", bar3, False, cfg, kal, d)
    assert alasan3[13] == "BAR_HILANG"


def test_skenario_bar_hilang(kal, skenario):
    cfg = buat_cfg(UBAH_SKENARIO)
    d, data = skenario
    alasan, t = _alasan_per_hari("UJIBOLONG", *data["UJIBOLONG"], cfg, kal, d)
    assert alasan[10:13] == ["DATA_BASI"] * 3
    assert alasan[13:20] == ["BAR_HILANG"] * 7  # jendela 10 hari bursa masih memuat 3 hari hilang
    assert alasan[20:] == [None] * (len(d) - 20)
    assert t["bar_hilang"].max() == 3


def test_skenario_ihsg_tanpa_volume_lolos(kal, skenario):
    cfg = buat_cfg(UBAH_SKENARIO)
    d, data = skenario
    alasan, t = _alasan_per_hari("IHSG", *data["IHSG"], cfg, kal, d)
    assert alasan == [None] * len(d)
    assert (t["volume_nol"] == 0).all()


def test_skenario_bar_libur_volume_nol_tidak_dihitung(kal, skenario):
    cfg = buat_cfg({**UBAH_SKENARIO, "likuiditas.maks_hari_volume_nol": 0})
    d, data = skenario
    alasan, t = _alasan_per_hari("UJILIBUR", *data["UJILIBUR"], cfg, kal, d)
    assert (t["volume_nol"] == 0).all()
    assert alasan[4:] == [None] * (len(d) - 4)
    st = validasi_struktur(data["UJILIBUR"][0])
    seri = validasi_bar(st.bar, None, "UJILIBUR", False, kal)
    assert ts("2026-08-17") not in seri.valid.index  # tidak memengaruhi indikator (dihitung dari valid)


# ---------------------------------------------------------------------------
# Kausalitas: potong ≤ d vs penuh (T-LA1a bagian tanda data), gangguan masa depan, kanari
# ---------------------------------------------------------------------------

LIBUR_ACAK = {
    date(2025, 8, 18): "FIKTIF – KHUSUS UJI",
    date(2025, 12, 25): "FIKTIF – KHUSUS UJI",
    date(2026, 1, 1): "FIKTIF – KHUSUS UJI",
    date(2026, 3, 20): "FIKTIF – KHUSUS UJI",
}
UBAH_KAUSAL = {
    "data.validasi.jendela_kelengkapan_bar": 10,
    "data.validasi.maks_bar_hilang": 2,
    "likuiditas.periode_hari": 7,
    "universe.min_bar_riwayat": 20,
    "kondisi_pasar.sma_panjang": 15,
}


def seri_acak(seed: int, kal: Kalender, n: int = 260, indeks: bool = False):
    """Seri sintetis berseed: random walk + bar rusak, tanpa transaksi, hilang, lonjakan, bar libur,
    dan (saham) satu split yang belum disesuaikan sumber."""
    rng = np.random.default_rng(seed)
    d = hari(kal, date(2025, 6, 2), n)
    close = 1000.0 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    for i in rng.choice(np.arange(5, n), size=4, replace=False):
        close[i] *= rng.choice([1.6, 0.5])  # lonjakan satu hari
    split = None
    if not indeks:
        s = int(rng.integers(n // 2, n - 10))
        close[s:] /= 5.0  # harga mentah sesudah split 1:5, harga sebelumnya belum disesuaikan
        split = pd.Series([5.0], index=pd.DatetimeIndex([ts(d[s])]))
    bar = buat_bar(d, close, volume=rng.lognormal(13, 1, n))
    bar["open"] = close * (1 + rng.normal(0, 0.003, n)).clip(0.98, 1.02)
    bar["high"] = np.maximum(bar["open"], close) * 1.01
    bar["low"] = np.minimum(bar["open"], close) * 0.99
    acak = rng.random(n)
    bar.loc[acak < 0.03, "close"] = np.nan
    bar.loc[(acak >= 0.03) & (acak < 0.05), "high"] = bar["low"] * 0.5
    if indeks:
        bar = bar.drop(columns=["volume"])
    else:
        bar.loc[(acak >= 0.05) & (acak < 0.12), "volume"] = 0.0
    bar = bar[~((acak >= 0.12) & (acak < 0.17))]  # bar hilang
    libur = [x for x in LIBUR_ACAK if d[0] <= x <= d[-1]]
    ekstra = buat_bar(libur, [1000.0] * len(libur), volume=0.0)
    if indeks:
        ekstra = ekstra.drop(columns=["volume"])
    bar = pd.concat([bar, ekstra]).sort_index()
    return d, bar, split


def _sama(a: pd.Series, b: pd.Series) -> bool:
    for k in KOLOM_TANDA:
        x, y = a[k], b[k]
        if pd.isna(x) and pd.isna(y):
            continue
        if pd.isna(x) or pd.isna(y) or x != y:
            return False
    return True


def periksa_kausal_tanda(bar, split, kode, indeks, kal, cfg, ok, tanggal_list, fungsi=hitung_tanda_asof,
                         potong_split=False) -> list[tuple[date, str]]:
    """Bandingkan baris d dari data penuh dengan baris terakhir dari data yang dipotong ≤ d."""
    st = validasi_struktur(bar, adalah_indeks=indeks)
    assert st.ok, st.pesan
    akhir = st.bar.index[-1].date()
    penuh = fungsi(validasi_bar(st.bar, split, kode, indeks, kal), kal, cfg, ok, akhir)
    tidak_cocok = []
    for d in tanggal_list:
        potong_bar = st.bar[st.bar.index <= ts(d)]
        sp = split
        if potong_split and split is not None:
            sp = split[split.index <= ts(d)]
        t = fungsi(validasi_bar(potong_bar, sp, kode, indeks, kal), kal, cfg, ok, d)
        if ts(d) not in penuh.index:
            if len(t) and t.index[-1] == ts(d):
                tidak_cocok.append((d, "baris ada di potongan tetapi tidak di data penuh"))
            continue
        if len(t) == 0 or t.index[-1] != ts(d):
            tidak_cocok.append((d, "baris d tidak ada di potongan"))
        elif not _sama(t.iloc[-1], penuh.loc[ts(d)]):
            tidak_cocok.append((d, f"potong={t.iloc[-1].to_dict()} penuh={penuh.loc[ts(d)].to_dict()}"))
    return tidak_cocok


@pytest.fixture
def kal_acak() -> Kalender:
    return Kalender(LIBUR_ACAK)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_kausal_potong_vs_penuh_saham(kal_acak, seed):
    cfg = buat_cfg(UBAH_KAUSAL)
    d, bar, split = seri_acak(seed, kal_acak)
    ok = {("UJIACAK", d[40])}
    penuh = hitung_tanda_asof(validasi_bar(validasi_struktur(bar).bar, split, "UJIACAK", False, kal_acak),
                              kal_acak, cfg, ok, d[-1])
    # fixture benar-benar memuat kejadian yang diuji
    assert penuh["anomali_aktif"].any() and penuh["split_belum_disesuaikan"].any()
    assert (penuh["volume_nol"] > 0).any() and (penuh["bar_hilang"] > cfg.data.validasi.maks_bar_hilang).any()
    assert periksa_kausal_tanda(bar, split, "UJIACAK", False, kal_acak, cfg, ok, d) == []
    rng = np.random.default_rng(seed + 100)
    sampel = sorted(rng.choice(np.arange(len(d)), size=60, replace=False))
    assert periksa_kausal_tanda(bar, split, "UJIACAK", False, kal_acak, cfg, ok, [d[i] for i in sampel],
                                potong_split=True) == []


def test_kausal_potong_vs_penuh_indeks(kal_acak):
    cfg = buat_cfg(UBAH_KAUSAL)
    d, bar, _ = seri_acak(7, kal_acak, indeks=True)
    assert periksa_kausal_tanda(bar, None, "IHSG", True, kal_acak, cfg, set(), d) == []


def test_kausal_akhir_memotong_hasil(kal_acak):
    cfg = buat_cfg(UBAH_KAUSAL)
    d, bar, split = seri_acak(4, kal_acak)
    seri = validasi_bar(validasi_struktur(bar).bar, split, "UJIACAK", False, kal_acak)
    penuh = hitung_tanda_asof(seri, kal_acak, cfg, set(), d[-1])
    for i in (0, 30, 131, len(d) - 2):
        sebagian = hitung_tanda_asof(seri, kal_acak, cfg, set(), d[i])
        pd.testing.assert_frame_equal(sebagian, penuh.loc[: ts(d[i])])


def test_gangguan_masa_depan_tidak_mengubah_masa_lalu(kal_acak):
    cfg = buat_cfg(UBAH_KAUSAL)
    d, bar, split = seri_acak(5, kal_acak)
    t_idx = 150
    batas = ts(d[t_idx])
    seri = validasi_bar(validasi_struktur(bar).bar, split, "UJIACAK", False, kal_acak)
    asli = hitung_tanda_asof(seri, kal_acak, cfg, set(), d[-1]).loc[:batas]
    rng = np.random.default_rng(99)
    masa_depan = bar[bar.index > batas].copy()
    n = len(masa_depan)
    derau = 10 ** rng.uniform(1, 4, n)  # lonjakan jauh > ambang
    masa_depan[["open", "high", "low", "close"]] = np.column_stack([derau, derau * 1.1, derau * 0.9, derau])
    masa_depan["volume"] = rng.choice([0.0, 1e6], n)
    masa_depan = masa_depan.iloc[::3]  # banyak bar hilang
    terganggu = pd.concat([bar[bar.index <= batas], masa_depan])
    split_lain = pd.concat([split, pd.Series([2.0], index=pd.DatetimeIndex([batas + pd.Timedelta(days=7)]))])
    seri2 = validasi_bar(validasi_struktur(terganggu).bar, split_lain, "UJIACAK", False, kal_acak)
    hasil = hitung_tanda_asof(seri2, kal_acak, cfg, set(), d[-1]).loc[:batas]
    if split.index[0] > batas:
        # split asli di masa depan: penandanya juga masa depan, tidak memengaruhi ≤ t
        assert not asli["split_belum_disesuaikan"].any()
    pd.testing.assert_frame_equal(hasil, asli)


def _tanda_bocor(seri, kal, cfg, ok, akhir):
    """Kanari (T-LA3): sengaja memakai nilai besok."""
    t = hitung_tanda_asof(seri, kal, cfg, ok, akhir)
    t["jumlah_bar_valid"] = t["jumlah_bar_valid"].shift(-1).fillna(-1).astype("int64")
    return t


def test_kanari_pemeriksa_kausal_menemukan_kebocoran(kal_acak):
    cfg = buat_cfg(UBAH_KAUSAL)
    d, bar, split = seri_acak(1, kal_acak)
    hasil = periksa_kausal_tanda(bar, split, "UJIACAK", False, kal_acak, cfg, set(), d[::10], fungsi=_tanda_bocor)
    assert len(hasil) > 0


def test_tervektor_cukup_cepat(kal_acak):
    # ±2.600 hari bursa × 50 saham harus jauh di bawah satu detik per saham (tidak ada loop per tanggal).
    cfg = buat_cfg()
    kal = Kalender(LIBUR_ACAK)
    rng = np.random.default_rng(3)
    d = kal.daftar_hari_bursa(date(2016, 1, 4), date(2026, 10, 9))
    close = 1000.0 * np.exp(np.cumsum(rng.normal(0, 0.02, len(d))))
    bar = buat_bar(d, close, volume=rng.choice([0.0, 1e6, 2e6], len(d), p=[0.05, 0.5, 0.45]))
    st = validasi_struktur(bar)
    mulai = time.perf_counter()
    for i in range(50):
        seri = validasi_bar(st.bar, None, f"S{i}", False, kal)
        t = hitung_tanda_asof(seri, kal, cfg, set(), d[-1])
    durasi = time.perf_counter() - mulai
    assert len(t) == len(d)
    assert durasi < 10.0, f"terlalu lambat: {durasi:.2f} s"


# ---------------------------------------------------------------------------
# Berkas kualitas
# ---------------------------------------------------------------------------


def _baca_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, skiprows=1, dtype=str, keep_default_na=False)


def test_tulis_berkas_kualitas(tmp_path):
    baris = [
        BarisKualitas(
            kode="ABCD", sumber="yfinance", status_ambil="OK", jenis_error=None, pesan="",
            bar_terakhir="2026-10-14", bar_hilang_60=0, anomali_terakhir="", split_belum_disesuaikan=False,
            alasan_dikeluarkan=None,
        ),
        BarisKualitas(
            kode="WXYZ", sumber="yfinance", status_ambil="GAGAL", jenis_error="TIDAK_ADA_DATA",
            pesan="possibly delisted,\nno data", alasan_dikeluarkan="GAGAL_AMBIL",
        ),
        BarisKualitas(
            kode="UJISPLIT", sumber="csv_manual", status_ambil="SEBAGIAN", bar_terakhir="2026-10-13",
            bar_hilang_60=3, anomali_terakhir="2026-09-01", split_belum_disesuaikan=True,
            alasan_dikeluarkan="BAR_HILANG",
        ),
    ]
    path = tmp_path / "output" / "kualitas" / "20261015-200000_unduh.csv"
    hasil = tulis_berkas_kualitas(path, baris)
    assert hasil == path
    teks = path.read_text(encoding="utf-8")
    baris_teks = teks.split("\n")
    assert baris_teks[0] == DISCLAIMER_CSV
    assert "BUKAN SARAN INVESTASI" in baris_teks[0]
    assert baris_teks[1] == ",".join(KOLOM_KUALITAS)
    df = _baca_csv(path)
    assert list(df.columns) == KOLOM_KUALITAS and len(df) == 3
    assert df.iloc[0].to_dict() == {
        "kode": "ABCD", "sumber": "yfinance", "status_ambil": "OK", "jenis_error": "", "pesan": "",
        "bar_terakhir": "2026-10-14", "bar_hilang_60": "0", "anomali_terakhir": "",
        "split_belum_disesuaikan": "0", "alasan_dikeluarkan": "",
    }
    assert df.iloc[1]["pesan"] == "possibly delisted, no data"  # satu baris
    assert df.iloc[1]["bar_hilang_60"] == "" and df.iloc[1]["split_belum_disesuaikan"] == ""
    assert df.iloc[2]["split_belum_disesuaikan"] == "1" and df.iloc[2]["anomali_terakhir"] == "2026-09-01"
    # atomik: tidak ada berkas sementara tersisa
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_tulis_berkas_kualitas_kosong(tmp_path):
    path = tmp_path / "kualitas_data.csv"
    tulis_berkas_kualitas(path, [])
    assert path.read_text(encoding="utf-8").split("\n")[:2] == [DISCLAIMER_CSV, ",".join(KOLOM_KUALITAS)]
    assert len(_baca_csv(path)) == 0


def test_tulis_berkas_kualitas_terkunci_nama_alternatif(tmp_path, monkeypatch):
    path = tmp_path / "kualitas_data.csv"
    asli = os.replace

    def replace_terkunci(src, dst, *a, **kw):
        if Path(dst) == path:
            raise PermissionError("berkas dibuka di Excel")
        return asli(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", replace_terkunci)
    peringatan: list[str] = []
    hasil = tulis_berkas_kualitas(path, [BarisKualitas(kode="ABCD")], peringatan)
    assert hasil == tmp_path / "kualitas_data_200000.csv"  # jam virtual 20:00:00 WIB
    assert hasil.read_text(encoding="utf-8").startswith(DISCLAIMER_CSV)
    assert len(peringatan) == 1 and "kualitas_data.csv" in peringatan[0]
    assert not path.exists()


def test_pesan_kualitas_disensor(tmp_path, monkeypatch):
    from asisten_bei import operasional

    penyaring = operasional.PenyaringRahasia()
    penyaring.tambah("123456:RAHASIA-token-uji")
    monkeypatch.setattr(operasional, "PENYARING", penyaring)
    path = tmp_path / "k.csv"
    tulis_berkas_kualitas(path, [BarisKualitas(kode="ABCD", pesan="gagal https://x/bot123456:RAHASIA-token-uji/a")])
    teks = path.read_text(encoding="utf-8")
    assert "RAHASIA-token-uji" not in teks and "***" in teks


def test_path_berkas_kualitas_jam_virtual(tmp_path):
    p = path_berkas_kualitas(tmp_path, "unduh")
    assert p == tmp_path / "output" / "kualitas" / "20261015-200000_unduh.csv"


def test_baris_kualitas_dari_tanda(kal, skenario):
    cfg = buat_cfg(UBAH_SKENARIO)
    d, data = skenario
    _, t = proses(data["UJIANOMALI"][0], cfg, kal, kode="UJIANOMALI", akhir=d[-1])
    alasan = alasan_data_asof(t.loc[ts(d[14])], cfg, False)
    b = baris_kualitas_dari_tanda(
        "UJIANOMALI", sumber="yfinance", status_ambil="OK", tanda=t, d=d[14], alasan=alasan
    )
    assert b.bar_terakhir == d[14].isoformat()
    assert b.bar_hilang_60 == 0 and b.anomali_terakhir == d[12].isoformat()
    assert b.split_belum_disesuaikan is False and b.alasan_dikeluarkan == "ANOMALI"

    _, t_nol = proses(data["UJINOL"][0], cfg, kal, kode="UJINOL", akhir=d[-1])
    b2 = baris_kualitas_dari_tanda("UJINOL", tanda=t_nol, d=d[11], alasan="DATA_BASI")
    assert b2.bar_terakhir == d[9].isoformat() and b2.bar_hilang_60 == 2 and b2.anomali_terakhir == ""

    gagal = baris_kualitas_dari_tanda(
        "WXYZ", sumber="yfinance", status_ambil="GAGAL", jenis_error="JARINGAN", pesan="timed out",
        alasan="GAGAL_AMBIL",
    )
    assert gagal.sebagai_dict() == {
        "kode": "WXYZ", "sumber": "yfinance", "status_ambil": "GAGAL", "jenis_error": "JARINGAN",
        "pesan": "timed out", "bar_terakhir": "", "bar_hilang_60": "", "anomali_terakhir": "",
        "split_belum_disesuaikan": "", "alasan_dikeluarkan": "GAGAL_AMBIL",
    }
