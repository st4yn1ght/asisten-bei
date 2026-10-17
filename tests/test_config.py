"""Uji T-CFG (SPEC §7, §22.1, §22.5): validasi `config.yaml` dan resolusi rujukan per konteks.

Bagian T-CFG "setiap kunci `strategi.*`, `skor.*`, `kondisi_pasar.*` mengubah keluaran fixture"
ada di berkas uji lain; berkas ini menguji validasi, resolusi `ikuti_*`, pemuatan berkas, dan
`batas_bar_final_offset`.

Semua tabel fraksi/ARA/ARB dan jam sesi "terverifikasi" di berkas ini **FIKTIF – KHUSUS UJI**:
angkanya sengaja janggal dan BUKAN aturan atau jadwal BEI (P4: aturan BEI tidak diisi dari ingatan).
"""

from __future__ import annotations

import copy
import dataclasses
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from asisten_bei.config import (
    Config,
    ConfigTidakValid,
    KonteksRisiko,
    baca_yaml,
    batas_bar_final_offset,
    muat_config,
    parse_jam,
    resolusi_konteks,
    validasi_dict,
)

AKAR_PROYEK = Path(__file__).resolve().parents[1]
PATH_CONFIG_RILIS = AKAR_PROYEK / "config.yaml"
_DASAR = baca_yaml(PATH_CONFIG_RILIS)

FIKTIF = "FIKTIF – KHUSUS UJI"
D = Decimal


# ---------------------------------------------------------------------------
# Pembantu
# ---------------------------------------------------------------------------

_HAPUS = object()


def _data(ubah: dict[str, Any] | None = None) -> dict:
    """Salinan dict config rilis dengan perubahan bertitik; nilai `_HAPUS` menghapus kunci.
    Kunci terakhir boleh belum ada (untuk menambah kunci tak dikenal)."""
    data = copy.deepcopy(_DASAR)
    for kunci, nilai in (ubah or {}).items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        if nilai is _HAPUS:
            del simpul[akhir]
        else:
            simpul[akhir] = copy.deepcopy(nilai)
    return data


def _cfg(ubah: dict[str, Any] | None = None) -> Config:
    return validasi_dict(_data(ubah))


def _galat(ubah: dict[str, Any]) -> list[str]:
    """Pesan ConfigTidakValid untuk perubahan yang harus ditolak."""
    with pytest.raises(ConfigTidakValid) as e:
        validasi_dict(_data(ubah))
    assert e.value.pesan, "ConfigTidakValid harus membawa minimal satu pesan"
    return e.value.pesan


def _ada(pesan: list[str], awalan: str, isi: str = "") -> bool:
    return any(p.startswith(awalan) and isi in p for p in pesan)


def _ada_kunci(pesan: list[str], jalur: str) -> bool:
    """Ada pesan yang menunjuk `jalur` atau elemen di bawahnya (mis. `laporan.format.1`)."""
    return _ada(pesan, f"{jalur}:") or _ada(pesan, f"{jalur}.")


def _jalur_kunci(d: dict, awal: str = ""):
    """Semua jalur bertitik kunci di pemetaan (rekursif ke sub-pemetaan, tidak ke list)."""
    for k, v in d.items():
        j = f"{awal}{k}"
        yield j
        if isinstance(v, dict):
            yield from _jalur_kunci(v, j + ".")


def _jalur_simpul(d: dict, awal: str = ""):
    """Jalur setiap pemetaan (termasuk akar = "")."""
    yield awal.rstrip(".")
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _jalur_simpul(v, f"{awal}{k}.")


def _jalur_model(model: type[BaseModel], awal: str = ""):
    """(jalur, FieldInfo) setiap field model pydantic, rekursif ke sub-model langsung."""
    for nama, info in model.model_fields.items():
        j = f"{awal}{nama}"
        yield j, info
        ann = info.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            yield from _jalur_model(ann, j + ".")


def _blok_yaml_spec_7() -> dict:
    teks = (AKAR_PROYEK / "SPEC.md").read_text(encoding="utf-8")
    i = teks.index("## 7. `config.yaml`")
    j = teks.index("\n## 8.", i)
    m = re.search(r"```yaml\n(.*?)\n```", teks[i:j], re.S)
    assert m, "blok YAML §7 tidak ditemukan di SPEC.md"
    return yaml.safe_load(m.group(1))


SEMUA_KUNCI = list(_jalur_kunci(_DASAR))
SEMUA_SIMPUL = list(_jalur_simpul(_DASAR))
KUNCI_PERSEN = [j for j in SEMUA_KUNCI if "pct" in j.split(".")[-1]] + ["jurnal.persentil_peringatan"]


# ---------------------------------------------------------------------------
# Config rilis
# ---------------------------------------------------------------------------


def test_config_rilis_lolos_validasi():
    cfg = validasi_dict(baca_yaml(PATH_CONFIG_RILIS))
    assert isinstance(cfg, Config)
    assert cfg.versi_config == 1
    assert cfg.akun.modal_awal_idr == D("3000000")
    assert cfg.risiko.maks_posisi == 3
    assert cfg.risiko.alokasi_maks_pct == "ikuti_maks_posisi"
    assert cfg.backtest.mode.edge.modal_idr == D("1000000000")
    assert cfg.backtest.mode.realistis.modal_idr == "ikuti_akun"
    assert cfg.jadwal.jam_run == "19:30"


def test_config_rilis_identik_dengan_isi_spec_7():
    # SPEC §7 "Isi lengkap dengan nilai default": config rilis = blok YAML §7 (nilai, bukan komentar).
    assert _blok_yaml_spec_7() == _DASAR


def test_model_config_tanpa_default_dan_kuncinya_sama_dengan_spec():
    # "Semua kunci wajib ada" → tidak ada field ber-default; himpunan kunci model = kunci SPEC §7.
    jalur_model = dict(_jalur_model(Config))
    tidak_wajib = [j for j, info in jalur_model.items() if not info.is_required()]
    assert tidak_wajib == []
    assert set(jalur_model) == set(_jalur_kunci(_blok_yaml_spec_7()))


def test_config_rilis_aturan_bei_belum_terverifikasi_dan_kosong():
    # SPEC §22.5: jadwal_sesi, fraksi_harga, auto_reject terverifikasi false dan nilai kosong/null.
    # Tidak ada nilai aturan atau jam sesi BEI yang diisi pengembang.
    p = _DASAR["pasar"]
    assert p["jadwal_sesi"] == {
        "terverifikasi": False, "sumber": "", "tanggal_cek": None,
        "jam_penutupan": None, "jam_pra_pembukaan": None,
    }
    assert p["fraksi_harga"] == {
        "terverifikasi": False, "sumber": "", "tanggal_cek": None, "berlaku_mulai": None, "tabel": [],
    }
    assert p["auto_reject"] == {
        "terverifikasi": False, "sumber": "", "tanggal_cek": None, "berlaku_mulai": None,
        "papan_berlaku": "", "harga_acuan": None, "ara": [], "arb": [],
    }
    cfg = _cfg()
    js, fh, ar = cfg.pasar.jadwal_sesi, cfg.pasar.fraksi_harga, cfg.pasar.auto_reject
    assert js.terverifikasi is False and js.jam_penutupan is None and js.jam_pra_pembukaan is None
    assert fh.terverifikasi is False and fh.tabel == [] and fh.berlaku_mulai is None
    assert ar.terverifikasi is False and ar.ara == [] and ar.arb == [] and ar.harga_acuan is None
    # Item §24 #9 dan #10 juga harus dicek pengguna sendiri.
    assert cfg.biaya.terverifikasi is False
    assert cfg.data.pengali_volume_terverifikasi is False


def test_persen_uang_menjadi_decimal_tepat_bukan_float():
    # §4.1 "Aritmetika uang": Decimal dari nilai config. 0.15 harus Decimal("0.15"), bukan
    # Decimal(0.15) = 0.1499999999999999944488848768742172978818416595458984375.
    b = _cfg().biaya
    assert isinstance(b.fee_beli_pct, Decimal)
    assert b.fee_beli_pct == D("0.15") and b.fee_beli_pct != D(0.15)
    assert b.fee_jual_pct == D("0.25")
    assert b.slippage_pct == D("0.10")
    assert _cfg().risiko.risiko_per_transaksi_pct == D("1.0")
    # Persen alokasi 33.3 (float YAML) → 3.000.000 × 33,3 / 100 = 999.000 tepat.
    k = resolusi_konteks(_cfg({"risiko.alokasi_maks_pct": 33.3}), "live")
    assert k.alokasi_maks(D("3000000")) == D("999000")


# ---------------------------------------------------------------------------
# Kunci hilang / tak dikenal / tipe
# ---------------------------------------------------------------------------


def test_daftar_kunci_tidak_kosong():
    # Penjaga agar uji berparameter di bawah tidak hampa.
    assert len(SEMUA_KUNCI) > 150
    assert len(SEMUA_SIMPUL) > 25
    assert len(KUNCI_PERSEN) >= 20


@pytest.mark.parametrize("jalur", SEMUA_KUNCI)
def test_kunci_hilang_error_menunjuk_kunci(jalur):
    pesan = _galat({jalur: _HAPUS})
    assert f"{jalur}: kunci wajib tidak ada" in pesan


@pytest.mark.parametrize("simpul", SEMUA_SIMPUL)
def test_kunci_tak_dikenal_error_di_setiap_tingkat(simpul):
    kunci = f"{simpul}.kunci_asing_uji" if simpul else "kunci_asing_uji"
    pesan = _galat({kunci: 1})
    assert _ada(pesan, f"{kunci}: kunci tidak dikenal")


def test_salah_ketik_kunci_melaporkan_hilang_dan_tak_dikenal_sekaligus():
    data = _data()
    data["risiko"]["maks_posisii"] = data["risiko"].pop("maks_posisi")
    with pytest.raises(ConfigTidakValid) as e:
        validasi_dict(data)
    assert "risiko.maks_posisi: kunci wajib tidak ada" in e.value.pesan
    assert _ada(e.value.pesan, "risiko.maks_posisii: kunci tidak dikenal")
    # Pesan pengecualian memuat semua baris.
    assert "Config tidak valid" in str(e.value) and "risiko.maks_posisii" in str(e.value)


@pytest.mark.parametrize("isi", [None, [], "versi_config: 1", 3])
def test_berkas_kosong_atau_bukan_pemetaan_error(isi):
    with pytest.raises(ConfigTidakValid) as e:
        validasi_dict(isi)
    assert "kosong atau bukan pemetaan" in e.value.pesan[0]


def test_bagian_bukan_pemetaan_error():
    assert _ada(_galat({"risiko": 3}), "risiko: harus pemetaan")


@pytest.mark.parametrize(
    "jalur, nilai",
    [
        ("risiko.maks_posisi", 3.5),
        ("risiko.maks_posisi", 3.0),       # bilangan bulat ketat: 3.0 bukan int
        ("risiko.maks_posisi", "3"),
        ("risiko.maks_posisi", True),
        ("risiko.maks_posisi", 0),         # minimal 1
        ("risiko.izinkan_satu_lot_melebihi_risiko", "false"),
        ("risiko.izinkan_satu_lot_melebihi_risiko", 0),
        ("akun.modal_awal_idr", 0),        # modal harus > 0
        ("akun.file_akun", 5),
        ("backtest.mulai", "2015-13-01"),
        ("data.provider_urutan", ["yfinance", "yfinance"]),
        ("data.provider_urutan", []),
        ("data.provider_urutan", ["yfinance", "stooq"]),
        ("laporan.format", ["terminal", "pdf"]),
        ("biaya.pembulatan_fee", "rupiah_ke_bawah"),
        ("biaya.fee_beli_pct", True),
    ],
)
def test_tipe_dan_nilai_tidak_sah_error(jalur, nilai):
    assert _ada_kunci(_galat({jalur: nilai}), jalur)


@pytest.mark.parametrize("nilai", [None, "rupiah_terdekat", "rupiah_ke_atas"])
def test_pembulatan_fee_nilai_sah(nilai):
    assert _cfg({"biaya.pembulatan_fee": nilai}).biaya.pembulatan_fee == nilai


# ---------------------------------------------------------------------------
# versi_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("versi", [0, 2, -1, 10])
def test_versi_config_selain_1_error(versi):
    pesan = _galat({"versi_config": versi})
    assert _ada(pesan, "versi_config:", "versi program lain")


@pytest.mark.parametrize("versi", ["1", 1.0, True, None])
def test_versi_config_bukan_bilangan_bulat_1_error(versi):
    assert _ada(_galat({"versi_config": versi}), "versi_config:")


def test_versi_config_1_lolos():
    assert _cfg({"versi_config": 1}).versi_config == 1


# ---------------------------------------------------------------------------
# skor.bobot
# ---------------------------------------------------------------------------


def _bobot(s, kr, lk, js) -> dict:
    return {"skor.bobot": {"sinyal": s, "kekuatan_relatif": kr, "likuiditas": lk, "jarak_stop": js}}


@pytest.mark.parametrize(
    "bobot",
    [
        (40, 30, 15, 15),        # default
        (0, 70, 15, 15),         # bobot 0 boleh (≥ 0)
        (100, 0, 0, 0),
        # 4.2 + 45.1 + 15 + 35.7 = 100 tepat secara desimal (float memberi 99.99999999999999).
        (4.2, 45.1, 15, 35.7),
    ],
)
def test_bobot_skor_jumlah_100_lolos(bobot):
    b = _cfg(_bobot(*bobot)).skor.bobot
    assert (b.sinyal, b.kekuatan_relatif, b.likuiditas, b.jarak_stop) == tuple(float(x) for x in bobot)


def test_kasus_bobot_desimal_memang_menjebak_float():
    # Penjaga: kasus (4.2, 45.1, 15, 35.7) di atas hanya bermakna bila jumlah float-nya ≠ 100.
    assert 4.2 + 45.1 + 15 + 35.7 != 100
    assert D("4.2") + D("45.1") + D(15) + D("35.7") == D(100)


@pytest.mark.parametrize(
    "bobot",
    [
        (41, 30, 15, 15),        # 101
        (40, 30, 15, 14.99),     # 99,99 (sisi bawah)
        (40, 30, 15, 15.01),     # 100,01 (sisi atas)
        (0, 0, 0, 0),
        (25, 25, 25, 24),
    ],
)
def test_bobot_skor_jumlah_bukan_100_error(bobot):
    assert _ada(_galat(_bobot(*bobot)), "skor.bobot:", "jumlah bobot harus 100")


def test_bobot_skor_negatif_error_walau_jumlah_100():
    # -10 + 80 + 15 + 15 = 100, tetapi setiap bobot wajib ≥ 0.
    assert _ada(_galat(_bobot(-10, 80, 15, 15)), "skor.bobot.sinyal:", ">= 0")


# ---------------------------------------------------------------------------
# Persen di luar rentang
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nilai", [-1, -0.01, 1_000_000, float("nan"), float("inf")])
@pytest.mark.parametrize("jalur", KUNCI_PERSEN)
def test_setiap_kunci_persen_di_luar_rentang_error(jalur, nilai):
    assert _ada(_galat({jalur: nilai}), f"{jalur}:")


@pytest.mark.parametrize(
    "jalur",
    [
        "data.toleransi_revisi_pct",
        "data.validasi.min_cakupan_universe_pct",
        "data.validasi.min_hari_kalender_historis_pct",
        "kondisi_pasar.min_cakupan_breadth_pct",
        "backtest.potongan_data_berakhir_pct",
        "gerbang.min_pct_jendela_pf_di_atas_1",
        "strategi.acak.peluang_sinyal_pct",
    ],
)
def test_persen_0_sampai_100_batas_kedua_sisi(jalur):
    # Persen biasa: 0 dan 100 sah (inklusif); -0,01 dan 100,01 ditolak.
    _cfg({jalur: 0})
    _cfg({jalur: 100})
    assert _ada(_galat({jalur: -0.01}), f"{jalur}:")
    assert _ada(_galat({jalur: 100.01}), f"{jalur}:")


@pytest.mark.parametrize(
    "jalur",
    [
        "risiko.risiko_per_transaksi_pct",
        "risiko.alokasi_maks_pct",
        "likuiditas.maks_pct_order_dari_nilai_harian",
        "backtest.mode.edge.alokasi_maks_pct",
        "backtest.mode.realistis.alokasi_maks_pct",
    ],
)
def test_persen_wajib_positif_batas_kedua_sisi(jalur):
    # Persen yang membagi/menjadi anggaran: 0 ditolak, 0,01 sah; di atas 100 ditolak.
    assert _ada(_galat({jalur: 0}), f"{jalur}:")
    _cfg({jalur: 0.01})
    assert _ada(_galat({jalur: 100.01}), f"{jalur}:")


@pytest.mark.parametrize("jalur", ["biaya.fee_beli_pct", "biaya.fee_jual_pct", "biaya.slippage_pct"])
def test_persen_biaya_batas(jalur):
    _cfg({jalur: 0})                       # tanpa fee/slippage sah
    assert _ada(_galat({jalur: -0.01}), f"{jalur}:")
    assert _ada(_galat({jalur: 100}), f"{jalur}:")  # fee 100% per sisi tidak wajar


def test_persen_tabel_auto_reject_di_luar_rentang_error():
    # FIKTIF – KHUSUS UJI. batas_pct harus > 0 dan ≤ 100.
    for batas in (0, -5, 100.01):
        ar = _ar_fiktif(arb=[{"harga_min": 0, "harga_maks": None, "batas_pct": batas}])
        assert _ada(_galat({"pasar.auto_reject": ar}), "pasar.auto_reject.arb.0.batas_pct:")


@pytest.mark.parametrize("nilai, sah", [(0, True), (1, True), (0.5, True), (-0.01, False), (1.01, False)])
def test_pengali_risiko_hati_hati_pecahan_0_sampai_1(nilai, sah):
    ubah = {"risiko.hati_hati.pengali_risiko": nilai}
    if sah:
        assert _cfg(ubah).risiko.hati_hati.pengali_risiko == D(str(nilai))
    else:
        assert _ada(_galat(ubah), "risiko.hati_hati.pengali_risiko:")


def test_breadth_jangan_maks_tidak_boleh_di_atas_boleh_min():
    # K2 (< jangan_maks → JANGAN BELI) dan K3 (≥ boleh_min → BOLEH BELI) tidak boleh bertabrakan.
    _cfg({"kondisi_pasar.breadth_jangan_maks_pct": 50, "kondisi_pasar.breadth_boleh_min_pct": 50})
    pesan = _galat({"kondisi_pasar.breadth_jangan_maks_pct": 50.01, "kondisi_pasar.breadth_boleh_min_pct": 50})
    assert any("breadth_jangan_maks_pct" in p for p in pesan)


def test_jarak_stop_ideal_harus_kurang_dari_buruk():
    # K_stop membagi dengan (buruk − ideal) (§13.1): sama besar → error.
    _cfg({"skor.jarak_stop_ideal_pct": 9.99, "skor.jarak_stop_buruk_pct": 10.0})
    assert _ada(_galat({"skor.jarak_stop_ideal_pct": 10.0, "skor.jarak_stop_buruk_pct": 10.0}), "skor:", "jarak_stop_ideal_pct")


def test_urutan_tanggal_backtest():
    assert _ada(_galat({"backtest.holdout_mulai": "2014-12-31"}), "backtest:", "holdout_mulai")
    assert _ada(_galat({"backtest.selesai": "2024-09-30"}), "backtest:", "holdout_mulai")
    assert _cfg({"backtest.selesai": "2025-12-31"}).backtest.selesai == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# Format jam
# ---------------------------------------------------------------------------

JAM_SAH = ["00:00", "07:05", "19:30", "23:59", "09:00"]
JAM_TIDAK_SAH = ["24:00", "19:60", "7:30", "19.30", "19:3", "1930", "19:30:00", " 19:30", "19:30 ", "", "ab:cd", "-1:30"]


@pytest.mark.parametrize("jam", JAM_SAH)
def test_jam_run_format_sah(jam):
    cfg = _cfg({"jadwal.jam_run": jam})
    assert cfg.jadwal.jam_run == jam
    hh, mm = jam.split(":")
    assert parse_jam(jam).hour == int(hh) and parse_jam(jam).minute == int(mm)


@pytest.mark.parametrize("jam", JAM_TIDAK_SAH)
def test_jam_run_format_tidak_sah_error(jam):
    assert _ada(_galat({"jadwal.jam_run": jam}), "jadwal.jam_run:", 'format jam harus "HH:MM"')


@pytest.mark.parametrize("jam", [None, 1930, 19.5])
def test_jam_run_bukan_teks_error(jam):
    assert _ada(_galat({"jadwal.jam_run": jam}), "jadwal.jam_run:")


def test_jam_tanpa_tanda_kutip_di_yaml_ditolak():
    # YAML 1.1 membaca 19:30 tanpa kutip sebagai bilangan seksagesimal 1170 → harus error, bukan 19:30.
    nilai = yaml.safe_load("jam_run: 19:30")["jam_run"]
    assert nilai == 1170
    assert _ada(_galat({"jadwal.jam_run": nilai}), "jadwal.jam_run:", "harus teks")


def test_jam_dengan_newline_akhir_ditolak():
    _galat({"jadwal.jam_run": "19:30\n"})


@pytest.mark.parametrize("kunci", ["jam_penutupan", "jam_pra_pembukaan"])
def test_jam_jadwal_sesi_format(kunci):
    jalur = f"pasar.jadwal_sesi.{kunci}"
    # null sah (belum diisi); "13:17" FIKTIF – KHUSUS UJI sah; format lain ditolak.
    assert getattr(_cfg({jalur: None}).pasar.jadwal_sesi, kunci) is None
    assert getattr(_cfg({jalur: "13:17"}).pasar.jadwal_sesi, kunci) == "13:17"
    for jam in ("24:00", "13:7", "1317", "13.17"):
        assert _ada(_galat({jalur: jam}), f"{jalur}:", 'format jam harus "HH:MM"')
    assert _ada(_galat({jalur: 797}), f"{jalur}:")  # 13:17 tanpa kutip di YAML


# ---------------------------------------------------------------------------
# Tabel fraksi dan ARA/ARB — FIKTIF – KHUSUS UJI
# ---------------------------------------------------------------------------

# FIKTIF – KHUSUS UJI: angka sengaja janggal, BUKAN fraksi harga BEI.
FRAKSI_FIKTIF = [
    {"harga_min": 0, "harga_maks": 333, "fraksi": 3},
    {"harga_min": 333, "harga_maks": 7777, "fraksi": 11},
    {"harga_min": 7777, "harga_maks": None, "fraksi": 37},
]
# FIKTIF – KHUSUS UJI: BUKAN batas ARA/ARB BEI.
ARA_FIKTIF = [
    {"harga_min": 0, "harga_maks": 444, "batas_pct": 41},
    {"harga_min": 444, "harga_maks": None, "batas_pct": 13},
]
ARB_FIKTIF = [{"harga_min": 0, "harga_maks": None, "batas_pct": 9}]


def _fh_fiktif(tabel=None, **lain) -> dict:
    return {
        "terverifikasi": True, "sumber": FIKTIF, "tanggal_cek": "2026-10-01",
        "berlaku_mulai": "2026-09-07", "tabel": copy.deepcopy(FRAKSI_FIKTIF if tabel is None else tabel), **lain,
    }


def _ar_fiktif(ara=None, arb=None, **lain) -> dict:
    return {
        "terverifikasi": True, "sumber": FIKTIF, "tanggal_cek": "2026-10-01", "berlaku_mulai": "2026-09-07",
        "papan_berlaku": FIKTIF, "harga_acuan": "close_kemarin",
        "ara": copy.deepcopy(ARA_FIKTIF if ara is None else ara),
        "arb": copy.deepcopy(ARB_FIKTIF if arb is None else arb), **lain,
    }


def _ganti_baris(tabel: list[dict], i: int, **ubah) -> list[dict]:
    t = copy.deepcopy(tabel)
    t[i].update(ubah)
    return t


def test_tabel_fiktif_bersambung_lolos():
    cfg = _cfg({"pasar.fraksi_harga": _fh_fiktif(), "pasar.auto_reject": _ar_fiktif()})
    fh, ar = cfg.pasar.fraksi_harga, cfg.pasar.auto_reject
    assert fh.terverifikasi is True and fh.berlaku_mulai == date(2026, 9, 7)
    assert [(b.harga_min, b.harga_maks, b.fraksi) for b in fh.tabel] == [
        (D(0), D(333), D(3)), (D(333), D(7777), D(11)), (D(7777), None, D(37)),
    ]
    assert [b.batas_pct for b in ar.ara] == [D(41), D(13)]
    assert ar.arb[0].harga_maks is None and ar.harga_acuan == "close_kemarin"


def test_tabel_fiktif_belum_terverifikasi_tetap_divalidasi():
    # Tabel boleh diisi sebelum terverifikasi, tetapi bentuknya tetap diperiksa.
    _cfg({"pasar.fraksi_harga": _fh_fiktif(terverifikasi=False)})
    rusak = _ganti_baris(FRAKSI_FIKTIF, 1, harga_min=300)
    assert _ada(_galat({"pasar.fraksi_harga": _fh_fiktif(rusak, terverifikasi=False)}), "pasar.fraksi_harga:", "tumpang tindih")


@pytest.mark.parametrize(
    "harga_min_baris_1, isi",
    [
        (333, None),             # tepat bersambung (harga_maks baris 0 = 333, eksklusif)
        (332, "tumpang tindih"),  # 1 rupiah tumpang tindih
        (300, "tumpang tindih"),
        (334, "berlubang"),       # celah 1 rupiah
        (500, "berlubang"),
    ],
)
def test_fraksi_tumpang_tindih_dan_berlubang(harga_min_baris_1, isi):
    tabel = _ganti_baris(FRAKSI_FIKTIF, 1, harga_min=harga_min_baris_1)
    ubah = {"pasar.fraksi_harga": _fh_fiktif(tabel)}
    if isi is None:
        assert _cfg(ubah).pasar.fraksi_harga.tabel[1].harga_min == D(333)
    else:
        assert _ada(_galat(ubah), "pasar.fraksi_harga:", isi)


@pytest.mark.parametrize(
    "tabel, isi",
    [
        (_ganti_baris(FRAKSI_FIKTIF, 2, harga_maks=99999), "baris terakhir harus harga_maks null"),
        (_ganti_baris(FRAKSI_FIKTIF, 0, harga_maks=None), "harga_maks null hanya boleh di baris terakhir"),
        (_ganti_baris(FRAKSI_FIKTIF, 1, harga_maks=333), "harga_maks harus > harga_min"),  # [333, 333)
        ([FRAKSI_FIKTIF[1], FRAKSI_FIKTIF[0], FRAKSI_FIKTIF[2]], ""),  # urutan terbalik
    ],
)
def test_fraksi_bentuk_tabel_tidak_sah(tabel, isi):
    assert _ada(_galat({"pasar.fraksi_harga": _fh_fiktif(tabel)}), "pasar.fraksi_harga", isi)


@pytest.mark.parametrize("ubah", [{"fraksi": 0}, {"fraksi": -3}, {"harga_min": -1}, {"catatan": "x"}])
def test_fraksi_nilai_baris_tidak_sah(ubah):
    tabel = _ganti_baris(FRAKSI_FIKTIF, 0, **ubah)
    assert _ada(_galat({"pasar.fraksi_harga": _fh_fiktif(tabel)}), "pasar.fraksi_harga.tabel.0.")


def test_fraksi_terverifikasi_dengan_tabel_kosong_atau_tanpa_berlaku_mulai_error():
    assert _ada(_galat({"pasar.fraksi_harga": _fh_fiktif([])}), "pasar.fraksi_harga:", "tabel kosong")
    assert _ada(_galat({"pasar.fraksi_harga": _fh_fiktif(berlaku_mulai=None)}), "pasar.fraksi_harga:", "berlaku_mulai")
    # Pembanding: tidak terverifikasi + tabel kosong = config rilis, sah.
    _cfg({"pasar.fraksi_harga": _fh_fiktif([], terverifikasi=False, berlaku_mulai=None)})


@pytest.mark.parametrize("sisi", ["ara", "arb"])
@pytest.mark.parametrize(
    "harga_min_baris_1, isi",
    [(444, None), (443, "tumpang tindih"), (445, "berlubang")],
)
def test_ara_arb_tumpang_tindih_dan_berlubang(sisi, harga_min_baris_1, isi):
    tabel = _ganti_baris(ARA_FIKTIF, 1, harga_min=harga_min_baris_1)
    ubah = {"pasar.auto_reject": _ar_fiktif(**{sisi: tabel})}
    if isi is None:
        assert getattr(_cfg(ubah).pasar.auto_reject, sisi)[1].harga_min == D(444)
    else:
        assert _ada(_galat(ubah), "pasar.auto_reject:", f"{sisi}[0] dan [1] {isi}")


@pytest.mark.parametrize("sisi", ["ara", "arb"])
def test_ara_arb_terverifikasi_dengan_tabel_kosong_error(sisi):
    assert _ada(_galat({"pasar.auto_reject": _ar_fiktif(**{sisi: []})}), "pasar.auto_reject:", "kosong")
    # Tidak terverifikasi + kosong sah.
    _cfg({"pasar.auto_reject": _ar_fiktif(ara=[], arb=[], terverifikasi=False, harga_acuan=None)})


def test_ara_arb_terverifikasi_tanpa_berlaku_mulai_atau_harga_acuan_tak_didukung_error():
    assert _ada(_galat({"pasar.auto_reject": _ar_fiktif(berlaku_mulai=None)}), "pasar.auto_reject:", "berlaku_mulai")
    for acuan in (None, "open_hari_ini"):
        assert _ada(_galat({"pasar.auto_reject": _ar_fiktif(harga_acuan=acuan)}), "pasar.auto_reject:", "harga_acuan")


# ---------------------------------------------------------------------------
# Resolusi rujukan per konteks (SPEC §7 "Resolusi rujukan")
# ---------------------------------------------------------------------------


def test_resolusi_live_default():
    k = resolusi_konteks(_cfg(), "live")
    assert k == KonteksRisiko(nama="live", modal_idr=D("3000000"), maks_posisi=3, maks_per_sektor=1, alokasi_maks_pct=None)
    hasil = k.alokasi_maks(D("3000000"))
    # ikuti_maks_posisi = pembagian langsung: 3.000.000 ÷ 3 = 1.000.000 tepat.
    assert isinstance(hasil, Decimal) and hasil == D("1000000")
    # Lewat persen (33,33…%) hasilnya tidak tepat — itu sebabnya SPEC meminta pembagian langsung.
    assert D("3000000") * (D(100) / D(3)) / D(100) != D("1000000")


def test_resolusi_edge_default_alokasi_ekuitas_dibagi_3():
    k = resolusi_konteks(_cfg(), "edge")
    assert k.nama == "edge"
    assert k.modal_idr == D("1000000000")       # angka eksplisit di mode edge
    assert k.maks_posisi == 3                     # ikuti_risiko
    assert k.maks_per_sektor == 1                 # ikuti_risiko
    assert k.alokasi_maks_pct is None             # ikuti_risiko → risiko: ikuti_maks_posisi
    assert k.alokasi_maks(D("3000000")) == D("1000000")
    assert k.alokasi_maks(D("1000000000")) == D("1000000000") / D(3)


def test_resolusi_realistis_default_ikuti_akun():
    k = resolusi_konteks(_cfg(), "realistis")
    assert k.modal_idr == D("3000000")            # ikuti_akun
    assert (k.maks_posisi, k.maks_per_sektor, k.alokasi_maks_pct) == (3, 1, None)
    assert k.alokasi_maks(k.modal_idr) == D("1000000")


def test_default_edge_dan_realistis_hanya_berbeda_pada_modal():
    cfg = _cfg()
    edge, real = resolusi_konteks(cfg, "edge"), resolusi_konteks(cfg, "realistis")
    assert edge.modal_idr != real.modal_idr
    assert dataclasses.replace(edge, nama="realistis", modal_idr=real.modal_idr) == real


def test_ikuti_akun_mengikuti_modal_awal():
    cfg = _cfg({"akun.modal_awal_idr": 4500000})
    assert resolusi_konteks(cfg, "realistis").modal_idr == D("4500000")
    assert resolusi_konteks(cfg, "live").modal_idr == D("4500000")
    assert resolusi_konteks(cfg, "edge").modal_idr == D("1000000000")  # eksplisit, tidak ikut


def test_ikuti_risiko_mengikuti_nilai_risiko():
    cfg = _cfg({"risiko.maks_posisi": 4, "risiko.maks_per_sektor": None})
    for nama in ("live", "edge", "realistis"):
        k = resolusi_konteks(cfg, nama)
        assert k.maks_posisi == 4 and k.maks_per_sektor is None
        assert k.alokasi_maks(D("3000000")) == D("750000")  # 3.000.000 ÷ 4


def test_ikuti_risiko_berantai_ke_ikuti_maks_posisi_diresolusi_dalam_konteks_mode():
    # edge.maks_posisi = 5 (eksplisit), edge.alokasi = ikuti_risiko → risiko.alokasi = ikuti_maks_posisi
    # → diresolusi dengan maks_posisi milik mode edge (5), bukan milik risiko (3).
    cfg = _cfg({"backtest.mode.edge.maks_posisi": 5})
    edge = resolusi_konteks(cfg, "edge")
    assert edge.maks_posisi == 5 and edge.alokasi_maks_pct is None
    assert edge.alokasi_maks(D("3000000")) == D("600000")      # 3.000.000 ÷ 5
    # Konteks lain tidak tercemar.
    assert resolusi_konteks(cfg, "live").alokasi_maks(D("3000000")) == D("1000000")
    assert resolusi_konteks(cfg, "realistis").alokasi_maks(D("3000000")) == D("1000000")


def test_ikuti_risiko_ke_persen_angka():
    # risiko.alokasi = 40 → edge (ikuti_risiko) = 40% ekuitas, berapa pun maks_posisi mode.
    cfg = _cfg({"risiko.alokasi_maks_pct": 40, "backtest.mode.edge.maks_posisi": 5})
    edge = resolusi_konteks(cfg, "edge")
    assert edge.alokasi_maks_pct == D(40)
    assert edge.alokasi_maks(D("3000000")) == D("1200000")     # 3.000.000 × 40 / 100
    assert resolusi_konteks(cfg, "live").alokasi_maks(D("3000000")) == D("1200000")


def test_mode_ikuti_maks_posisi_langsung_memakai_maks_posisi_mode():
    cfg = _cfg({
        "risiko.alokasi_maks_pct": 40,
        "backtest.mode.realistis.maks_posisi": 6,
        "backtest.mode.realistis.alokasi_maks_pct": "ikuti_maks_posisi",
    })
    real = resolusi_konteks(cfg, "realistis")
    assert real.alokasi_maks_pct is None
    assert real.alokasi_maks(D("3000000")) == D("500000")      # 3.000.000 ÷ 6
    assert resolusi_konteks(cfg, "live").alokasi_maks(D("3000000")) == D("1200000")  # tetap 40%


def test_angka_eksplisit_di_mode_mengalahkan_risiko_dan_akun():
    cfg = _cfg({
        "backtest.mode.edge.modal_idr": 2000000,
        "backtest.mode.edge.maks_posisi": 5,
        "backtest.mode.edge.maks_per_sektor": 2,
        "backtest.mode.edge.alokasi_maks_pct": 25,
    })
    edge = resolusi_konteks(cfg, "edge")
    assert edge == KonteksRisiko(nama="edge", modal_idr=D("2000000"), maks_posisi=5, maks_per_sektor=2, alokasi_maks_pct=D(25))
    assert edge.alokasi_maks(D("2000000")) == D("500000")      # 2.000.000 × 25 / 100
    live = resolusi_konteks(cfg, "live")
    assert (live.modal_idr, live.maks_posisi, live.maks_per_sektor, live.alokasi_maks_pct) == (D("3000000"), 3, 1, None)


def test_mode_maks_per_sektor_null_eksplisit_tanpa_batas():
    cfg = _cfg({"backtest.mode.realistis.maks_per_sektor": None})
    assert resolusi_konteks(cfg, "realistis").maks_per_sektor is None
    assert resolusi_konteks(cfg, "edge").maks_per_sektor == 1


@pytest.mark.parametrize(
    "jalur, nilai",
    [
        ("risiko.alokasi_maks_pct", "ikuti_risiko"),            # rujukan ke dirinya sendiri
        ("risiko.alokasi_maks_pct", "ikuti_akun"),
        ("backtest.mode.edge.modal_idr", "ikuti_risiko"),
        ("backtest.mode.edge.maks_posisi", "ikuti_akun"),
        ("backtest.mode.edge.maks_posisi", "ikuti_maks_posisi"),
        ("backtest.mode.realistis.maks_per_sektor", "ikuti_akun"),
        ("backtest.mode.realistis.alokasi_maks_pct", "ikuti_akun"),
        ("backtest.mode.realistis.alokasi_maks_pct", "ikut_risiko"),  # salah ketik
        ("backtest.mode.edge.modal_idr", 0),
        ("backtest.mode.edge.maks_posisi", 0),
    ],
)
def test_rujukan_tidak_sah_error(jalur, nilai):
    assert _ada(_galat({jalur: nilai}), f"{jalur}:")


def test_uraian_konteks_untuk_cek():
    teks = resolusi_konteks(_cfg(), "edge").uraian()
    assert teks.startswith("edge:")
    assert "Rp 1.000.000.000" in teks and "maks_posisi 3" in teks and "ekuitas ÷ maks_posisi" in teks
    cfg = _cfg({"backtest.mode.edge.maks_per_sektor": None, "backtest.mode.edge.alokasi_maks_pct": 25})
    teks = resolusi_konteks(cfg, "edge").uraian()
    assert "tanpa batas" in teks and "25% ekuitas" in teks


# ---------------------------------------------------------------------------
# Pemuatan berkas dan path relatif --akar
# ---------------------------------------------------------------------------


def test_muat_config_akar_default_folder_config_dan_path_relatif(tmp_path, monkeypatch):
    folder = tmp_path / "proyek"
    folder.mkdir()
    (folder / "config.yaml").write_text(PATH_CONFIG_RILIS.read_text(encoding="utf-8"), encoding="utf-8")
    lain = tmp_path / "lain"
    lain.mkdir()
    monkeypatch.chdir(lain)  # path tidak boleh relatif terhadap direktori kerja

    ctx = muat_config(folder / "config.yaml")
    assert ctx.akar == folder.resolve()
    assert ctx.path_config == (folder / "config.yaml").resolve()
    assert ctx.path(ctx.cfg.universe.file) == folder.resolve() / "input" / "universe.csv"
    absolut = tmp_path / "x.csv"
    assert ctx.path(absolut) == absolut
    assert ctx.konteks("edge") == resolusi_konteks(ctx.cfg, "edge")

    akar_lain = tmp_path / "akar"
    ctx2 = muat_config(folder / "config.yaml", akar_lain)
    assert ctx2.akar == akar_lain.resolve()
    assert ctx2.path("data/cache") == akar_lain.resolve() / "data" / "cache"


def test_muat_config_bom_utf8_diterima(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(PATH_CONFIG_RILIS.read_text(encoding="utf-8"), encoding="utf-8-sig")
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    assert muat_config(p).cfg.versi_config == 1


def test_muat_config_berkas_tidak_ada_kosong_atau_yaml_rusak(tmp_path):
    with pytest.raises(ConfigTidakValid) as e:
        muat_config(tmp_path / "tidak_ada.yaml")
    assert "tidak ditemukan" in e.value.pesan[0]

    kosong = tmp_path / "kosong.yaml"
    kosong.write_text("", encoding="utf-8")
    with pytest.raises(ConfigTidakValid) as e:
        muat_config(kosong)
    assert "kosong" in e.value.pesan[0]

    rusak = tmp_path / "rusak.yaml"
    rusak.write_text("akun: [1, 2\nrisiko: {", encoding="utf-8")
    with pytest.raises(ConfigTidakValid) as e:
        muat_config(rusak)
    assert "YAML tidak valid" in e.value.pesan[0]


def test_muat_config_menolak_kunci_tak_dikenal_dari_berkas(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(PATH_CONFIG_RILIS.read_text(encoding="utf-8") + "\nkunci_baru: 1\n", encoding="utf-8")
    with pytest.raises(ConfigTidakValid) as e:
        muat_config(p)
    assert _ada(e.value.pesan, "kunci_baru: kunci tidak dikenal")


# ---------------------------------------------------------------------------
# Batas bar final (SPEC §8.6) sebagai offset dari 00:00 WIB
# ---------------------------------------------------------------------------


def _sesi_fiktif(**ubah) -> dict:
    # FIKTIF – KHUSUS UJI: jam janggal, BUKAN jadwal sesi BEI.
    return {
        "terverifikasi": True, "sumber": FIKTIF, "tanggal_cek": "2026-10-01",
        "jam_penutupan": "13:17", "jam_pra_pembukaan": "06:41", **ubah,
    }


def test_batas_bar_final_belum_terverifikasi_memakai_jam_run():
    assert batas_bar_final_offset(_cfg()) == timedelta(hours=19, minutes=30)
    assert batas_bar_final_offset(_cfg({"jadwal.jam_run": "07:05"})) == timedelta(hours=7, minutes=5)
    # jam_penutupan terisi tetapi terverifikasi false → tetap jam_run.
    cfg = _cfg({"pasar.jadwal_sesi": _sesi_fiktif(terverifikasi=False)})
    assert batas_bar_final_offset(cfg) == timedelta(hours=19, minutes=30)


def test_batas_bar_final_terverifikasi_tanpa_jam_penutupan_memakai_jam_run():
    cfg = _cfg({"pasar.jadwal_sesi": _sesi_fiktif(jam_penutupan=None), "jadwal.jam_run": "21:12"})
    assert batas_bar_final_offset(cfg) == timedelta(hours=21, minutes=12)


@pytest.mark.parametrize(
    "penutupan, jeda, harapan",
    [
        ("13:17", 60, timedelta(hours=14, minutes=17)),   # 13:17 + 60 menit
        ("13:17", 0, timedelta(hours=13, minutes=17)),
        ("13:17", 43, timedelta(hours=14)),                # 13:17 + 43 = 14:00 (menit membawa jam)
        ("23:50", 9, timedelta(hours=23, minutes=59)),     # tepat sebelum tengah malam
        ("23:50", 10, timedelta(hours=24)),                # tepat tengah malam hari berikutnya
        ("23:50", 25, timedelta(days=1, minutes=15)),      # 24:15 → > 24 jam, tidak terbungkus ke 00:15
        ("22:22", 1500, timedelta(days=1, hours=23, minutes=22)),  # 22:22 + 25 jam = 47:22
    ],
)
def test_batas_bar_final_terverifikasi_penutupan_plus_jeda(penutupan, jeda, harapan):
    cfg = _cfg({
        "pasar.jadwal_sesi": _sesi_fiktif(jam_penutupan=penutupan),
        "data.jeda_data_final_menit": jeda,
        "jadwal.jam_run": "05:55",   # tidak dipakai saat jadwal terverifikasi
    })
    hasil = batas_bar_final_offset(cfg)
    assert isinstance(hasil, timedelta)
    assert hasil == harapan
