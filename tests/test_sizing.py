"""T-SZ: ukuran posisi (SPEC §14.1–§14.2, §13.2 cek d–g, §22.1 T-SZ).

Semua angka acuan dihitung tangan dari SPEC (aritmetika ditulis di komentar), bukan disalin dari
keluaran implementasi. Config dasar = `config.yaml` rilis lewat `bantu_pasar.cfg_uji` (fee 0,15%/0,25%,
slippage 0,10%, `fee_minimum_idr: 0`, `pembulatan_fee: null`, lot 100, likuiditas 1%); setiap
penyimpangan ditulis eksplisit. Fee minimum, fee lain, dan RataNilai kecil di berkas ini adalah
FIKTIF – KHUSUS UJI, bukan aturan atau tarif nyata.

Asumsi §14.2: ekuitas Rp 3.000.000 (kas Rp 3.000.000, tanpa posisi), BOLEH BELI → anggaran Rp 30.000
(1%), alokasi maks Rp 1.000.000 (÷ 3 posisi), RataNilai Rp 20 miliar; setiap kandidat dihitung terpisah.
"""

from __future__ import annotations

import math
import random
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

import pytest

from asisten_bei import biaya, teks
from asisten_bei.config import resolusi_konteks
from asisten_bei.kondisi_pasar import BOLEH_BELI, HATI_HATI
from asisten_bei.seleksi import (
    KAS_KURANG,
    REKOMENDASI,
    TIDAK_LIKUID,
    TIDAK_TERJANGKAU,
    KeadaanPortofolio,
    seleksi,
)
from asisten_bei.sizing import HasilSizing, hitung_sizing, n_terbesar
from asisten_bei.skor import KomponenSkor, SinyalBerskor
from asisten_bei.strategi.dasar import Sinyal, Strategi
from bantu_pasar import bar, cfg_uji, pasar, tgl

D = Decimal
CFG = cfg_uji()  # default rilis
L = 100  # pasar.lot_lembar default (diperiksa di test_asumsi_config_14_2)
EKUITAS = D(3_000_000)
KAS = D(3_000_000)
ANGGARAN = D(30_000)  # 3.000.000 × 1,0/100 × 1 (BOLEH BELI)
ANGGARAN_HH = D(15_000)  # 3.000.000 × 1,0/100 × 0,5 (HATI-HATI)
ALOKASI = D(1_000_000)  # 3.000.000 ÷ 3
RATA_NILAI = 20_000_000_000.0  # Rp 20 miliar (float, seperti `Sinyal.rata_nilai`)
T = date(2026, 10, 15)

# Kandidat §14.2 (harga sudah bulat; fraksi belum diverifikasi).
A = (D(2030), D(1888))  # PB: harga_maks 2.000 + 30; stop LLV5 1.900 − 12
B = (D(9125), D(8500))  # BO: 9.000 + 125; 9.000 − 500
C = (D(458), D(410))  # MR: 450 + 8; 450 − 40


def _sz(
    harga_maks: Decimal,
    stop: Decimal,
    *,
    anggaran: Decimal | int | str = ANGGARAN,
    alokasi: Decimal | int | str = ALOKASI,
    kas: Decimal | int | str = KAS,
    rata: float = RATA_NILAI,
    cfg=CFG,
) -> HasilSizing:
    return hitung_sizing(
        D(harga_maks), D(stop), anggaran_risiko=D(anggaran), alokasi_maks=D(alokasi), kas_tersedia=D(kas),
        rata_nilai=rata, cfg=cfg,
    )


def _pct_ekuitas(x: Decimal) -> str:
    """Tampilan "% ekuitas" 2 desimal (ROUND_HALF_UP), seperti §14.2 "(0,51%)"."""
    return teks.persen(x * 100 / EKUITAS, 2)


# ---------------------------------------------------------------------------
# Prasyarat §14.2: config default, anggaran, alokasi
# ---------------------------------------------------------------------------


def _porto() -> KeadaanPortofolio:
    return KeadaanPortofolio(posisi=(), kas_idr=KAS, ekuitas_idr=EKUITAS)


def _seleksi(sinyal: list[SinyalBerskor], status: str = BOLEH_BELI, cfg=CFG):
    return seleksi(
        sinyal, status_pasar=status, portofolio=_porto(), konteks=resolusi_konteks(cfg, "live"), cfg=cfg,
        strategi_lolos=None, deduplikasi=True, sektor_dari=lambda kode: f"SEKTOR_{kode}",
    )


def test_asumsi_config_14_2():
    assert CFG.pasar.lot_lembar == L
    assert biaya.fb(CFG) == D("0.0015") and biaya.fj(CFG) == D("0.0025") and biaya.slip(CFG) == D("0.001")
    assert CFG.biaya.fee_minimum_idr == 0 and CFG.biaya.pembulatan_fee is None
    assert CFG.risiko.risiko_per_transaksi_pct == 1 and CFG.risiko.maks_posisi == 3
    assert CFG.risiko.alokasi_maks_pct == "ikuti_maks_posisi"
    assert CFG.risiko.hati_hati.pengali_risiko == 0.5
    assert CFG.likuiditas.maks_pct_order_dari_nilai_harian == 1
    assert CFG.risiko.izinkan_satu_lot_melebihi_risiko is False


@pytest.mark.parametrize("status, anggaran", [(BOLEH_BELI, 30_000), (HATI_HATI, 15_000)])
def test_anggaran_dan_alokasi_maks_14_2(status, anggaran):
    # anggaran = 3.000.000 × 1/100 × (0,5 bila HATI-HATI); alokasi = 3.000.000 ÷ 3 = 1.000.000 TEPAT
    # (bukan 3.000.000 × 33,33% = 999.900 atau 999.999,99…, yang membuat lot_alokasi kasus P = 9.950 jadi 0).
    konteks = resolusi_konteks(CFG, "live")
    assert konteks.alokasi_maks(EKUITAS) == D(1_000_000)
    h = _seleksi([], status)
    assert h.anggaran_risiko == D(anggaran)
    assert h.alokasi_maks == D(1_000_000)
    assert isinstance(h.anggaran_risiko, Decimal) and isinstance(h.alokasi_maks, Decimal)


class _StrategiUji(Strategi):
    """Strategi kosong, hanya untuk memakai `_bangun_sinyal` (pembangun level harga bersama)."""

    kode = "PB"

    def evaluasi(self, dv, kode, bei):  # pragma: no cover - tidak dipakai
        raise NotImplementedError


@pytest.mark.parametrize(
    "close, atr, stop_dari_cfg, harga_maks, stop",
    [
        # A (PB): 2.000 + 0,5 × 60 = 2.030; LLV5 1.900 − penyangga 0,2 × 60 = 1.888
        (2000.0, 60.0, lambda c: 1900.0 - c.strategi.pb.penyangga_stop_atr * 60.0, 2030, 1888),
        # B (BO): 9.000 + 0,5 × 250 = 9.125; 9.000 − 2,0 × 250 = 8.500
        (9000.0, 250.0, lambda c: 9000.0 - c.strategi.bo.stop_atr * 250.0, 9125, 8500),
        # C (MR): 450 + 0,5 × 16 = 458; 450 − 2,5 × 16 = 410
        (450.0, 16.0, lambda c: 450.0 - c.strategi.mr.stop_atr * 16.0, 458, 410),
    ],
)
def test_harga_maks_dan_stop_tabel_14_2(close, atr, stop_dari_cfg, harga_maks, stop):
    assert CFG.entry.gap_maks_atr == 0.5
    p = pasar({"UJIA": bar([close] * 30)}, None, CFG, tgl(29))
    dv = p.tampilan(tgl(29))
    e = _StrategiUji(CFG)._bangun_sinyal(
        dv, "UJIA", [], close=close, atr=atr, stop_tersesuaikan=stop_dari_cfg(CFG), k_sinyal=0.5,
        target_r=None, rata_nilai=RATA_NILAI,
    )
    assert e.sinyal is not None
    assert e.sinyal.harga_maks == D(harga_maks)
    assert e.sinyal.stop == D(stop)
    assert not e.sinyal.fraksi_dipakai  # fraksi belum diverifikasi → rupiah bulat saja


# ---------------------------------------------------------------------------
# Tabel §14.2 (A/B/C, BOLEH BELI)
# ---------------------------------------------------------------------------

# (P, S, risiko_lembar, risiko per lot, lot_risiko, biaya_beli(1), lot_alokasi, lot_kas, lot_likuid, lot)
TABEL_14_2 = {
    # A: 2.030 × 1,0015 = 2.033,045; 1.888 × 0,999 × 0,9975 = 1.886,112 − 4,71528 = 1.881,39672
    #    risiko_lembar 151,64828; per lot 15.164,828; 30.000 / 15.164,828 = 1,978 → 1
    #    biaya_beli(1) = 203.000 + 304,50; alokasi: 4 × 203.304,5 = 813.218 ≤ 1 jt < 5 × = 1.016.522,5
    #    kas: 14 × = 2.846.263 ≤ 3 jt < 15 × = 3.049.567,5; likuid: 1% × 20 M = 200 jt / 203.000 = 985,2 → 985
    "A": (A[0], A[1], "151.64828", "15164.828", 1, "203304.50", 4, 14, 985, 1),
    # B: 9.125 × 1,0015 = 9.138,6875; 8.500 × 0,999 = 8.491,5; × 0,9975 = 8.491,5 − 21,22875 = 8.470,27125
    #    risiko_lembar 668,41625; per lot 66.841,625 > 30.000 → 0
    #    biaya_beli(1) = 912.500 + 1.368,75 = 913.868,75 ≤ 1 jt → alokasi 1; kas 3 × = 2.741.606,25 → 3
    #    likuid 200 jt / 912.500 = 219,2 → 219
    "B": (B[0], B[1], "668.41625", "66841.625", 0, "913868.75", 1, 3, 219, 0),
    # C: 458 × 1,0015 = 458,687; 410 × 0,999 = 409,59; × 0,9975 = 409,59 − 1,023975 = 408,566025
    #    risiko_lembar 50,120975; per lot 5.012,0975; 30.000 / 5.012,0975 = 5,985 → 5
    #    biaya_beli(1) = 45.800 + 68,70 = 45.868,70; alokasi 21 × = 963.242,7 (22 × = 1.009.111,4) → 21
    #    kas 65 × = 2.981.465,5 (66 × = 3.027.334,2) → 65; likuid 200 jt / 45.800 = 4.366,8 → 4.366
    "C": (C[0], C[1], "50.120975", "5012.0975", 5, "45868.70", 21, 65, 4366, 5),
}


@pytest.mark.parametrize("nama", ["A", "B", "C"])
def test_tabel_14_2_per_kolom(nama):
    P, S, rl, per_lot, l_risiko, beli1, l_alok, l_kas, l_likuid, lot = TABEL_14_2[nama]
    h = _sz(P, S)
    assert h.risiko_lembar == D(rl)
    assert h.risiko_1_lot == D(per_lot)
    assert h.lot_risiko == l_risiko
    assert h.biaya_1_lot == D(beli1)
    assert (h.lot_alokasi, h.lot_kas, h.lot_likuid) == (l_alok, l_kas, l_likuid)
    assert h.lot == lot == min(l_risiko, l_alok, l_kas, l_likuid)
    assert h.lembar == lot * L
    assert h.risiko_di_atas_anggaran is False
    for x in (h.risiko_lembar, h.risiko_1_lot, h.biaya_1_lot, h.nilai_idr, h.risiko_idr, h.biaya_beli_idr):
        assert isinstance(x, Decimal)


def test_hasil_a_1_lot():
    h = _sz(*A)
    assert h.lot == 1 and h.pembatas == "risiko"
    assert h.nilai_idr == D(203_000)  # 100 × 2.030
    assert h.fee_beli_idr == D("304.5") and h.biaya_beli_idr == D("203304.5")
    assert h.risiko_idr == D("15164.828")
    assert teks.rupiah(h.risiko_idr) == "Rp 15.164,83"  # tampilan 2 desimal HALF_UP
    assert _pct_ekuitas(h.risiko_idr) == "0,51%"  # 15.164,828 / 3 jt = 0,5055%


def test_hasil_b_tidak_terjangkau_karena_risiko():
    h = _sz(*B)
    assert h.lot == 0 and h.lembar == 0
    assert h.lot_risiko == 0 and h.pembatas == "risiko"  # §13.2 d (bukan alokasi: lot_alokasi = 1)
    assert teks.rupiah(h.risiko_1_lot) == "Rp 66.841,63"  # 66.841,625 → HALF_UP
    assert _pct_ekuitas(h.risiko_1_lot) == "2,23%"  # 66.841,625 / 3 jt = 2,228%
    assert h.risiko_1_lot > ANGGARAN


def test_hasil_c_5_lot():
    h = _sz(*C)
    assert h.lot == 5 and h.lembar == 500 and h.pembatas == "risiko"
    assert h.nilai_idr == D(229_000)  # 500 × 458
    assert h.fee_beli_idr == D("343.5")  # 0,0015 × 229.000
    assert h.biaya_beli_idr == D("229343.5")
    assert h.risiko_idr == D("25060.4875")  # 5 × 5.012,0975
    assert teks.rupiah(h.risiko_idr) == "Rp 25.060,49"
    assert _pct_ekuitas(h.risiko_idr) == "0,84%"  # 25.060,4875 / 3 jt = 0,835%
    assert h.risiko_idr <= ANGGARAN < h.risiko_1_lot * 6  # 6 lot = 30.072,585 > 30.000


# ---------------------------------------------------------------------------
# HATI-HATI (anggaran Rp 15.000)
# ---------------------------------------------------------------------------


def test_hati_hati_a_tidak_terjangkau():
    h = _sz(*A, anggaran=ANGGARAN_HH)
    assert h.risiko_1_lot == D("15164.828") > ANGGARAN_HH  # 15.164,83 > 15.000
    assert h.lot_risiko == 0 and h.lot == 0 and h.pembatas == "risiko"


def test_hati_hati_b_tidak_terjangkau():
    h = _sz(*B, anggaran=ANGGARAN_HH)
    assert h.lot_risiko == 0 and h.lot == 0 and h.pembatas == "risiko"


def test_hati_hati_c_2_lot():
    # 15.000 / 5.012,0975 = 2,993 → 2; risiko 2 × 5.012,0975 = 10.024,195 → tampil 10.024,20
    h = _sz(*C, anggaran=ANGGARAN_HH)
    assert h.lot_risiko == 2 and h.lot == 2
    assert h.risiko_idr == D("10024.195")
    assert teks.rupiah(h.risiko_idr) == "Rp 10.024,20"


@pytest.mark.parametrize(
    "anggaran, lot",
    [
        ("15036.2925", 3),  # tepat 3 × 5.012,0975 → 3 (batas inklusif: risiko ≤ anggaran)
        ("15036.2924", 2),  # 0,0001 di bawah → 2
        ("5012.0975", 1),  # tepat 1 lot
        ("5012.0974", 0),
    ],
)
def test_lot_risiko_batas_inklusif_kedua_sisi(anggaran, lot):
    assert _sz(*C, anggaran=anggaran).lot_risiko == lot


# ---------------------------------------------------------------------------
# Status seleksi untuk contoh §14.2 (setiap kandidat sendirian)
# ---------------------------------------------------------------------------


def _sinyal(kode: str, strategi: str, P: Decimal, S: Decimal, rata: float = RATA_NILAI) -> SinyalBerskor:
    sg = Sinyal(
        tanggal=T, kode=kode, strategi=strategi, cek=(), harga_ref=P, harga_maks=P, stop=S, atr=D(1),
        faktor_split=1.0, k_sinyal=0.5, target_r=None, rata_nilai=rata, fraksi_dipakai=False,
    )
    return SinyalBerskor(sg, KomponenSkor(k_sinyal=0.5, k_rs=0.5, k_likuid=0.5, k_stop=0.5, skor=50.0))


@pytest.mark.parametrize(
    "status_pasar, nama, strategi, harapan_status, harapan_lot, potongan_alasan",
    [
        (BOLEH_BELI, "A", "PB", REKOMENDASI, 1, ()),
        (BOLEH_BELI, "B", "BO", TIDAK_TERJANGKAU, 0, ("Rp 66.841,63", "2,23%", "Rp 30.000")),
        (BOLEH_BELI, "C", "MR", REKOMENDASI, 5, ()),
        (HATI_HATI, "A", "PB", TIDAK_TERJANGKAU, 0, ("Rp 15.164,83", "Rp 15.000")),
        (HATI_HATI, "B", "BO", TIDAK_TERJANGKAU, 0, ("Rp 66.841,63",)),
        (HATI_HATI, "C", "MR", REKOMENDASI, 2, ()),
    ],
)
def test_seleksi_contoh_14_2(status_pasar, nama, strategi, harapan_status, harapan_lot, potongan_alasan):
    P, S = TABEL_14_2[nama][:2]
    h = _seleksi([_sinyal(f"UJI{nama}", strategi, P, S)], status_pasar)
    (k,) = h.kandidat
    assert k.status == harapan_status
    assert k.sizing is not None and k.sizing.lot == harapan_lot
    if harapan_status == REKOMENDASI:
        assert k.alasan == ""
    else:
        assert "risiko 1 lot" in k.alasan  # §13.2 d: alasan risiko, bukan alokasi
        for p in potongan_alasan:
            assert p in k.alasan


# ---------------------------------------------------------------------------
# Fee minimum fiktif (FIKTIF – KHUSUS UJI): m = Rp 5.000
# ---------------------------------------------------------------------------

CFG_M5000 = cfg_uji({"biaya.fee_minimum_idr": 5000})  # FIKTIF – KHUSUS UJI


def test_fee_minimum_a_1_lot():
    # n = 1: biaya 203.000 + max(304,5; 5.000) = 208.000; hasil stop 188.611,2 − max(471,528; 5.000)
    #        = 183.611,20; risiko 24.388,80 ≤ 30.000.
    # n = 2: biaya 406.000 + max(609; 5.000) = 411.000; hasil 377.222,4 − max(943,056; 5.000) = 372.222,40;
    #        risiko 38.777,60 > 30.000 → 1 lot.
    h = _sz(*A, cfg=CFG_M5000)
    assert h.lot_risiko == 1 and h.lot == 1
    assert h.risiko_1_lot == D("24388.80")
    assert h.risiko_idr == D("24388.80")
    assert h.biaya_1_lot == D(208_000)
    assert teks.rupiah(h.risiko_idr) == "Rp 24.388,80"
    # alokasi: 4 lot = 812.000 + max(1.218; 5.000) = 817.000 ≤ 1 jt; kas: 14 lot = 2.842.000 + 5.000 ≤ 3 jt
    assert (h.lot_alokasi, h.lot_kas) == (4, 14)
    # risiko_lembar (§4.1) tidak memuat fee minimum.
    assert h.risiko_lembar == D("151.64828")


@pytest.mark.parametrize(
    "anggaran, lot_m5000, lot_m0",
    [
        ("38777.60", 2, 2),  # tepat risiko(2) dengan m → 2 (tanpa m: 2 × 15.164,828 = 30.329,656 → 2)
        ("38777.59", 1, 2),  # di bawah risiko(2) dengan m → 1
        ("24388.80", 1, 1),  # tepat risiko(1) dengan m → 1
        ("24388.79", 0, 1),  # fee minimum membuat A tak terjangkau, padahal tanpa m 1 lot
    ],
)
def test_fee_minimum_batas_kedua_sisi(anggaran, lot_m5000, lot_m0):
    assert _sz(*A, anggaran=anggaran, cfg=CFG_M5000).lot_risiko == lot_m5000
    assert _sz(*A, anggaran=anggaran).lot_risiko == lot_m0


def test_fee_minimum_menurunkan_lot_c_dari_5_ke_4():
    # Untuk n ≤ 48 kedua fee = 5.000 (0,0025 × 40.959 × 48 = 4.915 < 5.000), sehingga
    # risiko(n) = n × (45.800 − 40.959) + 10.000 = 4.841n + 10.000.
    # risiko(4) = 29.364 ≤ 30.000; risiko(5) = 34.205 > 30.000 → 4 lot (tanpa m: 5 lot).
    h = _sz(*C, cfg=CFG_M5000)
    assert h.lot_risiko == 4 and h.lot == 4
    assert h.risiko_idr == D(29_364)
    assert h.risiko_1_lot == D(14_841)
    assert _sz(*C).lot_risiko == 5


# ---------------------------------------------------------------------------
# Batas pembulatan: P = 1.060, S = 964, slippage 0 → tepat 3 lot (bukti Decimal)
# ---------------------------------------------------------------------------

CFG_SLIP0 = cfg_uji({"biaya.slippage_pct": 0})


def test_batas_pembulatan_tepat_3_lot():
    # risiko_lembar = 1.060 × 1,0015 − 964 × 1 × 0,9975 = 1.061,59 − 961,59 = 100,00
    # per lot = 10.000,00; 30.000 / 10.000 = 3 tepat → 3 lot, risiko = 30.000 = anggaran.
    h = _sz(D(1060), D(964), cfg=CFG_SLIP0)
    assert h.risiko_lembar == D(100)
    assert h.risiko_1_lot == D(10_000)
    assert h.lot_risiko == 3 and h.lot == 3
    assert h.risiko_idr == D(30_000) == ANGGARAN
    # Kontrol: rumus tertutup yang sama dengan float memberi 2 lot (100,00000000000011 per lembar),
    # sehingga uji ini membedakan Decimal dari float.
    assert math.floor(30000 / (100 * (1060 * (1 + 0.0015) - 964 * (1 - 0.0) * (1 - 0.0025)))) == 2


def test_batas_pembulatan_sisi_bawah():
    assert _sz(D(1060), D(964), anggaran="29999.99", cfg=CFG_SLIP0).lot_risiko == 2
    assert _sz(D(1060), D(964), anggaran="40000", cfg=CFG_SLIP0).lot_risiko == 4  # 4 × 10.000 tepat


# ---------------------------------------------------------------------------
# Alokasi maks 1.000.000 tepat; m = 5.000 fiktif, P = 9.950 → biaya_beli(1) = 1.000.000
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "P, alokasi, lot_alokasi",
    [
        # 100 × 9.950 = 995.000 + max(1.492,5; 5.000) = 1.000.000 ≤ 1.000.000 → 1
        (9950, None, 1),
        (9950, "999999.99", 0),  # 1 sen di bawah → 0
        # 100 × 9.951 = 995.100 + 5.000 = 1.000.100 > 1.000.000 → 0
        (9951, None, 0),
    ],
)
def test_alokasi_maks_tepat_dengan_fee_minimum(P, alokasi, lot_alokasi):
    alok = resolusi_konteks(CFG_M5000, "live").alokasi_maks(EKUITAS) if alokasi is None else D(alokasi)
    # Stop 9.900: risiko(1) = 1.000.000 − (989.010 − 5.000) = 15.990 ≤ 30.000 (bukan pembatas).
    h = _sz(D(P), D(9900), alokasi=alok, cfg=CFG_M5000)
    assert h.biaya_1_lot == D(P) * L + D(5000)
    assert h.lot_alokasi == lot_alokasi
    assert h.lot_risiko >= 1
    assert h.lot == lot_alokasi
    if lot_alokasi == 0:
        assert h.pembatas == "alokasi"


@pytest.mark.parametrize("P, harapan", [(9950, REKOMENDASI), (9951, TIDAK_TERJANGKAU)])
def test_seleksi_alokasi_maks_tepat(P, harapan):
    h = _seleksi([_sinyal("UJIX", "BO", D(P), D(9900))], cfg=CFG_M5000)
    (k,) = h.kandidat
    assert k.status == harapan
    if harapan == TIDAK_TERJANGKAU:
        # §13.2 e: "nilai 1 lot Rp X > alokasi maks Rp Z"
        assert "Rp 1.000.100" in k.alasan and "Rp 1.000.000" in k.alasan
        assert "risiko" not in k.alasan
    else:
        assert k.sizing.lot == 1


# ---------------------------------------------------------------------------
# izinkan_satu_lot_melebihi_risiko (§13.2 paragraf akhir)
# ---------------------------------------------------------------------------

CFG_IZIN = cfg_uji({"risiko.izinkan_satu_lot_melebihi_risiko": True})


def test_izinkan_false_b_tetap_0():
    h = _sz(*B)
    assert h.lot_risiko == 0 and h.lot == 0 and h.risiko_di_atas_anggaran is False


def test_izinkan_true_b_menjadi_1_lot_bertanda():
    h = _sz(*B, cfg=CFG_IZIN)
    assert h.lot_risiko == 1 and h.lot == 1  # min(1, 1, 3, 219)
    assert h.risiko_di_atas_anggaran is True
    assert h.risiko_idr == D("66841.625") > ANGGARAN
    assert h.pembatas == "risiko"  # seri dengan alokasi (1) → urutan §13.2: risiko dulu


def test_izinkan_true_hati_hati_a_1_lot_bertanda():
    h = _sz(*A, anggaran=ANGGARAN_HH, cfg=CFG_IZIN)
    assert h.lot == 1 and h.risiko_di_atas_anggaran is True


def test_izinkan_true_tidak_mengubah_lot_risiko_yang_sudah_cukup():
    h = _sz(*C, cfg=CFG_IZIN)
    assert h.lot_risiko == 5 and h.lot == 5 and h.risiko_di_atas_anggaran is False
    h = _sz(*A, cfg=CFG_IZIN)  # 1 lot sudah ≤ anggaran → tidak bertanda
    assert h.lot == 1 and h.risiko_di_atas_anggaran is False


@pytest.mark.parametrize(
    "ubah, lot, pembatas",
    [
        # Batas e–g tetap berlaku. biaya_beli(1) B = 913.868,75; nilai 1 lot = 912.500.
        ({"alokasi": "913868.74"}, 0, "alokasi"),
        ({"alokasi": "913868.75"}, 1, "risiko"),
        ({"kas": "913868.74"}, 0, "kas"),
        ({"kas": "913868.75"}, 1, "risiko"),
        # likuid: 1% × RataNilai / (9.125 × 100); 1% × 91.250.000 = 912.500 → 1 lot; 1 rupiah kurang → 0
        ({"rata": 91_249_999.0}, 0, "likuid"),
        ({"rata": 91_250_000.0}, 1, "risiko"),
    ],
)
def test_izinkan_true_batas_alokasi_likuid_kas_tetap_berlaku(ubah, lot, pembatas):
    h = _sz(*B, cfg=CFG_IZIN, **ubah)
    assert h.lot_risiko == 1
    assert h.lot == lot
    assert h.pembatas == pembatas


def test_seleksi_izinkan_true_rekomendasi_dengan_tanda():
    h = _seleksi([_sinyal("UJIB", "BO", *B)], cfg=CFG_IZIN)
    (k,) = h.kandidat
    assert k.status == REKOMENDASI
    assert k.sizing.lot == 1 and k.sizing.risiko_di_atas_anggaran
    assert "di atas anggaran" in k.alasan


# ---------------------------------------------------------------------------
# lot_likuid dan pembatas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rata, lot_likuid",
    [
        # C: 1% × 22.900.000 = 229.000 = 5 × 458 × 100 → 5 (tanpa fee: biaya_beli(5) = 229.343,5 > 229.000)
        (22_900_000.0, 5),
        (22_899_999.0, 4),  # 228.999,99 / 45.800 = 4,99… → 4
        (45_800.0 * 100, 1),  # 1% × 4.580.000 = 45.800 = 1 lot tepat
        (45_799.0 * 100, 0),
    ],
)
def test_lot_likuid_floor_tanpa_fee_kedua_sisi(rata, lot_likuid):
    h = _sz(*C, rata=rata)
    assert h.lot_likuid == lot_likuid
    assert h.lot == min(5, lot_likuid)


def test_lot_likuid_memakai_pct_dari_config():
    # FIKTIF – KHUSUS UJI: 2,5% × 20 M = 500 jt / 45.800 = 10.917,03 → 10.917; 0,5% → 2.183,4 → 2.183
    assert _sz(*C, cfg=cfg_uji({"likuiditas.maks_pct_order_dari_nilai_harian": 2.5})).lot_likuid == 10_917
    assert _sz(*C, cfg=cfg_uji({"likuiditas.maks_pct_order_dari_nilai_harian": 0.5})).lot_likuid == 2_183


def test_lot_likuid_membatasi_lot():
    h = _sz(*C, rata=22_899_999.0)
    assert h.lot_risiko == 5 and h.lot_likuid == 4
    assert h.lot == 4 and h.pembatas == "likuid"
    assert h.risiko_idr == D("20048.39")  # 4 × 5.012,0975


@pytest.mark.parametrize(
    "ubah, lot, pembatas",
    [
        ({}, 5, "risiko"),
        # alokasi 100.000: 2 × 45.868,7 = 91.737,4 ≤ 100.000 < 3 × = 137.606,1 → 2
        ({"alokasi": 100_000}, 2, "alokasi"),
        ({"kas": 50_000}, 1, "kas"),  # 45.868,7 ≤ 50.000 < 91.737,4
        ({"rata": 22_899_999.0}, 4, "likuid"),
        # Seri: alokasi tepat biaya_beli(5) = 229.343,5 → lot_alokasi 5 = lot_risiko → "risiko" (§13.2 d < e)
        ({"alokasi": "229343.5"}, 5, "risiko"),
        ({"alokasi": "229343.49"}, 4, "alokasi"),
        ({"kas": "229343.5"}, 5, "risiko"),
        ({"kas": "229343.49"}, 4, "kas"),
        # Seri di 0: alokasi dan kas sama-sama 0 → "alokasi" (§13.2 e < g)
        ({"alokasi": "45868.69", "kas": 0}, 0, "alokasi"),
        # likuid dan kas sama-sama 0 → "likuid" (§13.2 f < g)
        ({"rata": 1_000_000.0, "kas": 0}, 0, "likuid"),
        # risiko 0 dan alokasi 0 → "risiko" (§13.2 d < e)
        ({"anggaran": 5000, "alokasi": 1000}, 0, "risiko"),
    ],
)
def test_pembatas_mengikuti_urutan_13_2(ubah, lot, pembatas):
    h = _sz(*C, **ubah)
    assert h.lot == lot
    assert h.pembatas == pembatas


@pytest.mark.parametrize(
    "ubah, harapan",
    [
        ({"rata": 1_000_000.0}, TIDAK_LIKUID),  # 1% × 1 jt = 10.000 < 45.800 → lot_likuid 0
        ({"kas": 0}, KAS_KURANG),
    ],
)
def test_seleksi_status_dari_pembatas(ubah, harapan):
    rata = ubah.get("rata", RATA_NILAI)
    porto = KeadaanPortofolio(posisi=(), kas_idr=D(ubah.get("kas", KAS)), ekuitas_idr=EKUITAS)
    h = seleksi(
        [_sinyal("UJIC", "MR", *C, rata=rata)], status_pasar=BOLEH_BELI, portofolio=porto,
        konteks=resolusi_konteks(CFG, "live"), cfg=CFG, strategi_lolos=None, deduplikasi=True,
        sektor_dari=lambda kode: "SEKTOR_UJI",
    )
    assert h.kandidat[0].status == harapan


# ---------------------------------------------------------------------------
# Masukan tidak sah
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg, S",
    [
        pytest.param(CFG_SLIP0, 2030, id="slip0-sama"),
        pytest.param(CFG_SLIP0, 2031, id="slip0-di-atas"),
        pytest.param(CFG, 2100, id="slip-default-jauh-di-atas"),
        pytest.param(CFG, 2030, id="slip-default-sama"),
        pytest.param(CFG, 2031, id="slip-default-di-atas"),
    ],
)
def test_stop_tidak_di_bawah_harga_maks_value_error(cfg, S):
    with pytest.raises(ValueError):
        _sz(D(2030), D(S), cfg=cfg)


def test_stop_satu_rupiah_di_bawah_harga_maks_sah():
    # Sisi lain batas: S = P − 1 → risiko_lembar = 2.030 × 1,0015 − 2.029 × 0,999 × 0,9975
    #   = 2.033,045 − 2.026,971 × 0,9975 = 2.033,045 − 2.021,9035725 = 11,1414275 > 0
    # lot_risiko = floor(30.000 / 1.114,14275) = 26; lot_alokasi: 4 × 203.304,5 ≤ 1 jt → 4 (pembatas).
    h = _sz(D(2030), D(2029))
    assert h.risiko_lembar == D("11.1414275")
    assert h.lot_risiko == 26 and h.lot == 4 and h.pembatas == "alokasi"


# ---------------------------------------------------------------------------
# Pencarian monoton vs brute force (input fiktif acak, deterministik)
# ---------------------------------------------------------------------------


def test_n_terbesar_vs_brute_force():
    for batas in range(0, 15):
        for k in range(-1, 18):
            assert n_terbesar(lambda n, k=k: n <= k, batas) == max(0, min(k, batas))


def _ref_fee(nilai: Decimal, pct: Decimal, m: Decimal, mode: str | None) -> Decimal:
    """Fee rujukan dari SPEC: max(pct × nilai, m), lalu `pembulatan_fee` (null/HALF_UP/CEILING)."""
    f = max(nilai * pct, m)
    if mode == "rupiah_terdekat":
        return f.quantize(D(1), rounding=ROUND_HALF_UP)
    if mode == "rupiah_ke_atas":
        return f.quantize(D(1), rounding=ROUND_CEILING)
    return f


class _Rujukan:
    """Rumus §14.1 ditulis ulang langsung (tanpa memakai modul biaya/sizing)."""

    def __init__(self, fb_pct, fj_pct, slip_pct, m, mode, likuid_pct):
        self.fb = D(str(fb_pct)) / 100
        self.fj = D(str(fj_pct)) / 100
        self.slip = D(str(slip_pct)) / 100
        self.m = D(str(m))
        self.mode = mode
        self.likuid = D(str(likuid_pct)) / 100

    def biaya_beli(self, n: int, P: Decimal) -> Decimal:
        v = n * L * P
        return v + _ref_fee(v, self.fb, self.m, self.mode)

    def hasil_stop(self, n: int, S: Decimal) -> Decimal:
        v = n * L * S * (1 - self.slip)
        return v - _ref_fee(v, self.fj, self.m, self.mode)

    def risiko(self, n: int, P: Decimal, S: Decimal) -> Decimal:
        return self.biaya_beli(n, P) - self.hasil_stop(n, S)

    @staticmethod
    def n_maks(f, batas: Decimal, per_lot_bawah: Decimal) -> int:
        """Brute force TANPA asumsi monoton: periksa setiap n di [1, batas/per_lot_bawah + 2].
        Di luar rentang itu f(n) ≥ n × per_lot_bawah > batas karena fee ≥ 0."""
        hasil = 0
        atas = int((batas / per_lot_bawah).to_integral_value(rounding=ROUND_FLOOR)) + 2 if batas > 0 else 0
        for n in range(1, atas + 1):
            if f(n) <= batas:
                hasil = n
        return hasil


# FIKTIF – KHUSUS UJI: kombinasi tarif/minimum/pembulatan, bukan tarif broker nyata.
# (fee_beli_pct, fee_jual_pct, slippage_pct, fee_minimum_idr, pembulatan_fee, maks_pct_order)
KOMBINASI = [
    (0.15, 0.25, 0.10, 0, None, 1.0),
    (0.15, 0.25, 0.10, 5000, None, 1.0),
    (0.15, 0.25, 0.10, 0, "rupiah_terdekat", 1.0),
    (0.15, 0.25, 0.10, 0, "rupiah_ke_atas", 1.0),
    (0.15, 0.25, 0.10, 5000, "rupiah_terdekat", 0.5),
    (0.19, 0.29, 0.25, 12345, "rupiah_ke_atas", 2.5),
    (0.11, 0.21, 0, 2500, None, 1.0),
    (0.13, 0.33, 0.05, 777, "rupiah_terdekat", 1.5),
]


@pytest.mark.parametrize("izinkan", [False, True])
@pytest.mark.parametrize("idx", range(len(KOMBINASI)))
def test_sizing_vs_brute_force_acak(idx, izinkan):
    fb_pct, fj_pct, slip_pct, m, mode, likuid_pct = KOMBINASI[idx]
    cfg = cfg_uji(
        {
            "biaya.fee_beli_pct": fb_pct,
            "biaya.fee_jual_pct": fj_pct,
            "biaya.slippage_pct": slip_pct,
            "biaya.fee_minimum_idr": m,
            "biaya.pembulatan_fee": mode,
            "likuiditas.maks_pct_order_dari_nilai_harian": likuid_pct,
            "risiko.izinkan_satu_lot_melebihi_risiko": izinkan,
        }
    )
    ref = _Rujukan(fb_pct, fj_pct, slip_pct, m, mode, likuid_pct)
    rng = random.Random(20261015 + 2 * idx + int(izinkan))
    sen = D("0.01")

    def acak_atau_batas(f_batas, bawah: int, atas: int) -> Decimal:
        """Separuh kasus tepat di batas f(k) (± 1 sen) agar batas inklusif teruji; sisanya acak."""
        if rng.random() < 0.5:
            k = rng.randint(1, 6)
            return f_batas(k) + rng.choice([D(0), -sen, sen])
        return D(rng.randint(bawah, atas)) + D(rng.randint(0, 99)) * sen

    for _ in range(15):
        P = D(rng.randint(100, 10_000))
        S = D(rng.randint(max(1, int(P * D("0.75"))), int(P) - 1))
        anggaran = acak_atau_batas(lambda k: ref.risiko(k, P, S), 500, 60_000)
        alokasi = acak_atau_batas(lambda k: ref.biaya_beli(k, P), 20_000, 2_000_000)
        kas = acak_atau_batas(lambda k: ref.biaya_beli(k, P), 0, 3_000_000)
        if rng.random() < 0.5:  # RataNilai tepat di batas k lot (± 1 rupiah)
            k = rng.randint(0, 6)
            rata = float(k * L * P / ref.likuid + rng.choice([0, 1] if k == 0 else [0, -1, 1]))
        else:
            rata = rng.uniform(1e7, 5e10)

        per_lot_risiko = L * (P - S * (1 - ref.slip))
        r_risiko = ref.n_maks(lambda n: ref.risiko(n, P, S), anggaran, per_lot_risiko)
        r_alokasi = ref.n_maks(lambda n: ref.biaya_beli(n, P), alokasi, L * P)
        r_kas = ref.n_maks(lambda n: ref.biaya_beli(n, P), kas, L * P)
        r_likuid = max(int((ref.likuid * D(repr(rata)) / (P * L)).to_integral_value(rounding=ROUND_FLOOR)), 0)
        di_atas = izinkan and r_risiko == 0
        if di_atas:
            r_risiko = 1
        r_lot = min(r_risiko, r_alokasi, r_kas, r_likuid)

        h = _sz(P, S, anggaran=anggaran, alokasi=alokasi, kas=kas, rata=rata, cfg=cfg)
        konteks = f"P={P} S={S} anggaran={anggaran} alokasi={alokasi} kas={kas} rata={rata!r}"
        assert (h.lot_risiko, h.lot_alokasi, h.lot_kas, h.lot_likuid) == (r_risiko, r_alokasi, r_kas, r_likuid), konteks
        assert h.lot == r_lot, konteks
        assert h.risiko_di_atas_anggaran is di_atas, konteks
        assert h.risiko_1_lot == ref.risiko(1, P, S), konteks
        assert h.biaya_1_lot == ref.biaya_beli(1, P), konteks
        if m == 0 and mode is None and not di_atas:
            # §14.1: untuk m = 0 (tanpa pembulatan), lot_risiko = floor(anggaran / (L × risiko_lembar)).
            rl = P * (1 + ref.fb) - S * (1 - ref.slip) * (1 - ref.fj)
            assert h.lot_risiko == int((anggaran / (L * rl)).to_integral_value(rounding=ROUND_FLOOR)), konteks
        if h.lot >= 1:
            assert h.risiko_idr == ref.risiko(h.lot, P, S), konteks
            assert h.biaya_beli_idr == ref.biaya_beli(h.lot, P), konteks
            assert h.biaya_beli_idr <= min(alokasi, kas), konteks
            if not di_atas:
                assert h.risiko_idr <= anggaran, konteks
                # Jaminan §14.1: open t+1 ≤ harga_maks → rugi di stop (dengan slippage asumsi) ≤ anggaran.
                for open_ in {P, P - 1, S + 1, D(rng.randint(int(S) + 1, int(P)))}:
                    if open_ <= S:
                        continue
                    isi = min(open_ * (1 + ref.slip), P)
                    v_beli = h.lembar * isi
                    rugi = v_beli + _ref_fee(v_beli, ref.fb, ref.m, ref.mode) - ref.hasil_stop(h.lot, S)
                    assert rugi <= anggaran, (konteks, open_)
