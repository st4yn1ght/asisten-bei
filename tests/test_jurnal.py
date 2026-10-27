"""T-JR: jurnal kertas (SPEC §17) lewat perintah `harian`/`jurnal` pada proyek sintetis (FIKTIF – KHUSUS UJI).

Data: `test_backtest_cli` (acak berseed, Senin–Jumat 2024-01-01 … 2025-06-30, universe AAAA/BBBB/CCCC, breadth +DDDD),
gate_status dari backtest edge. Waktu virtual pukul 20:00 WIB (sesudah `jadwal.jam_run` 19:30).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pandas as pd

from asisten_bei import jurnal as jr
from asisten_bei.config import muat_config
from asisten_bei.kalender import Kalender


def _sinyal(p, tanggal: str) -> list[dict]:
    return p.tabel("sinyal", "WHERE tanggal_sinyal = ? ORDER BY kode, strategi", (tanggal,))


def test_run_ganda_t_final_idempoten(proyek):
    h1 = proyek.harian("2025-05-14")
    assert h1.kode == 0, h1.teks
    sebelum = proyek.dump()
    assert sebelum["sinyal"] and len(sebelum["run"]) == 1
    h2 = proyek.harian("2025-05-14T21:00:00+07:00")
    assert h2.kode == 0
    assert proyek.dump() == sebelum  # jurnal tidak berubah
    assert "sudah tercatat" in h2.teks
    # Laporan run ulang tidak menimpa versi sebelumnya.
    folder = proyek.folder_laporan / "2025-05-14"
    assert sorted(p.name for p in folder.iterdir() if p.is_dir()) == ["run_200000", "run_210000"]
    assert (folder / "laporan.md").is_file()


def test_hari_terlewat_dikejar_sebagai_susulan(proyek):
    assert proyek.harian("2025-05-12").kode == 0
    h = proyek.harian("2025-05-15")
    assert "2 hari dikejar" in h.teks
    for d in ("2025-05-13", "2025-05-14"):
        for r in _sinyal(proyek, d):
            assert (r["susulan"], r["ditampilkan"]) == (1, 0)
            assert r["status_live"]  # status seleksi tetap dihitung
    assert _sinyal(proyek, "2025-05-14"), "prasyarat: ada sinyal pada hari susulan"
    for r in _sinyal(proyek, "2025-05-15"):
        assert (r["susulan"], r["ditampilkan"]) == (0, 1)
    hari = json.loads(proyek.tabel("run")[-1]["hari_diproses"])
    assert [x["tanggal"] for x in hari] == ["2025-05-13", "2025-05-14", "2025-05-15"]
    assert [x["susulan"] for x in hari] == [1, 1, 0]


def test_tidak_lengkap_lalu_lengkap_menambah_sinyal_lama_dipertahankan(proyek):
    # CCCC juga saham breadth: ambang cakupan breadth ikut diturunkan agar tidak K0 (kode 0, bukan 2).
    proyek.ubah_config({"data.validasi.min_cakupan_universe_pct": 50, "kondisi_pasar.min_cakupan_breadth_pct": 50})
    lengkap = proyek.bar("CCCC")
    proyek.hapus_bar("CCCC", "2025-05-14")
    h1 = proyek.harian("2025-05-14")
    assert h1.kode == 0 and "TIDAK_LENGKAP" in h1.teks
    awal = {(r["kode"], r["strategi"]): r for r in _sinyal(proyek, "2025-05-14")}
    assert awal and all(r["sementara"] == 1 and r["ditampilkan"] == 1 for r in awal.values())
    assert not any(k == "CCCC" for k, _s in awal)
    proyek.tulis_bar("CCCC", lengkap)
    h2 = proyek.harian("2025-05-14T22:00:00+07:00")
    assert h2.kode == 0 and "Status data run: LENGKAP" in h2.teks
    akhir = {(r["kode"], r["strategi"]): r for r in _sinyal(proyek, "2025-05-14")}
    baru = set(akhir) - set(awal)
    assert baru and all(k == "CCCC" for k, _s in baru)
    for k in baru:
        assert (akhir[k]["ditampilkan"], akhir[k]["sementara"], akhir[k]["susulan"]) == (0, 0, 0)
    for k, r in awal.items():  # dipertahankan, kini final
        assert akhir[k]["sinyal_id"] == r["sinyal_id"] and akhir[k]["sementara"] == 0
        assert akhir[k]["tidak_muncul_di_data_final"] == 0


def test_sinyal_tidak_muncul_lagi_ditandai(proyek):
    # Run pertama TIDAK_LENGKAP (bar breadth DDDD hilang → K0): himpunan sinyal 2025-05-14 masih sementara.
    proyek.ubah_config({"data.validasi.min_cakupan_universe_pct": 50})
    ddd = proyek.bar("DDDD")
    proyek.hapus_bar("DDDD", "2025-05-14")
    assert proyek.harian("2025-05-14").kode == 2
    ada = _sinyal(proyek, "2025-05-14")
    assert ada and all(r["sementara"] == 1 for r in ada)
    kode = ada[0]["kode"]
    proyek.tulis_bar("DDDD", ddd)
    proyek.hapus_bar(kode, "2025-05-14")  # revisi: bar sinyal hilang → sinyal tidak muncul lagi
    proyek.ubah_config({"kondisi_pasar.min_cakupan_breadth_pct": 50})
    assert proyek.harian("2025-05-15").kode in (0, 2)
    r = next(x for x in _sinyal(proyek, "2025-05-14") if x["kode"] == kode)
    assert r["tidak_muncul_di_data_final"] == 1 and r["data_direvisi"] == 1


def test_split_sesudah_sinyal_bukan_revisi_palsu_tetapi_revisi_nyata_ditandai(proyek):
    assert proyek.harian("2025-05-14").kode == 0
    rows = _sinyal(proyek, "2025-05-14")
    kode = rows[0]["kode"]
    df = proyek.bar(kode)
    # Split 1:2 pada 2025-05-16, disesuaikan sumber: seluruh riwayat tersesuaikan = nominal lama ÷ 2 (harga
    # nominal pasca-split juga separuh), volume × 2. bar_saat_sinyal (nominal, F = 1 saat dicatat) ÷ F_terkini = 2
    # sama dengan cache baru → bukan revisi.
    s = pd.Timestamp("2025-05-16")
    for k in ("open", "high", "low", "close"):
        df[k] = df[k] / 2
    df["volume"] = df["volume"] * 2
    df.loc[s, "split"] = 2.0
    proyek.tulis_bar(kode, df)
    assert proyek.harian("2025-05-20").kode == 0
    assert all(r["data_direvisi"] == 0 for r in _sinyal(proyek, "2025-05-14"))
    # Revisi nyata: close sinyal berubah 5 % (> toleransi_revisi_pct 0,5 %).
    c = float(df.loc[pd.Timestamp("2025-05-14"), "close"])
    proyek.ubah_bar(kode, "2025-05-14", close=c * 1.05, high=max(c * 1.05, float(df.loc[pd.Timestamp("2025-05-14"), "high"])))
    proyek.harian("2025-05-21")
    r = next(x for x in _sinyal(proyek, "2025-05-14") if x["kode"] == kode)
    assert r["data_direvisi"] == 1


def _kertas(p) -> list[dict]:
    return p.tabel("kertas_transaksi", "WHERE status_isi = 'TERISI' ORDER BY tanggal_entry, kode")


def _kunci(r: dict) -> tuple:
    return (r["kode"], r["strategi"], r["tanggal_sinyal"], r["tanggal_entry"], r["lembar"], r["harga_beli_nominal"])


def test_perubahan_universe_tidak_mengubah_transaksi_kertas_lalu(proyek):
    for d in ("2025-05-14", "2025-05-15", "2025-05-16", "2025-05-19", "2025-05-20"):
        assert proyek.harian(d).kode in (0, 2)
    kertas = _kertas(proyek)
    assert kertas, "prasyarat: ada transaksi kertas"
    dihapus = kertas[0]["kode"]
    uni = (proyek.akar / "input_uji" / "universe.csv").read_text(encoding="utf-8").splitlines()
    proyek.tulis_input("universe.csv", "\n".join(b for b in uni if not b.startswith(dihapus + ",")) + "\n")
    assert proyek.harian("2025-05-21").kode in (0, 2)
    sesudah = {_kunci(r) for r in _kertas(proyek) if r["tanggal_sinyal"] <= "2025-05-20"}
    assert {_kunci(r) for r in kertas} <= sesudah
    snap = json.loads(proyek.tabel("run")[-1]["snapshot_input"])
    assert dihapus not in {u["kode"] for u in snap["universe"]}


def _info(waktu_run: str, t: str, lolos: bool, status: str = "LENGKAP") -> jr.InfoRun:
    return jr.InfoRun(
        waktu_run=datetime.fromisoformat(waktu_run), tanggal_data=date.fromisoformat(t), untuk_tanggal=None,
        status_data_run=status, status_pasar="BOLEH_BELI", alasan_pasar="", hash_aturan="h", hash_input="i",
        versi_program="0", gerbang_snapshot={"PB": {"lolos": lolos}, "BO": {"lolos": False}, "MR": {"lolos": lolos}},
        snapshot_input={"universe": [{"kode": "AAAA", "sektor": "S1"}], "exclude": []},
    )


def test_gerbang_hari_susulan_dari_run_sebelum_hari_itu(tmp_path, proyek):
    ctx = muat_config(proyek.config, proyek.akar)
    kal = Kalender({})
    with jr.Jurnal(tmp_path / "j.sqlite") as j:
        jr.catat(j, _info("2025-05-12T20:00:00+07:00", "2025-05-12", True), [], kal=kal, cfg=ctx.cfg)
        # Run berikutnya (gerbang tidak lolos) baru terjadi 2025-05-15 dan memproses 13–15 sebagai susulan.
        jr.catat(j, _info("2025-05-15T20:00:00+07:00", "2025-05-15", False), [], kal=kal, cfg=ctx.cfg)
        with j.con:
            j.con.execute("UPDATE run SET hari_diproses = ? WHERE run_id = 2", (json.dumps(
                [{"tanggal": d, "final": 1, "susulan": int(d < "2025-05-15")}
                 for d in ("2025-05-13", "2025-05-14", "2025-05-15")]),))
            j.con.execute("UPDATE run SET hari_diproses = ? WHERE run_id = 1", (json.dumps(
                [{"tanggal": "2025-05-12", "final": 1, "susulan": 0}]),))
        pada = jr.snapshot_per_hari(j)
        for d in ("2025-05-12", "2025-05-13", "2025-05-14"):
            s = pada(date.fromisoformat(d))
            assert s.lolos == {"PB", "MR"} and not s.gerbang_dari_pemroses
        s = pada(date(2025, 5, 15))  # run 2025-05-15 20:00 < 2025-05-16 00:00
        assert s.lolos == frozenset() and not s.gerbang_dari_pemroses
    with jr.Jurnal(tmp_path / "k.sqlite") as j:
        jr.catat(j, _info("2025-05-15T20:00:00+07:00", "2025-05-15", True), [], kal=kal, cfg=ctx.cfg)
        with j.con:
            j.con.execute("UPDATE run SET hari_diproses = ?", (json.dumps(
                [{"tanggal": "2025-05-14", "final": 1, "susulan": 1}]),))
        s = jr.snapshot_per_hari(j)(date(2025, 5, 14))  # tidak ada run sebelum 2025-05-15 00:00
        assert s.lolos == {"PB", "MR"} and s.gerbang_dari_pemroses


def test_belum_tersedia_tidak_menulis_sinyal(proyek):
    assert proyek.harian("2025-05-13").kode == 0
    for k in ("AAAA", "BBBB", "CCCC"):
        proyek.hapus_bar(k, "2025-05-14")
    h = proyek.harian("2025-05-14")
    assert h.kode == 2 and "DATA BELUM TERSEDIA" in h.teks
    assert _sinyal(proyek, "2025-05-14") == []
    run = proyek.tabel("run")[-1]
    assert run["status_data_run"] == "BELUM_TERSEDIA" and json.loads(run["hari_diproses"]) == []


def test_hitung_ulang_turunan_setelah_revisi_data(proyek):
    for d in ("2025-04-28", "2025-05-22"):  # run kedua mengejar 2025-04-29 … 2025-05-21 (susulan)
        proyek.harian(d)
    tertutup = [r for r in _kertas(proyek) if r["alasan_exit"] != "TERBUKA"]
    assert tertutup, "prasyarat: ada transaksi kertas tertutup"
    r = tertutup[0]
    # Revisi: open hari entry melonjak di atas harga_maks → order seharusnya BATAL_GAP.
    df = proyek.bar(r["kode"])
    ts = pd.Timestamp(r["tanggal_entry"])
    o = float(df.loc[ts, "open"]) * 1.5
    proyek.ubah_bar(r["kode"], r["tanggal_entry"], open=o, high=max(o, float(df.loc[ts, "high"])))
    h = proyek.harian("2025-05-23")
    assert _kunci(r) not in {_kunci(x) for x in _kertas(proyek)}
    assert "transaksi kertas berubah karena revisi data" in (proyek.folder_laporan / "2025-05-23" / "laporan.md").read_text(
        encoding="utf-8")
    assert h.kode in (0, 2)


def test_hasil_sinyal_dihitung_untuk_semua_kecuali_stop_terlalu_jauh(proyek):
    for d in ("2025-05-14", "2025-05-15", "2025-05-16", "2025-05-19"):
        proyek.harian(d)
    sinyal = proyek.tabel("sinyal")
    hasil = {r["sinyal_id"]: r for r in proyek.tabel("hasil_sinyal")}
    for s in sinyal:
        if s["status_live"] == "STOP_TERLALU_JAUH":
            assert s["sinyal_id"] not in hasil
        else:
            assert s["sinyal_id"] in hasil
    terisi = [h for h in hasil.values() if h["status_isi"] == "TERISI"]
    assert terisi and all(h["harga_entry"] and h["tanggal_entry"] for h in terisi)


def test_periode_kertas_baru_saat_hash_aturan_berubah(proyek):
    assert proyek.harian("2025-05-14").kode in (0, 2)
    assert proyek.harian("2025-05-15").kode in (0, 2)
    lama = proyek.tabel("kertas_ekuitas", "WHERE periode_id = 1 ORDER BY tanggal")
    proyek.ubah_config({"strategi.pb.target_r": 2.5})
    assert proyek.harian("2025-05-16").kode in (0, 2)
    per = proyek.tabel("kertas_periode", "ORDER BY periode_id")
    assert len(per) == 2
    assert per[0]["selesai"] == "2025-05-16" and per[1]["mulai"] == "2025-05-16" and per[1]["selesai"] is None
    assert per[0]["hash_aturan"] != per[1]["hash_aturan"]
    assert proyek.tabel("kertas_ekuitas", "WHERE periode_id = 1 ORDER BY tanggal") == lama  # dibekukan


def test_rekonsiliasi_fixture_nol_selisih(proyek):
    for d in ("2025-05-14", "2025-05-15", "2025-05-19", "2025-05-20"):
        proyek.harian(d)
    h = proyek.cli("2025-05-20T21:00:00+07:00", "jurnal", "--rekonsiliasi")
    assert h.kode == 0, h.teks
    assert "0 selisih" in h.teks
    assert "BUKAN SARAN INVESTASI" in h.teks


def test_rekonsiliasi_selisih_tetap_kode_0_dan_memberi_petunjuk(proyek):
    proyek.harian("2025-05-14")
    r = _sinyal(proyek, "2025-05-14")[0]
    df = proyek.bar(r["kode"])
    ts = pd.Timestamp("2025-05-14")
    proyek.ubah_bar(r["kode"], "2025-05-14", close=float(df.loc[ts, "close"]) * 1.2,
                    high=float(df.loc[ts, "high"]) * 1.2)
    h = proyek.cli("2025-05-14T21:00:00+07:00", "jurnal", "--rekonsiliasi")
    assert h.kode == 0
    assert "0 selisih" not in h.teks and r["kode"] in h.teks


def test_replay_tanpa_catat_tidak_menulis_dan_catat_ulang(proyek):
    proyek.harian("2025-05-15")
    sebelum = proyek.dump()
    h = proyek.harian("2025-05-21T20:00:00+07:00", "--tanggal", "2025-05-14")
    assert h.kode in (0, 2) and "REPLAY" in h.teks
    assert proyek.dump() == sebelum
    lama = {r["sinyal_id"] for r in _sinyal(proyek, "2025-05-15")}
    assert lama
    h = proyek.harian("2025-05-15T21:00:00+07:00", "--catat-ulang")
    assert h.kode == 0
    baru = _sinyal(proyek, "2025-05-15")
    run_id = proyek.tabel("run")[-1]["run_id"]
    assert {r["sinyal_id"] for r in baru}.isdisjoint(lama)  # id tidak pernah dipakai ulang
    assert all(r["ditampilkan"] == 1 and r["run_id"] == run_id for r in baru)
    assert "--catat-ulang" in (proyek.tabel("run")[-1]["catatan"] or "")


def test_ekspor_setiap_tabel_dengan_disclaimer(proyek):
    proyek.harian("2025-05-14")
    folder = proyek.akar / "output" / "jurnal"
    for t in jr.TABEL:
        teks = (folder / f"{t}.csv").read_text(encoding="utf-8")
        assert teks.startswith("# BUKAN SARAN INVESTASI")
    h = proyek.cli("2025-05-14T21:00:00+07:00", "jurnal", "--ekspor")
    assert h.kode == 0 and "6 berkas" in h.teks


def test_jurnal_rusak_kode_6_laporan_tetap_dibuat(proyek):
    proyek.path_jurnal.parent.mkdir(parents=True, exist_ok=True)
    proyek.path_jurnal.write_bytes(b"bukan database sqlite " * 100)
    h = proyek.harian("2025-05-14")
    assert h.kode == 6
    teks = (proyek.folder_laporan / "2025-05-14" / "laporan.md").read_text(encoding="utf-8")
    assert "Jurnal gagal" in teks and "rusak" in teks


def test_jurnal_terkunci_kode_6(proyek, monkeypatch):
    proyek.harian("2025-05-13")
    monkeypatch.setattr(jr, "BATAS_TUNGGU_KUNCI_DETIK", 0.05)
    kunci = sqlite3.connect(proyek.path_jurnal, timeout=0.05)
    kunci.execute("BEGIN EXCLUSIVE")
    try:
        h = proyek.harian("2025-05-14")
    finally:
        kunci.rollback()
        kunci.close()
    assert h.kode == 6
    assert "terkunci" in (proyek.folder_laporan / "2025-05-14" / "laporan.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pemantauan penyimpangan (§17.4)
# ---------------------------------------------------------------------------


def _cfg(**ubah):
    from bantu_pasar import cfg_uji

    return cfg_uji({f"jurnal.{k}": v for k, v in ubah.items()})


def test_bootstrap_deterministik_dan_memberi_peringatan():
    cfg = _cfg(min_transaksi_peringatan=5, bootstrap_iterasi=2000)
    holdout = [Decimal(x) for x in ("1.5", "-1", "2", "-1", "0.5", "1", "-0.8", "2.2")]
    buruk = [Decimal("-1")] * 6
    a = jr.uji_penyimpangan(buruk, holdout, cfg, "PB")
    b = jr.uji_penyimpangan(buruk, holdout, cfg, "PB")
    assert a == b and a.peringatan and a.persentil is not None
    baik = [Decimal("1")] * 6
    assert not jr.uji_penyimpangan(baik, holdout, cfg, "PB").peringatan
    c = jr.uji_penyimpangan(buruk, holdout, _cfg(min_transaksi_peringatan=5, bootstrap_iterasi=2000, bootstrap_seed=7),
                            "PB")
    assert c.persentil != a.persentil  # seed dari config dipakai


def test_bootstrap_sampel_kecil_belum_diuji():
    cfg = _cfg()
    p = jr.uji_penyimpangan([Decimal("-1")] * 3, [Decimal("1")] * 10, cfg, "MR")
    assert not p.peringatan and "belum diuji" in p.catatan


def test_hari_untuk_diproses_dan_final():
    kal = Kalender({})
    t = date(2025, 5, 16)
    assert jr.hari_untuk_diproses({}, kal, t, None) == [t]
    assert jr.hari_untuk_diproses({}, kal, t, date(2025, 5, 14)) == [date(2025, 5, 14), date(2025, 5, 15), t]
    diproses = {date(2025, 5, 12): True, date(2025, 5, 13): False}
    assert jr.hari_untuk_diproses(diproses, kal, t, None) == [date(2025, 5, 13), date(2025, 5, 14), date(2025, 5, 15), t]
    assert jr.final_pada(date(2025, 5, 13), t, "TIDAK_LENGKAP", kal, 3)  # 14, 15, 16 berlalu
    assert not jr.final_pada(date(2025, 5, 14), t, "TIDAK_LENGKAP", kal, 3)
    assert jr.final_pada(t, t, "LENGKAP", kal, 3)

