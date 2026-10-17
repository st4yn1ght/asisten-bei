"""T-BIAYA: fee, slippage, P&L bersih, target, dan R:R bersih (SPEC §4.1, §7 `biaya`, §14.1–§14.2, §15.1).

Semua angka acuan dihitung tangan dari SPEC (aritmetika ditulis di komentar), bukan disalin dari
keluaran implementasi. Config dasar = `config.yaml` rilis (fee 0,15%/0,25%, slippage 0,10%,
`fee_minimum_idr: 0`, `pembulatan_fee: null`, lot 100); setiap penyimpangan ditulis eksplisit.
Fee minimum dan tabel fraksi di berkas ini adalah FIKTIF – KHUSUS UJI, bukan aturan nyata.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from asisten_bei import biaya
from asisten_bei.aturan_bei import AturanBEI
from asisten_bei.strategi.dasar import hitung_target
from bantu_pasar import cfg_uji

D = Decimal
CFG = cfg_uji()  # default rilis
CFG_SLIP0 = cfg_uji({"biaya.slippage_pct": 0})  # contoh fee §14.2 memakai slippage 0
T = date(2026, 10, 15)  # tanggal sembarang; fraksi belum diverifikasi → rupiah bulat
L = 100  # pasar.lot_lembar default (diperiksa di test_config_default_sesuai_asumsi_spec)


def _cfg(**ubah) -> object:
    """Config rilis dengan perubahan `biaya.*` (kunci tanpa awalan)."""
    return cfg_uji({f"biaya.{k}": v for k, v in ubah.items()})


# ---------------------------------------------------------------------------
# Prasyarat: asumsi §14.2 = default config.yaml
# ---------------------------------------------------------------------------


def test_config_default_sesuai_asumsi_spec():
    assert biaya.fb(CFG) == D("0.0015")  # 0,15% → 0,0015
    assert biaya.fj(CFG) == D("0.0025")  # 0,25% → 0,0025
    assert biaya.slip(CFG) == D("0.001")  # 0,10% → 0,001
    assert biaya.slip(CFG_SLIP0) == 0
    assert CFG.biaya.fee_minimum_idr == 0
    assert CFG.biaya.pembulatan_fee is None
    assert CFG.pasar.lot_lembar == L
    assert all(isinstance(x, Decimal) for x in (biaya.fb(CFG), biaya.fj(CFG), biaya.slip(CFG)))


# ---------------------------------------------------------------------------
# Contoh fee §14.2 (slippage 0): beli 1 lot di 2.030, jual di 2.314 → P&L Rp 27.517
# ---------------------------------------------------------------------------


def test_contoh_fee_spec_14_2_pnl_27517():
    # Beli: order limit di harga_maks 2.030, open 2.030, slippage 0 → isi 2.030.
    isi_beli = biaya.harga_isi_beli(D(2030), D(2030), CFG_SLIP0)
    assert isi_beli == D(2030)
    # Nilai 100 × 2.030 = 203.000; fee 0,0015 × 203.000 = 304,50; kas −203.304,50.
    assert biaya.fee_beli(D(203_000), CFG_SLIP0) == D("304.50")
    assert biaya.biaya_beli(L, isi_beli, CFG_SLIP0) == D("203304.50")
    # Jual di target 2.314 (limit, tanpa slippage).
    isi_jual = biaya.harga_isi_jual(D(2314), CFG_SLIP0, slippage=False)
    assert isi_jual == D(2314)
    # Nilai 100 × 2.314 = 231.400; fee 0,0025 × 231.400 = 578,50; kas +230.821,50.
    assert biaya.fee_jual(D(231_400), CFG_SLIP0) == D("578.50")
    assert biaya.hasil_jual(L, isi_jual, CFG_SLIP0) == D("230821.50")
    # P&L = 230.821,50 − 203.304,50 = 27.517,00.
    pnl = biaya.pnl_bersih(L, isi_beli, isi_jual, CFG_SLIP0)
    assert isinstance(pnl, Decimal)
    assert pnl == D(27_517)


def test_contoh_fee_tidak_dibulatkan_saat_pembulatan_null():
    # null = tidak dibulatkan: 304,50 dan 578,50 tetap berpecahan (bukan 305/579 atau 304/578).
    assert biaya.fee_beli(D(203_000), CFG) == D("304.5")
    assert biaya.fee_jual(D(231_400), CFG) == D("578.5")
    assert biaya.fee_beli(D(202_800), CFG) == D("304.2")  # 0,0015 × 202.800


@pytest.mark.parametrize("mode", ["rupiah_terdekat", "rupiah_ke_atas"])
def test_contoh_fee_dengan_pembulatan_fee(mode):
    # 304,50 → 305 dan 578,50 → 579 pada kedua mode (HALF_UP / CEILING).
    # P&L = (231.400 − 579) − (203.000 + 305) = 230.821 − 203.305 = 27.516.
    cfg = _cfg(slippage_pct=0, pembulatan_fee=mode)
    assert biaya.fee_beli(D(203_000), cfg) == D(305)
    assert biaya.fee_jual(D(231_400), cfg) == D(579)
    assert biaya.biaya_beli(L, D(2030), cfg) == D(203_305)
    assert biaya.hasil_jual(L, D(2314), cfg) == D(230_821)
    assert biaya.pnl_bersih(L, D(2030), D(2314), cfg) == D(27_516)


@pytest.mark.parametrize(
    "nilai, pct_fee, mode, harapan",
    [
        # Beli 0,15%:
        (202_800, "beli", None, "304.2"),  # 0,0015 × 202.800 = 304,2
        (202_800, "beli", "rupiah_terdekat", "304"),  # 304,2 → 304
        (202_800, "beli", "rupiah_ke_atas", "305"),  # 304,2 → 305 (selalu ke atas)
        (203_000, "beli", "rupiah_terdekat", "305"),  # 304,5 → 305 (HALF_UP; HALF_EVEN = 304)
        (203_200, "beli", "rupiah_terdekat", "305"),  # 304,8 → 305
        (203_200, "beli", "rupiah_ke_atas", "305"),  # 304,8 → 305
        (200_000, "beli", "rupiah_ke_atas", "300"),  # 300 tepat → tetap 300 (tidak naik 1)
        (200_000, "beli", "rupiah_terdekat", "300"),
        # Jual 0,25%:
        (231_400, "jual", "rupiah_terdekat", "579"),  # 578,5 → 579 (HALF_UP; HALF_EVEN = 578)
        (231_200, "jual", "rupiah_ke_atas", "578"),  # 578 tepat → 578
        (231_240, "jual", "rupiah_terdekat", "578"),  # 578,1 → 578
        (231_240, "jual", "rupiah_ke_atas", "579"),  # 578,1 → 579
        (231_480, "jual", "rupiah_terdekat", "579"),  # 578,7 → 579
    ],
)
def test_pembulatan_fee_arah_dan_half_up(nilai, pct_fee, mode, harapan):
    cfg = _cfg(pembulatan_fee=mode)
    fungsi = biaya.fee_beli if pct_fee == "beli" else biaya.fee_jual
    assert fungsi(D(nilai), cfg) == D(harapan)


# ---------------------------------------------------------------------------
# Fee minimum (FIKTIF – KHUSUS UJI): max(pct × nilai, m), sekali per transaksi
# ---------------------------------------------------------------------------


def test_fee_minimum_berlaku_per_transaksi_bukan_per_lot():
    cfg = _cfg(fee_minimum_idr=5000)  # FIKTIF – KHUSUS UJI
    # 1 lot A: 0,0015 × 203.000 = 304,50 < 5.000 → 5.000.
    assert biaya.fee_beli(D(203_000), cfg) == D(5000)
    # 2 lot dalam SATU order: 0,0015 × 406.000 = 609 < 5.000 → 5.000 (bukan 2 × 5.000).
    assert biaya.fee_beli(D(406_000), cfg) == D(5000)
    assert biaya.biaya_beli(2 * L, D(2030), cfg) == D(411_000)  # §14.2: biaya 411.000
    # Di atas minimum: 0,0015 × 4.000.000 = 6.000 > 5.000 → 6.000.
    assert biaya.fee_beli(D(4_000_000), cfg) == D(6000)
    # Jual 0,25%: 0,0025 × 2.400.000 = 6.000 > 5.000.
    assert biaya.fee_jual(D(2_400_000), cfg) == D(6000)
    assert biaya.fee_jual(D(231_400), cfg) == D(5000)


@pytest.mark.parametrize(
    "nilai, harapan",
    [
        (1_999_900, "3000"),  # 0,0015 × 1.999.900 = 2.999,85 < 3.000 → minimum
        (2_000_000, "3000"),  # tepat di batas: 3.000 = 3.000
        (2_000_100, "3000.15"),  # 3.000,15 > 3.000 → persen
    ],
)
def test_fee_minimum_batas_kedua_sisi(nilai, harapan):
    cfg = _cfg(fee_minimum_idr=3000)  # FIKTIF – KHUSUS UJI
    assert biaya.fee_beli(D(nilai), cfg) == D(harapan)


def test_contoh_fee_minimum_spec_14_2_kandidat_a():
    # §14.2 "Fee minimum fiktif", m = 5.000, P = 2.030, S = 1.888, slippage 0,10%.
    cfg = _cfg(fee_minimum_idr=5000)  # FIKTIF – KHUSUS UJI
    stop_slip = biaya.harga_isi_jual(D(1888), cfg)  # 1.888 × 0,999 = 1.886,112
    assert stop_slip == D("1886.112")
    # n = 2: hasil stop = 200 × 1.886,112 − max(0,0025 × 377.222,4 = 943,056; 5.000) = 372.222,40.
    assert biaya.hasil_jual(2 * L, stop_slip, cfg) == D("372222.40")
    assert biaya.risiko_posisi(2 * L, D(2030), D(1888), cfg) == D("38777.60")  # 411.000 − 372.222,40
    # n = 1: 208.000 − (188.611,2 − 5.000 = 183.611,20) = 24.388,80.
    assert biaya.biaya_beli(L, D(2030), cfg) == D(208_000)
    assert biaya.hasil_jual(L, stop_slip, cfg) == D("183611.20")
    assert biaya.risiko_posisi(L, D(2030), D(1888), cfg) == D("24388.80")


def test_fee_minimum_lalu_pembulatan_tetap_rupiah_bulat():
    # Pembulatan berlaku pada fee yang dibayar (setelah minimum), sehingga fee selalu rupiah bulat.
    cfg = _cfg(fee_minimum_idr=5000, pembulatan_fee="rupiah_terdekat")  # FIKTIF – KHUSUS UJI
    assert biaya.fee_beli(D(203_000), cfg) == D(5000)
    cfg = _cfg(fee_minimum_idr="4999.5", pembulatan_fee="rupiah_terdekat")  # FIKTIF – KHUSUS UJI
    assert biaya.fee_beli(D(203_000), cfg) == D(5000)  # max(304,5; 4.999,5) = 4.999,5 → 5.000
    cfg = _cfg(fee_minimum_idr="4999.2", pembulatan_fee="rupiah_ke_atas")  # FIKTIF – KHUSUS UJI
    assert biaya.fee_jual(D(231_400), cfg) == D(5000)  # max(578,5; 4.999,2) → 5.000


# ---------------------------------------------------------------------------
# Slippage: harga isi beli dan jual (§4.1 harga_beli, §15.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "open_, harga_maks, slip0, harapan",
    [
        (2000, 2030, False, "2002.000"),  # 2.000 × 1,001 = 2.002 < 2.030
        (2027, 2030, False, "2029.027"),  # 2.027 × 1,001 = 2.029,027 < 2.030 (tidak dibulatkan)
        (2028, 2030, False, "2030"),  # 2.028 × 1,001 = 2.030,028 > 2.030 → dipotong limit
        (2030, 2030, False, "2030"),  # open = harga_maks → 2.032,03 → harga isi = harga_maks
        (2015, 2030, True, "2015"),  # slippage 0 → open
        (2030, 2030, True, "2030"),  # slippage 0, open = harga_maks
    ],
)
def test_harga_isi_beli_min_open_slip_dan_harga_maks(open_, harga_maks, slip0, harapan):
    cfg = CFG_SLIP0 if slip0 else CFG
    isi = biaya.harga_isi_beli(D(open_), D(harga_maks), cfg)
    assert isinstance(isi, Decimal)
    assert isi == D(harapan)
    assert isi <= D(harga_maks)  # order limit tidak pernah terisi di atas limit


@pytest.mark.parametrize(
    "harga, slippage, slip0, harapan",
    [
        (1888, True, False, "1886.112"),  # stop: 1.888 × 0,999
        (1850, True, False, "1848.150"),  # gap di bawah stop: open 1.850 × 0,999
        (2314, False, False, "2314"),  # target (limit): tanpa slippage
        (1888, True, True, "1888"),  # slippage 0
        (2314, False, True, "2314"),
    ],
)
def test_harga_isi_jual_dengan_dan_tanpa_slippage(harga, slippage, slip0, harapan):
    cfg = CFG_SLIP0 if slip0 else CFG
    assert biaya.harga_isi_jual(D(harga), cfg, slippage=slippage) == D(harapan)


def test_harga_isi_jual_default_memakai_slippage():
    assert biaya.harga_isi_jual(D(1888), CFG) == D("1886.112")


# ---------------------------------------------------------------------------
# risiko_lembar dan risiko per lot (§4.1, §14.2 baris "risiko_lembar")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "P, S, p_fb, s_bersih, rl, per_lot",
    [
        # A: 2.030 × 1,0015 = 2.033,045; 1.888 × 0,999 × 0,9975 = 1.881,39672
        (2030, 1888, "2033.045", "1881.39672", "151.64828", "15164.828"),
        # B: 9.125 × 1,0015 = 9.138,6875; 8.500 × 0,999 × 0,9975 = 8.470,27125
        (9125, 8500, "9138.6875", "8470.27125", "668.41625", "66841.625"),
        # C: 458 × 1,0015 = 458,687; 410 × 0,999 × 0,9975 = 408,566025
        (458, 410, "458.687", "408.566025", "50.120975", "5012.0975"),
    ],
)
def test_risiko_lembar_contoh_14_2(P, S, p_fb, s_bersih, rl, per_lot):
    assert D(P) * (1 + biaya.fb(CFG)) == D(p_fb)
    assert D(S) * (1 - biaya.slip(CFG)) * (1 - biaya.fj(CFG)) == D(s_bersih)
    assert biaya.risiko_lembar(D(P), D(S), CFG) == D(rl)
    # m = 0 → risiko(1 lot) = L × risiko_lembar (biaya beli − hasil jual di stop dengan slippage).
    assert biaya.risiko_posisi(L, D(P), D(S), CFG) == D(per_lot)


def test_pnl_di_stop_setelah_isi_di_harga_maks_sama_dengan_minus_risiko():
    # Jaminan §14.1: isi di harga_maks lalu keluar di stop × (1 − slip) → rugi = risiko(1) = 15.164,828.
    stop_slip = biaya.harga_isi_jual(D(1888), CFG)
    assert biaya.pnl_bersih(L, D(2030), stop_slip, CFG) == D("-15164.828")
    assert biaya.pnl_bersih(L, D(2030), stop_slip, CFG) == -biaya.risiko_posisi(L, D(2030), D(1888), CFG)


def test_pnl_dengan_slippage_kedua_sisi():
    # Isi beli: open 2.000 × 1,001 = 2.002 → biaya 200.200 + 300,30 = 200.500,30.
    # Jual di stop: 1.888 × 0,999 = 1.886,112 → 188.611,2 − 471,528 = 188.139,672.
    # P&L = 188.139,672 − 200.500,30 = −12.360,628.
    isi = biaya.harga_isi_beli(D(2000), D(2030), CFG)
    assert biaya.biaya_beli(L, isi, CFG) == D("200500.30")
    assert biaya.hasil_jual(L, biaya.harga_isi_jual(D(1888), CFG), CFG) == D("188139.672")
    assert biaya.pnl_bersih(L, isi, biaya.harga_isi_jual(D(1888), CFG), CFG) == D("-12360.628")


# ---------------------------------------------------------------------------
# Target A (PB, 2R) dan R:R bersih (§14.2, §12.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "harga_isi, target",
    [
        (2000, 2224),  # 2.000 + 2 × (2.000 − 1.888 = 112) = 2.224
        (2015, 2269),  # 2.015 + 2 × 127 = 2.269
        (2030, 2314),  # 2.030 + 2 × 142 = 2.314
    ],
)
def test_tabel_target_a(harga_isi, target):
    bei = AturanBEI(CFG)
    assert not bei.fraksi_berlaku(T)  # rilis: fraksi belum diverifikasi → rupiah bulat
    assert CFG.strategi.pb.target_r == 2.0
    hasil = hitung_target(D(harga_isi), D(1888), CFG.strategi.pb.target_r, bei, T)
    assert isinstance(hasil, Decimal)
    assert hasil == D(target)


@pytest.mark.parametrize(
    "harga_isi, target",
    [
        ("2001.3", 2227),  # 2.001,3 + 2 × 113,3 = 2.227,9 → ke BAWAH 2.227 (bukan 2.228)
        ("2000.9", 2226),  # 2.000,9 + 2 × 112,9 = 2.226,7 → 2.226
        ("2002.002", 2230),  # isi 2.000 × 1,001: 2.002,002 + 2 × 114,002 = 2.230,006 → 2.230
    ],
)
def test_target_dibulatkan_ke_bawah_ke_rupiah(harga_isi, target):
    hasil = hitung_target(D(harga_isi), D(1888), 2.0, AturanBEI(CFG), T)
    assert hasil == D(target)


def test_target_dibulatkan_ke_bawah_ke_fraksi_fiktif():
    # FIKTIF – KHUSUS UJI: fraksi 7 untuk semua harga (bukan aturan BEI).
    cfg = cfg_uji(
        {
            "pasar.fraksi_harga.terverifikasi": True,
            "pasar.fraksi_harga.berlaku_mulai": "2020-01-01",
            "pasar.fraksi_harga.tabel": [{"harga_min": 0, "harga_maks": None, "fraksi": 7}],
        }
    )
    bei = AturanBEI(cfg)
    # 2.224 / 7 = 317,71… → 317 × 7 = 2.219 (ke bawah; ke atas akan 2.226).
    assert hitung_target(D(2000), D(1888), 2.0, bei, T) == D(2219)
    # Sebelum berlaku_mulai: hanya rupiah bulat.
    assert hitung_target(D(2000), D(1888), 2.0, bei, date(2019, 12, 31)) == D(2224)


def test_target_tidak_terdefinisi_bila_harga_isi_tidak_di_atas_stop():
    with pytest.raises(ValueError):
        hitung_target(D(1888), D(1888), 2.0, AturanBEI(CFG), T)


def test_rr_bersih_a_1_81():
    # (2.314 × 0,9975 − 2.030 × 1,0015) / 151,64828 = (2.308,215 − 2.033,045) / 151,64828
    #   = 275,17 / 151,64828 = 1,8145… → 1,81. Target limit tanpa slippage; bila slippage ikut
    #   dikenakan pada target hasilnya 1,80 (uji ini akan gagal).
    rr = biaya.rr_bersih(D(2314), D(2030), D(1888), CFG)
    assert rr == D("275.17") / D("151.64828")
    assert rr.quantize(D("0.01"), rounding=ROUND_HALF_UP) == D("1.81")
