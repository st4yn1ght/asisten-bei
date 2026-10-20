"""Berkas contoh untuk SPEC §24 (folder `contoh/`) dan templat kosong di `input/`.

Contoh harus selalu sah menurut parser program (agar formatnya bisa ditiru apa adanya), tetapi tidak boleh
membawa nilai yang bisa disangka data sungguhan: aturan BEI fiktif, libur bertahun 2099, kode saham fiktif.
"""

from __future__ import annotations

import copy
import re
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from asisten_bei import inputs
from asisten_bei.cek import jalankan_cek
from asisten_bei.config import ConfigTidakValid, baca_yaml, muat_config, validasi_dict

AKAR = Path(__file__).resolve().parents[1]
CONTOH = AKAR / "contoh"

PEMBACA = {
    "universe.csv": (inputs.baca_universe, inputs.KOLOM_UNIVERSE),
    "breadth_universe.csv": (inputs.baca_breadth, inputs.KOLOM_BREADTH),
    "universe_history.csv": (inputs.baca_riwayat, inputs.KOLOM_RIWAYAT),
    "exclude.csv": (inputs.baca_exclude, inputs.KOLOM_EXCLUDE),
    "holidays.csv": (inputs.baca_libur, inputs.KOLOM_LIBUR),
    "anomali_ok.csv": (inputs.baca_anomali_ok, inputs.KOLOM_ANOMALI_OK),
}
TEMPLAT = {  # berkas wajib yang disediakan di input/ dengan baris judul saja
    "universe.csv": inputs.KOLOM_UNIVERSE,
    "breadth_universe.csv": inputs.KOLOM_BREADTH,
    "exclude.csv": inputs.KOLOM_EXCLUDE,
    "holidays.csv": inputs.KOLOM_LIBUR,
    "portfolio.csv": inputs.KOLOM_PORTOFOLIO,
}


def _header(skema) -> str:
    wajib, opsional = skema
    return ",".join([*wajib, *opsional])


def _gabung(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _gabung(a[k], v)
        else:
            a[k] = copy.deepcopy(v)
    return a


@pytest.mark.parametrize("nama", sorted(PEMBACA))
def test_contoh_csv_sah_dan_memakai_header_lengkap(nama):
    baca, skema = PEMBACA[nama]
    path = CONTOH / "input" / nama
    assert path.read_text(encoding="utf-8").splitlines()[0] == _header(skema)
    hasil, masalah = baca(path)
    assert hasil and [m for m in masalah if m.level == inputs.ERROR] == []


def test_contoh_portfolio_dan_akun_sah_dan_konsisten():
    universe, _ = inputs.baca_universe(CONTOH / "input" / "universe.csv")
    posisi, masalah = inputs.baca_portofolio(CONTOH / "input" / "portfolio.csv", {s.kode for s in universe})
    assert len(posisi) == 2 and [m for m in masalah if m.level == inputs.ERROR] == []
    akun, masalah = inputs.baca_akun(CONTOH / "input" / "akun.yaml", Decimal(3_000_000))
    assert masalah == [] and akun.dari_berkas
    # Komentar contoh: modal 3.000.000 − dua pembelian di contoh portfolio.csv (fee beli 0,15 %).
    beli = sum(Decimal(p.harga_beli) * p.lot * 100 * Decimal("1.0015") for p in posisi)
    assert akun.kas_idr == Decimal(3_000_000) - beli == Decimal(2_268_905)


def test_contoh_libur_tidak_bisa_disangka_kalender_sungguhan():
    libur, _ = inputs.baca_libur(CONTOH / "input" / "holidays.csv")
    assert libur and all(d.year == 2099 for d in libur)


@pytest.mark.parametrize("nama", sorted(TEMPLAT))
def test_templat_input_hanya_baris_judul(nama):
    path = AKAR / "input" / nama
    assert path.read_text(encoding="utf-8") == _header(TEMPLAT[nama]) + "\n"


def test_input_tidak_memuat_berkas_yang_berbahaya_bila_kosong_atau_contoh():
    # universe_history.csv kosong membuat backtest tanpa konstituen; akun.yaml contoh akan dipakai sebagai kas.
    for nama in ("universe_history.csv", "akun.yaml", "anomali_ok.csv"):
        assert not (AKAR / "input" / nama).exists(), nama


def test_contoh_config_sah_bila_disalin_apa_adanya():
    contoh = baca_yaml(CONTOH / "config_bei_contoh.yaml")
    cfg = validasi_dict(_gabung(baca_yaml(AKAR / "config.yaml"), contoh))
    assert not cfg.pasar.fraksi_harga.terverifikasi and not cfg.pasar.auto_reject.terverifikasi


@pytest.mark.parametrize("blok", ["jadwal_sesi", "fraksi_harga", "auto_reject"])
def test_contoh_aturan_bei_ditolak_bila_ditandai_terverifikasi_tanpa_mengganti_sumber(blok):
    data = _gabung(baca_yaml(AKAR / "config.yaml"), baca_yaml(CONTOH / "config_bei_contoh.yaml"))
    data["pasar"][blok]["terverifikasi"] = True
    with pytest.raises(ConfigTidakValid, match="CONTOH FIKTIF"):
        validasi_dict(data)
    # Bentuk isiannya sendiri sah: dengan sumber diganti, blok terverifikasi diterima.
    data["pasar"][blok]["sumber"] = "Sumber resmi FIKTIF – KHUSUS UJI"
    cfg = validasi_dict(data)
    assert getattr(cfg.pasar, blok).terverifikasi


def test_config_dengan_blok_tertempel_dua_kali_ditolak(tmp_path):
    teks = (AKAR / "config.yaml").read_text(encoding="utf-8")
    contoh = (CONTOH / "config_bei_contoh.yaml").read_text(encoding="utf-8")
    biaya = contoh[contoh.index("\nbiaya:"):contoh.index("\n# --- Item 10")]
    (tmp_path / "config.yaml").write_text(teks + biaya, encoding="utf-8")
    with pytest.raises(ConfigTidakValid, match="kunci ganda 'biaya'"):
        muat_config(tmp_path / "config.yaml", tmp_path)


def test_contoh_aturan_bei_ditandai_fiktif_dan_tidak_pernah_berlaku():
    pasar = baca_yaml(CONTOH / "config_bei_contoh.yaml")["pasar"]
    for blok in ("jadwal_sesi", "fraksi_harga", "auto_reject"):
        assert pasar[blok]["terverifikasi"] is False
        assert "CONTOH FIKTIF" in pasar[blok]["sumber"]
    # Bila tersalin lalu ditandai terverifikasi tanpa diganti, aturan fiktif tetap tidak pernah berlaku.
    assert pasar["fraksi_harga"]["berlaku_mulai"] == "2099-01-01"
    assert pasar["auto_reject"]["berlaku_mulai"] == "2099-01-01"
    teks = (CONTOH / "config_bei_contoh.yaml").read_text(encoding="utf-8")
    assert teks.startswith("# BUKAN SARAN INVESTASI")
    assert all("FIKTIF" in b for b in teks.splitlines() if re.search(r"(fraksi|batas_pct):\s*\d", b))


def test_contoh_env_berisi_dua_kunci_telegram():
    nilai = dotenv_values(CONTOH / "env.contoh")
    assert set(nilai) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert "FIKTIF" in nilai["TELEGRAM_BOT_TOKEN"]


def test_cek_dengan_contoh_input_tanpa_error(tmp_path):
    shutil.copy2(AKAR / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(CONTOH / "input", tmp_path / "input")
    baris: list[str] = []
    hasil = jalankan_cek(muat_config(tmp_path / "config.yaml", tmp_path), silang_kode=None, keluar=baris.append)
    assert hasil.kode_keluar == 0 and hasil.error == [], "\n".join(baris)
    # Contoh yang tersalin tanpa diganti tetap memicu peringatan: libur bertahun 2099 dan akun bertanggal 2099.
    assert any("holidays.csv belum berisi tanggal libur tahun" in b for b in baris)
    assert any("akun.yaml" in b and "berada di masa depan" in b for b in baris), "\n".join(baris)


def test_panduan_menyebut_semua_item_dan_tautannya_ada():
    teks = (CONTOH / "PANDUAN_ISI.md").read_text(encoding="utf-8")
    assert teks.startswith("# ") and "BUKAN SARAN INVESTASI" in teks
    for judul in ("Item 1 ", "Item 2 ", "Item 3 ", "Item 4 ", "Item 5 ", "Item 6–8", "Item 9 ", "Item 10 ",
                  "Item 11 ", "Item 12 ", "Item 13 ", "Item 14 "):
        assert f"## {judul}" in teks, judul
    for tautan in re.findall(r"\]\(([^)]+)\)", teks):
        assert (CONTOH / tautan).exists(), tautan
    assert "trade " not in teks.lower().replace("trade confirmation", "")


def test_berkas_yaml_contoh_diawali_disclaimer():
    for path in (CONTOH / "config_bei_contoh.yaml", CONTOH / "input" / "akun.yaml"):
        teks = path.read_text(encoding="utf-8")
        assert "BUKAN SARAN INVESTASI" in teks.splitlines()[0]
        yaml.safe_load(teks)


def test_env_contoh_yang_tersalin_apa_adanya_diberi_peringatan(tmp_path, monkeypatch):
    from asisten_bei import operasional

    monkeypatch.setattr(operasional, "PENYARING", operasional.PenyaringRahasia())  # token contoh tidak bocor ke uji lain
    shutil.copy2(CONTOH / "env.contoh", tmp_path / ".env")
    hasil = operasional.cek_env(tmp_path, telegram_aktif=True)
    assert len(hasil) == 1 and hasil[0][0] == "PERINGATAN" and "masih nilai contoh" in hasil[0][1]
