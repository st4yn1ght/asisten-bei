"""T-UN: universe dan eligibilitas (SPEC §11, §6.3, §6.4)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from asisten_bei.inputs import Akun, BarisExclude, Masukan, RiwayatKonstituen, SahamUniverse
from asisten_bei.universe import AturanUniverse, gabung_kandidat, kode_eligible, nilai_eligibilitas
from bantu_pasar import bar, cfg_uji, pasar, tgl

T = 30
UBAH = {
    "universe.min_bar_riwayat": 10,
    "likuiditas.periode_hari": 5,
    "likuiditas.nilai_transaksi_min_idr": 10_000_000_000,  # close 1.000 × volume 10 juta = tepat ambang
    "likuiditas.maks_hari_volume_nol": 0,
}
CFG = cfg_uji(UBAH)


def _datar(n: int = T + 1, close: float = 1000.0, volume: float = 10_000_000.0, **lain):
    return bar([close] * n, volume=volume, **lain)


def _masukan(kode=("AAAA",), riwayat=None, exclude=(), portofolio=()) -> Masukan:
    return Masukan(
        universe=[SahamUniverse(k, f"Contoh {k}", "Keuangan" if i % 2 == 0 else "Energi") for i, k in enumerate(kode)],
        breadth=[],
        riwayat=riwayat,
        exclude=list(exclude),
        portofolio=list(portofolio),
        akun=Akun(Decimal(3_000_000), None, False),
    )


def _nilai(saham: dict, masukan: Masukan, cfg=CFG, pos: int = T, konteks: str = "live"):
    p = pasar(saham, bar([7000.0] * (T + 1), indeks=True), cfg, tgl(T + 5))
    aturan = AturanUniverse.live(masukan) if konteks == "live" else AturanUniverse.backtest(masukan, cfg)
    return nilai_eligibilitas(p.tampilan(tgl(pos)), aturan)


# ---------------------------------------------------------------------------
# Likuiditas (syarat 4)
# ---------------------------------------------------------------------------


def test_likuiditas_tepat_di_ambang_lolos():
    h = _nilai({"AAAA": _datar()}, _masukan())
    s = h.status["AAAA"]
    assert s.eligible and s.alasan is None and s.rata_nilai == 10_000_000_000.0
    assert h.eligible == ["AAAA"] and kode_eligible(h) == ["AAAA"]


def test_likuiditas_di_bawah_ambang_dikeluarkan_dengan_rincian():
    h = _nilai({"AAAA": _datar(volume=9_999_999.0)}, _masukan())
    s = h.status["AAAA"]
    assert (s.eligible, s.alasan) == (False, "TIDAK_LIKUID_RATA2")
    assert s.rincian == "rata-rata nilai transaksi 5 hari Rp 9.999.999.000 < Rp 10.000.000.000"


# ---------------------------------------------------------------------------
# Konstituen (syarat 1) dan riwayat
# ---------------------------------------------------------------------------


def _riwayat(*baris):
    return [RiwayatKonstituen(k, m, kl) for k, m, kl in baris]


@pytest.mark.parametrize(
    "masuk, keluar, eligible",
    [
        (0, 0, True),  # masuk = t
        (-10, 0, True),  # keluar = t (inklusif)
        (-10, -1, False),  # keluar sehari sebelum t
        (1, None, False),  # masuk sesudah t
        (-10, None, True),  # keluar kosong = masih konstituen
    ],
)
def test_riwayat_batas_inklusif(masuk, keluar, eligible):
    d = tgl(T)
    r = _riwayat(("AAAA", d + timedelta(days=masuk), None if keluar is None else d + timedelta(days=keluar)))
    h = _nilai({"AAAA": _datar()}, _masukan(riwayat=r), konteks="backtest")
    assert h.status["AAAA"].eligible is eligible
    if not eligible:
        assert h.status["AAAA"].alasan == "BUKAN_KONSTITUEN"


def test_riwayat_keluar_lalu_masuk_lagi():
    d = tgl(T)
    r = _riwayat(("AAAA", d - timedelta(days=40), d - timedelta(days=20)), ("AAAA", d - timedelta(days=5), None))
    assert _nilai({"AAAA": _datar()}, _masukan(riwayat=r), konteks="backtest").status["AAAA"].eligible
    r2 = _riwayat(("AAAA", d - timedelta(days=40), d - timedelta(days=20)), ("AAAA", d + timedelta(days=1), None))
    assert not _nilai({"AAAA": _datar()}, _masukan(riwayat=r2), konteks="backtest").status["AAAA"].eligible


def test_riwayat_menggantikan_universe_di_backtest_dan_sektor_tidak_diketahui():
    d = tgl(T)
    r = _riwayat(("AAAA", d - timedelta(days=60), None), ("ZZZZ", d - timedelta(days=60), None))
    masukan = _masukan(kode=("AAAA", "BBBB"), riwayat=r)
    aturan = AturanUniverse.backtest(masukan, CFG)
    assert aturan.kandidat == ("AAAA", "ZZZZ")  # BBBB tidak dinilai: bukan anggota riwayat
    assert aturan.sektor("ZZZZ") == "TIDAK_DIKETAHUI:ZZZZ"
    assert aturan.sektor("AAAA") == "Keuangan"
    assert aturan.catatan == ("universe_history.csv dipakai",)
    h = _nilai({"AAAA": _datar(), "ZZZZ": _datar()}, masukan, konteks="backtest")
    assert sorted(h.eligible) == ["AAAA", "ZZZZ"]


def test_live_mengabaikan_riwayat():
    d = tgl(T)
    r = _riwayat(("AAAA", d + timedelta(days=30), None))  # menurut riwayat belum konstituen
    h = _nilai({"AAAA": _datar()}, _masukan(riwayat=r), konteks="live")
    assert h.status["AAAA"].eligible


def test_backtest_tanpa_riwayat_memakai_universe_dengan_catatan():
    aturan = AturanUniverse.backtest(_masukan(kode=("AAAA", "BBBB")), CFG)
    assert aturan.kandidat == ("AAAA", "BBBB")
    assert "tidak ada" in aturan.catatan[0]


# ---------------------------------------------------------------------------
# exclude.csv (syarat 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mulai, akhir, aktif",
    [
        (None, None, True),
        (0, None, True),  # mulai = t
        (None, 0, True),  # akhir = t
        (1, None, False),
        (None, -1, False),
    ],
)
def test_exclude_batas_inklusif_live(mulai, akhir, aktif):
    d = tgl(T)
    ex = BarisExclude(
        "AAAA", "Suspensi",
        None if mulai is None else d + timedelta(days=mulai),
        None if akhir is None else d + timedelta(days=akhir),
    )
    s = _nilai({"AAAA": _datar()}, _masukan(exclude=[ex])).status["AAAA"]
    assert (s.alasan == "DIKECUALIKAN") is aktif
    if aktif:
        assert s.rincian == "exclude.csv: Suspensi"


def test_backtest_mengabaikan_exclude_kecuali_pakai_exclude():
    ex = BarisExclude("AAAA", "Notasi khusus", None, None)
    masukan = _masukan(exclude=[ex])
    assert _nilai({"AAAA": _datar()}, masukan, konteks="backtest").status["AAAA"].eligible
    cfg = cfg_uji({**UBAH, "backtest.pakai_exclude": True})
    s = _nilai({"AAAA": _datar()}, masukan, cfg=cfg, konteks="backtest").status["AAAA"]
    assert s.alasan == "DIKECUALIKAN"


# ---------------------------------------------------------------------------
# Data (syarat 3) dan volume nol (syarat 5)
# ---------------------------------------------------------------------------


def test_alasan_data():
    saham = {
        "BASI": _datar(n=T),  # tidak ada bar t
        "PENDEK": _datar(n=8, mulai=T - 7),  # 8 bar valid < 10
        "BOLONG": _datar(buang=[20, 21, 22]),  # 3 bar hilang > 2
        "ANOM": bar([1000.0] * 25 + [1600.0] * (T + 1 - 25), volume=10_000_000.0),  # 6 bar ≤ t < 10
    }
    kode = (*saham, "TANPA_DATA")
    h = _nilai(saham, _masukan(kode=kode))
    alasan = {k: h.status[k].alasan for k in kode}
    assert alasan == {
        "BASI": "DATA_BASI",
        "PENDEK": "RIWAYAT_PENDEK",
        "BOLONG": "BAR_HILANG",
        "ANOM": "ANOMALI",
        "TANPA_DATA": "GAGAL_AMBIL",
    }
    assert h.status["BASI"].rincian == f"tidak ada bar valid pada {tgl(T).isoformat()}"
    assert h.status["PENDEK"].rincian == "8 bar valid < 10"
    assert h.status["BOLONG"].rincian == "3 bar hilang dalam 60 hari bursa terakhir > 2"
    assert h.status["ANOM"].rincian.startswith(f"anomali data sejak {tgl(25).isoformat()}")


def test_volume_nol_dinilai_sesudah_likuiditas():
    vol = [10_000_000.0] * (T + 1)
    vol[T - 2] = 0.0  # satu hari tanpa transaksi dalam 5 hari terakhir
    s = _nilai({"AAAA": bar([1000.0] * (T + 1), volume=vol)}, _masukan()).status["AAAA"]
    assert s.alasan == "KEMUNGKINAN_SUSPENSI"
    assert "kemungkinan suspensi — cek idx.co.id" in s.rincian
    # Tidak likuid dan ada volume nol → alasan likuiditas (syarat 4 mendahului syarat 5).
    vol2 = [9_000_000.0] * (T + 1)
    vol2[T - 2] = 0.0
    s2 = _nilai({"AAAA": bar([1000.0] * (T + 1), volume=vol2)}, _masukan()).status["AAAA"]
    assert s2.alasan == "TIDAK_LIKUID_RATA2"


def test_volume_nol_di_luar_jendela_tidak_mengeluarkan():
    vol = [10_000_000.0] * (T + 1)
    vol[T - 10] = 0.0  # di luar 5 hari bursa terakhir
    assert _nilai({"AAAA": bar([1000.0] * (T + 1), volume=vol)}, _masukan()).status["AAAA"].eligible


def test_urutan_syarat_konstituen_lalu_exclude_lalu_data():
    ex = BarisExclude("BASI", "Suspensi", None, None)
    h = _nilai({"BASI": _datar(n=T)}, _masukan(kode=("BASI",), exclude=[ex]))
    assert h.status["BASI"].alasan == "DIKECUALIKAN"  # exclude mendahului alasan data


def test_portofolio_tidak_memengaruhi_eligibilitas():
    from asisten_bei.inputs import BarisPortofolio

    p = BarisPortofolio("AAAA", tgl(T - 3), Decimal(1000), 1, "PB", Decimal(950), None, None, None, "", 2)
    assert _nilai({"AAAA": _datar()}, _masukan(portofolio=[p])).status["AAAA"].eligible


def test_gabung_kandidat_unik_stabil():
    assert gabung_kandidat(["A", "B"], ["B", "C"], ["A", "D"]) == ["A", "B", "C", "D"]
