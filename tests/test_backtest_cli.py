"""Uji perintah `backtest` lewat CLI (SPEC §16.1–§16.10, §19.2, §20.1; bagian T-BT/T-GB/T-LP §22.1).

`main(["--config", ..., "--akar", ..., "backtest", ...])` dijalankan dalam proses atas proyek sementara di
`tmp_path`: salinan `tests/fixtures/config_uji.yaml` (diubah lewat YAML: periode pendek, indikator pendek, ambang
gerbang longgar) + `input_uji/`, dan cache yfinance sintetis (format §8.3, ditulis `CacheProvider.tulis`) untuk
IHSG dan saham fiktif AAAA–DDDD. Data berseed tetap, tanpa jaringan dan tanpa `time.sleep`; waktu virtual
`2025-07-01 20:00 WIB` → t = 2025-07-01 (cache berakhir 2025-06-30, sehingga akhir data backtest = 2025-06-30).

Kode saham dan harga di sini FIKTIF – KHUSUS UJI.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from asisten_bei import DISCLAIMER, DISCLAIMER_CSV, operasional, waktu
from asisten_bei.__main__ import main
from asisten_bei.backtest import jalankan as jl
from asisten_bei.backtest import mesin
from asisten_bei.data.cache import CacheProvider, buat_meta
from asisten_bei.data.kualitas import KOLOM_KUALITAS
from asisten_bei.operasional import PenyaringRahasia, kunci_proses

AKAR_PROYEK = Path(__file__).resolve().parents[1]
FIXTURES = AKAR_PROYEK / "tests" / "fixtures"
DISCLAIMER_TEKS = "BUKAN SARAN INVESTASI"
WAKTU_BT = "2025-07-01T20:00:00+07:00"
POLA_FOLDER = re.compile(r"^(\d{8}-\d{6})_([0-9a-f]{8})$")

# Config uji: periode pendek (in-sample 2024-04-01 … 2024-12-31, holdout 2025-01-01 …), indikator pendek, dan
# ambang gerbang longgar agar PB/BO/MR bisa lolos pada data sintetis kecil.
UBAH_BT: dict = {
    "indikator.atr_periode": 5,
    "likuiditas.periode_hari": 5,
    "likuiditas.nilai_transaksi_min_idr": 1,
    "universe.min_bar_riwayat": 30,
    "kondisi_pasar.sma_pendek": 10,
    "kondisi_pasar.sma_panjang": 20,
    "kondisi_pasar.breadth_sma": 10,
    "kondisi_pasar.min_saham_breadth": 2,
    "skor.rs_lookback": 10,
    "strategi.pb.sma_cepat": 5,
    "strategi.pb.sma_tren": 10,
    "strategi.pb.sma_panjang": 20,
    "strategi.pb.lookback_slope_sma_tren": 3,
    "strategi.bo.sma_tren": 10,
    "strategi.bo.sma_panjang": 20,
    "strategi.bo.lookback_tertinggi": 10,
    "strategi.bo.lookback_volume": 10,
    "strategi.bo.min_rasio_volume": 1.0,
    "strategi.mr.sma_tren": 20,
    "strategi.mr.rsi_maks": 30,
    "strategi.acak.peluang_sinyal_pct": 10.0,
    "backtest.mulai": "2024-04-01",
    "backtest.holdout_mulai": "2025-01-01",
    "gerbang.min_transaksi_holdout": 1,
    "gerbang.min_pf_r_holdout": 0.0,
    "gerbang.min_ekspektansi_r_holdout": -100.0,
    "gerbang.min_pf_r_insample": 0.0,
    "gerbang.min_transaksi_per_jendela": 1,
    "gerbang.min_pct_jendela_pf_di_atas_1": 0.0,
    "gerbang.maks_drawdown_r": 1000.0,
}

# kode → (seed, harga awal, drift log harian, volatilitas harian). IHSG tanpa volume.
SAHAM_BT: dict[str, tuple[int, float, float, float]] = {
    "IHSG": (1, 7000.0, 0.0006, 0.008),
    "AAAA": (2, 1000.0, 0.001, 0.02),
    "BBBB": (3, 2500.0, 0.0005, 0.025),
    "CCCC": (4, 800.0, 0.0008, 0.022),
    "DDDD": (5, 1500.0, 0.0003, 0.02),
}
RUN_SEMUA = ("pb", "bo", "mr", "acak", "gabungan_aktif", "gabungan_lolos")
MODE_SEMUA = ("edge", "realistis")


# ---------------------------------------------------------------------------
# Proyek sintetis
# ---------------------------------------------------------------------------


def bingkai_sintetis(
    seed: int, awal: float, drift: float, vol: float, *, indeks: bool = False,
    mulai: str = "2024-01-01", akhir: str = "2025-06-30",
) -> pd.DataFrame:
    """Bar Senin–Jumat berseed (format cache §8.3: + `sumber`, `split`). Harga rupiah bulat, OHLC konsisten."""
    idx = pd.DatetimeIndex(pd.bdate_range(mulai, akhir), name="tanggal").as_unit("ns")
    rng = np.random.default_rng(seed)
    n = len(idx)
    c = awal * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    o = np.r_[awal, c[:-1]] * (1 + rng.normal(0, vol / 3, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    o, c, h, lo = (np.round(x) for x in (o, c, h, lo))
    h = np.maximum.reduce([h, o, c])
    lo = np.minimum.reduce([lo, o, c])
    v = np.full(n, np.nan) if indeks else rng.integers(1_000_000, 5_000_000, n).astype(float)
    df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": v}, index=idx)
    df["sumber"] = "yfinance"
    df["split"] = 0.0
    return df


def data_config(ubah: dict | None = None) -> dict:
    data = yaml.safe_load((FIXTURES / "config_uji.yaml").read_text(encoding="utf-8"))
    for kunci, nilai in {**UBAH_BT, **(ubah or {})}.items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = copy.deepcopy(nilai)
    return data


@dataclass
class Proyek:
    akar: Path

    @property
    def config(self) -> Path:
        return self.akar / "config_uji.yaml"

    @property
    def folder_bt(self) -> Path:
        return self.akar / "output" / "backtest"

    def argv(self, *opsi: str) -> list[str]:
        return ["--config", str(self.config), "--akar", str(self.akar), "backtest", *opsi]

    def tulis_config(self, ubah: dict | None = None) -> None:
        self.config.write_text(
            "# BUKAN SARAN INVESTASI. Config uji backtest (FIKTIF – KHUSUS UJI).\n"
            + yaml.safe_dump(data_config(ubah), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def tulis_cache(self, kode: str, df: pd.DataFrame) -> None:
        folder = self.akar / "data" / "cache" / "yfinance"
        CacheProvider(folder).tulis(kode, df, buat_meta(df, sekarang=waktu.sekarang(), versi_provider="palsu-1.0"))

    def folder_run(self) -> list[Path]:
        if not self.folder_bt.is_dir():
            return []
        return sorted(p for p in self.folder_bt.iterdir() if p.is_dir())


def buat_proyek(akar: Path, ubah: dict | None = None, *, cache: bool = True) -> Proyek:
    """Proyek sementara: config uji + input_uji + cache sintetis (bila `cache`)."""
    akar = Path(akar)
    akar.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES / "input_uji", akar / "input_uji")
    p = Proyek(akar)
    p.tulis_config(ubah)
    if cache:
        for kode, (seed, awal, drift, vol) in SAHAM_BT.items():
            p.tulis_cache(kode, bingkai_sintetis(seed, awal, drift, vol, indeks=(kode == "IHSG")))
    return p


@dataclass
class HasilCLI:
    kode: int
    baris: list[str]

    @property
    def teks(self) -> str:
        return "\n".join(self.baris)


def jalankan(proyek: Proyek, *opsi: str) -> HasilCLI:
    baris: list[str] = []
    kode = main(proyek.argv(*opsi), keluar=baris.append)
    return HasilCLI(kode, baris)


def baca_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, comment="#", dtype=str, keep_default_na=False)


def _isolasi_mp(mp: pytest.MonkeyPatch) -> None:
    """Tanpa jaringan dan tanpa tidur sungguhan; aliran sys & penyaring dipulihkan setelah uji."""

    def tolak(*_a, **_k):
        raise AssertionError("uji backtest tidak boleh mengakses jaringan atau tidur sungguhan")

    mp.setattr(socket.socket, "connect", tolak)
    mp.setattr(socket, "create_connection", tolak)
    mp.setattr(time, "sleep", tolak)
    for nama in ("stdout", "stderr", "excepthook"):  # main memasang penyaring di sini (§19.5)
        mp.setattr(sys, nama, getattr(sys, nama))
    mp.setattr(operasional, "PENYARING", PenyaringRahasia())


@pytest.fixture(autouse=True)
def _isolasi(monkeypatch):
    _isolasi_mp(monkeypatch)
    monkeypatch.setenv(waktu.ENV_WAKTU, WAKTU_BT)
    waktu.reset_virtual()
    yield


def jalankan_sekali(tmp_path_factory, nama: str, *opsi: str, ubah: dict | None = None) -> tuple[Proyek, HasilCLI]:
    """Untuk fixture berskop modul: proyek baru + satu run pada waktu virtual `WAKTU_BT`."""
    with pytest.MonkeyPatch.context() as mp:
        _isolasi_mp(mp)
        mp.setenv(waktu.ENV_WAKTU, WAKTU_BT)
        mp.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        waktu.reset_virtual()
        proyek = buat_proyek(tmp_path_factory.mktemp(nama), ubah)
        hasil = jalankan(proyek, *opsi)
        waktu.reset_virtual()
    return proyek, hasil


@pytest.fixture(scope="module")
def run_default(tmp_path_factory) -> tuple[Proyek, HasilCLI]:
    return jalankan_sekali(tmp_path_factory, "bt_default")


# ---------------------------------------------------------------------------
# Run default: kode 0, folder, semua berkas, kualitas, gerbang, log holdout
# ---------------------------------------------------------------------------


def berkas_diharapkan(runs=RUN_SEMUA, modes=MODE_SEMUA, strategi=("pb", "bo", "mr", "acak")) -> set[str]:
    nama = {"ringkasan.md", "ringkasan.html", "gerbang_run.json", "config_snapshot.yaml"}
    for r in runs:
        for m in modes:
            nama |= {f"transaksi_{r}_{m}.csv", f"ekuitas_{r}_{m}.csv", f"sinyal_dilewati_{r}_{m}.csv"}
    nama |= {f"sinyal_r_{s}.csv" for s in strategi}
    return nama


def test_default_kode_0_folder_run_dan_semua_berkas(run_default):
    proyek, h = run_default
    assert h.kode == 0, h.teks
    folder = proyek.folder_run()
    assert len(folder) == 1
    m = POLA_FOLDER.match(folder[0].name)
    assert m and m.group(1) == "20250701-200000"  # jam WIB virtual (§16.1)
    assert {p.name for p in folder[0].iterdir()} == berkas_diharapkan()
    assert (proyek.folder_bt / "gate_status.json").is_file()
    assert (proyek.folder_bt / "holdout_log.csv").is_file()


def test_default_terminal_berakhir_disclaimer_dan_memuat_kemajuan(run_default):
    _, h = run_default
    assert h.baris[0] == "BUKAN SARAN INVESTASI."
    assert h.baris[-1] == DISCLAIMER
    assert any("Keputusan harian" in b and "100%" in b for b in h.baris)
    assert any(b.startswith("Folder hasil: output/backtest/") for b in h.baris)
    assert "Gerbang (mode edge):" in h.teks


def test_default_hash8_folder_cocok_dengan_hash_aturan_global_dan_input(run_default):
    from asisten_bei.backtest import gerbang as gb
    from asisten_bei.config import muat_config

    proyek, _ = run_default
    ctx = muat_config(proyek.config, proyek.akar)
    h8 = gb.hash8(gb.hash_aturan_global(ctx.cfg), gb.hash_input(ctx))
    assert proyek.folder_run()[0].name.endswith("_" + h8)


def test_default_berkas_kualitas_backtest_ditulis(run_default):
    proyek, _ = run_default
    berkas = sorted((proyek.akar / "output" / "kualitas").glob("*_backtest.csv"))
    assert [p.name for p in berkas] == ["20250701-200000_backtest.csv"]
    teks = berkas[0].read_text(encoding="utf-8").splitlines()
    assert teks[0] == DISCLAIMER_CSV
    assert teks[1].split(",") == list(KOLOM_KUALITAS)
    df = baca_csv(berkas[0])
    assert set(df["kode"]) == {"IHSG", "AAAA", "BBBB", "CCCC", "DDDD"}
    # Dinilai as-of akhir data backtest (2025-06-30), bukan t (2025-07-01, tanpa bar): tidak ada DATA_BASI.
    assert set(df["bar_terakhir"]) == {"2025-06-30"}
    assert set(df["alasan_dikeluarkan"]) == {""}


def test_default_akhir_data_hari_terakhir_dengan_bar_ihsg(run_default):
    """§16.1: selesai null → hari bursa terakhir ≤ t (2025-07-01) yang punya bar IHSG valid = 2025-06-30."""
    proyek, _ = run_default
    folder = proyek.folder_run()[0]
    ek = baca_csv(folder / "ekuitas_pb_edge.csv")
    assert ek["tanggal"].iloc[0] == "2024-03-29"  # titik awal: hari bursa terakhir sebelum backtest.mulai
    assert ek["tanggal"].iloc[-1] == "2025-06-30"
    gate = json.loads((proyek.folder_bt / "gate_status.json").read_text(encoding="utf-8"))
    assert {e["tanggal_data_terakhir"] for e in gate["strategi"].values()} == {"2025-06-30"}


def test_default_gerbang_untuk_pb_bo_mr_saja_dan_folder_run_relatif(run_default):
    proyek, _ = run_default
    gate = json.loads((proyek.folder_bt / "gate_status.json").read_text(encoding="utf-8"))
    run = json.loads((proyek.folder_run()[0] / "gerbang_run.json").read_text(encoding="utf-8"))
    assert gate["catatan"] == DISCLAIMER and run["catatan"] == DISCLAIMER
    assert set(gate["strategi"]) == {"PB", "BO", "MR"}  # ACAK tidak pernah dinilai gerbang
    assert run["strategi"] == gate["strategi"]
    for e in gate["strategi"].values():
        assert e["folder_run"] == "output/backtest/" + proyek.folder_run()[0].name
        assert set(e["syarat"]) == {"G1", "G2", "G3", "G4", "G5", "G6"}
        assert e["holdout_mulai"] == "2025-01-01"
        assert e["dibuat_pada"] == "2025-07-01T20:00:00+07:00"


def test_default_holdout_log_satu_baris_per_run_mode(run_default):
    proyek, _ = run_default
    teks = (proyek.folder_bt / "holdout_log.csv").read_text(encoding="utf-8").splitlines()
    assert teks[0] == DISCLAIMER_CSV
    df = baca_csv(proyek.folder_bt / "holdout_log.csv")
    pasangan = list(zip(df["strategi"], df["mode"]))
    assert pasangan == [(r, m) for r in RUN_SEMUA for m in MODE_SEMUA]
    assert set(df["waktu"]) == {"2025-07-01T20:00:00+07:00"}  # satu `saat` per perintah → N = 1
    assert set(df["holdout_mulai"]) == {"2025-01-01"}
    assert len(set(df["hash_aturan"])) == 1


def test_default_gabungan_dijalankan_tanpa_opsi_strategi(run_default):
    proyek, h = run_default
    nama = {p.name for p in proyek.folder_run()[0].iterdir()}
    assert {"transaksi_gabungan_aktif_edge.csv", "transaksi_gabungan_lolos_realistis.csv"} <= nama
    assert "gabungan_lolos" in h.teks and "terseleksi dengan holdout — optimis" in h.teks
    # gabungan tidak pernah memuat ACAK (§12.4, §16.3)
    for r in ("gabungan_aktif", "gabungan_lolos"):
        tx = baca_csv(proyek.folder_run()[0] / f"transaksi_{r}_edge.csv")
        assert set(tx["strategi"]) <= {"PB", "BO", "MR"}


# ---------------------------------------------------------------------------
# Filter --strategi / --mode
# ---------------------------------------------------------------------------


def test_filter_strategi_dan_mode_tanpa_gabungan_tanpa_gate_status(tmp_path):
    proyek = buat_proyek(tmp_path)
    h = jalankan(proyek, "--strategi", "acak,PB", "--mode", "realistis")
    assert h.kode == 0, h.teks
    folder = proyek.folder_run()
    assert len(folder) == 1
    assert {p.name for p in folder[0].iterdir()} == berkas_diharapkan(
        runs=("pb", "acak"), modes=("realistis",), strategi=("pb", "acak"))
    # gate_status.json hanya ditulis bila run memuat mode edge (§16.6)
    assert not (proyek.folder_bt / "gate_status.json").exists()
    run = json.loads((folder[0] / "gerbang_run.json").read_text(encoding="utf-8"))
    assert run == {"catatan": DISCLAIMER, "strategi": {}}
    df = baca_csv(proyek.folder_bt / "holdout_log.csv")
    assert list(zip(df["strategi"], df["mode"])) == [("pb", "realistis"), ("acak", "realistis")]
    assert "gabungan" not in " ".join(p.name for p in folder[0].iterdir())
    assert h.baris[-1] == DISCLAIMER


def test_mode_realistis_tanpa_strategi_gabungan_lolos_dilewati_dengan_catatan(tmp_path):
    proyek = buat_proyek(tmp_path)
    h = jalankan(proyek, "--mode", "realistis")
    assert h.kode == 0, h.teks
    nama = {p.name for p in proyek.folder_run()[0].iterdir()}
    assert "transaksi_gabungan_aktif_realistis.csv" in nama
    assert not any("gabungan_lolos" in n for n in nama)
    assert "gabungan_lolos dilewati: memerlukan mode edge" in h.teks
    md = (proyek.folder_run()[0] / "ringkasan.md").read_text(encoding="utf-8")
    assert "gabungan_lolos dilewati: memerlukan mode edge" in md


def test_gabungan_lolos_dilewati_bila_tidak_ada_yang_lolos(tmp_path):
    proyek = buat_proyek(tmp_path, {"gerbang.min_transaksi_holdout": 100000})
    h = jalankan(proyek, "--mode", "edge")
    assert h.kode == 0, h.teks
    nama = {p.name for p in proyek.folder_run()[0].iterdir()}
    assert "transaksi_gabungan_aktif_edge.csv" in nama
    assert not any("gabungan_lolos" in n for n in nama)
    assert "gabungan_lolos dilewati: tidak ada strategi aktif yang lolos gerbang" in h.teks
    gate = json.loads((proyek.folder_bt / "gate_status.json").read_text(encoding="utf-8"))
    assert {k: v["lolos"] for k, v in gate["strategi"].items()} == {"PB": False, "BO": False, "MR": False}
    assert all(not v["syarat"]["G1"]["lolos"] for v in gate["strategi"].values())


def test_strategi_nonaktif_tetap_dijalankan_per_strategi_tetapi_tidak_di_gabungan(tmp_path):
    proyek = buat_proyek(tmp_path, {"strategi.bo.aktif": False})
    h = jalankan(proyek, "--mode", "edge")
    assert h.kode == 0, h.teks
    folder = proyek.folder_run()[0]
    assert (folder / "transaksi_bo_edge.csv").is_file()
    for r in ("gabungan_aktif", "gabungan_lolos"):
        tx = baca_csv(folder / f"transaksi_{r}_edge.csv")
        dl = baca_csv(folder / f"sinyal_dilewati_{r}_edge.csv")
        assert "BO" not in set(tx["strategi"]) | set(dl["strategi"])
    assert "strategi lolos tetapi tidak aktif (tidak ikut gabungan_lolos): BO" in h.teks


@pytest.mark.parametrize(
    "opsi",
    [
        ("--strategi", "pb,xx"),
        ("--strategi", "gabungan_aktif"),
        ("--strategi", "manual"),
        ("--strategi", ""),
        ("--mode", "edge,santai"),
        ("--mode", ","),
    ],
    ids=["strategi_asing", "nama_run_bukan_strategi", "manual", "strategi_kosong", "mode_asing", "mode_kosong"],
)
def test_opsi_tidak_valid_kode_3_tanpa_keluaran(tmp_path, opsi):
    proyek = buat_proyek(tmp_path)
    h = jalankan(proyek, *opsi)
    assert h.kode == 3, h.teks
    assert "ERROR argumen" in h.teks and opsi[0] in h.teks
    assert DISCLAIMER_TEKS in h.baris[-1]
    assert not (proyek.akar / "output" / "backtest").exists()
    assert not (proyek.akar / "output" / "kualitas").exists()


def test_parse_daftar_urutan_kanonik_tanpa_duplikat():
    assert jl.parse_daftar_strategi("acak, MR,pb,pb") == ("pb", "mr", "acak")
    assert jl.parse_daftar_mode("realistis,edge") == ("edge", "realistis")
    with pytest.raises(ValueError, match="tidak dikenal: zz"):
        jl.parse_daftar_strategi("pb,zz")
    with pytest.raises(ValueError, match="kosong"):
        jl.parse_daftar_mode(" , ")


# ---------------------------------------------------------------------------
# Kode keluar: kunci (5), identitas akuntansi (1), tanpa data (2), input (3)
# ---------------------------------------------------------------------------


def test_kunci_dipegang_kode_5_tanpa_keluaran(tmp_path):
    proyek = buat_proyek(tmp_path)
    with kunci_proses(proyek.akar, "unduh"):
        h = jalankan(proyek, "--strategi", "acak", "--mode", "edge")
    assert h.kode == 5, h.teks
    assert "proses lain sedang berjalan: unduh" in h.teks
    assert not (proyek.akar / "output" / "backtest").exists()
    assert not (proyek.akar / "output" / "kualitas").exists()
    assert not (proyek.akar / "data" / ".kunci.info").exists()  # info kunci dilepas pemiliknya


def test_identitas_akuntansi_gagal_kode_1_tanpa_ringkasan(tmp_path, monkeypatch):
    proyek = buat_proyek(tmp_path)

    def gagal(h):
        raise mesin.IdentitasAkuntansiGagal(f"identitas akuntansi gagal ({h.run.nama}/{h.mode}): selisih uji")

    monkeypatch.setattr(mesin, "periksa_identitas", gagal)
    h = jalankan(proyek, "--strategi", "acak", "--mode", "edge")
    assert h.kode == 1, h.teks
    assert "identitas akuntansi" in h.teks and "selisih uji" in h.teks
    assert h.baris[-1] == DISCLAIMER
    # Tidak ada ringkasan parsial yang menyesatkan; gerbang & log holdout tidak disentuh.
    assert proyek.folder_run() == []
    assert not (proyek.folder_bt / "gate_status.json").exists()
    assert not (proyek.folder_bt / "holdout_log.csv").exists()
    assert not (proyek.akar / "data" / ".kunci.info").exists()


def test_identitas_gagal_tidak_mengubah_gate_status_lama(tmp_path, monkeypatch, atur_waktu):
    proyek = buat_proyek(tmp_path)
    assert jalankan(proyek, "--strategi", "mr", "--mode", "edge").kode == 0
    lama = (proyek.folder_bt / "gate_status.json").read_bytes()
    log_lama = (proyek.folder_bt / "holdout_log.csv").read_bytes()

    def gagal(h):
        raise mesin.IdentitasAkuntansiGagal("selisih uji")

    monkeypatch.setattr(mesin, "periksa_identitas", gagal)
    atur_waktu("2025-07-02T20:00:00+07:00")
    assert jalankan(proyek, "--strategi", "mr", "--mode", "edge").kode == 1
    assert (proyek.folder_bt / "gate_status.json").read_bytes() == lama
    assert (proyek.folder_bt / "holdout_log.csv").read_bytes() == log_lama
    assert len(proyek.folder_run()) == 1


@pytest.mark.parametrize("ubah", [{}, {"backtest.selesai": "2025-03-31"}], ids=["selesai_null", "selesai_diisi"])
def test_tanpa_cache_kode_2_data_belum_tersedia(tmp_path, ubah):
    proyek = buat_proyek(tmp_path, ubah, cache=False)
    h = jalankan(proyek)
    assert h.kode == 2, h.teks
    assert "DATA BELUM TERSEDIA" in h.teks and "unduh" in h.teks
    assert h.baris[-1] == DISCLAIMER
    assert not (proyek.akar / "output" / "backtest").exists()
    kualitas = list((proyek.akar / "output" / "kualitas").glob("*_backtest.csv"))
    assert len(kualitas) == 1
    df = baca_csv(kualitas[0])
    assert set(df["alasan_dikeluarkan"]) == {"GAGAL_AMBIL"}


def test_input_tidak_valid_kode_3(tmp_path):
    proyek = buat_proyek(tmp_path)
    (proyek.akar / "input_uji" / "universe.csv").write_text("kode,nama,sektor\nAAAA,X,S\nAAAA,Y,T\n", encoding="utf-8")
    h = jalankan(proyek)
    assert h.kode == 3, h.teks
    assert "input tidak valid" in h.teks.lower()
    assert not (proyek.akar / "output" / "backtest").exists()


# ---------------------------------------------------------------------------
# Akhir data, log holdout N/M
# ---------------------------------------------------------------------------


def test_akhir_data_ikut_bar_ihsg_terakhir(tmp_path):
    """IHSG berhenti 2025-06-26, saham sampai 2025-06-30 → akhir data = 2025-06-26 (§16.1)."""
    proyek = buat_proyek(tmp_path)
    seed, awal, drift, vol = SAHAM_BT["IHSG"]
    proyek.tulis_cache("IHSG", bingkai_sintetis(seed, awal, drift, vol, indeks=True, akhir="2025-06-26"))
    h = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert h.kode == 0, h.teks
    ek = baca_csv(proyek.folder_run()[0] / "ekuitas_mr_edge.csv")
    assert ek["tanggal"].iloc[-1] == "2025-06-26"


def test_backtest_selesai_menentukan_akhir_data(tmp_path):
    proyek = buat_proyek(tmp_path, {"backtest.selesai": "2025-03-31"})
    h = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert h.kode == 0, h.teks
    ek = baca_csv(proyek.folder_run()[0] / "ekuitas_mr_edge.csv")
    assert ek["tanggal"].iloc[-1] == "2025-03-31"
    tx = baca_csv(proyek.folder_run()[0] / "transaksi_mr_edge.csv")
    assert (tx["tanggal_exit"] <= "2025-03-31").all()
    gate = json.loads((proyek.folder_bt / "gate_status.json").read_text(encoding="utf-8"))
    assert gate["strategi"]["MR"]["tanggal_data_terakhir"] == "2025-03-31"


def test_backtest_selesai_sesudah_t_dibatasi_dengan_catatan(tmp_path):
    proyek = buat_proyek(tmp_path, {"backtest.selesai": "2025-12-31"})
    h = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert h.kode == 0, h.teks
    ek = baca_csv(proyek.folder_run()[0] / "ekuitas_mr_edge.csv")
    assert ek["tanggal"].iloc[-1] == "2025-06-30"
    assert "backtest.selesai 2025-12-31 sesudah tanggal data t 2025-07-01" in h.teks


def test_holdout_log_bertambah_n_dan_m(tmp_path, atur_waktu):
    proyek = buat_proyek(tmp_path)
    h1 = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert h1.kode == 0, h1.teks
    assert "Holdout telah dilihat 1 kali dengan 1 konfigurasi aturan berbeda." in h1.teks

    atur_waktu("2025-07-02T20:00:00+07:00")
    h2 = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert "Holdout telah dilihat 2 kali dengan 1 konfigurasi aturan berbeda." in h2.teks
    assert "PERINGATAN" not in (proyek.folder_run()[-1] / "ringkasan.md").read_text(encoding="utf-8").split(
        "Disiplin holdout")[1].split("###")[0]

    proyek.tulis_config({"strategi.mr.rsi_maks": 25})  # aturan berubah → hash_aturan global berubah
    atur_waktu("2025-07-03T20:00:00+07:00")
    h3 = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert h3.kode == 0, h3.teks
    assert "Holdout telah dilihat 3 kali dengan 2 konfigurasi aturan berbeda." in h3.teks
    assert "holdout tidak lagi murni" in h3.teks
    md = (proyek.folder_run()[-1] / "ringkasan.md").read_text(encoding="utf-8")
    assert "Holdout telah dilihat 3 kali dengan 2 konfigurasi aturan berbeda." in md
    assert "holdout tidak lagi murni" in md
    teks = (proyek.folder_bt / "holdout_log.csv").read_text(encoding="utf-8")
    assert teks.count(DISCLAIMER_CSV) == 1  # disclaimer hanya saat berkas dibuat
    assert len(baca_csv(proyek.folder_bt / "holdout_log.csv")) == 3
    assert len(proyek.folder_run()) == 3


def test_data_berakhir_sebelum_holdout_tanpa_baris_log(tmp_path):
    """Akhir data sebelum holdout_mulai: holdout tidak dihitung → tidak ada baris holdout_log (§16.4)."""
    proyek = buat_proyek(tmp_path, {"backtest.holdout_mulai": "2025-09-01"})
    h = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert h.kode == 0, h.teks
    assert not (proyek.folder_bt / "holdout_log.csv").exists()
    gate = json.loads((proyek.folder_bt / "gate_status.json").read_text(encoding="utf-8"))
    assert gate["strategi"]["MR"]["lolos"] is False  # G1: tanpa transaksi holdout
    md = (proyek.folder_run()[0] / "ringkasan.md").read_text(encoding="utf-8")
    assert "Holdout belum dihitung" in md
    # Tabel tidak menampilkan angka untuk holdout yang belum tercapai, dan jendela terakhir dipotong di akhir data.
    assert "| Holdout | belum tercapai oleh data |" in md
    assert "| Holdout | 2025-09-01" not in md
    assert "| 2025 (parsial) | 2025-01-01 … 2025-06-30 |" in md
