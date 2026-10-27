"""Uji T-KL (SPEC §22.1) untuk `kalender.py` — §6.7, §8.6, §8.7 (tanpa bagian run/CLI).

Semua tanggal libur di berkas ini FIKTIF – KHUSUS UJI (bukan kalender libur BEI), dan jadwal
sesi "terverifikasi" memakai jam fiktif berlabel FIKTIF (P4: aturan BEI tidak diisi dari ingatan).
Config uji dibangun dari config.yaml rilis lalu divalidasi lewat `config.validasi_dict`.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from asisten_bei import waktu
from asisten_bei.config import Config, validasi_dict
from asisten_bei.kalender import (
    BANNER_RUN_T1,
    BANNER_T1_TANPA_KALENDER,
    BANNER_TERLAMBAT,
    Kalender,
    Peringatan,
    bandingkan_kalender,
    bangun_kalender,
    banner_keterlambatan,
    banner_t1_tanpa_kalender,
    batas_bar_final,
    hitung_kalender_turunan,
    peringatan_libur,
    tanggal_final_terakhir,
    tentukan_t,
    tentukan_t1,
)
from asisten_bei.waktu import ZONA

AKAR_PROYEK = Path(__file__).resolve().parents[1]
_CONFIG_RILIS = yaml.safe_load((AKAR_PROYEK / "tests" / "fixtures" / "config_rilis.yaml").read_text(encoding="utf-8"))

FIKTIF = "FIKTIF – KHUSUS UJI"
# Jadwal sesi fiktif (bukan jadwal BEI) untuk menguji cabang "terverifikasi".
JADWAL_FIKTIF = {
    "terverifikasi": True,
    "sumber": f"{FIKTIF} (bukan jadwal resmi)",
    "tanggal_cek": "2026-10-01",
    "jam_penutupan": "16:00",
    "jam_pra_pembukaan": "08:45",
}

# Kalender fiktif 2026: libur tunggal Selasa 20 Okt, dan libur panjang Rabu 4 Nov – Selasa 10 Nov
# (Rabu–Jumat + akhir pekan + Senin–Selasa).
LIBUR_TUNGGAL = date(2026, 10, 20)
LIBUR_PANJANG = [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)]
LIBUR_2026 = {LIBUR_TUNGGAL: FIKTIF, **{d: FIKTIF for d in LIBUR_PANJANG}}
LIBUR_2026_2027 = {**LIBUR_2026, date(2027, 1, 1): FIKTIF}


def buat_cfg(jadwal_sesi: dict | None = None, **data_ubah) -> Config:
    """Config rilis dengan perubahan: `jadwal_sesi` (update dict), `jam_run`, `jeda_data_final_menit`."""
    data = copy.deepcopy(_CONFIG_RILIS)
    if jadwal_sesi:
        data["pasar"]["jadwal_sesi"].update(jadwal_sesi)
    if "jam_run" in data_ubah:
        data["jadwal"]["jam_run"] = data_ubah.pop("jam_run")
    if "jeda_data_final_menit" in data_ubah:
        data["data"]["jeda_data_final_menit"] = data_ubah.pop("jeda_data_final_menit")
    assert not data_ubah, data_ubah
    return validasi_dict(data)


CFG = buat_cfg()  # jadwal_sesi belum terverifikasi (rilis): batas bar final = jam_run 19:30
CFG_V = buat_cfg(JADWAL_FIKTIF)  # terverifikasi fiktif: 16:00 + 60 menit = 17:00


def wib(teks: str) -> datetime:
    """"2026-10-15 19:30[:ss]" → datetime aware WIB."""
    return datetime.fromisoformat(teks).replace(tzinfo=ZONA)


@pytest.fixture
def kal() -> Kalender:
    return Kalender(LIBUR_2026_2027)


# ---------------------------------------------------------------------------
# Kalender manual: akhir pekan, libur, libur panjang
# ---------------------------------------------------------------------------


def test_akhir_pekan_bukan_hari_bursa(kal):
    # 2026-10-12 Senin … 2026-10-18 Minggu
    minggu = [date(2026, 10, 12) + timedelta(days=i) for i in range(7)]
    assert [kal.adalah_hari_bursa_manual(d) for d in minggu] == [True] * 5 + [False] * 2
    assert [kal.adalah_hari_bursa(d) for d in minggu] == [True] * 5 + [False] * 2
    assert kal.hari_bursa_berikut(date(2026, 10, 16)) == date(2026, 10, 19)
    assert kal.hari_bursa_sebelum(date(2026, 10, 19)) == date(2026, 10, 16)
    assert kal.hari_bursa_berikut(date(2026, 10, 17), manual=True) == date(2026, 10, 19)


def test_libur_holidays_csv(kal):
    assert not kal.adalah_hari_bursa_manual(LIBUR_TUNGGAL)
    assert not kal.adalah_hari_bursa(LIBUR_TUNGGAL)
    assert kal.adalah_hari_bursa(date(2026, 10, 19)) and kal.adalah_hari_bursa(date(2026, 10, 21))
    assert kal.hari_bursa_berikut(date(2026, 10, 19)) == date(2026, 10, 21)
    assert kal.hari_bursa_sebelum(date(2026, 10, 21)) == date(2026, 10, 19)
    assert kal.libur[LIBUR_TUNGGAL] == FIKTIF


def test_libur_panjang_beberapa_hari_berturut_dan_akhir_pekan(kal):
    assert kal.hari_bursa_berikut(date(2026, 11, 3)) == date(2026, 11, 11)
    assert kal.hari_bursa_sebelum(date(2026, 11, 11)) == date(2026, 11, 3)
    assert kal.daftar_hari_bursa(date(2026, 11, 2), date(2026, 11, 13)) == [
        date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)
    ]
    assert tentukan_t1(kal, date(2026, 11, 3)) == date(2026, 11, 11)
    # Run di tengah libur panjang (Sabtu / Senin libur) → t = hari bursa terakhir sebelum libur.
    assert tentukan_t(CFG, kal, wib("2026-11-07 10:00")) == date(2026, 11, 3)
    assert tentukan_t(CFG, kal, wib("2026-11-09 20:00")) == date(2026, 11, 3)
    assert tentukan_t(CFG, kal, wib("2026-11-11 19:29:59")) == date(2026, 11, 3)
    assert tentukan_t(CFG, kal, wib("2026-11-11 19:30")) == date(2026, 11, 11)


def test_daftar_dan_indeks_hari_bursa(kal):
    idx = kal.indeks_hari_bursa(date(2026, 10, 15), date(2026, 10, 21))
    assert isinstance(idx, pd.DatetimeIndex) and idx.name == "tanggal"
    assert list(idx) == [pd.Timestamp(d) for d in ("2026-10-15", "2026-10-16", "2026-10-19", "2026-10-21")]
    assert kal.daftar_hari_bursa(date(2026, 10, 17), date(2026, 10, 18)) == []
    assert kal.daftar_hari_bursa(date(2026, 10, 21), date(2026, 10, 15)) == []


def test_libur_sebagai_himpunan_tanggal():
    k = Kalender({LIBUR_TUNGGAL})
    assert not k.adalah_hari_bursa(LIBUR_TUNGGAL)
    assert k.tahun_manual == frozenset({2026})
    assert k.tercakup_manual(2026) and not k.tercakup_manual(2027)


# ---------------------------------------------------------------------------
# Batas bar final & penentuan t (§8.6)
# ---------------------------------------------------------------------------


def test_batas_bar_final_tidak_terverifikasi_memakai_jam_run():
    assert batas_bar_final(CFG, date(2026, 10, 15)) == wib("2026-10-15 19:30")
    assert batas_bar_final(CFG, date(2026, 10, 15)).tzinfo == ZONA
    # jam_penutupan terisi tetapi belum terverifikasi → tetap diabaikan.
    cfg = buat_cfg({"jam_penutupan": "16:00", "jam_pra_pembukaan": "08:45"})
    assert batas_bar_final(cfg, date(2026, 10, 15)) == wib("2026-10-15 19:30")
    # jam_run dari config (P4), bukan konstanta.
    assert batas_bar_final(buat_cfg(jam_run="21:15"), date(2026, 10, 15)) == wib("2026-10-15 21:15")


def test_batas_bar_final_terverifikasi_penutupan_plus_jeda():
    assert batas_bar_final(CFG_V, date(2026, 10, 15)) == wib("2026-10-15 17:00")
    cfg = buat_cfg(JADWAL_FIKTIF, jeda_data_final_menit=30, jam_run="21:00")
    assert batas_bar_final(cfg, date(2026, 10, 15)) == wib("2026-10-15 16:30")  # jam_run tidak dipakai
    cfg0 = buat_cfg(JADWAL_FIKTIF, jeda_data_final_menit=0)
    assert batas_bar_final(cfg0, date(2026, 10, 15)) == wib("2026-10-15 16:00")


def test_batas_bar_final_melewati_tengah_malam():
    cfg = buat_cfg(JADWAL_FIKTIF, jeda_data_final_menit=540)
    assert batas_bar_final(cfg, date(2026, 10, 15)) == wib("2026-10-16 01:00")
    assert tentukan_t(cfg, Kalender({}), wib("2026-10-15 10:00")) == date(2026, 10, 14)
    # 00:30 Jumat: bar Kamis baru final pukul 01:00 → t masih Rabu; pukul 01:00 → Kamis.
    assert tentukan_t(cfg, Kalender({}), wib("2026-10-16 00:30")) == date(2026, 10, 14)
    assert tentukan_t(cfg, Kalender({}), wib("2026-10-16 01:00")) == date(2026, 10, 15)
    assert tanggal_final_terakhir(cfg, wib("2026-10-16 00:30")) == date(2026, 10, 14)


def test_batas_terverifikasi_tanpa_jam_penutupan_kembali_ke_jam_run():
    cfg = buat_cfg({**JADWAL_FIKTIF, "jam_penutupan": None})
    assert batas_bar_final(cfg, date(2026, 10, 15)) == wib("2026-10-15 19:30")


@pytest.mark.parametrize(
    "sekarang, harapan",
    [
        ("2026-10-15 00:00", date(2026, 10, 14)),
        ("2026-10-15 16:30", date(2026, 10, 14)),
        ("2026-10-15 19:29:59", date(2026, 10, 14)),
        ("2026-10-15 19:30", date(2026, 10, 15)),
        ("2026-10-15 23:59:59", date(2026, 10, 15)),
        ("2026-10-19 08:00", date(2026, 10, 16)),  # Senin pagi → Jumat
    ],
)
def test_t_sebelum_sesudah_batas_tidak_terverifikasi(kal, sekarang, harapan):
    assert tentukan_t(CFG, kal, wib(sekarang)) == harapan


@pytest.mark.parametrize(
    "sekarang, harapan",
    [
        ("2026-10-15 16:00", date(2026, 10, 14)),
        ("2026-10-15 16:59:59", date(2026, 10, 14)),
        ("2026-10-15 17:00", date(2026, 10, 15)),
        ("2026-10-15 18:00", date(2026, 10, 15)),
    ],
)
def test_t_sebelum_sesudah_batas_terverifikasi(kal, sekarang, harapan):
    assert tentukan_t(CFG_V, kal, wib(sekarang)) == harapan


def test_t_zona_waktu_lain_dan_naive(kal):
    # Windows berzona UTC: 12:29:59Z = 19:29:59 WIB; 12:30Z = 19:30 WIB.
    assert tentukan_t(CFG, kal, datetime.fromisoformat("2026-10-15T12:29:59+00:00")) == date(2026, 10, 14)
    assert tentukan_t(CFG, kal, datetime.fromisoformat("2026-10-15T12:30:00+00:00")) == date(2026, 10, 15)
    # 2026-10-15 20:00 di New York = 2026-10-16 07:00 WIB (Jumat pagi) → t = Kamis.
    assert tentukan_t(CFG, kal, datetime.fromisoformat("2026-10-15T20:00:00-04:00")) == date(2026, 10, 15)
    # naive dianggap WIB
    assert tentukan_t(CFG, kal, datetime(2026, 10, 15, 19, 30)) == date(2026, 10, 15)


def test_t_dari_waktu_virtual(kal, atur_waktu):
    assert tentukan_t(CFG, kal, waktu.sekarang()) == date(2026, 10, 15)  # 20:00 WIB (conftest)
    atur_waktu("2026-10-15T19:00:00+07:00")
    assert tentukan_t(CFG, kal, waktu.sekarang()) == date(2026, 10, 14)
    waktu.tidur(30 * 60)  # menunggu sampai batas bar final (§8.6 "mulai terlalu awal")
    assert tentukan_t(CFG, kal, waktu.sekarang()) == date(2026, 10, 15)


def test_t_pada_hari_libur_dan_akhir_pekan(kal):
    assert tentukan_t(CFG, kal, wib("2026-10-20 20:00")) == date(2026, 10, 19)  # libur
    assert tentukan_t(CFG, kal, wib("2026-10-21 10:00")) == date(2026, 10, 19)  # pagi setelah libur
    assert tentukan_t(CFG, kal, wib("2026-10-18 23:59")) == date(2026, 10, 16)  # Minggu
    assert tentukan_t(CFG, kal, wib("2026-10-19 00:00")) == date(2026, 10, 16)  # Senin dini hari


def test_jumat_terlewat_dijalankan_sabtu_t_jumat(kal):
    assert tentukan_t(CFG, kal, wib("2026-10-17 10:00")) == date(2026, 10, 16)
    assert tentukan_t(CFG, kal, wib("2026-10-17 23:59")) == date(2026, 10, 16)
    assert tentukan_t(CFG_V, kal, wib("2026-10-18 09:00")) == date(2026, 10, 16)
    # Jumat libur (fiktif) → run Sabtu memproses Kamis.
    k = Kalender({date(2026, 10, 16): FIKTIF})
    assert tentukan_t(CFG, k, wib("2026-10-17 10:00")) == date(2026, 10, 15)


def test_jam_run_dari_config_mengubah_t(kal):
    cfg = buat_cfg(jam_run="21:00")
    assert tentukan_t(cfg, kal, wib("2026-10-15 20:00")) == date(2026, 10, 14)
    assert tentukan_t(cfg, kal, wib("2026-10-15 21:00")) == date(2026, 10, 15)


def _bar_volume(tanggal_volume: dict[str, float]) -> pd.DataFrame:
    """Bar mentah minimal (harga tetap) dengan volume per tanggal."""
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t in tanggal_volume], name="tanggal")
    n = len(idx)
    harga = np.full(n, 1000.0)
    return pd.DataFrame(
        {"open": harga, "high": harga, "low": harga, "close": harga,
         "volume": np.array(list(tanggal_volume.values()), dtype="float64")},
        index=idx,
    )


def test_t_dan_t1_tidak_ditentukan_kalender_turunan():
    """holidays.csv belum berisi 2026; kalender turunan (data belum lengkap) menyatakan 14–16 Okt
    bukan hari bursa. t dan t+1 tetap memakai kalender manual (§8.7)."""
    turunan = {date(2026, 10, 12), date(2026, 10, 13)}
    k = Kalender({date(2025, 12, 25): FIKTIF}, turunan, (date(2026, 10, 12), date(2026, 10, 16)))
    assert not k.adalah_hari_bursa(date(2026, 10, 15))  # efektif (historis)
    assert k.adalah_hari_bursa_manual(date(2026, 10, 15))
    assert tentukan_t(CFG, k, wib("2026-10-15 20:00")) == date(2026, 10, 15)
    assert tentukan_t(CFG, k, wib("2026-10-15 10:00")) == date(2026, 10, 14)
    assert tentukan_t1(k, date(2026, 10, 15)) == date(2026, 10, 16)
    assert k.hari_bursa_berikut(date(2026, 10, 13)) == date(2026, 10, 19)  # efektif berbeda


# ---------------------------------------------------------------------------
# t+1 dan banner keterlambatan (§8.7)
# ---------------------------------------------------------------------------


def test_teks_banner_sesuai_spec():
    assert BANNER_TERLAMBAT == "TERLAMBAT — rencana mungkin tidak bisa dieksekusi hari ini"
    assert BANNER_RUN_T1 == "RUN PADA HARI t+1 — pastikan sendiri masih sebelum pra-pembukaan"
    assert BANNER_T1_TANPA_KALENDER == "Tanggal t+1 belum terverifikasi terhadap kalender libur resmi"


@pytest.mark.parametrize(
    "t, harapan",
    [
        (date(2026, 10, 15), date(2026, 10, 16)),
        (date(2026, 10, 16), date(2026, 10, 19)),  # Jumat → Senin
        (date(2026, 10, 19), date(2026, 10, 21)),  # melewati libur Selasa
        (date(2026, 11, 3), date(2026, 11, 11)),  # libur panjang
        (date(2026, 12, 31), date(2027, 1, 4)),  # 2027-01-01 libur fiktif, lalu akhir pekan
    ],
)
def test_tentukan_t1(kal, t, harapan):
    assert tentukan_t1(kal, t) == harapan


@pytest.mark.parametrize(
    "sekarang, harapan",
    [
        ("2026-10-15 20:00", None),  # run pada t sendiri
        ("2026-10-16 00:00", BANNER_RUN_T1),
        ("2026-10-16 06:00", BANNER_RUN_T1),
        ("2026-10-16 08:45", BANNER_RUN_T1),
        ("2026-10-16 23:00", BANNER_RUN_T1),  # setiap run pada tanggal t+1
        ("2026-10-17 10:00", None),  # setelah t+1: tidak dinilai
    ],
)
def test_banner_run_t1_tidak_terverifikasi(kal, sekarang, harapan):
    assert banner_keterlambatan(CFG, kal, date(2026, 10, 15), wib(sekarang)) == harapan


@pytest.mark.parametrize(
    "sekarang, harapan",
    [
        ("2026-10-15 20:00", None),
        ("2026-10-16 06:00", None),
        ("2026-10-16 08:44:59", None),
        ("2026-10-16 08:45", BANNER_TERLAMBAT),
        ("2026-10-16 12:00", BANNER_TERLAMBAT),
    ],
)
def test_banner_terlambat_terverifikasi(kal, sekarang, harapan):
    assert banner_keterlambatan(CFG_V, kal, date(2026, 10, 15), wib(sekarang)) == harapan


def test_banner_terverifikasi_tanpa_jam_pra_pembukaan_memakai_banner_t1(kal):
    cfg = buat_cfg({**JADWAL_FIKTIF, "jam_pra_pembukaan": None})
    assert banner_keterlambatan(cfg, kal, date(2026, 10, 15), wib("2026-10-16 06:00")) == BANNER_RUN_T1


def test_banner_run_akhir_pekan_dan_libur(kal):
    # Jumat terlewat, run Sabtu: t+1 = Senin → belum terlambat.
    assert banner_keterlambatan(CFG, kal, date(2026, 10, 16), wib("2026-10-17 10:00")) is None
    assert banner_keterlambatan(CFG_V, kal, date(2026, 10, 16), wib("2026-10-18 21:00")) is None
    # Run Senin pagi untuk t = Jumat → t+1.
    assert banner_keterlambatan(CFG, kal, date(2026, 10, 16), wib("2026-10-19 07:00")) == BANNER_RUN_T1
    assert banner_keterlambatan(CFG_V, kal, date(2026, 10, 16), wib("2026-10-19 09:00")) == BANNER_TERLAMBAT
    # t = Senin, Selasa libur: run Selasa bukan t+1; run Rabu pagi adalah t+1.
    assert banner_keterlambatan(CFG, kal, date(2026, 10, 19), wib("2026-10-20 20:00")) is None
    assert banner_keterlambatan(CFG, kal, date(2026, 10, 19), wib("2026-10-21 06:00")) == BANNER_RUN_T1


def test_banner_keterlambatan_zona_waktu_lain(kal):
    # 2026-10-16 01:45Z = 08:45 WIB (t+1, terverifikasi) → TERLAMBAT.
    assert banner_keterlambatan(
        CFG_V, kal, date(2026, 10, 15), datetime.fromisoformat("2026-10-16T01:45:00+00:00")
    ) == BANNER_TERLAMBAT
    # 2026-10-15 23:00Z = 2026-10-16 06:00 WIB → tanggal WIB yang dinilai, bukan tanggal UTC.
    assert banner_keterlambatan(
        CFG, kal, date(2026, 10, 15), datetime.fromisoformat("2026-10-15T23:00:00+00:00")
    ) == BANNER_RUN_T1


# ---------------------------------------------------------------------------
# Peringatan holidays.csv (§6.7)
# ---------------------------------------------------------------------------


def test_peringatan_libur_lengkap_kosong():
    assert peringatan_libur(Kalender(LIBUR_2026_2027), wib("2026-10-15 20:00")) == []
    assert peringatan_libur(Kalender(LIBUR_2026_2027), waktu.sekarang()) == []


def test_peringatan_tahun_berikutnya_banner_sejak_desember():
    k = Kalender(LIBUR_2026)
    okt = peringatan_libur(k, wib("2026-10-15 20:00"))
    assert len(okt) == 1 and okt[0].level == "PERINGATAN"
    assert "holidays.csv" in okt[0].teks and "2027" in okt[0].teks
    nov = peringatan_libur(k, wib("2026-11-30 23:59:59"))
    assert [p.level for p in nov] == ["PERINGATAN"]
    des = peringatan_libur(k, wib("2026-12-01 00:00"))
    assert des == [Peringatan("BANNER", okt[0].teks)]
    assert [p.level for p in peringatan_libur(k, wib("2026-12-31 23:00"))] == ["BANNER"]


def test_peringatan_tahun_berjalan_kosong():
    k = Kalender({date(2027, 1, 1): FIKTIF})
    hasil = peringatan_libur(k, wib("2026-10-15 20:00"))
    assert len(hasil) == 1 and hasil[0].level == "PERINGATAN" and "2026" in hasil[0].teks
    # Desember: tahun berjalan yang kosong tetap PERINGATAN (banner hanya untuk tahun berikutnya).
    assert [p.level for p in peringatan_libur(k, wib("2026-12-15 20:00"))] == ["PERINGATAN"]


def test_peringatan_tanpa_holidays_sama_sekali():
    k = Kalender({})
    okt = peringatan_libur(k, wib("2026-10-15 20:00"))
    assert [(p.level, "2026" in p.teks, "2027" in p.teks) for p in okt] == [
        ("PERINGATAN", True, False), ("PERINGATAN", False, True)
    ]
    des = peringatan_libur(k, wib("2026-12-02 08:00"))
    assert [p.level for p in des] == ["PERINGATAN", "BANNER"]


def test_peringatan_awal_tahun_baru():
    # Januari 2027 dengan holidays hanya 2026: tahun berjalan (2027) dan berikutnya (2028) kosong.
    hasil = peringatan_libur(Kalender(LIBUR_2026), wib("2027-01-04 20:00"))
    assert [(p.level, p.teks) for p in hasil] == [
        ("PERINGATAN", "holidays.csv belum berisi tanggal libur tahun 2027 — isi dari idx.co.id"),
        ("PERINGATAN", "holidays.csv belum berisi tanggal libur tahun 2028 — isi dari idx.co.id"),
    ]


def test_banner_t1_tanpa_kalender():
    k = Kalender(LIBUR_2026)
    assert banner_t1_tanpa_kalender(k, date(2026, 10, 16)) is None
    t1 = tentukan_t1(k, date(2026, 12, 31))
    assert t1 == date(2027, 1, 1)  # tanpa entri 2027: Senin–Jumat
    assert banner_t1_tanpa_kalender(k, t1) == BANNER_T1_TANPA_KALENDER
    assert banner_t1_tanpa_kalender(Kalender(LIBUR_2026_2027), date(2027, 1, 4)) is None
    assert banner_t1_tanpa_kalender(Kalender({}), date(2026, 10, 16)) == BANNER_T1_TANPA_KALENDER


# ---------------------------------------------------------------------------
# Kalender turunan dari breadth (§8.7)
# ---------------------------------------------------------------------------


def test_turunan_ambang_min_pct_inklusif():
    # 2024-03-04 (Senin): A & B tercatat pertama. 2024-03-05: hanya A bervolume → 1/2 = 50%.
    bar = {
        "A": _bar_volume({"2024-03-04": 100, "2024-03-05": 100, "2024-03-06": 100}),
        "B": _bar_volume({"2024-03-04": 100, "2024-03-05": 0, "2024-03-06": 0}),
        "C": _bar_volume({"2024-03-01": 100, "2024-03-05": 0, "2024-03-06": 0}),
    }
    # Dengan C (bar pertama 03-01): 03-05 → 1/3 (A) < 50 → bukan; tanpa C: 1/2 = 50 → hari bursa.
    tanpa_c = {k: v for k, v in bar.items() if k != "C"}
    hari, rentang = hitung_kalender_turunan(tanpa_c, 50, ())
    assert date(2024, 3, 5) in hari and date(2024, 3, 6) in hari
    assert rentang == (date(2024, 3, 4), date(2024, 3, 6))
    hari, _ = hitung_kalender_turunan(tanpa_c, 50.01, ())
    assert date(2024, 3, 5) not in hari
    hari, _ = hitung_kalender_turunan(bar, 50, ())
    assert date(2024, 3, 5) not in hari and date(2024, 3, 6) not in hari
    # 03-04: penyebut hanya {C} (bar pertama 03-01 < 03-04) dan C tanpa bar → 0/1 → bukan hari bursa.
    assert hari == {date(2024, 3, 1)}


def test_turunan_penyebut_hanya_saham_yang_sudah_tercatat():
    """D baru melantai 2024-03-12: pada 03-12 D tidak masuk penyebut maupun pembilang; mulai 03-13 masuk."""
    tgl = ["2024-03-11", "2024-03-12", "2024-03-13"]
    bar = {
        "A": _bar_volume({"2024-03-08": 1, "2024-03-11": 1, "2024-03-12": 1, "2024-03-13": 1}),
        "B": _bar_volume({"2024-03-08": 1, "2024-03-11": 1, "2024-03-12": 0, "2024-03-13": 0}),
        "C": _bar_volume({"2024-03-08": 1, "2024-03-11": 1, "2024-03-12": 0, "2024-03-13": 0}),
        "D": _bar_volume({"2024-03-12": 5, "2024-03-13": 5}),
    }
    hari, _ = hitung_kalender_turunan(bar, 50, ())
    # 03-11: 3/3; 03-12: A saja dari {A,B,C} = 33% (D tidak dihitung); 03-13: A, D dari 4 = 50%.
    assert [date.fromisoformat(t) in hari for t in tgl] == [True, False, True]


def test_turunan_penyebut_nol():
    # Hari pertama data: belum ada saham yang bar pertamanya < d → hari bursa bila ada yang bervolume.
    hari, rentang = hitung_kalender_turunan({"A": _bar_volume({"2024-03-04": 10})}, 50, ())
    assert hari == {date(2024, 3, 4)} and rentang == (date(2024, 3, 4), date(2024, 3, 4))
    hari, rentang = hitung_kalender_turunan(
        {"A": _bar_volume({"2024-03-04": 0}), "B": _bar_volume({"2024-03-04": np.nan})}, 50, ()
    )
    assert hari == set() and rentang == (date(2024, 3, 4), date(2024, 3, 4))


def test_turunan_saham_tercatat_tanpa_bar_dihitung_di_penyebut():
    # C tercatat sejak 03-01 (dan masih punya bar 03-05) tetapi tidak punya bar 03-04; A, B baru tercatat
    # 03-04 → penyebut 03-04 = {C} → 0/1 → bukan hari bursa. 03-05: A, B sudah berakhir (bar terakhir
    # 03-04) → penyebut {C} → 1/1 → hari bursa. 03-01: penyebut kosong, C aktif → hari bursa.
    bar = {
        "A": _bar_volume({"2024-03-04": 100}),
        "B": _bar_volume({"2024-03-04": 100}),
        "C": _bar_volume({"2024-03-01": 100, "2024-03-05": 100}),
    }
    hari, _ = hitung_kalender_turunan(bar, 50, ())
    assert hari == {date(2024, 3, 1), date(2024, 3, 5)}


def test_turunan_saham_yang_datanya_berakhir_tidak_ikut_menilai():
    # §8.7: C berakhir 03-01 (delisting / cache basi) → tidak ikut penyebut sesudahnya; 03-04 = 2/2.
    bar = {
        "A": _bar_volume({"2024-02-29": 100, "2024-03-04": 100}),
        "B": _bar_volume({"2024-02-29": 100, "2024-03-04": 100}),
        "C": _bar_volume({"2024-02-29": 100, "2024-03-01": 100}),
    }
    hari, _ = hitung_kalender_turunan(bar, 50, ())
    assert date(2024, 3, 4) in hari
    # 03-01: penyebut {A, B, C} (A, B masih punya bar sesudahnya), hanya C aktif → 1/3 < 50% → bukan hari bursa.
    assert date(2024, 3, 1) not in hari
    # Sebagai pembanding: tanpa C, 03-04 tetap hari bursa; dengan C yang masih aktif tetapi tanpa bar → 2/3.
    bar["C"] = _bar_volume({"2024-02-29": 100, "2024-03-05": 100})
    hari, _ = hitung_kalender_turunan(bar, 70, ())
    assert date(2024, 3, 4) not in hari  # 2/3 < 70%


def test_turunan_volume_nan_nol_duplikat_akhir_pekan_dan_tahun_dikecualikan():
    a = _bar_volume({"2024-03-04": 1, "2024-03-05": np.nan, "2024-03-06": 1, "2024-03-09": 9})
    b = _bar_volume({"2024-03-04": 1, "2024-03-05": 1, "2024-03-06": 1, "2024-03-09": 9})
    # duplikat tanggal pada A (03-06 volume 0 lalu > 0) → cukup salah satu bervolume
    a = pd.concat([a, _bar_volume({"2024-03-06": 0})]).sort_index(kind="stable")
    bar = {"A": a, "B": b, "C": _bar_volume({"2024-03-04": 1, "2024-03-05": 0, "2024-03-06": 0})}
    hari, rentang = hitung_kalender_turunan(bar, 60, ())
    # 03-05: hanya B (NaN & 0 tidak aktif) = 1/3 < 60; 03-06: A (duplikat), B = 2/3 ≥ 60.
    assert hari == {date(2024, 3, 4), date(2024, 3, 6)}
    assert date(2024, 3, 9) not in hari  # Sabtu tidak pernah hari bursa
    assert rentang == (date(2024, 3, 4), date(2024, 3, 9))
    hari, _ = hitung_kalender_turunan(bar, 60, [2024])
    assert hari == set()


def test_turunan_kosong_dan_seri_kosong_diabaikan():
    assert hitung_kalender_turunan({}, 50, ()) == (set(), None)
    kosong = _bar_volume({})
    assert hitung_kalender_turunan({"A": kosong, "B": None}, 50, ()) == (set(), None)
    hari, _ = hitung_kalender_turunan({"A": kosong, "B": _bar_volume({"2024-03-04": 1})}, 50, ())
    assert hari == {date(2024, 3, 4)}


def test_turunan_min_pct_dari_config_decimal():
    min_pct = CFG.data.validasi.min_hari_kalender_historis_pct  # Decimal("50")
    bar = {
        "A": _bar_volume({"2024-03-04": 1, "2024-03-05": 1}),
        "B": _bar_volume({"2024-03-04": 1, "2024-03-05": 0}),
    }
    hari, _ = hitung_kalender_turunan(bar, min_pct, ())
    assert date(2024, 3, 5) in hari


# ---------------------------------------------------------------------------
# Kalender gabungan: prioritas manual, rentang turunan (§8.7)
# ---------------------------------------------------------------------------


def _breadth_akhir_tahun() -> dict[str, pd.DataFrame]:
    """4 saham breadth, semua hari kerja 2025-12-01 … 2026-01-16, dengan pengecualian:
    - 2025-12-25: tidak ada bar sama sekali (libur yang hanya tercermin di data);
    - 2025-12-26: hanya A bervolume (25% < 50);
    - 2026-01-01: tidak ada bar (tetapi tidak tercatat di holidays.csv);
    - 2026-01-02: semua bervolume (tetapi tercatat libur di holidays.csv)."""
    hari_kerja = [d.date() for d in pd.bdate_range("2025-12-01", "2026-01-16")]
    hasil = {}
    for kode in "ABCD":
        vol = {}
        for d in hari_kerja:
            if d in (date(2025, 12, 25), date(2026, 1, 1)):
                continue
            vol[d.isoformat()] = 0.0 if (d == date(2025, 12, 26) and kode != "A") else 1000.0
        hasil[kode] = _bar_volume(vol)
    return hasil


LIBUR_JAN = {date(2026, 1, 2): FIKTIF}


def test_bangun_kalender_turunan_untuk_tahun_tanpa_entri():
    min_pct = CFG.data.validasi.min_hari_kalender_historis_pct
    k = bangun_kalender(LIBUR_JAN, _breadth_akhir_tahun(), min_pct)
    assert (k.turunan_mulai, k.turunan_akhir) == (date(2025, 12, 1), date(2026, 1, 16))
    # 2025 tanpa entri → turunan
    assert not k.adalah_hari_bursa(date(2025, 12, 25))
    assert not k.adalah_hari_bursa(date(2025, 12, 26))
    assert k.adalah_hari_bursa(date(2025, 12, 24)) and k.adalah_hari_bursa(date(2025, 12, 29))
    assert k.adalah_hari_bursa_manual(date(2025, 12, 25))  # manual: Senin–Jumat
    assert k.hari_bursa_berikut(date(2025, 12, 24)) == date(2025, 12, 29)
    assert k.hari_bursa_berikut(date(2025, 12, 24), manual=True) == date(2025, 12, 25)
    assert k.daftar_hari_bursa(date(2025, 12, 22), date(2025, 12, 31)) == [
        date(2025, 12, 22), date(2025, 12, 23), date(2025, 12, 24), date(2025, 12, 29),
        date(2025, 12, 30), date(2025, 12, 31),
    ]


def test_prioritas_kalender_manual_di_tahun_bertanggal_libur():
    k = bangun_kalender(LIBUR_JAN, _breadth_akhir_tahun(), 50)
    # 2026 punya entri: kalender manual menang atas data breadth.
    assert not k.adalah_hari_bursa(date(2026, 1, 2))  # libur tercatat walau ada transaksi
    assert k.adalah_hari_bursa(date(2026, 1, 1))  # bukan libur tercatat walau tanpa transaksi
    assert k.hari_bursa_berikut(date(2025, 12, 31)) == date(2026, 1, 1)
    assert k.hari_bursa_berikut(date(2026, 1, 1)) == date(2026, 1, 5)


def test_tanggal_di_luar_rentang_turunan_senin_jumat():
    k = bangun_kalender(LIBUR_JAN, _breadth_akhir_tahun(), 50)
    assert k.adalah_hari_bursa(date(2025, 11, 28))  # Jumat sebelum data
    assert not k.adalah_hari_bursa(date(2025, 11, 29))  # Sabtu
    assert k.adalah_hari_bursa(date(2025, 1, 1))  # tanpa entri & di luar rentang → Senin–Jumat
    assert k.adalah_hari_bursa(date(2027, 1, 5))  # setelah data, tahun tanpa entri
    assert not k.adalah_hari_bursa(date(2027, 1, 9))
    # Rentang default dari turunan sendiri bila tidak diberikan.
    k2 = Kalender({}, {date(2024, 3, 5), date(2024, 3, 7)})
    assert (k2.turunan_mulai, k2.turunan_akhir) == (date(2024, 3, 5), date(2024, 3, 7))
    assert not k2.adalah_hari_bursa(date(2024, 3, 6))
    assert k2.adalah_hari_bursa(date(2024, 3, 4)) and k2.adalah_hari_bursa(date(2024, 3, 8))


def test_bangun_kalender_tanpa_breadth_senin_jumat():
    for breadth in (None, {}):
        k = bangun_kalender(LIBUR_JAN, breadth, 50)
        assert k.adalah_hari_bursa(date(2025, 12, 25))
        assert not k.adalah_hari_bursa(date(2026, 1, 2))
        assert k.turunan_mulai is None


# ---------------------------------------------------------------------------
# Perbandingan kalender manual vs turunan (`cek`, §8.7)
# ---------------------------------------------------------------------------


def test_bandingkan_kalender_mendeteksi_ketidakcocokan():
    breadth = _breadth_akhir_tahun()
    k = bangun_kalender(LIBUR_JAN, breadth, 50)
    pesan = bandingkan_kalender(k, breadth, 50, sampai=date(2026, 1, 16))
    assert pesan == [
        "2026-01-01: bukan libur menurut holidays.csv, tetapi data breadth hampir tanpa transaksi "
        "(mungkin libur yang belum dicatat)",
        "2026-01-02: tercatat libur di holidays.csv, tetapi data breadth menunjukkan ada transaksi",
    ]
    # 2025 (tanpa entri) tidak dibandingkan; tanggal > sampai tidak dibandingkan.
    pesan = bandingkan_kalender(k, breadth, 50, sampai=date(2026, 1, 1))
    assert len(pesan) == 1 and pesan[0].startswith("2026-01-01:")
    assert bandingkan_kalender(k, breadth, 50, sampai=date(2025, 12, 31)) == []


def test_bandingkan_kalender_cocok_dan_tanpa_data():
    breadth = _breadth_akhir_tahun()
    # holidays.csv yang cocok dengan data (2026-01-01 libur, 2026-01-02 bukan) → tanpa pesan.
    k = Kalender({date(2026, 1, 1): FIKTIF})
    assert bandingkan_kalender(k, breadth, 50, sampai=date(2026, 1, 16)) == []
    assert bandingkan_kalender(Kalender({}), breadth, 50, sampai=date(2026, 1, 16)) == []
    assert bandingkan_kalender(k, {}, 50, sampai=date(2026, 1, 16)) == []
    # libur tercatat pada akhir pekan tidak dibandingkan
    k3 = Kalender({date(2026, 1, 1): FIKTIF, date(2026, 1, 3): FIKTIF})
    assert bandingkan_kalender(k3, breadth, 50, sampai=date(2026, 1, 16)) == []


# ---------------------------------------------------------------------------
# Hari ke-N (§4.1)
# ---------------------------------------------------------------------------


def test_nomor_hari_kalender_manual(kal):
    entry = date(2026, 10, 15)  # Kamis
    assert kal.nomor_hari(entry, entry) == 1
    assert kal.nomor_hari(entry, date(2026, 10, 16)) == 2
    assert kal.nomor_hari(entry, date(2026, 10, 19)) == 3  # akhir pekan tidak dihitung
    assert kal.nomor_hari(entry, date(2026, 10, 20)) == 3  # libur tidak dihitung
    assert kal.nomor_hari(entry, date(2026, 10, 21)) == 4
    with pytest.raises(ValueError):
        kal.nomor_hari(entry, date(2026, 10, 14))


def test_nomor_hari_memakai_kalender_efektif():
    k = bangun_kalender(LIBUR_JAN, _breadth_akhir_tahun(), 50)
    # 2025: 25 & 26 Des bukan hari bursa (turunan) → Senin 29 Des = hari ke-2 sejak Rabu 24 Des.
    assert k.nomor_hari(date(2025, 12, 24), date(2025, 12, 29)) == 2
    # Lintas tahun: 30, 31 Des (2025), 1 Jan (2026: manual, bukan libur), 2 Jan libur, 5 Jan.
    assert k.nomor_hari(date(2025, 12, 29), date(2026, 1, 5)) == 5
