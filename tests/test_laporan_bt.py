"""Uji laporan backtest (SPEC §16.1 berkas & "Kolom berkas", §16.4–§16.10 isi ringkasan, §18.4 disclaimer,
§22.1 byte-identik dan perbandingan HTML; bagian T-LP/T-GB/T-BT).

- Uji berkas memakai jalur CLI sungguhan (`main(... backtest ...)`) atas proyek sintetis `test_backtest_cli`
  di `tmp_path` (waktu virtual, tanpa jaringan).
- Uji format/baris CSV memakai posisi fiktif yang dibangun langsung (`exit.Posisi`), termasuk split 1:5 FIKTIF.

Kode saham dan harga di sini FIKTIF – KHUSUS UJI.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from asisten_bei import DISCLAIMER, DISCLAIMER_CSV, DISCLAIMER_YAML
from asisten_bei import exit as ex
from asisten_bei.backtest import laporan_bt as lb
from asisten_bei.backtest import mesin
from asisten_bei.kalender import Kalender
from asisten_bei.operasional import teks_csv
from bantu_pasar import cfg_uji
from test_backtest_cli import (  # noqa: F401 - `_isolasi`: fixture autouse (waktu virtual, tanpa jaringan)
    MODE_SEMUA,
    RUN_SEMUA,
    WAKTU_BT,
    _isolasi,
    baca_csv,
    berkas_diharapkan,
    buat_proyek,
    jalankan,
    jalankan_sekali,
)

D = Decimal
POLA_BASE64 = re.compile(r"data:image/png;base64,[A-Za-z0-9+/=]+")


@pytest.fixture(scope="module")
def run_a(tmp_path_factory):
    return jalankan_sekali(tmp_path_factory, "lap_a")


@pytest.fixture(scope="module")
def run_b(tmp_path_factory):
    """Proyek kedua yang identik, dijalankan pada waktu virtual yang sama (§22.1 byte-identik)."""
    return jalankan_sekali(tmp_path_factory, "lap_b")


def _folder(proyek) -> Path:
    folder = proyek.folder_run()
    assert len(folder) == 1
    return folder[0]


def _teks_html(html: str) -> str:
    """Teks yang tampil: tanpa tag, entitas dasar dikembalikan, spasi dirapikan."""
    import html as h

    tanpa = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    tanpa = re.sub(r"<[^>]+>", " ", tanpa)
    return " ".join(h.unescape(tanpa).split())


# ---------------------------------------------------------------------------
# Disclaimer di setiap keluaran
# ---------------------------------------------------------------------------


def test_setiap_berkas_memuat_disclaimer(run_a):
    proyek, h = run_a
    assert h.kode == 0, h.teks
    folder = _folder(proyek)
    for p in sorted(folder.iterdir()):
        teks = p.read_text(encoding="utf-8")
        if p.suffix == ".csv":
            assert teks.splitlines()[0] == DISCLAIMER_CSV, p.name
            pd.read_csv(p, comment="#")  # terbaca dengan comment="#"
        elif p.suffix == ".json":
            assert json.loads(teks)["catatan"] == DISCLAIMER, p.name
        elif p.suffix == ".yaml":
            assert teks.splitlines()[0] == DISCLAIMER_YAML, p.name
        elif p.suffix == ".md":
            baris = teks.rstrip("\n").splitlines()
            assert baris[0].startswith("BUKAN SARAN INVESTASI"), p.name
            assert baris[-1] == DISCLAIMER, p.name
        elif p.suffix == ".html":
            tampil = _teks_html(teks)
            assert tampil.startswith("BUKAN SARAN INVESTASI"), tampil[:80]
            assert tampil.endswith(DISCLAIMER)
        else:  # pragma: no cover
            pytest.fail(f"berkas tak terduga: {p.name}")
    for p in (proyek.folder_bt / "gate_status.json", proyek.folder_bt / "holdout_log.csv"):
        teks = p.read_text(encoding="utf-8")
        assert DISCLAIMER in teks


def test_config_snapshot_salinan_config_yang_dipakai(run_a):
    import yaml

    proyek, _ = run_a
    snap = (_folder(proyek) / "config_snapshot.yaml").read_text(encoding="utf-8")
    assert snap.startswith(DISCLAIMER_YAML + "\n")
    asli = proyek.config.read_text(encoding="utf-8")
    assert snap.endswith(asli)
    assert yaml.safe_load(snap) == yaml.safe_load(asli)
    assert "# hash8: " + _folder(proyek).name.split("_")[1] in snap


# ---------------------------------------------------------------------------
# Kolom berkas (§16.1) persis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "awalan, kolom",
    [("transaksi", lb.KOLOM_TRANSAKSI), ("ekuitas", lb.KOLOM_EKUITAS), ("sinyal_dilewati", lb.KOLOM_SINYAL_DILEWATI)],
)
def test_header_csv_per_run_mode_persis(run_a, awalan, kolom):
    proyek, _ = run_a
    folder = _folder(proyek)
    for r in RUN_SEMUA:
        for m in MODE_SEMUA:
            baris = (folder / f"{awalan}_{r}_{m}.csv").read_text(encoding="utf-8").splitlines()
            assert baris[1].split(",") == list(kolom), (awalan, r, m)


def test_header_sinyal_r_persis(run_a):
    proyek, _ = run_a
    for s in ("pb", "bo", "mr", "acak"):
        baris = (_folder(proyek) / f"sinyal_r_{s}.csv").read_text(encoding="utf-8").splitlines()
        assert baris[1].split(",") == list(lb.KOLOM_SINYAL_R)


def test_kolom_spesifikasi_sama_dengan_spec():
    """Daftar kolom §16.1 "Kolom berkas" (disalin dari SPEC)."""
    assert ",".join(lb.KOLOM_TRANSAKSI) == (
        "kode,strategi,sektor,tanggal_sinyal,tanggal_entry,tanggal_exit,alasan_exit,tanda_transaksi,lembar,harga_beli,"
        "harga_beli_nominal,harga_exit,harga_exit_nominal,stop_awal,stop_awal_nominal,target,target_nominal,"
        "biaya_beli_idr,fee_beli_idr,fee_jual_idr,slippage_idr,hasil_jual_idr,pnl_idr,risiko_awal_idr,r,hari,skor,"
        "status_pasar_sinyal,pembatas_lot"
    )
    assert ",".join(lb.KOLOM_EKUITAS) == (
        "tanggal,kas_idr,nilai_posisi_idr,ekuitas_idr,n_posisi,perkiraan,status_pasar,ihsg_close,ekuitas_ihsg_idr"
    )
    assert ",".join(lb.KOLOM_SINYAL_DILEWATI) == (
        "tanggal_sinyal,kode,strategi,skor,status_sinyal,status_isi,tanggal_isi,alasan"
    )
    assert ",".join(lb.KOLOM_SINYAL_R) == (
        "tanggal_sinyal,kode,strategi,skor,status_pasar,status_isi,tanggal_entry,tanggal_exit,alasan_exit,r,hari,"
        "tanda_transaksi"
    )


def test_isi_csv_format_mesin_dan_enum(run_a):
    """Rupiah 2 desimal, R 4 desimal, harga tanpa nol di belakang, tanggal ISO, enum §4.3."""
    proyek, _ = run_a
    folder = _folder(proyek)
    rp = re.compile(r"^-?\d+\.\d{2}$")
    r4 = re.compile(r"^-?\d+\.\d{4}$")
    harga = re.compile(r"^\d+(\.\d{0,5}[1-9])?$")
    tgl = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    alasan_exit = {"STOP", "STOP_GAP", "STOP_GAP_HARI_ENTRY", "TRAILING", "TRAILING_GAP", "TARGET", "TARGET_GAP",
                   "KELUAR_SMA", "STOP_WAKTU", "PAKSA_JUAL_PASAR", "DATA_BERAKHIR", "TERBUKA"}
    n = 0
    for r in RUN_SEMUA:
        for m in MODE_SEMUA:
            tx = baca_csv(folder / f"transaksi_{r}_{m}.csv")
            for _, b in tx.iterrows():
                n += 1
                for k in ("biaya_beli_idr", "fee_beli_idr", "fee_jual_idr", "slippage_idr", "hasil_jual_idr", "pnl_idr",
                          "risiko_awal_idr"):
                    assert rp.match(b[k]), (k, b[k])
                assert r4.match(b["r"]), b["r"]
                for k in ("harga_beli", "harga_beli_nominal", "harga_exit", "harga_exit_nominal", "stop_awal",
                          "stop_awal_nominal", "lembar"):
                    assert harga.match(b[k]), (k, b[k])
                for k in ("tanggal_sinyal", "tanggal_entry", "tanggal_exit"):
                    assert tgl.match(b[k])
                assert b["alasan_exit"] in alasan_exit
                assert b["status_pasar_sinyal"] in {"BOLEH_BELI", "HATI_HATI"}
                assert b["pembatas_lot"] in {"risiko", "alokasi", "kas", "likuid"}
                assert int(b["hari"]) >= 1
                # Tanpa split: harga nominal = tersesuaikan; lembar nominal kelipatan lot.
                assert b["harga_beli"] == b["harga_beli_nominal"] and int(b["lembar"]) % 100 == 0
                if b["alasan_exit"] == "TERBUKA":
                    assert b["tanggal_exit"] == "2025-06-30" and b["fee_jual_idr"] == "0.00"
                # P&L bersih = hasil jual − biaya beli (Rupiah, 2 desimal)
                assert abs(D(b["hasil_jual_idr"]) - D(b["biaya_beli_idr"]) - D(b["pnl_idr"])) <= D("0.01")
            ek = baca_csv(folder / f"ekuitas_{r}_{m}.csv")
            assert ek["status_pasar"].iloc[0] == "" and set(ek["status_pasar"].iloc[1:]) <= {
                "BOLEH_BELI", "HATI_HATI", "JANGAN_BELI"}
            assert set(ek["perkiraan"]) <= {"0", "1"}
            assert (ek["ekuitas_ihsg_idr"].iloc[0]) == ek["ekuitas_idr"].iloc[0]  # modal sama pada titik awal
            for _, b in ek.iterrows():  # dibulatkan per kolom: selisih paling banyak 1 sen
                assert abs(D(b["kas_idr"]) + D(b["nilai_posisi_idr"]) - D(b["ekuitas_idr"])) <= D("0.01")
    assert n > 0


def test_tanda_melewati_holdout_ditulis_di_transaksi_dan_terbuka_in_sample(tmp_path):
    """§16.4: transaksi yang masuk di in-sample dan masih terbuka pada hari bursa terakhir sebelum holdout_mulai
    bertanda MELEWATI_HOLDOUT di transaksi_*.csv dan dihitung TERBUKA (bukan PF) di statistik in-sample."""
    proyek = buat_proyek(tmp_path, {"backtest.holdout_mulai": "2024-10-01", "strategi.acak.stop_waktu_hari": 20})
    h = jalankan(proyek, "--strategi", "acak", "--mode", "edge")
    assert h.kode == 0, h.teks
    tx = baca_csv(_folder(proyek) / "transaksi_acak_edge.csv")
    lewat = (tx["tanggal_entry"] <= "2024-09-30") & (tx["tanggal_exit"] > "2024-09-30")
    assert lewat.sum() >= 1
    assert (tx[lewat]["tanda_transaksi"].str.split(";").apply(lambda t: "MELEWATI_HOLDOUT" in t)).all()
    assert not tx[~lewat]["tanda_transaksi"].str.contains("MELEWATI_HOLDOUT").any()
    md = (_folder(proyek) / "ringkasan.md").read_text(encoding="utf-8")
    baris_is = next(b for b in md.splitlines() if b.startswith("| In-sample | ") and "R |" in b)
    kolom = [x.strip() for x in baris_is.strip("|").split("|")]
    tertutup_is = int(((tx["tanggal_entry"] <= "2024-09-30") & ~lewat).sum())
    assert (kolom[1], kolom[2]) == (str(tertutup_is), str(int(lewat.sum())))  # Tertutup, Terbuka
    assert f"MELEWATI_HOLDOUT {int(lewat.sum())}" in md


# ---------------------------------------------------------------------------
# Byte-identik (§22.1) dan penggabungan gate_status melalui CLI
# ---------------------------------------------------------------------------


def test_dua_run_sama_byte_identik(run_a, run_b):
    pa, ha = run_a
    pb_, hb = run_b
    assert ha.kode == hb.kode == 0
    fa, fb = _folder(pa), _folder(pb_)
    assert fa.name == fb.name  # waktu virtual dan hash sama
    for nama in sorted(berkas_diharapkan()):
        if nama.startswith(("transaksi_", "ekuitas_", "sinyal_")) or nama.endswith((".md", ".json")):
            assert (fa / nama).read_bytes() == (fb / nama).read_bytes(), nama
    ga = (pa.folder_bt / "gate_status.json").read_bytes()
    gb_ = (pb_.folder_bt / "gate_status.json").read_bytes()
    assert ga == gb_
    html_a = (fa / "ringkasan.html").read_text(encoding="utf-8")
    html_b = (fb / "ringkasan.html").read_text(encoding="utf-8")
    assert POLA_BASE64.sub("", html_a) == POLA_BASE64.sub("", html_b)
    assert len(POLA_BASE64.findall(html_a)) == len(POLA_BASE64.findall(html_b))
    assert (pa.folder_bt / "holdout_log.csv").read_bytes() == (pb_.folder_bt / "holdout_log.csv").read_bytes()


def _tanpa(entri: dict, *kunci: str) -> dict:
    return {k: v for k, v in entri.items() if k not in kunci}


def test_gate_status_digabung_per_strategi_lewat_cli(tmp_path, atur_waktu):
    proyek = buat_proyek(tmp_path)
    assert jalankan(proyek, "--mode", "edge").kode == 0
    path = proyek.folder_bt / "gate_status.json"
    awal = json.loads(path.read_text(encoding="utf-8"))
    folder_awal = proyek.folder_run()[0]

    # Run kedua hanya PB: entri BO/MR lama dipertahankan apa adanya; PB diperbarui.
    atur_waktu("2025-07-02T20:00:00+07:00")
    h = jalankan(proyek, "--strategi", "pb", "--mode", "edge")
    assert h.kode == 0, h.teks
    kedua = json.loads(path.read_text(encoding="utf-8"))
    assert kedua["strategi"]["BO"] == awal["strategi"]["BO"]
    assert kedua["strategi"]["MR"] == awal["strategi"]["MR"]
    pb_lama, pb_baru = awal["strategi"]["PB"], kedua["strategi"]["PB"]
    assert pb_baru["dibuat_pada"] == "2025-07-02T20:00:00+07:00"
    folder_baru = proyek.folder_run()[-1]
    assert folder_baru != folder_awal and folder_baru.name.startswith("20250702-200000_")
    assert pb_baru["folder_run"] == "output/backtest/" + folder_baru.name
    # Hasil simulasi yang sama → isi gerbang identik selain dibuat_pada & folder_run (§22.1).
    assert _tanpa(pb_baru, "dibuat_pada", "folder_run") == _tanpa(pb_lama, "dibuat_pada", "folder_run")
    for nama in ("transaksi_pb_edge.csv", "ekuitas_pb_edge.csv", "sinyal_dilewati_pb_edge.csv", "sinyal_r_pb.csv"):
        assert (folder_baru / nama).read_bytes() == (folder_awal / nama).read_bytes(), nama
    assert json.loads((folder_baru / "gerbang_run.json").read_text(encoding="utf-8"))["strategi"].keys() == {"PB"}

    # Run tanpa mode edge tidak menulis gate_status.json sama sekali.
    sebelum = path.read_bytes()
    atur_waktu("2025-07-03T20:00:00+07:00")
    assert jalankan(proyek, "--strategi", "pb,bo,mr", "--mode", "realistis").kode == 0
    assert path.read_bytes() == sebelum
    md = (proyek.folder_run()[-1] / "ringkasan.md").read_text(encoding="utf-8")
    assert "Gerbang tidak dinilai pada run ini (mode edge tidak dijalankan)" in md


def test_gate_status_lolos_dipakai_alasan_tidak_lolos_ctx(run_a):
    """Entri hasil CLI langsung dapat dibaca run harian (§16.6): hash cocok, belum kedaluwarsa → lolos."""
    from asisten_bei.backtest import gerbang as gb
    from asisten_bei.config import muat_config

    proyek, _ = run_a
    ctx = muat_config(proyek.config, proyek.akar)
    for s in ("PB", "BO", "MR"):
        assert gb.alasan_tidak_lolos_ctx(ctx, s, date(2025, 7, 1)) is None


# ---------------------------------------------------------------------------
# HTML mandiri dan isi ringkasan
# ---------------------------------------------------------------------------


def test_html_mandiri_dengan_gambar_tanpa_skrip_atau_url(run_a):
    proyek, _ = run_a
    html = (_folder(proyek) / "ringkasan.html").read_text(encoding="utf-8")
    gambar = POLA_BASE64.findall(html)
    assert len(gambar) >= 2
    assert len(gambar) == 2 * len(RUN_SEMUA)  # ekuitas vs IHSG + drawdown per run
    sisa = POLA_BASE64.sub("", html).lower()
    for terlarang in ("http", "<script", "<link", "@import", "url(", "javascript:", "<iframe"):
        assert terlarang not in sisa, terlarang
    assert all(src == "" for src in re.findall(r'src="([^"]*)"', POLA_BASE64.sub("", html)))
    assert "<style>" in html


def test_png_tersemat_adalah_png_sah(run_a):
    import base64

    proyek, _ = run_a
    html = (_folder(proyek) / "ringkasan.html").read_text(encoding="utf-8")
    for g in POLA_BASE64.findall(html)[:2]:
        data = base64.b64decode(g.split(",", 1)[1])
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 1000


def test_md_tanpa_rujukan_eksternal_dan_tanpa_kata_trade(run_a):
    proyek, _ = run_a
    folder = _folder(proyek)
    md = (folder / "ringkasan.md").read_text(encoding="utf-8")
    assert "http" not in md and "](" not in md and "<img" not in md
    assert "ringkasan.html" in md  # catatan teks untuk grafik
    for nama in ("ringkasan.md", "ringkasan.html"):
        assert not re.search(r"\btrade", (folder / nama).read_text(encoding="utf-8").lower()), nama


def test_ringkasan_memuat_semua_bagian_wajib(run_a):
    proyek, h = run_a
    folder = _folder(proyek)
    md = (folder / "ringkasan.md").read_text(encoding="utf-8")
    html = _teks_html((folder / "ringkasan.html").read_text(encoding="utf-8"))
    wajib = [
        "PERINGATAN SURVIVORSHIP BIAS: backtest memakai daftar universe saat ini untuk seluruh periode. Hasil "
        "cenderung terlalu optimis.",
        "Status gerbang",
        "Kinerja ekuitas vs IHSG beli-dan-tahan",
        "Per tahun kalender",
        "Jalur per sinyal (§16.7), tanpa hari JANGAN BELI",
        "Subset terjangkau modal realistis",
        "Holdout telah dilihat 1 kali dengan 1 konfigurasi aturan berbeda.",
        "Run gabungan_lolos — terseleksi dengan holdout — optimis",
        "Uji kewarasan ACAK",
        "DATA_BERAKHIR",
        "breadth_universe selalu memakai daftar saat ini",
        "Breadth dan K0 per tahun",
        "Sinyal dilewati per status",
        "Tanpa transaksi bertanda ANOMALI",
        "Keterbatasan (wajib dibaca)",
    ]
    for frasa in wajib:
        assert frasa in md, frasa
        assert frasa in html, frasa
    for s in ("PB", "BO", "MR"):
        assert re.search(rf"### {s} — (LOLOS|TIDAK LOLOS)", md)
    for g in ("G1", "G2", "G3", "G4", "G5", "G6"):
        assert f"| {g} |" in md
    for r in RUN_SEMUA:
        for m in MODE_SEMUA:
            assert f"### {r} — mode {m}" in md
    # Periode berdampingan: in-sample, holdout, penuh, dan jendela tahunan (parsial).
    for label in ("| In-sample |", "| Holdout |", "| Penuh |", "| 2024 (parsial) |"):
        assert label in md, label
    assert "Holdout (terseleksi dengan holdout — optimis)" in md  # baris holdout gabungan_lolos
    # Angka tampilan gaya Indonesia (Rupiah bertitik ribuan, koma desimal).
    assert "Rp 1.000.000.000" in md and "Rp 3.000.000" in md


def test_keterbatasan_15_butir_lengkap(run_a):
    proyek, _ = run_a
    md = (_folder(proyek) / "ringkasan.md").read_text(encoding="utf-8")
    bagian = md.split("## Keterbatasan (wajib dibaca)")[1]
    butir = re.findall(r"^(\d+)\. (.+)$", bagian, flags=re.M)
    assert [int(n) for n, _ in butir] == list(range(1, 16))
    kunci = [
        "Survivorship bias", "Kualitas data yfinance", "Dividen diabaikan", "Urutan intraday tidak diketahui",
        "Order otomatis diasumsikan terisi", "Gap turun", "Order beli diasumsikan terisi penuh",
        "Fraksi dan ARA/ARB hanya dimodelkan", "Likuiditas di pembukaan", "Settlement T+2 diabaikan",
        "Pemilihan parameter oleh manusia", "Sampel kecil", "Asumsi konservatif DATA_BERAKHIR (potongan 50%)",
        "Struktur pasar berubah", "Hasil masa lalu tidak menjamin hasil masa depan",
    ]
    for (_, teks), k in zip(butir, kunci):
        assert teks.startswith(k), (k, teks)


def test_peringatan_acak_tampil_bila_pf_r_holdout_di_atas_batas(tmp_path):
    proyek = buat_proyek(tmp_path, {"backtest.maks_pf_r_acak": 0.01})
    h = jalankan(proyek, "--strategi", "acak", "--mode", "edge")
    assert h.kode == 0, h.teks
    md = (_folder(proyek) / "ringkasan.md").read_text(encoding="utf-8")
    teks = ("Pembanding acak tampak menguntungkan — periksa kemungkinan bug/look-ahead atau periode naik kuat")
    assert f"> **{teks}**" in md
    assert teks in h.teks
    assert not (proyek.folder_bt / "gate_status.json").read_text(encoding="utf-8").count('"ACAK"')


def test_tanpa_peringatan_acak_bila_di_bawah_batas(tmp_path):
    proyek = buat_proyek(tmp_path, {"backtest.maks_pf_r_acak": 1000.0})
    assert jalankan(proyek, "--strategi", "acak", "--mode", "edge").kode == 0
    md = (_folder(proyek) / "ringkasan.md").read_text(encoding="utf-8")
    assert "Pembanding acak tampak menguntungkan" not in md
    assert "≤ 1.000,00: wajar" in md


def test_uji_acak_tidak_dinilai_bila_holdout_tanpa_transaksi_tertutup(tmp_path):
    proyek = buat_proyek(tmp_path, {"backtest.holdout_mulai": "2025-06-30"})  # holdout hanya hari terakhir data
    assert jalankan(proyek, "--strategi", "acak", "--mode", "edge").kode == 0
    md = (_folder(proyek) / "ringkasan.md").read_text(encoding="utf-8")
    assert "Tidak dinilai: ACAK tanpa transaksi tertutup di holdout." in md
    assert ": wajar" not in md


def test_universe_history_tanpa_banner_dan_menghitung_konstituen_tanpa_data(tmp_path):
    proyek = buat_proyek(tmp_path)
    (proyek.akar / "input_uji" / "universe_history.csv").write_text(
        "kode,tanggal_masuk,tanggal_keluar\nAAAA,2015-01-01,\nBBBB,2015-01-01,\nZZZZ,2015-01-01,2024-06-30\n",
        encoding="utf-8",
    )
    h = jalankan(proyek, "--strategi", "mr", "--mode", "edge")
    assert h.kode == 0, h.teks
    md = (_folder(proyek) / "ringkasan.md").read_text(encoding="utf-8")
    assert "PERINGATAN SURVIVORSHIP BIAS" not in md
    assert "Konstituen historis tanpa data yang dapat dipakai: 1 (ZZZZ; alasannya di berkas kualitas)" in md
    assert "universe_history.csv dipakai, tetapi 1 konstituen historis tidak punya data" in md
    assert "Konstituen historis tanpa data: 1" in h.teks
    # CCCC bukan konstituen historis → tidak pernah ditransaksikan.
    tx = baca_csv(_folder(proyek) / "transaksi_mr_edge.csv")
    assert "CCCC" not in set(tx["kode"])


# ---------------------------------------------------------------------------
# Baris CSV dari posisi fiktif (split FIKTIF 1:5, TERBUKA, format angka)
# ---------------------------------------------------------------------------


def test_format_angka_mesin():
    assert lb.csv_rupiah(D("203304.5")) == "203304.50"
    assert lb.csv_rupiah(D("66841.625")) == "66841.63"  # ROUND_HALF_UP
    assert lb.csv_rupiah(D("-0.004")) == "0.00"  # tanpa "-0.00"
    assert lb.csv_rupiah(None) == ""
    assert lb.csv_r(D("-1")) == "-1.0000"
    assert lb.csv_r(D("0.123456")) == "0.1235"
    assert lb.csv_harga(D("2314.000000001")) == "2314"
    assert lb.csv_harga(D("406.6")) == "406.6"
    assert lb.csv_harga(D("1.23456789")) == "1.234568"
    assert lb.csv_harga(D("100")) == "100"
    assert lb.csv_harga(D("0")) == "0"
    assert lb.csv_harga(63.2) == "63.2"
    assert lb.csv_harga(float("nan")) == ""
    assert lb.csv_tanda({"MELEWATI_HOLDOUT", "ANOMALI", "SUMBER_MANUAL"}) == "ANOMALI;SUMBER_MANUAL;MELEWATI_HOLDOUT"
    assert lb.csv_tanggal(None) == "" and lb.csv_tanggal(date(2025, 1, 2)) == "2025-01-02"


KAL5 = Kalender({})  # Senin–Jumat


def _posisi_split(keluar: bool = True) -> ex.Posisi:
    """Posisi FIKTIF pada saham yang kemudian split 1:5 (F(entry) = 5): 100 lembar nominal = 500 lembar_adj,
    harga beli nominal 2.030 = 406 adj, stop nominal 1.888, target nominal 2.314."""
    pos = ex.Posisi(
        kode="UJISPLIT", strategi="PB", sektor="Fiktif", tanggal_sinyal=date(2025, 1, 6), tanggal_entry=date(2025, 1, 7),
        lembar=D(500), harga_beli=D("406"), biaya_beli_idr=D("203304.50"), fee_beli_idr=D("304.50"),
        stop_awal=D("377.6"), stop=D("377.6"), target=D("462.8"), risiko_awal_idr=D("15164.828"),
        harga_maks=D("406"), faktor_entry=D(5), slippage_beli_idr=D(0), tanda={"MELEWATI_HOLDOUT", "ANOMALI"},
        tanggal_valid_terakhir=date(2025, 1, 10), close_valid_terakhir=D("420"),
        meta={"skor": 63.25, "status_pasar": "BOLEH_BELI", "pembatas": "risiko"},
    )
    if keluar:
        nilai = D(500) * D("462.8")
        fee = nilai * D("0.0025")
        pos.keluar = ex.Keluar(
            tanggal=date(2025, 1, 9), alasan=ex.TARGET, harga=D("462.8"), harga_nominal=D("2314"), nilai_idr=nilai,
            fee_jual_idr=fee, hasil_jual_idr=nilai - fee, pnl_idr=nilai - fee - D("203304.50"),
            r=(nilai - fee - D("203304.50")) / D("15164.828"),
        )
    return pos


def _lap(cfg, faktor=lambda k, d: D(1), close=None, close_hari=None) -> lb.LaporanBacktest:
    return lb.LaporanBacktest(
        cfg=cfg, kalender=KAL5, dibuat_pada=pd.Timestamp(WAKTU_BT).to_pydatetime(), versi_program="0", t=date(2025, 1, 10),
        akhir=date(2025, 1, 10), tanggal_awal=date(2025, 1, 3), hash_aturan="a" * 64, hash_input="b" * 64, hash8="c" * 8,
        folder_run="output/backtest/x", strategi_diminta=("PB",), mode_diminta=("edge",), runs=[], konteks={},
        close_ihsg=close or {}, close_ihsg_hari_itu=close_hari or {}, faktor=faktor, teks_config="a: 1\n",
        nama_config="config.yaml",
    )


def _rm(transaksi, ekuitas=(), akhir=date(2025, 1, 10)) -> lb.RunMode:
    hasil = SimpleNamespace(transaksi=list(transaksi), ekuitas=list(ekuitas), dilewati=[])
    return lb.RunMode("pb", "edge", ("PB",), hasil, SimpleNamespace(akhir_data=akhir), [])


def test_baris_transaksi_harga_nominal_pada_tanggal_entry_dan_exit():
    cfg = cfg_uji()
    df = lb.baris_transaksi(_rm([_posisi_split()]), _lap(cfg))
    b = df.iloc[0].to_dict()
    assert list(df.columns) == list(lb.KOLOM_TRANSAKSI)
    assert b["lembar"] == "100"  # lembar nominal pada tanggal entry = 500 / 5
    assert (b["harga_beli"], b["harga_beli_nominal"]) == ("406", "2030")
    assert (b["stop_awal"], b["stop_awal_nominal"]) == ("377.6", "1888")
    assert (b["target"], b["target_nominal"]) == ("462.8", "2314")
    assert (b["harga_exit"], b["harga_exit_nominal"]) == ("462.8", "2314")
    assert b["tanda_transaksi"] == "ANOMALI;MELEWATI_HOLDOUT"
    assert b["alasan_exit"] == "TARGET" and b["tanggal_exit"] == "2025-01-09" and b["hari"] == "3"
    # P&L nominal hitung tangan: 231.400 − 578,50 − 203.304,50 = 27.517 (sama dengan §14.2 T-BIAYA)
    assert (b["fee_jual_idr"], b["hasil_jual_idr"], b["pnl_idr"]) == ("578.50", "230821.50", "27517.00")
    assert b["biaya_beli_idr"] == "203304.50" and b["risiko_awal_idr"] == "15164.83"
    assert b["r"] == "1.8145"
    assert (b["skor"], b["status_pasar_sinyal"], b["pembatas_lot"]) == ("63.25", "BOLEH_BELI", "risiko")


def test_baris_transaksi_terbuka_dinilai_close_terakhir_tanpa_fee_jual():
    cfg = cfg_uji()
    pos = _posisi_split(keluar=False)
    df = lb.baris_transaksi(_rm([pos]), _lap(cfg, faktor=lambda k, d: D(5)))
    b = df.iloc[0].to_dict()
    assert b["alasan_exit"] == "TERBUKA" and b["tanggal_exit"] == "2025-01-10" and b["hari"] == "4"
    assert (b["harga_exit"], b["harga_exit_nominal"]) == ("420", "2100")  # close valid terakhir × F(akhir)
    assert b["fee_jual_idr"] == "0.00"
    assert b["hasil_jual_idr"] == "210000.00" and b["pnl_idr"] == "6695.50"
    assert pos.keluar is None  # posisi tidak diubah


def test_baris_ekuitas_titik_awal_dan_ihsg_tanpa_isian():
    cfg = cfg_uji()
    rows = [
        mesin.BarisEkuitas(date(2025, 1, 3), D(1000), D(0), D(1000), 0, False, None),
        mesin.BarisEkuitas(date(2025, 1, 6), D(500), D("510.5"), D("1010.5"), 1, False, "BOLEH_BELI"),
        mesin.BarisEkuitas(date(2025, 1, 7), D(500), D("490"), D("990"), 1, True, "HATI_HATI"),
    ]
    close = {date(2025, 1, 3): D(7000), date(2025, 1, 6): D(7070), date(2025, 1, 7): D(7070)}
    hari_itu = {date(2025, 1, 3): D(7000), date(2025, 1, 6): D("7070.5")}  # 7 Jan tanpa bar IHSG
    df = lb.baris_ekuitas(_rm([], rows), _lap(cfg, close=close, close_hari=hari_itu))
    assert list(df.columns) == list(lb.KOLOM_EKUITAS)
    assert df.iloc[0].tolist() == ["2025-01-03", "1000.00", "0.00", "1000.00", "0", "0", "", "7000", "1000.00"]
    assert df.iloc[1].tolist() == ["2025-01-06", "500.00", "510.50", "1010.50", "1", "0", "BOLEH_BELI", "7070.5",
                                   "1010.00"]
    # ihsg_close kosong (tidak diisi), ekuitas_ihsg memakai close berlaku terakhir; perkiraan = 1.
    assert df.iloc[2].tolist() == ["2025-01-07", "500.00", "490.00", "990.00", "1", "1", "HATI_HATI", "", "1010.00"]


def test_baris_sinyal_r_dan_dilewati():
    j = mesin.JalurSinyal(date(2025, 1, 6), "AAAA", "MR", 41.5, "JANGAN_BELI", ex.TERISI, date(2025, 1, 7),
                          date(2025, 1, 9), ex.KELUAR_SMA, D("0.25"), 3, (ex.ANOMALI,))
    m = mesin.JalurSinyal(date(2025, 1, 10), "BBBB", "MR", 30.0, "BOLEH_BELI", mesin.MENUNGGU)
    df = lb.baris_sinyal_r([j, m])
    assert df.iloc[0].tolist() == ["2025-01-06", "AAAA", "MR", "41.5", "JANGAN_BELI", "TERISI", "2025-01-07",
                                   "2025-01-09", "KELUAR_SMA", "0.2500", "3", "ANOMALI"]
    assert df.iloc[1].tolist() == ["2025-01-10", "BBBB", "MR", "30", "BOLEH_BELI", "MENUNGGU", "", "", "", "", "", ""]
    s = mesin.SinyalDilewati(date(2025, 1, 6), "AAAA", "PB", 55.0, "REKOMENDASI", "BATAL_GAP", "", date(2025, 1, 7))
    t = mesin.SinyalDilewati(date(2025, 1, 6), "CCCC", "PB", 50.0, "TIDAK_TERJANGKAU", None,
                             "risiko 1 lot = Rp 66.841,63 (2,23% ekuitas) > anggaran Rp 30.000 (1%, BOLEH BELI)")
    rm = _rm([])
    rm.hasil.dilewati = [s, t]
    df = lb.baris_dilewati(rm)
    assert df.iloc[0].tolist() == ["2025-01-06", "AAAA", "PB", "55", "REKOMENDASI", "BATAL_GAP", "2025-01-07", ""]
    assert df.iloc[1]["status_isi"] == "" and df.iloc[1]["alasan"].startswith("risiko 1 lot = Rp 66.841,63")
    assert teks_csv(df).startswith(DISCLAIMER_CSV + "\n" + ",".join(lb.KOLOM_SINYAL_DILEWATI) + "\n")


def test_stat_sinyal_tanpa_hari_jangan_beli():
    js = [
        mesin.JalurSinyal(date(2025, 1, 6), "A", "MR", 1.0, "JANGAN_BELI", ex.TERISI, alasan_exit=ex.STOP, r=D(-1)),
        mesin.JalurSinyal(date(2025, 1, 6), "B", "MR", 1.0, "BOLEH_BELI", ex.TERISI, alasan_exit=ex.TARGET, r=D(2)),
        mesin.JalurSinyal(date(2025, 1, 6), "C", "MR", 1.0, "HATI_HATI", ex.TERISI, alasan_exit=ex.STOP, r=D(-1)),
        mesin.JalurSinyal(date(2025, 1, 6), "D", "MR", 1.0, "BOLEH_BELI", ex.TERISI, alasan_exit=ex.TERBUKA, r=D(5)),
        mesin.JalurSinyal(date(2025, 1, 6), "E", "MR", 1.0, "BOLEH_BELI", "BATAL_GAP"),
        mesin.JalurSinyal(date(2025, 1, 6), "F", "MR", 1.0, "BOLEH_BELI", mesin.MENUNGGU),
    ]
    s = lb.stat_sinyal("MR", js)
    assert (s.n_sinyal, s.n_jangan_beli, s.n_terisi, s.n_tertutup, s.n_terbuka, s.n_batal, s.n_menunggu) == (
        5, 1, 3, 2, 1, 1, 1)
    assert s.pf_r.nilai == D(2) and s.ekspektansi_r == D("0.5") and s.win_rate_pct == 50.0


def test_split_fiktif_harga_dan_lembar_nominal_lewat_cli(tmp_path):
    """T-SP (bagian laporan): saham FIKTIF dengan split 1:5 pada 2024-10-01, harga cache sudah disesuaikan.
    Transaksi sebelum split: harga nominal = harga tersesuaikan × 5 dan lembar nominal kelipatan lot;
    sesudah split: nominal = tersesuaikan."""
    from test_backtest_cli import SAHAM_BT, bingkai_sintetis

    proyek = buat_proyek(tmp_path)
    for kode in ("AAAA", "BBBB", "CCCC"):
        seed, awal, drift, vol = SAHAM_BT[kode]
        df = bingkai_sintetis(seed, awal, drift, vol)
        df.loc[pd.Timestamp("2024-10-01"), "split"] = 5.0
        proyek.tulis_cache(kode, df)
    h = jalankan(proyek, "--strategi", "acak", "--mode", "edge,realistis")
    assert h.kode == 0, h.teks
    for m in ("edge", "realistis"):
        tx = baca_csv(_folder(proyek) / f"transaksi_acak_{m}.csv")
        sebelum = tx[tx["tanggal_entry"] < "2024-10-01"]
        sesudah = tx[tx["tanggal_entry"] >= "2024-10-01"]
        if m == "edge":  # realistis: harga nominal ×5 sebelum split sering membuat 1 lot tidak terjangkau
            assert len(sebelum) > 0 and len(sesudah) > 0
        for _, b in sebelum.iterrows():
            assert D(b["harga_beli_nominal"]) == (D(b["harga_beli"]) * 5).quantize(D("0.000001")).normalize()
            assert D(b["stop_awal_nominal"]) == (D(b["stop_awal"]) * 5).quantize(D("0.000001")).normalize()
            assert int(b["lembar"]) % 100 == 0
            if b["tanggal_exit"] < "2024-10-01":
                assert D(b["harga_exit_nominal"]) == (D(b["harga_exit"]) * 5).quantize(D("0.000001")).normalize()
            # Arus Rupiah = lembar_adj × harga_adj = lembar nominal × harga nominal.
            nilai = D(b["lembar"]) * D(b["harga_beli_nominal"])
            assert abs(D(b["biaya_beli_idr"]) - D(b["fee_beli_idr"]) - nilai) <= D("0.01")
        for _, b in sesudah.iterrows():
            assert b["harga_beli_nominal"] == b["harga_beli"] and int(b["lembar"]) % 100 == 0


def test_tutup_terbuka_harga_nominal_pada_tanggal_penilaian():
    """§16.1: harga exit nominal = harga pada tanggal exit × F(tanggal). Posisi FIKTIF tanpa split antara entry dan
    tanggal penilaian (F = 5 di kedua tanggal): close valid terakhir 420 (adj) → nominal 2.100."""
    pos = _posisi_split(keluar=False)
    k = ex.tutup_terbuka(pos, date(2025, 1, 10), cfg_uji(), D(5))
    assert k.harga == D(420)
    assert k.harga_nominal == D(2100)
