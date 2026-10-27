"""T-KL bagian run (SPEC §8.6–§8.7, §19.3) dan T-GB bagian run harian (§16.6) lewat perintah `harian`.

Proyek sintetis `bantu_harian` (FIKTIF – KHUSUS UJI): data Senin–Jumat s/d 2025-06-30, holidays.csv uji hanya berisi
2026–2027 (tahun 2025 memakai Senin–Jumat). Jam sesi/pra-pembukaan di uji ini FIKTIF dan bukan jadwal BEI.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

JADWAL_FIKTIF = {
    "pasar.jadwal_sesi.terverifikasi": True,
    "pasar.jadwal_sesi.sumber": "FIKTIF – KHUSUS UJI (bukan jadwal BEI)",
    "pasar.jadwal_sesi.tanggal_cek": "2025-01-02",
    "pasar.jadwal_sesi.jam_penutupan": "15:00",
    "pasar.jadwal_sesi.jam_pra_pembukaan": "08:45",
}


def md(p, t: str) -> str:
    return (p.folder_laporan / t / "laporan.md").read_text(encoding="utf-8")


def baris_status(teks: str, strategi: str) -> str:
    return next(b for b in teks.splitlines() if b.startswith(f"| {strategi} |"))


# ---------------------------------------------------------------------------
# T-KL bagian run
# ---------------------------------------------------------------------------


def test_run_sabtu_setelah_jumat_terlewat_t_jumat(proyek):
    h = proyek.harian("2025-05-17T10:00:00+07:00")
    assert h.kode == 0
    assert "Tanggal data t: 2025-05-16" in h.teks and "untuk hari bursa t+1: 2025-05-19" in h.teks
    assert (proyek.folder_laporan / "2025-05-16" / "laporan.md").is_file()


def test_libur_di_holidays_t_hari_bursa_sebelumnya(proyek):
    proyek.tulis_input("holidays.csv", "tanggal,keterangan\n2025-05-16,FIKTIF – KHUSUS UJI\n2026-12-25,FIKTIF\n")
    h = proyek.harian("2025-05-16")
    assert "Tanggal data t: 2025-05-15" in h.teks and "untuk hari bursa t+1: 2025-05-19" in h.teks


@pytest.mark.parametrize("jam, t", [("19:00", "2025-05-15"), ("19:30", "2025-05-16")])
def test_batas_bar_final_tidak_terverifikasi_jam_run(proyek, jam, t):
    h = proyek.harian(f"2025-05-16T{jam}:00+07:00")
    assert f"Tanggal data t: {t}" in h.teks


@pytest.mark.parametrize("jam, t", [("15:30", "2025-05-15"), ("16:00", "2025-05-16")])
def test_batas_bar_final_terverifikasi_penutupan_plus_jeda(proyek, jam, t):
    proyek.ubah_config(JADWAL_FIKTIF)
    h = proyek.harian(f"2025-05-16T{jam}:00+07:00")
    assert f"Tanggal data t: {t}" in h.teks


def test_banner_run_pada_hari_t1_tidak_terverifikasi(proyek):
    h = proyek.harian("2025-05-19T08:00:00+07:00")
    assert "Tanggal data t: 2025-05-16" in h.teks
    assert "RUN PADA HARI t+1 — pastikan sendiri masih sebelum pra-pembukaan" in h.teks
    assert "RUN PADA HARI t+1" not in proyek.harian("2025-05-16T21:00:00+07:00").teks


@pytest.mark.parametrize("jam, banner", [("08:00", False), ("09:00", True)])
def test_banner_terlambat_terverifikasi(proyek, jam, banner):
    proyek.ubah_config(JADWAL_FIKTIF)
    h = proyek.harian(f"2025-05-19T{jam}:00+07:00")
    assert ("TERLAMBAT — rencana mungkin tidak bisa dieksekusi hari ini" in h.teks) == banner
    assert "RUN PADA HARI t+1" not in h.teks
    run = proyek.tabel("run")[-1]
    assert run["terlambat"] == int(banner)


def test_peringatan_holidays_tahun_berjalan_dan_banner_t1_tanpa_kalender(proyek):
    h = proyek.harian("2025-05-16")
    teks = md(proyek, "2025-05-16")
    assert "holidays.csv belum berisi tanggal libur tahun 2025" in teks
    assert "Tanggal t+1 belum terverifikasi terhadap kalender libur resmi" in teks
    assert h.kode == 0


def test_jadwal_hari_libur_saat_t_sudah_lengkap_kode_0(proyek):
    assert proyek.harian("2025-05-16").kode == 0
    n = len(proyek.tabel("run"))
    h = proyek.harian("2025-05-17T19:45:00+07:00", "--jadwal")
    assert h.kode == 0 and "Tidak ada data baru" in h.teks
    assert len(proyek.tabel("run")) == n


def test_jadwal_mulai_sebelum_batas_final_menunggu_bukan_keluar(proyek):
    h = proyek.harian("2025-05-16T18:00:00+07:00", "--jadwal")
    assert h.kode == 0
    assert "Menunggu batas bar final 2025-05-16 19:30" in h.teks
    assert "Tanggal data t: 2025-05-16" in h.teks and "Dibuat: 2025-05-16 19:30:00" in h.teks


def test_jadwal_menunggu_data_ihsg_lalu_tidak_lengkap(proyek):
    proyek.hapus_bar("IHSG", "2025-05-16")
    h = proyek.harian("2025-05-16", "--jadwal")
    assert h.kode == 2
    assert h.teks.count("menunggu 15 menit") == 3
    assert "Dibuat: 2025-05-16 20:45:00" in h.teks
    assert "DATA TIDAK LENGKAP" in h.teks and "JANGAN BELI" in h.teks


def test_belum_tersedia_dengan_petunjuk_libur(proyek):
    for k in ("IHSG", "AAAA", "BBBB", "CCCC", "DDDD"):
        proyek.hapus_bar(k, "2025-05-16")
    h = proyek.harian("2025-05-16")
    assert h.kode == 2
    teks = md(proyek, "2025-05-16")
    assert "DATA BELUM TERSEDIA" in teks and "mungkin hari libur yang belum ada di holidays.csv" in teks
    assert "Tidak ada REKOMENDASI" not in teks and "tidak ada rencana beli" in teks


def test_tidak_lengkap_sebagian_saham_tanpa_bar_t_kode_0_data_basi(proyek):
    proyek.ubah_config({"data.validasi.min_cakupan_universe_pct": 50, "kondisi_pasar.min_cakupan_breadth_pct": 50})
    proyek.hapus_bar("CCCC", "2025-05-16")
    h = proyek.harian("2025-05-16")
    assert h.kode == 0 and "Status data run: TIDAK_LENGKAP" in h.teks
    teks = md(proyek, "2025-05-16")
    assert "| CCCC | DATA_BASI |" in teks


@pytest.mark.parametrize("tanggal", ["2025-05-17", "2025-06-02"])
def test_tanggal_replay_tidak_sah_kode_3(proyek, tanggal):
    h = proyek.harian("2025-05-20", "--tanggal", tanggal)
    assert h.kode == 3 and "--tanggal" in h.teks


def test_tanggal_format_salah_kode_3(proyek):
    assert proyek.harian("2025-05-20", "--tanggal", "20/05/2025").kode == 3


# ---------------------------------------------------------------------------
# T-GB bagian run harian
# ---------------------------------------------------------------------------


def test_tanpa_gate_status_semua_bayangan_gerbang(proyek):
    (proyek.akar / "output" / "backtest" / "gate_status.json").unlink()
    h = proyek.harian("2025-05-20")
    assert h.kode == 0
    teks = md(proyek, "2025-05-20")
    for s in ("PB", "BO", "MR"):
        assert "BAYANGAN_GERBANG" in baris_status(teks, s) and "Jalankan backtest.bat" in baris_status(teks, s)
    kandidat = pd.read_csv(proyek.folder_laporan / "2025-05-20" / "kandidat.csv", comment="#", dtype=str)
    assert len(kandidat) and set(kandidat["status_sinyal"]) <= {"BAYANGAN_GERBANG", "STOP_TERLALU_JAUH"}
    assert "REKOMENDASI" not in set(kandidat["status_sinyal"])


def test_ubah_strategi_bo_hanya_membatalkan_bo(proyek):
    proyek.ubah_config({"strategi.bo.trailing_atr": 2.5})
    proyek.harian("2025-05-20")
    teks = md(proyek, "2025-05-20")
    assert "BAYANGAN_GERBANG" in baris_status(teks, "BO") and "strategi.bo" in baris_status(teks, "BO")
    assert "| LOLOS |" in baris_status(teks, "PB") and "| LOLOS |" in baris_status(teks, "MR")


@pytest.mark.parametrize("ubah, bagian", [({"indikator.atr_periode": 6}, "indikator"),
                                          ({"gerbang.min_pf_r_holdout": 0.1}, "gerbang")])
def test_ubah_config_terhash_membatalkan_semua(proyek, ubah, bagian):
    proyek.ubah_config(ubah)
    proyek.harian("2025-05-20")
    teks = md(proyek, "2025-05-20")
    for s in ("PB", "BO", "MR"):
        assert "BAYANGAN_GERBANG" in baris_status(teks, s) and bagian in baris_status(teks, s)


def test_ubah_holidays_membatalkan_dan_jadwal_sesi_tidak(proyek):
    proyek.ubah_config(JADWAL_FIKTIF)
    proyek.harian("2025-05-20")
    assert "| LOLOS |" in baris_status(md(proyek, "2025-05-20"), "PB")
    proyek.tulis_input("holidays.csv", "tanggal,keterangan\n2026-12-25,FIKTIF – KHUSUS UJI\n2027-01-01,FIKTIF\n"
                                       "2027-12-24,FIKTIF\n")
    proyek.harian("2025-05-21")
    b = baris_status(md(proyek, "2025-05-21"), "PB")
    assert "BAYANGAN_GERBANG" in b and "holidays.csv" in b


def test_gerbang_kedaluwarsa(proyek):
    path = proyek.akar / "output" / "backtest" / "gate_status.json"
    isi = json.loads(path.read_text(encoding="utf-8"))
    for e in isi["strategi"].values():
        e["tanggal_data_terakhir"] = "2025-01-02"
    path.write_text(json.dumps(isi), encoding="utf-8")
    proyek.harian("2025-05-20")
    b = baris_status(md(proyek, "2025-05-20"), "MR")
    assert "BAYANGAN_GERBANG" in b and "kedaluwarsa" in b


def test_gerbang_lolos_menghasilkan_rekomendasi(proyek):
    h = proyek.harian("2025-06-30")
    assert "BELI AAAA" in h.teks
    kandidat = pd.read_csv(proyek.folder_laporan / "2025-06-30" / "kandidat.csv", comment="#", dtype=str)
    assert "REKOMENDASI" in set(kandidat["status_sinyal"])
