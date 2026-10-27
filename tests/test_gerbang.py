"""T-GB (bagian berkas) — gerbang G1–G6, hash, gate_status.json, holdout_log.csv, subset terjangkau
(SPEC §16.1, §16.4, §16.6, §16.7, §22.1 T-GB).

Metrik transaksi dibangun dari posisi fiktif (`test_metrik.buat_posisi`); satu uji integrasi menjalankan
`mesin.simulasikan` pada pasar sintetis kecil (`test_metrik.pasar_integrasi`). Uji berkas bekerja di
`tmp_path`/salinan fixture (`akar_uji`), tidak pernah di berkas pengguna.

Nilai aturan BEI di uji ini (jadwal sesi, ARA/ARB, fraksi) adalah **FIKTIF – KHUSUS UJI**: angka sengaja
dibuat-buat dan bukan aturan nyata; dipakai hanya untuk memastikan pengaruhnya pada hash.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from asisten_bei import DISCLAIMER, DISCLAIMER_CSV, exit as ex
from asisten_bei.backtest import gerbang as gb
from asisten_bei.backtest import jendela as jd
from asisten_bei.backtest import metrik as mt
from asisten_bei.config import KonteksApp, baca_yaml, validasi_dict
from asisten_bei.waktu import ZONA
from bantu_pasar import KAL, cfg_uji, tgl
from test_metrik import UBAH_INTEGRASI, buat_posisi, pasar_integrasi, simulasi_integrasi

D = Decimal

# FIKTIF – KHUSUS UJI: angka buatan, BUKAN aturan BEI.
ARA_FIKTIF = [{"harga_min": 0, "harga_maks": None, "jenis": "pct", "batas": 77}]
ARB_FIKTIF = [{"harga_min": 0, "harga_maks": None, "jenis": "pct", "batas": 33}]
FRAKSI_FIKTIF = [{"harga_min": 0, "harga_maks": 1234, "fraksi": 3}, {"harga_min": 1234, "harga_maks": None, "fraksi": 7}]
JAM_FIKTIF = {"jam_penutupan": "03:17", "jam_pra_pembukaan": "02:05"}


# ---------------------------------------------------------------------------
# Pembantu metrik
# ---------------------------------------------------------------------------

def _tx(r: list[float], mulai: int = 0) -> list[ex.Posisi]:
    """Transaksi dengan R yang diberikan; transaksi ke-i masuk `tgl(mulai + i)` dan keluar `tgl(mulai + i + 1)`,
    sehingga urutan exit = urutan daftar (tidak bergantung urutan uji). Kode unik per posisi."""
    return [buat_posisi(f"K{mulai + i:04d}", tgl(mulai + i), tgl(mulai + i + 1), x) for i, x in enumerate(r)]


def _selang(n_menang: int, n_kalah: int) -> list[float]:
    """`n_menang` × +1R dan `n_kalah` × −1R (n_kalah ≤ n_menang) berselang-seling, sisa menang di akhir:
    kurva R kumulatif turun paling banyak 1R dan berakhir di puncak."""
    assert n_kalah <= n_menang
    return [1.0, -1.0] * n_kalah + [1.0] * (n_menang - n_kalah)


def _m(daftar: list[ex.Posisi]) -> mt.MetrikTransaksi:
    return mt.metrik_transaksi(daftar, KAL)


def _skenario(
    holdout: list[float] | None = None,
    jendela: list[list[float]] | None = None,
    penuh_tambah: list[float] | None = None,
) -> dict:
    """Skenario dasar yang lolos semua syarat default (min holdout 30, PF_R holdout ≥ 1,2, ekspektansi > 0,
    PF_R in-sample ≥ 1,1, ≥ 50% jendela ≥ 5 transaksi ber-PF_R > 1, max DD R penuh ≤ 15):
      holdout `_selang(20, 10)` → n 30, PF_R 2, ekspektansi 0,3333R
      jendela [+1,+1,+1,−1,−1] (1,5), [+2,−1,−1,+1,−1] (1,0), [+1,+1,−1,+1,−1] (1,5) → 2/3 = 66,67%
      in-sample = gabungan jendela: menang 9R, kalah 7R → PF_R 1,2857
      penuh = in-sample + holdout (+ `penuh_tambah`), urutan exit: jendela, holdout, tambahan.
    Kurva R kumulatif penuh: jendela 1, 2, 3, 2, 1 | 3, 2, 1, 2, 1 | 2, 3, 2, 3, 2 (puncak 3 → 1: DD 2R);
    holdout dari 2 berselang-seling naik ke puncak 12 (DD 1R) → max DD R penuh = 2R."""
    holdout = _selang(20, 10) if holdout is None else holdout
    jendela = [[1, 1, 1, -1, -1], [2, -1, -1, 1, -1], [1, 1, -1, 1, -1]] if jendela is None else jendela
    blok, pos = [], 0
    for r in [*jendela, holdout, penuh_tambah or []]:
        blok.append(_tx(r, pos))
        pos += len(r)
    tx_j, tx_ho, tx_tambah = blok[: len(jendela)], blok[-2], blok[-1]
    tx_is = [p for j in tx_j for p in j]
    tx_pn = tx_is + tx_ho + tx_tambah
    return {
        "holdout": _m(tx_ho), "in_sample": _m(tx_is), "jendela": [_m(j) for j in tx_j], "penuh": _m(tx_pn),
    }


def _gerbang(sk: dict, cfg=None, strategi: str = "PB") -> gb.HasilGerbang:
    return gb.nilai_gerbang(strategi, cfg=cfg or cfg_uji(), **sk)


def _kode_gagal(h: gb.HasilGerbang) -> list[str]:
    return [s.kode for s in h.gagal]


# ---------------------------------------------------------------------------
# G1–G6: setiap syarat gagal sendiri dengan alasan tepat
# ---------------------------------------------------------------------------


def test_skenario_dasar_lolos_semua_syarat():
    h = _gerbang(_skenario())
    assert h.lolos and h.gagal == [] and h.alasan() == ""
    assert [s.kode for s in h.syarat] == ["G1", "G2", "G3", "G4", "G5", "G6"]
    u = {s.kode: s.uraian for s in h.syarat}
    assert u["G1"] == "G1 holdout: transaksi tertutup 30 ≥ 30"
    assert u["G2"] == "G2 holdout: PF_R 2,00 ≥ 1,20"
    assert u["G3"] == "G3 holdout: ekspektansi 0,3333R > 0,0000R"
    assert u["G4"] == "G4 in-sample: PF_R 1,29 ≥ 1,10"
    assert u["G5"] == "G5 in-sample: 2 dari 3 jendela (≥ 5 transaksi) ber-PF_R > 1 = 66,67% ≥ 50,00%"
    assert u["G6"] == "G6 penuh: max drawdown 2,00R ≤ 15,00R"


def test_g1_gagal_sendiri():
    """29 transaksi holdout (19 × +1, 10 × −1): PF_R 1,9 dan ekspektansi > 0 tetap lolos."""
    h = _gerbang(_skenario(holdout=_selang(19, 10)))
    assert not h.lolos and _kode_gagal(h) == ["G1"]
    assert h.alasan() == "G1 holdout: transaksi tertutup 29 < 30"
    g1 = h.syarat[0]
    assert (g1.nilai, g1.ambang, g1.lolos) == (29, 30, False)


def test_g2_gagal_sendiri():
    """30 transaksi: 16 × +1, 14 × −1 → PF_R 16/14 = 1,142… < 1,2; ekspektansi 2/30 > 0."""
    h = _gerbang(_skenario(holdout=_selang(16, 14)))
    assert _kode_gagal(h) == ["G2"]
    assert h.alasan() == "G2 holdout: PF_R 1,14 < 1,20"
    assert h.syarat[1].nilai.nilai == D(16) / D(14)


def test_g3_gagal_sendiri():
    """Dengan ambang default 0, PF_R ≥ 1,2 berarti ekspektansi > 0; G3 hanya bisa gagal sendiri bila ambangnya
    dinaikkan: min_ekspektansi_r_holdout 0,5 > ekspektansi 0,3333."""
    h = _gerbang(_skenario(), cfg=cfg_uji({"gerbang.min_ekspektansi_r_holdout": 0.5}))
    assert _kode_gagal(h) == ["G3"]
    assert h.alasan() == "G3 holdout: ekspektansi 0,3333R ≤ 0,5000R"


def test_g3_tanpa_transaksi_holdout_gagal():
    h = _gerbang(_skenario(holdout=[]), cfg=cfg_uji({"gerbang.min_transaksi_holdout": 0}))
    assert _kode_gagal(h) == ["G3"]  # G1 0 ≥ 0 lolos; G2 n/a lolos karena G1 lolos
    assert h.syarat[2].uraian == "G3 holdout: ekspektansi R tidak tersedia (tanpa transaksi tertutup)"


def test_g4_gagal_sendiri():
    """Jendela [+1,+1,+1,−1,−1] (1,5) dan [−1,−1,−1,−1,+1] (0,25) → 1/2 = 50% ≥ 50% (G5 lolos);
    in-sample: menang 4R, kalah 6R → PF_R 0,67 < 1,1."""
    h = _gerbang(_skenario(jendela=[[1, 1, 1, -1, -1], [-1, -1, -1, -1, 1]]))
    assert _kode_gagal(h) == ["G4"]
    assert h.alasan() == "G4 in-sample: PF_R 0,67 < 1,10"


def test_g5_gagal_sendiri():
    """Jendela PF_R 0,75; 0,75; 2,5 → 1/3 = 33,33% < 50%; in-sample menang 16R, kalah 12R → 1,33 ≥ 1,1."""
    h = _gerbang(_skenario(jendela=[[3, -1, -1, -1, -1], [3, -1, -1, -1, -1], [10, -1, -1, -1, -1]]))
    assert _kode_gagal(h) == ["G5"]
    assert h.alasan() == "G5 in-sample: 1 dari 3 jendela (≥ 5 transaksi) ber-PF_R > 1 = 33,33% < 50,00%"
    assert h.syarat[4].nilai == D(100) / D(3)


def test_g5_tanpa_jendela_memenuhi_syarat_gagal():
    """Setiap jendela hanya 4 transaksi (< min_transaksi_per_jendela 5) → G5 gagal walau semuanya untung."""
    h = _gerbang(_skenario(jendela=[[1, 1, 1, -1], [2, 1, -1, 1]]))
    assert _kode_gagal(h) == ["G5"]
    assert h.alasan() == "G5 in-sample: tidak ada jendela dengan ≥ 5 transaksi tertutup (gagal)"
    assert h.syarat[4].nilai is None


def test_g5_jendela_tanpa_kerugian_dihitung_di_atas_1():
    """Jendela [+1 × 5] (PF_R n/a dengan transaksi menang → > 1) dan [+1, −1 × 4] (0,25) → 1/2 = 50%."""
    h = _gerbang(_skenario(jendela=[[1, 1, 1, 1, 1], [1, -1, -1, -1, -1]]))
    assert h.lolos
    assert h.syarat[4].uraian == "G5 in-sample: 1 dari 2 jendela (≥ 5 transaksi) ber-PF_R > 1 = 50,00% ≥ 50,00%"


def test_g6_gagal_sendiri():
    """Penuh: tambahan 16 × −1R berturut-turut sesudah puncak 12R di akhir holdout → 12 → −4: max DD R
    tepat 16 > 15. Holdout, in-sample, dan jendela tidak berubah sehingga G1–G5 tetap lolos."""
    sk = _skenario(penuh_tambah=[-1.0] * 16)
    h = _gerbang(sk)
    assert _kode_gagal(h) == ["G6"]
    assert sk["penuh"].max_drawdown_r == D(16)
    assert h.alasan() == "G6 penuh: max drawdown 16,00R > 15,00R"


def test_g2_tanpa_kerugian_lolos_hanya_jika_g1_lolos():
    lolos = _gerbang(_skenario(holdout=[1.0] * 30))
    assert lolos.lolos
    assert lolos.syarat[1].uraian == "G2 holdout: PF_R n/a (tanpa kerugian) — dianggap lolos karena G1 lolos"
    gagal = _gerbang(_skenario(holdout=[1.0] * 10))
    assert _kode_gagal(gagal) == ["G1", "G2"]
    assert gagal.syarat[1].uraian == "G2 holdout: PF_R n/a (tanpa kerugian) — tidak lolos karena G1 gagal"


def test_acak_tidak_pernah_dinilai_gerbang():
    with pytest.raises(ValueError, match="tidak dinilai gerbang"):
        _gerbang(_skenario(), strategi="ACAK")


def test_nilai_gerbang_dari_metrik_periode():
    """`nilai_gerbang_dari_metrik` memilih holdout/in-sample/penuh/jendela dari keluaran jendela.py."""
    sk = _skenario()
    P = jd.Periode
    daftar = [
        jd.MetrikPeriode(P(jd.IN_SAMPLE, date(2015, 1, 1), date(2024, 9, 30)), None, sk["in_sample"], None, {}, False),
        jd.MetrikPeriode(P(jd.HOLDOUT, date(2024, 10, 1), date(2025, 1, 1)), None, sk["holdout"], None, {}, False),
        jd.MetrikPeriode(P(jd.PENUH, date(2015, 1, 1), date(2025, 1, 1)), None, sk["penuh"], None, {}, False),
        *[
            jd.MetrikPeriode(P(jd.JENDELA, date(2015 + i, 1, 1), date(2015 + i, 12, 31), 2015 + i), None, m, None, {}, True)
            for i, m in enumerate(sk["jendela"])
        ],
    ]
    assert gb.nilai_gerbang_dari_metrik("pb", daftar, cfg_uji()) == _gerbang(sk)


# ---------------------------------------------------------------------------
# gate_status.json: isi, penggabungan per strategi, determinisme
# ---------------------------------------------------------------------------

SAAT = datetime(2026, 10, 15, 20, 0, tzinfo=ZONA)


def _entri(hasil: gb.HasilGerbang, **ubah) -> dict:
    arg = dict(
        hash_aturan="a" * 64, hash_input="b" * 64, tanggal_data_terakhir=date(2026, 10, 15),
        holdout_mulai=date(2024, 10, 1), subset_terjangkau_lemah=True,
        folder_run="output/backtest/20261015-200000_12345678", dibuat_pada=SAAT,
        rincian={"aturan": {"indikator": "1" * 16}, "input": {"universe.csv": "2" * 16}},
    )
    arg.update(ubah)
    return gb.entri_gate(hasil, **arg)


def test_isi_entri_gate_status():
    h = _gerbang(_skenario(holdout=[1.0] * 30))
    e = _entri(h)
    assert set(e) == {
        "lolos", "syarat", "hash_aturan", "hash_input", "tanggal_data_terakhir", "dibuat_pada", "holdout_mulai",
        "subset_terjangkau_lemah", "folder_run", "rincian_hash",
    }
    assert e["lolos"] is True and e["dibuat_pada"] == "2026-10-15T20:00:00+07:00"
    assert e["tanggal_data_terakhir"] == "2026-10-15" and e["holdout_mulai"] == "2024-10-01"
    assert list(e["syarat"]) == ["G1", "G2", "G3", "G4", "G5", "G6"]
    assert e["syarat"]["G1"] == {"nilai": 30, "ambang": 30, "lolos": True, "uraian": "G1 holdout: transaksi tertutup 30 ≥ 30"}
    assert e["syarat"]["G2"]["nilai"] == "n/a (tanpa kerugian)" and e["syarat"]["G2"]["ambang"] == 1.2
    assert e["syarat"]["G3"]["nilai"] == 1 and e["syarat"]["G4"]["nilai"] == 1.285714  # 9/7, 6 desimal
    assert e["folder_run"] == "output/backtest/20261015-200000_12345678"
    assert gb.entri_gate(h, hash_aturan="x", hash_input="y", tanggal_data_terakhir=date(2026, 1, 2),
                         holdout_mulai=date(2024, 10, 1), subset_terjangkau_lemah=False,
                         folder_run=Path("output") / "backtest" / "r")["folder_run"] == "output/backtest/r"
    json.dumps(e, allow_nan=False)  # semua nilai tipe JSON


def test_gate_status_hanya_ditulis_bila_ada_mode_edge(tmp_path):
    e = _entri(_gerbang(_skenario()))
    assert gb.gabung_gate_status(tmp_path, {"PB": e}, ["realistis"]) is None
    assert not (tmp_path / "gate_status.json").exists()
    path = gb.gabung_gate_status(tmp_path, {"PB": e}, ["edge", "realistis"])
    assert path == tmp_path / "gate_status.json" and path.exists()


def test_penggabungan_per_strategi_mempertahankan_entri_lama(tmp_path):
    pb_lama = _entri(_gerbang(_skenario()), hash_aturan="1" * 64, tanggal_data_terakhir=date(2026, 9, 1))
    bo_lama = _entri(_gerbang(_skenario(holdout=[1.0] * 10)), hash_aturan="2" * 64, tanggal_data_terakhir=date(2026, 8, 3),
                     dibuat_pada=datetime(2026, 8, 3, 20, 0, tzinfo=ZONA))
    gb.gabung_gate_status(tmp_path, {"PB": pb_lama, "BO": bo_lama}, ["edge"])
    pb_baru = _entri(_gerbang(_skenario(holdout=_selang(19, 10))), hash_aturan="3" * 64)
    gb.gabung_gate_status(tmp_path, {"pb": pb_baru}, ["edge"])
    isi = json.loads((tmp_path / "gate_status.json").read_text(encoding="utf-8"))
    assert isi["catatan"] == DISCLAIMER and "BUKAN SARAN INVESTASI" in isi["catatan"]
    assert set(isi["strategi"]) == {"PB", "BO"}
    assert isi["strategi"]["BO"] == bo_lama  # hash, tanggal, dan dibuat_pada BO tidak tersentuh
    assert isi["strategi"]["PB"] == pb_baru and isi["strategi"]["PB"]["lolos"] is False
    assert gb.baca_gate_status(tmp_path / "gate_status.json") == {"PB": pb_baru, "BO": bo_lama}
    with pytest.raises(ValueError, match="tidak dinilai gerbang"):
        gb.gabung_gate_status(tmp_path, {"ACAK": pb_baru}, ["edge"])


def test_gate_status_deterministik_hanya_beda_dibuat_pada(tmp_path):
    h = _gerbang(_skenario())
    a, b = tmp_path / "a", tmp_path / "b"
    gb.gabung_gate_status(a, {"PB": _entri(h), "MR": _entri(h)}, ["edge"])
    gb.gabung_gate_status(b, {"MR": _entri(h, dibuat_pada=SAAT + timedelta(hours=1)),
                              "PB": _entri(h, dibuat_pada=SAAT + timedelta(hours=1))}, ["edge"])
    ta, tb = ((p / "gate_status.json").read_text(encoding="utf-8") for p in (a, b))
    assert ta != tb and ta.endswith("}\n")
    beda = [(x, y) for x, y in zip(ta.splitlines(), tb.splitlines()) if x != y]
    assert len(beda) == 2 and all('"dibuat_pada"' in x and '"dibuat_pada"' in y for x, y in beda)
    assert ta.splitlines()[1].startswith('  "catatan": ')  # kunci urut, indent 2
    assert "≥" in ta  # ensure_ascii=False


def test_gate_status_rusak_ditulis_ulang_dan_dilaporkan(tmp_path):
    (tmp_path / "gate_status.json").write_text("{rusak", encoding="utf-8")
    with pytest.raises(gb.GateStatusRusak):
        gb.baca_gate_status(tmp_path / "gate_status.json")
    gb.gabung_gate_status(tmp_path, {"PB": _entri(_gerbang(_skenario()))}, ["edge"])
    assert set(gb.baca_gate_status(tmp_path / "gate_status.json")) == {"PB"}
    assert gb.baca_gate_status(tmp_path / "tidak_ada.json") is None


def test_gerbang_run_json(tmp_path):
    e = _entri(_gerbang(_skenario()))
    path = gb.tulis_gerbang_run(tmp_path / "run", {"PB": e})
    assert path == tmp_path / "run" / "gerbang_run.json"
    isi = json.loads(path.read_text(encoding="utf-8"))
    assert isi == {"catatan": DISCLAIMER, "strategi": {"PB": e}}


# ---------------------------------------------------------------------------
# Hash aturan (config)
# ---------------------------------------------------------------------------


def _hash(cfg) -> dict[str, str]:
    return {**{x: gb.hash_aturan_strategi(cfg, x) for x in gb.STRATEGI_SEMUA}, "global": gb.hash_aturan_global(cfg)}


DASAR = _hash(cfg_uji())


@pytest.mark.parametrize(
    "ubah",
    [
        {"indikator.atr_periode": 15},
        {"gerbang.min_pf_r_holdout": 1.3},
        {"pasar.auto_reject.terverifikasi": True, "pasar.auto_reject.berlaku_mulai": "2020-01-01",
         "pasar.auto_reject.harga_acuan": "close_kemarin", "pasar.auto_reject.ara": ARA_FIKTIF,
         "pasar.auto_reject.arb": ARB_FIKTIF},
        {"pasar.fraksi_harga.terverifikasi": True, "pasar.fraksi_harga.berlaku_mulai": "2020-01-01",
         "pasar.fraksi_harga.tabel": FRAKSI_FIKTIF},
        {"biaya.fee_beli_pct": 0.16},
        {"backtest.holdout_mulai": "2024-11-01"},
        {"risiko.maks_posisi": 4},
        {"akun.modal_awal_idr": 5_000_000},
        {"data.pengali_volume": 2},
    ],
    ids=["atr_periode", "min_pf_r_holdout", "verifikasi_auto_reject", "verifikasi_fraksi", "fee", "holdout_mulai",
         "maks_posisi", "modal_akun", "pengali_volume"],
)
def test_perubahan_aturan_mengubah_semua_hash_strategi(ubah):
    h = _hash(cfg_uji(ubah))
    assert all(h[k] != DASAR[k] for k in DASAR), ubah


def test_strategi_bo_tidak_mengubah_hash_pb_mr():
    h = _hash(cfg_uji({"strategi.bo.trailing_atr": 3.5}))
    assert h["PB"] == DASAR["PB"] and h["MR"] == DASAR["MR"] and h["ACAK"] == DASAR["ACAK"]
    assert h["BO"] != DASAR["BO"] and h["global"] != DASAR["global"]


def test_strategi_acak_tidak_mengubah_hash_global():
    h = _hash(cfg_uji({"strategi.acak.seed": 999}))
    assert h["global"] == DASAR["global"] and h["PB"] == DASAR["PB"]
    assert h["ACAK"] != DASAR["ACAK"]


@pytest.mark.parametrize(
    "ubah",
    [
        {"pasar.jadwal_sesi.terverifikasi": True, "pasar.jadwal_sesi.sumber": "Halaman fiktif",
         "pasar.jadwal_sesi.tanggal_cek": "2026-10-01", **{f"pasar.jadwal_sesi.{k}": v for k, v in JAM_FIKTIF.items()}},
        {"pasar.fraksi_harga.sumber": "Peraturan fiktif", "pasar.fraksi_harga.tanggal_cek": "2026-10-01"},
        {"pasar.auto_reject.sumber": "Pengumuman fiktif", "pasar.auto_reject.tanggal_cek": "2026-10-01",
         "pasar.auto_reject.papan_berlaku": "papan fiktif"},
        {"biaya.terverifikasi": True},
        {"data.pengali_volume_terverifikasi": True},
        {"data.jeda_antar_permintaan_detik": 2.0, "data.jeda_batas_permintaan_detik": 5, "data.jeda_data_final_menit": 30},
        {"data.maks_simbol_kena_batas_berturut": 9, "data.coba_lagi_tidak_ada_data_hari": 1,
         "data.tunggu_data_hari_ini.jumlah": 7},
        {"backtest.maks_pf_r_acak": 2.0, "gerbang.kedaluwarsa_hari": 30},
        {"laporan.maks_kandidat_ditampilkan": 3, "notifikasi.telegram.aktif": False, "jurnal.bootstrap_seed": 7,
         "operasional.simpan_log_hari": 5, "jadwal.jam_run": "18:45", "akun.maks_umur_hari_bursa": 9},
        {"universe.file": "lain/u.csv", "universe.file_riwayat": "lain/h.csv", "universe.file_exclude": "lain/e.csv",
         "universe.file_portfolio": "lain/p.csv", "akun.file_akun": "lain/a.yaml", "pasar.file_libur": "lain/l.csv",
         "data.folder_cache": "lain/c", "data.folder_manual": "lain/m", "data.file_anomali_ok": "lain/o.csv",
         "kondisi_pasar.file_breadth_universe": "lain/b.csv"},
    ],
    ids=["jadwal_sesi", "sumber_fraksi", "sumber_auto_reject", "biaya_terverifikasi", "volume_terverifikasi",
         "jeda", "unduhan", "acak_kedaluwarsa", "laporan_dll", "path"],
)
def test_bagian_dikecualikan_tidak_mengubah_hash(ubah):
    assert _hash(cfg_uji(ubah)) == DASAR


def test_hash_kanonik_tidak_bergantung_format_angka():
    """`1.0` vs `1` di YAML (Decimal) dan urutan kunci tidak mengubah hash."""
    assert _hash(cfg_uji({"risiko.risiko_per_transaksi_pct": 1})) == DASAR
    assert _hash(cfg_uji({"likuiditas.nilai_transaksi_min_idr": 10_000_000_000.0})) == DASAR
    assert gb.serial_kanonik({"b": D("1.50"), "a": date(2024, 1, 2)}) == '{"a":"2024-01-02","b":"1.5"}'
    assert gb.serial_kanonik({"x": D("100")}) == '{"x":"100"}'


def test_versi_program_ikut_hash(monkeypatch):
    import asisten_bei

    monkeypatch.setattr(asisten_bei, "__version__", "99.0.0-fiktif")
    assert all(v != DASAR[k] for k, v in _hash(cfg_uji()).items())


def test_kunci_path_yang_dikecualikan():
    """Semua kunci path berkas/folder hilang dari isi hash; kunci lain tetap ada."""
    sisa = gb.config_terhash(cfg_uji())
    assert "file_akun" not in sisa["akun"] and "file_libur" not in sisa["pasar"]
    assert not any(k.startswith(("file", "folder")) for k in sisa["universe"]) and "min_bar_riwayat" in sisa["universe"]
    assert "file_breadth_universe" not in sisa["kondisi_pasar"] and "folder_cache" not in sisa["data"]
    assert "terverifikasi" in sisa["pasar"]["fraksi_harga"] and "terverifikasi" in sisa["pasar"]["auto_reject"]
    assert not set(sisa) & set(gb.BAGIAN_DIKECUALIKAN)


# ---------------------------------------------------------------------------
# Hash input (berkas)
# ---------------------------------------------------------------------------


def _ctx(akar: Path, ubah: dict | None = None) -> KonteksApp:
    data = baca_yaml(akar / "config_uji.yaml")
    for kunci, nilai in (ubah or {}).items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = copy.deepcopy(nilai)
    return KonteksApp(validasi_dict(data), akar, akar / "config_uji.yaml")


def _tambah_baris(path: Path, baris: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + baris + "\n", encoding="utf-8")


def test_berkas_input_yang_di_hash(akar_uji):
    ctx = _ctx(akar_uji)
    assert list(gb.berkas_input(ctx)) == ["universe.csv", "breadth_universe.csv", "holidays.csv"]
    (akar_uji / "input_uji" / "anomali_ok.csv").write_text("kode,tanggal,catatan\nAAAA,2020-01-02,fiktif\n", encoding="utf-8")
    (akar_uji / "input_uji" / "universe_history.csv").write_text("kode,tanggal_masuk,tanggal_keluar\n", encoding="utf-8")
    assert list(gb.berkas_input(ctx)) == [
        "universe.csv", "universe_history.csv", "breadth_universe.csv", "anomali_ok.csv", "holidays.csv",
    ]
    assert "exclude.csv" in gb.berkas_input(_ctx(akar_uji, {"backtest.pakai_exclude": True}))


@pytest.mark.parametrize("berkas", ["universe.csv", "holidays.csv", "breadth_universe.csv"])
def test_isi_berkas_input_mengubah_hash_input(akar_uji, berkas):
    ctx = _ctx(akar_uji)
    lama, rinci = gb.hash_input(ctx), gb.rincian_input(ctx)
    baris = {"universe.csv": "ZZZZ,Fiktif Z,Fiktif", "holidays.csv": "2026-12-30,Libur fiktif", "breadth_universe.csv": "ZZZZ"}
    _tambah_baris(akar_uji / "input_uji" / berkas, baris[berkas])
    assert gb.hash_input(ctx) != lama
    baru = gb.rincian_input(ctx)
    assert [k for k in baru if baru[k] != rinci[k]] == [berkas]
    assert gb.hash_aturan_strategi(ctx.cfg, "PB") == gb.hash_aturan_strategi(_ctx(akar_uji).cfg, "PB")


def test_berkas_opsional_dan_exclude(akar_uji):
    ctx = _ctx(akar_uji)
    lama = gb.hash_input(ctx)
    _tambah_baris(akar_uji / "input_uji" / "exclude.csv", "ZZZZ,fiktif,,")
    _tambah_baris(akar_uji / "input_uji" / "portfolio.csv", "")
    assert gb.hash_input(ctx) == lama  # exclude tanpa pakai_exclude; portfolio tidak di-hash
    ctx_ex = _ctx(akar_uji, {"backtest.pakai_exclude": True})
    h_ex = gb.hash_input(ctx_ex)
    assert h_ex != lama
    _tambah_baris(akar_uji / "input_uji" / "exclude.csv", "YYYY,fiktif,,")
    assert gb.hash_input(ctx_ex) != h_ex
    (akar_uji / "input_uji" / "universe_history.csv").write_text("kode,tanggal_masuk,tanggal_keluar\n", encoding="utf-8")
    assert gb.hash_input(ctx) != lama


def test_path_berbeda_isi_sama_hash_sama(akar_uji):
    ctx = _ctx(akar_uji)
    lain = akar_uji / "lain"
    lain.mkdir()
    (lain / "u.csv").write_bytes((akar_uji / "input_uji" / "universe.csv").read_bytes())
    assert gb.hash_input(_ctx(akar_uji, {"universe.file": "lain/u.csv"})) == gb.hash_input(ctx)


def test_hash8_dan_nama_folder_run():
    h = gb.hash8("a" * 64, "b" * 64)
    assert len(h) == 8 and h == gb.hash8("a" * 64, "b" * 64) and h != gb.hash8("a" * 64, "c" * 64)
    assert all(c in "0123456789abcdef" for c in h)
    assert gb.nama_folder_run(h) == f"20261015-200000_{h}"  # waktu virtual conftest


# ---------------------------------------------------------------------------
# Run harian: apakah strategi dianggap lolos (dipakai tahap 6)
# ---------------------------------------------------------------------------

T = date(2026, 10, 15)


def _tulis_status(ctx: KonteksApp, strategi: list[str], *, lolos: bool = True, tanggal: date = T) -> Path:
    sk = _skenario() if lolos else _skenario(holdout=_selang(19, 10))
    entri = {}
    for x in strategi:
        ident = gb.identitas_hash(ctx, x)
        entri[x] = gb.entri_gate(
            _gerbang(sk), hash_aturan=ident.hash_aturan, hash_input=ident.hash_input, tanggal_data_terakhir=tanggal,
            holdout_mulai=ctx.cfg.backtest.holdout_mulai, subset_terjangkau_lemah=False,
            folder_run="output/backtest/20261015-200000_abcdef12", rincian=ident.rincian,
        )
    return gb.gabung_gate_status(ctx.path(gb.FOLDER_BACKTEST), entri, ["edge"])


def test_alasan_berkas_atau_entri_tidak_ada(akar_uji):
    ctx = _ctx(akar_uji)
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) == "gate_status.json tidak ada — Jalankan backtest.bat terlebih dahulu"
    _tulis_status(ctx, ["PB"])
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) is None
    assert gb.alasan_tidak_lolos_ctx(ctx, "BO", T) == (
        "strategi BO belum ada di gate_status.json — Jalankan backtest.bat terlebih dahulu"
    )
    gb.path_gate_status(ctx).write_text("[]", encoding="utf-8")
    assert "tidak sah" in gb.alasan_tidak_lolos_ctx(ctx, "PB", T)


@pytest.mark.parametrize(
    ("ubah", "bagian"),
    [
        ({"indikator.atr_periode": 15}, "indikator"),
        ({"gerbang.min_pf_r_holdout": 1.3}, "gerbang"),
        ({"pasar.auto_reject.terverifikasi": True, "pasar.auto_reject.berlaku_mulai": "2020-01-01",
          "pasar.auto_reject.harga_acuan": "close_kemarin", "pasar.auto_reject.ara": ARA_FIKTIF,
          "pasar.auto_reject.arb": ARB_FIKTIF}, "pasar"),
        ({"strategi.pb.target_r": 2.5}, "strategi.pb"),
    ],
)
def test_config_berubah_menjadi_bayangan_gerbang_dengan_bagian_yang_berubah(akar_uji, ubah, bagian):
    _tulis_status(_ctx(akar_uji), ["PB", "MR"])
    alasan = gb.alasan_tidak_lolos_ctx(_ctx(akar_uji, ubah), "PB", T)
    assert alasan == f"strategi PB: bagian config berubah sejak backtest: {bagian} — jalankan ulang backtest.bat"


def test_versi_program_berubah_disebut_versi_bukan_bagian_config(akar_uji, monkeypatch):
    import asisten_bei

    _tulis_status(_ctx(akar_uji), ["PB"])
    monkeypatch.setattr(asisten_bei, "__version__", "99.0.0-fiktif")
    assert gb.alasan_tidak_lolos_ctx(_ctx(akar_uji), "PB", T) == (
        "strategi PB: versi program berubah sejak backtest — jalankan ulang backtest.bat"
    )
    alasan = gb.alasan_tidak_lolos_ctx(_ctx(akar_uji, {"indikator.atr_periode": 15}), "PB", T)
    assert alasan == ("strategi PB: versi program berubah sejak backtest; bagian config berubah sejak backtest: "
                      "indikator — jalankan ulang backtest.bat")


def test_strategi_bo_berubah_tidak_membatalkan_pb_mr(akar_uji):
    _tulis_status(_ctx(akar_uji), ["PB", "BO", "MR"])
    ctx = _ctx(akar_uji, {"strategi.bo.stop_atr": 2.5})
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) is None and gb.alasan_tidak_lolos_ctx(ctx, "MR", T) is None
    assert "strategi.bo" in gb.alasan_tidak_lolos_ctx(ctx, "BO", T)


def test_metadata_verifikasi_tidak_membatalkan_gerbang(akar_uji):
    _tulis_status(_ctx(akar_uji), ["PB"])
    ctx = _ctx(akar_uji, {
        "pasar.jadwal_sesi.terverifikasi": True, "pasar.jadwal_sesi.sumber": "Halaman fiktif",
        "pasar.jadwal_sesi.tanggal_cek": "2026-10-01", **{f"pasar.jadwal_sesi.{k}": v for k, v in JAM_FIKTIF.items()},
        "pasar.fraksi_harga.sumber": "fiktif", "pasar.auto_reject.tanggal_cek": "2026-10-01",
        "biaya.terverifikasi": True,
    })
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) is None


@pytest.mark.parametrize(("berkas", "baris"), [("universe.csv", "ZZZZ,Fiktif Z,Fiktif"), ("holidays.csv", "2026-12-30,fiktif")])
def test_input_berubah_menjadi_bayangan_gerbang_dengan_berkas_yang_berubah(akar_uji, berkas, baris):
    ctx = _ctx(akar_uji)
    _tulis_status(ctx, ["PB"])
    _tambah_baris(akar_uji / "input_uji" / berkas, baris)
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) == (
        f"strategi PB: berkas input berubah sejak backtest: {berkas} — jalankan ulang backtest.bat"
    )


def test_kedaluwarsa_dan_lolos_false(akar_uji):
    ctx = _ctx(akar_uji)
    _tulis_status(ctx, ["PB"], tanggal=T - timedelta(days=90))
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) is None  # tepat 90 hari: belum kedaluwarsa
    _tulis_status(ctx, ["PB"], tanggal=T - timedelta(days=91))
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) == (
        "strategi PB: hasil gerbang kedaluwarsa (data backtest terakhir 2026-07-16, 91 hari kalender sebelum "
        "2026-10-15 > 90 hari) — jalankan ulang backtest.bat"
    )
    _tulis_status(ctx, ["PB"], lolos=False)
    assert gb.alasan_tidak_lolos_ctx(ctx, "PB", T) == (
        "strategi PB tidak lolos gerbang backtest: G1 holdout: transaksi tertutup 29 < 30"
    )


def test_hash_berbeda_tanpa_rincian_tetap_tidak_lolos():
    status = {"PB": {"hash_aturan": "lama", "hash_input": "x", "tanggal_data_terakhir": "2026-10-15", "lolos": True}}
    kw = dict(hash_input="x", t=T, kedaluwarsa_hari=90)
    assert gb.alasan_tidak_lolos(status, "pb", hash_aturan="baru", **kw) == (
        "strategi PB: aturan (config atau versi program) berubah sejak backtest — jalankan ulang backtest.bat"
    )
    assert gb.alasan_tidak_lolos(status, "PB", hash_aturan="lama", **kw) is None


# ---------------------------------------------------------------------------
# holdout_log.csv
# ---------------------------------------------------------------------------


def test_holdout_log_dibuat_dengan_disclaimer_lalu_ditambah(tmp_path, atur_waktu):
    m_pf = _m(_tx([2, -1]))
    m_na = _m(_tx([1]))
    m_kosong = _m([])
    baris = [
        gb.baris_holdout_log(holdout_mulai=date(2024, 10, 1), hash_aturan="A" * 64, hash_input="I" * 64,
                             strategi="pb", mode="edge", holdout=m_pf),
        gb.baris_holdout_log(holdout_mulai=date(2024, 10, 1), hash_aturan="A" * 64, hash_input="I" * 64,
                             strategi="pb", mode="realistis", holdout=m_na),
    ]
    path, peringatan = gb.tambah_holdout_log(tmp_path, baris)
    assert peringatan is None
    atur_waktu("2026-10-16T21:00:00+07:00")
    gb.tambah_holdout_log(tmp_path, [
        gb.baris_holdout_log(holdout_mulai=date(2024, 10, 1), hash_aturan="B" * 64, hash_input="I" * 64,
                             strategi="mr", mode="edge", holdout=m_kosong),
    ])
    teks = path.read_text(encoding="utf-8").splitlines()
    assert teks[0] == DISCLAIMER_CSV and teks[0].startswith("# BUKAN SARAN INVESTASI")
    assert teks[1] == "waktu,holdout_mulai,hash_aturan,hash_input,strategi,mode,pf_r_holdout,n_holdout"
    assert sum(1 for t in teks if t.startswith("#")) == 1 and len(teks) == 5
    assert teks[2] == f"2026-10-15T20:00:00+07:00,2024-10-01,{'A' * 64},{'I' * 64},pb,edge,2.0000,2"
    assert teks[3].endswith(",pb,realistis,n/a,1") and teks[4].endswith(",mr,edge,,0")
    df = pd.read_csv(path, comment="#")
    assert list(df.columns) == list(gb.KOLOM_HOLDOUT_LOG) and len(df) == 3
    r = gb.baca_holdout_log(path)
    assert (r.n_dilihat, r.m_konfigurasi, r.n_baris, r.holdout_mulai_pertama) == (2, 2, 3, "2024-10-01")
    assert not r.murni
    assert r.teks().startswith("Holdout telah dilihat 2 kali dengan 2 konfigurasi aturan berbeda.")
    assert "PERINGATAN" in r.teks()


def test_holdout_log_kosong_dan_satu_konfigurasi(tmp_path):
    r = gb.baca_holdout_log(tmp_path / "holdout_log.csv")
    assert (r.n_dilihat, r.m_konfigurasi, r.holdout_mulai_pertama) == (0, 0, None)
    (tmp_path / "holdout_log.csv").write_text("", encoding="utf-8")  # berkas kosong → diawali disclaimer + header
    gb.tambah_holdout_log(tmp_path, [gb.baris_holdout_log(
        holdout_mulai=date(2024, 10, 1), hash_aturan="A", hash_input="I", strategi="pb", mode="edge", holdout=_m([]))])
    teks = (tmp_path / "holdout_log.csv").read_text(encoding="utf-8").splitlines()
    assert teks[0] == DISCLAIMER_CSV and teks[1].startswith("waktu,") and len(teks) == 3
    r = gb.baca_holdout_log(tmp_path / "holdout_log.csv")
    assert r.murni and r.teks() == "Holdout telah dilihat 1 kali dengan 1 konfigurasi aturan berbeda."


# ---------------------------------------------------------------------------
# Subset terjangkau (§16.7)
# ---------------------------------------------------------------------------


def _pos_subset(p: str, s: str, status: str, r: float = 1.0) -> ex.Posisi:
    return buat_posisi(f"S{p}", tgl(0), tgl(1), r, meta={
        "harga_maks_nominal": D(p), "stop_nominal": D(s), "status_pasar": status, "ekuitas_sinyal": D(1_000_000_000),
    })


def test_terjangkau_menurut_aturan_realistis_contoh_14_2():
    """Modal realistis Rp 3.000.000 (ikuti_akun) → anggaran Rp 30.000 (HATI-HATI 15.000), alokasi 1.000.000.
    A (2.030/1.888): risiko 1 lot 15.164,83 → terjangkau saat BOLEH BELI, tidak saat HATI-HATI.
    B (9.125/8.500): 66.841,63 > 30.000 → tidak. C (458/410) saat HATI-HATI: 5.012,10 ≤ 15.000 → terjangkau.
    10.500/10.290: risiko 26.173,93 ≤ 30.000 tetapi 1 lot = 1.051.575 > alokasi 1.000.000 → tidak.
    JANGAN BELI → tidak pernah terjangkau. Ekuitas mode edge (meta ekuitas_sinyal) tidak dipakai."""
    cfg = cfg_uji()
    assert gb.terjangkau_realistis(_pos_subset("2030", "1888", "BOLEH_BELI"), cfg)
    assert not gb.terjangkau_realistis(_pos_subset("2030", "1888", "HATI_HATI"), cfg)
    assert not gb.terjangkau_realistis(_pos_subset("9125", "8500", "BOLEH_BELI"), cfg)
    assert gb.terjangkau_realistis(_pos_subset("458", "410", "HATI_HATI"), cfg)
    assert not gb.terjangkau_realistis(_pos_subset("10500", "10290", "BOLEH_BELI"), cfg)
    assert not gb.terjangkau_realistis(_pos_subset("458", "410", "JANGAN_BELI"), cfg)
    besar = cfg_uji({"backtest.mode.realistis.modal_idr": 10_000_000})  # anggaran 100.000, alokasi 3.333.333,33
    assert gb.terjangkau_realistis(_pos_subset("9125", "8500", "BOLEH_BELI"), besar)
    with pytest.raises(ValueError, match="meta rencana"):
        gb.terjangkau_realistis(buat_posisi("X", tgl(0), tgl(1), 1), cfg)


def test_subset_terjangkau_n_pf_ekspektansi_dan_lemah():
    daftar = [
        _pos_subset("2030", "1888", "BOLEH_BELI", 2.0),
        _pos_subset("458", "410", "BOLEH_BELI", -1.0),
        _pos_subset("9125", "8500", "BOLEH_BELI", 5.0),  # tidak terjangkau: R-nya tidak ikut
        _pos_subset("2030", "1888", "HATI_HATI", -3.0),  # tidak terjangkau
    ]
    s = gb.subset_terjangkau(daftar, cfg_uji())
    assert (s.n, s.n_holdout) == (2, 4)
    assert s.pf_r.nilai == D(2) and s.ekspektansi_r == D("0.5")
    assert s.lemah  # n 2 < min_transaksi_holdout 30
    longgar = cfg_uji({"gerbang.min_transaksi_holdout": 2})
    assert not gb.subset_terjangkau(daftar, longgar).lemah  # PF_R 2 ≥ 1,2 dan n ≥ 2
    seimbang = [_pos_subset("2030", "1888", "BOLEH_BELI", 1.0), _pos_subset("458", "410", "BOLEH_BELI", -1.0)]
    assert gb.subset_terjangkau(seimbang, longgar).lemah  # PF_R 1 < 1,2
    tanpa_rugi = [_pos_subset("2030", "1888", "BOLEH_BELI", 1.0), _pos_subset("458", "410", "BOLEH_BELI", 1.0)]
    assert not gb.subset_terjangkau(tanpa_rugi, longgar).lemah  # PF_R n/a (tanpa kerugian)
    kosong = gb.subset_terjangkau([], cfg_uji({"gerbang.min_transaksi_holdout": 0}))
    assert kosong.n == 0 and kosong.lemah and kosong.ekspektansi_r is None


# ---------------------------------------------------------------------------
# Integrasi dengan mesin.simulasikan
# ---------------------------------------------------------------------------

LONGGAR = {
    "gerbang.min_transaksi_holdout": 1, "gerbang.min_pf_r_holdout": 0.5, "gerbang.min_ekspektansi_r_holdout": -1.0,
    "gerbang.min_pf_r_insample": 0.5, "gerbang.min_pct_jendela_pf_di_atas_1": 0.0,
}


def _jalankan_gerbang(akar: Path, cfg, saat: datetime) -> tuple[Path, gb.HasilGerbang]:
    """Rantai jalankan.py yang diharapkan: simulasi edge → periode → metrik → G1–G6 → subset → berkas."""
    h = simulasi_integrasi("edge")
    pb = jd.bagi_periode(h, cfg)
    metrik = jd.metrik_semua_periode(pb, KAL, cfg)
    # Transaksi berasal dari ACAK (pembangkit transaksi uji); fungsi gerbang hanya membaca metrik, label PB
    # dipakai sebagai kunci berkas karena ACAK tidak pernah dinilai gerbang.
    hasil = gb.nilai_gerbang_dari_metrik("PB", metrik, cfg)
    subset = gb.subset_terjangkau(pb.holdout.tertutup, cfg)
    folder_bt = akar / "output" / "backtest"
    nama = gb.nama_folder_run(gb.hash8("g" * 64, "i" * 64), saat)
    e = gb.entri_gate(
        hasil, hash_aturan=gb.hash_aturan_strategi(cfg, "PB"), hash_input="i" * 64,
        tanggal_data_terakhir=pb.akhir_data, holdout_mulai=cfg.backtest.holdout_mulai,
        subset_terjangkau_lemah=subset.lemah, folder_run=f"output/backtest/{nama}", dibuat_pada=saat,
    )
    gb.tulis_gerbang_run(folder_bt / nama, {"PB": e})
    holdout = next(m for m in metrik if m.periode.jenis == jd.HOLDOUT).transaksi
    gb.tambah_holdout_log(folder_bt, [gb.baris_holdout_log(
        holdout_mulai=cfg.backtest.holdout_mulai, hash_aturan=gb.hash_aturan_global(cfg), hash_input="i" * 64,
        strategi="acak", mode="edge", holdout=holdout, saat=saat)])
    return gb.gabung_gate_status(folder_bt, {"PB": e}, ["edge", "realistis"]), hasil


def test_integrasi_mesin_ke_gate_status(tmp_path):
    cfg = pasar_integrasi()[0]
    h = simulasi_integrasi("edge")
    assert len(h.tertutup) > 20
    path, hasil = _jalankan_gerbang(tmp_path / "a", cfg, SAAT)
    assert not hasil.lolos  # ACAK di pasar ini: PF_R holdout < 1,2 dan transaksi < 30
    isi = json.loads(path.read_text(encoding="utf-8"))
    pb = isi["strategi"]["PB"]
    assert pb["lolos"] is False and pb["syarat"]["G1"]["lolos"] is False
    assert pb["syarat"]["G1"]["nilai"] == len(jd.bagi_periode(h, cfg).holdout.tertutup)
    assert pb["tanggal_data_terakhir"] == h.ekuitas[-1].tanggal.isoformat()
    assert (tmp_path / "a" / "output" / "backtest" / pb["folder_run"].split("/")[-1] / "gerbang_run.json").exists()
    cfg_l = cfg_uji({**UBAH_INTEGRASI, **LONGGAR})
    path2, hasil2 = _jalankan_gerbang(tmp_path / "b", cfg_l, SAAT + timedelta(minutes=5))
    assert hasil2.lolos, hasil2.alasan()
    # Dua run identik → gate_status.json byte-identik kecuali dibuat_pada (dan folder_run yang memuat jam run).
    path3, _ = _jalankan_gerbang(tmp_path / "c", cfg_l, SAAT + timedelta(minutes=5))
    assert path2.read_bytes() == path3.read_bytes()
    log = gb.baca_holdout_log(tmp_path / "b" / "output" / "backtest" / "holdout_log.csv")
    assert log.n_dilihat == 1 and log.baris[0]["strategi"] == "acak" and int(log.baris[0]["n_holdout"]) > 0
    status = gb.baca_gate_status(path2)
    assert gb.alasan_tidak_lolos(status, "PB", hash_aturan=gb.hash_aturan_strategi(cfg_l, "PB"), hash_input="i" * 64,
                                 t=h.ekuitas[-1].tanggal, kedaluwarsa_hari=90) is None
    assert gb.alasan_tidak_lolos(gb.baca_gate_status(path), "PB", hash_aturan=gb.hash_aturan_strategi(cfg, "PB"),
                                 hash_input="i" * 64, t=h.ekuitas[-1].tanggal, kedaluwarsa_hari=90).startswith(
        "strategi PB tidak lolos gerbang backtest: G1 holdout: transaksi tertutup ")


def test_integrasi_subset_terjangkau_dari_transaksi_mesin():
    """Posisi mesin membawa meta rencana (harga nominal, status pasar) sehingga subset bisa dihitung; di pasar
    sintetis ini harga ± Rp 1.000–2.000 dengan jarak stop kecil → semua transaksi holdout terjangkau."""
    cfg = pasar_integrasi()[0]
    pb = jd.bagi_periode(simulasi_integrasi("edge"), cfg)
    s = gb.subset_terjangkau(pb.holdout.tertutup, cfg)
    assert s.n_holdout == len(pb.holdout.tertutup) > 0
    assert 0 < s.n <= s.n_holdout
    harap = mt.profit_factor([p.keluar.r for p in s.transaksi])
    assert s.pf_r == harap and s.lemah  # n < 30


def test_holdout_log_terkunci_ditulis_ke_nama_alternatif_dan_tetap_dihitung(tmp_path, monkeypatch, atur_waktu):
    """§20.2: berkas keluaran terkunci (dibuka di Excel) → nama alternatif + peringatan, run berlanjut. Baris di
    berkas alternatif tetap dihitung untuk N/M."""
    def baris(h: str):
        return [gb.baris_holdout_log(holdout_mulai=date(2024, 10, 1), hash_aturan=h, hash_input="I",
                                     strategi="pb", mode="edge", holdout=_m([]))]

    utama, _ = gb.tambah_holdout_log(tmp_path, baris("A"))
    asli = os.replace

    def replace_terkunci(src, dst, *a, **k):
        if Path(dst).name == "holdout_log.csv":
            raise PermissionError(13, "terkunci (FIKTIF)")
        return asli(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace_terkunci)
    atur_waktu("2026-10-16T21:00:00+07:00")
    alt, peringatan = gb.tambah_holdout_log(tmp_path, baris("B"))
    monkeypatch.setattr(os, "replace", asli)
    assert alt.name == "holdout_log_210000.csv" and "terkunci" in peringatan
    assert len(utama.read_text(encoding="utf-8").splitlines()) == 3  # berkas utama tidak berubah
    r = gb.baca_holdout_log(utama)
    assert (r.n_dilihat, r.m_konfigurasi, r.n_baris) == (2, 2, 2)  # baris lama di alternatif tidak dobel
    atur_waktu("2026-10-17T21:00:00+07:00")
    gb.tambah_holdout_log(tmp_path, baris("A"))  # berkas utama sudah bisa ditulis lagi
    r = gb.baca_holdout_log(utama)
    assert (r.n_dilihat, r.m_konfigurasi, r.n_baris) == (3, 2, 3)


def _baris_log(h: str, holdout_mulai: date = date(2024, 10, 1)):
    return [gb.baris_holdout_log(holdout_mulai=holdout_mulai, hash_aturan=h, hash_input="I",
                                 strategi="pb", mode="edge", holdout=_m([]))]


def _kunci_tulis(monkeypatch, nama: str = "holdout_log.csv"):
    asli = os.replace

    def replace_terkunci(src, dst, *a, **k):
        if Path(dst).name == nama:
            raise PermissionError(13, "terkunci (FIKTIF)")
        return asli(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace_terkunci)


def test_holdout_log_tidak_terbaca_n_m_disebut_batas_bawah(tmp_path, monkeypatch, atur_waktu):
    utama, _ = gb.tambah_holdout_log(tmp_path, _baris_log("A"))
    atur_waktu("2026-10-16T21:00:00+07:00")
    gb.tambah_holdout_log(tmp_path, _baris_log("B"))
    asli = Path.read_text

    def baca_terkunci(self, *a, **k):
        if self.name == "holdout_log.csv":
            raise PermissionError(13, "terkunci (FIKTIF)")
        return asli(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", baca_terkunci)
    _kunci_tulis(monkeypatch)
    atur_waktu("2026-10-17T21:00:00+07:00")
    alt, peringatan = gb.tambah_holdout_log(tmp_path, _baris_log("A", date(2024, 11, 1)))
    assert alt.name == "holdout_log_210000.csv" and "terkunci" in peringatan
    r = gb.baca_holdout_log(utama)
    assert r.tidak_terbaca == ("holdout_log.csv",) and not r.lengkap and r.perlu_peringatan
    assert (r.n_dilihat, r.m_konfigurasi) == (1, 1)  # hanya baris yang terbaca
    assert "sedikitnya 1 kali dengan sedikitnya 1 konfigurasi" in r.teks() and "tidak terbaca" in r.teks()
    monkeypatch.setattr(Path, "read_text", asli)
    r = gb.baca_holdout_log(utama)
    assert r.lengkap and (r.n_dilihat, r.m_konfigurasi) == (3, 2)
    assert r.holdout_mulai_pertama == "2024-10-01"  # baris dengan waktu paling awal, bukan urutan berkas


def test_holdout_log_alternatif_tidak_menimpa_alternatif_lama(tmp_path, monkeypatch, atur_waktu):
    utama, _ = gb.tambah_holdout_log(tmp_path, _baris_log("A"))
    _kunci_tulis(monkeypatch)
    atur_waktu("2026-10-16T21:00:00+07:00")
    alt1, _ = gb.tambah_holdout_log(tmp_path, _baris_log("B"))
    atur_waktu("2026-10-17T21:00:00+07:00")  # jam yang sama pada hari berikutnya
    alt2, _ = gb.tambah_holdout_log(tmp_path, _baris_log("A"))
    assert (alt1.name, alt2.name) == ("holdout_log_210000.csv", "holdout_log_210000_2.csv")
    r = gb.baca_holdout_log(utama)
    assert (r.n_dilihat, r.m_konfigurasi, r.n_baris) == (3, 2, 3) and not r.murni
