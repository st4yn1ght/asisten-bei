"""T-PF: saran posisi `portfolio.csv` (SPEC §15.3, §15.4), T-SP bagian live (posisi melewati split 1:5), dan
T-SK bagian portofolio live (slot/sektor dari saran §15.4 ke seleksi §13.2).

Semua kode saham, harga, dan sektor FIKTIF. Config rilis (§7): fee 0,15 %/0,25 %, slippage 0,10 %, fee minimum 0,
fraksi belum diverifikasi (pembulatan rupiah bulat), PB target 2R, BO trailing 3 × ATR(14), MR keluar SMA5 mulai
hari ke-2. Bar sintetis `bantu_pasar.bar`: open = close, high = close × 1,01, low = close × 0,99.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from asisten_bei import portofolio as pf
from asisten_bei.inputs import Akun, BarisPortofolio, Masukan, SahamUniverse
from asisten_bei.kondisi_pasar import BOLEH_BELI
from asisten_bei.seleksi import BATAS_SEKTOR, DIBLOKIR_PORTOFOLIO, REKOMENDASI, SLOT_PENUH, seleksi
from asisten_bei.config import resolusi_konteks
from bantu_pasar import bar, cfg_uji, hari, pasar, tgl
from test_skor_seleksi import berskor

D = Decimal
CFG = cfg_uji()
N = 300  # bar per saham; t = hari ke-(N−1)
T = tgl(N - 1)
KAS = D("3000000")
FB, FJ, SLIP = D("0.0015"), D("0.0025"), D("0.001")


def baris(kode: str, tanggal_beli: date, harga: str, stop: str, *, lot: int = 1, strategi: str = "PB",
          sektor: str | None = None, harga_per_tanggal: date | None = None, lembar: int | None = None,
          no: int = 2) -> BarisPortofolio:
    return BarisPortofolio(kode=kode, tanggal_beli=tanggal_beli, harga_beli=D(harga), lot=lot, strategi=strategi,
                           stop_awal=D(stop), sektor=sektor, harga_per_tanggal=harga_per_tanggal, lembar=lembar,
                           catatan="", baris=no)


def masukan(*rows: BarisPortofolio, universe: dict[str, str] | None = None, kas: D = KAS) -> Masukan:
    universe = universe if universe is not None else {r.kode: "SEKTOR_FIKTIF_1" for r in rows}
    return Masukan(
        universe=[SahamUniverse(k, f"{k} Tbk (FIKTIF)", s) for k, s in universe.items()],
        breadth=[], riwayat=None, exclude=[], portofolio=list(rows), akun=Akun(kas, T, True),
    )


def datar(close: float = 1000.0, n: int = N, **kw) -> pd.DataFrame:
    return bar([close] * n, **kw)


def ubah(df: pd.DataFrame, pos: int, **kolom: float) -> pd.DataFrame:
    """Ubah OHLC bar pada hari ke-`pos` (posisi absolut di `HARI`)."""
    df = df.copy()
    for k, v in kolom.items():
        df.loc[hari(pos), k] = v
    return df


def evaluasi(saham: dict[str, pd.DataFrame], m: Masukan, cfg=CFG, t: date = T, **kw) -> pf.HasilPortofolio:
    p = pasar(saham, None, cfg, t, split=kw.pop("split", None))
    return pf.evaluasi_portofolio(p, m, t, **kw)


def satu(h: pf.HasilPortofolio, kode: str) -> pf.SaranPosisi:
    return next(s for s in h.saran if s.kode == kode)


# ---------------------------------------------------------------------------
# §15.4 #1–#7
# ---------------------------------------------------------------------------


def test_1_tanpa_bar_t_tidak_dapat_dievaluasi_nilai_close_terakhir_dan_ekuitas_perkiraan():
    df = datar(buang=[N - 1])
    r = baris("ZPFA", tgl(N - 5), "1000", "950")
    h = evaluasi({"ZPFA": df}, masukan(r))
    s = satu(h, "ZPFA")
    assert (s.saran, s.nomor) == (pf.TIDAK_DAPAT_DIEVALUASI, 1)
    assert "tidak ada" in s.alasan and "level stop terakhir yang valid 950" in s.alasan
    assert s.perkiraan and h.perkiraan
    assert s.tanggal_close == tgl(N - 2) and s.nilai_idr == D(100) * D(1000)
    assert h.ekuitas_idr == KAS + D(100000)
    assert not s.dijadwalkan_jual  # tetap menempati slot


def test_1_tanpa_data_sama_sekali():
    kosong = datar().iloc[0:0]
    r = baris("ZPFB", tgl(N - 5), "1000", "950")
    s = satu(evaluasi({"ZPFB": kosong}, masukan(r)), "ZPFB")
    assert s.saran == pf.TIDAK_DAPAT_DIEVALUASI and "tidak ada data" in s.alasan
    assert s.nilai_idr == D(100000) and s.perkiraan  # harga beli sebagai nilai, ditandai ≈


def test_2_manual_di_bawah_stop_tetap_info():
    r = baris("ZPFC", tgl(N - 5), "1100", "1050", strategi="MANUAL")
    s = satu(evaluasi({"ZPFC": datar()}, masukan(r)), "ZPFC")
    assert (s.saran, s.nomor) == (pf.INFO_MANUAL, 2)
    assert "di bawah stop_awal" in s.alasan and "tanpa aturan exit sistem" in s.alasan
    # P&L = 100 × 1.000 − 100 × 1.100 × 1,0015 = 100.000 − 110.165 = −10.165
    assert s.pnl_idr == D("-10165")
    assert not s.dijadwalkan_jual


def test_3_stop_awal_ge_harga_beli_jual_tanpa_error():
    r = baris("ZPFD", tgl(N - 5), "1000", "1000")
    s = satu(evaluasi({"ZPFD": datar()}, masukan(r)), "ZPFD")
    assert (s.saran, s.nomor) == (pf.JUAL, 3)
    assert "di bawah stop sejak dibeli" in s.alasan
    assert s.dijadwalkan_jual


def test_3_stop_jauh_di_atas_harga_beli_r_na():
    r = baris("ZPFD", tgl(N - 5), "1000", "1100")  # risiko rencana ≤ 0 → R n/a
    s = satu(evaluasi({"ZPFD": datar()}, masukan(r)), "ZPFD")
    assert (s.saran, s.nomor) == (pf.JUAL, 3) and s.r is None and s.teks_r == "n/a"


def test_4_exit_sebelum_t_dan_pengingat():
    df = ubah(datar(), N - 3, low=940.0)  # stop 950 tersentuh dua hari sebelum t
    r = baris("ZPFE", tgl(N - 6), "1000", "950")
    h = evaluasi({"ZPFE": df}, masukan(r))
    s = satu(h, "ZPFE")
    assert (s.saran, s.nomor) == (pf.JUAL, 4)
    assert f"seharusnya keluar pada {tgl(N - 3).isoformat()} (STOP)" in s.alasan
    assert "perbarui portfolio.csv dan akun.yaml" in s.alasan
    assert any("ZPFE menurut data seharusnya sudah keluar" in p for p in h.pengingat)


def _mr_naik_pada(pos_naik: int) -> pd.DataFrame:
    """Close datar 1.000, lalu 1.100 pada hari ke-`pos_naik` (close > SMA5)."""
    close = [1000.0] * N
    close[pos_naik] = 1100.0
    return bar(close)


def test_4_jual_terjadwal_di_pembukaan_t_bukan_tahan():
    # MR dibeli t−3; close t−1 = 1.100 > SMA5 (hari ke-3) → KELUAR_SMA dijadwalkan untuk pembukaan t.
    r = baris("ZPFF", tgl(N - 4), "1000", "900", strategi="MR")
    s = satu(evaluasi({"ZPFF": _mr_naik_pada(N - 2)}, masukan(r)), "ZPFF")
    assert (s.saran, s.nomor) == (pf.JUAL, 4)
    assert f"seharusnya keluar pada {T.isoformat()} (KELUAR_SMA)" in s.alasan


def test_5_exit_order_otomatis_pada_t_cek_order():
    df = ubah(datar(), N - 1, low=940.0)
    r = baris("ZPFG", tgl(N - 4), "1000", "950")
    h = evaluasi({"ZPFG": df}, masukan(r))
    s = satu(h, "ZPFG")
    assert (s.saran, s.nomor) == (pf.CEK_ORDER, 5)
    assert s.level_order == D(950) and "terisi hari ini di 950 (STOP)" in s.alasan
    assert "JUAL di pembukaan t+1" in s.alasan
    assert not s.dijadwalkan_jual  # CEK ORDER tetap dihitung terisi


def test_5_target_pb_pada_t():
    df = ubah(datar(), N - 1, high=1101.0)  # target 1.000 + 2 × 50 = 1.100
    r = baris("ZPFG", tgl(N - 4), "1000", "950")
    s = satu(evaluasi({"ZPFG": df}, masukan(r)), "ZPFG")
    assert (s.saran, s.level_order) == (pf.CEK_ORDER, D(1100)) and "(TARGET)" in s.alasan


@pytest.mark.parametrize("aktif, harapan", [(True, pf.CEK_ORDER), (False, pf.TAHAN)])
def test_5_hari_entry_menghormati_stop_aktif_hari_entry(aktif, harapan):
    cfg = cfg_uji({"entry.stop_aktif_hari_entry": aktif})
    df = ubah(datar(), N - 1, low=940.0)
    r = baris("ZPFH", T, "1000", "950")
    s = satu(evaluasi({"ZPFH": df}, masukan(r), cfg=cfg), "ZPFH")
    assert s.saran == harapan


def test_6_mr_close_di_atas_sma5_jual_dengan_alasan():
    r = baris("ZPFI", tgl(N - 3), "1000", "900", strategi="MR")
    s = satu(evaluasi({"ZPFI": _mr_naik_pada(N - 1)}, masukan(r)), "ZPFI")
    assert (s.saran, s.nomor, s.jadwal) == (pf.JUAL, 6, "KELUAR_SMA")
    assert s.alasan == "MR: close 1.100 > SMA5 1.020"  # (4 × 1.000 + 1.100) / 5
    assert s.dijadwalkan_jual


def test_6_mr_hari_ke_1_belum_keluar_sma():
    r = baris("ZPFI", T, "1000", "900", strategi="MR")
    s = satu(evaluasi({"ZPFI": _mr_naik_pada(N - 1)}, masukan(r)), "ZPFI")
    assert s.saran == pf.TAHAN  # min_hari_tahan 2


def test_6_stop_waktu_pb_hari_ke_10():
    r = baris("ZPFJ", tgl(N - 10), "1000", "950")
    s = satu(evaluasi({"ZPFJ": datar()}, masukan(r)), "ZPFJ")
    assert (s.saran, s.jadwal, s.hari_ke) == (pf.JUAL, "STOP_WAKTU", 10)
    assert "hari ke-10" in s.alasan


def test_7_tahan_bo_trailing_hitung_tangan():
    # Close datar 1.000 → TR 20, ATR 20, HHV 1.010 → trailing 1.010 − 3 × 20 = 950 berlaku pada t.
    # Pada t close 1.050 (high 1.060,5; low 1.039,5): TR = 1.060,5 − 1.000 = 60,5;
    # ATR_t = (13 × 20 + 60,5)/14 = 22,892857…; trailing = 1.060,5 − 68,678571… = 991,82… → ke atas 992.
    close = [1000.0] * N
    close[N - 1] = 1050.0
    r = baris("ZPFK", tgl(N - 4), "1000", "900", strategi="BO")
    s = satu(evaluasi({"ZPFK": bar(close)}, masukan(r)), "ZPFK")
    assert (s.saran, s.nomor) == (pf.TAHAN, 7)
    assert s.stop_hari_ini == D(950) and s.stop_besok == D(992)
    assert "stop besok 992" in s.alasan and "naik dari 950" in s.alasan
    assert s.target is None and s.stop_waktu_hari is None and s.hari_ke == 4
    # P&L belum terealisasi: 100 × 1.050 − 100 × 1.000 × 1,0015 = 4.850; R = 4.850 / risiko rencana
    risiko = D(100) * (D(1000) * (1 + FB) - D(900) * (1 - SLIP) * (1 - FJ))
    assert s.pnl_idr == D(4850) and s.r == D(4850) / risiko


def test_7_tahan_pb_target_dan_stop_waktu():
    r = baris("ZPFL", tgl(N - 3), "1000", "950")
    s = satu(evaluasi({"ZPFL": datar()}, masukan(r)), "ZPFL")
    assert s.saran == pf.TAHAN
    assert s.target == D(1100) and s.stop_besok == D(950)
    assert "hari ke-3 dari 10 (stop waktu)" in s.alasan


# ---------------------------------------------------------------------------
# §15.3: split dan lonjakan tanpa data split (T-SP bagian live)
# ---------------------------------------------------------------------------

S_SPLIT = N - 5  # split 1:5 (disesuaikan sumber) pada hari ke-(N−5)


def _split_disesuaikan() -> tuple[pd.DataFrame, dict]:
    """Harga cache sudah disesuaikan: nominal 1.000 sebelum split = 200 tersesuaikan."""
    return datar(200.0), {"ZPSP": pd.Series({hari(S_SPLIT): 5.0})}


@pytest.mark.parametrize("cara", ["tanpa_harga_per_tanggal", "dengan_harga_per_tanggal"])
def test_split_dengan_dan_tanpa_harga_per_tanggal_sama(cara):
    df, split = _split_disesuaikan()
    if cara == "tanpa_harga_per_tanggal":
        r = baris("ZPSP", tgl(N - 8), "1000", "950")  # nilai nominal per tanggal beli (pra-split)
    else:  # sudah diperbarui ke nilai pasca-split
        r = baris("ZPSP", tgl(N - 8), "200", "190", lembar=500, harga_per_tanggal=tgl(S_SPLIT))
    h = evaluasi({"ZPSP": df}, masukan(r), split=split)
    s = satu(h, "ZPSP")
    assert s.saran == pf.TAHAN  # tidak ada saran JUAL palsu
    assert s.lembar == D(500) and s.harga_beli == D(200) and s.stop_besok == D(190) and s.target == D(220)
    # Nilai = 500 × 200; P&L Rp = hitung tangan nominal: 100.000 − 100 × 1.000 × 1,0015 = −150.
    assert s.nilai_idr == D(100000) and s.pnl_idr == D(-150)
    assert h.ekuitas_idr == KAS + D(100000)
    if cara == "tanpa_harga_per_tanggal":
        assert s.split is not None and "lembar 500" in s.split.teks("ZPSP") and h.banner_split
    else:
        assert s.split is None and not h.banner_split  # tidak disesuaikan dua kali


def test_split_belum_disesuaikan_sumber_dikonversi_tanpa_jual_palsu():
    close = [1000.0] * N
    for i in range(S_SPLIT, N):
        close[i] = 200.0  # cache tidak disesuaikan: lompat 1/5 pada tanggal split
    r = baris("ZPSQ", tgl(N - 8), "1000", "950")
    h = evaluasi({"ZPSQ": bar(close)}, masukan(r), split={"ZPSQ": pd.Series({hari(S_SPLIT): 5.0})})
    s = satu(h, "ZPSQ")
    assert s.saran == pf.TAHAN and s.stop_besok == D(190) and s.lembar == D(500)
    assert s.nilai_idr == D(100000) and s.pnl_idr == D(-150)
    assert any("belum disesuaikan" in p for p in s.peringatan)


def _manual_lonjakan() -> pd.DataFrame:
    close = [1000.0] * N
    for i in range(N - 3, N):
        close[i] = 1500.0  # +50 % tanpa data split
    df = bar(close)
    df["sumber"] = "csv_manual"
    return df


def test_lonjakan_tanpa_data_split_tidak_dapat_dievaluasi_dan_blokir():
    r = baris("ZPLJ", tgl(N - 6), "1000", "950")
    h = evaluasi({"ZPLJ": _manual_lonjakan()}, masukan(r), tanpa_data_split=lambda k: k == "ZPLJ")
    s = satu(h, "ZPLJ")
    assert (s.saran, s.blokir) == (pf.TIDAK_DAPAT_DIEVALUASI, True)
    assert "tanpa data split" in s.alasan and tgl(N - 3).isoformat() in s.alasan
    assert h.blokir and "ZPLJ" in h.blokir
    hs = seleksi([berskor("ZPLX", 60.0)], status_pasar=BOLEH_BELI, portofolio=h.keadaan,
                 konteks=resolusi_konteks(CFG, "live"), cfg=CFG, strategi_lolos=None, deduplikasi=True,
                 sektor_dari=lambda k: "SEKTOR_FIKTIF_9")
    k = hs.kandidat[0]
    assert k.status == DIBLOKIR_PORTOFOLIO and "ZPLJ" in k.alasan


def test_lonjakan_dengan_data_split_atau_anomali_ok_tidak_blokir():
    r = baris("ZPLJ", tgl(N - 6), "1000", "950")
    h = evaluasi({"ZPLJ": _manual_lonjakan()}, masukan(r))  # sumber punya data split
    assert not h.blokir and satu(h, "ZPLJ").saran != pf.TIDAK_DAPAT_DIEVALUASI
    m = masukan(r)
    from asisten_bei.inputs import AnomaliOk

    m.anomali_ok = [AnomaliOk("ZPLJ", tgl(N - 3), "")]
    h2 = evaluasi({"ZPLJ": _manual_lonjakan()}, m, tanpa_data_split=lambda k: True)
    assert not h2.blokir


# ---------------------------------------------------------------------------
# Pengingat konsistensi
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("open_t, dipegang, ada", [(1000.0, False, True), (1020.0, False, False),
                                                   (1000.0, True, False)])
def test_pengingat_rekomendasi_kemarin(open_t, dipegang, ada):
    df = ubah(datar(), N - 1, open=open_t, high=max(open_t, 1010.0))
    p = pasar({"ZPRK": df}, None, CFG, T)
    rek = [pf.RekomendasiLalu("ZPRK", "PB", tgl(N - 2), D(1010), D(950), 1)]
    hasil = pf.pengingat_rekomendasi(p.tampilan(T), rek, ["ZPRK"] if dipegang else [], CFG)
    assert bool(hasil) == ada
    if ada:
        assert "seharusnya terisi" in hasil[0] and "tidak ada di portfolio.csv" in hasil[0]


# ---------------------------------------------------------------------------
# T-SK bagian portofolio live
# ---------------------------------------------------------------------------

def _seleksi(h: pf.HasilPortofolio, *sinyal, sektor: str = "SEKTOR_FIKTIF_9"):
    return seleksi(list(sinyal), status_pasar=BOLEH_BELI, portofolio=h.keadaan, konteks=resolusi_konteks(CFG, "live"),
                   cfg=CFG, strategi_lolos=None, deduplikasi=True, sektor_dari=lambda k: sektor)


def _tiga(sektor: tuple[str, str, str], df_c: pd.DataFrame) -> tuple[dict, Masukan]:
    rows = [baris("ZPSA", tgl(N - 3), "1000", "950"), baris("ZPSB", tgl(N - 3), "1000", "950"),
            baris("ZPSC", tgl(N - 3), "1000", "950")]
    uni = dict(zip(("ZPSA", "ZPSB", "ZPSC"), sektor))
    return {"ZPSA": datar(), "ZPSB": datar(), "ZPSC": df_c}, masukan(*rows, universe=uni)


def test_slot_dibebaskan_saran_jual():
    saham, m = _tiga(("S1", "S2", "S3"), ubah(datar(), N - 3, low=940.0))  # ZPSC: exit sebelum t → JUAL
    h = evaluasi(saham, m)
    assert satu(h, "ZPSC").saran == pf.JUAL
    hs = _seleksi(h, berskor("ZPSX", 60.0, harga=("2030", "1888")))
    assert hs.kandidat[0].status == REKOMENDASI and hs.slot_awal == 1


def test_cek_order_tetap_menempati_slot():
    saham, m = _tiga(("S1", "S2", "S3"), ubah(datar(), N - 1, low=940.0))  # ZPSC: CEK_ORDER
    h = evaluasi(saham, m)
    assert satu(h, "ZPSC").saran == pf.CEK_ORDER
    hs = _seleksi(h, berskor("ZPSX", 60.0, harga=("2030", "1888")))
    assert hs.kandidat[0].status == SLOT_PENUH


def test_sektor_posisi_manual_dan_di_luar_universe_dihitung():
    manual = baris("ZPMN", tgl(N - 3), "1000", "950", strategi="MANUAL")
    luar = baris("ZPLU", tgl(N - 3), "1000", "950", sektor="SEKTOR_LUAR")
    m = masukan(manual, luar, universe={"ZPMN": "SEKTOR_MANUAL"})
    h = evaluasi({"ZPMN": datar(), "ZPLU": datar()}, m)
    assert {p.sektor for p in h.keadaan.posisi} == {"SEKTOR_MANUAL", "SEKTOR_LUAR"}
    for sek in ("SEKTOR_MANUAL", "SEKTOR_LUAR"):
        hs = _seleksi(h, berskor("ZPSX", 60.0, harga=("2030", "1888")), sektor=sek)
        assert hs.kandidat[0].status == BATAS_SEKTOR


def test_ekuitas_kas_ditambah_nilai_semua_posisi():
    rows = [baris("ZPEA", tgl(N - 3), "1000", "950"), baris("ZPEB", tgl(N - 3), "500", "450", lot=2, strategi="MANUAL")]
    h = evaluasi({"ZPEA": datar(1100.0), "ZPEB": datar(400.0)}, masukan(*rows))
    assert h.nilai_posisi_idr == D(100 * 1100 + 200 * 400)
    assert h.ekuitas_idr == KAS + h.nilai_posisi_idr and not h.perkiraan
    assert h.keadaan.ekuitas_idr == h.ekuitas_idr and h.keadaan.kas_idr == KAS
