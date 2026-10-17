"""Kontrak bersama strategi (SPEC §12 pemeriksaan STOP, §8.4 F(d)): `Strategi._bangun_sinyal`,
`nilai_baris`, dan faktor split yang dipakai level nominal."""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd
import pytest

from asisten_bei.aturan_bei import AturanBEI
from asisten_bei.strategi.dasar import CekAturan, Strategi, nilai_baris
from bantu_pasar import HARI, bar, cfg_uji, naik, pasar, tgl

CFG = cfg_uji({"pasar.fraksi_harga.terverifikasi": False, "entry.gap_maks_atr": 0.5})
N = 60
T = tgl(N - 1)


class _Uji(Strategi):
    """Strategi uji: semua aturan lolos, level dari parameter (ruang tersesuaikan)."""

    kode = "PB"

    def __init__(self, cfg, *, close, atr, stop, maks_jarak=None):
        super().__init__(cfg)
        self._close, self._atr, self._stop, self._maks = close, atr, stop, maks_jarak

    def evaluasi(self, dv, kode, bei):
        cek = [CekAturan("PB1", "uji", True)]
        return self._bangun_sinyal(
            dv, kode, cek, close=self._close, atr=self._atr, stop_tersesuaikan=self._stop,
            k_sinyal=0.5, target_r=2.0, rata_nilai=1e10, maks_jarak_stop_atr=self._maks,
        )


def _dv(split=None):
    return pasar({"UJI": bar(naik(N))}, None, CFG, T, split={"UJI": split} if split is not None else None).tampilan(T)


def _ev(**kw):
    return _Uji(CFG, **kw).evaluasi(_dv(), "UJI", AturanBEI(CFG))


def test_level_normal_dibulatkan_arah_benar():
    # harga_maks = ⌊1000 + 0,5 × 20⌋ = 1010; stop = ⌈949,2⌉ = 950
    ev = _ev(close=1000.0, atr=20.0, stop=949.2)
    s = ev.sinyal
    assert s is not None and ev.aturan_gagal == []
    assert (s.harga_maks, s.stop, s.harga_ref, s.atr) == (Decimal(1010), Decimal(950), Decimal(1000), Decimal(20))


@pytest.mark.parametrize(
    "kw",
    [
        dict(close=1000.0, atr=20.0, stop=math.nan),  # stop tidak terdefinisi
        dict(close=1000.0, atr=math.nan, stop=900.0),  # ATR NaN
        dict(close=1000.0, atr=0.0, stop=900.0),  # ATR = 0
        dict(close=1000.0, atr=-1.0, stop=900.0),  # ATR < 0
        dict(close=10.0, atr=20.0, stop=10.0 - 2.5 * 20.0),  # stop ≤ 0 (saham murah sangat volatil)
        dict(close=10.0, atr=20.0, stop=0.0),  # stop = 0 tepat
        dict(close=math.nan, atr=20.0, stop=900.0),  # close NaN
    ],
    ids=["stop-nan", "atr-nan", "atr-nol", "atr-negatif", "stop-negatif", "stop-nol", "close-nan"],
)
def test_stop_tidak_sah_gagal_cek_stop_tanpa_pengecualian(kw):
    ev = _ev(**kw)
    assert ev.sinyal is None
    assert ev.aturan_gagal == ["STOP"]
    assert [c.kode for c in ev.cek] == ["PB1", "STOP"]


@pytest.mark.parametrize("stop", [1e-9, 4e-7, 7.1e-15])
def test_stop_derau_float_di_atas_nol_gagal_cek_stop(stop):
    # stop > 0 dalam float tetapi 0 setelah dibersihkan ke 6 desimal → cek STOP gagal, bukan ValueError.
    ev = _ev(close=1000.0, atr=20.0, stop=stop)
    assert ev.sinyal is None and ev.aturan_gagal == ["STOP"]


def test_stop_tepat_di_atas_nol_masih_sinyal():
    ev = _ev(close=10.0, atr=4.0, stop=0.4)  # stop ⌈0,4⌉ = 1, harga_maks ⌊12⌋ = 12
    assert ev.sinyal is not None and ev.sinyal.stop == Decimal(1) and ev.sinyal.harga_maks == Decimal(12)


def test_stop_sama_dengan_harga_maks_setelah_pembulatan_gagal():
    # harga_maks ⌊1000 + 0,5 × 0,4⌋ = 1000; stop ⌈999,1⌉ = 1000 → stop ≥ harga_maks
    ev = _ev(close=1000.0, atr=0.4, stop=999.1)
    assert ev.sinyal is None and ev.aturan_gagal == ["STOP"]
    # satu rupiah di bawah: stop ⌈998,9⌉ = 999 < 1000 → sinyal
    assert _ev(close=1000.0, atr=0.4, stop=998.9).sinyal is not None


def test_alasan_stop_terlalu_jauh_tiga_desimal():
    # harga_maks 1010, stop 950 → jarak 60/20 = 3,000 ATR > 2,999
    s = _ev(close=1000.0, atr=20.0, stop=949.2, maks_jarak=2.999).sinyal
    assert s is not None and not s.valid
    assert "3,000 ATR > 2,999 ATR" in s.alasan_tidak_valid
    assert _ev(close=1000.0, atr=20.0, stop=949.2, maks_jarak=3.0).sinyal.valid


def test_nilai_baris_kolom_hilang_adalah_kesalahan_program():
    baris = pd.Series({"close": 1.0})
    assert nilai_baris(baris, "close") == 1.0
    assert math.isnan(nilai_baris(None, "close"))
    with pytest.raises(KeyError, match="config yang sama"):
        nilai_baris(baris, "sma_999")


def test_faktor_split_mengabaikan_split_yang_belum_disesuaikan_sumber():
    """§8.4: split yang belum disesuaikan sumber tidak tertanam di harga cache → tidak ikut F(d)."""
    s = pd.Series([5.0], index=pd.DatetimeIndex([HARI[40]], name="tanggal").as_unit("ns"), name="split")
    # Disesuaikan: harga sebelum split sudah dibagi 5 (tidak ada lompatan) → F = 5 sebelum split.
    disesuaikan = pasar({"UJI": bar(naik(N))}, None, CFG, T, split={"UJI": s})
    assert disesuaikan.tampilan(tgl(39)).faktor_split("UJI") == 5.0
    assert disesuaikan.tampilan(tgl(40)).faktor_split("UJI") == 1.0
    # Belum disesuaikan: harga sebelum split nominal (5× sesudahnya) → F = 1, dan saham dikeluarkan
    # sejak tanggal split dengan alasan SPLIT_BELUM_DISESUAIKAN.
    close = [5000.0] * 40 + [1000.0] * (N - 40)
    mentah = pasar({"UJI": bar(close)}, None, CFG, T, split={"UJI": s})
    assert mentah.tampilan(tgl(39)).faktor_split("UJI") == 1.0
    assert mentah.tampilan(tgl(39)).nominal("UJI", 5000.0) == 5000.0
    assert mentah.tampilan(tgl(40)).alasan_data("UJI") == "SPLIT_BELUM_DISESUAIKAN"
