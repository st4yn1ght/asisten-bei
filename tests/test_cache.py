"""Uji T-CA (SPEC §8.3, §22.1) untuk `data/cache.py`, plus bagian data T-SP (split di cache).

Provider palsu `PasarPalsu` memegang "kebenaran" pasar (DataFrame bar + seri split per kode) yang bisa
direvisi antar-run, dan mencatat setiap permintaan `(kode, mulai, akhir)`. Kalender tanpa libur
(Senin–Jumat); 2026-10-15 adalah Kamis. Config uji dibangun dari config.yaml rilis dengan
`data.mulai_unduh` dipersingkat. Jadwal sesi "terverifikasi" memakai jam FIKTIF (bukan jadwal BEI).
"""

from __future__ import annotations

import copy
import json
import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from asisten_bei.config import Config, validasi_dict
from asisten_bei.data.cache import (
    CATATAN_DISESUAIKAN_ULANG,
    KOLOM_CACHE,
    KUNCI_META,
    CacheProvider,
    IsiCache,
    buat_meta,
    perbarui_cache,
    teks_meta,
)
from asisten_bei.data.provider import KOLOM_BAR, NAMA_INDEX, HasilAmbil, gagal, ke_ts
from asisten_bei.data.split import faktor_split, seri_faktor, split_dari_kolom
from asisten_bei.kalender import Kalender
from asisten_bei.waktu import ZONA

AKAR_PROYEK = Path(__file__).resolve().parents[1]
_CONFIG_RILIS = yaml.safe_load((AKAR_PROYEK / "config.yaml").read_text(encoding="utf-8"))

FIKTIF = "FIKTIF – KHUSUS UJI"
JADWAL_FIKTIF = {  # bukan jadwal BEI: 16:00 + jeda 60 menit → batas bar final 17:00
    "terverifikasi": True,
    "sumber": f"{FIKTIF} (bukan jadwal resmi)",
    "tanggal_cek": "2026-10-01",
    "jam_penutupan": "16:00",
    "jam_pra_pembukaan": "08:45",
}
MULAI_UNDUH = date(2026, 9, 1)
KAL = Kalender({})
KODE = "ABCD"


def buat_cfg(jadwal_sesi: dict | None = None, **data_ubah) -> Config:
    data = copy.deepcopy(_CONFIG_RILIS)
    data["data"]["mulai_unduh"] = MULAI_UNDUH.isoformat()
    data["data"].update(data_ubah)
    if jadwal_sesi:
        data["pasar"]["jadwal_sesi"].update(jadwal_sesi)
    return validasi_dict(data)


CFG = buat_cfg()  # bar_cek_ulang 10, toleransi close 0.5%, volume 5%, coba lagi 7 hari, batas 19:30


def wib(teks: str) -> datetime:
    return datetime.fromisoformat(teks).replace(tzinfo=ZONA)


def pasar_harian(mulai: str, akhir: str, harga: float = 1000.0, dengan_volume: bool = True) -> pd.DataFrame:
    """Bar sintetis deterministik setiap Senin–Jumat: close = harga + i."""
    idx = pd.DatetimeIndex(pd.bdate_range(mulai, akhir), name=NAMA_INDEX)
    i = np.arange(len(idx), dtype="float64")
    close = harga + i
    df = pd.DataFrame(
        {"open": close - 2.0, "high": close + 5.0, "low": close - 5.0, "close": close}, index=idx
    )
    if dengan_volume:
        df["volume"] = 1_000_000.0 + 1000.0 * i
    return df


class PasarPalsu:
    """Fungsi `ambil` palsu: mengembalikan potongan [mulai, akhir] dari kebenaran pasar."""

    def __init__(self, sumber: str = "yfinance", versi: str = "palsu-1.0"):
        self.sumber = sumber
        self.versi = versi
        self.bar: dict[str, pd.DataFrame] = {}
        self.split: dict[str, pd.Series] = {}
        self.panggilan: list[tuple[str, date, date]] = []
        self.gagal: dict[str, tuple[str, str]] = {}  # kode → (jenis, pesan) untuk setiap panggilan
        self.gagal_pada: dict[int, tuple[str, str]] = {}  # nomor panggilan (mulai 1) → (jenis, pesan)
        self.abaikan_rentang = False  # provider "nakal" yang mengembalikan semua bar

    def ambil(self, kode: str, mulai: date, akhir: date) -> HasilAmbil:
        self.panggilan.append((kode, mulai, akhir))
        n = len(self.panggilan)
        if n in self.gagal_pada:
            return gagal(kode, self.sumber, *self.gagal_pada[n], versi=self.versi)
        if kode in self.gagal:
            return gagal(kode, self.sumber, *self.gagal[kode], versi=self.versi)
        df = self.bar[kode]
        sp = self.split.get(kode, pd.Series(dtype="float64", index=pd.DatetimeIndex([])))
        if not self.abaikan_rentang:
            df = df[(df.index >= ke_ts(mulai)) & (df.index <= ke_ts(akhir))]
            sp = sp[(sp.index >= ke_ts(mulai)) & (sp.index <= ke_ts(akhir))]
        if len(df) == 0:
            return gagal(kode, self.sumber, "TIDAK_ADA_DATA", "No data found, symbol may be delisted", self.versi)
        return HasilAmbil(kode, self.sumber, "OK", None, "", df.copy(), sp.copy(), self.versi)


@pytest.fixture
def pasar() -> PasarPalsu:
    p = PasarPalsu()
    p.bar[KODE] = pasar_harian("2026-09-01", "2026-10-16")
    return p


@pytest.fixture
def cache(tmp_path) -> CacheProvider:
    return CacheProvider(tmp_path / "cache" / "yfinance")


def perbarui(pasar: PasarPalsu, cache: CacheProvider, saat: str, kode: str = KODE, cfg: Config = CFG, **kw):
    return perbarui_cache(pasar.ambil, cache, kode, cfg=cfg, kal=KAL, sekarang=wib(saat), **kw)


def kebenaran_sampai(pasar: PasarPalsu, akhir: str, kode: str = KODE) -> pd.DataFrame:
    df = pasar.bar[kode]
    return df[df.index <= pd.Timestamp(akhir)]


def assert_bar_sama(cache_bar: pd.DataFrame, harapan: pd.DataFrame) -> None:
    """Bandingkan kolom harga/volume cache dengan kebenaran (index & nilai persis)."""
    assert list(cache_bar.index) == list(harapan.index)
    for k in KOLOM_BAR:
        if k in harapan.columns:
            np.testing.assert_array_equal(cache_bar[k].to_numpy(), harapan[k].to_numpy())


def isi_folder(cache: CacheProvider) -> list[str]:
    return sorted(p.name for p in cache.folder.iterdir())


def potret(cache: CacheProvider, kode: str = KODE) -> tuple[bytes | None, bytes | None]:
    p, m = cache.path_parquet(kode), cache.path_meta(kode)
    return (p.read_bytes() if p.exists() else None, m.read_bytes() if m.exists() else None)


def siapkan_cache_awal(pasar, cache, saat="2026-10-09 20:00"):
    """Unduh awal penuh s/d Jumat 2026-10-09; lalu kosongkan catatan panggilan."""
    h = perbarui(pasar, cache, saat)
    assert h.status == "OK" and h.unduh_penuh
    pasar.panggilan.clear()
    return h


# ---------------------------------------------------------------------------
# Unduh awal & format berkas
# ---------------------------------------------------------------------------


def test_unduh_awal_penuh_dan_format_berkas(pasar, cache):
    h = perbarui(pasar, cache, "2026-10-09 20:00", versi_provider="yfinance 1.7.0")
    assert pasar.panggilan == [(KODE, MULAI_UNDUH, date(2026, 10, 9))]
    assert (h.status, h.jenis_error, h.pesan, h.unduh_penuh, h.dilewati) == ("OK", None, "", True, False)
    assert isi_folder(cache) == ["ABCD.meta.json", "ABCD.parquet"]

    isi = cache.baca(KODE)
    assert not isi.rusak and isi.pesan == ""
    assert list(isi.bar.columns) == list(KOLOM_CACHE)
    assert isi.bar.index.name == NAMA_INDEX and str(isi.bar.index.dtype) == "datetime64[ns]"
    assert_bar_sama(isi.bar, kebenaran_sampai(pasar, "2026-10-09"))
    assert (isi.bar["sumber"] == "yfinance").all()  # nama folder provider
    assert (isi.bar["split"] == 0.0).all()
    pd.testing.assert_frame_equal(isi.bar, h.isi.bar)

    meta = isi.meta
    assert sorted(meta) == sorted(KUNCI_META)
    assert meta == {
        "bar_pertama": "2026-09-01",
        "bar_terakhir": "2026-10-09",
        "diambil_pada": "2026-10-09T20:00:00+07:00",
        "jumlah_bar": 29,  # 1 Sep – 9 Okt 2026, Senin–Jumat
        "split_terakhir": None,
        "tidak_ada_data_sejak": None,
        "versi_provider": "yfinance 1.7.0",  # parameter mengalahkan versi dari HasilAmbil
    }
    teks = cache.path_meta(KODE).read_text(encoding="utf-8")
    assert teks == json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_versi_provider_dari_hasil_ambil_bila_parameter_kosong(pasar, cache):
    h = perbarui(pasar, cache, "2026-10-09 20:00")
    assert h.isi.meta["versi_provider"] == "palsu-1.0"


def test_penuh_true_mengunduh_dari_mulai_unduh(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    h = perbarui(pasar, cache, "2026-10-15 20:00", penuh=True)
    assert pasar.panggilan == [(KODE, MULAI_UNDUH, date(2026, 10, 15))]
    assert h.unduh_penuh and h.status == "OK"
    assert_bar_sama(cache.baca(KODE).bar, kebenaran_sampai(pasar, "2026-10-15"))


# ---------------------------------------------------------------------------
# Inkremental: hanya bar baru + bar_cek_ulang bar cek ulang
# ---------------------------------------------------------------------------


def test_inkremental_hanya_meminta_bar_ke_10_dari_belakang_sampai_hari_ini(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    lama = cache.baca(KODE).bar
    # Hitung tangan: 10 bar terakhir s/d Jumat 9 Okt = 9,8,7,6,5,2,1 Okt, 30,29,28 Sep → mulai 28 Sep.
    assert lama.index[-10] == pd.Timestamp("2026-09-28")
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert pasar.panggilan == [(KODE, date(2026, 9, 28), date(2026, 10, 15))]
    assert (lama.index >= pd.Timestamp("2026-09-28")).sum() == 10  # tepat 10 bar tumpang tindih
    assert h.status == "OK" and not h.unduh_penuh and not h.dilewati
    assert h.revisi_volume == [] and h.catatan == []
    isi = cache.baca(KODE)
    assert_bar_sama(isi.bar, kebenaran_sampai(pasar, "2026-10-15"))
    assert isi.meta["bar_terakhir"] == "2026-10-15" and isi.meta["jumlah_bar"] == 33
    assert isi.meta["diambil_pada"] == "2026-10-15T20:00:00+07:00"


def test_bar_cek_ulang_dari_config(pasar, cache):
    cfg3 = buat_cfg(bar_cek_ulang=3)
    perbarui(pasar, cache, "2026-10-09 20:00", cfg=cfg3)
    pasar.panggilan.clear()
    perbarui(pasar, cache, "2026-10-15 20:00", cfg=cfg3)
    assert pasar.panggilan == [(KODE, date(2026, 10, 7), date(2026, 10, 15))]  # 9, 8, 7 Okt


def test_cache_lebih_pendek_dari_bar_cek_ulang_mulai_dari_bar_pertama(pasar, cache):
    pasar.bar[KODE] = pasar_harian("2026-10-05", "2026-10-16")  # saham baru melantai 5 Okt
    perbarui(pasar, cache, "2026-10-07 20:00")  # 3 bar
    pasar.panggilan.clear()
    perbarui(pasar, cache, "2026-10-15 20:00")
    assert pasar.panggilan == [(KODE, date(2026, 10, 5), date(2026, 10, 15))]
    assert_bar_sama(cache.baca(KODE).bar, kebenaran_sampai(pasar, "2026-10-15"))


def test_provider_yang_mengabaikan_rentang_tidak_menggandakan_bar(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    pasar.abaikan_rentang = True  # mengembalikan semua bar 1 Sep – 16 Okt (16 Okt = besok)
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    isi = cache.baca(KODE)
    assert isi.bar.index.is_unique
    assert_bar_sama(isi.bar, kebenaran_sampai(pasar, "2026-10-15"))
    assert "bar 2026-10-16 bertanggal sesudah hari ini: dibuang" in h.catatan


# ---------------------------------------------------------------------------
# Tumpang tindih & revisi
# ---------------------------------------------------------------------------


def test_tumpang_tindih_selalu_ditimpa_nilai_baru(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    df = pasar.bar[KODE].copy()
    t = pd.Timestamp("2026-10-05")
    df.loc[t, "close"] = df.loc[t, "close"] * 1.004  # 0,4% < toleransi 0,5%
    df.loc[t, "high"] = df.loc[t, "high"] + 3.0
    df.loc[t, "open"] = df.loc[t, "open"] - 1.0
    df.loc[t, "volume"] = df.loc[t, "volume"] * 1.02  # 2% < toleransi 5%
    pasar.bar[KODE] = df
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert len(pasar.panggilan) == 1 and not h.unduh_penuh
    assert h.revisi_volume == [] and CATATAN_DISESUAIKAN_ULANG not in h.catatan
    baris = cache.baca(KODE).bar.loc[t]
    for k in KOLOM_BAR:
        assert baris[k] == df.loc[t, k]


@pytest.mark.parametrize(
    "close_baru, penuh",
    [
        (1004.0, False),  # 0,4%
        (1005.0, False),  # tepat 0,5% — tidak melebihi toleransi
        (1006.0, True),  # 0,6%
        (994.0, True),  # −0,6%
    ],
)
def test_revisi_close_terhadap_toleransi(pasar, cache, close_baru, penuh):
    siapkan_cache_awal(pasar, cache)
    df = pasar.bar[KODE].copy()
    t = pd.Timestamp("2026-10-06")
    lama_close = df.loc[t, "close"]
    df["close"] = df["close"].astype("float64")
    df.loc[t, "close"] = lama_close * close_baru / 1000.0
    df.loc[t, ["high", "low"]] = [max(df.loc[t, "high"], df.loc[t, "close"]), min(df.loc[t, "low"], df.loc[t, "close"])]
    pasar.bar[KODE] = df
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert h.unduh_penuh is penuh
    assert (CATATAN_DISESUAIKAN_ULANG in h.catatan) is penuh
    if penuh:
        assert pasar.panggilan == [
            (KODE, date(2026, 9, 28), date(2026, 10, 15)),
            (KODE, MULAI_UNDUH, date(2026, 10, 15)),
        ]
        assert any(c.startswith("revisi close 2026-10-06") for c in h.catatan)
    else:
        assert len(pasar.panggilan) == 1
    assert_bar_sama(cache.baca(KODE).bar, kebenaran_sampai(pasar, "2026-10-15"))


def test_revisi_close_mengunduh_ulang_seluruh_riwayat(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    # Sumber menyesuaikan ulang SELURUH riwayat (mis. koreksi data): semua bar lama ikut berubah.
    pasar.bar[KODE] = pasar_harian("2026-09-01", "2026-10-16", harga=980.0)
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert h.status == "OK" and h.unduh_penuh
    assert h.catatan[-1] == CATATAN_DISESUAIKAN_ULANG
    isi = cache.baca(KODE)
    # Bar sebelum jendela cek ulang (< 28 Sep) juga ikut diganti — bukan hanya tumpang tindih.
    assert isi.bar.loc["2026-09-01", "close"] == 980.0
    assert_bar_sama(isi.bar, kebenaran_sampai(pasar, "2026-10-15"))


def test_revisi_volume_dicatat_tanpa_unduh_ulang(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    df = pasar.bar[KODE].copy()
    t = pd.Timestamp("2026-10-06")
    lama_vol = float(df.loc[t, "volume"])
    df.loc[t, "volume"] = lama_vol * 1.10
    t2 = pd.Timestamp("2026-10-07")
    df.loc[t2, "volume"] = float(df.loc[t2, "volume"]) * 1.05  # tepat 5%: tidak dicatat
    pasar.bar[KODE] = df
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert len(pasar.panggilan) == 1 and not h.unduh_penuh
    assert h.revisi_volume == [(date(2026, 10, 6), lama_vol, lama_vol * 1.10)]
    assert any(c.startswith("revisi volume 2026-10-06") for c in h.catatan)
    assert cache.baca(KODE).bar.loc[t, "volume"] == lama_vol * 1.10  # tetap ditimpa


def test_bar_cache_yang_hilang_dari_unduhan_dihapus(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    pasar.bar[KODE] = pasar.bar[KODE].drop(pd.Timestamp("2026-10-07"))
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert "bar 2026-10-07 dihapus (hilang dari unduhan)" in h.catatan
    isi = cache.baca(KODE)
    assert pd.Timestamp("2026-10-07") not in isi.bar.index
    assert not h.unduh_penuh
    # Tidak ada bar yang dibuat: index cache ⊆ index kebenaran.
    assert set(isi.bar.index) <= set(pasar.bar[KODE].index)


def test_bar_rusak_disimpan_apa_adanya_tanpa_diisi(pasar, cache):
    df = pasar.bar[KODE].copy()
    df.loc[pd.Timestamp("2026-10-02"), "close"] = np.nan
    df.loc[pd.Timestamp("2026-10-05"), "volume"] = 0.0
    pasar.bar[KODE] = df
    siapkan_cache_awal(pasar, cache)
    h = perbarui(pasar, cache, "2026-10-15 20:00")  # NaN di cache & unduhan → bukan revisi
    assert not h.unduh_penuh and h.revisi_volume == []
    isi = cache.baca(KODE)
    assert np.isnan(isi.bar.loc["2026-10-02", "close"])
    assert isi.bar.loc["2026-10-05", "volume"] == 0.0
    assert len(isi.bar) == len(kebenaran_sampai(pasar, "2026-10-15"))


def test_close_kosong_menjadi_terisi_dianggap_revisi(pasar, cache):
    df = pasar.bar[KODE].copy()
    df.loc[pd.Timestamp("2026-10-02"), "close"] = np.nan
    pasar.bar[KODE] = df
    siapkan_cache_awal(pasar, cache)
    pasar.bar[KODE].loc[pd.Timestamp("2026-10-02"), "close"] = 1023.0
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert h.unduh_penuh and CATATAN_DISESUAIKAN_ULANG in h.catatan
    assert cache.baca(KODE).bar.loc["2026-10-02", "close"] == 1023.0


# ---------------------------------------------------------------------------
# Split di cache
# ---------------------------------------------------------------------------


def test_split_baru_memicu_unduh_ulang_penuh_lalu_stabil(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    # Split 1:2 pada Senin 12 Okt; sumber menyesuaikan riwayat: harga sebelum 12 Okt ÷ 2, volume × 2.
    s = pd.Timestamp("2026-10-12")
    df = pasar.bar[KODE].copy()
    sebelum = df.index < s
    df.loc[sebelum, ["open", "high", "low", "close"]] = df.loc[sebelum, ["open", "high", "low", "close"]] / 2.0
    df.loc[sebelum, "volume"] = df.loc[sebelum, "volume"] * 2.0
    pasar.bar[KODE] = df
    pasar.split[KODE] = pd.Series([2.0], index=pd.DatetimeIndex([s]))
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert h.unduh_penuh and CATATAN_DISESUAIKAN_ULANG in h.catatan
    assert any(c.startswith("split baru/berubah dalam unduhan (2026-10-12=2)") for c in h.catatan)
    isi = cache.baca(KODE)
    assert_bar_sama(isi.bar, kebenaran_sampai(pasar, "2026-10-15"))
    assert isi.bar.loc[s, "split"] == 2.0 and (isi.bar["split"] != 0).sum() == 1
    assert isi.meta["split_terakhir"] == "2026-10-12"
    # F dari kolom cache: harga nominal 9 Okt = harga tersesuaikan × 2 = harga asli sebelum split.
    sp = split_dari_kolom(isi.bar)
    assert faktor_split(sp, date(2026, 10, 9)) == 2.0 and faktor_split(sp, date(2026, 10, 12)) == 1.0
    asli = pasar_harian("2026-09-01", "2026-10-16").loc["2026-10-09", "close"]
    assert isi.bar.loc["2026-10-09", "close"] * faktor_split(sp, date(2026, 10, 9)) == asli

    # Run berikutnya: split yang sama sudah tercatat → inkremental biasa, tanpa unduh ulang penuh.
    pasar.panggilan.clear()
    h2 = perbarui(pasar, cache, "2026-10-16 20:00")
    assert len(pasar.panggilan) == 1 and not h2.unduh_penuh
    assert CATATAN_DISESUAIKAN_ULANG not in h2.catatan
    assert cache.baca(KODE).bar.loc[s, "split"] == 2.0


def test_split_baru_tanpa_perubahan_harga_tetap_memicu_unduh_ulang(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    pasar.split[KODE] = pd.Series([5.0], index=pd.DatetimeIndex([pd.Timestamp("2026-10-01")]))
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert h.unduh_penuh and len(pasar.panggilan) == 2
    assert cache.baca(KODE).bar.loc["2026-10-01", "split"] == 5.0


def test_split_hilang_dari_unduhan_memicu_unduh_ulang(pasar, cache):
    pasar.split[KODE] = pd.Series([5.0], index=pd.DatetimeIndex([pd.Timestamp("2026-10-01")]))
    siapkan_cache_awal(pasar, cache)
    pasar.split[KODE] = pd.Series(dtype="float64", index=pd.DatetimeIndex([]))
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert h.unduh_penuh and (cache.baca(KODE).bar["split"] == 0.0).all()


def test_split_tanpa_bar_pada_tanggalnya_dicatat_pada_bar_berikutnya(pasar, cache):
    sabtu = pd.Timestamp("2026-10-10")
    pasar.split[KODE] = pd.Series([5.0], index=pd.DatetimeIndex([sabtu]))
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert "split 2026-10-10 (rasio 5) tanpa bar pada tanggalnya: dicatat pada bar 2026-10-12" in h.catatan
    isi = cache.baca(KODE)
    assert isi.bar.loc["2026-10-12", "split"] == 5.0
    # F di setiap tanggal bar sama dengan F dari seri split asli.
    asli = seri_faktor(pasar.split[KODE], isi.bar.index)
    np.testing.assert_array_equal(seri_faktor(split_dari_kolom(isi.bar), isi.bar.index).to_numpy(), asli.to_numpy())
    # Pemetaan stabil: run berikutnya tidak menganggapnya split baru.
    pasar.panggilan.clear()
    h2 = perbarui(pasar, cache, "2026-10-16 20:00")
    assert len(pasar.panggilan) == 1 and not h2.unduh_penuh


# ---------------------------------------------------------------------------
# Bar hari ini & batas bar final (§8.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "saat, tersimpan",
    [
        ("2026-10-15 15:00:00", False),
        ("2026-10-15 19:29:59", False),
        ("2026-10-15 19:30:00", True),  # batas = jadwal.jam_run (jadwal sesi belum terverifikasi)
        ("2026-10-15 22:00:00", True),
    ],
)
def test_bar_hari_ini_hanya_disimpan_setelah_batas_final(pasar, cache, saat, tersimpan):
    siapkan_cache_awal(pasar, cache, "2026-10-14 20:00")
    h = perbarui(pasar, cache, saat)
    assert pasar.panggilan == [(KODE, date(2026, 10, 1), date(2026, 10, 15))]  # akhir tetap hari ini
    isi = cache.baca(KODE)
    assert (pd.Timestamp("2026-10-15") in isi.bar.index) is tersimpan
    assert isi.meta["bar_terakhir"] == ("2026-10-15" if tersimpan else "2026-10-14")
    pesan = "bar 2026-10-15 belum final (sebelum batas bar final): tidak disimpan"
    assert (pesan in h.catatan) is (not tersimpan)


@pytest.mark.parametrize("saat, tersimpan", [("2026-10-15 16:59:59", False), ("2026-10-15 17:00:00", True)])
def test_bar_hari_ini_dengan_jadwal_sesi_terverifikasi_fiktif(pasar, cache, saat, tersimpan):
    cfg = buat_cfg(JADWAL_FIKTIF)
    h = perbarui(pasar, cache, saat, cfg=cfg)  # cache kosong → unduh penuh
    assert h.unduh_penuh
    assert (pd.Timestamp("2026-10-15") in cache.baca(KODE).bar.index) is tersimpan


def test_batas_final_lewat_tengah_malam_bar_kemarin_belum_final(pasar, cache):
    """16:00 + 540 menit → bar 15 Okt baru final 16 Okt 01:00. Pukul 00:30 bar itu belum disimpan dan
    catatannya 'belum final', bukan 'bertanggal sesudah hari ini'."""
    cfg = buat_cfg(JADWAL_FIKTIF, jeda_data_final_menit=540)
    siapkan_cache_awal(pasar, cache, "2026-10-14 20:00")
    h = perbarui(pasar, cache, "2026-10-16 00:30", cfg=cfg)
    assert pd.Timestamp("2026-10-15") not in cache.baca(KODE).bar.index
    assert "bar 2026-10-15 belum final (sebelum batas bar final): tidak disimpan" in h.catatan
    assert not any("2026-10-15 bertanggal sesudah hari ini" in c for c in h.catatan)
    h2 = perbarui(pasar, cache, "2026-10-16 01:00", cfg=cfg)
    assert pd.Timestamp("2026-10-15") in cache.baca(KODE).bar.index and h2.status == "OK"


def test_unduhan_hanya_berisi_bar_belum_final_tidak_mengubah_cache(cache):
    pasar = PasarPalsu()
    pasar.bar["BARU"] = pasar_harian("2026-10-15", "2026-10-15")  # melantai hari ini
    h = perbarui(pasar, cache, "2026-10-15 15:00", kode="BARU")
    assert h.status == "OK" and h.isi.kosong
    assert "tidak ada bar final dalam unduhan: cache tidak diubah" in h.catatan
    assert not cache.folder.exists() or isi_folder(cache) == []


# ---------------------------------------------------------------------------
# Kegagalan provider & struktur
# ---------------------------------------------------------------------------


def test_gagal_jaringan_cache_tidak_diubah(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    sebelum = potret(cache)
    lama = cache.baca(KODE).bar
    pasar.gagal[KODE] = ("JARINGAN", "Failed to connect")
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert (h.status, h.jenis_error, h.pesan) == ("GAGAL", "JARINGAN", "Failed to connect")
    assert not h.dilewati and not h.unduh_penuh
    assert potret(cache) == sebelum
    pd.testing.assert_frame_equal(h.isi.bar, lama)


@pytest.mark.parametrize(
    "rusakkan, potongan_pesan",
    [
        (lambda df: df.rename(index={pd.Timestamp("2026-10-12"): pd.Timestamp("2026-10-10")}), "akhir pekan"),
        (lambda df: df.drop(columns="close"), "kolom wajib hilang: close"),
        (lambda df: df.drop(columns="volume"), "kolom wajib hilang: volume"),  # saham: volume wajib
    ],
)
def test_struktur_tidak_valid_gagal_format_cache_tidak_diubah(pasar, cache, rusakkan, potongan_pesan):
    siapkan_cache_awal(pasar, cache)
    sebelum = potret(cache)
    pasar.bar[KODE] = rusakkan(pasar.bar[KODE]).sort_index()
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert (h.status, h.jenis_error) == ("GAGAL", "FORMAT")
    assert potongan_pesan in h.pesan
    assert potret(cache) == sebelum


def test_unduh_ulang_penuh_yang_gagal_tidak_mengubah_cache(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    sebelum = potret(cache)
    pasar.bar[KODE] = pasar_harian("2026-09-01", "2026-10-16", harga=980.0)  # revisi → unduh penuh
    pasar.gagal_pada[2] = ("JARINGAN", "timed out")
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert len(pasar.panggilan) == 2
    assert (h.status, h.jenis_error) == ("GAGAL", "JARINGAN")
    assert "unduh ulang penuh gagal: cache tidak diubah" in h.catatan
    assert potret(cache) == sebelum


def test_ihsg_tanpa_volume(cache):
    pasar = PasarPalsu()
    pasar.bar["IHSG"] = pasar_harian("2026-09-01", "2026-10-16", harga=7000.0, dengan_volume=False)
    h = perbarui(pasar, cache, "2026-10-09 20:00", kode="IHSG", adalah_indeks=True)
    assert h.status == "OK"
    isi = cache.baca("IHSG")
    assert isi.bar["volume"].isna().all() and len(isi.bar) == 29
    h2 = perbarui(pasar, cache, "2026-10-15 20:00", kode="IHSG", adalah_indeks=True)
    assert h2.status == "OK" and not h2.unduh_penuh and h2.revisi_volume == []
    assert cache.baca("IHSG").meta["bar_terakhir"] == "2026-10-15"


# ---------------------------------------------------------------------------
# TIDAK_ADA_DATA: tidak dicoba lagi sebelum N hari
# ---------------------------------------------------------------------------


def test_tidak_ada_data_tidak_dicoba_lagi_sebelum_n_hari(cache):
    pasar = PasarPalsu()
    pasar.gagal["HILANG"] = ("TIDAK_ADA_DATA", "possibly delisted")

    h1 = perbarui(pasar, cache, "2026-10-08 20:00", kode="HILANG")
    assert (h1.status, h1.jenis_error, h1.dilewati) == ("GAGAL", "TIDAK_ADA_DATA", False)
    assert len(pasar.panggilan) == 1
    assert isi_folder(cache) == ["HILANG.meta.json"]  # hanya meta, tanpa parquet
    isi = cache.baca("HILANG")
    assert not isi.rusak and isi.bar is None
    assert isi.meta["tidak_ada_data_sejak"] == "2026-10-08" and isi.meta["jumlah_bar"] == 0

    # 6 hari kemudian (< 7): dilewati, tidak ada permintaan.
    h2 = perbarui(pasar, cache, "2026-10-14 20:00", kode="HILANG")
    assert (h2.status, h2.jenis_error, h2.dilewati) == ("GAGAL", "TIDAK_ADA_DATA", True)
    assert "2026-10-15" in h2.pesan
    assert len(pasar.panggilan) == 1

    # Tepat 7 hari: dicoba lagi. Masih gagal → tanggal "sejak" tidak berubah.
    h3 = perbarui(pasar, cache, "2026-10-15 20:00", kode="HILANG")
    assert len(pasar.panggilan) == 2 and not h3.dilewati
    assert cache.baca("HILANG").meta["tidak_ada_data_sejak"] == "2026-10-08"

    # Data muncul kembali → unduh penuh, penanda dihapus.
    del pasar.gagal["HILANG"]
    pasar.bar["HILANG"] = pasar_harian("2026-09-01", "2026-10-16")
    h4 = perbarui(pasar, cache, "2026-10-15 20:00", kode="HILANG")
    assert h4.status == "OK" and h4.unduh_penuh
    assert pasar.panggilan[-1] == ("HILANG", MULAI_UNDUH, date(2026, 10, 15))
    assert cache.baca("HILANG").meta["tidak_ada_data_sejak"] is None


def test_tidak_ada_data_dengan_cache_lama_bar_dipertahankan(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    sebelum_parquet, _ = potret(cache)
    lama = cache.baca(KODE).bar
    pasar.gagal[KODE] = ("TIDAK_ADA_DATA", "symbol may be delisted")
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert (h.status, h.jenis_error) == ("GAGAL", "TIDAK_ADA_DATA")
    pd.testing.assert_frame_equal(h.isi.bar, lama)
    assert potret(cache)[0] == sebelum_parquet  # parquet tidak disentuh
    isi = cache.baca(KODE)
    assert isi.meta["tidak_ada_data_sejak"] == "2026-10-15"
    assert isi.meta["jumlah_bar"] == 29 and isi.meta["diambil_pada"] == "2026-10-09T20:00:00+07:00"
    pasar.panggilan.clear()
    h2 = perbarui(pasar, cache, "2026-10-16 20:00")
    assert h2.dilewati and pasar.panggilan == []
    pd.testing.assert_frame_equal(h2.isi.bar, lama)


def test_jeda_coba_lagi_dari_config(cache):
    cfg2 = buat_cfg(coba_lagi_tidak_ada_data_hari=2)
    pasar = PasarPalsu()
    pasar.gagal["HILANG"] = ("TIDAK_ADA_DATA", "No data found")
    perbarui(pasar, cache, "2026-10-08 20:00", kode="HILANG", cfg=cfg2)
    assert perbarui(pasar, cache, "2026-10-09 20:00", kode="HILANG", cfg=cfg2).dilewati
    assert not perbarui(pasar, cache, "2026-10-10 20:00", kode="HILANG", cfg=cfg2).dilewati
    assert len(pasar.panggilan) == 2


# ---------------------------------------------------------------------------
# Penulisan atomik
# ---------------------------------------------------------------------------


def test_penulisan_atomik_tidak_meninggalkan_berkas_sementara(pasar, cache):
    siapkan_cache_awal(pasar, cache)
    for saat in ("2026-10-12 20:00", "2026-10-13 20:00", "2026-10-15 20:00"):
        perbarui(pasar, cache, saat)
    assert isi_folder(cache) == ["ABCD.meta.json", "ABCD.parquet"]


def _gagalkan_replace_untuk(monkeypatch, akhiran: str):
    asli = os.replace

    def replace_palsu(src, dst, *a, **kw):
        if str(dst).endswith(akhiran):
            raise OSError(28, "No space left on device (simulasi)")
        return asli(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", replace_palsu)


def test_gagal_saat_mengganti_parquet_berkas_lama_utuh(pasar, cache, monkeypatch):
    siapkan_cache_awal(pasar, cache)
    sebelum = potret(cache)
    _gagalkan_replace_untuk(monkeypatch, ".parquet")
    with pytest.raises(OSError):
        perbarui(pasar, cache, "2026-10-15 20:00")
    monkeypatch.undo()
    assert potret(cache) == sebelum
    assert isi_folder(cache) == ["ABCD.meta.json", "ABCD.parquet"]  # berkas sementara dibersihkan
    isi = cache.baca(KODE)
    assert not isi.rusak and isi.meta["bar_terakhir"] == "2026-10-09"


def test_gagal_di_tengah_menulis_isi_berkas_lama_utuh(pasar, cache, monkeypatch):
    siapkan_cache_awal(pasar, cache)
    sebelum = potret(cache)

    def fsync_gagal(fd):
        raise OSError(5, "Input/output error (simulasi)")

    monkeypatch.setattr(os, "fsync", fsync_gagal)
    with pytest.raises(OSError):
        perbarui(pasar, cache, "2026-10-15 20:00")
    monkeypatch.undo()
    assert potret(cache) == sebelum
    assert isi_folder(cache) == ["ABCD.meta.json", "ABCD.parquet"]


def test_gagal_serialisasi_pyarrow_berkas_lama_utuh(pasar, cache, monkeypatch):
    import pyarrow.parquet as pq

    siapkan_cache_awal(pasar, cache)
    sebelum = potret(cache)

    def tulis_gagal(*a, **kw):
        raise RuntimeError("pyarrow gagal (simulasi)")

    monkeypatch.setattr(pq, "write_table", tulis_gagal)
    with pytest.raises(RuntimeError):
        perbarui(pasar, cache, "2026-10-15 20:00")
    monkeypatch.undo()
    assert potret(cache) == sebelum and isi_folder(cache) == ["ABCD.meta.json", "ABCD.parquet"]


def test_gagal_saat_menulis_meta_parquet_baru_tetap_terbaca(pasar, cache, monkeypatch):
    siapkan_cache_awal(pasar, cache)
    _, meta_lama = potret(cache)
    _gagalkan_replace_untuk(monkeypatch, ".meta.json")
    with pytest.raises(OSError):
        perbarui(pasar, cache, "2026-10-15 20:00")
    monkeypatch.undo()
    assert potret(cache)[1] == meta_lama
    isi = cache.baca(KODE)
    assert not isi.rusak  # parquet = sumber kebenaran
    assert isi.bar.index[-1] == pd.Timestamp("2026-10-15")
    assert "tidak cocok dengan parquet" in isi.pesan
    # Run berikutnya memperbaiki meta.
    h = perbarui(pasar, cache, "2026-10-15 21:00")
    assert any("tidak cocok dengan parquet" in c for c in h.catatan)
    isi2 = cache.baca(KODE)
    assert isi2.pesan == "" and isi2.meta["bar_terakhir"] == "2026-10-15"


# ---------------------------------------------------------------------------
# Cache rusak → diperlakukan kosong → unduh ulang penuh
# ---------------------------------------------------------------------------


def _parquet_tanpa_kolom_split(cache: CacheProvider):
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = cache.baca(KODE).bar.drop(columns="split")
    pq.write_table(pa.Table.from_pandas(df), cache.path_parquet(KODE))


def _parquet_tanggal_ganda(cache: CacheProvider):
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = cache.baca(KODE).bar
    df = pd.concat([df, df.iloc[[-1]]])
    pq.write_table(pa.Table.from_pandas(df), cache.path_parquet(KODE))


PERUSAK = {
    "parquet_sampah": lambda c: c.path_parquet(KODE).write_bytes(b"ini bukan parquet" * 20),
    "parquet_terpotong": lambda c: c.path_parquet(KODE).write_bytes(c.path_parquet(KODE).read_bytes()[:200]),
    "parquet_kosong": lambda c: c.path_parquet(KODE).write_bytes(b""),
    "parquet_tanpa_kolom_split": _parquet_tanpa_kolom_split,
    "parquet_tanggal_ganda": _parquet_tanggal_ganda,
    "meta_json_rusak": lambda c: c.path_meta(KODE).write_text("{bukan json", encoding="utf-8"),
    "meta_kunci_hilang": lambda c: c.path_meta(KODE).write_text('{"jumlah_bar": 29}', encoding="utf-8"),
    "meta_tanggal_salah": lambda c: c.path_meta(KODE).write_text(
        c.path_meta(KODE).read_text(encoding="utf-8").replace('"2026-10-09"', '"09/10/2026"'), encoding="utf-8"
    ),
    "meta_hilang": lambda c: c.path_meta(KODE).unlink(),
    "parquet_hilang": lambda c: c.path_parquet(KODE).unlink(),
}


@pytest.mark.parametrize("jenis", sorted(PERUSAK))
def test_cache_rusak_diunduh_ulang_penuh(pasar, cache, jenis):
    siapkan_cache_awal(pasar, cache)
    PERUSAK[jenis](cache)
    isi = cache.baca(KODE)
    assert isi.rusak and isi.bar is None and isi.pesan
    h = perbarui(pasar, cache, "2026-10-15 20:00")
    assert pasar.panggilan == [(KODE, MULAI_UNDUH, date(2026, 10, 15))]
    assert h.status == "OK" and h.unduh_penuh
    assert any("rusak" in c and "diunduh ulang penuh" in c for c in h.catatan)
    isi2 = cache.baca(KODE)
    assert not isi2.rusak and isi2.pesan == ""
    assert_bar_sama(isi2.bar, kebenaran_sampai(pasar, "2026-10-15"))


# ---------------------------------------------------------------------------
# CacheProvider langsung
# ---------------------------------------------------------------------------


def _bingkai_contoh() -> pd.DataFrame:
    df = pasar_harian("2026-10-05", "2026-10-09")
    df["sumber"] = "yfinance"
    df["split"] = [0.0, 0.0, 5.0, 0.0, 0.0]
    return df


def test_tulis_baca_round_trip_dan_byte_identik(cache):
    df = _bingkai_contoh()
    meta = buat_meta(df, sekarang=wib("2026-10-09 20:00"), versi_provider="v")
    assert meta["split_terakhir"] == "2026-10-07"
    cache.tulis("XYZ", df, meta)
    pertama = potret(cache, "XYZ")
    isi = cache.baca("XYZ")
    assert isinstance(isi, IsiCache) and not isi.rusak and isi.meta == meta
    assert str(isi.bar.index.dtype) == "datetime64[ns]" and isi.bar.index.name == NAMA_INDEX
    assert list(isi.bar.columns) == list(KOLOM_CACHE)
    assert_bar_sama(isi.bar, df)
    assert isi.bar["split"].tolist() == [0.0, 0.0, 5.0, 0.0, 0.0]
    cache.tulis("XYZ", isi.bar, meta)  # tulis ulang isi yang sama → byte-identik (deterministik)
    assert potret(cache, "XYZ") == pertama
    assert cache.path_meta("XYZ").read_text(encoding="utf-8") == teks_meta(meta)


def test_baca_parquet_tanpa_metadata_pandas_dan_unit_mikrodetik(cache):
    """Parquet dari penulis lain (mis. pembangkit fixture): index tersimpan sebagai kolom `tanggal`
    bertipe timestamp[us], tanpa metadata pandas, `sumber` berupa kategori → tetap terbaca standar."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = _bingkai_contoh()
    df["sumber"] = df["sumber"].astype("category")
    df.index = df.index.as_unit("us")
    tabel = pa.Table.from_pandas(df.reset_index(), preserve_index=False).replace_schema_metadata(None)
    cache.folder.mkdir(parents=True)
    pq.write_table(tabel, cache.path_parquet("XYZ"), compression="zstd")
    meta = buat_meta(df, sekarang=wib("2026-10-09 20:00"), versi_provider="fixture")
    cache.path_meta("XYZ").write_text(teks_meta(meta), encoding="utf-8")
    isi = cache.baca("XYZ")
    assert not isi.rusak and isi.pesan == ""
    assert str(isi.bar.index.dtype) == "datetime64[ns]" and isi.bar.index.name == NAMA_INDEX
    assert list(isi.bar.columns) == list(KOLOM_CACHE)
    assert_bar_sama(isi.bar, _bingkai_contoh())
    assert isi.bar["sumber"].tolist() == ["yfinance"] * 5


def test_tulis_menolak_bingkai_atau_meta_tidak_sah(cache):
    meta = buat_meta(_bingkai_contoh(), sekarang=wib("2026-10-09 20:00"), versi_provider="v")
    with pytest.raises(ValueError, match="kolom hilang"):
        cache.tulis("XYZ", _bingkai_contoh().drop(columns="sumber"), meta)
    ganda = pd.concat([_bingkai_contoh(), _bingkai_contoh().iloc[[0]]])
    with pytest.raises(ValueError, match="duplikat"):
        cache.tulis("XYZ", ganda, meta)
    with pytest.raises(ValueError, match="kunci meta"):
        cache.tulis("XYZ", _bingkai_contoh(), {**meta, "lain": 1})
    assert not cache.folder.exists() or isi_folder(cache) == []


def test_baca_tanpa_berkas_dan_daftar_kode(cache, pasar):
    isi = cache.baca("TIDAKADA")
    assert (isi.bar, isi.meta, isi.rusak, isi.pesan) == (None, None, False, "")
    assert isi.kosong
    assert cache.daftar_kode() == []
    siapkan_cache_awal(pasar, cache)
    pasar.gagal["HILANG"] = ("TIDAK_ADA_DATA", "No data found")
    perbarui(pasar, cache, "2026-10-09 20:00", kode="HILANG")
    (cache.folder / ".ABCD.parquet.abc123.tmp").write_bytes(b"sisa")  # berkas sementara diabaikan
    assert cache.daftar_kode() == ["ABCD", "HILANG"]
