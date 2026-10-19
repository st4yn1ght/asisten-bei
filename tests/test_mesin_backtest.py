"""T-BT (mesin backtest), T-LA2/T-LA3 pada tingkat mesin, T-SP bagian mesin, dan jalur per sinyal (§16.7).

SPEC: §4.1, §4.3, §8.4, §8.5, §8.8, §13.2, §14.1, §15.1, §16.2, §16.3, §16.4, §16.7, §16.9, §22.1.

Pasar sintetis (semua kode saham, sektor, dan harga FIKTIF; deterministik, berseed):

- enam saham fiktif dalam tiga sektor fiktif (dua per sektor, sehingga `maks_per_sektor` = 1 bekerja),
  random walk dengan faktor pasar dan gelombang, lonjakan volume pada hari naik (BO), periode turun
  (JANGAN BELI / HATI-HATI), cacat data (bar hilang, volume 0, bar rusak);
- FKSP dengan split 1:5 yang sudah disesuaikan sumber pada `HARI[I_SPLIT]` (F = 5 sebelum split);
- IHSG fiktif tanpa volume.

Ambang config diperkecil lewat `cfg_uji` (periode indikator pendek, `min_saham_breadth` 2, likuiditas
kecil, ACAK berpeluang 25 %) agar PB, BO, MR, dan ACAK benar-benar bersinyal. Mesin dijalankan
(`siapkan_keputusan` + `simulasikan`) untuk setiap run §16.3 (pb, bo, mr, acak, gabungan_aktif,
gabungan_lolos) × mode (edge, realistis; `config.resolusi_konteks`).

Pemeriksaan:

- **T-BT** — buku kas diputar ulang dari daftar transaksi setiap hari (kas close = kas kemarin + hasil
  jual − biaya beli, tidak pernah negatif, beli di open hanya dibiayai kas yang sah menurut
  `dana_jual_langsung_dipakai`); slot dan sektor tidak pernah terlampaui (setelah open dan pada close);
  nilai posisi = lembar × close valid terakhir (§14.1, tanda "≈"); harga isi ≤ harga_maks; posisi hanya
  lahir dari `REKOMENDASI` pada close hari bursa sebelumnya (perekam `seleksi`/`_order` mesin); setiap
  sinyal lain tercatat di `dilewati`; identitas akuntansi §16.9 (dan `periksa_identitas` menolak hasil
  yang dirusak); tanda `ANOMALI` as-of; `nilai_pada` + `MELEWATI_HOLDOUT`; dua run identik.
- **T-LA2** — semua bar sesudah `t` diganti derau acak (termasuk lonjakan > `lonjakan_mencurigakan_pct`,
  bar hilang, volume 0, bar rusak), metadata split diambil dari data asli (§8.8): keputusan ≤ t (status
  pasar, breadth, eligibilitas, sinyal, skor), order, isi, exit, dan ekuitas ≤ t tidak berubah.
  Ditambah pembanding T-LA1 tingkat mesin: data dipotong ≤ t vs data penuh.
- **T-LA3** — `shift(-1)` yang disuntikkan ke `indikator.sma` WAJIB membuat kedua pembanding gagal.
- **T-SP (mesin)** — posisi yang melewati split 1:5: sizing nominal (lembar nominal = lot × 100 pada
  tanggal sinyal), ekuitas kontinu melewati hari split, P&L Rupiah = hitung tangan nominal, dan sama
  dengan dunia setara tanpa split.
- **Jalur per sinyal** — satu unit per sinyal (tanpa `STOP_TERLALU_JAUH`), tanpa batas slot/modal, fee
  minimum diabaikan, `MENUNGGU` untuk sinyal hari terakhir, `TERBUKA` di akhir, R = R posisi portofolio.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from functools import lru_cache

import numpy as np
import pandas as pd
import pytest

from asisten_bei import biaya
from asisten_bei import exit as ex
from asisten_bei import indikator as ind
from asisten_bei.backtest import jendela as jd
from asisten_bei.backtest import mesin
from asisten_bei.config import Config, KonteksRisiko, resolusi_konteks
from asisten_bei.data.provider import ke_ts
from asisten_bei.data.tampilan import DataPasar, DataSaham
from asisten_bei.inputs import Akun, Masukan, SahamUniverse
from asisten_bei.kondisi_pasar import BOLEH_BELI, HATI_HATI, JANGAN_BELI
from asisten_bei.seleksi import REKOMENDASI, HasilSeleksi, KeadaanPortofolio
from asisten_bei.strategi.dasar import daftar_strategi
from asisten_bei.universe import AturanUniverse
from bantu_pasar import HARI, KAL, cfg_uji, data_saham, tgl

D = Decimal

# =====================================================================================================
# Enum §4.3 (salinan untuk memeriksa keluaran mesin)
# =====================================================================================================

ENUM_STATUS_PASAR = {BOLEH_BELI, HATI_HATI, JANGAN_BELI}
ENUM_STATUS_SINYAL = {
    "REKOMENDASI", "SUDAH_DIPEGANG", "DIBLOKIR_PORTOFOLIO", "SLOT_PENUH", "BATAS_ENTRY_HARIAN", "BATAS_SEKTOR",
    "TIDAK_TERJANGKAU", "TIDAK_LIKUID", "KAS_KURANG", "BAYANGAN_GERBANG", "BAYANGAN_PASAR", "STOP_TERLALU_JAUH",
    "DUPLIKAT_STRATEGI",
}
ENUM_STATUS_ISI = {
    "MENUNGGU", "TERISI", "BATAL_GAP", "BATAL_DATA", "BATAL_ARA", "BATAL_SLOT_SAAT_OPEN", "BATAL_SEKTOR_SAAT_OPEN",
    "BATAL_KAS_SAAT_OPEN",
}
ENUM_ALASAN_EXIT = {
    "STOP", "STOP_GAP", "STOP_GAP_HARI_ENTRY", "TRAILING", "TRAILING_GAP", "TARGET", "TARGET_GAP", "KELUAR_SMA",
    "STOP_WAKTU", "PAKSA_JUAL_PASAR", "DATA_BERAKHIR", "TERBUKA",
}
ENUM_TANDA = {"ANOMALI", "TERTUNDA_ARB", "TERTUNDA_TANPA_TRANSAKSI", "SUMBER_MANUAL", "MELEWATI_HOLDOUT"}
BATAL_DI_OPEN = ENUM_STATUS_ISI - {"MENUNGGU", "TERISI"}

# =====================================================================================================
# Pasar sintetis (FIKTIF)
# =====================================================================================================

SEED = 20261019
N_BAR = 360
I_MULAI = 60  # backtest.mulai (pemanasan 60 bar)
I_HOLDOUT = 260  # backtest.holdout_mulai
I_SPLIT = 250  # split 1:5 FKSP (sudah disesuaikan sumber)
AKHIR = tgl(N_BAR - 1)

UBAH = {
    "indikator.atr_periode": 5,
    "likuiditas.periode_hari": 5,
    "likuiditas.nilai_transaksi_min_idr": 100_000_000,
    "likuiditas.maks_hari_volume_nol": 1,
    "universe.min_bar_riwayat": 40,
    "data.validasi.maks_bar_hilang": 4,
    "kondisi_pasar.sma_pendek": 10,
    "kondisi_pasar.sma_panjang": 30,
    "kondisi_pasar.breadth_sma": 10,
    "kondisi_pasar.min_saham_breadth": 2,
    "strategi.pb.sma_cepat": 5,
    "strategi.pb.sma_tren": 10,
    "strategi.pb.sma_panjang": 30,
    "strategi.pb.lookback_slope_sma_tren": 3,
    "strategi.pb.lookback_sentuh_sma_cepat": 3,
    "strategi.pb.lookback_swing_low": 3,
    "strategi.pb.maks_jarak_stop_atr": 2.0,  # sebagian sinyal PB → STOP_TERLALU_JAUH
    "strategi.pb.stop_waktu_hari": 6,
    "strategi.bo.lookback_tertinggi": 10,
    "strategi.bo.lookback_volume": 10,
    "strategi.bo.sma_tren": 10,
    "strategi.bo.sma_panjang": 30,
    "strategi.bo.min_rasio_volume": 1.3,
    "strategi.mr.sma_tren": 30,
    "strategi.mr.rsi_maks": 20,
    "strategi.mr.sma_keluar": 3,
    "strategi.acak.peluang_sinyal_pct": 25.0,
    "strategi.acak.stop_waktu_hari": 6,
    "skor.rs_lookback": 10,
    "backtest.mulai": tgl(I_MULAI).isoformat(),
    "backtest.holdout_mulai": tgl(I_HOLDOUT).isoformat(),
}
CFG = cfg_uji(UBAH)
LOT = int(CFG.pasar.lot_lembar)
SLIP, FB, FJ = biaya.slip(CFG), biaya.fb(CFG), biaya.fj(CFG)

KODE_SPLIT = "FKSP"
# kode fiktif: (sektor fiktif, harga awal tersesuaikan, drift harian)
SAHAM = {
    "FKAA": ("SEKTOR_FIKTIF_A", 1000.0, 0.0012),
    "FKAB": ("SEKTOR_FIKTIF_A", 2500.0, 0.0008),
    "FKBA": ("SEKTOR_FIKTIF_B", 800.0, 0.0015),
    "FKBB": ("SEKTOR_FIKTIF_B", 4000.0, 0.0005),
    "FKCA": ("SEKTOR_FIKTIF_C", 1500.0, 0.0010),
    KODE_SPLIT: ("SEKTOR_FIKTIF_C", 1200.0, 0.0011),  # tersesuaikan; nominal sebelum split ×5
}
KODE = list(SAHAM)
STRATEGI_AKTIF = ("PB", "BO", "MR")
STRATEGI_LOLOS = ("PB", "MR")  # himpunan lolos fiktif untuk run gabungan_lolos
RUN = {
    "pb": mesin.SpesifikasiRun.per_strategi("PB"),
    "bo": mesin.SpesifikasiRun.per_strategi("BO"),
    "mr": mesin.SpesifikasiRun.per_strategi("MR"),
    "acak": mesin.SpesifikasiRun.per_strategi("ACAK"),
    "gabungan_aktif": mesin.SpesifikasiRun.gabungan_aktif(STRATEGI_AKTIF),
    "gabungan_lolos": mesin.SpesifikasiRun.gabungan_lolos(STRATEGI_AKTIF, STRATEGI_LOLOS),
}
MODE = ("edge", "realistis")
PASANGAN_RUN = [(r, m) for r in RUN for m in MODE]


def _ohlc(logc: np.ndarray, rng: np.random.Generator, vol_dasar: float, indeks: bool) -> pd.DataFrame:
    c = np.exp(logc)
    n = len(c)
    o = np.empty(n)
    o[0] = c[0]
    lompat = np.where(rng.random(n - 1) < 0.06, rng.choice([-0.035, 0.035], n - 1), 0.0)
    o[1:] = c[:-1] * np.exp(rng.normal(0, 0.006, n - 1) + lompat)  # gap pembukaan kecil + sesekali ±3,5 %
    hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.008, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.008, n)))
    r = np.diff(np.log(c), prepend=np.log(c[0]))
    v = vol_dasar * np.exp(rng.normal(0, 0.3, n)) * (1 + 4 * np.clip(r, 0, None) / 0.02)  # volume naik saat naik
    df = pd.DataFrame(
        {"open": np.round(o, 2), "high": np.round(hi, 2), "low": np.round(lo, 2), "close": np.round(c, 2),
         "volume": np.full(n, np.nan) if indeks else np.round(v)},
        index=pd.DatetimeIndex(HARI[:n], name="tanggal").as_unit("ns"),
    )
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    df["sumber"] = "yfinance"
    return df


def _bangkitkan() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    rng = np.random.default_rng(SEED)
    faktor = np.cumsum(rng.normal(0.0006, 0.009, N_BAR))
    faktor[150:190] -= np.linspace(0, 0.18, 40)  # periode turun → JANGAN BELI / HATI-HATI
    faktor[190:] -= 0.18
    saham = {}
    for kode, (_, awal, drift) in SAHAM.items():
        idio = np.cumsum(rng.normal(drift - 0.0006, 0.015, N_BAR))
        idio = idio + 0.03 * np.sin(np.arange(N_BAR) / 6.0 + rng.uniform(0, 6))
        saham[kode] = _ohlc(math.log(awal) + 1.2 * faktor + idio, rng, 2e6, False)
    ihsg = _ohlc(math.log(7000) + faktor, rng, 1, True)
    # Cacat data (§8.5): bar hilang, bar tanpa transaksi, bar rusak.
    saham["FKAB"] = saham["FKAB"].drop(saham["FKAB"].index[[120, 121]])
    saham["FKBB"] = saham["FKBB"].drop(saham["FKBB"].index[[230]])
    saham["FKBA"].iloc[140, saham["FKBA"].columns.get_loc("volume")] = 0.0
    rusak = saham["FKCA"].index[175]
    h, lo = saham["FKCA"].loc[rusak, "high"], saham["FKCA"].loc[rusak, "low"]
    saham["FKCA"].loc[rusak, ["high", "low"]] = [lo, h]  # high < low → BAR_RUSAK
    return saham, ihsg


MENTAH, IHSG_MENTAH = _bangkitkan()
SPLIT = {KODE_SPLIT: pd.Series([5.0], index=pd.DatetimeIndex([HARI[I_SPLIT]], name="tanggal"), name="split")}


def _masukan(kode_sektor: dict[str, str]) -> Masukan:
    return Masukan(
        universe=[SahamUniverse(k, "Fiktif " + k, s) for k, s in kode_sektor.items()],
        breadth=list(kode_sektor), riwayat=None, exclude=[], portofolio=[], akun=Akun(D(3_000_000), None, False),
    )


ATURAN = AturanUniverse.backtest(_masukan({k: v[0] for k, v in SAHAM.items()}), CFG)


def bangun_pasar(
    mentah: dict[str, pd.DataFrame] | None = None,
    ihsg: pd.DataFrame | None = None,
    *,
    cfg: Config = CFG,
    akhir: date = AKHIR,
    metadata_split: dict[str, DataSaham] | None = None,
) -> DataPasar:
    """DataPasar dari bar tersesuaikan + seri split asli. `metadata_split`: DataSaham asli yang metadata
    split-nya (`split`, `split_faktor`) dipakai apa adanya (§8.8, T-LA2)."""
    mentah = MENTAH if mentah is None else mentah
    ihsg = IHSG_MENTAH if ihsg is None else ihsg
    saham = {}
    for k, df in mentah.items():
        ds = data_saham(k, df, cfg, akhir, split=SPLIT.get(k))
        if metadata_split is not None:
            asli = metadata_split[k]
            ds = dataclasses.replace(ds, split=asli.split, split_faktor=asli.split_faktor)
        saham[k] = ds
    return DataPasar(saham, data_saham("IHSG", ihsg, cfg, akhir, indeks=True), KAL, cfg)


def keputusan(pasar: DataPasar, akhir: date = AKHIR) -> tuple[mesin.KeputusanHari, ...]:
    _, hari = mesin.hari_simulasi(pasar, pasar.cfg.backtest.mulai, akhir)
    strategi = daftar_strategi(pasar.cfg, termasuk_acak=True)
    return tuple(mesin.siapkan_keputusan(pasar, hari, ATURAN, KODE, strategi))


T_AWAL, HARI_SIM = mesin.hari_simulasi(bangun_pasar(), CFG.backtest.mulai, AKHIR)
D_PENILAIAN = jd.tanggal_penilaian(KAL, CFG)  # (akhir in-sample,)


@lru_cache(maxsize=None)
def pasar_dasar() -> DataPasar:
    return bangun_pasar()


@lru_cache(maxsize=None)
def keputusan_dasar() -> tuple[mesin.KeputusanHari, ...]:
    return keputusan(pasar_dasar())


# =====================================================================================================
# Bentuk pembanding (NaN = NaN; Decimal dibandingkan persis termasuk representasinya)
# =====================================================================================================


def bentuk(x):
    if isinstance(x, float):
        return "NaN" if math.isnan(x) else x
    if isinstance(x, np.generic):
        return bentuk(x.item())
    if isinstance(x, Decimal):
        return ("D", str(x))
    if isinstance(x, pd.Timestamp):
        return ("TS", x.isoformat())
    if isinstance(x, date):
        return x.isoformat()
    if isinstance(x, dict):
        return ("dict",) + tuple(sorted(((bentuk(k), bentuk(v)) for k, v in x.items()), key=repr))
    if isinstance(x, (set, frozenset)):
        return ("set",) + tuple(sorted((bentuk(v) for v in x), key=repr))
    if isinstance(x, (tuple, list)):
        return tuple(bentuk(v) for v in x)
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return (type(x).__name__,) + tuple((f.name, bentuk(getattr(x, f.name))) for f in dataclasses.fields(x))
    return x


def selisih_daftar(nama: str, a: Sequence, b: Sequence, maks: int = 3) -> list[str]:
    """Pesan ketidakcocokan dua daftar (per elemen, pakai `bentuk`)."""
    salah = []
    if len(a) != len(b):
        salah.append(f"{nama}: panjang beda {len(a)} vs {len(b)}")
    for i, (x, y) in enumerate(zip(a, b)):
        bx, by = bentuk(x), bentuk(y)
        if bx != by:
            salah.append(f"{nama}[{i}]: {repr(bx)[:300]} != {repr(by)[:300]}")
            if len(salah) >= maks:
                break
    return salah


def test_bentuk_pembanding_peka():
    p = ex.Posisi("FKAA", "PB", "S", date(2024, 1, 2), date(2024, 1, 3), D(100), D("1000"), D("100150"),
                  D("150"), D(950), D(950), None, D(1), D(1010), D(1), tanda={"ANOMALI"}, meta={"x": math.nan})
    assert bentuk(p) == bentuk(copy.deepcopy(p))
    for ubah in ({"lembar": D(101)}, {"stop": D("950.0")}, {"tanda": set()}, {"meta": {"x": 1.0}},
                 {"keluar": ex.tutup_terbuka(p, date(2024, 1, 4), CFG)}):
        assert bentuk(dataclasses.replace(p, **ubah)) != bentuk(p), ubah


# =====================================================================================================
# Perekam simulasi: hasil + panggilan seleksi (dengan keadaan portofolio) + order yang dibuat mesin
# =====================================================================================================


@dataclasses.dataclass
class Rekaman:
    hasil: mesin.HasilSimulasi
    seleksi: list[tuple[HasilSeleksi, KeadaanPortofolio]]
    order: list[ex.Order]
    konteks: KonteksRisiko
    pasar: DataPasar
    keputusan: tuple[mesin.KeputusanHari, ...]


def simulasi(
    kep: Sequence[mesin.KeputusanHari],
    pasar: DataPasar,
    run: mesin.SpesifikasiRun,
    mode: str,
    *,
    tanggal_penilaian: Sequence[date] = D_PENILAIAN,
    sektor_dari=ATURAN.sektor,
    tanggal_awal: date = T_AWAL,
) -> Rekaman:
    catat_sel: list[tuple[HasilSeleksi, KeadaanPortofolio]] = []
    catat_order: list[ex.Order] = []
    asli_sel, asli_order = mesin.seleksi, mesin._order

    def seleksi_rekam(*a, **kw):
        hs = asli_sel(*a, **kw)
        catat_sel.append((hs, kw["portofolio"]))
        return hs

    def order_rekam(*a, **kw):
        o = asli_order(*a, **kw)
        catat_order.append(o)
        return o

    konteks = resolusi_konteks(pasar.cfg, mode)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mesin, "seleksi", seleksi_rekam)
        mp.setattr(mesin, "_order", order_rekam)
        h = mesin.simulasikan(
            list(kep), pasar, run, mode=mode, konteks=konteks, sektor_dari=sektor_dari, tanggal_awal=tanggal_awal,
            tanggal_penilaian=tanggal_penilaian,
        )
    return Rekaman(h, catat_sel, catat_order, konteks, pasar, tuple(kep))


@lru_cache(maxsize=None)
def semua_run() -> dict[tuple[str, str], Rekaman]:
    kep, p = keputusan_dasar(), pasar_dasar()
    return {(r, m): simulasi(kep, p, RUN[r], m) for r, m in PASANGAN_RUN}


@lru_cache(maxsize=None)
def jalur_dasar(strategi: str) -> tuple[mesin.JalurSinyal, ...]:
    return tuple(mesin.jalur_per_sinyal(keputusan_dasar(), pasar_dasar(), strategi, ATURAN.sektor))


# =====================================================================================================
# Pembantu buku besar
# =====================================================================================================

SAAT_OPEN, SAAT_ENTRY, SAAT_INTRADAY = "open", "entry", "intraday"
ALASAN_OPEN = {ex.STOP_GAP, ex.TRAILING_GAP, ex.TARGET_GAP, ex.DATA_BERAKHIR, *ex.PRIORITAS_JADWAL}
ALASAN_INTRADAY = {ex.STOP, ex.TRAILING, ex.TARGET}


def saat_keluar(p: ex.Posisi) -> str:
    """Kapan exit terjadi dalam hari exit: di pembukaan (langkah 0–4), sesaat setelah isi (hari entry), atau
    sepanjang hari (langkah 5–6)."""
    a = p.keluar.alasan
    if a == ex.STOP_GAP_HARI_ENTRY:
        return SAAT_ENTRY if p.keluar.tanggal == p.tanggal_entry else SAAT_OPEN
    if a in ALASAN_OPEN:
        return SAAT_OPEN
    assert a in ALASAN_INTRADAY, a
    return SAAT_INTRADAY


def keluar_pada(p: ex.Posisi, d: date, saat: str) -> bool:
    return p.keluar is not None and p.keluar.tanggal == d and saat_keluar(p) == saat


def terbuka_pada(p: ex.Posisi, d: date) -> bool:
    """Posisi terbuka pada close `d`."""
    return p.tanggal_entry <= d and (p.keluar is None or p.keluar.tanggal > d)


@dataclasses.dataclass(frozen=True)
class CloseValid:
    """Close valid (tersesuaikan, Decimal seperti mesin) per kode, dari data penuh."""

    tanggal: dict[str, list[date]]
    close: dict[str, list[Decimal]]

    def pada(self, kode: str, d: date) -> Decimal | None:
        i = bisect_right(self.tanggal[kode], d)
        return self.close[kode][i - 1] if i > 0 else None

    def ada(self, kode: str, d: date) -> bool:
        i = bisect_right(self.tanggal[kode], d)
        return i > 0 and self.tanggal[kode][i - 1] == d


_CV: dict[int, tuple[DataPasar, CloseValid]] = {}  # pasar ikut disimpan agar id() tidak dipakai ulang


def close_valid(pasar: DataPasar) -> CloseValid:
    if id(pasar) not in _CV:
        dv = pasar.tampilan(AKHIR)
        tg, cl = {}, {}
        for k in dv.kode():
            v = dv.valid(k)
            tg[k] = [t.date() for t in v.index]
            cl[k] = [biaya.ke_decimal(float(x)) for x in v["close"]]
        _CV[id(pasar)] = (pasar, CloseValid(tg, cl))
    return _CV[id(pasar)][1]


def jumlah(xs) -> Decimal:
    return sum(xs, D(0))


def periksa_buku_harian(rek: Rekaman) -> list[str]:
    """Putar ulang kas, okupansi slot/sektor, dan nilai posisi setiap hari dari daftar transaksi (§16.2)."""
    h, cfg, kons = rek.hasil, rek.pasar.cfg, rek.konteks
    cv = close_valid(rek.pasar)
    rows = h.ekuitas
    salah: list[str] = []
    r0 = rows[0]
    if (r0.tanggal, r0.kas, r0.nilai_posisi, r0.ekuitas, r0.n_posisi) != (h.tanggal_awal, h.modal, 0, h.modal, 0):
        salah.append(f"baris titik awal salah: {r0}")
    if [r.tanggal for r in rows[1:]] != [k.tanggal for k in rek.keputusan]:
        salah.append("baris ekuitas tidak tepat satu per hari simulasi")
    masuk: dict[date, list[ex.Posisi]] = defaultdict(list)
    keluar: dict[date, list[ex.Posisi]] = defaultdict(list)
    for p in h.transaksi:
        masuk[p.tanggal_entry].append(p)
        if p.keluar is not None:
            keluar[p.keluar.tanggal].append(p)
            if p.keluar.tanggal < p.tanggal_entry:
                salah.append(f"{p.kode} exit {p.keluar.tanggal} sebelum entry {p.tanggal_entry}")
    dana = bool(cfg.risiko.dana_jual_langsung_dipakai)
    maks_s = kons.maks_per_sektor
    terbuka: list[ex.Posisi] = []
    for i in range(1, len(rows)):
        r, prev, d = rows[i], rows[i - 1], rows[i].tanggal
        beli = jumlah(p.biaya_beli_idr for p in masuk[d])
        jual_open = jumlah(p.keluar.hasil_jual_idr for p in keluar[d] if saat_keluar(p) == SAAT_OPEN)
        jual = jumlah(p.keluar.hasil_jual_idr for p in keluar[d])
        if prev.kas + jual - beli != r.kas:
            salah.append(f"{d}: kas {r.kas} != kas kemarin {prev.kas} + jual {jual} − beli {beli}")
        if r.kas < 0:
            salah.append(f"{d}: kas negatif {r.kas}")
        boleh = prev.kas + (jual_open if dana else D(0))
        if beli > boleh:
            salah.append(f"{d}: beli di open {beli} > kas tersedia {boleh} (dana_jual_langsung_dipakai={dana})")
        if prev.kas + jual_open - beli < 0:
            salah.append(f"{d}: kas negatif sesaat setelah pembukaan")
        # Okupansi sesudah pembukaan (jual di open sudah keluar, beli baru masuk; STOP_GAP_HARI_ENTRY tidak
        # menempati slot karena langsung keluar).
        sesudah_open = [p for p in terbuka if not keluar_pada(p, d, SAAT_OPEN)]
        sesudah_open += [p for p in masuk[d] if not keluar_pada(p, d, SAAT_ENTRY)]
        if len(sesudah_open) > kons.maks_posisi:
            salah.append(f"{d}: {len(sesudah_open)} posisi sesudah open > maks_posisi {kons.maks_posisi}")
        if len({p.kode for p in sesudah_open}) != len(sesudah_open):
            salah.append(f"{d}: satu saham dipegang dua kali")
        if maks_s is not None:
            for sek, n in Counter(p.sektor for p in sesudah_open).items():
                if n > maks_s:
                    salah.append(f"{d}: sektor {sek} {n} posisi sesudah open > {maks_s}")
        terbuka = [p for p in terbuka + masuk[d] if not (p.keluar and p.keluar.tanggal == d)]
        if len(terbuka) != r.n_posisi or r.n_posisi > kons.maks_posisi:
            salah.append(f"{d}: n_posisi {r.n_posisi} vs rekonstruksi {len(terbuka)} (maks {kons.maks_posisi})")
        if maks_s is not None and any(n > maks_s for n in Counter(p.sektor for p in terbuka).values()):
            salah.append(f"{d}: batas sektor terlampaui pada close")
        nilai = jumlah(p.lembar * cv.pada(p.kode, d) for p in terbuka)
        if nilai != r.nilai_posisi or r.ekuitas != r.kas + r.nilai_posisi:
            salah.append(f"{d}: nilai posisi {r.nilai_posisi} != Σ lembar × close valid terakhir {nilai}")
        if r.perkiraan != any(not cv.ada(p.kode, d) for p in terbuka):
            salah.append(f"{d}: tanda perkiraan (≈) salah")
        if r.status_pasar != rek.keputusan[i - 1].status_pasar or r.status_pasar not in ENUM_STATUS_PASAR:
            salah.append(f"{d}: status pasar baris ekuitas {r.status_pasar}")
    if h.kas_akhir != rows[-1].kas or h.kas_awal != h.modal or h.modal != kons.modal_idr:
        salah.append("kas_awal/kas_akhir/modal tidak cocok dengan baris ekuitas")
    if {p.kode for p in terbuka} != {p.kode for p in h.terbuka}:
        salah.append("posisi terbuka di akhir tidak cocok")
    return salah


def _hari_sesudah(d: date) -> date:
    return KAL.hari_bursa_berikut(d)


def periksa_asal_posisi(rek: Rekaman) -> list[str]:
    """Posisi hanya dari REKOMENDASI pada close hari bursa sebelumnya; setiap sinyal lain di `dilewati`;
    order = sizing seleksi; seleksi menerima keadaan portofolio close hari itu (§13.2, §16.2 langkah 7)."""
    h, run = rek.hasil, rek.hasil.run
    salah: list[str] = []
    kh_per_tanggal = {k.tanggal: k for k in rek.keputusan}
    hari_akhir = rek.keputusan[-1].tanggal
    rows = {r.tanggal: r for r in h.ekuitas}
    reko: Counter = Counter()
    lain: Counter = Counter()
    kandidat_reko = {}
    izin = run.strategi if run.strategi_lolos is None else run.strategi & run.strategi_lolos
    for hs, porto in rek.seleksi:
        d = hs.tanggal
        kh = kh_per_tanggal[d]
        diharap = [s for s in kh.sinyal if s.strategi in run.strategi]
        if sorted(bentuk(k.sinyal) for k in hs.kandidat) != sorted(bentuk(s) for s in diharap):
            salah.append(f"{d}: sinyal yang diseleksi bukan sinyal run {run.nama} hari itu")
        # Keadaan portofolio = close d (§16.2 langkah 7: ekuitas_d dan posisi sesudah langkah 6).
        terbuka_d = sorted((p.kode, p.sektor) for p in h.transaksi
                           if p.tanggal_entry <= d and (p.keluar is None or p.keluar.tanggal > d))
        if (porto.kas_idr, porto.ekuitas_idr) != (rows[d].kas, rows[d].ekuitas):
            salah.append(f"{d}: seleksi memakai kas/ekuitas {porto.kas_idr}/{porto.ekuitas_idr} ≠ close hari itu")
        if sorted((x.kode, x.sektor) for x in porto.posisi) != terbuka_d:
            salah.append(f"{d}: posisi di seleksi ≠ posisi terbuka pada close")
        for x in porto.posisi:
            if x.dijadwalkan_jual:
                p = next(p for p in h.transaksi if p.kode == x.kode and p.tanggal_entry <= d
                         and (p.keluar is None or p.keluar.tanggal > d))
                ok = (p.keluar is None and p.jual_terjadwal is not None) or (
                    p.keluar is not None and saat_keluar(p) == SAAT_OPEN
                    and p.keluar.alasan in (*ex.PRIORITAS_JADWAL, ex.DATA_BERAKHIR, ex.STOP_GAP_HARI_ENTRY))
                if not ok:
                    salah.append(f"{d}: {x.kode} dijadwalkan jual tetapi tidak keluar lewat jadwal")
        for k in hs.kandidat:
            if k.status not in ENUM_STATUS_SINYAL:
                salah.append(f"{d}: status sinyal di luar enum {k.status}")
            kunci = (d, k.kode, k.strategi)
            if k.status == REKOMENDASI:
                if k.strategi not in izin:
                    salah.append(f"{d}: REKOMENDASI strategi {k.strategi} tidak diizinkan run {run.nama}")
                reko[kunci] += 1
                kandidat_reko[kunci] = k
            else:
                lain[(*kunci, k.sinyal.skor, k.status, k.alasan)] += 1
    # Order = REKOMENDASI, dengan lot/harga dari seleksi.
    if Counter((o.tanggal_sinyal, o.kode, o.strategi) for o in rek.order) != reko:
        salah.append("order yang dibuat mesin ≠ daftar REKOMENDASI")
    order_per = {(o.tanggal_sinyal, o.kode, o.strategi): o for o in rek.order}
    for kunci, k in kandidat_reko.items():
        o = order_per.get(kunci)
        if o is not None and (o.lembar, o.harga_maks, o.stop) != (k.sizing.lembar, k.sinyal.sinyal.harga_maks,
                                                                   k.sinyal.sinyal.stop):
            salah.append(f"{kunci}: order tidak sama dengan sizing/sinyal seleksi")
    # Posisi: dari REKOMENDASI, entry tepat hari bursa berikutnya, tidak sebelum backtest.mulai.
    terpakai: Counter = Counter()
    for p in h.transaksi:
        kunci = (p.tanggal_sinyal, p.kode, p.strategi)
        terpakai[kunci] += 1
        if kunci not in reko:
            salah.append(f"posisi {kunci} tanpa REKOMENDASI")
        if p.tanggal_entry != _hari_sesudah(p.tanggal_sinyal):
            salah.append(f"posisi {kunci} masuk {p.tanggal_entry}, bukan hari bursa sesudah sinyal")
        if p.tanggal_sinyal < CFG.backtest.mulai:
            salah.append(f"posisi {kunci} dari sinyal sebelum backtest.mulai")
    # dilewati = sinyal non-REKOMENDASI + REKOMENDASI yang batal di open / menunggu.
    lain_dicatat: Counter = Counter()
    for s in h.dilewati:
        if s.status_sinyal == REKOMENDASI:
            kunci = (s.tanggal, s.kode, s.strategi)
            terpakai[kunci] += 1
            if s.status_isi == mesin.MENUNGGU:
                if s.tanggal != hari_akhir or s.tanggal_isi is not None:
                    salah.append(f"{kunci}: MENUNGGU bukan untuk sinyal hari terakhir")
            elif s.status_isi not in BATAL_DI_OPEN or s.tanggal_isi != _hari_sesudah(s.tanggal):
                salah.append(f"{kunci}: status_isi {s.status_isi} / tanggal_isi {s.tanggal_isi} salah")
        else:
            if s.status_isi is not None or s.tanggal_isi is not None:
                salah.append(f"{s}: sinyal non-REKOMENDASI punya status_isi")
            lain_dicatat[(s.tanggal, s.kode, s.strategi, s.skor, s.status_sinyal, s.alasan)] += 1
    if terpakai != reko:
        beda = (terpakai - reko) + (reko - terpakai)
        salah.append(f"REKOMENDASI tidak tepat sekali menjadi posisi/BATAL/MENUNGGU: {beda}")
    if lain_dicatat != lain:
        salah.append("sinyal non-REKOMENDASI tidak tercatat tepat di dilewati")
    return salah


def periksa_harga(rek: Rekaman) -> list[str]:
    """Harga isi = min(open × (1 + slip), harga_maks) ≤ harga_maks; lembar = lot × 100 × F(sinyal); level di
    ruang tersesuaikan; harga & P&L exit konsisten dengan bar dan label (§4.1, §8.4, §15.1)."""
    h, cfg, p_ = rek.hasil, rek.pasar.cfg, rek.pasar
    salah: list[str] = []
    order_per = {(o.tanggal_sinyal, o.kode, o.strategi): o for o in rek.order}
    for p in h.transaksi:
        kunci = (p.tanggal_sinyal, p.kode, p.strategi)
        o = order_per[kunci]
        f_sinyal = ex._f(p_.tampilan(p.tanggal_sinyal).faktor_split(p.kode))
        b = ex.bar_hari(p_.tampilan(p.tanggal_entry), p.kode)
        hm = o.harga_maks / o.faktor_sinyal
        if o.faktor_sinyal != f_sinyal or p.faktor_entry != b.faktor:
            salah.append(f"{kunci}: faktor split order/posisi salah")
        if (o.lembar != p.meta["lot"] * LOT or p.lembar != o.lembar * f_sinyal
                or o.harga_maks != p.meta["harga_maks_nominal"] or o.stop != p.meta["stop_nominal"]):
            salah.append(f"{kunci}: lembar/harga nominal order tidak sama dengan rencana sizing")
        if not b.valid or b.open > hm:
            salah.append(f"{kunci}: terisi pada bar tidak valid atau open {b.open} > harga_maks {hm}")
        if p.harga_maks != hm or p.harga_beli != min(b.open * (1 + SLIP), hm) or p.harga_beli > p.harga_maks:
            salah.append(f"{kunci}: harga isi {p.harga_beli} ≠ min(open × (1 + slip), harga_maks {hm})")
        if p.stop_awal != o.stop / o.faktor_sinyal or p.stop < p.stop_awal:
            salah.append(f"{kunci}: stop awal/trailing salah")
        if p.risiko_awal_idr != o.lembar * biaya.risiko_lembar(o.harga_maks, o.stop, cfg) or p.risiko_awal_idr <= 0:
            salah.append(f"{kunci}: risiko_awal_idr salah")
        nilai_beli = p.lembar * p.harga_beli
        if p.fee_beli_idr != biaya.fee_beli(nilai_beli, cfg) or p.biaya_beli_idr != nilai_beli + p.fee_beli_idr:
            salah.append(f"{kunci}: biaya beli salah")
        if not p.tanda <= ENUM_TANDA:
            salah.append(f"{kunci}: tanda di luar enum {p.tanda}")
        if p.strategi in ("PB", "ACAK") and p.harga_beli > p.stop_awal:
            # Target = harga isi + target_r × (harga isi − stop), nominal pada hari entry, ke bawah (rupiah bulat:
            # fraksi belum terverifikasi), lalu kembali ke ruang tersesuaikan (§4.1, §8.4, §12.1).
            f = p.faktor_entry
            isi, s = p.harga_beli * f, p.stop_awal * f
            t_nom = (isi + D(repr(float(o.target_r))) * (isi - s)).to_integral_value(rounding=ROUND_FLOOR)
            if p.target != t_nom / f:
                salah.append(f"{kunci}: target {p.target} ≠ hitung tangan {t_nom / f}")
        elif p.target is not None:
            salah.append(f"{kunci}: target ada padahal strategi {p.strategi} / harga isi ≤ stop")
        k = p.keluar
        if k is None:
            continue
        a = k.alasan
        bx = ex.bar_hari(p_.tampilan(k.tanggal), p.kode)
        if a not in ENUM_ALASAN_EXIT - {ex.TERBUKA}:
            salah.append(f"{kunci}: alasan exit {a}")
            continue
        label_bo = a in (ex.TRAILING, ex.TRAILING_GAP)
        if (label_bo or a in (ex.STOP, ex.STOP_GAP)) and label_bo != (p.strategi == "BO"):
            salah.append(f"{kunci}: label stop {a} tidak sesuai strategi {p.strategi} (§4.3)")
        if a in (ex.TARGET, ex.TARGET_GAP) and p.target is None:
            salah.append(f"{kunci}: {a} tanpa target")
        if a == ex.PAKSA_JUAL_PASAR and not cfg.kondisi_pasar.jangan_beli_paksa_jual:
            salah.append(f"{kunci}: PAKSA_JUAL_PASAR padahal jangan_beli_paksa_jual false")
        if a == ex.KELUAR_SMA and (p.strategi != "MR" or KAL.nomor_hari(p.tanggal_entry, p.tanggal_jadwal)
                                   < int(cfg.strategi.mr.min_hari_tahan)):
            salah.append(f"{kunci}: KELUAR_SMA sebelum min_hari_tahan / bukan MR")
        if a == ex.STOP_WAKTU:
            n = ex._param_strategi(cfg, p.strategi).stop_waktu_hari
            if n is None or KAL.nomor_hari(p.tanggal_entry, p.tanggal_jadwal) < int(n) or k.tanggal <= p.tanggal_jadwal:
                salah.append(f"{kunci}: STOP_WAKTU sebelum hari ke-{n}")
        if a == ex.STOP_GAP_HARI_ENTRY:
            harap, syarat = bx.open * (1 - SLIP), bx.open <= p.stop_awal and k.tanggal == p.tanggal_entry
        elif a in (ex.STOP_GAP, ex.TRAILING_GAP):
            harap, syarat = bx.open * (1 - SLIP), bx.open <= p.stop and k.tanggal > p.tanggal_entry
        elif a in ex.PRIORITAS_JADWAL:
            harap, syarat = bx.open * (1 - SLIP), k.tanggal > p.tanggal_entry
        elif a == ex.TARGET_GAP:
            harap, syarat = bx.open, bx.open >= p.target and k.tanggal > p.tanggal_entry
        elif a in (ex.STOP, ex.TRAILING):
            harap, syarat = p.stop * (1 - SLIP), bx.low <= p.stop
        elif a == ex.TARGET:
            harap, syarat = p.target, bx.high >= p.target and bx.low > p.stop
        else:  # DATA_BERAKHIR (tidak terjadi di pasar ini)
            harap, syarat = k.harga, not bx.valid
        if not syarat or k.harga != harap:
            salah.append(f"{kunci}: exit {a} pada {k.tanggal} di {k.harga}, harapan {harap} (syarat {syarat})")
        if bx.valid is False and a != ex.DATA_BERAKHIR:
            salah.append(f"{kunci}: exit {a} pada bar tidak valid")
        if (k.nilai_idr != p.lembar * k.harga or k.fee_jual_idr != biaya.fee_jual(k.nilai_idr, cfg)
                or k.hasil_jual_idr != k.nilai_idr - k.fee_jual_idr or k.pnl_idr != k.hasil_jual_idr - p.biaya_beli_idr
                or k.r != k.pnl_idr / p.risiko_awal_idr or k.harga_nominal != k.harga * bx.faktor):
            salah.append(f"{kunci}: aritmetika exit salah")
    return salah


# =====================================================================================================
# Prasyarat: pasar sintetis benar-benar menguji mesin (uji tidak hampa)
# =====================================================================================================


def test_prasyarat_pasar_sintetis_tidak_hampa():
    kep = keputusan_dasar()
    assert len(kep) == len(HARI_SIM) and kep[0].tanggal == CFG.backtest.mulai
    sinyal = Counter((s.strategi, s.sinyal.valid) for k in kep for s in k.sinyal)
    for st in ("PB", "BO", "MR", "ACAK"):
        assert sinyal[(st, True)] >= 30, sinyal
    assert sinyal[("PB", False)] >= 10  # STOP_TERLALU_JAUH ada
    status = Counter(k.status_pasar for k in kep)
    assert all(status[s] >= 20 for s in ENUM_STATUS_PASAR), status
    # Sinyal FKSP sebelum split memakai F = 5 (harga nominal), sesudahnya F = 1.
    f_fksp = {s.sinyal.faktor_split for k in kep for s in k.sinyal if s.kode == KODE_SPLIT}
    assert f_fksp == {1.0, 5.0}
    # Eligibilitas menolak saham dengan cacat data pada suatu saat; breadth menghitung ≥ 2 saham.
    alasan = Counter(st.alasan for k in kep for st in k.eligibilitas.status.values())
    assert alasan[None] > 0 and alasan["BAR_HILANG"] + alasan["DATA_BASI"] > 0, alasan
    assert min(k.kondisi.breadth.n_terhitung for k in kep) >= 2

    hasil = semua_run()
    for (r, m), rek in hasil.items():
        assert len(rek.hasil.tertutup) >= 10, (r, m)
    alasan_exit = Counter(p.keluar.alasan for rek in hasil.values() for p in rek.hasil.tertutup)
    for a in (ex.STOP, ex.STOP_GAP, ex.TRAILING, ex.TARGET, ex.KELUAR_SMA, ex.STOP_WAKTU):
        assert alasan_exit[a] > 0, alasan_exit
    status_sinyal = Counter(s.status_sinyal for rek in hasil.values() for s in rek.hasil.dilewati)
    for st in ("SUDAH_DIPEGANG", "SLOT_PENUH", "BATAS_SEKTOR", "TIDAK_TERJANGKAU", "BAYANGAN_PASAR",
               "BAYANGAN_GERBANG", "DUPLIKAT_STRATEGI", "STOP_TERLALU_JAUH", "BATAS_ENTRY_HARIAN"):
        assert status_sinyal[st] > 0, (st, status_sinyal)
    status_isi = Counter(s.status_isi for rek in hasil.values() for s in rek.hasil.dilewati)
    assert status_isi["BATAL_GAP"] > 0 and status_isi["MENUNGGU"] > 0, status_isi
    assert any(rek.hasil.terbuka for rek in hasil.values())
    # Batas slot dan sektor benar-benar tercapai.
    assert any(r.n_posisi == rek.konteks.maks_posisi for rek in hasil.values() for r in rek.hasil.ekuitas)


# =====================================================================================================
# T-BT: invarian harian setiap run × mode
# =====================================================================================================


@pytest.mark.parametrize("run, mode", PASANGAN_RUN)
def test_t_bt_buku_kas_slot_sektor_dan_nilai_harian(run, mode):
    salah = periksa_buku_harian(semua_run()[(run, mode)])
    assert salah == [], "\n".join(salah[:20])


@pytest.mark.parametrize("run, mode", PASANGAN_RUN)
def test_t_bt_posisi_hanya_dari_rekomendasi_hari_sebelumnya(run, mode):
    rek = semua_run()[(run, mode)]
    salah = periksa_asal_posisi(rek)
    assert salah == [], "\n".join(salah[:20])
    # Run per strategi hanya memuat strategi itu; gabungan tidak pernah memuat ACAK (§12.4, §16.3).
    strategi = {p.strategi for p in rek.hasil.transaksi} | {s.strategi for s in rek.hasil.dilewati}
    assert strategi <= set(RUN[run].strategi)
    if run == "gabungan_lolos":
        assert {p.strategi for p in rek.hasil.transaksi} <= set(STRATEGI_LOLOS)
        bayangan = {s.strategi for s in rek.hasil.dilewati if s.status_sinyal == "BAYANGAN_GERBANG"}
        assert bayangan == {"BO"}
    if run.startswith("gabungan"):
        assert "ACAK" not in strategi
    else:
        assert not any(s.status_sinyal in ("DUPLIKAT_STRATEGI", "BAYANGAN_GERBANG") for s in rek.hasil.dilewati)


@pytest.mark.parametrize("run, mode", PASANGAN_RUN)
def test_t_bt_harga_isi_maksimal_harga_maks_dan_exit_konsisten(run, mode):
    salah = periksa_harga(semua_run()[(run, mode)])
    assert salah == [], "\n".join(salah[:20])


def test_pemeriksa_invarian_peka_terhadap_hasil_rusak():
    """Pemeriksa di atas tidak hampa: kerusakan kecil pada hasil mesin selalu dilaporkan."""
    rek = semua_run()[("gabungan_aktif", "edge")]
    assert periksa_buku_harian(rek) == periksa_asal_posisi(rek) == periksa_harga(rek) == []

    def rusak(fungsi) -> Rekaman:
        salinan = dataclasses.replace(rek, hasil=copy.deepcopy(rek.hasil))
        fungsi(salinan.hasil)
        return salinan

    def dua_sektor(h: mesin.HasilSimulasi) -> None:
        for r in h.ekuitas[1:]:
            buka = [p for p in h.transaksi if terbuka_pada(p, r.tanggal)]
            if len(buka) >= 2:
                buka[1].sektor = buka[0].sektor  # dua posisi satu sektor (maks_per_sektor 1)
                return
        raise AssertionError("tidak ada hari dengan dua posisi")

    def sinyal_mundur(h: mesin.HasilSimulasi) -> None:
        h.transaksi[0].tanggal_sinyal = KAL.hari_bursa_sebelum(h.transaksi[0].tanggal_sinyal)

    def isi_di_atas_maks(h: mesin.HasilSimulasi) -> None:
        h.transaksi[0].harga_beli = h.transaksi[0].harga_maks + D(1)

    assert periksa_buku_harian(rusak(lambda h: setattr(h.ekuitas[5], "kas", h.ekuitas[5].kas + D(1))))
    assert periksa_buku_harian(rusak(lambda h: setattr(h.transaksi[3], "biaya_beli_idr", D(0))))
    assert periksa_buku_harian(rusak(lambda h: setattr(h.ekuitas[9], "n_posisi", 4)))
    assert any("sektor" in s for s in periksa_buku_harian(rusak(dua_sektor)))
    assert periksa_asal_posisi(rusak(sinyal_mundur))
    assert periksa_asal_posisi(rusak(lambda h: h.dilewati.pop(0)))
    assert periksa_harga(rusak(isi_di_atas_maks))
    assert periksa_harga(rusak(lambda h: setattr(h.tertutup[0].keluar, "harga", h.tertutup[0].keluar.harga + 1)))


@pytest.mark.parametrize("run, mode", PASANGAN_RUN)
def test_t_bt_identitas_akuntansi(run, mode):
    h = semua_run()[(run, mode)].hasil
    mesin.periksa_identitas(h)  # tidak melempar
    pnl = jumlah(p.keluar.pnl_idr for p in h.tertutup)
    terbuka = jumlah(p.nilai() - p.biaya_beli_idr for p in h.terbuka)
    assert h.ekuitas[-1].ekuitas - h.modal == pnl + terbuka  # tepat (Decimal), di bawah toleransi §16.9
    beli = jumlah(p.biaya_beli_idr for p in h.transaksi)
    jual = jumlah(p.keluar.hasil_jual_idr for p in h.tertutup)
    assert h.kas_akhir == h.kas_awal - beli + jual


def test_t_bt_periksa_identitas_menolak_hasil_yang_dirusak():
    h = semua_run()[("gabungan_aktif", "edge")].hasil
    n = len(h.transaksi)
    tol = mesin.TOLERANSI_IDENTITAS_PER_TRANSAKSI * n

    def rusak(fungsi) -> mesin.HasilSimulasi:
        salinan = copy.deepcopy(h)
        fungsi(salinan)
        return salinan

    kasus = {
        "kas_akhir": lambda x: setattr(x, "kas_akhir", x.kas_akhir + tol + D("0.01")),
        "ekuitas_akhir": lambda x: setattr(x.ekuitas[-1], "ekuitas", x.ekuitas[-1].ekuitas - D(1) - tol),
        "pnl_tertutup": lambda x: setattr(x.tertutup[0].keluar, "pnl_idr", x.tertutup[0].keluar.pnl_idr + D(100)),
        "hasil_jual": lambda x: setattr(x.tertutup[0].keluar, "hasil_jual_idr",
                                        x.tertutup[0].keluar.hasil_jual_idr - D(50)),
        "biaya_beli": lambda x: setattr(x.transaksi[0], "biaya_beli_idr", x.transaksi[0].biaya_beli_idr + D(7)),
        "transaksi_hilang": lambda x: x.transaksi.pop(0),
    }
    for nama, fungsi in kasus.items():
        with pytest.raises(mesin.IdentitasAkuntansiGagal):
            mesin.periksa_identitas(rusak(fungsi))
    # Di dalam toleransi Rp 0,01 × jumlah transaksi: lolos.
    mesin.periksa_identitas(rusak(lambda x: setattr(x, "kas_akhir", x.kas_akhir + tol / 2)))
    mesin.periksa_identitas(h)


def test_t_bt_tanpa_transaksi_sebelum_backtest_mulai():
    for rek in semua_run().values():
        h = rek.hasil
        assert h.tanggal_awal == T_AWAL == KAL.hari_bursa_sebelum(CFG.backtest.mulai)
        assert h.ekuitas[1].tanggal == CFG.backtest.mulai
        assert all(p.tanggal_entry > CFG.backtest.mulai for p in h.transaksi)
        assert all(s.tanggal >= CFG.backtest.mulai for s in h.dilewati)


# --- nilai_pada & MELEWATI_HOLDOUT (§16.4) ----------------------------------------------------------


def test_t_bt_nilai_pada_tanggal_penilaian_dan_melewati_holdout():
    (d_is,) = D_PENILAIAN
    assert d_is == tgl(I_HOLDOUT - 1)
    cv = close_valid(pasar_dasar())
    ada = 0
    for (run, mode), rek in semua_run().items():
        h = rek.hasil
        harap_tanda = []
        for p in h.transaksi:
            buka = p.tanggal_entry <= d_is and (p.keluar is None or p.keluar.tanggal > d_is)
            catat = p.meta.get("nilai_pada", {})
            assert (d_is in catat) == buka, (run, mode, p.kode, p.tanggal_entry)
            assert set(catat) <= {d_is}
            if buka:
                ada += 1
                assert catat[d_is] == p.lembar * cv.pada(p.kode, d_is)
                harap_tanda.append(p)
        salinan = copy.deepcopy(h)
        ditandai = jd.tandai_melewati_holdout(salinan, CFG)
        kunci = lambda xs: sorted((p.kode, p.tanggal_entry) for p in xs)  # noqa: E731
        assert kunci(ditandai) == kunci(harap_tanda)
        assert all(ex.MELEWATI_HOLDOUT in p.tanda for p in ditandai)
        assert not any(ex.MELEWATI_HOLDOUT in p.tanda for p in h.transaksi)  # mesin tidak menandai sendiri
    assert ada >= 3  # uji tidak hampa: ada posisi terbuka melewati akhir in-sample


def test_t_bt_tanpa_tanggal_penilaian_tidak_ada_nilai_pada():
    rek = simulasi(keputusan_dasar(), pasar_dasar(), RUN["acak"], "edge", tanggal_penilaian=())
    assert not any("nilai_pada" in p.meta for p in rek.hasil.transaksi)
    # Selain catatan nilai_pada, hasilnya identik dengan run yang mencatat.
    asli = copy.deepcopy(semua_run()[("acak", "edge")].hasil)
    for p in asli.transaksi:
        p.meta.pop("nilai_pada", None)
    assert bentuk(rek.hasil) == bentuk(asli)


# --- Determinisme ------------------------------------------------------------------------------------


def test_t_bt_dua_run_identik():
    """Pasar dibangun ulang dari nol (tanpa cache), keputusan dan simulasi diulang: hasil identik."""
    p2 = bangun_pasar()
    kep2 = keputusan(p2)
    assert selisih_daftar("keputusan", keputusan_dasar(), kep2) == []
    for r, m in (("gabungan_aktif", "edge"), ("acak", "realistis"), ("bo", "edge")):
        a, b = semua_run()[(r, m)], simulasi(kep2, p2, RUN[r], m)
        assert bentuk(a.hasil) == bentuk(b.hasil), (r, m)
        assert repr(bentuk(a.hasil)) == repr(bentuk(b.hasil))
        assert bentuk(a.order) == bentuk(b.order)
    for st in ("PB", "BO"):
        assert bentuk(jalur_dasar(st)) == bentuk(mesin.jalur_per_sinyal(kep2, p2, st, ATURAN.sektor))


# --- dana_jual_langsung_dipakai (§13.2 langkah 8, §16.2) -------------------------------------------

UBAH_KAS_KETAT = {"risiko.alokasi_maks_pct": 60.0, "risiko.risiko_per_transaksi_pct": 5.0}


@lru_cache(maxsize=None)
def run_dana(dana: bool) -> Rekaman:
    cfg = cfg_uji({**UBAH, **UBAH_KAS_KETAT, "risiko.dana_jual_langsung_dipakai": dana})
    return simulasi(keputusan_dasar(), bangun_pasar(cfg=cfg), RUN["acak"], "realistis")


def _beli_dan_jual_open(h: mesin.HasilSimulasi):
    """Per hari: (kas close kemarin, Σ biaya beli, Σ hasil jual di open)."""
    hasil = []
    for prev, r in zip(h.ekuitas, h.ekuitas[1:]):
        d = r.tanggal
        beli = jumlah(p.biaya_beli_idr for p in h.transaksi if p.tanggal_entry == d)
        jual = jumlah(p.keluar.hasil_jual_idr for p in h.tertutup if keluar_pada(p, d, SAAT_OPEN))
        hasil.append((d, prev.kas, beli, jual))
    return hasil


def test_dana_jual_langsung_dipakai_menentukan_pembiayaan_beli_di_open():
    salah_f, salah_t = periksa_buku_harian(run_dana(False)), periksa_buku_harian(run_dana(True))
    assert salah_f == [] and salah_t == [], (salah_f[:5], salah_t[:5])
    # false: beli di open hanya dari kas close kemarin (hasil jual di open yang sama tidak dipakai).
    assert all(beli <= kas for _, kas, beli, _ in _beli_dan_jual_open(run_dana(False).hasil))
    # true: ada hari ketika beli di open hanya mungkin karena hasil jual di open yang sama.
    pakai = [(d, kas, beli, jual) for d, kas, beli, jual in _beli_dan_jual_open(run_dana(True).hasil) if beli > kas]
    assert pakai, "skenario tidak menguji pemakaian hasil jual di open"
    assert all(beli <= kas + jual and jual > 0 for _, kas, beli, jual in pakai)
    # Seleksi false tidak pernah menambahkan estimasi hasil jual terjadwal ke kas tersedia (KAS_KURANG lebih sering).
    kurang = {dana: sum(s.status_sinyal == "KAS_KURANG" for s in run_dana(dana).hasil.dilewati)
              for dana in (False, True)}
    assert kurang[False] > kurang[True], kurang
    assert bentuk(run_dana(False).hasil.transaksi) != bentuk(run_dana(True).hasil.transaksi)


# --- ANOMALI as-of (§8.5) ----------------------------------------------------------------------------


@lru_cache(maxsize=None)
def skenario_anomali():
    """Pilih posisi ACAK (edge) yang ditahan ≥ 4 hari, lalu buat lonjakan +50 % (level bergeser) pada saham
    itu mulai hari bursa sesudah entry `a`. Simulasi diulang pada pasar yang diubah."""
    rek = semua_run()[("acak", "edge")]
    cv = close_valid(pasar_dasar())
    cocok = [p for p in rek.hasil.tertutup if p.kode != KODE_SPLIT
             and KAL.nomor_hari(p.tanggal_entry, p.keluar.tanggal) >= 4
             and cv.ada(p.kode, _hari_sesudah(p.tanggal_entry))]
    p0 = min(cocok, key=lambda p: (p.tanggal_entry, p.kode))
    a = _hari_sesudah(p0.tanggal_entry)
    mentah = dict(MENTAH)
    df = mentah[p0.kode].copy()
    mask = df.index >= ke_ts(a)
    df.loc[mask, ["open", "high", "low", "close"]] = (df.loc[mask, ["open", "high", "low", "close"]] * 1.5).round(2)
    mentah[p0.kode] = df
    p_an = bangun_pasar(mentah)
    kep_an = keputusan(p_an)
    return p0, a, p_an, kep_an, simulasi(kep_an, p_an, RUN["acak"], "edge")


def test_t_bt_tanda_anomali_asof_untuk_posisi_yang_terbuka_saat_anomali():
    p0, a, p_an, kep_an, rek = skenario_anomali()
    x = p0.kode
    t = p_an.tampilan(a).tanda(x)
    assert pd.Timestamp(t["anomali_terakhir"]).date() == a  # anomali tercatat as-of pada a
    assert pd.isna(pasar_dasar().tampilan(a).tanda(x)["anomali_terakhir"])  # data asli tanpa anomali
    h = rek.hasil
    ditandai = {(p.kode, p.tanggal_entry) for p in h.transaksi if ex.ANOMALI in p.tanda}
    harap = {(p.kode, p.tanggal_entry) for p in h.transaksi
             if p.kode == x and p.tanggal_entry <= a and (p.keluar is None or p.keluar.tanggal >= a)}
    assert (x, p0.tanggal_entry) in harap
    assert ditandai == harap
    # Posisi sebelum a tidak berubah (tanpa look-ahead) dan tidak ditandai surut.
    asli = {(p.kode, p.tanggal_entry): p for p in semua_run()[("acak", "edge")].hasil.transaksi}
    for p in h.transaksi:
        if p.keluar is not None and p.keluar.tanggal < a:
            q = asli[(p.kode, p.tanggal_entry)]
            assert bentuk(dataclasses.replace(p, meta={})) == bentuk(dataclasses.replace(q, meta={}))
            assert ex.ANOMALI not in p.tanda
    # Posisi yang terbuka saat a disimulasikan apa adanya: lonjakan di open a → TARGET_GAP di open a.
    pa = next(p for p in h.transaksi if (p.kode, p.tanggal_entry) == (x, p0.tanggal_entry))
    assert pa.keluar.tanggal == a and pa.keluar.alasan == ex.TARGET_GAP
    # Saham tidak eligible untuk sinyal baru selama anomali dalam jendela min_bar_riwayat (as-of).
    jendela = [k for k in kep_an if a <= k.tanggal][: int(CFG.universe.min_bar_riwayat) - 5]
    assert all(k.eligibilitas.alasan(x) == "ANOMALI" for k in jendela)
    assert not any(s.kode == x for k in jendela for s in k.sinyal)
    sebelum = [k for k in kep_an if k.tanggal < a]
    assert not any(k.eligibilitas.alasan(x) == "ANOMALI" for k in sebelum)


# =====================================================================================================
# T-SP bagian mesin: posisi melewati split 1:5 (§8.4, §14.1)
# =====================================================================================================

KODE_SM = "FKSQ"  # fiktif
N_SM = 120
I_SM = 90  # split 1:5 (sudah disesuaikan sumber)
UBAH_SM = {
    **UBAH,
    "kondisi_pasar.min_saham_breadth": 1,
    "strategi.acak.peluang_sinyal_pct": 100.0,
    "strategi.acak.stop_atr": 4.0,
    "strategi.acak.target_r": 6.0,
    "strategi.acak.stop_waktu_hari": 7,
    "backtest.mulai": tgl(45).isoformat(),
    "backtest.holdout_mulai": tgl(110).isoformat(),
}
CFG_SM = cfg_uji(UBAH_SM)


def _harga_setara_sm() -> pd.DataFrame:
    """Harga "setara tanpa split" (Rupiah per 1/5 lembar pasca-split = per lembar pra-split), kelipatan 5,
    naik halus. Nominal pra-split = setara; nominal pasca-split = setara ÷ 5; cache tersesuaikan = setara ÷ 5."""
    i = np.arange(N_SM)
    c = 1000 * 1.003 ** i * (1 + 0.006 * np.sin(i / 2.5))
    c5 = lambda x: np.round(np.asarray(x) / 5) * 5  # noqa: E731
    close = c5(c)
    open_ = c5(np.r_[close[0], close[:-1]] * (1 + 0.002 * np.cos(i / 1.7)))
    high = np.maximum(open_, close) + 10
    low = np.minimum(open_, close) - 10
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": np.full(N_SM, 1_000_000.0)},
        index=pd.DatetimeIndex(HARI[:N_SM], name="tanggal").as_unit("ns"),
    )


SETARA_SM = _harga_setara_sm()


def _ihsg_sm() -> pd.DataFrame:
    c = np.round(7000 * 1.002 ** np.arange(N_SM), 2)
    return pd.DataFrame(
        {"open": c, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": np.nan},
        index=pd.DatetimeIndex(HARI[:N_SM], name="tanggal").as_unit("ns"),
    ).assign(sumber="yfinance")


@lru_cache(maxsize=None)
def pasar_sm(dengan_split: bool) -> tuple[DataPasar, tuple[mesin.KeputusanHari, ...], Rekaman]:
    akhir = tgl(N_SM - 1)
    df = SETARA_SM.copy()
    split = None
    if dengan_split:  # cache tersesuaikan: harga ÷ 5, volume × 5 untuk SELURUH seri; split 5 pada I_SM
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] / 5
        df["volume"] = df["volume"] * 5
        split = pd.Series([5.0], index=pd.DatetimeIndex([HARI[I_SM]], name="tanggal"), name="split")
    df["sumber"] = "yfinance"
    ds = data_saham(KODE_SM, df, CFG_SM, akhir, split=split)
    p = DataPasar({KODE_SM: ds}, data_saham("IHSG", _ihsg_sm(), CFG_SM, akhir, indeks=True), KAL, CFG_SM)
    aturan = AturanUniverse.backtest(_masukan({KODE_SM: "SEKTOR_FIKTIF_S"}), CFG_SM)
    t0, hari = mesin.hari_simulasi(p, CFG_SM.backtest.mulai, akhir)
    kep = tuple(mesin.siapkan_keputusan(p, hari, aturan, [KODE_SM], daftar_strategi(CFG_SM, True, ["ACAK"])))
    rek = simulasi(kep, p, RUN["acak"], "realistis", tanggal_penilaian=(), sektor_dari=aturan.sektor, tanggal_awal=t0)
    return p, kep, rek


def _nominal_sm(kolom: str, i: int) -> Decimal:
    """Harga nominal pada HARI[i]: setara sebelum split, setara ÷ 5 sejak split (kelipatan 5 → bulat)."""
    x = D(int(SETARA_SM[kolom].iloc[i]))
    return x if i < I_SM else x / 5


def test_t_sp_posisi_melewati_split_sizing_nominal_ekuitas_kontinu_pnl_hitung_tangan():
    p_sp, kep, rek = pasar_sm(True)
    h = rek.hasil
    hari_split = tgl(I_SM)
    lintas = [p for p in h.transaksi if p.tanggal_entry < hari_split and p.keluar is not None
              and p.keluar.tanggal > hari_split]
    assert len(lintas) == 1, [(p.tanggal_entry, p.keluar and p.keluar.tanggal) for p in h.transaksi]
    (p,) = lintas
    i_s, i_e, i_x = (HARI.get_loc(ke_ts(x)) for x in (p.tanggal_sinyal, p.tanggal_entry, p.keluar.tanggal))
    dv_s = p_sp.tampilan(p.tanggal_sinyal)
    assert dv_s.faktor_split(KODE_SM) == 5.0 and p_sp.tampilan(p.keluar.tanggal).faktor_split(KODE_SM) == 1.0

    # --- Sizing nominal (§8.4, §14.1), dihitung tangan dengan Decimal.
    sg = next(s for s in kep[[k.tanggal for k in kep].index(p.tanggal_sinyal)].sinyal if s.kode == KODE_SM).sinyal
    P, S = sg.harga_maks, sg.stop  # nominal pada tanggal sinyal
    assert sg.harga_ref == _nominal_sm("close", i_s)  # close nominal (setara), bukan harga cache ÷ 5
    assert (P, S) == (p.meta["harga_maks_nominal"], p.meta["stop_nominal"])
    assert (p.harga_maks, p.stop_awal) == (P / 5, S / 5)  # level disimpan di ruang tersesuaikan
    baris = {r.tanggal: r for r in h.ekuitas}
    e_sinyal, kas_sinyal = baris[p.tanggal_sinyal].ekuitas, baris[p.tanggal_sinyal].kas
    assert p.meta["ekuitas_sinyal"] == e_sinyal and p.meta["status_pasar"] == BOLEH_BELI

    def lot_tangan(P: Decimal, S: Decimal) -> int:
        lantai = lambda x: int(x.to_integral_value(rounding=ROUND_FLOOR))  # noqa: E731
        anggaran = e_sinyal * D(str(CFG_SM.risiko.risiko_per_transaksi_pct)) / 100
        alokasi = e_sinyal / rek.konteks.maks_posisi
        rl = P * (1 + FB) - S * (1 - SLIP) * (1 - FJ)
        lot_risiko = lantai(anggaran / (LOT * rl))
        lot_alokasi = lantai(alokasi / (LOT * P * (1 + FB)))
        lot_kas = lantai(kas_sinyal / (LOT * P * (1 + FB)))
        lot_likuid = lantai(D(str(CFG_SM.likuiditas.maks_pct_order_dari_nilai_harian)) / 100
                            * biaya.ke_decimal(sg.rata_nilai) / (P * LOT))
        return min(lot_risiko, lot_alokasi, lot_kas, lot_likuid)

    lot = lot_tangan(P, S)
    assert lot >= 1 and p.meta["lot"] == lot
    assert lot_tangan(P / 5, S / 5) >= 4 * lot  # sizing dengan harga cache (÷ 5) akan salah besar
    assert p.lembar == lot * LOT * 5  # lembar_adj = lembar nominal (lot × 100) × F(sinyal)

    # --- Entry: harga isi nominal = min(open × (1 + slip), harga_maks); biaya Rupiah.
    open_e = _nominal_sm("open", i_e)
    isi_nom = min(open_e * (1 + SLIP), P)
    assert p.harga_beli * 5 == isi_nom
    biaya_beli = lot * LOT * isi_nom * (1 + FB)
    assert p.biaya_beli_idr == biaya_beli

    # --- Ekuitas kontinu melewati hari split: nilai = lembar nominal × close nominal pada tanggalnya.
    kas = baris[tgl(I_SM - 1)].kas
    assert baris[tgl(I_SM)].kas == kas  # tidak ada arus kas pada hari split
    sebelum = kas + lot * LOT * _nominal_sm("close", I_SM - 1)  # 100 lembar/lot pra-split
    sesudah = kas + lot * LOT * 5 * _nominal_sm("close", I_SM)  # 500 lembar/lot pasca-split
    assert baris[tgl(I_SM - 1)].ekuitas == sebelum and baris[tgl(I_SM)].ekuitas == sesudah
    assert abs(sesudah / sebelum - 1) < D("0.02")  # tidak ada lompatan ×5 atau ÷5

    # --- Exit (STOP_WAKTU di open pasca-split) dan P&L Rupiah hitung tangan nominal.
    k = p.keluar
    assert k.alasan == ex.STOP_WAKTU
    jual_nom = _nominal_sm("open", i_x) * (1 - SLIP)
    assert k.harga_nominal == jual_nom and k.harga == jual_nom  # F(exit) = 1
    hasil_jual = lot * LOT * 5 * jual_nom * (1 - FJ)
    assert k.hasil_jual_idr == hasil_jual
    assert k.pnl_idr == hasil_jual - biaya_beli
    assert k.r == k.pnl_idr / (lot * LOT * (P * (1 + FB) - S * (1 - SLIP) * (1 - FJ)))


def test_t_sp_dunia_setara_tanpa_split_identik_dalam_rupiah():
    """Cache tersesuaikan + split 1:5 vs dunia setara tanpa split (harga ×5 sepanjang seri): sampai exit posisi
    yang melewati split, ekuitas harian, lot, P&L, dan R identik; lembar_adj = 5 × lembar."""
    _, _, rek = pasar_sm(True)
    _, _, rek0 = pasar_sm(False)
    h, h0 = rek.hasil, rek0.hasil
    hari_split = tgl(I_SM)
    (p,) = [p for p in h.transaksi if p.tanggal_entry < hari_split and p.keluar and p.keluar.tanggal > hari_split]
    (p0,) = [q for q in h0.transaksi if q.tanggal_entry == p.tanggal_entry]
    batas = p.keluar.tanggal
    assert [(r.tanggal, r.ekuitas, r.kas, r.nilai_posisi) for r in h.ekuitas if r.tanggal <= batas] == [
        (r.tanggal, r.ekuitas, r.kas, r.nilai_posisi) for r in h0.ekuitas if r.tanggal <= batas]
    assert p.meta["lot"] == p0.meta["lot"] and p.lembar == 5 * p0.lembar
    assert (p.keluar.alasan, p.keluar.tanggal) == (p0.keluar.alasan, p0.keluar.tanggal)
    assert (p.biaya_beli_idr, p.keluar.hasil_jual_idr, p.keluar.pnl_idr, p.keluar.r, p.risiko_awal_idr) == (
        p0.biaya_beli_idr, p0.keluar.hasil_jual_idr, p0.keluar.pnl_idr, p0.keluar.r, p0.risiko_awal_idr)
    sebelum = [(q.tanggal_entry, q.keluar.pnl_idr) for q in h.tertutup if q.keluar.tanggal <= batas]
    sebelum0 = [(q.tanggal_entry, q.keluar.pnl_idr) for q in h0.tertutup if q.keluar.tanggal <= batas]
    assert sebelum == sebelum0 and len(sebelum) >= 3


# =====================================================================================================
# Jalur per sinyal (§16.7)
# =====================================================================================================

STATUS_ISI_UNIT = {ex.TERISI, ex.BATAL_DATA, ex.BATAL_GAP, ex.BATAL_ARA}


@pytest.mark.parametrize("strategi", ["PB", "BO", "MR", "ACAK"])
def test_jalur_satu_unit_per_sinyal_tanpa_stop_terlalu_jauh(strategi):
    kep = keputusan_dasar()
    j = jalur_dasar(strategi)
    harap = [(k.tanggal, s.kode, s.strategi, s.skor, k.status_pasar)
             for k in kep for s in k.sinyal if s.strategi == strategi and s.sinyal.valid]
    assert [(u.tanggal_sinyal, u.kode, u.strategi, u.skor, u.status_pasar) for u in j] == harap
    if strategi == "PB":
        assert any(not s.sinyal.valid for k in kep for s in k.sinyal if s.strategi == "PB")  # dikecualikan
    akhir = kep[-1].tanggal
    for u in j:
        if u.tanggal_sinyal == akhir:
            assert (u.status_isi, u.tanggal_entry, u.alasan_exit) == (mesin.MENUNGGU, None, None)
            continue
        assert u.status_isi in STATUS_ISI_UNIT, u
        if u.status_isi != ex.TERISI:
            assert (u.tanggal_entry, u.alasan_exit, u.r) == (None, None, None)
            continue
        assert u.tanggal_entry == _hari_sesudah(u.tanggal_sinyal)
        assert u.alasan_exit in ENUM_ALASAN_EXIT and u.r is not None and set(u.tanda) <= ENUM_TANDA
        if u.alasan_exit == ex.TERBUKA:
            assert u.tanggal_exit is None and u.hari is None
        else:
            assert u.tanggal_exit >= u.tanggal_entry and u.hari == KAL.nomor_hari(u.tanggal_entry, u.tanggal_exit)
    assert any(u.tanggal_sinyal == akhir for u in j) or not any(
        s.strategi == strategi and s.sinyal.valid for s in kep[-1].sinyal)


def test_jalur_tanpa_batas_slot_modal_status_pasar_dan_terbuka():
    semua = {st: jalur_dasar(st) for st in ("PB", "BO", "MR", "ACAK")}
    # Tanpa batas slot: unit ACAK yang terisi jauh lebih banyak daripada posisi run acak, dan beberapa unit
    # saham yang sama terbuka bersamaan.
    terisi = [u for u in semua["ACAK"] if u.status_isi == ex.TERISI]
    assert len(terisi) > 2 * len(semua_run()[("acak", "edge")].hasil.transaksi)
    tumpang = 0
    for a in terisi:
        for b in terisi:
            if a is not b and a.kode == b.kode and a.tanggal_entry < b.tanggal_entry and (
                    a.tanggal_exit is None or a.tanggal_exit >= b.tanggal_entry):
                tumpang += 1
    assert tumpang > 0
    # Sinyal hari JANGAN BELI tetap disimulasikan (kolom status_pasar untuk statistik §16.7).
    assert any(u.status_pasar == JANGAN_BELI and u.status_isi == ex.TERISI for u in terisi)
    semua_unit = [u for js in semua.values() for u in js]
    assert sum(u.alasan_exit == ex.TERBUKA for u in semua_unit) >= 1
    assert sum(u.status_isi == mesin.MENUNGGU for u in semua_unit) >= 1
    assert sum(u.status_isi == ex.BATAL_GAP for u in semua_unit) >= 1


@pytest.mark.parametrize("strategi", ["PB", "BO", "MR", "ACAK"])
def test_jalur_r_unit_sama_dengan_r_posisi_portofolio(strategi):
    """Fee persentase tanpa minimum → R per lembar tidak bergantung ukuran: setiap posisi run per strategi
    (edge) punya unit dengan entry, exit, alasan, tanda, dan R yang sama persis."""
    unit = {(u.tanggal_sinyal, u.kode): u for u in jalur_dasar(strategi)}
    h = semua_run()[(strategi.lower(), "edge")].hasil
    akhir = keputusan_dasar()[-1].tanggal
    for p in h.transaksi:
        u = unit[(p.tanggal_sinyal, p.kode)]
        assert (u.status_isi, u.tanggal_entry) == (ex.TERISI, p.tanggal_entry)
        assert u.tanda == tuple(t for t in ex.URUTAN_TANDA if t in p.tanda)
        if p.keluar is None:
            assert u.alasan_exit == ex.TERBUKA and u.r == ex.tutup_terbuka(p, akhir, CFG).r
        else:
            assert (u.tanggal_exit, u.alasan_exit, u.r) == (p.keluar.tanggal, p.keluar.alasan, p.keluar.r)


def test_jalur_fee_minimum_dan_pembulatan_fee_diabaikan():
    cfg_fee = cfg_uji({**UBAH, "biaya.fee_minimum_idr": 5000, "biaya.pembulatan_fee": "rupiah_ke_atas"})
    p_fee = bangun_pasar(cfg=cfg_fee)
    kep = keputusan_dasar()
    for st in ("PB", "ACAK"):
        assert bentuk(mesin.jalur_per_sinyal(kep, p_fee, st, ATURAN.sektor)) == bentuk(jalur_dasar(st)), st
    # Pembanding: di simulasi portofolio fee minimum FIKTIF memang berpengaruh.
    h = simulasi(kep, p_fee, RUN["pb"], "realistis").hasil
    assert any(p.fee_beli_idr == 5000 for p in h.transaksi)
    assert bentuk(h.transaksi) != bentuk(semua_run()[("pb", "realistis")].hasil.transaksi)
    cfg_unit = mesin.cfg_per_unit(cfg_fee)
    assert cfg_unit.biaya.fee_minimum_idr == 0 and cfg_unit.biaya.pembulatan_fee is None
    assert cfg_unit.biaya.fee_beli_pct == cfg_fee.biaya.fee_beli_pct


@pytest.mark.xfail(
    strict=True,
    reason="BUG: mesin.jalur_per_sinyal tidak memanggil _tandai, sehingga unit yang terbuka saat anomali tidak "
    "bertanda ANOMALI (dan bar CSV manual tidak bertanda SUMBER_MANUAL), padahal posisi yang sama di "
    "simulasikan bertanda (§8.5, §16.1 kolom tanda_transaksi sinyal_r_*, §16.7)",
)
def test_jalur_unit_yang_terbuka_saat_anomali_bertanda_anomali():
    """§8.5: posisi yang terbuka saat anomali diberi tanda ANOMALI; unit jalur per sinyal disimulasikan
    dengan §15.1 yang sama (§16.7) dan `sinyal_r_*` punya kolom `tanda_transaksi` (§16.1)."""
    p0, a, p_an, kep_an, rek = skenario_anomali()
    j = mesin.jalur_per_sinyal(kep_an, p_an, "ACAK", ATURAN.sektor)
    u = next(u for u in j if (u.tanggal_sinyal, u.kode) == (p0.tanggal_sinyal, p0.kode))
    assert u.status_isi == ex.TERISI and u.tanggal_exit == a
    pos = next(p for p in rek.hasil.transaksi if (p.kode, p.tanggal_entry) == (p0.kode, p0.tanggal_entry))
    assert ex.ANOMALI in pos.tanda
    assert ex.ANOMALI in u.tanda


# =====================================================================================================
# T-LA2: gangguan masa depan (§8.8, §22.1) + pembanding T-LA1 tingkat mesin
# =====================================================================================================

POS_T = [100, 185, I_SPLIT - 1, I_HOLDOUT + 2]  # t: tren, periode turun, sehari sebelum split, holdout
RUN_LA = [("gabungan_aktif", "edge"), ("acak", "realistis"), ("bo", "edge"), ("mr", "realistis")]
LONJAKAN = float(CFG.data.validasi.lonjakan_mencurigakan_pct) / 100


def _derau_setelah(df: pd.DataFrame, t: date, rng: np.random.Generator, indeks: bool) -> pd.DataFrame:
    """Bar ≤ t asli; setiap hari bursa sesudah t sampai AKHIR diganti derau: random walk ±5 %, lonjakan
    ±(>40 %), bar hilang, volume 0, dan bar rusak."""
    lama = df[df.index <= ke_ts(t)]
    idx = HARI[(HARI > ke_ts(t)) & (HARI <= ke_ts(AKHIR))]
    n = len(idx)
    lr = rng.normal(0, 0.05, n)
    lonjak = rng.random(n) < 0.08
    lonjak[[0, n // 2]] = True  # pasti ada lonjakan, termasuk tepat pada t+1
    lr[lonjak] += rng.choice([math.log(1 + LONJAKAN + 0.3), math.log(1 - LONJAKAN - 0.15)], int(lonjak.sum()))
    c = float(lama["close"].iloc[-1]) * np.exp(np.cumsum(lr))
    o = c * np.exp(rng.normal(0, 0.03, n))
    hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.03, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.03, n)))
    rusak = rng.random(n) < 0.03
    hi[rusak], lo[rusak] = lo[rusak] * 0.9, hi[rusak] * 1.1  # high < low
    vol = np.full(n, np.nan) if indeks else np.round(2e6 * np.exp(rng.normal(0, 1.0, n)))
    if not indeks:
        vol[rng.random(n) < 0.05] = 0.0
    baru = pd.DataFrame(
        {"open": np.round(o, 2), "high": np.round(hi, 2), "low": np.round(lo, 2), "close": np.round(c, 2),
         "volume": vol}, index=pd.DatetimeIndex(idx, name="tanggal").as_unit("ns"),
    )
    baru["sumber"] = "yfinance"
    hilang = rng.random(n) < 0.12
    hilang[min(1, n - 1)] = True
    return pd.concat([lama, baru[~hilang]])


def _ds_asli(pasar: DataPasar) -> dict[str, DataSaham]:
    return {k: pasar.tampilan(AKHIR).seri(k)._ds for k in KODE}


def mentah_ganggu(t: date) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    rng = np.random.default_rng(SEED + t.toordinal())
    mentah = {k: _derau_setelah(df, t, rng, False) for k, df in MENTAH.items()}
    return mentah, _derau_setelah(IHSG_MENTAH, t, rng, True)


def pasar_ganggu(t: date, asli: DataPasar | None = None) -> DataPasar:
    """Bar sesudah t diganti derau; metadata split (F(d), klasifikasi split belum disesuaikan) dari data asli."""
    mentah, ihsg = mentah_ganggu(t)
    return bangun_pasar(mentah, ihsg, metadata_split=_ds_asli(asli or pasar_dasar()))


def pasar_potong(t: date) -> DataPasar:
    """Bar ≤ t saja (akhir run = t). Split FKSP tertanam di harga cache → seri split lengkap tetap dipakai
    (metadata split as-of t, §8.8)."""
    mentah = {k: df[df.index <= ke_ts(t)] for k, df in MENTAH.items()}
    return bangun_pasar(mentah, IHSG_MENTAH[IHSG_MENTAH.index <= ke_ts(t)], akhir=t)


@lru_cache(maxsize=None)
def ganggu_dasar(pos: int) -> tuple[DataPasar, tuple[mesin.KeputusanHari, ...]]:
    p = pasar_ganggu(tgl(pos))
    return p, keputusan(p)


def _k(kep: Sequence[mesin.KeputusanHari], t: date) -> int:
    return sum(k.tanggal <= t for k in kep)


def bandingkan_sampai_t(
    t: date,
    kep_a: Sequence[mesin.KeputusanHari],
    pasar_a: DataPasar,
    kep_b: Sequence[mesin.KeputusanHari],
    pasar_b: DataPasar,
    *,
    run_la: Sequence[tuple[str, str]] = RUN_LA,
) -> dict[str, list[str]]:
    """Keputusan ≤ t dan simulasi yang dipotong pada t (semua field hasil + order + panggilan seleksi), serta
    jalur per sinyal yang dipotong pada t. Mengembalikan ketidakcocokan per kategori."""
    salah: dict[str, list[str]] = defaultdict(list)
    ka, kb = kep_a[: _k(kep_a, t)], kep_b[: _k(kep_b, t)]
    for kat, ambil in (("tanggal", lambda k: k.tanggal), ("kondisi", lambda k: k.kondisi),
                       ("breadth", lambda k: k.kondisi.breadth), ("eligibilitas", lambda k: k.eligibilitas),
                       ("sinyal", lambda k: k.sinyal)):
        salah[kat] += selisih_daftar(kat, [ambil(k) for k in ka], [ambil(k) for k in kb])
    if salah["tanggal"]:
        return salah
    for r, m in run_la:
        a, b = simulasi(ka, pasar_a, RUN[r], m), simulasi(kb, pasar_b, RUN[r], m)
        salah["ekuitas"] += selisih_daftar(f"{r}/{m} ekuitas", a.hasil.ekuitas, b.hasil.ekuitas)
        salah["transaksi"] += selisih_daftar(f"{r}/{m} transaksi", a.hasil.transaksi, b.hasil.transaksi)
        salah["dilewati"] += selisih_daftar(f"{r}/{m} dilewati", a.hasil.dilewati, b.hasil.dilewati)
        salah["order"] += selisih_daftar(f"{r}/{m} order", a.order, b.order)
        salah["seleksi"] += selisih_daftar(f"{r}/{m} seleksi", a.seleksi, b.seleksi)
        if bentuk(a.hasil) != bentuk(b.hasil):
            salah["hasil"].append(f"{r}/{m}: HasilSimulasi beda")
    for st in ("PB", "BO", "MR", "ACAK"):
        salah["jalur"] += selisih_daftar(f"jalur {st}", mesin.jalur_per_sinyal(ka, pasar_a, st, ATURAN.sektor),
                                         mesin.jalur_per_sinyal(kb, pasar_b, st, ATURAN.sektor))
    return {k: v for k, v in salah.items() if v}


def bandingkan_run_penuh_sampai_t(t: date, a: Rekaman, b: Rekaman) -> list[str]:
    """Run penuh pada data asli vs data terganggu: semua yang terjadi ≤ t identik."""
    salah: list[str] = []
    nama = f"{a.hasil.run.nama}/{a.hasil.mode}"
    salah += selisih_daftar(f"{nama} ekuitas ≤ t", [r for r in a.hasil.ekuitas if r.tanggal <= t],
                            [r for r in b.hasil.ekuitas if r.tanggal <= t])

    def bagian_entry(p: ex.Posisi):
        meta = {k: v for k, v in p.meta.items() if k != "nilai_pada"}
        meta["nilai_pada"] = {d: v for d, v in p.meta.get("nilai_pada", {}).items() if d <= t}
        return (p.kode, p.strategi, p.sektor, p.tanggal_sinyal, p.tanggal_entry, p.lembar, p.harga_beli,
                p.biaya_beli_idr, p.fee_beli_idr, p.stop_awal, p.target, p.risiko_awal_idr, p.harga_maks,
                p.faktor_entry, p.slippage_beli_idr, meta)

    def exit_sampai_t(p: ex.Posisi):
        return p.keluar if p.keluar is not None and p.keluar.tanggal <= t else None

    pa = [p for p in a.hasil.transaksi if p.tanggal_entry <= t]
    pb = [p for p in b.hasil.transaksi if p.tanggal_entry <= t]
    salah += selisih_daftar(f"{nama} entry ≤ t", [bagian_entry(p) for p in pa], [bagian_entry(p) for p in pb])
    salah += selisih_daftar(f"{nama} exit ≤ t", [exit_sampai_t(p) for p in pa], [exit_sampai_t(p) for p in pb])

    def peristiwa(s: mesin.SinyalDilewati) -> date:
        return s.tanggal_isi if s.status_isi is not None and s.tanggal_isi is not None else (
            s.tanggal if s.status_isi is None else date.max)

    salah += selisih_daftar(f"{nama} dilewati ≤ t", [s for s in a.hasil.dilewati if peristiwa(s) <= t],
                            [s for s in b.hasil.dilewati if peristiwa(s) <= t])
    salah += selisih_daftar(f"{nama} order ≤ t", [o for o in a.order if o.tanggal_sinyal <= t],
                            [o for o in b.order if o.tanggal_sinyal <= t])
    return salah


def test_t_la2_derau_memuat_lonjakan_bar_hilang_dan_bar_rusak():
    """Prasyarat: data terganggu benar-benar memuat lonjakan > ambang, bar hilang, volume 0, dan bar rusak
    sesudah t, sementara bar ≤ t dan metadata split identik dengan data asli."""
    for pos in POS_T:
        t = tgl(pos)
        p, _ = ganggu_dasar(pos)
        dv, dv0 = p.tampilan(AKHIR), pasar_dasar().tampilan(AKHIR)
        anomali = hilang = nol = rusak = 0
        for k in KODE:
            m, m0 = dv.mentah(k), dv0.mentah(k)
            pd.testing.assert_frame_equal(m[m.index <= ke_ts(t)], m0[m0.index <= ke_ts(t)])
            sesudah = HARI[(HARI > ke_ts(t)) & (HARI <= ke_ts(AKHIR))]
            hilang += int((~sesudah.isin(m.index)).sum())
            ms = m[m.index > ke_ts(t)]
            nol += int((ms["volume"] == 0).sum())
            rusak += int((ms["tanda_bar"] == "BAR_RUSAK").sum())
            an = dv.tanda(k)["anomali_terakhir"]
            anomali += int(not pd.isna(an) and pd.Timestamp(an).date() > t)
            assert dv.seri(k)._ds.split_faktor is dv0.seri(k)._ds.split_faktor
            for d in (t, tgl(pos - 30)):
                assert p.tampilan(d).faktor_split(k) == pasar_dasar().tampilan(d).faktor_split(k)
        assert anomali >= 3 and hilang >= 10 and nol >= 3 and rusak >= 1, (pos, anomali, hilang, nol, rusak)
        if t < tgl(I_SPLIT):
            assert p.tampilan(t).faktor_split(KODE_SPLIT) == 5.0  # metadata split asli: F(t) = 5


@pytest.mark.parametrize("pos", POS_T)
def test_t_la2_gangguan_sesudah_t_tidak_mengubah_keputusan_sampai_t(pos):
    t = tgl(pos)
    p_g, kep_g = ganggu_dasar(pos)
    salah = bandingkan_sampai_t(t, keputusan_dasar(), pasar_dasar(), kep_g, p_g)
    assert salah == {}, "\n".join(x for v in salah.values() for x in v[:5])
    # Run penuh pada data terganggu: semua peristiwa ≤ t identik dengan run penuh pada data asli.
    beda_sesudah = 0
    for r, m in RUN_LA:
        a = semua_run()[(r, m)]
        b = simulasi(kep_g, p_g, RUN[r], m)
        s = bandingkan_run_penuh_sampai_t(t, a, b)
        assert s == [], "\n".join(s[:10])
        beda_sesudah += [x.ekuitas for x in a.hasil.ekuitas if x.tanggal > t] != [
            x.ekuitas for x in b.hasil.ekuitas if x.tanggal > t]
    assert beda_sesudah >= 1  # uji tidak hampa: derau memang mengubah hasil sesudah t


def test_t_la2_metadata_split_wajib_dari_data_asli():
    """§8.8: derau yang kebetulan membuat bar hari split ≈ 1/5 close sebelumnya akan mengklasifikasikan split
    FKSP sebagai "belum disesuaikan" bila metadata dihitung dari data terganggu → F(d ≤ t) = 1 dan harga
    nominal sebelum t menjadi 1/5 — selisih yang BUKAN look-ahead mesin, melainkan pembanding yang salah.
    Dengan metadata split dari data asli, keputusan ≤ t identik."""
    t = tgl(I_SPLIT - 1)
    mentah, ihsg = mentah_ganggu(t)
    df = mentah[KODE_SPLIT].copy()
    hs = HARI[I_SPLIT]
    c_t = float(df.loc[df.index <= ke_ts(t), "close"].iloc[-1])
    baris = pd.DataFrame(
        {"open": c_t / 5, "high": c_t / 5 * 1.01, "low": c_t / 5 * 0.99, "close": c_t / 5, "volume": 2e6,
         "sumber": "yfinance"}, index=pd.DatetimeIndex([hs], name="tanggal").as_unit("ns"),
    )
    df = pd.concat([df[df.index != hs], baris]).sort_index()
    mentah[KODE_SPLIT] = df
    naif = bangun_pasar(mentah, ihsg)
    assert naif.tampilan(AKHIR).seri(KODE_SPLIT)._ds.split_faktor is not None  # split dianggap belum disesuaikan
    assert naif.tampilan(t).faktor_split(KODE_SPLIT) == 1.0
    awal = tgl(I_SPLIT - 40)  # cukup 40 hari terakhir sebelum t (bar ≤ t identik dengan data asli)
    kep0 = tuple(k for k in keputusan_dasar() if k.tanggal >= awal)
    hari = [k.tanggal for k in kep0 if k.tanggal <= t]
    strategi = daftar_strategi(CFG, termasuk_acak=True)
    kep_naif = tuple(mesin.siapkan_keputusan(naif, hari, ATURAN, KODE, strategi))
    beda = selisih_daftar("sinyal", [k.sinyal for k in kep0[: len(hari)]], [k.sinyal for k in kep_naif])
    assert beda, "pembanding naif seharusnya berbeda (F = 1 vs 5)"
    benar = bangun_pasar(mentah, ihsg, metadata_split=_ds_asli(pasar_dasar()))
    assert benar.tampilan(t).faktor_split(KODE_SPLIT) == 5.0
    kep_benar = tuple(mesin.siapkan_keputusan(benar, hari, ATURAN, KODE, strategi))
    assert selisih_daftar("keputusan", kep0[: len(hari)], kep_benar) == []


def test_t_la2_tidak_hampa_ada_posisi_dan_order_melintasi_t():
    lintas = order_t = 0
    for pos in POS_T:
        t = tgl(pos)
        for r, m in RUN_LA:
            rek = semua_run()[(r, m)]
            lintas += sum(terbuka_pada(p, t) for p in rek.hasil.transaksi)
            order_t += sum(o.tanggal_sinyal == t for o in rek.order)
    assert lintas >= 4 and order_t >= 1, (lintas, order_t)


@pytest.mark.parametrize("pos", POS_T)
def test_t_la1_mesin_data_dipotong_sama_dengan_data_penuh(pos):
    t = tgl(pos)
    p_t = pasar_potong(t)
    salah = bandingkan_sampai_t(t, keputusan_dasar(), pasar_dasar(), keputusan(p_t, t), p_t)
    assert salah == {}, "\n".join(x for v in salah.values() for x in v[:5])


# =====================================================================================================
# T-LA3: kanari shift(-1) WAJIB membuat pembanding gagal
# =====================================================================================================

POS_KANARI = [100, I_HOLDOUT + 2]


def test_t_la3_kanari_sma_shift_min1_menggagalkan_t_la2_dan_t_la1(monkeypatch):
    asli = ind.sma
    with monkeypatch.context() as mp:
        mp.setattr(ind, "sma", lambda x, n: asli(x, n).shift(-1))
        p_c = bangun_pasar()
        kep_c = keputusan(p_c)
        kategori: Counter = Counter()
        mesin_gagal = 0
        for pos in POS_KANARI:
            t = tgl(pos)
            p_g = pasar_ganggu(t, p_c)
            s_la2 = bandingkan_sampai_t(t, kep_c, p_c, keputusan(p_g), p_g, run_la=[("gabungan_aktif", "edge")])
            p_t = pasar_potong(t)
            s_la1 = bandingkan_sampai_t(t, kep_c, p_c, keputusan(p_t, t), p_t, run_la=[("gabungan_aktif", "edge")])
            assert s_la2, f"T-LA2 tidak mendeteksi kanari pada {t}"
            assert s_la1, f"T-LA1 tidak mendeteksi kanari pada {t}"
            kategori.update(s_la2.keys())
            kategori.update(s_la1.keys())
            mesin_gagal += bool({"ekuitas", "transaksi", "dilewati", "order", "seleksi", "hasil"} & set(s_la1))
        assert {"kondisi", "sinyal"} <= set(kategori), kategori
        assert mesin_gagal >= 1, kategori  # kebocoran merambat sampai keluaran mesin
    # Sesudah kanari dicabut, pembanding yang sama lolos lagi (kanari tidak bocor ke cache uji lain).
    assert ind.sma is asli
    t = tgl(POS_KANARI[0])
    p_g, kep_g = ganggu_dasar(POS_KANARI[0])
    assert bandingkan_sampai_t(t, keputusan_dasar(), pasar_dasar(), kep_g, p_g, run_la=[]) == {}
