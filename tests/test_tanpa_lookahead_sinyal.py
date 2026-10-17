"""T-LA1b (SPEC §22.1, §8.4, §8.8, P3): sinyal, skor, stop, dan harga_maks — data dipotong ≤ t vs data penuh.

Untuk setiap tanggal uji `t`, keluaran mesin dihitung dua kali:

- **penuh**: satu `DataPasar` dari seluruh data sintetis (akhir run `AKHIR_RUN`), dilihat lewat
  `tampilan(t)`;
- **dipotong**: `DataPasar` baru yang dibangun dari bar ≤ t saja (`akhir = t`), dilihat lewat
  `tampilan(t)`.

Yang dibandingkan (toleransi 0; NaN dianggap sama):

- `EvaluasiStrategi` setiap strategi (PB, BO, MR, ACAK, dan ACAK berpeluang 100% agar level harga
  teruji di hampir setiap pasangan) atas setiap saham — termasuk teks setiap cek aturan;
- setiap field `Sinyal` (harga_ref, harga_maks, stop, atr, faktor_split, k_sinyal, target_r,
  rata_nilai, fraksi_dipakai, valid, alasan_tidak_valid, …; daftar field diambil dari dataclass sehingga
  field baru otomatis ikut);
- eligibilitas (`universe.nilai_eligibilitas`) dan skor (`skor.beri_skor` atas sinyal saham eligible,
  K_rs atas himpunan eligible), termasuk setiap komponen K;
- `faktor_split(kode)` pada t.

Pasar sintetis dipakai ulang dari `tests/test_tanpa_lookahead.py` (random walk ±600 bar berseed, bar
volume 0, bar hilang, bar rusak, bar hari libur, lonjakan anomali, split yang sudah disesuaikan di CCCC
pada HARI[350], split yang BELUM disesuaikan sumber di DDDD pada HARI[380], saham baru melantai EEEE,
dan hari sesudah data berakhir).

Split dan `F(t)` — mengapa data dipotong menerima SELURUH seri split
--------------------------------------------------------------------
§8.4/§8.8: harga cache disesuaikan split untuk SELURUH riwayat, dan `F(d)` (hasil kali rasio split
sesudah d) adalah satu-satunya data "sesudah as_of" yang sah dipakai, karena ia hanya membatalkan
penyesuaian yang sudah tertanam di harga cache. Data sintetis menyimpan harga tersesuaikan untuk seluruh
seri. Memotong bar ≤ t tetapi juga membuang split > t menghasilkan keadaan yang tidak pernah ada di dunia
nyata: harga yang sudah dibagi 5 tanpa catatan split yang menjelaskannya. Pada keadaan itu F(t) = 1 dan
harga nominal CCCC sebelum HARI[350] menjadi 1/5 harga sebenarnya — selisih yang BUKAN look-ahead mesin,
melainkan pembanding yang salah (dibuktikan di `test_pemotongan_naif_split_memberi_nominal_salah`).

Maka:

1. Uji utama: data dipotong = bar ≤ t + metadata split as-of t (split ≤ t, ditambah split sesudah t yang
   penyesuaiannya tertanam di harga cache; `_split_potong`). Semua keluaran
   wajib IDENTIK, termasuk `faktor_split`. Ini membuktikan strategi/skor/eligibilitas hanya memakai bar
   ≤ t; jalur F(t) = 5 (CCCC sebelum HARI[350]) ikut teruji.
2. Uji rekonstruksi: pembanding paling setia untuk CCCC sebelum split adalah cache sebagaimana diunduh
   pada t — harga NOMINAL (tersesuaikan × 5), volume ÷ 5, tanpa split. Keputusan (pola lolos cek, ada
   tidaknya sinyal, valid) wajib sama dan level nominal (harga_ref, harga_maks, stop, atr) sama; hanya
   presisi float yang boleh berbeda pada nilai tak dibulatkan. Teks cek tidak dibandingkan: uraiannya
   memakai nilai ruang tersesuaikan (cek aturan dihitung sebelum konversi nominal), sehingga di backtest
   angka di teks cek sebelum split sesudahnya adalah harga tersesuaikan, bukan nominal — kosmetik, tidak
   memengaruhi keputusan karena semua aturan invarian skala.
3. DDDD (split BELUM disesuaikan sumber): harga sebelum HARI[380] adalah harga nominal mentah. Split itu
   tidak tertanam di harga cache sehingga tidak ikut F(t) (§8.4): F(t) = 1 dan level nominal data penuh
   sama dengan cache yang diunduh pada t; lihat `test_split_belum_disesuaikan_tidak_boleh_memberi_nominal_salah`.
   (Jalur F(t) = 5 di uji utama hanya untuk CCCC.)

T-LA3 (kanari): strategi/skor yang sengaja mengintip masa depan lewat `DataSaham` mentah, dan `shift(-1)`
yang disuntikkan ke `indikator.sma`, WAJIB membuat pemeriksa yang sama gagal; pembanding kausal yang juga
membaca `DataSaham` mentah (≤ t) wajib lolos.
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter
from datetime import date
from decimal import Decimal
from functools import lru_cache

import numpy as np
import pandas as pd
import pytest

from asisten_bei import indikator as ind
from asisten_bei import skor as modul_skor
from asisten_bei.aturan_bei import AturanBEI, ke_desimal_harga
from asisten_bei.data.provider import ke_ts
from asisten_bei.data.tampilan import DataPasar, DataSaham, DataView, bangun_data_saham
from asisten_bei.inputs import Akun, Masukan, SahamUniverse
from asisten_bei.strategi.acak import Acak
from asisten_bei.strategi.breakout import Breakout
from asisten_bei.strategi.dasar import (
    CekAturan,
    EvaluasiStrategi,
    Sinyal,
    Strategi,
    daftar_strategi,
    hitung_sinyal,
)
from asisten_bei.universe import AturanUniverse, kode_eligible, nilai_eligibilitas
from test_tanpa_lookahead import (
    AKHIR_RUN,
    ANOMALI_OK,
    BARU_MENTAH,
    BARU_SPLIT,
    HARI,
    KAL,
    MENTAH,
    SAHAM,
    SEED,
    SPLIT,
    _cfg,
)

# ---------------------------------------------------------------------------
# Config, pasar, dan strategi uji
# ---------------------------------------------------------------------------

# Ambang kecil agar saham sintetis (RataNilai ±1–2 miliar, ±5% bar hilang/volume 0) cukup sering eligible
# — K_rs lalu diperingkat atas 3–4 saham — sementara setiap alasan data (BAR_HILANG, ANOMALI, DATA_BASI,
# RIWAYAT_PENDEK, SPLIT_BELUM_DISESUAIKAN, KEMUNGKINAN_SUSPENSI) tetap muncul di rentang uji.
UBAH_LA = {
    "kondisi_pasar.min_saham_breadth": 2,
    "likuiditas.nilai_transaksi_min_idr": 500_000_000,
    "likuiditas.maks_hari_volume_nol": 2,
    "data.validasi.maks_bar_hilang": 5,
}
CFG_LA = _cfg(UBAH_LA)
CFG_ACAK_PENUH = _cfg({**UBAH_LA, "strategi.acak.peluang_sinyal_pct": 100.0})

KODE_UJI = [*SAHAM, "EEEE"]  # EEEE baru melantai pada HARI[350]
SUMBER = {**MENTAH, "EEEE": BARU_MENTAH}
SUMBER_SPLIT = {**SPLIT, "EEEE": BARU_SPLIT}

POS_SPLIT_CCCC = 350  # split 1:5 yang sudah disesuaikan sumber
POS_SPLIT_DDDD = 380  # split 1:5 yang BELUM disesuaikan sumber (q ≈ 1/5)


def _strategi_uji() -> list[tuple[str, Strategi]]:
    hasil = [(s.kode, s) for s in daftar_strategi(CFG_LA, termasuk_acak=True)]
    hasil.append(("ACAK100", Acak(CFG_ACAK_PENUH)))  # sinyal di hampir setiap bar valid → level harga teruji
    return hasil


STRATEGI = _strategi_uji()


def _aturan_universe() -> AturanUniverse:
    masukan = Masukan(
        universe=[SahamUniverse(k, k, "S" + k) for k in KODE_UJI], breadth=list(KODE_UJI), riwayat=None,
        exclude=[], portofolio=[], akun=Akun(Decimal(3_000_000), None, False),
    )
    return AturanUniverse.live(masukan)


ATURAN = _aturan_universe()


def _bangun(kode: str, mentah: pd.DataFrame, split: pd.Series, akhir: date) -> DataSaham:
    return bangun_data_saham(
        kode, mentah, split, adalah_indeks=(kode == "IHSG"), kalender=KAL, cfg=CFG_LA,
        anomali_ok=ANOMALI_OK, akhir=akhir,
    )


def _sampai(df: pd.DataFrame, d: date) -> pd.DataFrame:
    return df[df.index <= ke_ts(d)]


@lru_cache(maxsize=1)
def pasar_penuh() -> DataPasar:
    saham = {k: _bangun(k, SUMBER[k], SUMBER_SPLIT[k], AKHIR_RUN) for k in KODE_UJI}
    return DataPasar(saham, _bangun("IHSG", MENTAH["IHSG"], SPLIT["IHSG"], AKHIR_RUN), KAL, CFG_LA)


@lru_cache(maxsize=None)
def _split_tertanam(kode: str) -> pd.Series:
    """Split yang tertanam di harga cache (ikut F): seri split tanpa split yang belum disesuaikan sumber."""
    ds = _bangun(kode, SUMBER[kode], SUMBER_SPLIT[kode], AKHIR_RUN)
    return ds.split if ds.split_faktor is None else ds.split_faktor


def _split_potong(kode: str, d: date) -> pd.Series:
    """Metadata split as-of d: split ≤ d (peristiwa yang sudah terjadi; diperiksa §8.4) ditambah split
    sesudah d yang penyesuaiannya tertanam di harga cache. Split DDDD (belum disesuaikan) sebelum
    terjadi tidak tertanam di mana pun, sehingga tidak ikut — sama seperti F(d) data penuh (§8.4)."""
    s = SUMBER_SPLIT[kode]
    return s[(s.index <= ke_ts(d)) | s.index.isin(_split_tertanam(kode).index)]


@lru_cache(maxsize=40)
def pasar_potong(d: date) -> DataPasar:
    """Bar ≤ d saja, akhir run = d; metadata split as-of d (`_split_potong`, lihat docstring modul)."""
    saham = {k: _bangun(k, _sampai(SUMBER[k], d), _split_potong(k, d), d) for k in KODE_UJI}
    return DataPasar(saham, _bangun("IHSG", _sampai(MENTAH["IHSG"], d), SPLIT["IHSG"], d), KAL, CFG_LA)


# ---------------------------------------------------------------------------
# Bentuk pembanding (semua field dataclass; NaN dianggap sama; Decimal dibandingkan persis)
# ---------------------------------------------------------------------------


def _normal(x):
    if isinstance(x, float) and math.isnan(x):
        return "NaN"
    if isinstance(x, Decimal):
        return ("D", str(x))
    if isinstance(x, (tuple, list)):
        return tuple(_normal(y) for y in x)
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return (type(x).__name__,) + tuple((f.name, _normal(getattr(x, f.name))) for f in dataclasses.fields(x))
    return x


def test_bentuk_pembanding_memuat_semua_field_dan_peka():
    """Pembanding benar-benar memuat setiap field Sinyal dan membedakan perubahan sekecil apa pun."""
    cek = (CekAturan("PB1", "close 1.000,00 > SMA50 990,00", True),)
    s = Sinyal(
        tanggal=date(2024, 1, 2), kode="AAAA", strategi="PB", cek=cek, harga_ref=Decimal("1000"),
        harga_maks=Decimal("1010"), stop=Decimal("950"), atr=Decimal("20"), faktor_split=1.0, k_sinyal=0.5,
        target_r=2.0, rata_nilai=math.nan, fraksi_dipakai=False,
    )
    bentuk = _normal(s)
    nama = {f.name for f in dataclasses.fields(Sinyal)}
    assert {b[0] for b in bentuk[1:]} == nama
    assert {"harga_ref", "harga_maks", "stop", "atr", "faktor_split", "k_sinyal", "valid"} <= nama
    assert _normal(s) == _normal(dataclasses.replace(s))  # NaN = NaN
    for ubah in (
        {"harga_maks": Decimal("1009")},
        {"stop": Decimal("951")},
        {"atr": Decimal("20.000001")},
        {"faktor_split": 5.0},
        {"k_sinyal": 0.5 + 1e-15},
        {"valid": False},
        {"rata_nilai": 1.0},
        {"cek": (CekAturan("PB1", "close 1.000,00 > SMA50 990,01", True),)},
    ):
        assert _normal(dataclasses.replace(s, **ubah)) != bentuk, ubah
    # Decimal dengan nilai sama tetapi representasi berbeda tetap dianggap berbeda (hasil hitungan yang
    # sama selalu memberi representasi yang sama).
    assert _normal(Decimal("1.0")) != _normal(Decimal("1.00"))


# ---------------------------------------------------------------------------
# Pemeriksa potong vs penuh
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Hasil:
    evaluasi: dict[tuple[str, str], EvaluasiStrategi]
    eligible: list[str]
    eligibilitas: object
    skor: list
    faktor: dict[str, float]


def hitung_pada(dv: DataView, strategi, beri_skor=modul_skor.beri_skor) -> Hasil:
    bei = AturanBEI(dv.cfg)
    evaluasi = {(k, nama): st.evaluasi(dv, k, bei) for k in KODE_UJI for nama, st in strategi}
    el = nilai_eligibilitas(dv, ATURAN)
    eligible = kode_eligible(el)
    sinyal = hitung_sinyal(dv, eligible, [st for _, st in strategi], bei)
    return Hasil(evaluasi, eligible, el, beri_skor(dv, sinyal, eligible), {k: dv.faktor_split(k) for k in KODE_UJI})


def selisih(d: date, strategi=STRATEGI, beri_skor=modul_skor.beri_skor) -> tuple[list[tuple[str, str]], Hasil]:
    """Daftar (kategori, pesan) ketidakcocokan pada `d`, dan hasil data penuh (untuk statistik)."""
    a = hitung_pada(pasar_penuh().tampilan(d), strategi, beri_skor)
    b = hitung_pada(pasar_potong(d).tampilan(d), strategi, beri_skor)
    salah: list[tuple[str, str]] = []
    for kunci, ev in a.evaluasi.items():
        ea, eb = _normal(ev), _normal(b.evaluasi[kunci])
        if ea != eb:
            salah.append((kunci[1], f"{d} {kunci[0]} {kunci[1]}: penuh={ea!r:.400} potong={eb!r:.400}"))
    if a.eligibilitas != b.eligibilitas:
        salah.append(("ELIGIBILITAS", f"{d}: eligibilitas beda {a.eligible} vs {b.eligible}"))
    sa, sb = _normal(a.skor), _normal(b.skor)
    if sa != sb:
        salah.append(("SKOR", f"{d}: skor beda penuh={sa!r:.400} potong={sb!r:.400}"))
    if a.faktor != b.faktor:
        salah.append(("FAKTOR", f"{d}: faktor split beda {a.faktor} vs {b.faktor}"))
    return salah, a


# ---------------------------------------------------------------------------
# Pemilihan tanggal: acak berseed + tanggal kritis + tanggal bersinyal
# ---------------------------------------------------------------------------

N_TANGGAL_ACAK = 55  # × 4 saham SAHAM = 220 pasangan (saham, tanggal) acak (+ EEEE)

# Posisi HARI kritis (lihat `_bangun_pasar` di test_tanpa_lookahead.py).
POS_KRITIS = sorted({
    # AAAA lonjakan +60% di 300; jendela anomali (min_bar_riwayat 120 bar valid) kedaluwarsa ±420–426
    298, 299, 300, 301, 302, 419, 420, 422, 426,
    # BBBB: anomali tercatat di anomali_ok (200), volume 0 beruntun 400–404, bar bolong 450–455
    199, 200, 201, 399, 400, 403, 404, 405, 449, 450, 452, 455, 456,
    # CCCC: bar rusak (120); split sudah disesuaikan (350) — F(t) = 5 sampai 349, 1 sejak 350
    119, 120, 121, 345, 348, 349, 350, 351, 352,
    # DDDD: split belum disesuaikan sumber (380)
    378, 379, 380, 381, 382,
    # EEEE melantai 350; SMA50/SMA200 baru terdefinisi ±50/200 bar kemudian
    399, 401, 549, 550,
    # IHSG bolong 500–504; bar terakhir data 599; sesudah data berakhir
    500, 503, 504, 505, 598, 599, 600, 601, 610,
})
TANGGAL_KHUSUS = [
    date(2024, 8, 16),  # libur FIKTIF – KHUSUS UJI (bukan hari bursa)
    date(2024, 8, 17),  # Sabtu
    date(2024, 5, 2),  # sehari sesudah bar hari libur CCCC (2024-05-01, dibuang)
    date(2025, 5, 2),  # sesudah data berakhir
    AKHIR_RUN,
]


def _tanggal_bersinyal() -> list[pd.Timestamp]:
    """Tanggal sinyal pertama setiap (strategi PB/BO/MR, saham) pada data penuh, plus setiap tanggal
    sinyal STOP_TERLALU_JAUH — menjamin uji tidak hampa untuk setiap strategi."""
    dp = pasar_penuh()
    bei = AturanBEI(CFG_LA)
    pertama: dict[tuple[str, str], pd.Timestamp] = {}
    tidak_valid: set[pd.Timestamp] = set()
    utama = [(n, s) for n, s in STRATEGI if n in ("PB", "BO", "MR")]
    for t in HARI[200:600]:
        dv = dp.tampilan(t.date())
        for k in KODE_UJI:
            for nama, st in utama:
                sinyal = st.evaluasi(dv, k, bei).sinyal
                if sinyal is not None:
                    pertama.setdefault((nama, k), t)
                    if not sinyal.valid:
                        tidak_valid.add(t)
    return sorted({*pertama.values(), *tidak_valid})


def _tanggal_acak() -> list[pd.Timestamp]:
    rng = np.random.default_rng(SEED + 40)
    rentang = HARI[120:640]  # sebelum indikator panjang terdefinisi s/d sesudah data berakhir
    return sorted({rentang[int(i)] for i in rng.choice(len(rentang), N_TANGGAL_ACAK, replace=False)})


TANGGAL_ACAK = _tanggal_acak()


def _semua_tanggal() -> list[date]:
    semua = {*TANGGAL_ACAK, *(HARI[p] for p in POS_KRITIS), *(ke_ts(d) for d in TANGGAL_KHUSUS),
             *_tanggal_bersinyal()}
    return sorted(t.date() for t in semua)


# ---------------------------------------------------------------------------
# T-LA1b
# ---------------------------------------------------------------------------


def test_t_la1b_sinyal_skor_stop_harga_maks_potong_sama_dengan_penuh():
    tanggal = _semua_tanggal()
    assert len(TANGGAL_ACAK) * len(SAHAM) >= 200  # ≥ 200 pasangan (saham, tanggal) acak berseed
    salah: list[str] = []
    sinyal_per_strategi: Counter = Counter()
    faktor_sinyal: set[float] = set()
    cek_data, tidak_valid, skor_n = 0, 0, 0
    nilai_k_rs: set[float] = set()
    alasan: set[str | None] = set()
    for d in tanggal:
        s, hasil = selisih(d)
        salah += [p for _, p in s]
        for (kode, nama), ev in hasil.evaluasi.items():
            if any(c.kode == "DATA" for c in ev.cek):
                cek_data += 1
            if ev.sinyal is not None:
                sinyal_per_strategi[nama] += 1
                faktor_sinyal.add(ev.sinyal.faktor_split)
                tidak_valid += not ev.sinyal.valid
        skor_n += len(hasil.skor)
        nilai_k_rs.update(x.komponen.k_rs for x in hasil.skor)
        alasan.update(st.alasan for st in hasil.eligibilitas.status.values())
    assert salah == [], "\n".join(salah[:20])

    # Uji tidak hampa: setiap strategi benar-benar menghasilkan sinyal di sampel; jalur F(t) = 5 dan
    # F(t) = 1, STOP_TERLALU_JAUH, bar tanpa data, dan K_rs dengan banyak saham eligible ikut teruji.
    for nama in ("PB", "BO", "MR", "ACAK", "ACAK100"):
        assert sinyal_per_strategi[nama] >= 1, (nama, sinyal_per_strategi)
    assert sinyal_per_strategi["ACAK100"] >= 300, sinyal_per_strategi
    assert {1.0, 5.0} <= faktor_sinyal, faktor_sinyal
    assert tidak_valid >= 1
    assert cek_data >= 20
    assert skor_n >= 200, skor_n
    # K_rs dengan n = 1 (0,5), n ≥ 2 (0 dan 1), dan n ≥ 4 (1/3, 2/3) muncul.
    assert {0.0, 0.5, 1.0, 1 / 3, 2 / 3} <= nilai_k_rs, sorted(nilai_k_rs)
    assert {None, "BAR_HILANG", "ANOMALI", "DATA_BASI", "SPLIT_BELUM_DISESUAIKAN"} <= alasan, alasan


def test_t_la1b_jalur_split_tersesuaikan_benar_benar_teruji():
    """Di sekitar split CCCC: F(t) = 5 sebelum HARI[350], 1 sejak HARI[350]; nominal = tersesuaikan × F."""
    dp = pasar_penuh()
    bei = AturanBEI(CFG_ACAK_PENUH)
    acak = Acak(CFG_ACAK_PENUH)
    for pos, f_harapan in ((POS_SPLIT_CCCC - 1, 5.0), (POS_SPLIT_CCCC, 1.0)):
        d = HARI[pos].date()
        for dv in (dp.tampilan(d), pasar_potong(d).tampilan(d)):
            assert dv.faktor_split("CCCC") == f_harapan
            s = acak.evaluasi(dv, "CCCC", bei).sinyal
            assert s is not None and s.faktor_split == f_harapan
            close = float(dv.baris("CCCC")["close"])
            assert s.harga_ref == ke_desimal_harga(close * f_harapan)


# ---------------------------------------------------------------------------
# Split: rekonstruksi cache sebagaimana diunduh pada t (ruang nominal, tanpa split sesudah t)
# ---------------------------------------------------------------------------

POS_PRA_SPLIT_CCCC_TETAP = [230, 251, 268, 287, 300, 316, 333, 340, 344, 346, 347, 348, 349]


def _pos_sinyal_cccc_pra_split() -> list[int]:
    """Posisi HARI[200:350] dengan sinyal PB/BO/MR CCCC pada data penuh (F(t) = 5)."""
    dp = pasar_penuh()
    bei = AturanBEI(CFG_LA)
    utama = [s for n, s in STRATEGI if n in ("PB", "BO", "MR")]
    return [p for p in range(200, POS_SPLIT_CCCC)
            if any(st.evaluasi(dp.tampilan(HARI[p].date()), "CCCC", bei).sinyal for st in utama)]


def _nominal_tanpa_split(df: pd.DataFrame, faktor: float) -> pd.DataFrame:
    """Cache seperti diunduh sebelum split: harga × F, volume ÷ F (§8.4), kolom lain tetap."""
    hasil = df.copy()
    for k in ("open", "high", "low", "close"):
        hasil[k] = df[k] * faktor
    hasil["volume"] = df["volume"] / faktor
    return hasil


def _split_kosong() -> pd.Series:
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="tanggal"), name="split")


def pasar_asof(d: date, ganti: dict[str, tuple[pd.DataFrame, pd.Series]]) -> DataPasar:
    """Seperti `pasar_potong(d)` (bar ≤ d, akhir = d), tetapi kode di `ganti` memakai (bar, split)
    pengganti apa adanya — mis. cache nominal tanpa split. Kode lain identik dengan uji utama, sehingga
    perbedaan hanya bisa berasal dari kode yang diganti."""
    saham = {}
    for k in KODE_UJI:
        mentah, split = ganti.get(k, (SUMBER[k], _split_potong(k, d)))
        saham[k] = _bangun(k, _sampai(mentah, d), split, d)
    ihsg = _bangun("IHSG", _sampai(MENTAH["IHSG"], d), SPLIT["IHSG"], d)
    return DataPasar(saham, ihsg, KAL, CFG_LA)


def _dekat(a: float, b: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def test_t_la1b_split_cccc_sama_dengan_cache_nominal_asof_t():
    """CCCC sebelum split: data penuh (tersesuaikan, F = 5) = cache nominal yang diunduh pada t (F = 1)."""
    rekon = {"CCCC": (_nominal_tanpa_split(MENTAH["CCCC"], 5.0), _split_kosong())}
    strategi = [*STRATEGI]
    bersinyal = _pos_sinyal_cccc_pra_split()
    assert len(bersinyal) >= 2, bersinyal
    posisi = sorted({*POS_PRA_SPLIT_CCCC_TETAP, *bersinyal})
    jumlah_sinyal: Counter = Counter()
    hari_bar = 0
    for pos in posisi:
        d = HARI[pos].date()
        a = hitung_pada(pasar_penuh().tampilan(d), strategi)
        b = hitung_pada(pasar_asof(d, rekon).tampilan(d), strategi)
        assert a.faktor["CCCC"] == 5.0 and b.faktor["CCCC"] == 1.0, d
        assert a.eligible == b.eligible, d
        for nama, _ in strategi:
            ea, eb = a.evaluasi[("CCCC", nama)], b.evaluasi[("CCCC", nama)]
            assert [(c.kode, c.lolos) for c in ea.cek] == [(c.kode, c.lolos) for c in eb.cek], (d, nama)
            assert (ea.sinyal is None) == (eb.sinyal is None), (d, nama)
            if ea.sinyal is None:
                continue
            sa, sb = ea.sinyal, eb.sinyal
            jumlah_sinyal[nama] += 1
            # Level nominal dibulatkan (rupiah bulat, fraksi belum terverifikasi): identik.
            assert (sa.harga_ref, sa.harga_maks, sa.stop) == (sb.harga_ref, sb.harga_maks, sb.stop), (d, nama)
            assert (sa.valid, sa.fraksi_dipakai, sa.target_r) == (sb.valid, sb.fraksi_dipakai, sb.target_r)
            # Nilai tak dibulatkan: sama sampai presisi float (ATR dihitung atas harga × 5).
            assert abs(sa.atr - sb.atr) <= Decimal("0.00001"), (d, nama, sa.atr, sb.atr)
            assert _dekat(sa.k_sinyal, sb.k_sinyal) and _dekat(sa.rata_nilai, sb.rata_nilai), (d, nama)
        hari_bar += a.evaluasi[("CCCC", "ACAK100")].cek[0].kode != "DATA"
        # Skor semua sinyal eligible (urutan sama di kedua sisi): K_rs persis (peringkat Ret invarian
        # skala), komponen lain sama sampai presisi float.
        assert [(x.kode, x.strategi) for x in a.skor] == [(x.kode, x.strategi) for x in b.skor], d
        for xa, xb in zip(a.skor, b.skor):
            ka, kb = xa.komponen, xb.komponen
            assert ka.k_rs == kb.k_rs, (d, xa.kode)
            for komp in ("k_sinyal", "k_likuid", "k_stop", "skor"):
                assert _dekat(getattr(ka, komp), getattr(kb, komp)), (d, xa.kode, komp)
    assert hari_bar >= len(posisi) - 2
    assert jumlah_sinyal["ACAK100"] == hari_bar  # level teruji di setiap tanggal ber-bar valid
    assert sum(v for k, v in jumlah_sinyal.items() if k not in ("ACAK", "ACAK100")) >= 2, jumlah_sinyal


def test_pemotongan_naif_split_memberi_nominal_salah():
    """Bukti pemilihan pembanding: bar tersesuaikan ≤ t TANPA split > t (keadaan yang tidak pernah ada)
    memberi F(t) = 1 dan harga nominal 1/5 yang sebenarnya. Pembanding seperti ini akan menuduh
    look-ahead palsu; karena itu uji utama memberi data dipotong seri split yang sama."""
    acak = Acak(CFG_ACAK_PENUH)
    bei = AturanBEI(CFG_ACAK_PENUH)
    for pos in (300, 349):
        d = HARI[pos].date()
        split_naif = SPLIT["CCCC"][SPLIT["CCCC"].index <= ke_ts(d)]  # split CCCC (HARI[350]) dibuang
        assert len(split_naif) == 0
        naif = pasar_asof(d, {"CCCC": (MENTAH["CCCC"], split_naif)}).tampilan(d)
        penuh = pasar_penuh().tampilan(d)
        assert naif.faktor_split("CCCC") == 1.0 and penuh.faktor_split("CCCC") == 5.0
        sn, sp = acak.evaluasi(naif, "CCCC", bei).sinyal, acak.evaluasi(penuh, "CCCC", bei).sinyal
        assert sn is not None and sp is not None
        assert sn.cek == sp.cek  # keputusan & teks sama (ruang tersesuaikan) …
        assert abs(sn.harga_ref * 5 - sp.harga_ref) <= Decimal("0.00001")  # … tetapi nominal 1/5
        assert sn.harga_maks != sp.harga_maks and sn.stop != sp.stop


# ---------------------------------------------------------------------------
# DDDD: split BELUM disesuaikan sumber — tidak ikut F(d) (§8.4)
# ---------------------------------------------------------------------------

POS_PRA_SPLIT_DDDD = [250, 270, 290, 310, 330, 350, 360, 370, 375, 379]


def _eligible_dddd_pra_split() -> list[date]:
    dp = pasar_penuh()
    return [HARI[p].date() for p in POS_PRA_SPLIT_DDDD
            if "DDDD" in kode_eligible(nilai_eligibilitas(dp.tampilan(HARI[p].date()), ATURAN))]


def test_prasyarat_dddd_eligible_sebelum_split_tanpa_faktor_split():
    """Prasyarat uji di bawah: sebelum HARI[380] DDDD eligible pada data penuh dan bar mentahnya sudah
    harga nominal (sumber tidak menyesuaikan: close sebelum split ≈ 5 × close sesudahnya), sehingga split
    itu tidak ikut F(t): F(t) = 1 (§8.4), sedangkan split CCCC yang tertanam tetap memberi F = 5."""
    tanggal = _eligible_dddd_pra_split()
    assert len(tanggal) >= 3, tanggal
    dp = pasar_penuh()
    for d in tanggal:
        assert dp.tampilan(d).faktor_split("DDDD") == 1.0
    assert dp.tampilan(HARI[POS_SPLIT_CCCC - 1].date()).faktor_split("CCCC") == 5.0
    sebelum = float(MENTAH["DDDD"]["close"][MENTAH["DDDD"].index < HARI[POS_SPLIT_DDDD]].iloc[-1])
    sesudah = float(MENTAH["DDDD"]["close"][MENTAH["DDDD"].index >= HARI[POS_SPLIT_DDDD]].iloc[0])
    assert 3.0 < sebelum / sesudah < 8.0  # lompatan ≈ ×5: harga sebelum split belum dibagi 5
    # Tanda SPLIT_BELUM_DISESUAIKAN baru berlaku sejak tanggal bukti (as-of), tidak sebelumnya. Pada
    # HARI[380] lompatan −80% juga lonjakan, tetapi SPLIT_BELUM_DISESUAIKAN didahulukan dari ANOMALI (§8.4).
    assert not bool(dp.tampilan(HARI[POS_SPLIT_DDDD - 1].date()).tanda("DDDD")["split_belum_disesuaikan"])
    assert bool(dp.tampilan(HARI[POS_SPLIT_DDDD].date()).tanda("DDDD")["split_belum_disesuaikan"])
    assert dp.tampilan(HARI[POS_SPLIT_DDDD].date()).alasan_data("DDDD") == "SPLIT_BELUM_DISESUAIKAN"


def test_split_belum_disesuaikan_tidak_boleh_memberi_nominal_salah():
    """Data penuh pada t < HARI[380] wajib setara dengan cache DDDD yang diunduh pada t (bar mentah =
    nominal, belum ada split): level nominal sama (split yang tidak tertanam tidak ikut F, §8.4)."""
    acak = Acak(CFG_ACAK_PENUH)
    bei = AturanBEI(CFG_ACAK_PENUH)
    rekon = {"DDDD": (MENTAH["DDDD"], _split_kosong())}
    salah = []
    for d in _eligible_dddd_pra_split():
        penuh = pasar_penuh().tampilan(d)
        asof = pasar_asof(d, rekon).tampilan(d)
        sp, sa = acak.evaluasi(penuh, "DDDD", bei).sinyal, acak.evaluasi(asof, "DDDD", bei).sinyal
        if sp is None or sa is None:
            continue
        if (sp.harga_ref, sp.harga_maks, sp.stop) != (sa.harga_ref, sa.harga_maks, sa.stop):
            salah.append(f"{d}: penuh harga_ref {sp.harga_ref} (F {sp.faktor_split}) vs as-of {sa.harga_ref}")
    assert salah == [], "\n".join(salah)


# ---------------------------------------------------------------------------
# T-LA3: kanari — pemeriksa yang sama WAJIB menangkap look-ahead
# ---------------------------------------------------------------------------


def _ds_mentah(dv: DataView, kode: str) -> DataSaham:
    """Jalan pintas terlarang ke DataSaham utuh (melewati pemotongan DataView) — hanya untuk kanari."""
    return dv.seri(kode)._ds


class KanariIntipBesok(Breakout):
    """BO + syarat 'close besok > close hari ini' yang dibaca dari bar valid sesudah as_of."""

    def evaluasi(self, dv, kode, bei):
        ev = super().evaluasi(dv, kode, bei)
        ind = _ds_mentah(dv, kode).indikator
        pos = int(ind.index.searchsorted(ke_ts(dv.as_of), side="right"))
        besok = float(ind["close"].iloc[pos]) if pos < len(ind) else math.nan
        kini = dv.sebelum(kode, "close", 0)
        cek = CekAturan("INTIP", "close besok > close hari ini", besok > kini)
        return EvaluasiStrategi(kode, self.kode, (*ev.cek, cek), ev.sinyal if cek.lolos else None)


class KanariNormalisasiSampel(Acak):
    """ACAK 100% dengan K_sinyal = close_t / close tertinggi SELURUH seri (normalisasi seluruh sampel)."""

    def evaluasi(self, dv, kode, bei):
        ev = super().evaluasi(dv, kode, bei)
        if ev.sinyal is None:
            return ev
        maks = float(_ds_mentah(dv, kode).valid["close"].max())
        k = dv.sebelum(kode, "close", 0) / maks
        return dataclasses.replace(ev, sinyal=dataclasses.replace(ev.sinyal, k_sinyal=k))


class PembandingKausal(Acak):
    """Membaca DataSaham mentah juga, tetapi hanya bar ≤ as_of → harus LOLOS pemeriksa."""

    def evaluasi(self, dv, kode, bei):
        ev = super().evaluasi(dv, kode, bei)
        if ev.sinyal is None:
            return ev
        valid = _ds_mentah(dv, kode).valid
        sampai = valid["close"].iloc[: int(valid.index.searchsorted(ke_ts(dv.as_of), side="right"))]
        k = dv.sebelum(kode, "close", 0) / float(sampai.max())
        return dataclasses.replace(ev, sinyal=dataclasses.replace(ev.sinyal, k_sinyal=k))


def skor_kanari_ret_terakhir(dv, sinyal, eligible):
    """K_rs dari Ret pada bar TERAKHIR seluruh seri (bukan pada t)."""
    kolom = f"ret_{dv.cfg.skor.rs_lookback}"
    nilai = {k: float(_ds_mentah(dv, k).indikator[kolom].iloc[-1]) for k in eligible}
    nilai = {k: v for k, v in nilai.items() if not math.isnan(v)}
    urut = sorted(nilai, key=nilai.get)
    k_rs = {k: (i / (len(urut) - 1) if len(urut) > 1 else 0.5) for i, k in enumerate(urut)}
    return [modul_skor.SinyalBerskor(s, modul_skor.hitung_skor(s, k_rs.get(s.kode, 0.0), dv.cfg)) for s in sinyal]


TANGGAL_KANARI = [t.date() for t in TANGGAL_ACAK if HARI[220] <= t <= HARI[590]][:30]


def test_kanari_tanggal_cukup():
    assert len(TANGGAL_KANARI) >= 20


@pytest.mark.parametrize(
    "nama, strategi, beri_skor, kategori, min_pct",
    [
        ("intip close besok", [("KANARI", KanariIntipBesok(CFG_LA))], modul_skor.beri_skor, "KANARI", 25),
        ("normalisasi seluruh sampel", [("KANARI", KanariNormalisasiSampel(CFG_ACAK_PENUH))],
         modul_skor.beri_skor, "KANARI", 50),
        ("K_rs dari Ret bar terakhir", [("ACAK100", Acak(CFG_ACAK_PENUH))], skor_kanari_ret_terakhir, "SKOR", 50),
    ],
)
def test_t_la3_kanari_strategi_dan_skor_terdeteksi(nama, strategi, beri_skor, kategori, min_pct):
    terdeteksi, pasangan = 0, 0
    for d in TANGGAL_KANARI:
        salah, hasil = selisih(d, strategi, beri_skor)
        kena = {p.split(":")[0] for kat, p in salah if kat == kategori}
        if kategori == "SKOR":
            pasangan += 1
            terdeteksi += bool(kena)
        else:
            punya_bar = [k for k in KODE_UJI if hasil.evaluasi[(k, "KANARI")].cek[0].kode != "DATA"]
            pasangan += len(punya_bar)
            terdeteksi += len(kena)
    assert pasangan > 0
    assert terdeteksi * 100 >= min_pct * pasangan, (nama, terdeteksi, pasangan)


def test_t_la3_pembanding_kausal_lolos():
    """Membaca DataSaham mentah tidak otomatis dianggap look-ahead: yang dideteksi hanya data > t."""
    strategi = [("KAUSAL", PembandingKausal(CFG_ACAK_PENUH))]
    total = 0
    for d in TANGGAL_KANARI:
        salah, hasil = selisih(d, strategi)
        assert salah == [], salah[:3]
        total += sum(ev.sinyal is not None for ev in hasil.evaluasi.values())
    assert total >= 50


def test_t_la3_kanari_indikator_shift_min1_menggagalkan_t_la1b(monkeypatch):
    """`shift(-1)` yang disuntikkan ke `indikator.sma` merambat ke cek/sinyal/skor dan WAJIB tertangkap."""
    asli = ind.sma
    pasar_penuh.cache_clear()
    pasar_potong.cache_clear()
    try:
        with monkeypatch.context() as mp:
            mp.setattr(ind, "sma", lambda x, n: asli(x, n).shift(-1))
            tanggal = TANGGAL_KANARI[:10]
            kategori: Counter = Counter()
            terdeteksi = 0
            for d in tanggal:
                salah, _ = selisih(d)
                terdeteksi += bool(salah)
                kategori.update(k for k, _ in salah)
            assert terdeteksi == len(tanggal), (terdeteksi, kategori)
            assert {"PB", "BO", "MR"} <= set(kategori), kategori
    finally:
        pasar_penuh.cache_clear()  # jangan wariskan pasar kanari ke uji lain
        pasar_potong.cache_clear()
