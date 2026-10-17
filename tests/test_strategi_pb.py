"""Uji T-ST strategi PB — pullback dalam tren naik (SPEC §12.1, §13.1 K_sinyal PB, §22.1 T-ST/T-CFG).

Fixture sintetis kecil dan deterministik dengan periode config yang diperkecil agar setiap nilai bisa
dihitung tangan. Semua harga fiktif (ruang tersesuaikan); pembulatan harga memakai rupiah bulat karena
fraksi harga dipaksa `terverifikasi: false` (tidak ada tabel fraksi BEI di uji ini).

Config uji (semua parameter PB disematkan eksplisit, tidak bergantung pada nilai rilis):
  indikator.atr_periode = 3, likuiditas.periode_hari = 3, data.pengali_volume = 1, entry.gap_maks_atr = 0,5
  pb: sma_cepat 3, sma_tren 5, sma_panjang 8, lookback_slope_sma_tren 2, lookback_sentuh_sma_cepat 2,
      maks_jarak_atas_sma_cepat_atr 1,0, lookback_swing_low 3, penyangga_stop_atr 0,2,
      maks_jarak_stop_atr 3,0, target_r 2,0

Fixture dasar (12 bar hari bursa HARI[0..11], t = posisi 11 = 2024-01-16, volume 1.000.000):
  bar "standar" posisi 0..10: open = close = c, high = c + 20, low = c − 20
     pos:   0     1     2     3     4     5     6     7     8     9    10
     c  : 1000  1020  1040  1060  1080  1100  1120  1140  1120  1100  1116
  bar t (pos 11): open 1150, high 1196, low 1146, close 1171

Hitung tangan:
  TR_0 = high − low = 40; pos 1..10: |Δclose| ≤ 20 → TR = max(40, |h − c_{−1}|, |l − c_{−1}|) = 40.
  ATR_2 = (40 + 40 + 40)/3 = 40 → ATR_10 = 40 (Wilder dengan TR tetap 40).
  TR_11 = max(1196 − 1146 = 50, |1196 − 1116| = 80, |1146 − 1116| = 30) = 80
  ATR_11 = (40 × 2 + 80)/3 = 160/3 = 53,3333…
  SMA3_t     = (1100 + 1116 + 1171)/3 = 3387/3 = 1129
  SMA3_{t−1} = (1120 + 1100 + 1116)/3 = 3336/3 = 1112
  SMA5_t     = (1140 + 1120 + 1100 + 1116 + 1171)/5 = 5647/5 = 1129,4
  SMA5_{t−2} = (1100 + 1120 + 1140 + 1120 + 1100)/5 = 5580/5 = 1116
  SMA8_t     = (1080 + 1100 + 1120 + 5647)/8 = 8947/8 = 1118,375
  CLV_t      = (1171 − 1146)/(1196 − 1146) = 25/50 = 0,5
  LLV(low,3)_t = min(1080, 1096, 1146) = 1080
  PB1: 1171 > 1129,4 ✓ dan 1129,4 > 1118,375 ✓
  PB2: 1129,4 > 1116 ✓
  PB3: k=0: low 1146 > SMA3 1129 (tidak); k=1: low 1096 ≤ SMA3 1112 ✓ (satu-satunya sentuhan di jendela)
  PB4: 1171 > high kemarin 1136 ✓ dan 1171 > open 1150 ✓
  PB5: (1171 − 1129)/(160/3) = 42 × 3/160 = 0,7875 ≤ 1,0 ✓
  harga_maks = 1171 + 0,5 × 160/3 = 1197,666… → bawah 1197 (bukan 1198)
  stop       = 1080 − 0,2 × 160/3 = 1069,333… → atas 1070 (bukan 1069)
  jarak stop = (1197 − 1070)/53,333333 = 2,38125… ATR ≤ 3,0 → valid
  K_sinyal   = 0,5 × (1 − 0,7875) + 0,5 × 0,5 = 0,10625 + 0,25 = 0,35625
  RataNilai  = (1100 + 1116 + 1171) × 1.000.000 / 3 = 1.129.000.000
"""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd
import pytest

from asisten_bei.aturan_bei import AturanBEI
from asisten_bei.data.tampilan import DataPasar
from asisten_bei.strategi.dasar import CekAturan, EvaluasiStrategi, daftar_strategi
from asisten_bei.strategi.pullback import Pullback
from bantu_pasar import HARI, KAL, cfg_uji, data_saham, tgl

KODE = "UJPB"
POS_T = 11
ATURAN = ["PB1", "PB2", "PB3", "PB4", "PB5"]

UBAH_DASAR = {
    "indikator.atr_periode": 3,
    "likuiditas.periode_hari": 3,
    "data.pengali_volume": 1,
    "entry.gap_maks_atr": 0.5,
    "pasar.fraksi_harga.terverifikasi": False,  # pembulatan ke rupiah bulat (§4.1)
    "strategi.pb.aktif": True,
    "strategi.pb.sma_cepat": 3,
    "strategi.pb.sma_tren": 5,
    "strategi.pb.sma_panjang": 8,
    "strategi.pb.lookback_slope_sma_tren": 2,
    "strategi.pb.lookback_sentuh_sma_cepat": 2,
    "strategi.pb.maks_jarak_atas_sma_cepat_atr": 1.0,
    "strategi.pb.lookback_swing_low": 3,
    "strategi.pb.penyangga_stop_atr": 0.2,
    "strategi.pb.maks_jarak_stop_atr": 3.0,
    "strategi.pb.target_r": 2.0,
}

CLOSE_DASAR = [1000, 1020, 1040, 1060, 1080, 1100, 1120, 1140, 1120, 1100, 1116]
BAR_T = {"open": 1150.0, "high": 1196.0, "low": 1146.0, "close": 1171.0}


def cfg_pb(ubah: dict | None = None):
    return cfg_uji({**UBAH_DASAR, **(ubah or {})})


def standar(c: float) -> dict:
    """Bar standar: open = close = c, high = c + 20, low = c − 20."""
    return {"open": float(c), "high": float(c) + 20, "low": float(c) - 20, "close": float(c)}


def buat_bar(ubah: dict | None = None, *, sesudah: list[dict] | None = None) -> pd.DataFrame:
    """Fixture dasar; `ubah` = {posisi: close_baru (bar standar) | {kolom: nilai}}; `sesudah` = bar
    tambahan setelah t (posisi 12, 13, …)."""
    baris = [standar(c) for c in CLOSE_DASAR] + [dict(BAR_T)] + list(sesudah or [])
    for pos, isi in (ubah or {}).items():
        baris[pos] = standar(isi) if isinstance(isi, (int, float)) else {**baris[pos], **isi}
    df = pd.DataFrame(baris, index=pd.DatetimeIndex(HARI[: len(baris)], name="tanggal").as_unit("ns"))
    df = df[["open", "high", "low", "close"]].astype("float64")
    df["volume"] = 1_000_000.0
    df["sumber"] = "yfinance"
    return df


def evaluasi(
    df: pd.DataFrame | None = None,
    cfg=None,
    *,
    pos_t: int = POS_T,
    split: pd.Series | None = None,
) -> EvaluasiStrategi:
    cfg = cfg or cfg_pb()
    df = buat_bar() if df is None else df
    akhir = max(tgl(pos_t), df.index[-1].date())
    ds = data_saham(KODE, df, cfg, akhir, split=split)
    pasar = DataPasar({KODE: ds}, None, KAL, cfg)
    dv = pasar.tampilan(tgl(pos_t))
    return Pullback(cfg).evaluasi(dv, KODE, AturanBEI(cfg))


def uraian(ev: EvaluasiStrategi, kode_aturan: str) -> str:
    return next(c.uraian for c in ev.cek if c.kode == kode_aturan)


def ringkas(ev: EvaluasiStrategi) -> dict:
    s = ev.sinyal
    hasil = {"gagal": ev.aturan_gagal, "ada_sinyal": s is not None}
    if s is not None:
        hasil.update(
            harga_maks=s.harga_maks, stop=s.stop, k_sinyal=round(s.k_sinyal, 9), target_r=s.target_r, valid=s.valid
        )
    return hasil


# ---------------------------------------------------------------------------
# Fixture yang lolos semua aturan
# ---------------------------------------------------------------------------


def test_fixture_dasar_lolos_semua_aturan_dan_level_hitung_tangan():
    ev = evaluasi()
    assert ev.kode == KODE and ev.strategi == "PB" and Pullback.kode == "PB"
    assert [c.kode for c in ev.cek] == ATURAN
    assert ev.aturan_gagal == []
    s = ev.sinyal
    assert s is not None
    assert (s.tanggal, s.kode, s.strategi) == (tgl(POS_T), KODE, "PB")
    assert s.harga_ref == Decimal("1171")
    assert s.harga_maks == Decimal("1197")  # 1197,666… dibulatkan ke BAWAH
    assert s.stop == Decimal("1070")  # 1069,333… dibulatkan ke ATAS
    assert s.atr == Decimal("53.333333")  # 160/3 nominal (F = 1), 6 desimal
    assert s.faktor_split == 1.0
    assert s.k_sinyal == pytest.approx(0.35625, abs=1e-12)
    assert s.target_r == 2.0
    assert s.rata_nilai == pytest.approx(1_129_000_000.0, rel=1e-12)
    assert s.valid is True and s.alasan_tidak_valid is None
    assert s.fraksi_dipakai is False  # fraksi belum terverifikasi → rupiah bulat + peringatan
    assert s.cek == ev.cek


def test_uraian_memuat_nilai_aktual():
    ev = evaluasi()
    assert uraian(ev, "PB1") == "close 1.171,00 > SMA5 1.129,40 dan SMA5 1.129,40 > SMA8 1.118,38"
    assert uraian(ev, "PB2") == "SMA5 1.129,40 > SMA5 2 bar lalu 1.116,00"
    assert uraian(ev, "PB3") == (
        "low ≤ SMA3 dalam 2 bar terakhir: hari ini low 1.146,00 > SMA3 1.129,00; "
        "1 bar lalu low 1.096,00 ≤ SMA3 1.112,00"
    )
    assert uraian(ev, "PB4") == "close 1.171,00 > high kemarin 1.136,00 dan close 1.171,00 > open 1.150,00"
    assert uraian(ev, "PB5") == "(close 1.171,00 − SMA3 1.129,00) / ATR 53,33 = 0,79 ≤ 1,00"
    assert ev.cek[3].teks().endswith("✓")


# ---------------------------------------------------------------------------
# Satu varian per aturan: hanya aturan itu yang dilanggar (dan batas sisi lainnya lolos)
# ---------------------------------------------------------------------------

# (id, perubahan bar, aturan yang gagal). Hitung tangan per varian:
VARIAN_GAGAL = [
    # c7 = 1348 → SMA5_t = (1348 + 1120 + 1100 + 1116 + 1171)/5 = 5855/5 = 1171 = close → bukan ">".
    # c7 tidak masuk jendela SMA3 dan selisih PB2 (c10 + c11 − c5 − c6) tidak berubah; ATR membesar.
    ("PB1_close_tidak_di_atas_sma_tren", {7: 1348}, "PB1"),
    # c7 = 1143, c4 = 1170 → SMA5_t = (1143 + 1120 + 1100 + 1116 + 1171)/5 = 5650/5 = 1130;
    # SMA8_t = (1170 + 1100 + 1120 + 5650)/8 = 9040/8 = 1130 = SMA5_t → bukan ">".
    # c4 di luar jendela SMA5_{t−2}; SMA5_{t−2} = (1100+1120+1143+1120+1100)/5 = 1116,6 < 1130 (PB2 lolos).
    ("PB1_sma_tren_tidak_di_atas_sma_panjang", {7: 1143, 4: 1170}, "PB1"),
    # c6 = 1187 → SMA5_{t−2} = (1100 + 1187 + 1140 + 1120 + 1100)/5 = 5647/5 = 1129,4 = SMA5_t → bukan ">".
    # SMA8_t = (1080 + 1100 + 1187 + 5647)/8 = 1126,75 < 1129,4 → PB1 tetap lolos.
    ("PB2_sma_tren_tidak_menanjak", {6: 1187}, "PB2"),
    # low_{t−1} = 1113 > SMA3_{t−1} 1112, dan low_t 1146 > SMA3_t 1129 → tidak ada sentuhan.
    ("PB3_tidak_menyentuh_sma_cepat", {10: {"low": 1113.0}}, "PB3"),
    # high_{t−1} = 1171 = close_t → bukan ">". TR/ATR membesar (PB5 makin longgar).
    ("PB4_close_tidak_di_atas_high_kemarin", {10: {"high": 1171.0}}, "PB4"),
    # open_t = 1171 = close_t → bukan ">".
    ("PB4_close_tidak_di_atas_open", {POS_T: {"open": 1171.0}}, "PB4"),
    # close_t = 1206, high_t = 1231: TR_t = 1231 − 1116 = 115, ATR = (80 + 115)/3 = 65,
    # SMA3_t = (1100 + 1116 + 1206)/3 = 1140,667 → (1206 − 1140,667)/65 = 65,333/65 = 1,0051 > 1,0.
    ("PB5_terlalu_jauh_di_atas_sma_cepat", {POS_T: {"high": 1231.0, "close": 1206.0}}, "PB5"),
]

# Sisi lain setiap batas: varian yang tepat lolos (perbedaan 1 rupiah dari varian gagal).
VARIAN_LOLOS = [
    ("PB1_close", {7: 1347}),  # SMA5_t = 5854/5 = 1170,8 < 1171
    ("PB1_sma", {7: 1143, 4: 1169}),  # SMA8_t = 9039/8 = 1129,875 < SMA5_t 1130
    ("PB2", {6: 1186}),  # SMA5_{t−2} = 5646/5 = 1129,2 < 1129,4
    ("PB3_sama_dengan_sma", {10: {"low": 1112.0}}),  # low = SMA3 1112 → "≤" terpenuhi
    # Sentuhan hanya pada k = 0: low_{t−1} 1113 > SMA3_{t−1} 1112, low_t 1129 = SMA3_t 1129.
    ("PB3_sentuh_hanya_hari_ini", {10: {"low": 1113.0}, POS_T: {"low": 1129.0}}),
    ("PB4_high", {10: {"high": 1170.0}}),  # 1171 > 1170
    ("PB4_open", {POS_T: {"open": 1170.0}}),  # 1171 > 1170
    # close 1204, high 1229: ATR = (80 + 113)/3 = 64,333; SMA3_t = 3420/3 = 1140 → 64/64,333 = 0,9948 ≤ 1
    ("PB5", {POS_T: {"high": 1229.0, "close": 1204.0}}),
]


@pytest.mark.parametrize(("ubah", "aturan"), [v[1:] for v in VARIAN_GAGAL], ids=[v[0] for v in VARIAN_GAGAL])
def test_varian_hanya_melanggar_satu_aturan(ubah, aturan):
    ev = evaluasi(buat_bar(ubah))
    assert ev.sinyal is None
    assert ev.aturan_gagal == [aturan]
    assert [c.kode for c in ev.cek] == ATURAN  # semua aturan tetap dievaluasi & dicantumkan
    gagal = next(c for c in ev.cek if c.kode == aturan)
    assert gagal.teks().endswith("✗")


@pytest.mark.parametrize("ubah", [v[1] for v in VARIAN_LOLOS], ids=[v[0] for v in VARIAN_LOLOS])
def test_sisi_lain_batas_aturan_lolos(ubah):
    ev = evaluasi(buat_bar(ubah))
    assert ev.aturan_gagal == []
    assert ev.sinyal is not None and ev.sinyal.valid


def test_pb5_batas_inklusif():
    # high_t = 1186: TR_t = 1186 − 1116 = 70 → ATR_t = (80 + 70)/3 = 50 (tepat); SMA3_t = 1129 (tepat)
    # → jarak = (1171 − 1129)/50 = 0,84 (tepat sama dengan literal 0.84).
    df = buat_bar({POS_T: {"high": 1186.0}})
    ev = evaluasi(df, cfg_pb({"strategi.pb.maks_jarak_atas_sma_cepat_atr": 0.84}))
    assert ev.aturan_gagal == []  # 0,84 ≤ 0,84
    # K_sinyal = 0,5 × (1 − 0,84) + 0,5 × CLV (1171 − 1146)/(1186 − 1146) = 0,08 + 0,3125 = 0,3925
    assert ev.sinyal is not None and ev.sinyal.k_sinyal == pytest.approx(0.3925, abs=1e-12)
    # harga_maks = 1171 + 0,5 × 50 = 1196; stop = 1080 − 0,2 × 50 = 1070 (sudah bulat)
    assert (ev.sinyal.harga_maks, ev.sinyal.stop, ev.sinyal.atr) == (Decimal("1196"), Decimal("1070"), Decimal("50"))
    ev = evaluasi(df, cfg_pb({"strategi.pb.maks_jarak_atas_sma_cepat_atr": 0.83}))
    assert ev.sinyal is None and ev.aturan_gagal == ["PB5"]


def test_k_sinyal_jarak_dipotong_ke_0_sampai_1():
    # Bawah: c9 = 1300 → SMA3_t = (1300 + 1116 + 1171)/3 = 1195,667 > close 1171 → jarak < 0 → clip 0.
    #   Aturan lain tetap lolos: SMA5_t = 5847/5 = 1169,4 < 1171; SMA8_t = 9147/8 = 1143,375;
    #   SMA5_{t−2} = 5780/5 = 1156; low_t 1146 ≤ SMA3_t (k = 0). K = 0,5 × (1 − 0) + 0,5 × 0,5 = 0,75.
    ev = evaluasi(buat_bar({9: 1300}))
    assert ev.aturan_gagal == []
    assert ev.sinyal is not None and ev.sinyal.k_sinyal == pytest.approx(0.75, abs=1e-12)
    # Atas: varian jarak 1,0051 dengan batas PB5 1,01 → clip 1.
    #   CLV = (1206 − 1146)/(1231 − 1146) = 60/85 → K = 0,5 × (1 − 1) + 0,5 × 60/85 = 30/85.
    ev = evaluasi(
        buat_bar({POS_T: {"high": 1231.0, "close": 1206.0}}),
        cfg_pb({"strategi.pb.maks_jarak_atas_sma_cepat_atr": 1.01}),
    )
    assert ev.aturan_gagal == []
    assert ev.sinyal is not None and ev.sinyal.k_sinyal == pytest.approx(30 / 85, abs=1e-12)


def test_uraian_pb3_gagal_menyebut_setiap_bar_yang_diperiksa():
    ev = evaluasi(buat_bar({10: {"low": 1113.0}}))
    assert uraian(ev, "PB3") == (
        "low ≤ SMA3 dalam 2 bar terakhir: hari ini low 1.146,00 > SMA3 1.129,00; "
        "1 bar lalu low 1.113,00 > SMA3 1.112,00"
    )


def test_pb3_jendela_sentuhan_k_sampai_lookback_minus_1():
    # Dasar: satu-satunya sentuhan di k = 1 = lookback − 1 → dihitung (lolos).
    assert evaluasi().aturan_gagal == []
    # Varian low_{t−1} = 1113: sentuhan terdekat di k = 2 (low_9 1080 ≤ SMA3_9 = (1140+1120+1100)/3 = 1120)
    # = lookback → di luar jendela {0, 1} → gagal; dengan lookback 3 sentuhan itu masuk → lolos.
    df = buat_bar({10: {"low": 1113.0}})
    assert evaluasi(df).aturan_gagal == ["PB3"]
    assert evaluasi(df, cfg_pb({"strategi.pb.lookback_sentuh_sma_cepat": 3})).aturan_gagal == []


# ---------------------------------------------------------------------------
# STOP_TERLALU_JAUH (§12.1 Validasi, §13.2 langkah 2)
# ---------------------------------------------------------------------------


def test_stop_terlalu_jauh_sinyal_tetap_dikembalikan_tidak_valid():
    # jarak = (1197 − 1070)/53,333333 = 2,38125… ATR > 2,38 → STOP_TERLALU_JAUH
    ev = evaluasi(cfg=cfg_pb({"strategi.pb.maks_jarak_stop_atr": 2.38}))
    assert ev.aturan_gagal == []
    s = ev.sinyal
    assert s is not None
    assert s.valid is False
    assert s.alasan_tidak_valid is not None
    assert "jarak stop" in s.alasan_tidak_valid
    assert "harga maks 1.197, stop 1.070" in s.alasan_tidak_valid
    assert (s.harga_maks, s.stop) == (Decimal("1197"), Decimal("1070"))  # level tetap dicatat


def test_stop_tepat_di_bawah_batas_jarak_valid():
    ev = evaluasi(cfg=cfg_pb({"strategi.pb.maks_jarak_stop_atr": 2.39}))  # 2,38125 ≤ 2,39
    assert ev.sinyal is not None and ev.sinyal.valid is True and ev.sinyal.alasan_tidak_valid is None


# ---------------------------------------------------------------------------
# NaN / riwayat kurang / ATR tidak sah
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("buang_awal", "gagal"),
    [
        # 7 bar (pos 5..11): SMA8_t NaN → PB1; SMA5_{t−2} = c5..c9 masih ada; ATR tetap 160/3.
        (5, ["PB1"]),
        # 6 bar (pos 6..11): SMA5_{t−2} butuh c5 → NaN → PB2 juga gagal.
        (6, ["PB1", "PB2"]),
        # 3 bar (pos 9..11): SMA3_{t−1} NaN dan low_t > SMA3_t → PB3 gagal; ATR_t = (40+40+80)/3 masih ada.
        (9, ["PB1", "PB2", "PB3"]),
        # 2 bar: ATR (n = 3) NaN → PB5 gagal; high_{t−1} dan open_t masih ada → PB4 lolos.
        (10, ["PB1", "PB2", "PB3", "PB5"]),
        # 1 bar: high_{t−1} tidak ada → PB4 juga gagal.
        (11, ATURAN),
    ],
)
def test_riwayat_kurang_nilai_nan_membuat_aturan_tidak_terpenuhi(buang_awal, gagal):
    df = buat_bar().iloc[buang_awal:]
    ev = evaluasi(df)
    assert ev.sinyal is None
    assert ev.aturan_gagal == gagal
    assert [c.kode for c in ev.cek] == ATURAN


def test_uraian_nan_ditampilkan_sebagai_strip():
    ev = evaluasi(buat_bar().iloc[5:])
    assert uraian(ev, "PB1") == "close 1.171,00 > SMA5 1.129,40 dan SMA5 1.129,40 > SMA8 —"
    ev = evaluasi(buat_bar().iloc[9:])
    assert "1 bar lalu low 1.096,00 vs SMA3 —" in uraian(ev, "PB3")


def test_atr_nan_hanya_pb5_gagal():
    # atr_periode 13 > 12 bar → ATR_t NaN; aturan lain tidak memakai ATR dan tetap lolos.
    ev = evaluasi(cfg=cfg_pb({"indikator.atr_periode": 13}))
    assert ev.sinyal is None
    assert ev.aturan_gagal == ["PB5"]
    assert "tidak dapat dihitung" in uraian(ev, "PB5")


def test_atr_nol_pb5_tidak_terpenuhi_tanpa_pembagian_nol():
    # 12 bar datar open = high = low = close = 1000 → TR = 0, ATR = 0.
    # PB1: 1000 > 1000 ✗; PB2: SMA sama ✗; PB3: low 1000 ≤ SMA3 1000 ✓; PB4: 1000 > 1000 ✗; PB5: ATR 0 ✗.
    df = buat_bar({p: {"open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0} for p in range(12)})
    ev = evaluasi(df)
    assert ev.sinyal is None
    assert ev.aturan_gagal == ["PB1", "PB2", "PB4", "PB5"]
    assert "ATR 0,00 tidak dapat dihitung" in uraian(ev, "PB5")


@pytest.mark.parametrize(
    "ubah",
    [
        # lookback_swing_low 13 > 12 bar → LLV NaN: semua aturan lolos tetapi stop tidak dapat dihitung.
        {"strategi.pb.lookback_swing_low": 13},
        # stop = 1080 − 25 × 160/3 = −253,3 ≤ 0 → tidak dapat dipakai (tanpa error pembulatan).
        {"strategi.pb.penyangga_stop_atr": 25.0},
    ],
    ids=["llv_nan", "stop_negatif"],
)
def test_stop_tidak_dapat_dihitung_tidak_membuat_sinyal(ubah):
    ev = evaluasi(cfg=cfg_pb(ubah))
    assert ev.sinyal is None
    assert ev.aturan_gagal == ["STOP"]
    assert [c.kode for c in ev.cek] == [*ATURAN, "STOP"]


# ---------------------------------------------------------------------------
# Tidak ada bar valid pada t
# ---------------------------------------------------------------------------


def _cek_tanpa_bar(ev: EvaluasiStrategi) -> None:
    assert ev.sinyal is None
    assert ev.cek == (CekAturan("DATA", f"tidak ada bar valid pada {tgl(POS_T).isoformat()}", False),)
    assert tgl(POS_T).isoformat() == "2024-01-16"
    assert ev.aturan_gagal == ["DATA"]


def test_tanpa_bar_valid_pada_t_volume_nol():
    df = buat_bar()
    df.loc[HARI[POS_T], "volume"] = 0.0  # bar tanpa transaksi → bukan bar valid
    _cek_tanpa_bar(evaluasi(df))


def test_tanpa_bar_pada_t_data_berhenti_sebelumnya():
    _cek_tanpa_bar(evaluasi(buat_bar().iloc[:POS_T]))


# ---------------------------------------------------------------------------
# Konversi nominal dengan split sesudah t (§8.4)
# ---------------------------------------------------------------------------


def test_konversi_nominal_dengan_split_sesudah_t():
    # Harga cache tersesuaikan. Split 1:2 pada pos 5 (sebelum t, tidak ikut F) dan 1:5 pada pos 13
    # (sesudah t) → F(t) = 5. Konversi ke nominal SEBELUM pembulatan:
    #   harga_maks = bawah(1197,666… × 5 = 5988,333…) = 5988  (bukan 1197 × 5 = 5985)
    #   stop       = atas(1069,333… × 5 = 5346,666…)  = 5347  (bukan 1070 × 5 = 5350)
    #   harga_ref = 1171 × 5 = 5855; ATR nominal = 160/3 × 5 = 266,666667
    sesudah = [standar(1180), standar(1190), standar(1200)]
    split = pd.Series(
        [2.0, 5.0], index=pd.DatetimeIndex([HARI[5], HARI[13]], name="tanggal").as_unit("ns"), name="split"
    )
    ev = evaluasi(buat_bar(sesudah=sesudah), split=split)
    assert ev.aturan_gagal == []
    s = ev.sinyal
    assert s is not None
    assert s.faktor_split == 5.0
    assert s.harga_ref == Decimal("5855")
    assert s.harga_maks == Decimal("5988")
    assert s.stop == Decimal("5347")
    assert s.atr == Decimal("266.666667")
    # K_sinyal dan RataNilai invarian terhadap split.
    assert s.k_sinyal == pytest.approx(0.35625, abs=1e-12)
    assert s.rata_nilai == pytest.approx(1_129_000_000.0, rel=1e-12)
    # (5988 − 5347)/266,666667 = 2,40375 ≤ 3 → valid
    assert s.valid is True


def test_tanpa_split_sesudah_t_faktor_satu():
    split = pd.Series([5.0], index=pd.DatetimeIndex([HARI[5]], name="tanggal").as_unit("ns"), name="split")
    s = evaluasi(split=split).sinyal
    assert s is not None and s.faktor_split == 1.0 and s.harga_maks == Decimal("1197")


# ---------------------------------------------------------------------------
# P3: bar sesudah t tidak memengaruhi evaluasi
# ---------------------------------------------------------------------------


def test_bar_sesudah_t_tidak_berpengaruh():
    acuan = evaluasi(buat_bar())
    liar = [
        {"open": 5000.0, "high": 9000.0, "low": 10.0, "close": 8000.0},  # lonjakan ekstrem
        {"open": 20.0, "high": 30.0, "low": 1.0, "close": 2.0},
        standar(1100),
    ]
    df = buat_bar(sesudah=liar).drop(HARI[13])  # termasuk bar hilang sesudah t
    assert evaluasi(df) == acuan


# ---------------------------------------------------------------------------
# T-CFG: setiap kunci strategi.pb.* (dan entry.gap_maks_atr) benar-benar berpengaruh
# ---------------------------------------------------------------------------

# (kunci, nilai baru, perubahan bar fixture | None, harapan). Pembanding = fixture yang sama dengan
# config dasar. Hitung tangan:
KASUS_CFG = [
    # Varian low_{t−1} = 1113 (PB3 gagal dengan SMA3). SMA4_{t−1} = (1140+1120+1100+1116)/4 = 1119 ≥ 1113
    # → menyentuh → lolos.
    ("strategi.pb.sma_cepat", 4, {10: {"low": 1113.0}}, {"gagal": []}),
    # SMA2_t = (1116 + 1171)/2 = 1143,5 → jarak (1171 − 1143,5)/(160/3) = 0,515625
    # → K_sinyal = 0,5 × 0,484375 + 0,25 = 0,4921875 (dasar 0,35625).
    ("strategi.pb.sma_cepat", 2, None, {"gagal": [], "k_sinyal": 0.4921875}),
    # SMA9_t = (1060 + 8947)/9 = 1111,89 < SMA8_t 1118,375 → PB1 gagal.
    ("strategi.pb.sma_tren", 9, None, {"gagal": ["PB1"]}),
    # SMA2_t = 1143,5 > SMA5_t 1129,4 → PB1 gagal.
    ("strategi.pb.sma_panjang", 2, None, {"gagal": ["PB1"]}),
    # Varian c6 = 1187 (PB2 gagal dengan lookback 2). SMA5_{t−3} = (1080+1100+1187+1140+1120)/5 = 1125,4
    # < 1129,4 → lolos.
    ("strategi.pb.lookback_slope_sma_tren", 3, {6: 1187}, {"gagal": []}),
    # Hanya k = 0: low_t 1146 > SMA3_t 1129 → PB3 gagal.
    ("strategi.pb.lookback_sentuh_sma_cepat", 1, None, {"gagal": ["PB3"]}),
    # Varian low_{t−1} = 1113: k = 2 masuk jendela (low_9 1080 ≤ SMA3_9 1120) → lolos.
    ("strategi.pb.lookback_sentuh_sma_cepat", 3, {10: {"low": 1113.0}}, {"gagal": []}),
    # 0,7875 > 0,78 → PB5 gagal.
    ("strategi.pb.maks_jarak_atas_sma_cepat_atr", 0.78, None, {"gagal": ["PB5"]}),
    # Varian PB5 gagal (jarak 1,0051): batas 1,01 → lolos.
    ("strategi.pb.maks_jarak_atas_sma_cepat_atr", 1.01, {POS_T: {"high": 1231.0, "close": 1206.0}}, {"gagal": []}),
    # LLV(low, 1)_t = 1146 → stop = atas(1146 − 10,666…) = atas(1135,333…) = 1136.
    ("strategi.pb.lookback_swing_low", 1, None, {"stop": Decimal("1136")}),
    # stop = atas(1080 − 0,5 × 160/3) = atas(1053,333…) = 1054.
    ("strategi.pb.penyangga_stop_atr", 0.5, None, {"stop": Decimal("1054")}),
    # 2,38125 > 2,0 → STOP_TERLALU_JAUH.
    ("strategi.pb.maks_jarak_stop_atr", 2.0, None, {"valid": False}),
    ("strategi.pb.target_r", 1.5, None, {"target_r": 1.5}),
    # harga_maks = bawah(1171 + 0,25 × 160/3) = bawah(1184,333…) = 1184.
    ("entry.gap_maks_atr", 0.25, None, {"harga_maks": Decimal("1184")}),
]


@pytest.mark.parametrize(
    ("kunci", "nilai", "ubah_bar", "harapan"),
    KASUS_CFG,
    ids=[f"{k}={v}" + ("-varian" if u else "") for k, v, u, _ in KASUS_CFG],
)
def test_kunci_config_berpengaruh(kunci, nilai, ubah_bar, harapan):
    df = buat_bar(ubah_bar)
    dasar = ringkas(evaluasi(df))
    hasil = ringkas(evaluasi(df, cfg_pb({kunci: nilai})))
    assert hasil != dasar
    for k, v in harapan.items():
        assert hasil[k] == v, k


def test_semua_kunci_pb_yang_dipakai_evaluasi_teruji():
    """Penjaga: setiap kunci strategi.pb yang dibaca `evaluasi` punya kasus T-CFG (aktif: daftar_strategi;
    stop_waktu_hari dipakai mesin exit, bukan evaluasi sinyal)."""
    teruji = {k.split(".")[-1] for k, *_ in KASUS_CFG if k.startswith("strategi.pb.")}
    semua = set(cfg_pb().strategi.pb.model_dump()) - {"aktif", "stop_waktu_hari"}
    assert teruji == semua


# ---------------------------------------------------------------------------
# Registri strategi
# ---------------------------------------------------------------------------


def test_daftar_strategi_memuat_pb_bila_aktif():
    daftar = daftar_strategi(cfg_pb({"strategi.pb.aktif": True}))
    pb = [s for s in daftar if isinstance(s, Pullback)]
    assert len(pb) == 1 and pb[0].kode == "PB"
    assert daftar[0] is pb[0]  # urutan PB, BO, MR
    hanya = daftar_strategi(cfg_pb(), hanya=["pb"])
    assert len(hanya) == 1 and isinstance(hanya[0], Pullback)


def test_daftar_strategi_tanpa_pb_bila_tidak_aktif():
    cfg = cfg_pb({"strategi.pb.aktif": False})
    assert not any(isinstance(s, Pullback) for s in daftar_strategi(cfg))
    assert daftar_strategi(cfg, hanya=["pb"]) == []


# ---------------------------------------------------------------------------
# Config rilis (periode asli dari config.yaml): kolom yang dibaca tersedia
# ---------------------------------------------------------------------------


def test_berjalan_dengan_periode_config_rilis():
    cfg = cfg_uji({"pasar.fraksi_harga.terverifikasi": False})
    p = cfg.strategi.pb
    n = max(p.sma_panjang, p.sma_tren + p.lookback_slope_sma_tren) + 20
    close = [1000.0 * (1.002**i) for i in range(n)]
    df = pd.DataFrame(
        {"open": close, "high": [c * 1.01 for c in close], "low": [c * 0.99 for c in close], "close": close},
        index=pd.DatetimeIndex(HARI[:n], name="tanggal").as_unit("ns"),
    )
    df["volume"] = 1_000_000.0
    df["sumber"] = "yfinance"
    ds = data_saham(KODE, df, cfg, tgl(n - 1))
    dv = DataPasar({KODE: ds}, None, KAL, cfg).tampilan(tgl(n - 1))
    ev = Pullback(cfg).evaluasi(dv, KODE, AturanBEI(cfg))
    assert [c.kode for c in ev.cek] == ATURAN
    assert f"SMA{p.sma_tren}" in uraian(ev, "PB1") and f"SMA{p.sma_panjang}" in uraian(ev, "PB1")
    assert f"SMA{p.sma_cepat}" in uraian(ev, "PB5")
    assert "—" not in uraian(ev, "PB1")  # riwayat cukup: tidak ada NaN
    # Deret naik geometris: close_t = open_t → PB4 gagal; nilai lain terdefinisi.
    assert "PB4" in ev.aturan_gagal
    assert not math.isnan(dv.sebelum(KODE, "atr", 0))
