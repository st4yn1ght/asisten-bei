"""T-KP: filter kondisi pasar (SPEC §10) — setiap cabang K0–K4, batas tepat, cakupan, dan akibat status."""

from __future__ import annotations

from decimal import Decimal

import pytest

from asisten_bei.kondisi_pasar import (
    BOLEH_BELI,
    HATI_HATI,
    JANGAN_BELI,
    hitung_breadth,
    hitung_kondisi_pasar,
    parameter_entry,
)
from bantu_pasar import bar, cfg_uji, naik, pasar, tgl, turun

T = 40  # posisi hari evaluasi; seri mulai di posisi 0 → 41 bar sampai t
_UBAH = {
    "kondisi_pasar.sma_pendek": 5,
    "kondisi_pasar.sma_panjang": 20,
    "kondisi_pasar.breadth_sma": 5,
    "kondisi_pasar.min_saham_breadth": 3,
}
CFG = cfg_uji(_UBAH)


def _breadth(n_naik: int, n_turun: int, **lain) -> dict:
    saham = {f"N{i:02d}": bar(naik(T + 1, 500 + i)) for i in range(n_naik)}
    saham.update({f"T{i:02d}": bar(turun(T + 1, 500 + i)) for i in range(n_turun)})
    saham.update(lain)
    return saham


def _hitung(saham: dict, ihsg=None, cfg=CFG, pos: int = T, kode_breadth=None, anomali_ok=None):
    if ihsg is None:
        ihsg = bar(naik(T + 1, 7000), indeks=True)
    p = pasar(saham, ihsg, cfg, tgl(T + 5), anomali_ok=anomali_ok)
    dv = p.tampilan(tgl(pos))
    return hitung_kondisi_pasar(dv, kode_breadth if kode_breadth is not None else list(saham))


# ---------------------------------------------------------------------------
# K3 / K4 / K2 / K1
# ---------------------------------------------------------------------------


def test_k3_boleh_beli_dengan_ihsg_tanpa_volume():
    h = _hitung(_breadth(3, 2))
    assert (h.status, h.aturan, h.tampilan) == (BOLEH_BELI, "K3", "BOLEH BELI")
    assert h.alasan == "Tren IHSG naik dan breadth sehat"
    assert (h.breadth.n_tercatat, h.breadth.n_terhitung, h.breadth.n_di_atas_sma) == (5, 5, 3)
    assert h.breadth.breadth_pct == Decimal(60)
    assert h.data_lengkap and h.kekurangan_data == ()
    # Tabel syarat selalu lengkap; semua syarat K3 ✓.
    assert len(h.syarat) == 15
    assert all(s.terpenuhi for s in h.syarat if s.aturan == "K0")
    assert all(s.terpenuhi for s in h.syarat if s.aturan == "K3")
    assert all(s.terpenuhi is False for s in h.syarat if s.aturan in ("K1", "K2"))


def test_k4_hati_hati_breadth_di_bawah_ambang_boleh():
    h = _hitung(_breadth(2, 3))
    assert (h.status, h.aturan) == (HATI_HATI, "K4")
    assert "breadth 40% < 50%" in h.alasan


def test_k4_hati_hati_close_di_bawah_sma_pendek():
    # Naik 1%/hari lalu turun 1%/hari selama 3 hari: close < SMA5 tetapi masih > SMA20, SMA5 > SMA20.
    c = naik(T - 2, 7000, 0.01) + [7000 * 1.01 ** (T - 3) * 0.99 ** k for k in (1, 2, 3)]
    h = _hitung(_breadth(3, 2), ihsg=bar(c, indeks=True))
    assert h.status == HATI_HATI
    assert "tidak di atas SMA5" in h.alasan
    k3 = {s.uraian: s.terpenuhi for s in h.syarat if s.aturan == "K3"}
    assert k3["IHSG close > SMA5"] is False and k3["IHSG close > SMA20"] is True


def test_k2_breadth_lemah():
    h = _hitung(_breadth(1, 4))
    assert (h.status, h.aturan) == (JANGAN_BELI, "K2")
    assert h.alasan == "Breadth lemah: 20% < 30%"


def test_k1_ihsg_tren_turun():
    h = _hitung(_breadth(5, 0), ihsg=bar(turun(T + 1, 7000), indeks=True))
    assert (h.status, h.aturan, h.alasan) == (JANGAN_BELI, "K1", "IHSG dalam tren turun")


def test_k1_butuh_kedua_syarat_koreksi_awal_hati_hati():
    """close < SMA20 tetapi SMA5 masih > SMA20 → bukan K1 (DAN, bukan ATAU) → K4."""
    c = naik(T, 7000, 0.01)
    c = c + [c[-1] * 0.88]  # koreksi tajam pada t
    h = _hitung(_breadth(3, 2), ihsg=bar(c, indeks=True))
    assert h.ihsg_close < h.ihsg_sma_panjang and h.ihsg_sma_pendek > h.ihsg_sma_panjang  # prasyarat
    assert (h.status, h.aturan) == (HATI_HATI, "K4")


def test_k3_butuh_sma_pendek_di_atas_sma_panjang_pemulihan_v():
    """Pemulihan tajam: close > SMA5 dan close > SMA20, tetapi SMA5 < SMA20 → K4, bukan K3."""
    c = turun(T - 1, 7000, 0.01)
    c = c + [c[-1] * 1.06, c[-1] * 1.06 * 1.06]
    h = _hitung(_breadth(3, 2), ihsg=bar(c, indeks=True))
    assert h.ihsg_close > h.ihsg_sma_panjang and h.ihsg_close > h.ihsg_sma_pendek  # prasyarat
    assert h.ihsg_sma_pendek < h.ihsg_sma_panjang
    assert (h.status, h.aturan) == (HATI_HATI, "K4")
    assert "SMA5" in h.alasan and "tidak di atas SMA20" in h.alasan


def test_k1_mendahului_k2():
    h = _hitung(_breadth(0, 5), ihsg=bar(turun(T + 1, 7000), indeks=True))
    assert h.aturan == "K1"


# ---------------------------------------------------------------------------
# Batas tepat (perbandingan silang, bukan float)
# ---------------------------------------------------------------------------


def test_breadth_tepat_50_persen_lolos_k3():
    h = _hitung(_breadth(2, 2))
    assert h.breadth.breadth_pct == Decimal(50)
    assert h.status == BOLEH_BELI


def test_breadth_tepat_30_persen_bukan_k2():
    h = _hitung(_breadth(3, 7))
    assert h.breadth.breadth_pct == Decimal(30)
    assert (h.status, h.aturan) == (HATI_HATI, "K4")


def test_ambang_sedikit_di_atas_breadth_k2():
    # Ambang 30,000001%: breadth tepat 30% kini di bawah ambang → K2.
    cfg = cfg_uji({**_UBAH, "kondisi_pasar.breadth_jangan_maks_pct": "30.000001"})
    h = _hitung(_breadth(3, 7), cfg=cfg)
    assert h.aturan == "K2"


def test_batas_tepat_yang_meleset_bila_memakai_float():
    """57/100 × 100 = 56,99999999999999 dalam float; perbandingan tepat harus tetap lolos 57%."""
    assert 57 / 100 * 100 < 57  # prasyarat: float memang meleset di sini
    cfg = cfg_uji({**_UBAH, "kondisi_pasar.breadth_boleh_min_pct": 57})
    h = _hitung(_breadth(57, 43), cfg=cfg)
    assert h.breadth.breadth_pct == Decimal(57)
    assert h.status == BOLEH_BELI


def test_cakupan_tepat_yang_meleset_bila_memakai_float():
    cfg = cfg_uji({**_UBAH, "kondisi_pasar.min_cakupan_breadth_pct": 57})
    saham = _breadth(57, 0)
    saham.update({f"H{i:02d}": bar(naik(T + 1, 400 + i), buang=[T]) for i in range(43)})  # tanpa bar t
    h = _hitung(saham, cfg=cfg)
    assert (h.breadth.n_tercatat, h.breadth.n_terhitung) == (100, 57)
    assert h.aturan != "K0"



# ---------------------------------------------------------------------------
# K0: data
# ---------------------------------------------------------------------------


def test_k0_bar_ihsg_t_tidak_ada():
    ihsg = bar(naik(T + 1, 7000), indeks=True, buang=[T])
    h = _hitung(_breadth(4, 1), ihsg=ihsg)
    assert (h.status, h.aturan) == (JANGAN_BELI, "K0")
    assert h.alasan.startswith("Data tidak lengkap: ")
    assert f"bar IHSG {tgl(T).isoformat()} tidak ada" in h.alasan


def test_k0_data_ihsg_tidak_tersedia():
    saham = _breadth(4, 1)
    p = pasar(saham, None, CFG, tgl(T + 5))
    h = hitung_kondisi_pasar(p.tampilan(tgl(T)), list(saham))
    assert h.aturan == "K0" and "data IHSG tidak tersedia" in h.alasan


def test_k0_sma_panjang_ihsg_belum_terdefinisi():
    ihsg = bar(naik(10, 7000), mulai=T - 9, indeks=True)
    h = _hitung(_breadth(4, 1), ihsg=ihsg)
    assert h.aturan == "K0" and "SMA20 IHSG belum terdefinisi" in h.alasan


def test_k0_data_ihsg_bolong():
    ihsg = bar(naik(T + 1, 7000), indeks=True, buang=[30, 31, 32])  # 3 bar hilang > maks 2
    h = _hitung(_breadth(4, 1), ihsg=ihsg)
    assert h.aturan == "K0" and "data IHSG bolong (3 bar hilang" in h.alasan


def test_k0_anomali_ihsg_dan_anomali_ok():
    c = naik(T + 1, 7000)
    c = c[:30] + [x * 1.6 for x in c[30:]]  # +60% pada posisi 30, dalam 20 bar terakhir
    ihsg = bar(c, indeks=True)
    h = _hitung(_breadth(4, 1), ihsg=ihsg)
    assert h.aturan == "K0" and "anomali data IHSG" in h.alasan
    h_ok = _hitung(_breadth(4, 1), ihsg=ihsg, anomali_ok={("IHSG", tgl(30))})
    assert h_ok.status == BOLEH_BELI


def test_k0_anomali_ihsg_di_luar_jendela_sma_panjang_tidak_k0():
    c = naik(T + 1, 7000)
    c = c[:10] + [x * 1.6 for x in c[10:]]  # 31 bar sebelum t > 20
    h = _hitung(_breadth(4, 1), ihsg=bar(c, indeks=True))
    assert h.aturan != "K0"


@pytest.mark.parametrize("n_hilang, harapan_k0", [(1, False), (2, True)])
def test_k0_cakupan_breadth_batas_80_persen(n_hilang, harapan_k0):
    saham = _breadth(5, 0)
    for i in range(n_hilang):
        saham[f"N{i:02d}"] = bar(naik(T + 1, 500 + i), buang=[T])  # tanpa bar t
    h = _hitung(saham)
    assert h.breadth.n_tercatat == 5 and h.breadth.n_terhitung == 5 - n_hilang
    assert (h.aturan == "K0") is harapan_k0
    if harapan_k0:
        assert "cakupan breadth 60% < 80% (3 dari 5 saham)" in h.alasan


def test_k0_saham_terhitung_kurang_dari_minimum():
    h = _hitung(_breadth(2, 0))
    assert h.aturan == "K0" and "saham breadth terhitung 2 < 3" in h.alasan
    assert _hitung(_breadth(3, 0)).aturan != "K0"  # tepat minimum lolos


def test_k0_ihsg_alasan_seri_muncul_di_tabel():
    from asisten_bei.data.tampilan import DataPasar

    from bantu_pasar import KAL, data_saham

    saham = _breadth(4, 1)
    akhir = tgl(T + 5)
    ds = {k: data_saham(k, df, CFG, akhir) for k, df in saham.items()}
    ih = data_saham("IHSG", bar(naik(T + 1, 7000), indeks=True), CFG, akhir, indeks=True,
                    alasan_seri="GAGAL_AMBIL")
    h = hitung_kondisi_pasar(DataPasar(ds, ih, KAL, CFG).tampilan(tgl(T)), list(saham))
    assert h.aturan == "K0" and "data IHSG tidak dapat dipakai (GAGAL_AMBIL)" in h.alasan
    baris = next(s for s in h.syarat if s.uraian == "Data IHSG tersedia dan dapat dipakai")
    assert (baris.nilai, baris.terpenuhi, baris.tanda) == ("GAGAL_AMBIL", False, "✗")


def test_tabel_k0_menandai_syarat_yang_gagal():
    h = _hitung(_breadth(4, 1), ihsg=bar(naik(T + 1, 7000), indeks=True, buang=[30, 31, 32]))
    k0 = {s.uraian: s.terpenuhi for s in h.syarat if s.aturan == "K0"}
    assert k0["Bar IHSG hilang dalam 60 hari bursa terakhir"] is False


def test_ihsg_bar_hilang_tepat_maksimum_bukan_k0():
    h = _hitung(_breadth(4, 1), ihsg=bar(naik(T + 1, 7000), indeks=True, buang=[30, 31]))  # 2 = maks
    assert h.aturan != "K0"


@pytest.mark.parametrize("n_sebelum, tercatat", [(5, True), (4, False)])
def test_tercatat_tepat_breadth_sma_bar_sebelum_t(n_sebelum, tercatat):
    saham = _breadth(3, 1, BARU=bar(naik(n_sebelum + 1, 300), mulai=T - n_sebelum))
    h = _hitung(saham)
    assert h.breadth.n_tercatat == 4 + int(tercatat)


def test_close_sama_dengan_sma_tidak_dihitung_di_atas():
    saham = _breadth(3, 1, DATAR=bar([700.0] * (T + 1)))  # close = SMA5 tepat
    h = _hitung(saham)
    assert (h.breadth.n_terhitung, h.breadth.n_di_atas_sma) == (5, 3)


def test_saham_belum_tercatat_tidak_masuk_penyebut():
    saham = _breadth(3, 1)
    saham["BARU"] = bar(naik(5, 300), mulai=T - 4)  # hanya 4 bar sebelum t < breadth_sma 5
    saham["IPO_TANPA_BAR_T"] = bar(naik(3, 300), mulai=T - 3)  # 3 bar, berakhir sebelum t
    h = _hitung(saham)
    assert h.breadth.n_tercatat == 4 and h.breadth.n_terhitung == 4
    assert h.breadth.cakupan_pct == Decimal(100)


@pytest.mark.parametrize("bar_sejak_anomali, dihitung", [(5, False), (6, True)])
def test_anomali_breadth_tepat_di_batas_jendela(bar_sejak_anomali, dihitung):
    """Anomali dengan tepat breadth_sma (5) bar valid dalam [a, t] masih dalam jendela."""
    c = naik(T + 1, 500)
    a = T + 1 - bar_sejak_anomali
    saham = _breadth(4, 0, ANOM=bar(c[:a] + [x * 1.6 for x in c[a:]]))
    br = hitung_breadth(
        pasar(saham, bar(naik(T + 1, 7000), indeks=True), CFG, tgl(T + 5)).tampilan(tgl(T)), list(saham)
    )
    assert br.n_terhitung == 4 + int(dihitung)


def test_anomali_saham_breadth_dalam_jendela_tidak_dihitung():
    c = naik(T + 1, 500)
    dalam = c[: T - 1] + [x * 1.6 for x in c[T - 1 :]]  # anomali 2 bar sebelum t (≤ 5 bar)
    luar = c[: T - 10] + [x * 1.6 for x in c[T - 10 :]]  # 11 bar ≤ t → di luar jendela 5
    saham = _breadth(4, 0, ANOM_DALAM=bar(dalam), ANOM_LUAR=bar(luar))
    br = hitung_breadth(
        pasar(saham, bar(naik(T + 1, 7000), indeks=True), CFG, tgl(T + 5)).tampilan(tgl(T)), list(saham)
    )
    assert br.n_tercatat == 6 and br.n_terhitung == 5


def test_kode_breadth_tanpa_data_dan_ganda_diabaikan():
    saham = _breadth(3, 1)
    h = _hitung(saham, kode_breadth=[*saham, "TIDAK_ADA", "N00", "IHSG"])
    assert h.breadth.n_tercatat == 4


# ---------------------------------------------------------------------------
# Akibat status & kausalitas
# ---------------------------------------------------------------------------


def test_parameter_entry_mengikuti_config():
    p = parameter_entry(BOLEH_BELI, CFG)
    assert (p.boleh_entry, p.pengali_risiko, p.maks_entry_baru_per_hari) == (
        True, Decimal(1), CFG.risiko.maks_entry_baru_per_hari
    )
    p = parameter_entry(HATI_HATI, CFG)
    assert (p.boleh_entry, p.pengali_risiko, p.maks_entry_baru_per_hari) == (True, Decimal("0.5"), 1)
    p = parameter_entry(JANGAN_BELI, CFG)
    assert (p.boleh_entry, p.maks_entry_baru_per_hari) == (False, 0)
    with pytest.raises(ValueError):
        parameter_entry("LAIN", CFG)  # type: ignore[arg-type]


def test_status_tidak_berubah_oleh_data_sesudah_t():
    """Data sesudah t (IHSG jatuh, breadth runtuh) tidak memengaruhi status pada t."""
    saham = _breadth(3, 2)
    dasar = _hitung(saham)
    ekor = {k: bar(list(df["close"]) + turun(5, df["close"].iloc[-1], 0.2)) for k, df in saham.items()}
    ihsg = bar(naik(T + 1, 7000) + turun(5, 7000 * 1.002**T, 0.2), indeks=True)
    p = pasar(ekor, ihsg, CFG, tgl(T + 5))
    sesudah = hitung_kondisi_pasar(p.tampilan(tgl(T)), list(ekor))
    assert (sesudah.status, sesudah.aturan, sesudah.alasan) == (dasar.status, dasar.aturan, dasar.alasan)
    assert sesudah.breadth == dasar.breadth
