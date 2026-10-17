"""T-BEI: aturan BEI dari config (SPEC §4.1 "Pembulatan harga", §7 `pasar`, §15.1 langkah 1,
§16.2 BATAL_ARA, §22.1 T-BEI).

SEMUA TABEL DI BERKAS INI "FIKTIF – KHUSUS UJI". Angkanya sengaja tidak wajar (fraksi Rp 4, batas
rentang Rp 120, ARA 11 %, ARB 13 %, ...) dan BUKAN aturan BEI sungguhan (P4). Jangan disalin ke
config.yaml; nilai rilis tetap `terverifikasi: false` dan kosong.
"""

from __future__ import annotations

import copy
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from decimal import Decimal

import pytest

from asisten_bei.aturan_bei import AturanBEI, HargaBulat, ke_desimal_harga
from asisten_bei.config import ConfigTidakValid
from bantu_pasar import cfg_uji

LABEL_FIKTIF = "FIKTIF – KHUSUS UJI"
SUMBER_FIKTIF = f"{LABEL_FIKTIF} (bukan aturan BEI, jangan dipakai)"

# --- Tabel fraksi FIKTIF – KHUSUS UJI ---------------------------------------------------------------
# Rentang: [0, 120) fraksi 1; [120, 1000) fraksi 4; [1000, ∞) fraksi 10. Batas rentang (120, 1000)
# adalah kelipatan fraksi kedua baris yang mengapitnya (konvensi tabel di aturan_bei.py).
TABEL_FRAKSI_FIKTIF = [
    {"harga_min": 0, "harga_maks": 120, "fraksi": 1},
    {"harga_min": 120, "harga_maks": 1000, "fraksi": 4},
    {"harga_min": 1000, "harga_maks": None, "fraksi": 10},
]

# --- Tabel ARA/ARB FIKTIF – KHUSUS UJI (persen, rentang menurut harga acuan) ---------------------------
ARA_TUNGGAL_FIKTIF = [{"harga_min": 0, "harga_maks": None, "batas_pct": 11}]
ARB_TUNGGAL_FIKTIF = [{"harga_min": 0, "harga_maks": None, "batas_pct": 13}]
ARA_MULTI_FIKTIF = [
    {"harga_min": 0, "harga_maks": 120, "batas_pct": 50},
    {"harga_min": 120, "harga_maks": 1000, "batas_pct": 22},
    {"harga_min": 1000, "harga_maks": None, "batas_pct": 9},
]
ARB_MULTI_FIKTIF = [
    {"harga_min": 0, "harga_maks": 120, "batas_pct": 40},
    {"harga_min": 120, "harga_maks": 1000, "batas_pct": 17},
    {"harga_min": 1000, "harga_maks": None, "batas_pct": 6},
]

BERLAKU = date(2025, 3, 3)  # berlaku_mulai FIKTIF (Senin)
SEBELUM = date(2025, 2, 28)  # hari bursa terakhir sebelum berlaku_mulai (Jumat)
SESUDAH = date(2025, 6, 2)


def _fraksi(*, terverifikasi: bool = True, tabel=TABEL_FRAKSI_FIKTIF, berlaku=BERLAKU) -> dict:
    return {
        "pasar.fraksi_harga.terverifikasi": terverifikasi,
        "pasar.fraksi_harga.sumber": SUMBER_FIKTIF,
        "pasar.fraksi_harga.tanggal_cek": date(2025, 1, 6),
        "pasar.fraksi_harga.berlaku_mulai": berlaku,
        "pasar.fraksi_harga.tabel": copy.deepcopy(tabel),
    }


def _ar(
    *,
    terverifikasi: bool = True,
    ara=ARA_TUNGGAL_FIKTIF,
    arb=ARB_TUNGGAL_FIKTIF,
    berlaku=BERLAKU,
    acuan: str | None = "close_kemarin",
) -> dict:
    return {
        "pasar.auto_reject.terverifikasi": terverifikasi,
        "pasar.auto_reject.sumber": SUMBER_FIKTIF,
        "pasar.auto_reject.tanggal_cek": date(2025, 1, 6),
        "pasar.auto_reject.berlaku_mulai": berlaku,
        "pasar.auto_reject.papan_berlaku": LABEL_FIKTIF,
        "pasar.auto_reject.harga_acuan": acuan,
        "pasar.auto_reject.ara": copy.deepcopy(ara),
        "pasar.auto_reject.arb": copy.deepcopy(arb),
    }


def _bei(*ubah: dict) -> AturanBEI:
    gabung: dict = {}
    for u in ubah:
        gabung.update(u)
    return AturanBEI(cfg_uji(gabung))


BEI_RILIS = AturanBEI(cfg_uji())
BEI_FRAKSI = _bei(_fraksi())  # fraksi fiktif terverifikasi; ARA/ARB belum
BEI_AR = _bei(_ar())  # ARA/ARB fiktif satu baris terverifikasi; fraksi belum (rupiah bulat)
BEI_AR_FRAKSI = _bei(_fraksi(), _ar())  # keduanya terverifikasi
BEI_AR_MULTI = _bei(_ar(ara=ARA_MULTI_FIKTIF, arb=ARB_MULTI_FIKTIF))


def _D(x) -> Decimal:
    return Decimal(str(x))


# =====================================================================================================
# Config rilis (P4): tidak ada nilai aturan BEI
# =====================================================================================================


def test_config_rilis_tidak_berisi_aturan_bei():
    p = cfg_uji().pasar
    assert p.fraksi_harga.terverifikasi is False
    assert p.fraksi_harga.tabel == [] and p.fraksi_harga.berlaku_mulai is None
    assert p.auto_reject.terverifikasi is False
    assert p.auto_reject.ara == [] and p.auto_reject.arb == []
    assert p.auto_reject.berlaku_mulai is None and p.auto_reject.harga_acuan is None
    for t in (SEBELUM, SESUDAH, date(2026, 10, 15)):
        assert BEI_RILIS.fraksi_berlaku(t) is False
        assert BEI_RILIS.auto_reject_berlaku(t) is False
        assert BEI_RILIS.fraksi(1234, t) is None


# =====================================================================================================
# Belum terverifikasi → rupiah bulat dengan arah §4.1 (harga_maks/target ke bawah, stop ke atas)
# =====================================================================================================


@pytest.mark.parametrize(
    ("harga", "bawah", "atas"),
    [
        (1234.56, 1234, 1235),
        (1234.01, 1234, 1235),  # pecahan kecil tetap naik saat dibulatkan ke atas
        (1234.99, 1234, 1235),  # pecahan besar tetap turun saat dibulatkan ke bawah
        (1234.0, 1234, 1234),  # sudah bulat: tidak bergeser ke dua arah
        (121.5, 121, 122),  # tanpa fraksi: tidak ada lompatan ke kelipatan 4
        (997.2, 997, 998),
        (Decimal("1.5"), 1, 2),
        (7, 7, 7),  # int
    ],
)
def test_belum_terverifikasi_bulat_rupiah_dengan_arah(harga, bawah, atas):
    for t in (SEBELUM, SESUDAH):
        hb = BEI_RILIS.bulat_bawah(harga, t)
        ha = BEI_RILIS.bulat_atas(harga, t)
        assert isinstance(hb, HargaBulat) and isinstance(hb.nilai, Decimal)
        assert (hb.nilai, hb.fraksi_dipakai) == (Decimal(bawah), False)
        assert (ha.nilai, ha.fraksi_dipakai) == (Decimal(atas), False)


def test_tabel_terisi_tetapi_belum_terverifikasi_tidak_dipakai():
    # Tabel fiktif dan berlaku_mulai terisi, tetapi terverifikasi false → tetap rupiah bulat.
    bei = _bei(_fraksi(terverifikasi=False))
    assert bei.fraksi_berlaku(SESUDAH) is False
    assert bei.fraksi(121.5, SESUDAH) is None
    assert bei.bulat_bawah(121.5, SESUDAH) == HargaBulat(Decimal(121), False)  # fraksi akan → 120
    assert bei.bulat_atas(997.2, SESUDAH) == HargaBulat(Decimal(998), False)  # fraksi akan → 1000
    assert bei.harga_valid(121, SESUDAH) is True  # rupiah bulat dianggap valid
    # Kontrol: tabel yang sama dengan terverifikasi true memang mengubah hasil.
    assert BEI_FRAKSI.bulat_bawah(121.5, SESUDAH) == HargaBulat(Decimal(120), True)
    assert BEI_FRAKSI.bulat_atas(997.2, SESUDAH) == HargaBulat(Decimal(1000), True)


@pytest.mark.parametrize(
    ("harga", "valid"),
    [(121, True), (121.5, False), (1004, True), (1234.000001, False), (2.03 * 1000, True)],
)
def test_harga_valid_belum_terverifikasi_berarti_rupiah_bulat(harga, valid):
    assert BEI_RILIS.harga_valid(harga, SESUDAH) is valid


def test_peringatan_belum_terverifikasi_menyebut_arah_pembulatan():
    hasil = BEI_RILIS.peringatan(SESUDAH)
    assert len(hasil) == 2
    fraksi, ar = hasil
    assert "Fraksi harga belum diverifikasi" in fraksi
    assert "rupiah" in fraksi
    assert "limit beli dan target ke BAWAH" in fraksi  # §4.1: harga_maks & target ke bawah
    assert "stop ke ATAS" in fraksi  # §4.1: stop ke atas
    assert "ARA/ARB belum diverifikasi" in ar


def test_peringatan_hanya_untuk_aturan_yang_belum_berlaku():
    # Keduanya terverifikasi dan berlaku → tidak ada peringatan.
    assert BEI_AR_FRAKSI.peringatan(SESUDAH) == []
    assert BEI_AR_FRAKSI.peringatan(BERLAKU) == []
    # Sebelum berlaku_mulai keduanya belum dimodelkan → kedua peringatan muncul.
    sebelum = BEI_AR_FRAKSI.peringatan(SEBELUM)
    assert len(sebelum) == 2 and "Fraksi" in sebelum[0] and "ARA/ARB" in sebelum[1]
    # Hanya fraksi terverifikasi → hanya peringatan ARA/ARB; dan sebaliknya.
    hanya_ar = BEI_FRAKSI.peringatan(SESUDAH)
    assert len(hanya_ar) == 1 and "ARA/ARB" in hanya_ar[0]
    hanya_fraksi = BEI_AR.peringatan(SESUDAH)
    assert len(hanya_fraksi) == 1 and "Fraksi" in hanya_fraksi[0] and "stop ke ATAS" in hanya_fraksi[0]


@pytest.mark.parametrize("bei", [BEI_RILIS, BEI_FRAKSI], ids=["belum_terverifikasi", "fiktif"])
@pytest.mark.parametrize("harga", [0, 0.0, -5, Decimal("-0.5")])
def test_harga_tidak_positif_ditolak(bei, harga):
    with pytest.raises(ValueError):
        bei.bulat_bawah(harga, SESUDAH)
    with pytest.raises(ValueError):
        bei.bulat_atas(harga, SESUDAH)


# =====================================================================================================
# ke_desimal_harga: derau float dibuang (6 desimal) sebelum pembulatan
# =====================================================================================================


@pytest.mark.parametrize(
    ("masukan", "harapan"),
    [
        (2029.9999999998, "2030"),
        (2.03 * 1000, "2030"),  # = 2029.9999999999998
        (0.1 * 3 * 1000, "300"),  # = 300.00000000000006
        (123.99999999999999, "124"),
        (1000.0000000000001, "1000"),
        (2029.99999, "2029.99999"),  # pecahan sungguhan (5 desimal) tidak dibuang
        (1234.000001, "1234.000001"),  # 6 desimal masih dipertahankan
        (Decimal("1234.5678901"), "1234.567890"),
        (Decimal("0.0000005"), "0.000000"),  # ROUND_HALF_EVEN: 0.0000005 → 0 (genap)
        (Decimal("0.0000015"), "0.000002"),  # ROUND_HALF_EVEN: 0.0000015 → 0.000002 (genap)
        (7, "7"),
    ],
)
def test_ke_desimal_harga_membuang_derau_float(masukan, harapan):
    d = ke_desimal_harga(masukan)
    assert isinstance(d, Decimal)
    assert d == Decimal(harapan)
    assert d.as_tuple().exponent == -6  # selalu 6 desimal


@pytest.mark.parametrize("masukan", [float("nan"), float("inf"), float("-inf")])
def test_ke_desimal_harga_menolak_nan_dan_tak_hingga(masukan):
    with pytest.raises(ValueError):
        ke_desimal_harga(masukan)


def test_derau_float_tidak_menggeser_pembulatan():
    # Tanpa pembersihan, 2029.9999999999998 dibulatkan ke bawah menjadi 2029 dan 300.00000000000006
    # ke atas menjadi 301.
    assert BEI_RILIS.bulat_bawah(2.03 * 1000, SESUDAH).nilai == Decimal(2030)
    assert BEI_RILIS.bulat_atas(0.1 * 3 * 1000, SESUDAH).nilai == Decimal(300)
    # Tabel fiktif: 123.99999999999999 → 124 (kelipatan 4), bukan 120; 1000.0000000000001 → 1000, bukan 1010.
    assert BEI_FRAKSI.bulat_bawah(123.99999999999999, SESUDAH).nilai == Decimal(124)
    assert BEI_FRAKSI.bulat_atas(1000.0000000000001, SESUDAH).nilai == Decimal(1000)
    # Sisi lain: pecahan sungguhan tetap dibulatkan.
    assert BEI_RILIS.bulat_bawah(2029.99999, SESUDAH).nilai == Decimal(2029)
    assert BEI_RILIS.bulat_atas(2030.00001, SESUDAH).nilai == Decimal(2031)


# =====================================================================================================
# Tabel fraksi FIKTIF terverifikasi
# =====================================================================================================


def test_fraksi_hanya_berlaku_mulai_berlaku_mulai():
    assert BEI_FRAKSI.fraksi_berlaku(BERLAKU - timedelta(days=1)) is False
    assert BEI_FRAKSI.fraksi_berlaku(SEBELUM) is False
    assert BEI_FRAKSI.fraksi_berlaku(BERLAKU) is True  # inklusif
    assert BEI_FRAKSI.fraksi_berlaku(SESUDAH) is True


@pytest.mark.parametrize(
    ("harga", "fraksi"),
    [(0.5, 1), (119, 1), (119.999999, 1), (120, 4), (120.5, 4), (999.99, 4), (1000, 10), (10_000_000, 10)],
)
def test_fraksi_per_rentang_min_inklusif_maks_eksklusif(harga, fraksi):
    assert BEI_FRAKSI.fraksi(harga, SESUDAH) == Decimal(fraksi)
    assert BEI_FRAKSI.fraksi(harga, SEBELUM) is None


@pytest.mark.parametrize(
    ("harga", "harapan"),
    [
        (55.7, 55),  # rentang fraksi 1
        (119.9, 119),  # tepat di bawah batas 120: fraksi 1
        (120, 120),  # tepat di batas: sudah valid
        (120.5, 120),  # tepat di atas batas: fraksi 4 → 120
        (121, 120),
        (123.99, 120),  # ⌊123,99 / 4⌋ × 4 = 30 × 4 = 120
        (124, 124),
        (999.9, 996),  # tepat di bawah 1000: ⌊999,9 / 4⌋ × 4 = 249 × 4 = 996
        (1000, 1000),  # tepat di batas
        (1000.5, 1000),  # tepat di atas batas: fraksi 10
        (1009.99, 1000),
        (1010, 1010),
        (Decimal("2345.678"), 2340),
    ],
)
def test_bulat_bawah_tabel_fiktif(harga, harapan):
    hasil = BEI_FRAKSI.bulat_bawah(harga, SESUDAH)
    assert hasil == HargaBulat(Decimal(harapan), True)
    assert BEI_FRAKSI.harga_valid(hasil.nilai, SESUDAH)


@pytest.mark.parametrize(
    ("harga", "harapan"),
    [
        (55.2, 56),  # rentang fraksi 1
        (119, 119),  # sudah valid di rentang fraksi 1
        (119.2, 120),  # menyeberang ke rentang fraksi 4: 120 valid (kelipatan 4)
        (120, 120),  # tepat di batas
        (120.1, 124),  # tepat di atas batas: ⌈120,1 / 4⌉ × 4 = 31 × 4 = 124
        (121, 124),
        (124, 124),
        (996.5, 1000),  # ⌈996,5 / 4⌉ × 4 = 250 × 4 = 1000 → masuk rentang fraksi 10, valid
        (997, 1000),
        (999.99, 1000),  # tepat di bawah batas
        (1000, 1000),  # tepat di batas
        (1000.01, 1010),  # tepat di atas batas: fraksi 10
        (1001, 1010),
        (Decimal("2340.001"), 2350),
    ],
)
def test_bulat_atas_tabel_fiktif(harga, harapan):
    hasil = BEI_FRAKSI.bulat_atas(harga, SESUDAH)
    assert hasil == HargaBulat(Decimal(harapan), True)
    assert BEI_FRAKSI.harga_valid(hasil.nilai, SESUDAH)


def _harga_valid_fiktif(sampai: int) -> list[int]:
    """Semua harga valid tabel fiktif ≤ `sampai`, disusun langsung dari definisi tabel."""
    return list(range(1, 120)) + list(range(120, 1000, 4)) + list(range(1000, sampai + 1, 10))


def test_pembulatan_tabel_fiktif_cocok_dengan_harga_valid_terdekat():
    # Oracle independen: bulat_bawah = harga valid terbesar ≤ h, bulat_atas = harga valid terkecil ≥ h.
    valid = _harga_valid_fiktif(1300)
    for k in range(3, 3300):  # h = 1,11 … 1220,63
        h = Decimal(k) * Decimal("0.37")
        harap_bawah = valid[bisect_right(valid, h) - 1]
        harap_atas = valid[bisect_left(valid, h)]
        assert BEI_FRAKSI.bulat_bawah(h, SESUDAH) == HargaBulat(Decimal(harap_bawah), True), h
        assert BEI_FRAKSI.bulat_atas(h, SESUDAH) == HargaBulat(Decimal(harap_atas), True), h


@pytest.mark.parametrize(
    ("harga", "valid"),
    [
        (119, True),
        (119.5, False),
        (120, True),
        (121, False),
        (122, False),
        (124, True),
        (996, True),
        (998, False),
        (1000, True),
        (1004, False),
        (1010, True),
        (123.99999999999999, True),  # derau float → 124
    ],
)
def test_harga_valid_tabel_fiktif(harga, valid):
    assert BEI_FRAKSI.harga_valid(harga, SESUDAH) is valid


def test_tidak_ada_pembulatan_fraksi_sebelum_berlaku_mulai():
    # Sebelum berlaku_mulai: rupiah bulat saja, fraksi_dipakai False.
    assert BEI_FRAKSI.bulat_bawah(121.5, SEBELUM) == HargaBulat(Decimal(121), False)
    assert BEI_FRAKSI.bulat_atas(997.2, SEBELUM) == HargaBulat(Decimal(998), False)
    assert BEI_FRAKSI.bulat_atas(1001, SEBELUM) == HargaBulat(Decimal(1001), False)
    assert BEI_FRAKSI.harga_valid(121, SEBELUM) is True
    assert BEI_FRAKSI.harga_valid(1004, SEBELUM) is True
    # Tepat pada berlaku_mulai: fraksi dipakai.
    assert BEI_FRAKSI.bulat_bawah(121.5, BERLAKU) == HargaBulat(Decimal(120), True)
    assert BEI_FRAKSI.bulat_atas(997.2, BERLAKU) == HargaBulat(Decimal(1000), True)
    assert BEI_FRAKSI.bulat_atas(1001, BERLAKU) == HargaBulat(Decimal(1010), True)
    assert BEI_FRAKSI.harga_valid(121, BERLAKU) is False
    assert BEI_FRAKSI.harga_valid(1004, BERLAKU) is False


def test_harga_di_bawah_baris_pertama_memakai_baris_pertama():
    # FIKTIF – KHUSUS UJI: tabel dimulai di 50 (konvensi aturan_bei: harga < harga_min baris pertama
    # memakai baris pertama).
    bei = _bei(_fraksi(tabel=[
        {"harga_min": 50, "harga_maks": 200, "fraksi": 5},
        {"harga_min": 200, "harga_maks": None, "fraksi": 20},
    ]))
    assert bei.fraksi(33, SESUDAH) == Decimal(5)
    assert bei.bulat_bawah(33, SESUDAH) == HargaBulat(Decimal(30), True)
    assert bei.bulat_atas(33, SESUDAH) == HargaBulat(Decimal(35), True)
    assert bei.harga_valid(35, SESUDAH) is True and bei.harga_valid(33, SESUDAH) is False


TABEL_TIDAK_SEJAJAR_FIKTIF = [  # FIKTIF – KHUSUS UJI: batas 100 bukan kelipatan fraksi 3 maupun 7
    {"harga_min": 0, "harga_maks": 100, "fraksi": 3},
    {"harga_min": 100, "harga_maks": None, "fraksi": 7},
]


def test_tabel_tidak_sejajar_tetap_aman_arah_dan_valid():
    # Pengaman §4.1: apa pun tabelnya, hasil ke bawah ≤ harga, hasil ke atas ≥ harga, dan keduanya valid.
    bei = _bei(_fraksi(tabel=TABEL_TIDAK_SEJAJAR_FIKTIF))
    for k in range(10, 700):
        h = Decimal(k) * Decimal("0.3")  # 3,0 … 209,7
        b, a = bei.bulat_bawah(h, SESUDAH), bei.bulat_atas(h, SESUDAH)
        assert b.fraksi_dipakai and a.fraksi_dipakai
        assert b.nilai <= h <= a.nilai, h
        assert bei.harga_valid(b.nilai, SESUDAH) and bei.harga_valid(a.nilai, SESUDAH), h
    # 99,5 ke atas: 102 (kelipatan 3) berada di rentang fraksi 7 dan tidak valid → 105.
    assert bei.bulat_atas(Decimal("99.5"), SESUDAH).nilai == Decimal(105)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "BUG: tabel fraksi yang batas rentangnya bukan kelipatan fraksi lolos validasi config, tetapi "
        "bulat_bawah(101) memberi 96 padahal 99 valid (kelipatan 3 di rentang [0,100)); §4.1 'dibulatkan "
        "ke bawah ke fraksi yang berlaku' = harga valid terbesar ≤ harga. Perbaikan: tolak tabel seperti "
        "ini di config.py atau cari harga valid terdekat."
    ),
)
def test_tabel_tidak_sejajar_ditolak_atau_dibulatkan_ke_harga_valid_terdekat():
    try:
        bei = _bei(_fraksi(tabel=TABEL_TIDAK_SEJAJAR_FIKTIF))
    except ConfigTidakValid:
        return  # perbaikan lewat validasi config juga memenuhi
    # Harga valid ≤ 101: …, 96, 99 (rentang fraksi 3); 100 dan 101 tidak valid (rentang fraksi 7).
    assert bei.bulat_bawah(101, SESUDAH).nilai == Decimal(99)


# =====================================================================================================
# Validasi config (§7): tabel tumpang tindih/berlubang dan terverifikasi tanpa isi → error
# =====================================================================================================


def _pesan_error(ubah: dict) -> str:
    with pytest.raises(ConfigTidakValid) as e:
        cfg_uji(ubah)
    return "\n".join(e.value.pesan)


_TABEL_FRAKSI_RUSAK = {
    "tumpang_tindih": (
        [{"harga_min": 0, "harga_maks": 130, "fraksi": 1}, {"harga_min": 120, "harga_maks": 1000, "fraksi": 4},
         {"harga_min": 1000, "harga_maks": None, "fraksi": 10}],
        "tumpang tindih",
    ),
    "berlubang": (
        [{"harga_min": 0, "harga_maks": 110, "fraksi": 1}, {"harga_min": 120, "harga_maks": 1000, "fraksi": 4},
         {"harga_min": 1000, "harga_maks": None, "fraksi": 10}],
        "berlubang",
    ),
    "berlubang_selisih_kecil": (
        [{"harga_min": 0, "harga_maks": 120, "fraksi": 1}, {"harga_min": 121, "harga_maks": None, "fraksi": 4}],
        "berlubang",
    ),
    "tumpang_tindih_selisih_kecil": (
        [{"harga_min": 0, "harga_maks": 120, "fraksi": 1}, {"harga_min": 119, "harga_maks": None, "fraksi": 4}],
        "tumpang tindih",
    ),
    "baris_terakhir_berbatas": (
        [{"harga_min": 0, "harga_maks": 120, "fraksi": 1}, {"harga_min": 120, "harga_maks": 1000, "fraksi": 4}],
        "berlubang",
    ),
    "null_di_tengah": (
        [{"harga_min": 0, "harga_maks": None, "fraksi": 1}, {"harga_min": 120, "harga_maks": None, "fraksi": 4}],
        "null hanya boleh di baris terakhir",
    ),
    "maks_tidak_lebih_dari_min": (
        [{"harga_min": 0, "harga_maks": 120, "fraksi": 1}, {"harga_min": 120, "harga_maks": 120, "fraksi": 4},
         {"harga_min": 120, "harga_maks": None, "fraksi": 10}],
        "harga_maks harus > harga_min",
    ),
}


@pytest.mark.parametrize("terverifikasi", [True, False])
@pytest.mark.parametrize("nama", list(_TABEL_FRAKSI_RUSAK))
def test_tabel_fraksi_rusak_ditolak(nama, terverifikasi):
    tabel, kata = _TABEL_FRAKSI_RUSAK[nama]
    pesan = _pesan_error(_fraksi(terverifikasi=terverifikasi, tabel=tabel))
    assert "pasar.fraksi_harga" in pesan
    assert kata in pesan


@pytest.mark.parametrize("fraksi", [0, -1])
def test_fraksi_harus_positif(fraksi):
    pesan = _pesan_error(_fraksi(tabel=[{"harga_min": 0, "harga_maks": None, "fraksi": fraksi}]))
    assert "pasar.fraksi_harga.tabel.0.fraksi" in pesan


def test_tabel_fraksi_fiktif_bersambung_diterima():
    # Kontrol positif: tabel bersambung (harga_maks[i] == harga_min[i+1]) lolos, terverifikasi atau tidak.
    for terverifikasi in (True, False):
        cfg = cfg_uji(_fraksi(terverifikasi=terverifikasi))
        assert [b.fraksi for b in cfg.pasar.fraksi_harga.tabel] == [Decimal(1), Decimal(4), Decimal(10)]
    cfg = cfg_uji(_fraksi(tabel=[{"harga_min": 0, "harga_maks": None, "fraksi": 3}]))  # satu baris
    assert len(cfg.pasar.fraksi_harga.tabel) == 1


def test_fraksi_terverifikasi_tanpa_tabel_ditolak():
    pesan = _pesan_error(_fraksi(tabel=[]))
    assert "pasar.fraksi_harga" in pesan and "tabel kosong" in pesan
    # Sisi lain: tabel kosong dengan terverifikasi false (= config rilis) diterima.
    assert cfg_uji(_fraksi(terverifikasi=False, tabel=[])).pasar.fraksi_harga.tabel == []


def test_fraksi_terverifikasi_tanpa_berlaku_mulai_ditolak():
    pesan = _pesan_error(_fraksi(berlaku=None))
    assert "pasar.fraksi_harga" in pesan and "berlaku_mulai" in pesan


@pytest.mark.parametrize("sisi", ["ara", "arb"])
@pytest.mark.parametrize("nama", ["tumpang_tindih", "berlubang", "baris_terakhir_berbatas", "null_di_tengah"])
def test_tabel_ara_arb_rusak_ditolak(sisi, nama):
    tabel_fraksi, kata = _TABEL_FRAKSI_RUSAK[nama]
    tabel = [{"harga_min": b["harga_min"], "harga_maks": b["harga_maks"], "batas_pct": 15} for b in tabel_fraksi]
    for terverifikasi in (True, False):
        pesan = _pesan_error(_ar(terverifikasi=terverifikasi, **{sisi: tabel}))
        assert "pasar.auto_reject" in pesan
        assert sisi in pesan and kata in pesan


@pytest.mark.parametrize(
    ("ubah", "kata"),
    [
        ({"ara": []}, "tabel ara/arb kosong"),
        ({"arb": []}, "tabel ara/arb kosong"),
        ({"berlaku": None}, "berlaku_mulai"),
        ({"acuan": None}, "harga_acuan"),
        ({"acuan": "open_hari_ini"}, "harga_acuan"),
    ],
)
def test_auto_reject_terverifikasi_tidak_lengkap_ditolak(ubah, kata):
    pesan = _pesan_error(_ar(**ubah))
    assert "pasar.auto_reject" in pesan and kata in pesan
    # Sisi lain: isian yang sama dengan terverifikasi false diterima (belum dimodelkan).
    cfg_uji(_ar(terverifikasi=False, **ubah))


@pytest.mark.parametrize("pct", [0, -1, 101])
def test_batas_pct_di_luar_rentang_ditolak(pct):
    pesan = _pesan_error(_ar(ara=[{"harga_min": 0, "harga_maks": None, "batas_pct": pct}]))
    assert "pasar.auto_reject.ara.0.batas_pct" in pesan


# =====================================================================================================
# ARA/ARB FIKTIF: batas_ara ke bawah, batas_arb ke atas, None bila tidak dimodelkan
# =====================================================================================================


def test_auto_reject_hanya_berlaku_mulai_berlaku_mulai():
    assert BEI_AR.auto_reject_berlaku(BERLAKU - timedelta(days=1)) is False
    assert BEI_AR.auto_reject_berlaku(SEBELUM) is False
    assert BEI_AR.auto_reject_berlaku(BERLAKU) is True
    assert BEI_AR.auto_reject_berlaku(SESUDAH) is True


@pytest.mark.parametrize(
    ("acuan", "ara", "arb"),
    [
        # ARA = acuan × 1,11 dibulatkan ke BAWAH; ARB = acuan × 0,87 dibulatkan ke ATAS (rupiah bulat).
        (1003, 1113, 873),  # 1.113,33 → 1.113; 872,61 → 873
        (1000, 1110, 870),  # tepat bulat: tidak bergeser
        (1002.9999999999999, 1113, 873),  # derau float pada acuan
        (Decimal("57"), 63, 50),  # 63,27 → 63; 49,59 → 50
    ],
)
def test_batas_ara_arb_satu_baris_rupiah(acuan, ara, arb):
    assert BEI_AR.batas_ara(acuan, SESUDAH) == Decimal(ara)
    assert BEI_AR.batas_arb(acuan, SESUDAH) == Decimal(arb)
    assert BEI_AR.batas_ara(acuan, BERLAKU) == Decimal(ara)


@pytest.mark.parametrize(
    ("acuan", "ara", "arb"),
    [
        # Fraksi fiktif juga terverifikasi: ARA ke bawah dan ARB ke atas ke fraksi rentang hasilnya.
        (1003, 1110, 876),  # 1.113,33 → fraksi 10 → 1.110; 872,61 → fraksi 4 → ⌈218,15⌉ × 4 = 876
        (900, 996, 784),  # 999 → fraksi 4 → 996 (bukan 999); 783 → ⌈195,75⌉ × 4 = 784
        (130, 144, 114),  # 144,3 → fraksi 4 → 144; 113,1 → fraksi 1 → 114
        (110, 120, 96),  # 122,1 → fraksi 4 → 120; 95,7 → 96
    ],
)
def test_batas_ara_arb_satu_baris_dengan_fraksi_fiktif(acuan, ara, arb):
    assert BEI_AR_FRAKSI.batas_ara(acuan, SESUDAH) == Decimal(ara)
    assert BEI_AR_FRAKSI.batas_arb(acuan, SESUDAH) == Decimal(arb)
    assert BEI_AR_FRAKSI.harga_valid(ara, SESUDAH) and BEI_AR_FRAKSI.harga_valid(arb, SESUDAH)


@pytest.mark.parametrize(
    ("acuan", "ara", "arb"),
    [
        # Rentang menurut harga acuan (harga_min inklusif, harga_maks eksklusif); rupiah bulat.
        (119, 178, 72),  # 119 × 1,50 = 178,5 → 178; 119 × 0,60 = 71,4 → 72
        (120, 146, 100),  # 120 × 1,22 = 146,4 → 146; 120 × 0,83 = 99,6 → 100
        (999, 1218, 830),  # 999 × 1,22 = 1.218,78 → 1.218; 999 × 0,83 = 829,17 → 830
        (1000, 1090, 940),  # 1.000 × 1,09 = 1.090; 1.000 × 0,94 = 940
        (1001, 1091, 941),  # 1.001 × 1,09 = 1.091,09 → 1.091; 1.001 × 0,94 = 940,94 → 941
    ],
)
def test_batas_ara_arb_multi_baris(acuan, ara, arb):
    assert BEI_AR_MULTI.batas_ara(acuan, SESUDAH) == Decimal(ara)
    assert BEI_AR_MULTI.batas_arb(acuan, SESUDAH) == Decimal(arb)


def test_batas_ara_arb_none_bila_tidak_dimodelkan():
    for acuan in (1003, 120):
        # Config rilis.
        assert BEI_RILIS.batas_ara(acuan, SESUDAH) is None
        assert BEI_RILIS.batas_arb(acuan, SESUDAH) is None
        # Tabel terisi tetapi terverifikasi false.
        bei = _bei(_ar(terverifikasi=False))
        assert bei.batas_ara(acuan, SESUDAH) is None and bei.batas_arb(acuan, SESUDAH) is None
        # Terverifikasi, tetapi sebelum berlaku_mulai.
        assert BEI_AR.batas_ara(acuan, SEBELUM) is None and BEI_AR.batas_arb(acuan, SEBELUM) is None
        assert BEI_AR.batas_ara(acuan, BERLAKU - timedelta(days=1)) is None
        # Sisi lain: tepat pada berlaku_mulai sudah dimodelkan.
        assert BEI_AR.batas_ara(acuan, BERLAKU) is not None and BEI_AR.batas_arb(acuan, BERLAKU) is not None


def test_berlaku_mulai_ara_dan_fraksi_independen():
    # ARA/ARB berlaku 2025-03-03, fraksi baru 2025-06-02 (keduanya FIKTIF).
    bei = _bei(_fraksi(berlaku=date(2025, 6, 2)), _ar(berlaku=BERLAKU))
    antara = date(2025, 4, 1)
    assert bei.batas_ara(1003, antara) == Decimal(1113)  # rupiah bulat (fraksi belum berlaku)
    assert bei.batas_arb(1003, antara) == Decimal(873)
    assert bei.batas_ara(1003, date(2025, 6, 2)) == Decimal(1110)  # fraksi 10
    assert bei.batas_arb(1003, date(2025, 6, 2)) == Decimal(876)  # fraksi 4


def test_arb_seratus_persen_berarti_batas_nol():
    # FIKTIF – KHUSUS UJI: ARB 100 % → acuan × 0 = 0 (tanpa batas bawah); bar positif tidak terkunci ARB.
    bei = _bei(_ar(arb=[{"harga_min": 0, "harga_maks": None, "batas_pct": 100}]))
    assert bei.batas_arb(1003, SESUDAH) == Decimal(0)
    assert bei.terkunci_arb(1, 1, 1, 1, 1003, SESUDAH) is False


# =====================================================================================================
# Bar terkunci ARB (§15.1 langkah 1) dan ARA (§16.2 BATAL_ARA)
# =====================================================================================================

# Acuan (close kemarin, nominal) 1003 → batas ARB 873, batas ARA 1113 (rupiah bulat, lihat di atas).
ACUAN = 1003.0


@pytest.mark.parametrize(
    ("o", "h", "l", "c", "harapan"),
    [
        (873, 873, 873, 873, True),  # datar tepat di batas ARB
        (872, 872, 872, 872, True),  # datar di bawah batas
        (873.0000000000001,) * 4 + (True,),  # derau float di batas → tetap terkunci
        (874, 874, 874, 874, False),  # datar tetapi di dalam batas
        (873, 880, 873, 873, False),  # tidak datar: high > close
        (880, 880, 873, 873, False),  # tidak datar: open/high di atas
        (873, 873, 870, 873, False),  # tidak datar: low < close
        (873, 873, 873, 872, False),  # tidak datar: close berbeda
    ],
)
def test_terkunci_arb(o, h, l, c, harapan):
    assert BEI_AR.terkunci_arb(o, h, l, c, ACUAN, SESUDAH) is harapan


@pytest.mark.parametrize(
    ("o", "h", "l", "c", "harapan"),
    [
        (1113, 1113, 1113, 1113, True),  # datar tepat di batas ARA
        (1114, 1114, 1114, 1114, True),  # datar di atas batas
        (1112.9999999999998,) * 4 + (True,),  # derau float di batas → tetap terkunci
        (1112, 1112, 1112, 1112, False),  # datar tetapi di dalam batas
        (1113, 1113, 1100, 1113, False),  # tidak datar: low < close
        (1100, 1113, 1100, 1113, False),  # tidak datar: open di bawah
        (1113, 1120, 1113, 1113, False),  # tidak datar: high > close
    ],
)
def test_terkunci_ara(o, h, l, c, harapan):
    assert BEI_AR.terkunci_ara(o, h, l, c, ACUAN, SESUDAH) is harapan


def test_bar_datar_hanya_terkunci_di_sisinya():
    # Bar datar di batas ARA tidak terkunci ARB, dan sebaliknya.
    assert BEI_AR.terkunci_arb(1113, 1113, 1113, 1113, ACUAN, SESUDAH) is False
    assert BEI_AR.terkunci_ara(873, 873, 873, 873, ACUAN, SESUDAH) is False


def test_terkunci_dengan_fraksi_fiktif_memakai_batas_yang_dibulatkan():
    # Fraksi juga terverifikasi: ARA 1.110 (bukan 1.113), ARB 876 (bukan 873).
    assert BEI_AR_FRAKSI.terkunci_ara(1110, 1110, 1110, 1110, ACUAN, SESUDAH) is True
    assert BEI_AR_FRAKSI.terkunci_ara(1100, 1100, 1100, 1100, ACUAN, SESUDAH) is False
    assert BEI_AR_FRAKSI.terkunci_arb(876, 876, 876, 876, ACUAN, SESUDAH) is True
    assert BEI_AR_FRAKSI.terkunci_arb(880, 880, 880, 880, ACUAN, SESUDAH) is False
    # Dengan rupiah bulat (BEI_AR), 1.110 dan 876 berada di dalam batas.
    assert BEI_AR.terkunci_ara(1110, 1110, 1110, 1110, ACUAN, SESUDAH) is False
    assert BEI_AR.terkunci_arb(876, 876, 876, 876, ACUAN, SESUDAH) is False


@pytest.mark.parametrize(
    ("bei", "tanggal"),
    [
        (BEI_RILIS, SESUDAH),  # config rilis: belum terverifikasi
        (_bei(_ar(terverifikasi=False)), SESUDAH),  # tabel terisi tetapi belum terverifikasi
        (BEI_AR, SEBELUM),  # terverifikasi tetapi sebelum berlaku_mulai
        (BEI_AR, BERLAKU - timedelta(days=1)),
    ],
    ids=["rilis", "tabel_belum_terverifikasi", "sebelum_berlaku", "sehari_sebelum_berlaku"],
)
def test_tidak_pernah_terkunci_bila_tidak_dimodelkan(bei, tanggal):
    for p in (873, 500, 1):
        assert bei.terkunci_arb(p, p, p, p, ACUAN, tanggal) is False
    for p in (1113, 2000, 10_000):
        assert bei.terkunci_ara(p, p, p, p, ACUAN, tanggal) is False


def test_terkunci_mulai_tepat_pada_berlaku_mulai():
    assert BEI_AR.terkunci_arb(873, 873, 873, 873, ACUAN, BERLAKU) is True
    assert BEI_AR.terkunci_ara(1113, 1113, 1113, 1113, ACUAN, BERLAKU) is True


def test_terkunci_multi_baris_memakai_rentang_harga_acuan():
    # Acuan 120 (baris kedua): ARB = ⌈120 × 0,83⌉ = ⌈99,6⌉ = 100; ARA = ⌊146,4⌋ = 146.
    assert BEI_AR_MULTI.terkunci_arb(100, 100, 100, 100, 120, SESUDAH) is True
    assert BEI_AR_MULTI.terkunci_arb(101, 101, 101, 101, 120, SESUDAH) is False
    assert BEI_AR_MULTI.terkunci_ara(146, 146, 146, 146, 120, SESUDAH) is True
    assert BEI_AR_MULTI.terkunci_ara(145, 145, 145, 145, 120, SESUDAH) is False
    # Acuan 119 (baris pertama): ARB = ⌈71,4⌉ = 72 → 100 bukan terkunci; ARA = 178 → 146 bukan terkunci.
    assert BEI_AR_MULTI.terkunci_arb(100, 100, 100, 100, 119, SESUDAH) is False
    assert BEI_AR_MULTI.terkunci_ara(146, 146, 146, 146, 119, SESUDAH) is False
    assert BEI_AR_MULTI.terkunci_arb(72, 72, 72, 72, 119, SESUDAH) is True
    assert BEI_AR_MULTI.terkunci_ara(178, 178, 178, 178, 119, SESUDAH) is True
