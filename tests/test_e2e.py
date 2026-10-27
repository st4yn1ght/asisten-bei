"""T-E2E: skenario end-to-end offline (SPEC §22.2) atas `tests/fixtures/e2e/` (FIKTIF – KHUSUS UJI).

Fixture dibangkitkan `tests/fixtures/buat_fixture.py` (deterministik, ikut di-commit). Semua langkah memakai
`--config <tmp>/config_uji.yaml --akar <tmp>` dan waktu virtual pukul 20:00 WIB pada tanggal yang diproses.
Langkah dijalankan berurutan sekali per modul; setiap uji memeriksa hasil satu langkah.

Acuan (golden) laporan langkah 2 ada di `tests/fixtures/e2e/acuan/`; perbarui dengan
`ASISTEN_BEI_PERBARUI_ACUAN=1 .venv/bin/python -m pytest tests/test_e2e.py` lalu tinjau selisihnya.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from asisten_bei import waktu
from asisten_bei.__main__ import main
from asisten_bei.data.cache import CacheProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures"
E2E = FIXTURES / "e2e"
ACUAN = E2E / "acuan"
sys.path.insert(0, str(FIXTURES))
import buat_fixture as bf  # noqa: E402

T1 = bf.T1
T1_1 = bf.geser(T1, 1)
T1_5 = bf.geser(T1, 5)
NAMA_ACUAN = ("laporan.md", "laporan.html", "kandidat.csv", "rencana.xlsx", "kualitas_data.csv")
DISCLAIMER = "BUKAN SARAN INVESTASI"


@dataclass
class Langkah:
    kode: int
    baris: list[str]

    @property
    def teks(self) -> str:
        return "\n".join(self.baris)


@dataclass
class Skenario:
    akar: Path
    langkah: dict[str, Langkah] = field(default_factory=dict)
    dump: dict[str, dict] = field(default_factory=dict)
    kandidat: dict[str, pd.DataFrame] = field(default_factory=dict)

    def jalan(self, nama: str, saat: str, *argv: str, config: str = "config_uji.yaml") -> Langkah:
        os.environ[waktu.ENV_WAKTU] = saat
        waktu.reset_virtual()
        baris: list[str] = []
        kode = main(["--config", str(self.akar / config), "--akar", str(self.akar), *argv], keluar=baris.append)
        self.langkah[nama] = Langkah(kode, baris)
        return self.langkah[nama]

    def harian(self, nama: str, tanggal, *opsi: str, config: str = "config_uji.yaml", jam: str = "20:00:00") -> Langkah:
        return self.jalan(nama, f"{tanggal}T{jam}+07:00", "harian", "--offline", "--tanpa-notifikasi", *opsi,
                          config=config)

    def laporan(self, tanggal) -> Path:
        return self.akar / "output" / "laporan" / str(tanggal)

    def baca_kandidat(self, tanggal) -> pd.DataFrame:
        return pd.read_csv(self.laporan(tanggal) / "kandidat.csv", comment="#", dtype=str, keep_default_na=False)

    def q(self, sql: str, param=()) -> list[dict]:
        con = sqlite3.connect(self.akar / "data" / "jurnal.sqlite")
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, param)]
        finally:
            con.close()

    def simpan_dump(self, nama: str) -> None:
        self.dump[nama] = {t: self.q(f"SELECT * FROM {t} ORDER BY 1, 2")
                           for t in ("run", "sinyal", "hasil_sinyal", "kertas_periode", "kertas_transaksi",
                                     "kertas_ekuitas")}


@pytest.fixture(scope="module")
def sk(tmp_path_factory) -> Skenario:
    from test_backtest_cli import _isolasi_mp

    akar = tmp_path_factory.mktemp("e2e") / "proyek"
    shutil.copytree(E2E, akar, ignore=shutil.ignore_patterns("acuan"))
    s = Skenario(akar)
    with pytest.MonkeyPatch.context() as mp:
        _isolasi_mp(mp)
        mp.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        mp.setenv(waktu.ENV_WAKTU, f"{bf.T_KOSONG}T20:00:00+07:00")
        s.jalan("1", f"{bf.T_KOSONG}T20:00:00+07:00", "backtest", "--mode", "edge")
        s.harian("2", T1, "--catat", "--tanggal", str(T1))
        s.kandidat["2"] = s.baca_kandidat(T1)
        s.simpan_dump("2")
        s.harian("3", T1, "--catat", "--tanggal", str(T1), jam="20:30:00")
        s.simpan_dump("3")
        s.harian("4", T1_1, "--catat", "--tanggal", str(T1_1))
        s.simpan_dump("4")
        s.harian("5", T1_5, "--catat", "--tanggal", str(T1_5))
        s.simpan_dump("5")
        s.harian("6", T1_5, "--tanggal", str(T1_5), config="config_uji_ujibo.yaml", jam="21:00:00")
        s.simpan_dump("6")
        s.harian("7", bf.T_TURUN, "--catat", "--tanggal", str(bf.T_TURUN))
        s.kandidat["7"] = s.baca_kandidat(bf.T_TURUN)
        s.simpan_dump("7")
        (akar / "output" / "backtest" / "gate_status.json").unlink()
        s.harian("8", T1, "--catat", "--tanggal", str(T1), jam="21:00:00")
        s.kandidat["8"] = s.baca_kandidat(T1)
        s.simpan_dump("8")
        s.harian("9", bf.T_KOSONG, "--catat", "--tanggal", str(bf.T_KOSONG), config="config_uji_ujibo.yaml")
        s.jalan("10", f"{bf.T_KOSONG}T21:00:00+07:00", "jurnal", "--rekonsiliasi")
        waktu.reset_virtual()
    return s


def baris(df: pd.DataFrame, kode: str, strategi: str | None = None) -> dict:
    r = df[(df["kode"] == kode) & ((df["strategi"] == strategi) if strategi else True)]
    assert len(r) == 1, (kode, strategi, df.to_string())
    return r.iloc[0].to_dict()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def test_fixture_deterministik_sama_dengan_pembangkit():
    pasar = bf.bangun_pasar()
    cache = CacheProvider(E2E / "pasar_sintetis" / "yfinance")
    assert sorted(cache.daftar_kode()) == sorted(pasar)
    for kode, df in pasar.items():
        tersimpan = cache.baca(kode).bar
        pd.testing.assert_frame_equal(tersimpan, df, check_freq=False, check_dtype=False, obj=kode)


def test_fixture_tanggal_kunci_hari_bursa_dan_berurutan():
    assert bf.T_BO < T1 < T1_5 < bf.T_HATI < bf.T_TURUN < bf.T_KOSONG
    for d in (bf.T_BO, T1, bf.T_HATI, bf.T_TURUN, bf.T_KOSONG):
        assert d in bf.IDX


# ---------------------------------------------------------------------------
# Langkah 1: backtest
# ---------------------------------------------------------------------------


def _folder_run(sk) -> Path:
    folder = [p for p in (sk.akar / "output" / "backtest").iterdir() if p.is_dir()]
    assert len(folder) == 1
    return folder[0]


def test_langkah1_backtest_gerbang_lolos_dan_ringkasan_lengkap(sk):
    assert sk.langkah["1"].kode == 0, sk.langkah["1"].teks
    md = (_folder_run(sk) / "ringkasan.md").read_text(encoding="utf-8")
    for bagian in ("PERINGATAN SURVIVORSHIP BIAS", "## Status gerbang", "## Diagnostik", "## Keterbatasan",
                   "## Run pb", "## Run bo", "## Run mr", "## Run gabungan_lolos"):
        assert bagian in md, bagian
    gerbang = json.loads((_folder_run(sk) / "gerbang_run.json").read_text(encoding="utf-8"))["strategi"]
    assert {k: v["lolos"] for k, v in gerbang.items()} == {"PB": True, "BO": True, "MR": True}


def test_langkah1_ujisplit_sizing_nominal_di_backtest(sk):
    df = pd.read_csv(_folder_run(sk) / "transaksi_pb_edge.csv", comment="#", dtype=str, keep_default_na=False)
    split = df[(df["kode"] == "UJISPLIT") & (df["tanggal_entry"] < "2024-10-01")]
    assert len(split) >= 1
    for _i, r in split.iterrows():
        lembar = Decimal(r["lembar"])
        assert lembar % 100 == 0
        assert abs(Decimal(r["harga_beli_nominal"]) - 5 * Decimal(r["harga_beli"])) < Decimal("0.00001")
        assert Decimal(r["stop_awal_nominal"]) == Decimal(r["stop_awal_nominal"]).to_integral_value()
        nilai = lembar * Decimal(r["harga_beli_nominal"])
        assert abs(Decimal(r["biaya_beli_idr"]) - (nilai * Decimal("1.0015")).quantize(Decimal("0.01"))) <= Decimal("0.02")
        assert Decimal(r["risiko_awal_idr"]) <= Decimal("0.01") * Decimal(1_100_000_000)


# ---------------------------------------------------------------------------
# Langkah 2–3: T1
# ---------------------------------------------------------------------------


def test_langkah2_status_dan_seleksi_t1(sk):
    h = sk.langkah["2"]
    assert h.kode == 0, h.teks
    assert "Status pasar: BOLEH BELI (K3): Tren IHSG naik dan breadth sehat" in h.teks
    k = sk.kandidat["2"]
    pb = baris(k, "UJIPB", "PB")
    assert (pb["status_sinyal"], pb["lot"], pb["harga_maks"], pb["stop_awal"], pb["target_harga"]) == (
        "REKOMENDASI", "3", "1724", "1655", "1862")
    mahal = baris(k, "UJIMAHAL", "PB")
    assert mahal["status_sinyal"] == "TIDAK_TERJANGKAU" and "risiko 1 lot" in mahal["alasan"]
    mahal2 = baris(k, "UJIMAHAL2", "PB")
    assert (mahal2["harga_maks"], mahal2["stop_awal"]) == ("10500", "10290")
    assert mahal2["status_sinyal"] == "TIDAK_TERJANGKAU"
    assert mahal2["alasan"] == "nilai 1 lot Rp 1.051.575 > alokasi maks Rp 1.000.000"
    assert baris(k, "UJISEKTOR", "PB")["status_sinyal"] == "BATAS_SEKTOR"
    assert baris(k, "UJIGAP", "PB")["status_sinyal"] == "REKOMENDASI"
    assert "UJIBOLONG" not in set(k["kode"])
    md = (sk.laporan(T1) / "laporan.md").read_text(encoding="utf-8")
    assert "| UJIBOLONG | BAR_HILANG |" in md


def test_langkah2_risiko_ujimahal2_hitung_tangan():
    # 1 lot: 100 × (10.500 × 1,0015 − 10.290 × 0,999 × 0,9975) = 26.173,9275 ≤ 30.000; biaya 1.051.575.
    risiko = 100 * (Decimal(10500) * Decimal("1.0015") - Decimal(10290) * Decimal("0.999") * Decimal("0.9975"))
    assert risiko.quantize(Decimal("0.01")) == Decimal("26173.93")
    assert 100 * Decimal(10500) * Decimal("1.0015") == Decimal("1051575")


def _normal(teks: str) -> str:
    teks = re.sub(r"Dibuat: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} WIB", "Dibuat: <WAKTU> WIB", teks)
    teks = re.sub(r"hash8 [0-9a-f]{8}", "hash8 <HASH8>", teks)
    teks = re.sub(r"versi \d+\.\d+\.\d+\S*", "versi <VERSI>", teks)
    return teks


def _html_teks(teks: str) -> tuple[str, int]:
    n = len(re.findall(r"data:image/png;base64,", teks))
    return _normal(re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "data:image/png;base64,<PNG>", teks)), n


def _xlsx(path: Path) -> dict[str, list[tuple]]:
    wb = load_workbook(path)
    return {ws.title: [tuple(_normal(x) if isinstance(x, str) else x for x in r) for r in ws.iter_rows(values_only=True)]
            for ws in wb.worksheets}


def test_langkah2_lima_berkas_laporan_cocok_acuan(sk):
    folder = sk.laporan(T1) / "run_200000"  # laporan langkah 2 (salinan terbaru tertimpa langkah 8)
    if os.environ.get("ASISTEN_BEI_PERBARUI_ACUAN"):
        ACUAN.mkdir(parents=True, exist_ok=True)
        for n in NAMA_ACUAN:
            shutil.copy2(folder / n, ACUAN / n)
    for n in ("laporan.md", "kandidat.csv", "kualitas_data.csv"):
        assert _normal((folder / n).read_text(encoding="utf-8")) == _normal((ACUAN / n).read_text(encoding="utf-8")), n
    baru, n_baru = _html_teks((folder / "laporan.html").read_text(encoding="utf-8"))
    acuan, n_acuan = _html_teks((ACUAN / "laporan.html").read_text(encoding="utf-8"))
    assert baru == acuan and n_baru == n_acuan == 4  # IHSG, breadth, UJIPB, UJIGAP
    assert _xlsx(folder / "rencana.xlsx") == _xlsx(ACUAN / "rencana.xlsx")


def test_langkah3_ulang_t1_jurnal_tidak_berubah(sk):
    h = sk.langkah["3"]
    assert h.kode == 0 and "sudah tercatat" in h.teks
    assert sk.dump["3"] == sk.dump["2"]


# ---------------------------------------------------------------------------
# Langkah 4–5: pengisian, susulan, target
# ---------------------------------------------------------------------------


def _hasil(dump: dict, kode: str, strategi: str, tanggal) -> tuple[dict, dict]:
    s = next(x for x in dump["sinyal"] if x["kode"] == kode and x["strategi"] == strategi
             and x["tanggal_sinyal"] == str(tanggal))
    h = next(x for x in dump["hasil_sinyal"] if x["sinyal_id"] == s["sinyal_id"])
    return s, h


def test_langkah4_ujipb_terisi_ujigap_batal_gap(sk):
    assert sk.langkah["4"].kode == 0
    _s, h = _hasil(sk.dump["4"], "UJIPB", "PB", T1)
    assert (h["status_isi"], h["tanggal_entry"]) == ("TERISI", str(T1_1))
    _s, h = _hasil(sk.dump["4"], "UJIGAP", "PB", T1)
    assert h["status_isi"] == "BATAL_GAP"
    kertas = [r for r in sk.dump["4"]["kertas_transaksi"] if r["kode"] == "UJIPB"]
    assert len(kertas) == 1 and kertas[0]["tanggal_entry"] == str(T1_1) and kertas[0]["status_isi"] == "TERISI"
    md = (sk.laporan(T1_1) / "laporan.md").read_text(encoding="utf-8")
    assert "REKOMENDASI kemarin UJIPB" in md and "tidak ada di portfolio.csv" in md


def test_langkah5_susulan_dan_target(sk):
    h = sk.langkah["5"]
    assert h.kode == 0 and "3 hari dikejar" in h.teks
    dump = sk.dump["5"]
    susulan = [x for x in dump["sinyal"] if str(bf.geser(T1, 2)) <= x["tanggal_sinyal"] <= str(bf.geser(T1, 4))]
    assert susulan and all((x["susulan"], x["ditampilkan"]) == (1, 0) and x["status_live"] for x in susulan)
    _s, hp = _hasil(dump, "UJIPB", "PB", T1)
    assert (hp["alasan_exit"], hp["tanggal_exit"]) == ("TARGET", str(bf.geser(T1, 3)))
    kertas = next(r for r in dump["kertas_transaksi"] if r["kode"] == "UJIPB")
    assert (kertas["alasan_exit"], kertas["tanggal_exit"]) == ("TARGET", str(bf.geser(T1, 3)))
    # UJIMR: sinyal MR T1+1, entry T1+2; close hari ke-3 (T1+4) > SMA5 → keluar di pembukaan T1+5.
    _s, hm = _hasil(dump, "UJIMR", "MR", T1_1)
    assert (hm["tanggal_entry"], hm["alasan_exit"], hm["tanggal_exit"]) == (
        str(bf.geser(T1, 2)), "KELUAR_SMA", str(T1_5))


def test_langkah5_ujianomali_eligible_t1_lalu_dikeluarkan(sk):
    md1 = (sk.laporan(T1) / "run_200000" / "laporan.md").read_text(encoding="utf-8")
    assert "| UJIANOMALI |" not in md1.split("## Kualitas data")[1].split("## ")[0]
    md5 = (sk.laporan(T1_5) / "laporan.md").read_text(encoding="utf-8")
    assert "| UJIANOMALI | ANOMALI |" in md5


# ---------------------------------------------------------------------------
# Langkah 6: posisi UJIBO (trailing hitung tangan)
# ---------------------------------------------------------------------------


def _trailing_hitung_tangan(sk) -> tuple[Decimal, Decimal]:
    """ATR Wilder 14 dan HHV high sejak entry dari bar fixture; trail = max(trail, ⌈HHV − 3 × ATR⌉)."""
    df = CacheProvider(E2E / "pasar_sintetis" / "yfinance").baca("UJIBO").bar
    df = df[df.index <= pd.Timestamp(T1_5)]
    h, lo, c = df["high"].tolist(), df["low"].tolist(), df["close"].tolist()
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(df))]
    atr = [math.nan] * len(df)
    atr[13] = sum(tr[:14]) / 14
    for i in range(14, len(df)):
        atr[i] = (atr[i - 1] * 13 + tr[i]) / 14
    entry = df.index.get_loc(pd.Timestamp(bf.geser(bf.T_BO, 1)))
    baris_ujibo = (E2E / "input_uji" / "portfolio_ujibo.csv").read_text(encoding="utf-8").splitlines()[1].split(",")
    trail = Decimal(baris_ujibo[5])
    riwayat = []
    for i in range(entry, len(df)):
        riwayat.append(trail)
        hhv = max(h[entry: i + 1])
        kandidat = Decimal(math.ceil(round(hhv - 3 * atr[i], 6)))
        trail = max(trail, kandidat)
    return riwayat[-1], trail  # (berlaku pada t, untuk t+1)


def test_langkah6_ujibo_tahan_dengan_trailing_hitung_tangan(sk):
    h = sk.langkah["6"]
    assert h.kode == 0
    hari_ini, besok = _trailing_hitung_tangan(sk)
    md = (sk.laporan(T1_5) / "laporan.md").read_text(encoding="utf-8")
    bagian = md.split("## Posisi dipegang")[1].split("## ")[0]
    assert "UJIBO: TAHAN — stop besok " + bf_angka(besok) in bagian
    if besok != hari_ini:
        assert f"naik dari {bf_angka(hari_ini)}" in bagian
        assert f"Perbarui stop BO UJIBO ke {bf_angka(besok)}" in h.teks
    assert sk.dump["6"] == sk.dump["5"]  # tanpa --catat: jurnal tidak berubah


def bf_angka(x: Decimal) -> str:
    return f"{int(x):,}".replace(",", ".")


# ---------------------------------------------------------------------------
# Langkah 7–10
# ---------------------------------------------------------------------------


def test_langkah7_jangan_beli_daftar_pantau(sk):
    h = sk.langkah["7"]
    assert h.kode == 0 and "Status pasar: JANGAN BELI" in h.teks
    k = sk.kandidat["7"]
    assert "REKOMENDASI" not in set(k["status_sinyal"])
    assert baris(k, "UJIMR", "MR")["status_sinyal"] == "BAYANGAN_PASAR"
    md = (sk.laporan(bf.T_TURUN) / "laporan.md").read_text(encoding="utf-8")
    pantau = md.split("## Daftar pantau")[1].split("## ")[0]
    assert "UJIMR MR" in pantau
    tercatat = [x for x in sk.dump["7"]["sinyal"] if x["tanggal_sinyal"] == str(bf.T_TURUN)]
    assert tercatat and all(x["status_live"] == "BAYANGAN_PASAR" for x in tercatat)


def test_langkah8_tanpa_gate_status_semua_bayangan_gerbang(sk):
    h = sk.langkah["8"]
    assert h.kode == 0
    assert set(sk.kandidat["8"]["status_sinyal"]) == {"BAYANGAN_GERBANG"}
    md = (sk.laporan(T1) / "laporan.md").read_text(encoding="utf-8")
    assert "Jalankan backtest.bat" in md
    assert "Tidak ada REKOMENDASI" in md
    # Replay tanggal lampau: turunan terbaru tidak terpotong.
    assert sk.dump["8"]["kertas_ekuitas"] == sk.dump["7"]["kertas_ekuitas"]
    assert sk.dump["8"]["hasil_sinyal"] == sk.dump["7"]["hasil_sinyal"]


def test_langkah9_belum_tersedia_dengan_saran_exit(sk):
    h = sk.langkah["9"]
    assert h.kode == 2
    md = (sk.laporan(bf.T_KOSONG) / "laporan.md").read_text(encoding="utf-8")
    assert "DATA BELUM TERSEDIA" in md and "mungkin hari libur yang belum ada di holidays.csv" in md
    posisi = md.split("## Posisi dipegang")[1].split("## ")[0]
    assert "| UJIBO | BO | TAHAN |" in posisi
    assert not [x for x in sk.q("SELECT * FROM sinyal WHERE tanggal_sinyal = ?", (str(bf.T_KOSONG),))]


def test_langkah10_rekonsiliasi_nol_selisih(sk):
    h = sk.langkah["10"]
    assert h.kode == 0, h.teks
    assert "0 selisih" in h.teks


def test_semua_langkah_memuat_disclaimer(sk):
    for nama, h in sk.langkah.items():
        assert DISCLAIMER in h.teks, nama


# ---------------------------------------------------------------------------
# IHSG & breadth pada fixture (baca saja)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pasar_fixture(tmp_path_factory):
    from asisten_bei.config import muat_config
    from asisten_bei.data.sumber import muat_pasar
    from asisten_bei.inputs import muat_masukan

    akar = tmp_path_factory.mktemp("e2e_baca") / "proyek"
    shutil.copytree(E2E, akar, ignore=shutil.ignore_patterns("acuan"))
    ctx = muat_config(akar / "config_uji.yaml", akar)
    m = muat_masukan(ctx)
    t = bf.geser(bf.T_KOSONG, -1)
    kode = sorted({s.kode for s in m.universe} | set(m.breadth))
    return muat_pasar(ctx, m, kode, akhir=t, t=t).pasar, m


@pytest.mark.parametrize("tanggal, status", [(T1, "BOLEH_BELI"), (bf.T_HATI, "HATI_HATI"), (bf.T_TURUN, "JANGAN_BELI")])
def test_ihsg_tiga_status_pasar_pada_tanggal_kunci(pasar_fixture, tanggal, status):
    from asisten_bei.kondisi_pasar import hitung_kondisi_pasar

    pasar, m = pasar_fixture
    assert hitung_kondisi_pasar(pasar.tampilan(tanggal), m.breadth).status == status


def test_breadth_penyebut_mengabaikan_saham_baru_melantai(pasar_fixture):
    from asisten_bei.kondisi_pasar import hitung_breadth

    pasar, m = pasar_fixture
    awal = hitung_breadth(pasar.tampilan(bf.geser(bf.MELANTAI, 10)), m.breadth)
    assert awal.n_tercatat == bf.N_BREADTH - bf.N_BREADTH_BARU  # baru 10 bar < breadth_sma: belum tercatat
    assert hitung_breadth(pasar.tampilan(T1), m.breadth).n_tercatat == bf.N_BREADTH
