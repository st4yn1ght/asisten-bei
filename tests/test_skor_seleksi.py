"""T-SK: skor (SPEC §13.1) dan urutan seleksi (SPEC §13.2), memakai angka contoh §14.2 (A/B/C).

Bagian yang hanya ada di portofolio live (CEK ORDER §15.4, pembacaan portfolio.csv) diuji di tahap
berikutnya; di sini posisi terbuka cukup berupa `PosisiTerbuka` (MANUAL dan di luar universe juga).

Config rilis (§7): fee 0,15%/0,25%, slippage 0,10%, fee minimum 0, maks_posisi 3, maks_per_sektor 1,
alokasi = ekuitas ÷ 3. Ekuitas Rp 3.000.000 → anggaran Rp 30.000 (BOLEH BELI) / Rp 15.000 (HATI-HATI),
alokasi maks Rp 1.000.000.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from asisten_bei import config
from asisten_bei.seleksi import (
    BATAS_ENTRY_HARIAN,
    BATAS_SEKTOR,
    BAYANGAN_GERBANG,
    BAYANGAN_PASAR,
    DIBLOKIR_PORTOFOLIO,
    DUPLIKAT_STRATEGI,
    KAS_KURANG,
    REKOMENDASI,
    SLOT_PENUH,
    STOP_TERLALU_JAUH,
    SUDAH_DIPEGANG,
    TIDAK_LIKUID,
    TIDAK_TERJANGKAU,
    KeadaanPortofolio,
    PosisiTerbuka,
    kunci_urutan,
    seleksi,
)
from asisten_bei.skor import CATATAN_SKOR, KomponenSkor, SinyalBerskor, beri_skor, hitung_k_rs, hitung_skor
from asisten_bei.strategi.dasar import Sinyal
from bantu_pasar import bar, cfg_uji, pasar, tgl

CFG = cfg_uji()
T_SINYAL = date(2026, 10, 15)
EKUITAS = Decimal("3000000")

# Harga nominal (harga_maks, stop) yang sudah bulat — §14.2.
A = ("2030", "1888")  # PB: risiko 1 lot 15.164,828; biaya 1 lot 203.304,50 → 1 lot (HATI-HATI: tak terjangkau)
B = ("9125", "8500")  # BO: risiko 1 lot 66.841,625 > 30.000 → TIDAK_TERJANGKAU
C = ("458", "410")  # MR: risiko 1 lot 5.012,0975; biaya 1 lot 45.868,70 → 5 lot (HATI-HATI: 2 lot)
# Fiktif: risiko/lembar = 12.000 × 1,0015 − 11.900 × 0,999 × 0,9975 = 12.018 − 11.858,37975 = 159,62025
# → risiko 1 lot 15.962,025 ≤ 30.000, tetapi biaya 1 lot = 1.200.000 × 1,0015 = 1.201.800 > 1.000.000.
MAHAL = ("12000", "11900")
# Fiktif: risiko/lembar = 12.018 − 11.000 × 0,999 × 0,9975 = 1.056,4725 → risiko 1 lot 105.647,25 > 30.000,
# dan biaya 1 lot 1.201.800 > alokasi → gagal d dan e sekaligus.
MAHAL_LEBAR = ("12000", "11000")

RN = 20e9  # RataNilai Rp 20 miliar (§14.2)


# ---------------------------------------------------------------------------
# Pembantu
# ---------------------------------------------------------------------------


def sinyal(
    kode: str,
    strategi: str = "PB",
    harga: tuple[str, str] = A,
    *,
    rata_nilai: float = RN,
    k_sinyal: float = 0.5,
    valid: bool = True,
    alasan: str | None = None,
) -> Sinyal:
    maks, stop = harga
    return Sinyal(
        tanggal=T_SINYAL,
        kode=kode,
        strategi=strategi,
        cek=(),
        harga_ref=Decimal(maks),
        harga_maks=Decimal(maks),
        stop=Decimal(stop),
        atr=Decimal(60),
        faktor_split=1.0,
        k_sinyal=k_sinyal,
        target_r=2.0 if strategi == "PB" else None,
        rata_nilai=rata_nilai,
        fraksi_dipakai=False,
        valid=valid,
        alasan_tidak_valid=alasan,
    )


def berskor(kode: str, skor: float, strategi: str = "PB", harga: tuple[str, str] = A, **kw) -> SinyalBerskor:
    """SinyalBerskor dengan skor yang ditetapkan langsung (komponen tidak relevan untuk seleksi)."""
    return SinyalBerskor(sinyal(kode, strategi, harga, **kw), KomponenSkor(0.0, 0.0, 0.0, 0.0, skor))


def pos(kode: str, sektor: str, jual: bool = False) -> PosisiTerbuka:
    return PosisiTerbuka(kode=kode, sektor=sektor, dijadwalkan_jual=jual)


def porto(
    *posisi: PosisiTerbuka,
    kas: Decimal | str = EKUITAS,
    ekuitas: Decimal | str = EKUITAS,
    estimasi: Decimal | str = Decimal(0),
    blokir: str | None = None,
) -> KeadaanPortofolio:
    return KeadaanPortofolio(
        posisi=tuple(posisi),
        kas_idr=Decimal(kas),
        ekuitas_idr=Decimal(ekuitas),
        estimasi_hasil_jual_terjadwal_idr=Decimal(estimasi),
        blokir=blokir,
    )


def jalankan(
    daftar,
    *,
    status: str = "BOLEH_BELI",
    portofolio: KeadaanPortofolio | None = None,
    cfg=CFG,
    lolos=("PB", "BO", "MR"),
    dedup: bool = True,
    sektor: dict[str, str] | None = None,
):
    """`seleksi` dengan konteks live. `sektor`: peta kode → sektor yang ketat (kode tak dikenal →
    KeyError, sehingga terbukti seleksi tidak menanyakan sektor posisi lewat `sektor_dari`).
    Tanpa peta: setiap kode punya sektornya sendiri."""
    if sektor is None:
        def sektor_dari(kode: str) -> str:
            return f"SEKTOR_{kode}"
    else:
        sektor_dari = sektor.__getitem__
    return seleksi(
        list(daftar),
        status_pasar=status,
        portofolio=portofolio if portofolio is not None else porto(),
        konteks=config.resolusi_konteks(cfg, "live"),
        cfg=cfg,
        strategi_lolos=lolos,
        deduplikasi=dedup,
        sektor_dari=sektor_dari,
    )


def ringkas(hasil) -> list[tuple[str, str, str]]:
    return [(k.kode, k.strategi, k.status) for k in hasil.kandidat]


def satu(hasil, kode: str, strategi: str = "PB"):
    cocok = [k for k in hasil.kandidat if k.kode == kode and k.strategi == strategi]
    assert len(cocok) == 1, (kode, strategi, ringkas(hasil))
    return cocok[0]


# ---------------------------------------------------------------------------
# §13.1 Skor: komponen dan jumlah berbobot
# ---------------------------------------------------------------------------


def test_skor_contoh_a_cocok_hitung_tangan():
    s = sinyal("AAAA", "PB", A, rata_nilai=20e9, k_sinyal=0.5)
    k = hitung_skor(s, 0.5, CFG)
    # jarak = (2.030 − 1.888)/2.030 × 100 = 6,995073...%
    assert s.jarak_stop_pct == pytest.approx(142 / 2030 * 100, abs=1e-12)
    # K_stop = (10 − 6,995073...)/(10 − 3) = 0,429275...
    k_stop = (10 - 142 / 2030 * 100) / 7
    # K_likuid = log10(20 miliar / 10 miliar) = log10 2 = 0,30103...
    assert k.k_likuid == pytest.approx(0.30102999566398120, abs=1e-12)
    assert k.k_stop == pytest.approx(k_stop, abs=1e-12)
    assert (k.k_sinyal, k.k_rs) == (0.5, 0.5)
    # skor = 40 × 0,5 + 30 × 0,5 + 15 × 0,30103 + 15 × 0,429275 = 20 + 15 + 4,515450 + 6,439127 = 45,954577
    assert k.skor == pytest.approx(20 + 15 + 15 * math.log10(2) + 15 * k_stop, abs=1e-9)
    assert k.skor == pytest.approx(45.954577, abs=1e-6)
    assert k.tampilan == "46,0"


@pytest.mark.parametrize(
    ("rata_nilai", "harapan"),
    [
        (0.0, 0.0),
        (5e9, 0.0),  # log10(0,5) < 0 → dipotong 0
        (1e10, 0.0),  # tepat di ambang: log10(1) = 0
        (2e10, math.log10(2)),
        (1e11, 1.0),  # log10(10) = 1 tepat
        (5e11, 1.0),  # log10(50) = 1,69 → dipotong 1
    ],
)
def test_k_likuid_log10_dan_dipotong(rata_nilai, harapan):
    k = hitung_skor(sinyal("AAAA", rata_nilai=rata_nilai), 0.0, CFG)
    assert k.k_likuid == pytest.approx(harapan, abs=1e-12)


def test_k_likuid_memakai_ambang_config():
    cfg = cfg_uji({"likuiditas.nilai_transaksi_min_idr": 2_000_000_000})
    # log10(20 miliar / 2 miliar) = log10(10) = 1, padahal dengan ambang rilis = log10 2.
    assert hitung_skor(sinyal("AAAA", rata_nilai=2e10), 0.0, cfg).k_likuid == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize(
    ("harga", "harapan"),
    [
        (("1000", "980"), 1.0),  # jarak 2% < ideal 3% → (10 − 2)/7 = 1,14 dipotong 1
        (("1000", "970"), 1.0),  # jarak tepat 3% → 1
        (("1000", "935"), 0.5),  # jarak 6,5% → (10 − 6,5)/7 = 0,5
        (("1000", "900"), 0.0),  # jarak tepat 10% → 0
        (("1000", "880"), 0.0),  # jarak 12% → −2/7 dipotong 0
    ],
)
def test_k_stop_batas_dan_dipotong(harga, harapan):
    k = hitung_skor(sinyal("AAAA", harga=harga), 0.0, CFG)
    assert k.k_stop == pytest.approx(harapan, abs=1e-12)


def test_k_stop_memakai_jarak_ideal_dan_buruk_config():
    cfg = cfg_uji({"skor.jarak_stop_ideal_pct": 2.0, "skor.jarak_stop_buruk_pct": 12.0})
    # (12 − 6,5)/(12 − 2) = 0,55
    assert hitung_skor(sinyal("AAAA", harga=("1000", "935")), 0.0, cfg).k_stop == pytest.approx(0.55, abs=1e-12)


def test_skor_jumlah_berbobot_mengikuti_bobot_config():
    # K_sinyal 0,25; K_rs 0,75; K_likuid 1 (RataNilai 100 miliar); K_stop 0,5 (jarak 6,5%).
    s = sinyal("AAAA", harga=("1000", "935"), rata_nilai=1e11, k_sinyal=0.25)
    # Bobot rilis 40/30/15/15: 10 + 22,5 + 15 + 7,5 = 55
    assert hitung_skor(s, 0.75, CFG).skor == pytest.approx(55.0, abs=1e-9)
    cfg = cfg_uji({"skor.bobot": {"sinyal": 10, "kekuatan_relatif": 20, "likuiditas": 30, "jarak_stop": 40}})
    # 10 × 0,25 + 20 × 0,75 + 30 × 1 + 40 × 0,5 = 2,5 + 15 + 30 + 20 = 67,5
    k = hitung_skor(s, 0.75, cfg)
    assert k.skor == pytest.approx(67.5, abs=1e-9)
    assert k.tampilan == "67,5"


def test_skor_komponen_dipotong_ke_0_1():
    # Semua komponen di atas 1 → skor tepat 100 (bukan lebih).
    tinggi = sinyal("AAAA", harga=("1000", "980"), rata_nilai=5e11, k_sinyal=1.7)
    k = hitung_skor(tinggi, 1.4, CFG)
    assert (k.k_sinyal, k.k_rs, k.k_likuid, k.k_stop) == (1.0, 1.0, 1.0, 1.0)
    assert k.skor == pytest.approx(100.0, abs=1e-9)
    # Semua komponen di bawah 0 (atau NaN) → skor 0.
    rendah = sinyal("AAAA", harga=("1000", "880"), rata_nilai=5e9, k_sinyal=-0.3)
    k = hitung_skor(rendah, -0.2, CFG)
    assert (k.k_sinyal, k.k_rs, k.k_likuid, k.k_stop) == (0.0, 0.0, 0.0, 0.0)
    assert k.skor == 0.0
    assert hitung_skor(rendah, math.nan, CFG).k_rs == 0.0


def test_tampilan_satu_desimal_dan_catatan_heuristik():
    assert KomponenSkor(0, 0, 0, 0, 70.7142857).tampilan == "70,7"
    assert KomponenSkor(0, 0, 0, 0, 50.04).tampilan == KomponenSkor(0, 0, 0, 0, 50.01).tampilan == "50,0"
    assert "bukan peluang keberhasilan" in CATATAN_SKOR


# ---------------------------------------------------------------------------
# §13.1 K_rs: peringkat persentil Ret(rs_lookback) di antara saham eligible
# ---------------------------------------------------------------------------

CFG_RS = cfg_uji({"skor.rs_lookback": 5})
POS_T = 9  # 10 bar (posisi 0..9); Ret5_t = close_9 / close_4 − 1


def _seri(akhir: float) -> "object":
    """Close 100 selama 9 bar, lalu `akhir` pada t → Ret5 = akhir/100 − 1."""
    return bar([100.0] * POS_T + [akhir])


def _dv(saham: dict, cfg=CFG_RS):
    return pasar(saham, None, cfg, tgl(POS_T)).tampilan(tgl(POS_T))


def test_k_rs_persentil_tanpa_nilai_sama():
    # Ret: AAAA +20%, BBBB −10%, CCCC +10%, DDDD +5% → peringkat 4, 1, 3, 2 (n = 4)
    dv = _dv({"AAAA": _seri(120), "BBBB": _seri(90), "CCCC": _seri(110), "DDDD": _seri(105)})
    k = hitung_k_rs(dv, ["AAAA", "BBBB", "CCCC", "DDDD"])
    # (peringkat − 1)/(n − 1): 3/3, 0/3, 2/3, 1/3
    assert k == pytest.approx({"AAAA": 1.0, "BBBB": 0.0, "CCCC": 2 / 3, "DDDD": 1 / 3}, abs=1e-12)


def test_k_rs_nilai_sama_memakai_rata_rata_peringkat():
    # Ret −10%, +5%, +5%, +20% → peringkat 1, 2,5, 2,5, 4 → 0, 1,5/3, 1,5/3, 1
    dv = _dv({"AAAA": _seri(90), "BBBB": _seri(105), "CCCC": _seri(105), "DDDD": _seri(120)})
    k = hitung_k_rs(dv, ["AAAA", "BBBB", "CCCC", "DDDD"])
    assert k == pytest.approx({"AAAA": 0.0, "BBBB": 0.5, "CCCC": 0.5, "DDDD": 1.0}, abs=1e-12)
    # Tiga sama di puncak: peringkat (2 + 3 + 4)/3 = 3 → (3 − 1)/3 = 2/3 (bukan 1 dan bukan 1/3).
    dv = _dv({"AAAA": _seri(90), "BBBB": _seri(120), "CCCC": _seri(120), "DDDD": _seri(120)})
    k = hitung_k_rs(dv, ["AAAA", "BBBB", "CCCC", "DDDD"])
    assert k == pytest.approx({"AAAA": 0.0, "BBBB": 2 / 3, "CCCC": 2 / 3, "DDDD": 2 / 3}, abs=1e-12)
    # Semua sama: peringkat 2,5 → 0,5.
    dv = _dv({"AAAA": _seri(105), "BBBB": _seri(105), "CCCC": _seri(105), "DDDD": _seri(105)})
    assert hitung_k_rs(dv, ["AAAA", "BBBB", "CCCC", "DDDD"]) == pytest.approx(
        {"AAAA": 0.5, "BBBB": 0.5, "CCCC": 0.5, "DDDD": 0.5}, abs=1e-12
    )


def test_k_rs_n_satu_bernilai_setengah():
    dv = _dv({"AAAA": _seri(90), "BBBB": _seri(120)})
    assert hitung_k_rs(dv, ["AAAA"]) == {"AAAA": 0.5}
    # n = 2 sebagai pembanding: 0 dan 1.
    assert hitung_k_rs(dv, ["AAAA", "BBBB"]) == {"AAAA": 0.0, "BBBB": 1.0}
    assert hitung_k_rs(dv, []) == {}


def test_k_rs_ret_nan_bernilai_nol_dan_tidak_ikut_n():
    saham = {
        "AAAA": _seri(90),
        "BBBB": _seri(105),
        "CCCC": _seri(120),
        "EEEE": bar([100.0, 100.0, 300.0], mulai=POS_T - 2),  # 3 bar < rs_lookback + 1 → Ret NaN
        "FFFF": bar([100.0] * POS_T),  # tanpa bar pada t → Ret_t NaN
    }
    dv = _dv(saham)
    k = hitung_k_rs(dv, list(saham))
    # n = 3 (bukan 5): AAAA 0, BBBB (2 − 1)/2 = 0,5, CCCC 1; NaN → 0.
    assert k == pytest.approx({"AAAA": 0.0, "BBBB": 0.5, "CCCC": 1.0, "EEEE": 0.0, "FFFF": 0.0}, abs=1e-12)
    # Satu-satunya Ret yang sah di antara NaN → n = 1 → 0,5.
    assert hitung_k_rs(dv, ["EEEE", "BBBB", "FFFF"]) == {"BBBB": 0.5, "EEEE": 0.0, "FFFF": 0.0}


def test_k_rs_memakai_rs_lookback_config():
    # AAAA: naik di awal lalu datar → Ret5 = 110/100 − 1 = +10%, Ret3 = 110/110 − 1 = 0.
    # BBBB: datar lalu naik di akhir → Ret5 = Ret3 = +5%.
    saham = {"AAAA": bar([100.0] * 5 + [105.0, 110.0, 110.0, 110.0, 110.0]), "BBBB": _seri(105)}
    assert hitung_k_rs(_dv(saham, CFG_RS), ["AAAA", "BBBB"]) == {"AAAA": 1.0, "BBBB": 0.0}
    cfg3 = cfg_uji({"skor.rs_lookback": 3})
    assert hitung_k_rs(_dv(saham, cfg3), ["AAAA", "BBBB"]) == {"AAAA": 0.0, "BBBB": 1.0}


def test_k_rs_tidak_bergantung_pada_sinyal_strategi_atau_posisi():
    saham = {"AAAA": _seri(90), "BBBB": _seri(105), "CCCC": _seri(110), "DDDD": _seri(120)}
    dv = _dv(saham)
    semua = ["AAAA", "BBBB", "CCCC", "DDDD"]
    acuan = hitung_k_rs(dv, semua)
    assert acuan["BBBB"] == pytest.approx(1 / 3, abs=1e-12)
    # Urutan dan duplikat kode eligible tidak berpengaruh.
    assert hitung_k_rs(dv, ["DDDD", "BBBB", "DDDD", "CCCC", "AAAA"]) == acuan
    # Sinyal hanya untuk BBBB (tiga strategi): K_rs tetap diperingkat atas SEMUA eligible (n = 4),
    # termasuk DDDD yang sedang dipegang, dan sama untuk setiap strategi.
    sb = beri_skor(dv, [sinyal("BBBB", "PB"), sinyal("BBBB", "BO"), sinyal("BBBB", "MR")], semua)
    assert [x.komponen.k_rs for x in sb] == pytest.approx([1 / 3] * 3, abs=1e-12)
    assert sb[0].skor == pytest.approx(hitung_skor(sb[0].sinyal, 1 / 3, CFG_RS).skor, abs=1e-12)
    # Seleksi dengan DDDD dipegang tidak menghitung ulang skor/K_rs.
    pf = porto(pos("DDDD", "SEKTOR_DDDD"))
    h = jalankan(sb, portofolio=pf, cfg=CFG_RS)
    assert [k.sinyal for k in h.kandidat if k.kode == "BBBB"] and all(
        k.sinyal.komponen.k_rs == pytest.approx(1 / 3, abs=1e-12) for k in h.kandidat
    )


# ---------------------------------------------------------------------------
# Presisi penuh vs tampilan 1 desimal
# ---------------------------------------------------------------------------


def test_peringkat_memakai_presisi_penuh_walau_tampilan_sama():
    # Tampil sama ("50,0"). Jika peringkat memakai tampilan, AAAA menang lewat RataNilai lebih besar.
    zzzz = berskor("ZZZZ", 50.04, rata_nilai=20e9)
    aaaa = berskor("AAAA", 50.01, rata_nilai=30e9)
    assert zzzz.komponen.tampilan == aaaa.komponen.tampilan == "50,0"
    pf = porto(pos("P1", "S1"), pos("P2", "S2"))  # slot = 3 − 2 = 1
    h = jalankan([aaaa, zzzz], portofolio=pf)
    assert ringkas(h) == [("ZZZZ", "PB", REKOMENDASI), ("AAAA", "PB", SLOT_PENUH)]
    assert kunci_urutan(zzzz) < kunci_urutan(aaaa)


@pytest.mark.parametrize("terbalik", [False, True])
def test_deduplikasi_memakai_presisi_penuh(terbalik):
    # Skor tampil sama; PB akan menang bila seri, tetapi BO lebih tinggi pada presisi penuh.
    # Hasil tidak boleh bergantung pada urutan masukan.
    daftar = [berskor("AAAA", 50.01, "PB", C), berskor("AAAA", 50.04, "BO", C)]
    h = jalankan(daftar[::-1] if terbalik else daftar)
    assert satu(h, "AAAA", "BO").status == REKOMENDASI
    dup = satu(h, "AAAA", "PB")
    assert dup.status == DUPLIKAT_STRATEGI
    assert dup.alasan == "kalah dari sinyal BO untuk saham yang sama (skor 50,0)"
    # Seri tepat → PB dipertahankan.
    daftar = [berskor("AAAA", 50.04, "BO", C), berskor("AAAA", 50.04, "PB", C)]
    h = jalankan(daftar[::-1] if terbalik else daftar)
    assert (satu(h, "AAAA", "PB").status, satu(h, "AAAA", "BO").status) == (REKOMENDASI, DUPLIKAT_STRATEGI)


# ---------------------------------------------------------------------------
# §13.2 seluruh langkah dalam satu run
# ---------------------------------------------------------------------------


def test_alur_lengkap_langkah_1_sampai_9():
    pf = porto(pos("HOLD", "S_HOLD"), kas="300000")  # slot = 3 − 1 = 2; ekuitas tetap 3 juta
    sektor = {
        "INVL": "S0", "GATE": "S0", "DUPL": "S1", "HOLD": "S_HOLD", "SEKT": "S1", "MHAL": "S2",
        "ALOK": "S3", "LIKU": "S4", "KASK": "S5", "MRCC": "S6", "SLOT": "S7",
    }
    daftar = [  # sengaja tidak urut
        berskor("SLOT", 88, "MR", C),
        berskor("KASK", 90, "PB", A),
        berskor("DUPL", 96, "MR", C),
        berskor("INVL", 99, "PB", A, valid=False, alasan="jarak stop 3,50 ATR > 3,00 ATR"),
        berskor("LIKU", 91, "PB", A, rata_nilai=20_299_999.0),
        berskor("GATE", 98, "BO", A),
        berskor("MHAL", 93, "PB", B),
        berskor("HOLD", 95, "PB", A),
        berskor("ALOK", 92, "PB", MAHAL),
        berskor("DUPL", 97, "PB", A),
        berskor("MRCC", 89, "MR", C),
        berskor("SEKT", 94, "PB", A),
    ]
    h = jalankan(daftar, portofolio=pf, lolos={"PB", "MR"}, sektor=sektor)
    # Semua sinyal tercatat dalam urutan langkah 5 (skor ↓).
    assert ringkas(h) == [
        ("INVL", "PB", STOP_TERLALU_JAUH),  # 2
        ("GATE", "BO", BAYANGAN_GERBANG),  # 3
        ("DUPL", "PB", REKOMENDASI),  # 9h: kas 300.000 − 203.304,50 = 96.695,50
        ("DUPL", "MR", DUPLIKAT_STRATEGI),  # 4
        ("HOLD", "PB", SUDAH_DIPEGANG),  # 6
        ("SEKT", "PB", BATAS_SEKTOR),  # 9c: S1 sudah terisi DUPL
        ("MHAL", "PB", TIDAK_TERJANGKAU),  # 9d
        ("ALOK", "PB", TIDAK_TERJANGKAU),  # 9e
        ("LIKU", "PB", TIDAK_LIKUID),  # 9f
        ("KASK", "PB", KAS_KURANG),  # 9g: 203.304,50 > 96.695,50
        ("MRCC", "MR", REKOMENDASI),  # 9h: 2 lot (kas), 2 × 45.868,70 = 91.737,40
        ("SLOT", "MR", SLOT_PENUH),  # 9a: 2 slot terpakai
    ]
    assert [(k.kode, k.sizing.lot) for k in h.rekomendasi] == [("DUPL", 1), ("MRCC", 2)]
    assert satu(h, "INVL").alasan == "jarak stop 3,50 ATR > 3,00 ATR"
    assert satu(h, "DUPL", "MR").alasan == "kalah dari sinyal PB untuk saham yang sama (skor 97,0)"
    assert satu(h, "MHAL").alasan.startswith("risiko 1 lot = Rp 66.841,63")
    assert satu(h, "ALOK").alasan.startswith("nilai 1 lot")
    assert satu(h, "KASK").alasan == "biaya 1 lot Rp 203.304,50 > kas tersedia Rp 96.695,50"
    assert satu(h, "MRCC", "MR").sizing.pembatas == "kas"
    # Status sebelum langkah 9 dan cek a–c tidak menghitung sizing; d–h menghitung.
    tanpa_sizing = {"INVL", "GATE", "HOLD", "SEKT", "SLOT"}
    for k in h.kandidat:
        if k.kode in tanpa_sizing or k.status == DUPLIKAT_STRATEGI:
            assert k.sizing is None, k
        else:
            assert k.sizing is not None, k
    assert (h.slot_awal, h.kas_awal, h.anggaran_risiko, h.alokasi_maks) == (
        2, Decimal(300000), Decimal(30000), Decimal(1000000)
    )
    assert h.tanggal == T_SINYAL and h.status_pasar == "BOLEH_BELI"
    assert h.status("SLOT", "MR") == SLOT_PENUH and h.status("SLOT", "PB") is None


# ---------------------------------------------------------------------------
# Langkah 2–5
# ---------------------------------------------------------------------------


def test_stop_terlalu_jauh_keluar_dari_antrean():
    pf = porto(pos("P1", "S1"), pos("P2", "S2"))  # slot 1
    h = jalankan(
        [
            berskor("XXXX", 99, "PB", valid=False, alasan="jarak stop terlalu jauh"),
            berskor("XXXX", 60, "BO", C),  # sinyal valid saham yang sama tidak menjadi duplikat
            berskor("YYYY", 98, "BO", valid=False),  # BO belum lolos gerbang, tetapi langkah 2 lebih dulu
            berskor("ZZZZ", 50, "MR", C),
        ],
        portofolio=pf,
        lolos={"PB", "MR"},
    )
    assert ringkas(h) == [
        ("XXXX", "PB", STOP_TERLALU_JAUH),
        ("YYYY", "BO", STOP_TERLALU_JAUH),
        ("XXXX", "BO", BAYANGAN_GERBANG),
        ("ZZZZ", "MR", REKOMENDASI),  # sinyal tidak valid tidak memakan slot
    ]
    assert satu(h, "XXXX").alasan == "jarak stop terlalu jauh"
    assert satu(h, "YYYY", "BO").alasan == ""


def test_bayangan_gerbang_tidak_menggugurkan_sinyal_strategi_lolos():
    # BO (belum lolos) berskor lebih tinggi pada saham yang sama dengan PB (lolos).
    h = jalankan([berskor("AAAA", 90, "BO", C), berskor("AAAA", 40, "PB", C)], lolos={"PB", "MR"})
    assert satu(h, "AAAA", "PB").status == REKOMENDASI
    bayang = satu(h, "AAAA", "BO")
    assert (bayang.status, bayang.alasan) == (BAYANGAN_GERBANG, "strategi BO belum lolos gerbang backtest")
    assert bayang.sizing is None
    # Tanpa gerbang (semua lolos), BO yang menang deduplikasi.
    h = jalankan([berskor("AAAA", 90, "BO", C), berskor("AAAA", 40, "PB", C)], lolos={"PB", "BO", "MR"})
    assert (satu(h, "AAAA", "BO").status, satu(h, "AAAA", "PB").status) == (REKOMENDASI, DUPLIKAT_STRATEGI)


def test_bayangan_gerbang_tidak_memakan_slot_dan_tetap_di_jangan_beli():
    pf = porto(pos("P1", "S1"), pos("P2", "S2"))  # slot 1
    daftar = [berskor("GGGG", 90, "BO", C), berskor("LLLL", 40, "MR", C)]
    h = jalankan(daftar, portofolio=pf, lolos=["pb", "mr"])  # huruf kecil diterima
    assert ringkas(h) == [("GGGG", "BO", BAYANGAN_GERBANG), ("LLLL", "MR", REKOMENDASI)]
    h = jalankan(daftar, status="JANGAN_BELI", lolos={"PB", "MR"})
    assert ringkas(h) == [("GGGG", "BO", BAYANGAN_GERBANG), ("LLLL", "MR", BAYANGAN_PASAR)]
    h = jalankan(daftar, lolos=set())  # belum ada strategi lolos
    assert {k.status for k in h.kandidat} == {BAYANGAN_GERBANG} and h.rekomendasi == []


@pytest.mark.parametrize(
    ("skor", "pemenang"),
    [
        ({"PB": 60, "BO": 70, "MR": 65}, "BO"),
        ({"PB": 60, "BO": 65, "MR": 70}, "MR"),
        ({"PB": 50, "BO": 50, "MR": 50}, "PB"),  # seri → PB, BO, MR
        ({"BO": 50, "MR": 50}, "BO"),
        ({"PB": 40, "BO": 50, "MR": 50}, "BO"),  # seri di puncak antara BO dan MR
    ],
)
@pytest.mark.parametrize("terbalik", [False, True])
def test_deduplikasi_skor_tertinggi_seri_pb_bo_mr(skor, pemenang, terbalik):
    daftar = [berskor("AAAA", s, st, C) for st, s in skor.items()]
    h = jalankan(daftar[::-1] if terbalik else daftar)
    for st in skor:
        k = satu(h, "AAAA", st)
        if st == pemenang:
            assert k.status == REKOMENDASI
        else:
            assert k.status == DUPLIKAT_STRATEGI
            assert k.alasan == f"kalah dari sinyal {pemenang} untuk saham yang sama (skor {skor[pemenang]},0)"


def test_urutan_skor_lalu_rata_nilai_lalu_kode():
    daftar = [
        berskor("BBBB", 70, rata_nilai=20e9),
        berskor("AAAA", 70, rata_nilai=20e9),
        berskor("CCCC", 70, rata_nilai=25e9),
        berskor("DDDD", 80, rata_nilai=10e9),
    ]
    # JANGAN BELI: daftar pantau mengikuti urutan langkah 5.
    h = jalankan(daftar, status="JANGAN_BELI")
    assert [k.kode for k in h.daftar_pantau] == ["DDDD", "CCCC", "AAAA", "BBBB"]
    # Urutan menentukan siapa yang mendapat slot.
    pf = porto(pos("P1", "S1"), pos("P2", "S2"))  # slot 1
    h = jalankan(daftar[:3], portofolio=pf)  # skor sama: CCCC menang lewat RataNilai
    assert [k.kode for k in h.rekomendasi] == ["CCCC"]
    h = jalankan(daftar[:2], portofolio=pf)  # skor & RataNilai sama: AAAA menang lewat kode
    assert ringkas(h) == [("AAAA", "PB", REKOMENDASI), ("BBBB", "PB", SLOT_PENUH)]


# ---------------------------------------------------------------------------
# Langkah 6, 6a, 7
# ---------------------------------------------------------------------------


def test_sudah_dipegang_tidak_memakan_slot():
    pf = porto(pos("HOLD", "S_HOLD"), pos("P2", "S2"))  # slot 1
    h = jalankan([berskor("HOLD", 99, "PB", C), berskor("HOLD", 90, "MR", C), berskor("NEW", 50, "PB", C)],
                 portofolio=pf)
    assert ringkas(h) == [
        ("HOLD", "PB", SUDAH_DIPEGANG),  # deduplikasi lebih dulu
        ("HOLD", "MR", DUPLIKAT_STRATEGI),
        ("NEW", "PB", REKOMENDASI),
    ]
    assert satu(h, "HOLD").alasan == "saham sedang dipegang; tidak ada penambahan posisi"


def test_sudah_dipegang_berlaku_juga_untuk_jual_terjadwal_jangan_beli_dan_blokir():
    pf = porto(pos("HOLD", "S_HOLD", jual=True))
    assert satu(jalankan([berskor("HOLD", 50)], portofolio=pf), "HOLD").status == SUDAH_DIPEGANG
    h = jalankan([berskor("HOLD", 50), berskor("NEW", 40)], portofolio=pf, status="JANGAN_BELI")
    assert ringkas(h) == [("HOLD", "PB", SUDAH_DIPEGANG), ("NEW", "PB", BAYANGAN_PASAR)]
    pf = porto(pos("HOLD", "S_HOLD"), blokir="ABCD")
    h = jalankan([berskor("HOLD", 50), berskor("NEW", 40)], portofolio=pf)
    assert ringkas(h) == [("HOLD", "PB", SUDAH_DIPEGANG), ("NEW", "PB", DIBLOKIR_PORTOFOLIO)]


@pytest.mark.parametrize("status_pasar", ["BOLEH_BELI", "HATI_HATI", "JANGAN_BELI"])
def test_diblokir_portofolio_menghentikan_seleksi(status_pasar):
    pf = porto(blokir="ABCD: lonjakan harga tanpa data split")
    h = jalankan(
        [berskor("AAAA", 90, "PB", C), berskor("AAAA", 80, "MR", C), berskor("BBBB", 70, "BO", C),
         berskor("CCCC", 60, "MR", C, valid=False)],
        portofolio=pf, status=status_pasar, lolos={"PB", "MR"},
    )
    # 6a sebelum 7: JANGAN BELI pun → DIBLOKIR_PORTOFOLIO; status langkah 2–4 tetap.
    assert ringkas(h) == [
        ("AAAA", "PB", DIBLOKIR_PORTOFOLIO),
        ("AAAA", "MR", DUPLIKAT_STRATEGI),
        ("BBBB", "BO", BAYANGAN_GERBANG),
        ("CCCC", "MR", STOP_TERLALU_JAUH),
    ]
    alasan = satu(h, "AAAA").alasan
    assert alasan == "PORTOFOLIO TIDAK DAPAT DIEVALUASI: ABCD: lonjakan harga tanpa data split"
    assert "ABCD" in alasan
    assert h.rekomendasi == [] and h.daftar_pantau == []
    assert all(k.sizing is None for k in h.kandidat)


def test_jangan_beli_menjadi_bayangan_pasar_tanpa_sizing():
    h = jalankan([berskor("AAAA", 60, "PB", A), berskor("BBBB", 70, "MR", C)], status="JANGAN_BELI")
    assert ringkas(h) == [("BBBB", "MR", BAYANGAN_PASAR), ("AAAA", "PB", BAYANGAN_PASAR)]
    assert [k.kode for k in h.daftar_pantau] == ["BBBB", "AAAA"]
    assert h.rekomendasi == []
    assert all(k.sizing is None and k.alasan == "status pasar JANGAN BELI (daftar pantau)" for k in h.kandidat)


# ---------------------------------------------------------------------------
# Langkah 8–9a: slot
# ---------------------------------------------------------------------------


def test_slot_penuh_batas_dua_sisi():
    daftar = [berskor(f"K{i}", 90 - i, "MR", C) for i in range(4)]
    # Tanpa posisi: 3 slot → 3 REKOMENDASI, yang ke-4 SLOT_PENUH.
    h = jalankan(daftar)
    assert [k.status for k in h.kandidat] == [REKOMENDASI] * 3 + [SLOT_PENUH]
    assert satu(h, "K3", "MR").alasan == "slot posisi penuh (3 posisi)"
    assert h.slot_awal == 3
    # 2 posisi → slot 1.
    h = jalankan(daftar, portofolio=porto(pos("P1", "S1"), pos("P2", "S2")))
    assert [k.status for k in h.kandidat] == [REKOMENDASI] + [SLOT_PENUH] * 3 and h.slot_awal == 1
    # 3 posisi → slot 0.
    h = jalankan(daftar, portofolio=porto(pos("P1", "S1"), pos("P2", "S2"), pos("P3", "S3")))
    assert [k.status for k in h.kandidat] == [SLOT_PENUH] * 4 and h.slot_awal == 0
    # 4 posisi (mis. ada MANUAL di atas batas) → slot tetap 0, tidak negatif.
    h = jalankan(daftar, portofolio=porto(*(pos(f"P{i}", f"S{i}") for i in range(4))))
    assert [k.status for k in h.kandidat] == [SLOT_PENUH] * 4 and h.slot_awal == 0


@pytest.mark.parametrize("bebas", [True, False])
def test_slot_dibebaskan_jual_terjadwal_hanya_bila_flag(bebas):
    cfg = cfg_uji({"risiko.slot_bebas_dari_jual_terjadwal": bebas})
    pf = porto(pos("P1", "S1"), pos("P2", "S2"), pos("JUAL", "S_JUAL", jual=True))
    h = jalankan([berskor("NEW", 50, "MR", C)], portofolio=pf, cfg=cfg)
    k = satu(h, "NEW", "MR")
    if bebas:  # slot = 3 − (3 − 1) = 1, dan slot itu bergantung pada jual JUAL
        assert (h.slot_awal, k.status, k.bergantung_jual) == (1, REKOMENDASI, ("JUAL",))
    else:  # slot = 3 − 3 = 0
        assert (h.slot_awal, k.status, k.bergantung_jual) == (0, SLOT_PENUH, ())


def test_bergantung_jual_hanya_untuk_kandidat_yang_memakai_slot_jual():
    # 2 posisi, 1 dijadwalkan jual: slot = 3 − (2 − 1) = 2; tanpa jual hanya 1 slot.
    pf = porto(pos("P1", "S1"), pos("JUAL", "S_JUAL", jual=True))
    h = jalankan([berskor("K1", 60, "MR", C), berskor("K2", 50, "MR", C)], portofolio=pf)
    assert [k.status for k in h.kandidat] == [REKOMENDASI, REKOMENDASI]
    assert satu(h, "K1", "MR").bergantung_jual == ()
    assert satu(h, "K2", "MR").bergantung_jual == ("JUAL",)


# ---------------------------------------------------------------------------
# 9b: batas entry harian
# ---------------------------------------------------------------------------


def test_batas_entry_harian_hati_hati_satu_per_hari():
    # HATI-HATI: anggaran 15.000. A (risiko 1 lot 15.164,83) tak terjangkau dan tidak memakai kuota.
    h = jalankan([berskor("AAAA", 90, "PB", A), berskor("C1", 80, "MR", C), berskor("C2", 70, "MR", C)],
                 status="HATI_HATI")
    assert ringkas(h) == [
        ("AAAA", "PB", TIDAK_TERJANGKAU),
        ("C1", "MR", REKOMENDASI),
        ("C2", "MR", BATAS_ENTRY_HARIAN),
    ]
    assert satu(h, "C2", "MR").alasan == "batas entry baru per hari tercapai (1, status HATI-HATI)"
    assert satu(h, "C2", "MR").sizing is None
    # BOLEH BELI dengan maks_entry_baru_per_hari null: hanya dibatasi slot.
    h = jalankan([berskor("C1", 80, "MR", C), berskor("C2", 70, "MR", C)])
    assert [k.status for k in h.kandidat] == [REKOMENDASI, REKOMENDASI]


def test_batas_entry_harian_dari_config():
    daftar = [berskor(f"C{i}", 80 - i, "MR", C) for i in range(3)]
    h = jalankan(daftar, cfg=cfg_uji({"risiko.maks_entry_baru_per_hari": 1}))
    assert [k.status for k in h.kandidat] == [REKOMENDASI, BATAS_ENTRY_HARIAN, BATAS_ENTRY_HARIAN]
    assert satu(h, "C1", "MR").alasan == "batas entry baru per hari tercapai (1, status BOLEH BELI)"
    h = jalankan(daftar, status="HATI_HATI", cfg=cfg_uji({"risiko.hati_hati.maks_entry_baru_per_hari": 2}))
    assert [k.status for k in h.kandidat] == [REKOMENDASI, REKOMENDASI, BATAS_ENTRY_HARIAN]


# ---------------------------------------------------------------------------
# 9c: batas sektor
# ---------------------------------------------------------------------------


def test_batas_sektor_menghitung_kandidat_yang_diterima():
    sektor = {"K1": "Energi", "K2": "Energi", "K3": "Bank"}
    h = jalankan([berskor("K1", 60, "MR", C), berskor("K2", 50, "MR", C), berskor("K3", 40, "MR", C)],
                 sektor=sektor)
    assert [k.status for k in h.kandidat] == [REKOMENDASI, BATAS_SEKTOR, REKOMENDASI]
    assert satu(h, "K2", "MR").alasan == "sektor Energi sudah mencapai 1 posisi"


def test_batas_sektor_menghitung_posisi_manual_dan_di_luar_universe():
    # Posisi "LUAR" (di luar universe, sektor dari portfolio.csv) dan "MANL" (strategi MANUAL) hanyalah
    # PosisiTerbuka; `sektor_dari` ketat dan tidak mengenal keduanya → sektornya dari posisi itu sendiri.
    pf = porto(pos("LUAR", "Energi"), pos("MANL", "Bank"))
    sektor = {"K1": "Energi", "K2": "Bank", "K3": "Properti"}
    h = jalankan([berskor("K1", 60, "MR", C), berskor("K2", 50, "MR", C), berskor("K3", 40, "MR", C)],
                 portofolio=pf, sektor=sektor)
    assert [k.status for k in h.kandidat] == [BATAS_SEKTOR, BATAS_SEKTOR, REKOMENDASI]
    assert h.slot_awal == 1  # keduanya juga menempati slot


@pytest.mark.parametrize("bebas", [True, False])
def test_batas_sektor_dikurangi_jual_terjadwal_hanya_bila_flag(bebas):
    cfg = cfg_uji({"risiko.slot_bebas_dari_jual_terjadwal": bebas})
    pf = porto(pos("JUAL", "Energi", jual=True))
    sektor = {"K1": "Energi", "K2": "Bank"}
    h = jalankan([berskor("K1", 60, "MR", C), berskor("K2", 50, "MR", C)], portofolio=pf, cfg=cfg, sektor=sektor)
    k1, k2 = satu(h, "K1", "MR"), satu(h, "K2", "MR")
    if bebas:  # Energi: 1 − 1 = 0 < 1 → diterima, bergantung pada jual JUAL (slot tidak: 3 − 1 = 2 ≥ 1)
        assert (k1.status, k1.bergantung_jual) == (REKOMENDASI, ("JUAL",))
    else:
        assert (k1.status, k1.bergantung_jual) == (BATAS_SEKTOR, ())
    assert (k2.status, k2.bergantung_jual) == (REKOMENDASI, ())


def test_batas_sektor_dari_config_dua_sisi_dan_null():
    cfg2 = cfg_uji({"risiko.maks_per_sektor": 2})
    pf = porto(pos("LAMA", "Energi"))
    sektor = {"K1": "Energi", "K2": "Energi"}
    h = jalankan([berskor("K1", 60, "MR", C), berskor("K2", 50, "MR", C)], portofolio=pf, cfg=cfg2, sektor=sektor)
    # Energi: 1 posisi + 0 diterima = 1 < 2 → K1 diterima; 1 + 1 = 2 ≥ 2 → K2 ditolak.
    assert [k.status for k in h.kandidat] == [REKOMENDASI, BATAS_SEKTOR]
    assert satu(h, "K2", "MR").alasan == "sektor Energi sudah mencapai 2 posisi"
    # null = tanpa batas sektor.
    cfg_n = cfg_uji({"risiko.maks_per_sektor": None})
    sektor = {"K1": "Energi", "K2": "Energi", "K3": "Energi"}
    h = jalankan([berskor(k, 60 - i, "MR", C) for i, k in enumerate(sektor)], cfg=cfg_n, sektor=sektor)
    assert [k.status for k in h.kandidat] == [REKOMENDASI] * 3


# ---------------------------------------------------------------------------
# 9d–9h: sizing (angka §14.2)
# ---------------------------------------------------------------------------


def test_tidak_terjangkau_risiko_alasan_boleh_beli_dan_hati_hati():
    h = jalankan([berskor("AAAA", 90, "PB", A), berskor("BBBB", 80, "BO", B), berskor("CCCC", 70, "MR", C)])
    assert h.anggaran_risiko == Decimal(30000)
    assert ringkas(h) == [("AAAA", "PB", REKOMENDASI), ("BBBB", "BO", TIDAK_TERJANGKAU), ("CCCC", "MR", REKOMENDASI)]
    # risiko 1 lot B = 66.841,625 → "Rp 66.841,63"; 66.841,625/3.000.000 = 2,228% → "2,23%"
    assert satu(h, "BBBB", "BO").alasan == (
        "risiko 1 lot = Rp 66.841,63 (2,23% ekuitas) > anggaran Rp 30.000 (1%, status BOLEH BELI)"
    )
    assert satu(h, "BBBB", "BO").sizing.lot_risiko == 0
    assert satu(h, "AAAA").sizing.lot == 1 and satu(h, "AAAA").sizing.risiko_idr == Decimal("15164.828")
    assert satu(h, "CCCC", "MR").sizing.lot == 5
    assert satu(h, "CCCC", "MR").sizing.risiko_idr == Decimal("25060.4875")
    # HATI-HATI: anggaran 30.000 × 0,5 = 15.000 → A tak terjangkau (15.164,83 > 15.000), C 2 lot.
    h = jalankan([berskor("AAAA", 90, "PB", A), berskor("CCCC", 70, "MR", C)], status="HATI_HATI")
    assert h.anggaran_risiko == Decimal(15000)
    assert satu(h, "AAAA").status == TIDAK_TERJANGKAU
    assert satu(h, "AAAA").alasan == (
        "risiko 1 lot = Rp 15.164,83 (0,51% ekuitas) > anggaran Rp 15.000 (0,50%, status HATI-HATI)"
    )
    c = satu(h, "CCCC", "MR")
    assert (c.status, c.sizing.lot, c.sizing.risiko_idr) == (REKOMENDASI, 2, Decimal("10024.195"))


def test_tidak_terjangkau_alokasi_dan_alokasi_dari_config():
    h = jalankan([berskor("MHAL", 50, "PB", MAHAL)])
    k = satu(h, "MHAL")
    assert h.alokasi_maks == Decimal(1000000)  # 3.000.000 ÷ 3 tepat
    assert k.status == TIDAK_TERJANGKAU
    assert k.alasan == "nilai 1 lot Rp 1.201.800 > alokasi maks Rp 1.000.000"
    assert (k.sizing.lot_risiko, k.sizing.lot_alokasi) == (1, 0)
    # alokasi 50% = 1.500.000 ≥ 1.201.800 → terjangkau.
    h = jalankan([berskor("MHAL", 50, "PB", MAHAL)], cfg=cfg_uji({"risiko.alokasi_maks_pct": 50}))
    assert h.alokasi_maks == Decimal(1500000)
    assert satu(h, "MHAL").status == REKOMENDASI


def test_tidak_likuid_batas_dua_sisi():
    # lot_likuid = floor(1% × RataNilai / (2.030 × 100)); batas 1 lot tepat pada RataNilai 20.300.000.
    h = jalankan([berskor("PAS", 60, "PB", A, rata_nilai=20_300_000.0),
                  berskor("KRG", 50, "PB", A, rata_nilai=20_299_999.0)])
    pas, krg = satu(h, "PAS"), satu(h, "KRG")
    assert (pas.status, pas.sizing.lot_likuid, pas.sizing.lot) == (REKOMENDASI, 1, 1)
    assert (krg.status, krg.sizing.lot_likuid) == (TIDAK_LIKUID, 0)
    assert krg.alasan == "1 lot > 1% rata-rata nilai transaksi harian (Rp 20.299.999)"


def test_prioritas_cek_a_sampai_g():
    # a sebelum b: HATI-HATI, slot 1, kandidat kedua melanggar keduanya → SLOT_PENUH.
    pf1 = porto(pos("P1", "S1"), pos("P2", "S2"))
    h = jalankan([berskor("C1", 60, "MR", C), berskor("C2", 50, "MR", C)], status="HATI_HATI", portofolio=pf1)
    assert satu(h, "C2", "MR").status == SLOT_PENUH
    # b sebelum c: HATI-HATI, sektor sama → BATAS_ENTRY_HARIAN.
    sektor = {"C1": "Energi", "C2": "Energi"}
    h = jalankan([berskor("C1", 60, "MR", C), berskor("C2", 50, "MR", C)], status="HATI_HATI", sektor=sektor)
    assert satu(h, "C2", "MR").status == BATAS_ENTRY_HARIAN
    # c sebelum d: B (tak terjangkau) di sektor penuh → BATAS_SEKTOR tanpa sizing.
    h = jalankan([berskor("BBBB", 50, "BO", B)], portofolio=porto(pos("LAMA", "Energi")), sektor={"BBBB": "Energi"})
    assert (satu(h, "BBBB", "BO").status, satu(h, "BBBB", "BO").sizing) == (BATAS_SEKTOR, None)
    # d sebelum e: gagal risiko dan alokasi → alasan risiko.
    k = satu(jalankan([berskor("LEBR", 50, "PB", MAHAL_LEBAR)]), "LEBR")
    assert k.status == TIDAK_TERJANGKAU and k.alasan.startswith("risiko 1 lot = Rp 105.647,25")
    # e sebelum f: gagal alokasi dan likuiditas → alasan alokasi.
    k = satu(jalankan([berskor("MHAL", 50, "PB", MAHAL, rata_nilai=1e6)]), "MHAL")
    assert k.status == TIDAK_TERJANGKAU and k.alasan.startswith("nilai 1 lot")
    assert k.sizing.lot_likuid == 0
    # f sebelum g: gagal likuiditas dan kas → TIDAK_LIKUID.
    k = satu(jalankan([berskor("AAAA", 50, "PB", A, rata_nilai=1e6)], portofolio=porto(kas="1000")), "AAAA")
    assert k.status == TIDAK_LIKUID and k.sizing.lot_kas == 0


def test_kas_kurang_batas_dua_sisi():
    # biaya_beli(1) A = 203.000 × 1,0015 = 203.304,50
    k = satu(jalankan([berskor("AAAA", 50)], portofolio=porto(kas="203304.50")), "AAAA")
    assert (k.status, k.sizing.lot) == (REKOMENDASI, 1)
    k = satu(jalankan([berskor("AAAA", 50)], portofolio=porto(kas="203304.49")), "AAAA")
    assert k.status == KAS_KURANG
    assert k.alasan == "biaya 1 lot Rp 203.304,50 > kas tersedia Rp 203.304,49"


def test_kas_berkurang_setelah_setiap_rekomendasi():
    daftar = [berskor("A1", 90, "PB", A), berskor("A2", 80, "PB", A), berskor("C1", 70, "MR", C),
              berskor("C2", 60, "MR", C)]
    h = jalankan(daftar, portofolio=porto(kas="300000"))
    assert ringkas(h) == [
        ("A1", "PB", REKOMENDASI),  # kas 300.000 → 300.000 − 203.304,50 = 96.695,50
        ("A2", "PB", KAS_KURANG),  # 203.304,50 > 96.695,50 (tidak memakan slot)
        ("C1", "MR", REKOMENDASI),  # lot_kas = 2 (2 × 45.868,70 = 91.737,40) → kas 4.958,10
        ("C2", "MR", KAS_KURANG),  # 45.868,70 > 4.958,10
    ]
    assert h.kas_awal == Decimal(300000)
    c1 = satu(h, "C1", "MR")
    assert c1.sizing.kas_tersedia == Decimal("96695.50")
    assert (c1.sizing.lot, c1.sizing.pembatas, c1.sizing.biaya_beli_idr) == (2, "kas", Decimal("91737.40"))
    assert satu(h, "A2").alasan == "biaya 1 lot Rp 203.304,50 > kas tersedia Rp 96.695,50"
    assert satu(h, "C2", "MR").alasan == "biaya 1 lot Rp 45.868,70 > kas tersedia Rp 4.958,10"


@pytest.mark.parametrize("pakai", [True, False])
def test_dana_jual_langsung_dipakai_menambah_estimasi_hasil_jual(pakai):
    cfg = cfg_uji({"risiko.dana_jual_langsung_dipakai": pakai})
    pf = porto(pos("JUAL", "S_JUAL", jual=True), kas="100000", estimasi="150000")
    k = satu(h := jalankan([berskor("AAAA", 50)], portofolio=pf, cfg=cfg), "AAAA")
    if pakai:  # kas tersedia 100.000 + 150.000 = 250.000 ≥ 203.304,50
        assert (h.kas_awal, k.status, k.sizing.kas_tersedia) == (Decimal(250000), REKOMENDASI, Decimal(250000))
    else:
        assert (h.kas_awal, k.status) == (Decimal(100000), KAS_KURANG)


def test_izinkan_satu_lot_melebihi_risiko():
    h = jalankan([berskor("BBBB", 50, "BO", B)])
    assert satu(h, "BBBB", "BO").status == TIDAK_TERJANGKAU
    cfg = cfg_uji({"risiko.izinkan_satu_lot_melebihi_risiko": True})
    h = jalankan([berskor("BBBB", 50, "BO", B), berskor("AAAA", 40, "PB", A)], cfg=cfg)
    b = satu(h, "BBBB", "BO")
    assert (b.status, b.sizing.lot, b.sizing.risiko_di_atas_anggaran) == (REKOMENDASI, 1, True)
    assert b.alasan == "risiko 1 lot di atas anggaran (diizinkan config)"
    assert b.sizing.risiko_idr == Decimal("66841.625")
    a = satu(h, "AAAA")  # lot_risiko ≥ 1 tanpa bantuan flag → tanpa tanda
    assert (a.status, a.alasan, a.sizing.risiko_di_atas_anggaran) == (REKOMENDASI, "", False)
    # Batas e–g tetap berlaku.
    k = satu(jalankan([berskor("LEBR", 50, "PB", MAHAL_LEBAR)], cfg=cfg), "LEBR")
    assert k.status == TIDAK_TERJANGKAU and k.alasan == "nilai 1 lot Rp 1.201.800 > alokasi maks Rp 1.000.000"
    k = satu(jalankan([berskor("BBBB", 50, "BO", B, rata_nilai=1e6)], cfg=cfg), "BBBB", "BO")
    assert k.status == TIDAK_LIKUID
    k = satu(jalankan([berskor("BBBB", 50, "BO", B)], cfg=cfg, portofolio=porto(kas="900000")), "BBBB", "BO")
    assert k.status == KAS_KURANG  # biaya 1 lot 913.868,75 > 900.000


def test_anggaran_dari_ekuitas_bukan_kas():
    h = jalankan([berskor("AAAA", 50)], portofolio=porto(kas="500000", ekuitas="3000000"))
    assert (h.anggaran_risiko, h.alokasi_maks, h.kas_awal) == (Decimal(30000), Decimal(1000000), Decimal(500000))
    assert satu(h, "AAAA").status == REKOMENDASI


# ---------------------------------------------------------------------------
# Backtest per strategi
# ---------------------------------------------------------------------------


def test_backtest_per_strategi_tanpa_gerbang_dan_tanpa_deduplikasi():
    cfg = cfg_uji({"risiko.maks_per_sektor": None})
    daftar = [berskor("AAAA", 60, "PB", C), berskor("AAAA", 50, "BO", C), berskor("BBBB", 40, "BO", C)]
    h = jalankan(daftar, cfg=cfg, lolos=None, dedup=False)
    # Tidak ada BAYANGAN_GERBANG walau BO "belum lolos", dan duplikat tetap diproses.
    assert ringkas(h) == [("AAAA", "PB", REKOMENDASI), ("AAAA", "BO", REKOMENDASI), ("BBBB", "BO", REKOMENDASI)]
    # Pembanding: dengan deduplikasi, sinyal BO AAAA menjadi duplikat.
    h = jalankan(daftar, cfg=cfg, lolos=None, dedup=True)
    assert satu(h, "AAAA", "BO").status == DUPLIKAT_STRATEGI


def test_seleksi_tanpa_sinyal():
    h = jalankan([])
    assert h.kandidat == () and h.rekomendasi == [] and h.daftar_pantau == []
