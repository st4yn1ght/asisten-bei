"""Pembangkit fixture end-to-end offline (SPEC §22.2). FIKTIF – KHUSUS UJI: semua kode, nama, sektor, harga, dan
tanggal libur di sini buatan dan BUKAN data pasar atau aturan BEI.

    .venv/bin/python tests/fixtures/buat_fixture.py            # tulis ulang tests/fixtures/e2e/
    .venv/bin/python tests/fixtures/buat_fixture.py --periksa  # tulis lalu cetak sinyal pada tanggal kunci

Menulis `tests/fixtures/e2e/`:

- `pasar_sintetis/yfinance/<KODE>.parquet` + `.meta.json` (format cache §8.3);
- `config_uji.yaml` (config rilis + path fixture, holdout & gerbang longgar, Telegram nonaktif) dan
  `config_uji_ujibo.yaml` (sama, tetapi `file_portfolio` → `input_uji/portfolio_ujibo.csv`);
- `input_uji/` (universe, breadth, holidays, akun, portfolio kosong, portfolio_ujibo).

Deterministik: seri UJI* dibangun dari tren halus + pola eksplisit (tanpa acak); 40 saham breadth memakai RNG
berseed tetap. Beberapa seri ditransformasi afin (× k + b) agar angka kunci tepat (mis. UJIMAHAL2 harga_maks
10.500 dan stop 10.290): transformasi afin mempertahankan SMA, RSI, CLV, dan aturan strategi, sedangkan ATR
ikut terskala k.
"""

from __future__ import annotations

import copy
import math
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

AKAR_PROYEK = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AKAR_PROYEK / "src"))

FOLDER = Path(__file__).resolve().parent / "e2e"
LABEL = "FIKTIF – KHUSUS UJI"

MULAI = date(2023, 1, 2)
T1 = date(2025, 9, 10)  # sinyal PB UJIPB dkk., status BOLEH BELI
T_BO = date(2025, 9, 3)  # breakout UJIBO (portfolio_ujibo: beli T_BO+1)
T_HATI = date(2025, 10, 21)  # status HATI-HATI
T_TURUN = date(2025, 11, 19)  # status JANGAN BELI; sinyal MR UJIMR (daftar pantau)
T_KOSONG = date(2025, 12, 1)  # sebagian besar saham tanpa bar → BELUM_TERSEDIA
LIBUR = {
    date(2023, 12, 25): f"Libur {LABEL}",
    date(2024, 12, 25): f"Libur {LABEL}",
    date(2025, 12, 25): f"Libur {LABEL}",
    date(2026, 1, 1): f"Libur {LABEL}",
}
SEED_BREADTH = 220910
N_BREADTH = 40
N_BREADTH_BARU = 5  # "baru melantai" di tengah periode
MELANTAI = date(2025, 4, 1)
DIAMBIL_PADA = datetime.fromisoformat("2025-12-01T20:00:00+07:00")
KAS = 3_000_000

SEKTOR = {
    "UJIPB": "Sektor Fiktif A", "UJISEKTOR": "Sektor Fiktif A", "UJIBO": "Sektor Fiktif B", "UJIMR": "Sektor Fiktif C",
    "UJIMAHAL": "Sektor Fiktif D", "UJIMAHAL2": "Sektor Fiktif E", "UJIGAP": "Sektor Fiktif F",
    "UJIBOLONG": "Sektor Fiktif G", "UJIANOMALI": "Sektor Fiktif H", "UJISPLIT": "Sektor Fiktif I",
}


def hari_bursa(akhir: date = T_KOSONG) -> list[date]:
    hasil, d = [], MULAI
    while d <= akhir:
        if d.weekday() < 5 and d not in LIBUR:
            hasil.append(d)
        d += timedelta(days=1)
    return hasil


HARI = hari_bursa()
IDX = {d: i for i, d in enumerate(HARI)}


def geser(d: date, n: int) -> date:
    """Hari bursa ke-n sesudah (n > 0) atau sebelum (n < 0) `d`."""
    return HARI[IDX[d] + n]


# ---------------------------------------------------------------------------
# Pembangun seri
# ---------------------------------------------------------------------------


@dataclass
class Seri:
    """Seri harian dari return close per hari + bentuk bar. open = close kemarin × (1 + gap); high = max(o, c) ×
    (1 + atas); low = min(o, c) × (1 − bawah)."""

    awal: float
    rentang: float = 0.006
    volume: float = 3_000_000.0
    ret: np.ndarray = field(default=None)
    gap: dict = field(default_factory=dict)
    atas: dict = field(default_factory=dict)
    bawah: dict = field(default_factory=dict)
    vol: dict = field(default_factory=dict)
    mulai: date = MULAI
    akhir: date = T_KOSONG
    hilang: set = field(default_factory=set)
    split: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.ret is None:
            self.ret = np.zeros(len(HARI))

    def tren(self, dari: date, sampai: date, laju: float) -> "Seri":
        """Return harian `laju` untuk hari (dari, sampai] — inklusif sampai."""
        self.ret[IDX[dari] + 1: IDX[sampai] + 1] = laju
        return self

    def hari(self, d: date, ret: float, gap: float = 0.0, atas: float | None = None, bawah: float | None = None,
             vol: float | None = None) -> "Seri":
        i = IDX[d]
        self.ret[i] = ret
        if gap:
            self.gap[i] = gap
        if atas is not None:
            self.atas[i] = atas
        if bawah is not None:
            self.bawah[i] = bawah
        if vol is not None:
            self.vol[i] = vol
        return self

    def bingkai(self, k: float = 1.0, b: float = 0.0) -> pd.DataFrame:
        n = len(HARI)
        c = self.awal * np.cumprod(1.0 + self.ret)
        o = np.empty(n)
        o[0] = c[0]
        o[1:] = c[:-1]
        for i, g in self.gap.items():
            o[i] = c[i - 1] * (1.0 + g)
        h = np.maximum(o, c) * (1.0 + np.array([self.atas.get(i, self.rentang) for i in range(n)]))
        lo = np.minimum(o, c) * (1.0 - np.array([self.bawah.get(i, self.rentang) for i in range(n)]))
        v = np.array([self.vol.get(i, self.volume) for i in range(n)])
        df = pd.DataFrame({"open": o * k + b, "high": h * k + b, "low": lo * k + b, "close": c * k + b, "volume": v},
                          index=pd.DatetimeIndex([pd.Timestamp(d) for d in HARI], name="tanggal").as_unit("ns"))
        df = df[(df.index >= pd.Timestamp(self.mulai)) & (df.index <= pd.Timestamp(self.akhir))]
        if self.hilang:
            df = df.drop([pd.Timestamp(d) for d in self.hilang if pd.Timestamp(d) in df.index])
        df = df.round(6)
        df["sumber"] = "yfinance"
        df["split"] = 0.0
        for d, r in self.split.items():
            df.loc[pd.Timestamp(d), "split"] = r
        return df


def ihsg() -> pd.DataFrame:
    """Naik (memuat T1), transisi, lalu turun (memuat T_TURUN); tanpa volume."""
    s = Seri(6000.0, rentang=0.004)
    s.tren(MULAI, geser(T1, 12), 0.0009)
    s.tren(geser(T1, 12), T_HATI, -0.0012)
    s.tren(T_HATI, T_KOSONG, -0.0045)
    gelombang = 0.0025 * np.sin(np.arange(len(HARI)) / 3.0)
    s.ret = s.ret + gelombang * (np.arange(len(HARI)) > 0)
    df = s.bingkai()
    df["volume"] = np.nan
    return df


def breadth() -> dict[str, pd.DataFrame]:
    """40 saham breadth: tren IHSG + derau berseed; 5 baru melantai pada `MELANTAI`."""
    rng = np.random.default_rng(SEED_BREADTH)
    dasar = ihsg()["close"].to_numpy()
    log_dasar = np.log(dasar / dasar[0])
    hasil = {}
    for i in range(N_BREADTH):
        kode = f"BRD{i + 1:02d}"
        beta = 0.8 + 0.4 * rng.random()
        derau = np.cumsum(rng.normal(0, 0.006, len(HARI)))
        c = (500 + 4500 * rng.random()) * np.exp(beta * log_dasar + derau)
        s = Seri(1.0)
        s.ret = np.r_[0.0, c[1:] / c[:-1] - 1.0]
        s.awal = float(c[0])
        if i >= N_BREADTH - N_BREADTH_BARU:
            s.mulai = MELANTAI
        hasil[kode] = s.bingkai()
        hasil[kode]["volume"] = rng.integers(2_000_000, 6_000_000, len(hasil[kode])).astype(float)
    return hasil


# ---------------------------------------------------------------------------
# Pola
# ---------------------------------------------------------------------------


def pola_pb(s: Seri, t: date, turun: float = 0.011, hari_turun: int = 4, pantul: float = 0.024) -> Seri:
    """Pullback `hari_turun` hari (low menyentuh SMA20), lalu pantulan pada t: close > high kemarin, close > open."""
    for j in range(hari_turun, 0, -1):
        s.hari(geser(t, -j), -turun)
    s.hari(t, pantul, atas=0.002, bawah=0.002)
    return s


def pola_bo(s: Seri, t: date, datar: int = 25, naik: float = 0.018, rasio_vol: float = 2.2) -> Seri:
    """Konsolidasi datar `datar` hari, lalu breakout bervolume pada t (close dekat high)."""
    for j in range(datar, 0, -1):
        s.hari(geser(t, -j), 0.002 if j % 2 else -0.002)
    s.hari(t, naik, atas=0.001, bawah=0.004, vol=s.volume * rasio_vol)
    return s


def pola_mr(s: Seri, t: date, turun1: float = -0.012, turun2: float = -0.03) -> Seri:
    """Dua hari turun (t−1, t): RSI(2) < 10 pada t, close tetap di atas SMA200."""
    s.hari(geser(t, -1), turun1)
    s.hari(t, turun2)
    return s


# ---------------------------------------------------------------------------
# Saham uji
# ---------------------------------------------------------------------------


def saham_uji() -> dict[str, tuple[Seri, float, float]]:
    """kode → (seri, k, b) untuk transformasi afin."""
    hasil: dict[str, tuple[Seri, float, float]] = {}

    def naik(awal: float, laju: float = 0.0025, rentang: float = 0.006, volume: float = 3e6) -> Seri:
        return Seri(awal, rentang=rentang, volume=volume).tren(MULAI, T_KOSONG, laju)

    # UJIPB: PB in-sample & holdout awal, PB pada T1 lalu naik ke target.
    s = naik(800.0, laju=0.0012, volume=4e7)
    pola_pb(s, date(2024, 5, 15))
    pola_pb(s, date(2025, 3, 12))
    pola_pb(s, T1)
    s.hari(geser(T1, 1), 0.002, gap=-0.002)
    s.hari(geser(T1, 2), 0.004)
    s.hari(geser(T1, 3), 0.05, atas=0.01)
    hasil["UJIPB"] = (s, 1.0, 0.0)

    s = naik(1500.0, laju=0.0012, volume=6e6)
    pola_pb(s, T1, pantul=0.02)
    hasil["UJISEKTOR"] = (s, 1.0, 0.0)

    s = naik(600.0, laju=0.0012, volume=3e7)
    pola_pb(s, T1)
    s.hari(geser(T1, 1), 0.04, gap=0.04)
    hasil["UJIGAP"] = (s, 1.0, 0.0)

    s = naik(4000.0, laju=0.0012, rentang=0.012, volume=8e6)
    pola_pb(s, T1, turun=0.012, pantul=0.035)
    hasil["UJIMAHAL"] = (s, 1.0, 0.0)

    s = naik(1000.0, laju=0.0012, volume=5e6)
    pola_pb(s, T1)
    hasil["UJIMAHAL2"] = (s, *atur_mahal2(s))

    s = naik(900.0, laju=0.0012, volume=1e7)
    pola_bo(s, date(2024, 6, 12))
    s.hari(date(2024, 7, 15), -0.08)
    pola_bo(s, T_BO)
    for j in range(1, 6):
        s.hari(geser(T_BO, j), 0.006)
    hasil["UJIBO"] = (s, 1.0, 0.0)

    s = naik(1200.0, laju=0.0015, volume=1e7)
    pola_mr(s, date(2024, 7, 10))
    s.hari(date(2024, 7, 11), 0.004)
    s.hari(date(2024, 7, 12), 0.003)
    s.hari(date(2024, 7, 15), 0.02)
    pola_mr(s, geser(T1, 1))
    s.hari(geser(T1, 2), 0.004)
    s.hari(geser(T1, 3), -0.002)
    s.hari(geser(T1, 4), 0.025)
    pola_mr(s, T_TURUN)
    hasil["UJIMR"] = (s, 1.0, 0.0)

    s = naik(700.0, laju=0.0012, volume=2e7)
    s.hilang = {geser(T1, -12), geser(T1, -11), geser(T1, -10)}
    hasil["UJIBOLONG"] = (s, 1.0, 0.0)

    s = naik(1100.0, laju=0.0012, volume=2e7)
    pola_bo(s, date(2024, 9, 18))
    s.hari(date(2024, 10, 21), -0.09)
    pola_bo(s, date(2025, 4, 16))
    s.hari(date(2025, 5, 20), -0.09)
    s.hari(geser(T1, 3), 0.6)  # lonjakan tanpa split sesudah T1
    hasil["UJIANOMALI"] = (s, 1.0, 0.0)

    s = naik(1000.0, laju=0.0012, volume=2e7)
    pola_pb(s, date(2024, 4, 17))
    pola_pb(s, date(2024, 8, 14))
    s.split = {date(2024, 10, 1): 5.0}
    hasil["UJISPLIT"] = (s, 1.0, 0.0)
    return hasil


HARGA_MAKS_MAHAL2 = 10500  # SPEC §22.2: harga_maks tepat 10.500, jarak stop 2 % (stop 10.290)
STOP_MAHAL2 = 10290


def _cfg():
    from asisten_bei.config import baca_yaml, validasi_dict

    dasar = baca_yaml(AKAR_PROYEK / "tests" / "fixtures" / "config_rilis.yaml")
    return validasi_dict(_dengan(dasar, UBAH_CONFIG))


def atur_mahal2(s: Seri) -> tuple[float, float]:
    """(k, b) agar sinyal PB UJIMAHAL2 pada T1 tepat: close + 0,5 ATR = 10.500,5 (→ harga_maks 10.500) dan
    LLV5 − 0,2 ATR = 10.289,5 (→ stop 10.290). Transformasi afin: SMA/LLV → k·x + b, ATR → k·ATR."""
    from asisten_bei.indikator import hitung_indikator, k_llv_low

    cfg = _cfg()
    df = s.bingkai()
    ind = hitung_indikator(df.loc[:, ["open", "high", "low", "close", "volume"]], cfg)
    r = ind.loc[pd.Timestamp(T1)]
    pb = cfg.strategi.pb
    atas = r["close"] + float(cfg.entry.gap_maks_atr) * r["atr"]
    bawah = r[k_llv_low(pb.lookback_swing_low)] - float(pb.penyangga_stop_atr) * r["atr"]
    k = (HARGA_MAKS_MAHAL2 - STOP_MAHAL2 + 1.0) / (atas - bawah)
    b = HARGA_MAKS_MAHAL2 + 0.5 - k * atas
    return float(k), float(b)


# ---------------------------------------------------------------------------
# Config & input
# ---------------------------------------------------------------------------

UBAH_CONFIG = {
    "akun.file_akun": "input_uji/akun.yaml",
    "pasar.file_libur": "input_uji/holidays.csv",
    "data.folder_cache": "pasar_sintetis",
    "data.folder_manual": "manual_uji",
    "data.file_anomali_ok": "input_uji/anomali_ok.csv",
    "universe.file": "input_uji/universe.csv",
    "universe.file_riwayat": "input_uji/universe_history.csv",
    "universe.file_exclude": "input_uji/exclude.csv",
    "universe.file_portfolio": "input_uji/portfolio.csv",
    "kondisi_pasar.file_breadth_universe": "input_uji/breadth_universe.csv",
    "notifikasi.telegram.aktif": False,
    "backtest.mulai": "2024-01-02",
    "backtest.holdout_mulai": "2025-01-02",
    "gerbang.min_transaksi_holdout": 1,
    "gerbang.min_pf_r_holdout": 0.0,
    "gerbang.min_ekspektansi_r_holdout": -100.0,
    "gerbang.min_pf_r_insample": 0.0,
    "gerbang.min_transaksi_per_jendela": 1,
    "gerbang.min_pct_jendela_pf_di_atas_1": 0.0,
    "gerbang.maks_drawdown_r": 1000.0,
}


def _dengan(data: dict, ubah: dict) -> dict:
    data = copy.deepcopy(data)
    for kunci, nilai in ubah.items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = nilai
    return data


def tulis_config(folder: Path) -> None:
    from asisten_bei.config import baca_yaml

    dasar = baca_yaml(AKAR_PROYEK / "tests" / "fixtures" / "config_rilis.yaml")
    kepala = (f"# BUKAN SARAN INVESTASI. Config uji end-to-end (SPEC §22.2), {LABEL}: salinan config rilis dengan path\n"
              "# fixture, holdout & ambang gerbang longgar, Telegram nonaktif. Dibangkitkan tests/fixtures/buat_fixture.py.\n")
    for nama, ubah in (("config_uji.yaml", UBAH_CONFIG),
                       ("config_uji_ujibo.yaml", {**UBAH_CONFIG, "universe.file_portfolio": "input_uji/portfolio_ujibo.csv"})):
        teks = yaml.safe_dump(_dengan(dasar, ubah), allow_unicode=True, sort_keys=False)
        (folder / nama).write_text(kepala + teks, encoding="utf-8")


def tulis_input(folder: Path, pasar: dict[str, pd.DataFrame], ujibo: dict) -> None:
    inp = folder / "input_uji"
    inp.mkdir(parents=True, exist_ok=True)
    (inp / "universe.csv").write_text(
        "kode,nama,sektor\n" + "".join(f"{k},{k.title()} Tbk ({LABEL}),{s}\n" for k, s in SEKTOR.items()),
        encoding="utf-8")
    (inp / "breadth_universe.csv").write_text(
        "kode\n" + "".join(f"BRD{i + 1:02d}\n" for i in range(N_BREADTH)), encoding="utf-8")
    (inp / "holidays.csv").write_text(
        "tanggal,keterangan\n" + "".join(f"{d.isoformat()},{k}\n" for d, k in sorted(LIBUR.items())), encoding="utf-8")
    (inp / "exclude.csv").write_text("kode,alasan,tanggal_mulai,tanggal_akhir\n", encoding="utf-8")
    kolom = "kode,tanggal_beli,harga_beli,lot,strategi,stop_awal,sektor,harga_per_tanggal,lembar,catatan\n"
    (inp / "portfolio.csv").write_text(kolom, encoding="utf-8")
    (inp / "portfolio_ujibo.csv").write_text(
        kolom + f"UJIBO,{ujibo['tanggal_beli']},{ujibo['harga_beli']},{ujibo['lot']},BO,{ujibo['stop_awal']},,,,{LABEL}\n",
        encoding="utf-8")
    (inp / "akun.yaml").write_text(
        f"# BUKAN SARAN INVESTASI. {LABEL}\nkas_idr: {KAS}\ndiperbarui: {T1.isoformat()}\n", encoding="utf-8")


def tulis_pasar(folder: Path, pasar: dict[str, pd.DataFrame]) -> None:
    from asisten_bei.data.cache import CacheProvider, buat_meta

    tujuan = folder / "pasar_sintetis" / "yfinance"
    if tujuan.exists():
        shutil.rmtree(tujuan)
    cache = CacheProvider(tujuan)
    for kode, df in sorted(pasar.items()):
        cache.tulis(kode, df, buat_meta(df, sekarang=DIAMBIL_PADA, versi_provider=f"sintetis ({LABEL})"))


# ---------------------------------------------------------------------------
# Rakit
# ---------------------------------------------------------------------------


def bangun_pasar() -> dict[str, pd.DataFrame]:
    pasar = {"IHSG": ihsg(), **breadth()}
    for kode, (s, k, b) in saham_uji().items():
        pasar[kode] = s.bingkai(k, b)
    # Sebagian besar saham tanpa bar T_KOSONG (UJIBO dan dua saham breadth tetap punya).
    tetap = {"UJIBO", "BRD01", "BRD02"}
    for kode, df in pasar.items():
        if kode not in tetap:
            pasar[kode] = df.drop(pd.Timestamp(T_KOSONG), errors="ignore")
    return pasar


def baris_ujibo(df: pd.DataFrame) -> dict:
    """Posisi UJIBO di portfolio_ujibo.csv: dibeli di pembukaan T_BO+1 (harga isi = open × (1 + slippage),
    dibulatkan ke rupiah), stop_awal = stop sinyal BO pada T_BO (close − stop_atr × ATR, ke atas)."""
    from asisten_bei.indikator import hitung_indikator

    cfg = _cfg()
    ind = hitung_indikator(df.loc[:, ["open", "high", "low", "close", "volume"]], cfg)
    r = ind.loc[pd.Timestamp(T_BO)]
    stop = math.ceil(round(r["close"] - float(cfg.strategi.bo.stop_atr) * r["atr"], 6))
    isi = round(float(df.loc[pd.Timestamp(geser(T_BO, 1)), "open"]) * (1 + float(cfg.biaya.slippage_pct) / 100))
    return {"tanggal_beli": geser(T_BO, 1).isoformat(), "harga_beli": str(isi), "lot": 2, "stop_awal": str(stop)}


def bangun(folder: Path = FOLDER) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    pasar = bangun_pasar()
    ujibo = baris_ujibo(pasar["UJIBO"])
    tulis_config(folder)
    tulis_pasar(folder, pasar)
    tulis_input(folder, pasar, ujibo)
    return {"pasar": pasar, "ujibo": ujibo}


if __name__ == "__main__":
    bangun()
    print(f"fixture ditulis ke {FOLDER}")
