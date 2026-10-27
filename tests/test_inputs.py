"""Uji berkas input (SPEC §6, §11, §15.3; §22.1 T-INP dan bagian T-UN: exclude, universe_history, sektor).

Semua berkas ditulis sebagai byte ke `tmp_path` (tanpa terjemahan baris baru), sehingga BOM, CRLF,
dan byte non-UTF-8 diuji persis.
"""

from __future__ import annotations

import codecs
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from asisten_bei import waktu
from asisten_bei.config import muat_config
from asisten_bei.inputs import (
    PESAN_REGIONAL,
    Akun,
    AnomaliOk,
    BarisExclude,
    BarisPortofolio,
    PERINGATAN,
    InputTidakValid,
    Masalah,
    Masukan,
    RiwayatKonstituen,
    SahamUniverse,
    baca_akun,
    baca_anomali_ok,
    baca_breadth,
    baca_csv_ketat,
    baca_exclude,
    baca_libur,
    baca_portofolio,
    baca_riwayat,
    baca_universe,
    cek_portofolio_kalender,
    cek_umur_akun,
    muat_masukan,
    parse_desimal,
    parse_int,
    parse_kode,
    parse_tanggal,
    pastikan_valid,
    sektor_tidak_diketahui,
)
from asisten_bei.kalender import Kalender

AKAR_PROYEK = Path(__file__).resolve().parents[1]
FIXTURES = AKAR_PROYEK / "tests" / "fixtures"

HEADER_PORTOFOLIO = "kode,tanggal_beli,harga_beli,lot,strategi,stop_awal,sektor,harga_per_tanggal,lembar,catatan"
MODAL = Decimal("3000000")


def tulis(path: Path, teks: str, bom: bool = False, encoding: str = "utf-8") -> Path:
    data = teks.encode(encoding)
    if bom:
        data = codecs.BOM_UTF8 + data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def error(masalah: list[Masalah]) -> list[Masalah]:
    return [m for m in masalah if m.level == "ERROR"]


def peringatan(masalah: list[Masalah]) -> list[Masalah]:
    return [m for m in masalah if m.level == "PERINGATAN"]


def masukan(**kw) -> Masukan:
    dasar = dict(
        universe=[], breadth=[], riwayat=None, exclude=[], portofolio=[],
        akun=Akun(MODAL, date(2026, 10, 9), True),
    )
    dasar.update(kw)
    return Masukan(**dasar)


def baris_portofolio(kode="ABCD", tanggal_beli=date(2026, 10, 8), harga_per_tanggal=None, baris=2, **kw):
    nilai = dict(
        kode=kode, tanggal_beli=tanggal_beli, harga_beli=Decimal("2850"), lot=2, strategi="PB",
        stop_awal=Decimal("2720"), sektor=None, harga_per_tanggal=harga_per_tanggal, lembar=None,
        catatan="", baris=baris,
    )
    nilai.update(kw)
    return BarisPortofolio(**nilai)


# ---------------------------------------------------------------------------
# Pengurai nilai
# ---------------------------------------------------------------------------


class TestParseTanggal:
    def test_iso_diterima(self):
        assert parse_tanggal("2026-10-08") == date(2026, 10, 8)
        assert parse_tanggal(" 2026-10-08 ") == date(2026, 10, 8)

    @pytest.mark.parametrize(
        "s",
        ["08/10/2026", "8/10/2026", "08-10-2026", "08.10.2026", "8/10/26", "2026/10/08",
         "8 Okt 2026", "08-Okt-2026", "2026-10-08 00:00:00", "2026-10-8", "20261008"],
    )
    def test_format_lain_ditolak_dengan_pesan_regional(self, s):
        with pytest.raises(ValueError) as e:
            parse_tanggal(s)
        assert PESAN_REGIONAL in str(e.value)
        assert "YYYY-MM-DD" in str(e.value)

    @pytest.mark.parametrize("s", ["2026-02-30", "2026-13-01", "2026-00-10"])
    def test_tanggal_tidak_ada_di_kalender(self, s):
        with pytest.raises(ValueError) as e:
            parse_tanggal(s)
        assert "tidak ada di kalender" in str(e.value)
        assert PESAN_REGIONAL not in str(e.value)

    @pytest.mark.parametrize("s", ["", "   ", "besok"])
    def test_bukan_tanggal(self, s):
        with pytest.raises(ValueError):
            parse_tanggal(s)


class TestParseAngka:
    @pytest.mark.parametrize(
        "s, harapan",
        [("2850", Decimal("2850")), ("2850.5", Decimal("2850.5")), ("-1.25", Decimal("-1.25")),
         ("0", Decimal("0")), ("0.1", Decimal("0.1")), (" 42 ", Decimal("42"))],
    )
    def test_desimal_valid(self, s, harapan):
        hasil = parse_desimal(s)
        assert isinstance(hasil, Decimal)
        assert hasil == harapan
        assert str(hasil) == s.strip()  # tanpa jalan memutar lewat float

    @pytest.mark.parametrize("s", ["1,5", "2850,50", "2.850,50", "1,000.50", "1.000.000"])
    def test_desimal_regional(self, s):
        with pytest.raises(ValueError) as e:
            parse_desimal(s)
        assert PESAN_REGIONAL in str(e.value)

    @pytest.mark.parametrize("s", ["", " ", "Rp 2850", "1e3", "+5", ".5", "5.", "2 850", "NaN", "inf"])
    def test_desimal_tidak_valid(self, s):
        with pytest.raises(ValueError):
            parse_desimal(s)

    def test_titik_ribuan_disebut(self):
        with pytest.raises(ValueError, match="pemisah ribuan"):
            parse_desimal("1.000.000")

    @pytest.mark.parametrize("s, harapan", [("2", 2), ("-3", -3), ("007", 7), (" 10 ", 10)])
    def test_int_valid(self, s, harapan):
        assert parse_int(s) == harapan

    @pytest.mark.parametrize("s", ["2.0", "1.000", "", "dua", "1e2"])
    def test_int_tidak_valid(self, s):
        with pytest.raises(ValueError):
            parse_int(s)

    def test_int_koma_regional(self):
        with pytest.raises(ValueError) as e:
            parse_int("1,5")
        assert PESAN_REGIONAL in str(e.value)


class TestParseKode:
    @pytest.mark.parametrize("s", ["ABCD", "BBCA", "A1B2", "IHSG", "ABCDEFGHIJKL"])
    def test_valid(self, s):
        assert parse_kode(s) == s

    def test_huruf_kecil_tidak_diubah_otomatis(self):
        with pytest.raises(ValueError) as e:
            parse_kode("abcd")
        assert "huruf besar" in str(e.value) and "'ABCD'" in str(e.value)

    @pytest.mark.parametrize("s", ["ABCD.JK", "abcd.jk", "ABCD.jk"])
    def test_akhiran_jk(self, s):
        with pytest.raises(ValueError) as e:
            parse_kode(s)
        assert ".JK" in str(e.value) and "'ABCD'" in str(e.value)

    @pytest.mark.parametrize("s", ["", "AB-CD", "AB CD", "ABCDEFGHIJKLM", "^JKSE"])
    def test_tidak_valid(self, s):
        with pytest.raises(ValueError):
            parse_kode(s)


# ---------------------------------------------------------------------------
# Pembaca CSV ketat
# ---------------------------------------------------------------------------

WAJIB_U = ["kode", "nama", "sektor"]


class TestBacaCsvKetat:
    def test_bom_diterima(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\nAAAA,Satu,Keuangan\n", bom=True)
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert masalah == []
        assert baris == [{"kode": "AAAA", "nama": "Satu", "sektor": "Keuangan", "_baris": 2}]

    def test_crlf_baris_kosong_dan_nomor_baris(self, tmp_path):
        teks = "kode,nama,sektor\r\nAAAA,Satu,Keuangan\r\n\r\n  \r\n,,\r\nBBBB,Dua,Energi\r\n\r\n"
        p = tulis(tmp_path / "u.csv", teks)
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert masalah == []
        assert [(b["kode"], b["_baris"]) for b in baris] == [("AAAA", 2), ("BBBB", 6)]

    def test_nilai_di_strip_dan_kutip(self, tmp_path):
        p = tulis(tmp_path / "u.csv", ' kode , nama ,sektor\n  AAAA , "Contoh, Tbk",  Keuangan \n')
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert masalah == []
        assert baris[0]["kode"] == "AAAA"
        assert baris[0]["nama"] == "Contoh, Tbk"
        assert baris[0]["sektor"] == "Keuangan"

    def test_teks_setelah_kutip_penutup_ditolak(self, tmp_path):
        # Tidak ditebak: '"Contoh, Tbk" ,' bukan CSV yang sah.
        p = tulis(tmp_path / "u.csv", 'kode,nama,sektor\nAAAA,"Contoh, Tbk" ,Keuangan\n')
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris == [] and len(error(masalah)) == 1 and masalah[0].baris == 2

    def test_kolom_opsional_boleh_tidak_ada(self, tmp_path):
        p = tulis(tmp_path / "x.csv", "tanggal\n2026-12-25\n")
        baris, masalah = baca_csv_ketat(p, ["tanggal"], ["keterangan"])
        assert masalah == []
        assert baris == [{"tanggal": "2026-12-25", "keterangan": "", "_baris": 2}]

    def test_urutan_kolom_bebas(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "sektor,kode,nama\nKeuangan,AAAA,Satu\n")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert masalah == [] and baris[0]["kode"] == "AAAA"

    def test_pemisah_titik_koma(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode;nama;sektor\nAAAA;Satu;Keuangan\n", bom=True)
        baris, masalah = baca_csv_ketat(p, WAJIB_U, nama_berkas="input/universe.csv")
        assert baris == []
        assert len(masalah) == 1
        m = masalah[0]
        assert (m.level, m.berkas, m.baris) == ("ERROR", "input/universe.csv", 1)
        assert PESAN_REGIONAL in m.pesan
        assert str(m).startswith("ERROR input/universe.csv baris 1: ")

    def test_pemisah_tab(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode\tnama\tsektor\nAAAA\tSatu\tKeuangan\n")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris == [] and len(error(masalah)) == 1 and "tab" in masalah[0].pesan

    def test_kolom_wajib_hilang(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama\nAAAA,Satu\n")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris == []
        assert len(masalah) == 1 and masalah[0].baris == 1
        assert "kolom wajib tidak ada: sektor" in masalah[0].pesan

    def test_kolom_tidak_dikenal(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor,harga\nAAAA,Satu,Keuangan,1\n")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris == []
        assert "kolom tidak dikenal: harga" in masalah[0].pesan

    def test_kolom_ganda_dan_tanpa_nama(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor,kode,\nAAAA,Satu,Keuangan,AAAA,\n")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        teks = " | ".join(m.pesan for m in masalah)
        assert baris == []
        assert "kolom ganda di header: kode" in teks
        assert "tanpa nama" in teks

    def test_header_huruf_besar_ditolak(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "Kode,Nama,Sektor\nAAAA,Satu,Keuangan\n")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris == [] and error(masalah)

    def test_berkas_kosong(self, tmp_path):
        for isi in ("", "\n\n", "﻿"):
            p = tulis(tmp_path / "u.csv", isi)
            baris, masalah = baca_csv_ketat(p, WAJIB_U)
            assert baris == [] and len(error(masalah)) == 1 and "kosong" in masalah[0].pesan

    def test_baris_pertama_bukan_header(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "\nkode,nama,sektor\nAAAA,Satu,Keuangan\n")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris == [] and masalah[0].baris == 1 and "header" in masalah[0].pesan

    def test_berkas_tidak_ada(self, tmp_path):
        baris, masalah = baca_csv_ketat(tmp_path / "tidak_ada.csv", WAJIB_U, nama_berkas="input/x.csv")
        assert baris == []
        assert masalah == [Masalah("ERROR", "input/x.csv", None, "berkas tidak ditemukan")]

    def test_bukan_utf8(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\nAAAA,Satu,Keuangan\nBBBB,Café,Energi\n", encoding="cp1252")
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris == []
        assert len(masalah) == 1 and masalah[0].baris == 3 and "UTF-8" in masalah[0].pesan

    def test_bukan_utf8_dengan_titik_koma(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode;nama;sektor\nBBBB;Café;Energi\n", encoding="cp1252")
        _, masalah = baca_csv_ketat(p, WAJIB_U)
        assert any(PESAN_REGIONAL in m.pesan for m in masalah)

    def test_kolom_berlebih_koma_desimal(self, tmp_path):
        teks = HEADER_PORTOFOLIO + "\nABCD,2026-10-08,2850,5,2,PB,2720,,,,\nEFGH,2026-10-08,1000,1,PB,950,,,,\n"
        p = tulis(tmp_path / "p.csv", teks)
        baris, masalah = baca_csv_ketat(p, HEADER_PORTOFOLIO.split(","))
        assert [b["kode"] for b in baris] == ["EFGH"]  # baris lain tetap dibaca
        assert len(masalah) == 1
        assert masalah[0].baris == 2 and PESAN_REGIONAL in masalah[0].pesan

    def test_kolom_kurang(self, tmp_path):
        p = tulis(tmp_path / "p.csv", HEADER_PORTOFOLIO + "\nABCD,2026-10-08,2850,2,PB,2720\n")
        baris, masalah = baca_csv_ketat(p, HEADER_PORTOFOLIO.split(","))
        assert baris == []
        assert masalah[0].baris == 2
        assert "6 kolom" in masalah[0].pesan and "10 kolom" in masalah[0].pesan
        assert PESAN_REGIONAL not in masalah[0].pesan

    def test_titik_koma_di_baris_data(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\nAAAA;Satu;Keuangan\n")
        _, masalah = baca_csv_ketat(p, WAJIB_U)
        assert masalah[0].baris == 2 and PESAN_REGIONAL in masalah[0].pesan

    def test_kutip_tidak_seimbang(self, tmp_path):
        p = tulis(tmp_path / "u.csv", 'kode,nama,sektor\nAAAA,"Con"x,Keuangan\n')
        _, masalah = baca_csv_ketat(p, WAJIB_U)
        assert len(error(masalah)) == 1 and masalah[0].baris == 2

    def test_rekaman_multibaris_memakai_baris_awal(self, tmp_path):
        p = tulis(tmp_path / "u.csv", 'kode,nama,sektor\nAAAA,"dua\nbaris",Keuangan\nBBBB,x,y,z\n')
        baris, masalah = baca_csv_ketat(p, WAJIB_U)
        assert baris[0]["_baris"] == 2
        assert masalah[0].baris == 4


def test_masalah_str():
    assert str(Masalah("ERROR", "input/universe.csv", 3, "kode kosong")) == (
        "ERROR input/universe.csv baris 3: kode kosong"
    )
    assert str(Masalah("PERINGATAN", "input/akun.yaml", None, "x")) == "PERINGATAN input/akun.yaml: x"


# ---------------------------------------------------------------------------
# universe.csv, breadth_universe.csv
# ---------------------------------------------------------------------------


class TestUniverse:
    def test_contoh_spec_dengan_bom(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\nAAAA,Contoh Satu Tbk,Keuangan\nBBBB,Contoh Dua Tbk,Infrastruktur\n", bom=True)
        hasil, masalah = baca_universe(p)
        assert masalah == []
        assert hasil == [
            SahamUniverse("AAAA", "Contoh Satu Tbk", "Keuangan"),
            SahamUniverse("BBBB", "Contoh Dua Tbk", "Infrastruktur"),
        ]

    def test_kode_ganda_error(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\nAAAA,Satu,Keuangan\nBBBB,Dua,Energi\nAAAA,Satu Lagi,Energi\n")
        hasil, masalah = baca_universe(p, "input/universe.csv")
        assert [s.kode for s in hasil] == ["AAAA", "BBBB"]
        assert len(error(masalah)) == 1
        m = masalah[0]
        assert (m.berkas, m.baris) == ("input/universe.csv", 4)
        assert "AAAA" in m.pesan and "baris 2" in m.pesan

    def test_kode_ganda_tetap_terdeteksi_walau_baris_pertama_salah(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\nAAAA,Satu,\nAAAA,Satu,Keuangan\n")
        _, masalah = baca_universe(p)
        assert [m.baris for m in error(masalah)] == [2, 3]

    def test_sektor_wajib(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\nAAAA,Satu,\nBBBB,,Energi\n")
        hasil, masalah = baca_universe(p)
        assert [s.kode for s in hasil] == ["BBBB"]  # nama boleh kosong
        assert len(error(masalah)) == 1
        assert masalah[0].baris == 2 and "sektor wajib diisi" in masalah[0].pesan

    @pytest.mark.parametrize("kode, potongan", [("aaaa", "huruf besar"), ("AAAA.JK", ".JK"), ("IHSG", "indeks")])
    def test_kode_tidak_valid(self, tmp_path, kode, potongan):
        p = tulis(tmp_path / "u.csv", f"kode,nama,sektor\n{kode},Satu,Keuangan\n")
        hasil, masalah = baca_universe(p)
        assert hasil == []
        assert len(error(masalah)) == 1 and potongan in masalah[0].pesan and masalah[0].baris == 2

    def test_hanya_header_peringatan(self, tmp_path):
        p = tulis(tmp_path / "u.csv", "kode,nama,sektor\n")
        hasil, masalah = baca_universe(p)
        assert hasil == [] and error(masalah) == [] and len(peringatan(masalah)) == 1

    def test_berkas_wajib(self, tmp_path):
        hasil, masalah = baca_universe(tmp_path / "tidak_ada.csv")
        assert hasil == [] and "tidak ditemukan" in error(masalah)[0].pesan


class TestBreadth:
    def test_ganda_peringatan_dipakai_sekali(self, tmp_path):
        p = tulis(tmp_path / "b.csv", "kode\nAAAA\nCCCC\nAAAA\n")
        hasil, masalah = baca_breadth(p)
        assert hasil == ["AAAA", "CCCC"]
        assert error(masalah) == []
        assert len(peringatan(masalah)) == 1 and masalah[0].baris == 4 and "dipakai sekali" in masalah[0].pesan

    def test_kode_salah_error(self, tmp_path):
        p = tulis(tmp_path / "b.csv", "kode\nAAAA\nbbbb\n")
        hasil, masalah = baca_breadth(p)
        assert hasil == ["AAAA"] and error(masalah)[0].baris == 3

    def test_berkas_wajib(self, tmp_path):
        _, masalah = baca_breadth(tmp_path / "tidak_ada.csv")
        assert error(masalah)


# ---------------------------------------------------------------------------
# universe_history.csv (§6.3) dan exclude.csv (§6.4) — T-UN
# ---------------------------------------------------------------------------


class TestRiwayat:
    def test_opsional_tidak_ada(self, tmp_path):
        assert baca_riwayat(tmp_path / "universe_history.csv") == (None, [])

    def test_contoh_spec(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "kode,tanggal_masuk,tanggal_keluar\nAAAA,2015-01-01,\nDDDD,2016-02-01,2019-07-31\n")
        hasil, masalah = baca_riwayat(p)
        assert masalah == []
        assert hasil == [
            RiwayatKonstituen("AAAA", date(2015, 1, 1), None),
            RiwayatKonstituen("DDDD", date(2016, 2, 1), date(2019, 7, 31)),
        ]

    def test_masuk_setelah_keluar_error(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "kode,tanggal_masuk,tanggal_keluar\nAAAA,2020-01-02,2019-12-31\nBBBB,2020-01-02,2020-01-02\n")
        hasil, masalah = baca_riwayat(p)
        assert [r.kode for r in hasil] == ["BBBB"]  # masuk = keluar sah (satu hari)
        assert len(error(masalah)) == 1 and masalah[0].baris == 2

    def test_tanggal_masuk_wajib_dan_format(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "kode,tanggal_masuk,tanggal_keluar\nAAAA,,\nBBBB,01/02/2016,\n")
        hasil, masalah = baca_riwayat(p)
        assert hasil == []
        assert [m.baris for m in error(masalah)] == [2, 3]
        assert PESAN_REGIONAL in masalah[1].pesan

    def test_tumpang_tindih_peringatan(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "kode,tanggal_masuk,tanggal_keluar\nAAAA,2015-01-01,2018-12-31\nAAAA,2018-06-01,\nBBBB,2015-01-01,2016-12-31\nBBBB,2017-01-02,\n")
        hasil, masalah = baca_riwayat(p)
        assert len(hasil) == 4
        assert error(masalah) == []
        assert len(peringatan(masalah)) == 1 and "AAAA" in masalah[0].pesan

    def test_hanya_header(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "kode,tanggal_masuk,tanggal_keluar\n")
        hasil, masalah = baca_riwayat(p)
        assert hasil == [] and error(masalah) == [] and len(peringatan(masalah)) == 1


class TestExclude:
    def test_berkas_wajib(self, tmp_path):
        hasil, masalah = baca_exclude(tmp_path / "exclude.csv", "input/exclude.csv")
        assert hasil == [] and error(masalah)[0].berkas == "input/exclude.csv"

    def test_hanya_header_sah(self, tmp_path):
        p = tulis(tmp_path / "e.csv", "kode,alasan,tanggal_mulai,tanggal_akhir\n")
        assert baca_exclude(p) == ([], [])

    def test_contoh_spec(self, tmp_path):
        p = tulis(tmp_path / "e.csv", "kode,alasan,tanggal_mulai,tanggal_akhir\nEEEE,Suspensi,2026-10-01,\nFFFF,Notasi khusus,,\n")
        hasil, masalah = baca_exclude(p)
        assert masalah == []
        assert hasil == [
            BarisExclude("EEEE", "Suspensi", date(2026, 10, 1), None),
            BarisExclude("FFFF", "Notasi khusus", None, None),
        ]

    def test_mulai_setelah_akhir_error(self, tmp_path):
        p = tulis(tmp_path / "e.csv", "kode,alasan,tanggal_mulai,tanggal_akhir\nEEEE,x,2026-10-02,2026-10-01\n")
        hasil, masalah = baca_exclude(p)
        assert hasil == [] and error(masalah)[0].baris == 2

    def test_tanggal_regional(self, tmp_path):
        p = tulis(tmp_path / "e.csv", "kode,alasan,tanggal_mulai,tanggal_akhir\nEEEE,x,01/10/2026,\n")
        _, masalah = baca_exclude(p)
        assert PESAN_REGIONAL in error(masalah)[0].pesan

    @pytest.mark.parametrize(
        "mulai, akhir, d, harapan",
        [
            (date(2026, 10, 1), date(2026, 10, 9), date(2026, 9, 30), False),
            (date(2026, 10, 1), date(2026, 10, 9), date(2026, 10, 1), True),   # tepi bawah inklusif
            (date(2026, 10, 1), date(2026, 10, 9), date(2026, 10, 9), True),   # tepi atas inklusif
            (date(2026, 10, 1), date(2026, 10, 9), date(2026, 10, 10), False),
            (date(2026, 10, 1), None, date(2026, 9, 30), False),
            (date(2026, 10, 1), None, date(2026, 10, 1), True),
            (date(2026, 10, 1), None, date(2099, 1, 1), True),                 # tanpa batas atas
            (None, date(2026, 10, 9), date(2000, 1, 3), True),                 # tanpa batas bawah
            (None, date(2026, 10, 9), date(2026, 10, 9), True),
            (None, date(2026, 10, 9), date(2026, 10, 12), False),
            (None, None, date(2026, 10, 15), True),
            (date(2026, 10, 5), date(2026, 10, 5), date(2026, 10, 5), True),   # satu hari
        ],
    )
    def test_aktif_pada_tepi(self, mulai, akhir, d, harapan):
        assert BarisExclude("EEEE", "x", mulai, akhir).aktif_pada(d) is harapan

    def test_dikecualikan_pada_beberapa_baris(self):
        m = masukan(exclude=[
            BarisExclude("EEEE", "Suspensi", date(2026, 1, 5), date(2026, 1, 9)),
            BarisExclude("EEEE", "Notasi", date(2026, 3, 2), None),
            BarisExclude("FFFF", "Pemantauan", None, None),
        ])
        assert m.dikecualikan_pada("EEEE", date(2026, 1, 4)) is None
        assert m.dikecualikan_pada("EEEE", date(2026, 1, 5)).alasan == "Suspensi"
        assert m.dikecualikan_pada("EEEE", date(2026, 1, 9)).alasan == "Suspensi"
        assert m.dikecualikan_pada("EEEE", date(2026, 2, 2)) is None
        assert m.dikecualikan_pada("EEEE", date(2026, 3, 2)).alasan == "Notasi"
        assert m.dikecualikan_pada("FFFF", date(2013, 1, 2)).alasan == "Pemantauan"
        assert m.dikecualikan_pada("GGGG", date(2026, 1, 5)) is None


class TestKonstituen:
    RIWAYAT = [
        RiwayatKonstituen("AAAA", date(2015, 1, 1), None),
        RiwayatKonstituen("DDDD", date(2016, 2, 1), date(2019, 7, 31)),
        RiwayatKonstituen("DDDD", date(2021, 2, 1), date(2022, 7, 29)),  # keluar lalu masuk lagi
    ]

    @pytest.mark.parametrize(
        "kode, d, harapan",
        [
            ("AAAA", date(2014, 12, 31), False),
            ("AAAA", date(2015, 1, 1), True),
            ("AAAA", date(2026, 10, 15), True),
            ("DDDD", date(2016, 1, 29), False),
            ("DDDD", date(2016, 2, 1), True),     # masuk inklusif
            ("DDDD", date(2019, 7, 31), True),    # keluar inklusif
            ("DDDD", date(2019, 8, 1), False),
            ("DDDD", date(2020, 6, 1), False),    # di antara dua periode
            ("DDDD", date(2021, 2, 1), True),     # masuk lagi
            ("DDDD", date(2022, 7, 29), True),
            ("DDDD", date(2022, 8, 1), False),
            ("ZZZZ", date(2020, 1, 2), False),    # tidak pernah konstituen
        ],
    )
    def test_konstituen_pada_inklusif(self, kode, d, harapan):
        assert masukan(riwayat=list(self.RIWAYAT)).konstituen_pada(kode, d) is harapan

    def test_tanpa_riwayat_memakai_universe(self):
        m = masukan(universe=[SahamUniverse("AAAA", "", "Keuangan")])
        assert m.konstituen_pada("AAAA", date(2015, 1, 2)) is True
        assert m.konstituen_pada("BBBB", date(2015, 1, 2)) is False

    def test_riwayat_kosong_bukan_none(self):
        m = masukan(universe=[SahamUniverse("AAAA", "", "Keuangan")], riwayat=[])
        assert m.konstituen_pada("AAAA", date(2026, 1, 2)) is False

    def test_indeks_dibangun_ulang_saat_daftar_diganti(self):
        m = masukan(riwayat=[RiwayatKonstituen("AAAA", date(2020, 1, 1), None)])
        assert m.konstituen_pada("AAAA", date(2021, 1, 4))
        m.riwayat = [RiwayatKonstituen("BBBB", date(2020, 1, 1), None)]
        assert not m.konstituen_pada("AAAA", date(2021, 1, 4))
        assert m.konstituen_pada("BBBB", date(2021, 1, 4))


class TestSektor:
    def test_urutan_sumber_sektor(self):
        m = masukan(
            universe=[SahamUniverse("AAAA", "Satu", "Keuangan")],
            portofolio=[
                baris_portofolio("AAAA", sektor="Energi"),       # universe menang
                baris_portofolio("XXXX", sektor="Teknologi"),
                baris_portofolio("YYYY", sektor=None),
            ],
        )
        assert m.sektor("AAAA") == "Keuangan"
        assert m.sektor("XXXX") == "Teknologi"
        assert m.sektor("DDDD") == "TIDAK_DIKETAHUI:DDDD"   # mis. kode riwayat di luar universe (§6.3)
        assert m.sektor("YYYY") == sektor_tidak_diketahui("YYYY") == "TIDAK_DIKETAHUI:YYYY"

    def test_sektor_tidak_diketahui_satu_per_kode(self):
        m = masukan()
        assert m.sektor("DDDD") != m.sektor("EEEE")


# ---------------------------------------------------------------------------
# portfolio.csv (§6.5, §15.3)
# ---------------------------------------------------------------------------


def tulis_portofolio(tmp_path: Path, *baris: str, header: str = HEADER_PORTOFOLIO) -> Path:
    return tulis(tmp_path / "portfolio.csv", header + "\n" + "".join(b + "\n" for b in baris))


class TestPortofolio:
    def test_contoh_spec(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,2026-10-08,2850,2,PB,2720,,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert masalah == []
        (b,) = hasil
        assert b == BarisPortofolio(
            kode="ABCD", tanggal_beli=date(2026, 10, 8), harga_beli=Decimal("2850"), lot=2, strategi="PB",
            stop_awal=Decimal("2720"), sektor=None, harga_per_tanggal=None, lembar=None, catatan="", baris=2,
        )
        assert isinstance(b.harga_beli, Decimal) and isinstance(b.stop_awal, Decimal)
        assert b.lembar_nominal(100) == 200
        assert b.tanggal_acuan() == date(2026, 10, 8)

    def test_kolom_opsional_lengkap(self, tmp_path):
        p = tulis_portofolio(tmp_path, 'XXXX,2026-10-01,2850.5,3,MANUAL,2600.25,Energi,2026-10-09,1500,"split 1:5, sudah dicek"')
        (b,), masalah = baca_portofolio(p, set())
        assert masalah == []
        assert b.harga_beli == Decimal("2850.5") and b.stop_awal == Decimal("2600.25")
        assert b.sektor == "Energi" and b.catatan == "split 1:5, sudah dicek"
        assert b.lembar_nominal(100) == 1500            # lembar menggantikan lot × lot_lembar
        assert b.tanggal_acuan() == date(2026, 10, 9)   # harga_per_tanggal

    def test_header_minimal_tanpa_kolom_opsional(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,2026-10-08,2850,2,BO,2720",
                             header="kode,tanggal_beli,harga_beli,lot,strategi,stop_awal")
        (b,), masalah = baca_portofolio(p, {"ABCD"})
        assert masalah == [] and b.sektor is None and b.lembar is None and b.catatan == ""

    def test_sektor_wajib_di_luar_universe(self, tmp_path):
        p = tulis_portofolio(tmp_path, "XXXX,2026-10-08,2850,2,PB,2720,,,,", "YYYY,2026-10-08,2850,2,PB,2720,Energi,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"}, "input/portfolio.csv")
        assert [b.kode for b in hasil] == ["YYYY"]
        (m,) = error(masalah)
        assert m.baris == 2 and "sektor wajib" in m.pesan and "XXXX" in m.pesan
        assert str(m).startswith("ERROR input/portfolio.csv baris 2:")

    def test_sektor_tidak_wajib_di_dalam_universe(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,2026-10-08,2850,2,PB,2720,,,,")
        assert error(baca_portofolio(p, {"ABCD"})[1]) == []

    def test_kode_ganda_error(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,2026-10-08,2850,2,PB,2720,,,,", "ABCD,2026-10-09,2900,1,PB,2750,,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert len(hasil) == 1
        (m,) = error(masalah)
        assert m.baris == 3 and "ganda" in m.pesan and "harga rata-rata" in m.pesan

    @pytest.mark.parametrize("stop", ["2850", "2900"])
    def test_stop_di_atas_harga_beli_peringatan(self, tmp_path, stop):
        p = tulis_portofolio(tmp_path, f"ABCD,2026-10-08,2850,2,PB,{stop},,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert len(hasil) == 1                     # bukan ERROR: baris tetap dipakai (saran JUAL, §15.4 #3)
        assert error(masalah) == []
        (m,) = peringatan(masalah)
        assert m.baris == 2 and "stop_awal" in m.pesan

    @pytest.mark.parametrize(
        "lot, potongan",
        [("0", "lot harus > 0"), ("-1", "lot harus > 0"), ("1.5", "bilangan bulat"), ("", "wajib diisi")],
    )
    def test_lot_tidak_valid(self, tmp_path, lot, potongan):
        p = tulis_portofolio(tmp_path, f"ABCD,2026-10-08,2850,{lot},PB,2720,,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == [] and potongan in error(masalah)[0].pesan

    @pytest.mark.parametrize("strategi", ["ACAK", "pb", "XX", ""])
    def test_strategi_tidak_valid(self, tmp_path, strategi):
        p = tulis_portofolio(tmp_path, f"ABCD,2026-10-08,2850,2,{strategi},2720,,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == [] and "strategi" in error(masalah)[0].pesan

    @pytest.mark.parametrize("strategi", ["PB", "BO", "MR", "MANUAL"])
    def test_strategi_valid(self, tmp_path, strategi):
        p = tulis_portofolio(tmp_path, f"ABCD,2026-10-08,2850,2,{strategi},2720,,,,")
        (b,), masalah = baca_portofolio(p, {"ABCD"})
        assert masalah == [] and b.strategi == strategi

    @pytest.mark.parametrize("kolom_baris", ["ABCD,2026-10-08,0,2,PB,2720,,,,", "ABCD,2026-10-08,2850,2,PB,0,,,,",
                                             "ABCD,2026-10-08,-5,2,PB,2720,,,,", "ABCD,2026-10-08,2850,2,PB,,,,,"])
    def test_harga_dan_stop_harus_positif(self, tmp_path, kolom_baris):
        hasil, masalah = baca_portofolio(tulis_portofolio(tmp_path, kolom_baris), {"ABCD"})
        assert hasil == [] and error(masalah)

    def test_lembar_tidak_valid(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,2026-10-08,2850,2,PB,2720,,,0,", "EFGH,2026-10-08,2850,2,PB,2720,Energi,,12.5,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == [] and [m.baris for m in error(masalah)] == [2, 3]

    def test_koma_desimal_dikutip(self, tmp_path):
        p = tulis_portofolio(tmp_path, 'ABCD,2026-10-08,"2850,5",2,PB,2720,,,,')
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == []
        (m,) = error(masalah)
        assert m.baris == 2 and "harga_beli" in m.pesan and PESAN_REGIONAL in m.pesan

    def test_koma_desimal_tanpa_kutip(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,2026-10-08,2850,5,2,PB,2720,,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == [] and PESAN_REGIONAL in error(masalah)[0].pesan

    def test_tanggal_regional(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,08/10/2026,2850,2,PB,2720,,,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == []
        (m,) = error(masalah)
        assert m.baris == 2 and "tanggal_beli" in m.pesan and PESAN_REGIONAL in m.pesan

    def test_berpemisah_titik_koma(self, tmp_path):
        p = tulis(tmp_path / "portfolio.csv", HEADER_PORTOFOLIO.replace(",", ";") + "\nABCD;2026-10-08;2850;2;PB;2720;;;;\n")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == [] and PESAN_REGIONAL in error(masalah)[0].pesan

    def test_semua_error_satu_baris_dilaporkan(self, tmp_path):
        p = tulis_portofolio(tmp_path, "abcd,08/10/2026,2850,0,XX,2720,,,,")
        _, masalah = baca_portofolio(p, set())
        teks = " | ".join(m.pesan for m in error(masalah))
        for kolom in ("kode", "tanggal_beli", "lot", "strategi"):
            assert f"kolom {kolom}" in teks

    def test_harga_per_tanggal_sebelum_tanggal_beli(self, tmp_path):
        p = tulis_portofolio(tmp_path, "ABCD,2026-10-08,2850,2,PB,2720,,2026-10-07,,")
        hasil, masalah = baca_portofolio(p, {"ABCD"})
        assert hasil == [] and "harga_per_tanggal" in error(masalah)[0].pesan

    def test_hanya_header(self, tmp_path):
        assert baca_portofolio(tulis_portofolio(tmp_path), {"ABCD"}) == ([], [])


# ---------------------------------------------------------------------------
# akun.yaml (§6.6)
# ---------------------------------------------------------------------------


class TestAkun:
    def test_contoh_spec(self, tmp_path):
        p = tulis(tmp_path / "akun.yaml", "kas_idr: 2429145          # komentar\ndiperbarui: 2026-10-15\n")
        akun, masalah = baca_akun(p, MODAL)
        assert masalah == []
        assert akun == Akun(Decimal("2429145"), date(2026, 10, 15), True)
        assert isinstance(akun.kas_idr, Decimal)

    @pytest.mark.parametrize(
        "nilai, harapan",
        [("2429145.50", Decimal("2429145.5")), ("0", Decimal("0")), ('"2429145.5"', Decimal("2429145.5")),
         ("0.1", Decimal("0.1"))],
    )
    def test_kas_decimal(self, tmp_path, nilai, harapan):
        p = tulis(tmp_path / "akun.yaml", f"kas_idr: {nilai}\ndiperbarui: '2026-10-15'\n", bom=True)
        akun, masalah = baca_akun(p, MODAL)
        assert masalah == [] and akun.kas_idr == harapan and akun.diperbarui == date(2026, 10, 15)

    def test_tidak_ada_kas_modal_awal_dengan_peringatan(self, tmp_path):
        akun, masalah = baca_akun(tmp_path / "akun.yaml", MODAL, "input/akun.yaml")
        assert akun == Akun(Decimal("3000000"), None, False)
        (m,) = masalah
        assert m.level == "PERINGATAN" and m.berkas == "input/akun.yaml"
        assert "modal_awal_idr" in m.pesan and "Rp 3.000.000" in m.pesan and "tidak ditemukan" in m.pesan

    @pytest.mark.parametrize(
        "isi, potongan, baris",
        [
            ("kas_idr: -1\ndiperbarui: 2026-10-15\n", "negatif", 1),
            ("kas_idr: 100\ndiperbarui: 2026-10-15\nsaldo: 5\n", "kunci tidak dikenal: saldo", 3),
            ("kas_idr: 100\n", "kunci wajib tidak ada: diperbarui", None),
            ("diperbarui: 2026-10-15\n", "kunci wajib tidak ada: kas_idr", None),
            ("kas_idr: 100\nkas_idr: 200\ndiperbarui: 2026-10-15\n", "kunci ganda", 2),
            ("kas_idr: true\ndiperbarui: 2026-10-15\n", "harus angka", 1),
            ("kas_idr:\ndiperbarui: 2026-10-15\n", "wajib diisi", 1),
            ("kas_idr: 2.429.145\ndiperbarui: 2026-10-15\n", "pemisah ribuan", 1),
            ("kas_idr: 100\ndiperbarui: 2026-10-15 10:00:00\n", "tanpa jam", 2),
            ("kas_idr: 100\ndiperbarui:\n", "wajib diisi", 2),
            ("kas_idr: 100\ndiperbarui: 20261015\n", "YYYY-MM-DD", 2),
            ("kas_idr: [100\ndiperbarui: 2026-10-15\n", "YAML tidak valid", 2),
            ("- 100\n- 2026-10-15\n", "kas_idr", None),
            ("", "kas_idr", None),
        ],
    )
    def test_error(self, tmp_path, isi, potongan, baris):
        p = tulis(tmp_path / "akun.yaml", isi)
        akun, masalah = baca_akun(p, MODAL)
        galat = error(masalah)
        assert galat, masalah
        assert any(potongan in m.pesan for m in galat), galat
        assert any(m.baris == baris for m in galat if potongan in m.pesan)
        assert akun.kas_idr == 0  # arah aman bila tidak valid

    @pytest.mark.parametrize("isi", ["kas_idr: 2429145,50\ndiperbarui: 2026-10-15\n",
                                     "kas_idr: 100\ndiperbarui: 15/10/2026\n"])
    def test_format_regional(self, tmp_path, isi):
        _, masalah = baca_akun(tulis(tmp_path / "akun.yaml", isi), MODAL)
        assert PESAN_REGIONAL in error(masalah)[0].pesan


# ---------------------------------------------------------------------------
# holidays.csv (§6.7), anomali_ok.csv (§6.8)
# ---------------------------------------------------------------------------


class TestLibur:
    def test_valid(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "tanggal,keterangan\n2026-12-25,Contoh format\n2026-12-31,\n")
        libur, masalah = baca_libur(p)
        assert masalah == []
        assert libur == {date(2026, 12, 25): "Contoh format", date(2026, 12, 31): ""}

    def test_ganda_peringatan(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "tanggal,keterangan\n2026-12-25,A\n2026-12-25,B\n")
        libur, masalah = baca_libur(p)
        assert libur == {date(2026, 12, 25): "A"}
        (m,) = masalah
        assert m.level == "PERINGATAN" and m.baris == 3

    def test_akhir_pekan_peringatan(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "tanggal,keterangan\n2026-10-10,Sabtu\n")
        libur, masalah = baca_libur(p)
        assert date(2026, 10, 10) in libur
        assert [m.level for m in masalah] == ["PERINGATAN"]

    def test_tanggal_regional_error(self, tmp_path):
        p = tulis(tmp_path / "h.csv", "tanggal,keterangan\n25/12/2026,Natal\n")
        libur, masalah = baca_libur(p)
        assert libur == {} and PESAN_REGIONAL in error(masalah)[0].pesan

    def test_berkas_wajib(self, tmp_path):
        assert error(baca_libur(tmp_path / "holidays.csv")[1])


class TestAnomaliOk:
    def test_opsional(self, tmp_path):
        assert baca_anomali_ok(tmp_path / "anomali_ok.csv") == ([], [])

    def test_valid_termasuk_ihsg(self, tmp_path):
        p = tulis(tmp_path / "a.csv", "kode,tanggal,catatan\nABCD,2021-10-13,Sudah dicek\nIHSG,2020-03-24,\nABCD,2021-10-13,lagi\n")
        hasil, masalah = baca_anomali_ok(p)
        assert hasil == [AnomaliOk("ABCD", date(2021, 10, 13), "Sudah dicek"), AnomaliOk("IHSG", date(2020, 3, 24), "")]
        assert [m.level for m in masalah] == ["PERINGATAN"]
        m = masukan(anomali_ok=hasil)
        assert m.anomali_ok_set() == {("ABCD", date(2021, 10, 13)), ("IHSG", date(2020, 3, 24))}

    def test_tanggal_salah(self, tmp_path):
        p = tulis(tmp_path / "a.csv", "kode,tanggal,catatan\nABCD,13-10-2021,x\n")
        hasil, masalah = baca_anomali_ok(p)
        assert hasil == [] and PESAN_REGIONAL in error(masalah)[0].pesan


# ---------------------------------------------------------------------------
# muat_masukan & pastikan_valid
# ---------------------------------------------------------------------------


def isi_input_lengkap(akar: Path) -> None:
    """Input sah minimal sesuai path config.yaml rilis (relatif terhadap akar)."""
    tulis(akar / "input/universe.csv", "kode,nama,sektor\nAAAA,Contoh Satu Tbk,Keuangan\nBBBB,Contoh Dua Tbk,Infrastruktur\n", bom=True)
    tulis(akar / "input/breadth_universe.csv", "kode\nAAAA\nCCCC\n")
    tulis(akar / "input/exclude.csv", "kode,alasan,tanggal_mulai,tanggal_akhir\nEEEE,Suspensi,2026-10-01,\n")
    tulis(akar / "input/portfolio.csv", HEADER_PORTOFOLIO + "\nAAAA,2026-10-08,2850,2,PB,2720,,,,\nXXXX,2026-10-08,1000,1,MANUAL,900,Energi,,,\n")
    tulis(akar / "input/akun.yaml", "kas_idr: 2429145\ndiperbarui: 2026-10-15\n")
    tulis(akar / "input/holidays.csv", "tanggal,keterangan\n2026-12-25,FIKTIF – KHUSUS UJI\n2027-01-01,FIKTIF – KHUSUS UJI\n")


@pytest.fixture
def ctx_tmp(tmp_path):
    """config.yaml rilis dengan akar = tmp_path (berkas pengguna tidak disentuh)."""
    return muat_config(AKAR_PROYEK / "tests" / "fixtures" / "config_rilis.yaml", tmp_path)


class TestMuatMasukan:
    def test_lengkap_tanpa_error(self, ctx_tmp, tmp_path):
        isi_input_lengkap(tmp_path)
        m = muat_masukan(ctx_tmp)
        assert m.masalah == []
        assert not m.ada_error()
        pastikan_valid(m)  # tidak melempar
        assert m.kode_universe() == ["AAAA", "BBBB"]
        assert m.breadth == ["AAAA", "CCCC"]
        assert m.riwayat is None             # opsional
        assert m.anomali_ok == []            # opsional
        assert [p.kode for p in m.portofolio] == ["AAAA", "XXXX"]
        assert m.akun == Akun(Decimal("2429145"), date(2026, 10, 15), True)
        assert set(m.libur) == {date(2026, 12, 25), date(2027, 1, 1)}
        assert m.sektor("XXXX") == "Energi"
        assert m.dikecualikan_pada("EEEE", date(2026, 10, 1)) is not None

    def test_path_relatif_di_pesan_dan_error_kode_3(self, ctx_tmp, tmp_path):
        isi_input_lengkap(tmp_path)
        tulis(tmp_path / "input/portfolio.csv", HEADER_PORTOFOLIO + "\nZZZZ,2026-10-08,2850,2,PB,2720,,,,\n")
        m = muat_masukan(ctx_tmp)
        assert m.ada_error()
        (galat,) = m.daftar_error()
        assert galat.berkas == "input/portfolio.csv" and galat.baris == 2
        with pytest.raises(InputTidakValid) as e:
            pastikan_valid(m)
        assert e.value.KODE_KELUAR == 3
        assert e.value.masalah == [galat]
        assert "ERROR input/portfolio.csv baris 2:" in str(e.value)

    def test_peringatan_tidak_menghentikan(self, ctx_tmp, tmp_path):
        isi_input_lengkap(tmp_path)
        (tmp_path / "input/akun.yaml").unlink()
        m = muat_masukan(ctx_tmp)
        assert not m.ada_error()
        pastikan_valid(m)
        assert m.akun == Akun(Decimal("3000000"), None, False)  # akun.modal_awal_idr config rilis
        (p,) = m.daftar_peringatan()
        assert p.berkas == "input/akun.yaml" and "Rp 3.000.000" in p.pesan

    def test_berkas_opsional_ada(self, ctx_tmp, tmp_path):
        isi_input_lengkap(tmp_path)
        tulis(tmp_path / "input/universe_history.csv", "kode,tanggal_masuk,tanggal_keluar\nAAAA,2015-01-01,\nDDDD,2016-02-01,2019-07-31\n")
        tulis(tmp_path / "input/anomali_ok.csv", "kode,tanggal,catatan\nIHSG,2020-03-24,dicek\n")
        m = muat_masukan(ctx_tmp)
        assert m.masalah == []
        assert m.konstituen_pada("DDDD", date(2019, 7, 31)) and not m.konstituen_pada("BBBB", date(2020, 1, 2))
        assert m.sektor("DDDD") == "TIDAK_DIKETAHUI:DDDD"
        assert m.anomali_ok_set() == {("IHSG", date(2020, 3, 24))}

    def test_berkas_wajib_hilang(self, ctx_tmp, tmp_path):
        isi_input_lengkap(tmp_path)
        for nama in ("exclude.csv", "holidays.csv", "portfolio.csv", "breadth_universe.csv", "universe.csv"):
            (tmp_path / "input" / nama).unlink()
        m = muat_masukan(ctx_tmp)
        hilang = {g.berkas for g in m.daftar_error() if "tidak ditemukan" in g.pesan}
        assert hilang == {"input/exclude.csv", "input/holidays.csv", "input/portfolio.csv",
                          "input/breadth_universe.csv", "input/universe.csv"}

    def test_sektor_silang(self, ctx_tmp, tmp_path):
        isi_input_lengkap(tmp_path)
        tulis(tmp_path / "input/portfolio.csv", HEADER_PORTOFOLIO + "\nAAAA,2026-10-08,2850,2,PB,2720,Perbankan,,,\nXXXX,2026-10-08,1000,1,MANUAL,900,keuangan,,,\n")
        m = muat_masukan(ctx_tmp)
        assert not m.ada_error()
        teks = [str(p) for p in m.daftar_peringatan()]
        assert any("input/portfolio.csv baris 2" in t and "'Keuangan'" in t for t in teks)
        assert any("ejaan berbeda" in t and "'keuangan'" in t for t in teks)
        assert m.sektor("AAAA") == "Keuangan"

    def test_fixture_uji(self, request):
        """Input fixture tahap 1 (tests/fixtures/input_uji) harus lolos tanpa ERROR."""
        if not (FIXTURES / "config_uji.yaml").exists() or not (FIXTURES / "input_uji").exists():
            pytest.skip("fixture config_uji.yaml/input_uji belum tersedia")
        ctx = request.getfixturevalue("ctx_uji")
        m = muat_masukan(ctx)
        assert m.daftar_error() == []
        assert m.universe and m.breadth
        assert all(g.berkas.startswith("input_uji/") for g in m.masalah)


# ---------------------------------------------------------------------------
# Pemeriksaan terhadap kalender
# ---------------------------------------------------------------------------

LIBUR_FIKTIF = {date(2026, 10, 9): "FIKTIF – KHUSUS UJI"}  # Jumat


class TestCekPortofolioKalender:
    T = date(2026, 10, 15)

    def test_sah(self):
        kal = Kalender(LIBUR_FIKTIF)
        rows = [baris_portofolio(tanggal_beli=date(2026, 10, 8)), baris_portofolio(tanggal_beli=self.T, harga_per_tanggal=self.T)]
        assert cek_portofolio_kalender(rows, kal, self.T) == []

    @pytest.mark.parametrize(
        "tanggal_beli, potongan",
        [(date(2026, 10, 10), "bukan hari bursa"),   # Sabtu
         (date(2026, 10, 9), "bukan hari bursa"),    # libur holidays.csv
         (date(2026, 10, 16), "setelah tanggal data t")],
    )
    def test_tanggal_beli(self, tanggal_beli, potongan):
        hasil = cek_portofolio_kalender([baris_portofolio(tanggal_beli=tanggal_beli, baris=5)], Kalender(LIBUR_FIKTIF), self.T,
                                        "input/portfolio.csv")
        assert len(hasil) == 1
        assert (hasil[0].level, hasil[0].berkas, hasil[0].baris) == ("ERROR", "input/portfolio.csv", 5)
        assert potongan in hasil[0].pesan

    def test_harga_per_tanggal_setelah_t(self):
        hasil = cek_portofolio_kalender(
            [baris_portofolio(harga_per_tanggal=date(2026, 10, 16))], Kalender(LIBUR_FIKTIF), self.T
        )
        assert len(hasil) == 1 and "harga_per_tanggal" in hasil[0].pesan

    def test_kalender_manual_dipakai(self):
        # Kalender turunan menyatakan 2026-10-08 bukan hari bursa, tetapi 2026 tercakup manual → tetap sah.
        kal = Kalender(LIBUR_FIKTIF, turunan={date(2026, 10, 7)}, turunan_rentang=(date(2026, 1, 1), date(2026, 12, 31)))
        assert cek_portofolio_kalender([baris_portofolio(tanggal_beli=date(2026, 10, 8))], kal, self.T) == []


class TestCekUmurAkun:
    HARI_INI = date(2026, 10, 15)  # Kamis

    @pytest.mark.parametrize(
        "diperbarui, libur, harapan_peringatan",
        [
            (date(2026, 10, 15), {}, False),
            (date(2026, 10, 8), {}, False),               # 9,12,13,14,15 = 5 hari bursa = batas
            (date(2026, 10, 7), {}, True),                # 6 hari bursa > 5
            (date(2026, 10, 7), LIBUR_FIKTIF, False),     # libur 9 Okt → 5 hari bursa
            (date(2026, 10, 3), {}, True),                # Sabtu: 5,6,7,8,9,12,13,14,15 = 9
        ],
    )
    def test_batas(self, diperbarui, libur, harapan_peringatan):
        akun = Akun(Decimal("1"), diperbarui, True)
        hasil = cek_umur_akun(akun, Kalender(libur), self.HARI_INI, 5)
        if harapan_peringatan:
            assert hasil is not None and hasil.level == "PERINGATAN" and hasil.berkas == "input/akun.yaml"
            assert diperbarui.isoformat() in hasil.pesan
        else:
            assert hasil is None

    def test_angka_hari_di_pesan(self):
        hasil = cek_umur_akun(Akun(Decimal("1"), date(2026, 10, 7), True), Kalender({}), self.HARI_INI, 5)
        assert "6 hari bursa" in hasil.pesan and "batas 5" in hasil.pesan

    def test_tanpa_berkas(self):
        assert cek_umur_akun(Akun(MODAL, None, False), Kalender({}), self.HARI_INI, 5) is None

    def test_masa_depan(self):
        hasil = cek_umur_akun(Akun(MODAL, date(2026, 10, 16), True), Kalender({}), self.HARI_INI, 5)
        assert hasil is not None and "masa depan" in hasil.pesan

    def test_memakai_hari_ini_waktu_virtual(self):
        assert waktu.hari_ini() == self.HARI_INI  # conftest: 2026-10-15T20:00 WIB
        akun = Akun(Decimal("1"), date(2026, 10, 7), True)
        assert cek_umur_akun(akun, Kalender({}), waktu.hari_ini(), 5) is not None


# --- Titik ribuan satu kelompok (§6): sah sebagai desimal, tetapi diberi PERINGATAN ----------------------


def test_portofolio_harga_dengan_satu_titik_ribuan_diberi_peringatan(tmp_path):
    p = tmp_path / "portfolio.csv"
    p.write_text(
        "kode,tanggal_beli,harga_beli,lot,strategi,stop_awal,sektor,harga_per_tanggal,lembar,catatan\n"
        "ZXQA,2026-10-08,2.850,2,PB,2.720,Fiktif,,,\n"
        "ZXQB,2026-10-08,2850,2,PB,2720,Fiktif,,,\n"
        "ZXQC,2026-10-08,120.667,3,MANUAL,110,Fiktif,,,rata-rata sah dengan 3 desimal\n",
        encoding="utf-8",
    )
    posisi, masalah = baca_portofolio(p, set())
    assert [b.harga_beli for b in posisi] == [Decimal("2.850"), Decimal("2850"), Decimal("120.667")]
    assert all(m.level == PERINGATAN for m in masalah)
    pesan = [(m.baris, m.pesan) for m in masalah]
    assert (2, "harga_beli 2.850 dibaca sebagai 2,850; bila maksud Anda 2.850 rupiah (pemisah ribuan), "
               "tulis 2850 tanpa titik") in pesan
    assert any(b == 2 and t.startswith("stop_awal 2.720 dibaca") for b, t in pesan)
    assert any(b == 4 and t.startswith("harga_beli 120.667 dibaca") for b, t in pesan)  # tetap diterima
    assert not any(b == 3 for b, _ in pesan)


def test_akun_kas_dengan_satu_titik_ribuan_diberi_peringatan(tmp_path):
    p = tmp_path / "akun.yaml"
    p.write_text("kas_idr: 950.000   # salah ketik gaya Indonesia\ndiperbarui: 2026-10-15\n", encoding="utf-8")
    akun, masalah = baca_akun(p, Decimal(3_000_000))
    assert akun.kas_idr == Decimal("950.0")
    assert [(m.level, m.baris) for m in masalah] == [(PERINGATAN, 1)]
    assert "tulis 950000 tanpa titik" in masalah[0].pesan
    p.write_text("kas_idr: 950000\ndiperbarui: 2026-10-15\n", encoding="utf-8")
    assert baca_akun(p, Decimal(3_000_000))[1] == []
