"""T-MT — metrik backtest (SPEC §16.4, §16.5, §22.1 T-MT).

Semua angka fiktif dan dihitung tangan di docstring/komentar setiap uji. Posisi dan baris ekuitas dibangun
langsung (field terdokumentasi di `exit.py` / `mesin.py`), kecuali uji integrasi di akhir berkas yang
menjalankan `mesin.simulasikan` pada pasar sintetis kecil dari `bantu_pasar` (pembangunnya juga dipakai
`test_gerbang.py`).
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache

import pytest

from asisten_bei import exit as ex
from asisten_bei.backtest import jendela as jd
from asisten_bei.backtest import mesin
from asisten_bei.backtest import metrik as mt
from bantu_pasar import KAL, bar, cfg_uji, pasar, tgl

D = Decimal


# ---------------------------------------------------------------------------
# Pembangun bersama (juga dipakai test_gerbang.py)
# ---------------------------------------------------------------------------


def buat_posisi(
    kode: str,
    entry: date,
    keluar: date | None,
    r: float | Decimal | None,
    *,
    risiko: Decimal = D(1000),
    strategi: str = "PB",
    alasan: str = ex.STOP_WAKTU,
    tanda: tuple[str, ...] = (),
    meta: dict | None = None,
    fee_beli: Decimal = D(0),
    fee_jual: Decimal = D(0),
    slip_beli: Decimal = D(0),
    slip_jual: Decimal = D(0),
    sinyal: date | None = None,
    biaya_beli: Decimal = D(100_000),
    close_terakhir: Decimal | None = None,
) -> ex.Posisi:
    """Posisi fiktif. P&L bersih = r × risiko; `keluar` None = masih terbuka (r diabaikan)."""
    pos = ex.Posisi(
        kode=kode, strategi=strategi, sektor="S" + kode, tanggal_sinyal=sinyal or (entry - timedelta(days=1)),
        tanggal_entry=entry, lembar=D(100), harga_beli=D(1000), biaya_beli_idr=biaya_beli, fee_beli_idr=fee_beli,
        stop_awal=D(950), stop=D(950), target=None, risiko_awal_idr=risiko, harga_maks=D(1010), faktor_entry=D(1),
        slippage_beli_idr=slip_beli, tanda=set(tanda), meta=dict(meta or {}),
        close_valid_terakhir=close_terakhir, tanggal_valid_terakhir=entry,
    )
    if keluar is not None:
        pnl = D(str(r)) * risiko
        hasil = biaya_beli + pnl
        pos.keluar = ex.Keluar(
            tanggal=keluar, alasan=alasan, harga=D(1000), harga_nominal=D(1000), nilai_idr=hasil + fee_jual,
            fee_jual_idr=fee_jual, hasil_jual_idr=hasil, pnl_idr=pnl, r=pnl / risiko, slippage_idr=slip_jual,
        )
    return pos


def baris_ekuitas(
    nilai: list[tuple[date, float | Decimal]] | list[tuple[date, float | Decimal, float | Decimal, int]],
) -> list[mesin.BarisEkuitas]:
    """Baris ekuitas dari (tanggal, ekuitas) atau (tanggal, ekuitas, nilai_posisi, n_posisi)."""
    hasil = []
    for x in nilai:
        t, e = x[0], D(str(x[1]))
        npos = D(str(x[2])) if len(x) > 2 else D(0)
        n = x[3] if len(x) > 3 else 0
        hasil.append(mesin.BarisEkuitas(t, e - npos, npos, e, n, False, None))
    return hasil


# Pasar sintetis kecil untuk integrasi: dua saham fiktif (sektor berbeda) dengan tren + gelombang, IHSG naik
# bergelombang; ACAK berpeluang 100% sebagai pembangkit transaksi (sinyal di setiap bar eligible).
N_INTEGRASI = 460
UBAH_INTEGRASI = {
    "indikator.atr_periode": 3,
    "likuiditas.periode_hari": 3,
    "likuiditas.nilai_transaksi_min_idr": 1,
    "universe.min_bar_riwayat": 5,
    "kondisi_pasar.sma_pendek": 3,
    "kondisi_pasar.sma_panjang": 5,
    "kondisi_pasar.breadth_sma": 3,
    "kondisi_pasar.min_saham_breadth": 1,
    "skor.rs_lookback": 3,
    "strategi.acak.peluang_sinyal_pct": 100.0,
    "strategi.acak.stop_waktu_hari": 5,
    "backtest.mulai": "2024-03-01",
    "backtest.holdout_mulai": "2025-07-01",
}


def _gelombang(n: int, awal: float, tren: float, amp: float, periode: int) -> list[float]:
    return [round(awal * (1 + tren) ** i * (1 + amp * math.sin(2 * math.pi * i / periode)), 2) for i in range(n)]


@lru_cache(maxsize=None)
def pasar_integrasi(ubah: tuple = ()):
    """(cfg, DataPasar, aturan universe, tanggal_awal, keputusan) — dibangun sekali per sesi uji."""
    from asisten_bei.inputs import Akun, Masukan, SahamUniverse
    from asisten_bei.strategi.dasar import daftar_strategi
    from asisten_bei.universe import AturanUniverse

    cfg = cfg_uji({**UBAH_INTEGRASI, **dict(ubah)})
    akhir = tgl(N_INTEGRASI - 1)
    saham = {
        "AAAA": bar(_gelombang(N_INTEGRASI, 1000, 0.001, 0.05, 37)),
        "BBBB": bar(_gelombang(N_INTEGRASI, 2000, -0.0002, 0.06, 23)),
    }
    ihsg = bar(_gelombang(N_INTEGRASI, 7000, 0.0005, 0.01, 50), indeks=True)
    p = pasar(saham, ihsg, cfg, akhir)
    masukan = Masukan(
        universe=[SahamUniverse("AAAA", "Fiktif A", "S1"), SahamUniverse("BBBB", "Fiktif B", "S2")],
        breadth=["AAAA", "BBBB"], riwayat=None, exclude=[], portofolio=[], akun=Akun(D(3_000_000), None, False),
    )
    aturan = AturanUniverse.backtest(masukan, cfg)
    t0, hari = mesin.hari_simulasi(p, cfg.backtest.mulai, akhir)
    strategi = daftar_strategi(cfg, termasuk_acak=True, hanya=["ACAK"])
    kep = mesin.siapkan_keputusan(p, hari, aturan, ["AAAA", "BBBB"], strategi)
    return cfg, p, aturan, t0, tuple(kep)


def simulasi_integrasi(mode: str = "edge", ubah: tuple = ()) -> mesin.HasilSimulasi:
    from asisten_bei.config import resolusi_konteks

    cfg, p, aturan, t0, kep = pasar_integrasi(ubah)
    return mesin.simulasikan(
        kep, p, mesin.SpesifikasiRun.per_strategi("ACAK"), mode=mode, konteks=resolusi_konteks(cfg, mode),
        sektor_dari=aturan.sektor, tanggal_awal=t0, tanggal_penilaian=jd.tanggal_penilaian(p.kalender, cfg),
    )


# ---------------------------------------------------------------------------
# Kurva ekuitas: CAGR, drawdown, Sharpe, exposure
# ---------------------------------------------------------------------------


def test_cagr_kurva_sintetis_tepat_10_persen():
    """2020-01-01 → 2024-01-01 = 1.461 hari kalender (satu 29 Februari) → pangkat 365,25/1.461 = 0,25.
    E: 1.000.000 → 1.464.100 = 1,1⁴ → CAGR = 10%. hari_kalender dihitung dari tanggal titik awal."""
    rows = baris_ekuitas([(date(2020, 1, 1), 1_000_000), (date(2021, 6, 1), 900_000), (date(2024, 1, 1), 1_464_100)])
    m = mt.metrik_ekuitas(rows)
    assert m.seri.hari_kalender == 1461
    assert m.cagr == pytest.approx(0.10, abs=1e-12)
    assert m.seri.imbal_hasil == pytest.approx(0.4641, abs=1e-12)
    assert mt.cagr(D(100), D(100), 0) is None
    assert mt.cagr(D(100), D(121), 730) == pytest.approx(1.21 ** (365.25 / 730) - 1, rel=1e-12)


def test_max_drawdown_puncak_lembah_pulih_dan_durasi():
    """E: 100, 120, 90, 110, 130, 104.
    Puncak 120 (hari ke-1) → lembah 90 (hari ke-2): DD = 1 − 90/120 = 25%; pulih pada 130 (hari ke-4).
    Drawdown kedua 130 → 104 = 20% (lebih kecil). Durasi di bawah puncak: {90, 110} = 2 hari bursa, {104} = 1."""
    t = [tgl(i) for i in range(6)]
    rows = baris_ekuitas(list(zip(t, [100, 120, 90, 110, 130, 104])))
    dd = mt.metrik_ekuitas(rows).drawdown
    assert dd.maks == D("0.25")
    assert (dd.tanggal_puncak, dd.tanggal_lembah, dd.tanggal_pulih) == (t[1], t[2], t[4])
    assert dd.durasi_terpanjang == 2
    assert dd.maks_pct == pytest.approx(25.0)


def test_drawdown_belum_pulih_dan_durasi_sampai_akhir():
    """E: 100, 80, 120, 110, 105, 100, 118: DD terbesar = 1 − 80/100 = 20% (pulih di 120);
    durasi terpanjang = 4 hari (110, 105, 100, 118 di bawah puncak 120) dan seri belum pulih."""
    t = [tgl(i) for i in range(7)]
    rows = baris_ekuitas(list(zip(t, [100, 80, 120, 110, 105, 100, 118])))
    dd = mt.drawdown([(r.tanggal, r.ekuitas) for r in rows])
    assert dd.maks == D("0.2") and dd.tanggal_puncak == t[0] and dd.tanggal_lembah == t[1] and dd.tanggal_pulih == t[2]
    assert dd.durasi_terpanjang == 4
    naik = mt.drawdown([(t[0], D(1)), (t[1], D(2))])
    assert naik == mt.Drawdown(D(0), None, None, None, 0)


def test_drawdown_lembah_terdalam_belum_pulih():
    t = [tgl(i) for i in range(4)]
    dd = mt.drawdown(list(zip(t, [D(100), D(110), D(55), D(60)])))
    assert dd.maks == D("0.5") and dd.tanggal_puncak == t[1] and dd.tanggal_lembah == t[2] and dd.tanggal_pulih is None
    assert dd.durasi_terpanjang == 2


def test_calmar_cagr_dibagi_maxdd():
    rows = baris_ekuitas([(date(2020, 1, 1), 100), (date(2022, 1, 1), 80), (date(2024, 1, 1), 146.41)])
    m = mt.metrik_ekuitas(rows)
    assert m.drawdown.maks == D("0.2")
    assert m.calmar == pytest.approx(m.cagr / 0.2)
    datar = mt.metrik_ekuitas(baris_ekuitas([(date(2020, 1, 1), 100), (date(2021, 1, 1), 110)]))
    assert datar.calmar is None  # MaxDD 0 → Calmar tidak terdefinisi


def test_sharpe_hitung_tangan_dan_hari_bursa_per_tahun():
    """r_harian = E_d/E_{d−1} − 1, std sampel, × √hari_bursa_per_tahun."""
    e = [D(100), D(101), D("99.99"), D("101.9898"), D("101.9898")]
    t = [tgl(i) for i in range(5)]
    rows = baris_ekuitas(list(zip(t, e)))
    r = [float(b / a) - 1 for a, b in zip(e, e[1:])]
    harap = statistics.mean(r) / statistics.stdev(r) * math.sqrt(252)
    m = mt.metrik_ekuitas(rows)
    assert m.hari_bursa_per_tahun == 252  # tanpa tahun kalender penuh
    assert m.sharpe == pytest.approx(harap, rel=1e-12)
    assert mt.metrik_ekuitas(rows, hari_per_tahun=250).sharpe == pytest.approx(
        statistics.mean(r) / statistics.stdev(r) * math.sqrt(250), rel=1e-12
    )
    assert mt.metrik_ekuitas(rows[:2]).sharpe is None  # < 2 return harian


def test_hari_bursa_per_tahun_hanya_tahun_kalender_penuh():
    """Titik awal 2014-12-31; hari Senin–Jumat 2015-01-01 … 2017-03-31. Tahun penuh: 2015 (261 hari kerja)
    dan 2016 (261). 2017 parsial tidak dihitung → (261 + 261)/2 = 261."""
    hari = [d for d in (date(2015, 1, 1) + timedelta(days=i) for i in range(820)) if d.weekday() < 5 and d <= date(2017, 3, 31)]
    assert mt.hari_bursa_per_tahun(date(2014, 12, 31), hari) == 261
    # titik awal di dalam 2015 → 2015 bukan tahun penuh
    assert mt.hari_bursa_per_tahun(date(2015, 1, 2), [d for d in hari if d > date(2015, 1, 2)]) == 261  # hanya 2016
    assert mt.hari_bursa_per_tahun(date(2015, 6, 1), [d for d in hari if date(2015, 6, 1) < d < date(2016, 6, 1)]) == 252


def test_hari_bursa_per_tahun_akhir_tahun_menurut_kalender():
    """Kalender fiktif: Jumat 2016-12-30 libur (31 Desember jatuh hari Sabtu) → hari bursa terakhir 2016 =
    Kamis 2016-12-29. Data berakhir 2016-12-29. Tanpa kalender, 2016 tampak parsial (akhir < 31 Desember)
    → hanya 2015 (261). Dengan kalender, 2016 penuh dengan 261 − 1 = 260 hari → (261 + 260)/2 = 260,5."""
    from asisten_bei.kalender import Kalender

    kal = Kalender({date(2016, 12, 30): "Libur fiktif uji"})
    hari = kal.daftar_hari_bursa(date(2015, 1, 1), date(2016, 12, 29))
    assert hari[-1] == date(2016, 12, 29)
    assert mt.hari_bursa_per_tahun(date(2014, 12, 31), hari) == 261
    assert mt.hari_bursa_per_tahun(date(2014, 12, 31), hari, kal=kal) == 260.5
    rows = baris_ekuitas([(date(2014, 12, 31), 100), *[(d, 100 + i % 3) for i, d in enumerate(hari)]])
    assert mt.metrik_ekuitas(rows, kal=kal).hari_bursa_per_tahun == 260.5


def test_exposure_dan_modal_terpakai_tanpa_baris_titik_awal():
    """Baris titik awal (n_posisi 0) tidak dihitung. Hari simulasi: posisi 1, 0, 2, 0 → exposure 50%.
    Modal terpakai = rata-rata (nilai posisi ÷ ekuitas) = (0,5 + 0 + 0,25 + 0)/4 = 18,75%."""
    t = [tgl(i) for i in range(5)]
    rows = baris_ekuitas([(t[0], 1000, 900, 3), (t[1], 1000, 500, 1), (t[2], 1000, 0, 0), (t[3], 1000, 250, 2), (t[4], 1000, 0, 0)])
    m = mt.metrik_ekuitas(rows)
    assert m.hari_simulasi == 4
    assert m.exposure_pct == pytest.approx(50.0)
    assert m.modal_terpakai_pct == pytest.approx(18.75)
    kosong = mt.metrik_ekuitas(rows[:1])
    assert kosong.exposure_pct is None and kosong.modal_terpakai_pct is None and kosong.cagr is None


# ---------------------------------------------------------------------------
# Transaksi: win rate, PF, PF_R, max drawdown R, dll.
# ---------------------------------------------------------------------------


def _daftar_dasar() -> list[ex.Posisi]:
    """Enam transaksi tertutup, sengaja diberikan tidak berurutan. Urutan tanggal EXIT:
        A +2R (exit hari 3), B −1R (5), C +1R (7), D −1R (9), E −1R (11), F +3R (13).
    Kurva R kumulatif: 2, 1, 2, 1, 0, 3 → puncak 2, terendah sesudahnya 0 → max DD R = 2.
    D masuk PALING AWAL (hari 0) tetapi keluar hari 9: urutan exit, bukan entry, yang menentukan.
    Risiko 1.000 → P&L Rp = 1.000 × R."""
    return [
        buat_posisi("FFFF", tgl(12), tgl(13), 3),
        buat_posisi("DDDD", tgl(0), tgl(9), -1),
        buat_posisi("AAAA", tgl(1), tgl(3), 2),
        buat_posisi("CCCC", tgl(6), tgl(7), 1),
        buat_posisi("BBBB", tgl(4), tgl(5), -1),
        buat_posisi("EEEE", tgl(10), tgl(11), -1),
    ]


def test_win_rate_pf_pf_r_max_dd_r_hitung_tangan():
    m = mt.metrik_transaksi(_daftar_dasar(), KAL)
    assert (m.n_tertutup, m.n_menang, m.n_kalah) == (6, 3, 3)
    assert m.win_rate_pct == pytest.approx(50.0)
    assert m.pf_r.nilai == D(2)  # (2 + 1 + 3)/|−3|
    assert m.pf_idr.nilai == D(2)  # 6.000/3.000
    assert m.max_drawdown_r == D(2)
    assert m.ekspektansi_r == D("0.5") and m.ekspektansi_idr == D(500)
    assert m.rata_menang_r == D(2) and m.rata_kalah_r == D(-1)
    assert m.rata_menang_idr == D(2000) and m.rata_kalah_idr == D(-1000)
    assert m.kerugian_beruntun_terpanjang == 2  # D (exit 9) lalu E (exit 11)
    assert m.total_r == D(3) and m.total_pnl_idr == D(3000)


def test_max_dd_r_mulai_dari_nol_sebelum_transaksi_pertama():
    """Kurva −1, −2, −1.5: puncak awal 0 → DD = 2."""
    daftar = [buat_posisi("A", tgl(0), tgl(1), -1), buat_posisi("B", tgl(1), tgl(2), -1), buat_posisi("C", tgl(2), tgl(3), "0.5")]
    assert mt.max_drawdown_r(daftar) == D(2)
    assert mt.max_drawdown_r([]) == D(0)


def test_pf_tanpa_kerugian_na():
    """PF tanpa transaksi kalah → n/a (tanpa kerugian); tetap dianggap melewati ambang apa pun.
    Tanpa transaksi sama sekali → n/a tetapi TIDAK melewati ambang (tidak ada bukti)."""
    m = mt.metrik_transaksi([buat_posisi("A", tgl(0), tgl(1), 1), buat_posisi("B", tgl(1), tgl(2), 2)], KAL)
    assert m.pf_r.nilai is None and m.pf_r.na and m.pf_r.tak_hingga
    assert m.pf_r.teks() == "n/a (tanpa kerugian)" == mt.TEKS_NA_TANPA_KERUGIAN
    assert m.pf_idr.na and m.pf_idr.teks() == "n/a (tanpa kerugian)"
    assert m.pf_r.minimal(1_000_000) and m.pf_r.lebih_dari(1)
    assert m.win_rate_pct == pytest.approx(100.0) and m.max_drawdown_r == 0
    kosong = mt.metrik_transaksi([], KAL)
    assert kosong.pf_r.na and not kosong.pf_r.tak_hingga and not kosong.pf_r.minimal(0)
    assert kosong.pf_r.teks() == "n/a (tanpa transaksi)"
    assert kosong.win_rate_pct is None and kosong.ekspektansi_r is None and kosong.rata_lama_menahan_hari is None


def test_pf_nol_tanpa_menang_dan_r_nol_bukan_menang_atau_kalah():
    pf = mt.profit_factor([D(-1), D(0), D(-2)])
    assert pf.nilai == D(0) and (pf.n_menang, pf.n_kalah) == (0, 2)
    assert not pf.minimal(D("0.01")) and pf.minimal(0)
    m = mt.metrik_transaksi([buat_posisi("A", tgl(0), tgl(1), 0)], KAL)
    assert (m.n_menang, m.n_kalah, m.win_rate_pct) == (0, 0, 0.0)


def test_lama_menahan_hari_bursa_hari_entry_hari_ke_1():
    """KAL tanpa libur: Senin 2024-01-01 → Jumat 2024-01-05 = hari ke-5; entry = exit = hari ke-1;
    Jumat 2024-01-05 → Senin 2024-01-08 = hari ke-2. Rata-rata (5 + 1 + 2)/3."""
    daftar = [
        buat_posisi("A", date(2024, 1, 1), date(2024, 1, 5), 1),
        buat_posisi("B", date(2024, 1, 2), date(2024, 1, 2), -1, alasan=ex.STOP_GAP_HARI_ENTRY),
        buat_posisi("C", date(2024, 1, 5), date(2024, 1, 8), 1),
    ]
    assert mt.metrik_transaksi(daftar, KAL).rata_lama_menahan_hari == pytest.approx(8 / 3)


def test_fee_slippage_dan_persen_laba_kotor():
    """Laba kotor = Σ (P&L bersih + fee + slippage):
        T1: P&L +1.000, fee 100 + 150, slippage 20 + 30 → 1.300
        T2: P&L −500,  fee 50 + 60,   slippage 10 + 5  → −375
    Laba kotor 925; biaya 250 + 110 + 50 + 15 = 425 → 425/925 = 45,9459…%."""
    daftar = [
        buat_posisi("A", tgl(0), tgl(2), 1, fee_beli=D(100), fee_jual=D(150), slip_beli=D(20), slip_jual=D(30)),
        buat_posisi("B", tgl(1), tgl(3), "-0.5", fee_beli=D(50), fee_jual=D(60), slip_beli=D(10), slip_jual=D(5)),
    ]
    m = mt.metrik_transaksi(daftar, KAL)
    assert m.fee_idr == D(360) and m.slippage_idr == D(65) and m.biaya_total_idr == D(425)
    assert m.laba_kotor_idr == D(925)
    assert m.biaya_pct_laba_kotor == pytest.approx(425 / 925 * 100)
    rugi = mt.metrik_transaksi([buat_posisi("A", tgl(0), tgl(1), -1, fee_beli=D(10))], KAL)
    assert rugi.laba_kotor_idr == D(-990) and rugi.biaya_pct_laba_kotor is None  # n/a: laba kotor ≤ 0


def test_pembatas_lot_risiko_terealisasi_alasan_dan_tanda():
    """Pembatas: risiko, alokasi, kas, likuid → 3 dari 4 dibatasi selain risiko = 75%.
    Risiko terealisasi = rata-rata risiko_awal ÷ ekuitas tanggal sinyal: 1.000/100.000 = 1%,
    500/100.000 = 0,5%, 2.000/400.000 = 0,5%, 1.000/50.000 = 2% → 1%."""
    daftar = [
        buat_posisi("A", tgl(0), tgl(1), 1, meta={"pembatas": "risiko", "ekuitas_sinyal": D(100_000)}, tanda=(ex.ANOMALI,)),
        buat_posisi("B", tgl(0), tgl(2), -1, risiko=D(500), alasan=ex.STOP,
                    meta={"pembatas": "alokasi", "ekuitas_sinyal": D(100_000)}, tanda=(ex.SUMBER_MANUAL,)),
        buat_posisi("C", tgl(1), tgl(3), 1, risiko=D(2000), alasan=ex.TARGET,
                    meta={"pembatas": "kas", "ekuitas_sinyal": D(400_000)}),
        buat_posisi("D", tgl(2), tgl(4), -1, alasan=ex.STOP, meta={"pembatas": "likuid", "ekuitas_sinyal": D(50_000)},
                    tanda=(ex.ANOMALI, ex.TERTUNDA_ARB)),
    ]
    terbuka = [mt.transaksi_terbuka(buat_posisi("E", tgl(3), None, None), tgl(5), D(101_000), (ex.MELEWATI_HOLDOUT,))]
    m = mt.metrik_transaksi(daftar, KAL, terbuka=terbuka)
    assert m.pembatas_lot_pct == pytest.approx(75.0)
    assert m.pembatas_lot == {"risiko": 1, "alokasi": 1, "likuid": 1, "kas": 1}
    assert m.risiko_terealisasi_pct == pytest.approx(1.0)
    assert m.per_alasan_exit == {ex.STOP: 2, ex.TARGET: 1, ex.STOP_WAKTU: 1}
    assert m.per_tanda == {ex.ANOMALI: 2, ex.TERTUNDA_ARB: 1, ex.TERTUNDA_TANPA_TRANSAKSI: 0, ex.SUMBER_MANUAL: 1,
                           ex.MELEWATI_HOLDOUT: 1}
    assert list(m.per_tanda) == list(ex.URUTAN_TANDA)
    assert m.n_terbuka == 1 and m.pnl_terbuka_idr == D(1000)  # 101.000 − biaya beli 100.000, tanpa fee jual
    # Tanpa ANOMALI: B (−1R × 500) dan C (+1R × 2.000) saja.
    t = m.tanpa_anomali
    assert t.n_tertutup == 2 and t.pf_r.nilai == D(1) and t.pf_idr.nilai == D(4)
    assert t.per_tanda[ex.ANOMALI] == 0 and t.n_terbuka == 1 and t.tanpa_anomali is None


def test_transaksi_terbuka_tidak_masuk_pf_dan_bukan_tertutup_ditolak():
    pos = buat_posisi("A", tgl(0), None, None)
    with pytest.raises(ValueError, match="bukan transaksi tertutup"):
        mt.metrik_transaksi([pos], KAL)
    t = mt.transaksi_terbuka(pos, tgl(3), D(90_000))
    assert t.pnl_idr == D(-10_000) and t.r == D(-10)  # risiko 1.000
    m = mt.metrik_transaksi([buat_posisi("B", tgl(0), tgl(1), 1)], KAL, terbuka=[t])
    assert m.n_tertutup == 1 and m.pf_r.na and m.win_rate_pct == 100.0  # rugi TERBUKA tidak masuk PF/win rate


def test_sinyal_dilewati_per_status():
    """Kunci = status_isi bila order sudah dibuat (BATAL_*, MENUNGGU), selain itu status_sinyal; urutan enum §4.3."""
    S = mesin.SinyalDilewati
    daftar = [
        S(tgl(0), "A", "PB", 1.0, "REKOMENDASI", "BATAL_GAP", ""),
        S(tgl(0), "B", "PB", 1.0, "SLOT_PENUH", None, ""),
        S(tgl(1), "C", "PB", 1.0, "TIDAK_TERJANGKAU", None, ""),
        S(tgl(1), "D", "PB", 1.0, "SLOT_PENUH", None, ""),
        S(tgl(2), "E", "PB", 1.0, "REKOMENDASI", "MENUNGGU", ""),
    ]
    hasil = mt.hitung_dilewati(daftar)
    assert hasil == {"SLOT_PENUH": 2, "TIDAK_TERJANGKAU": 1, "MENUNGGU": 1, "BATAL_GAP": 1}
    assert list(hasil) == ["SLOT_PENUH", "TIDAK_TERJANGKAU", "MENUNGGU", "BATAL_GAP"]


# ---------------------------------------------------------------------------
# Periode, jendela, dan penugasan berdasarkan tanggal entry (§16.4)
# ---------------------------------------------------------------------------

MULAI = date(2023, 10, 2)  # Senin
HOLDOUT = date(2024, 3, 1)  # Jumat → akhir in-sample Kamis 2024-02-29
AKHIR = date(2024, 3, 29)
CFG_PERIODE = cfg_uji({"backtest.mulai": MULAI.isoformat(), "backtest.holdout_mulai": HOLDOUT.isoformat()})


def _hari(mulai: date, akhir: date) -> list[date]:
    return KAL.daftar_hari_bursa(mulai, akhir)


def _hasil_periode() -> tuple[mesin.HasilSimulasi, dict[str, ex.Posisi]]:
    """Ekuitas harian 2023-09-29 (titik awal, Jumat) … 2024-03-29; ekuitas = 1.000.000 + 1.000 × i.
    Transaksi (risiko 1.000):
      T1  entry 2023-10-10, exit 2023-10-20, +1R   → jendela 2023, in-sample tertutup
      T2  entry 2023-12-27, exit 2024-01-05, −1R   → jendela 2023 (tanggal ENTRY), bukan 2024
      T3  entry 2024-02-26, exit 2024-03-05, +2R   → in-sample & jendela 2024: TERBUKA (MELEWATI_HOLDOUT),
                                                     dinilai nilai_pada[2024-02-29] = 103.000; penuh: tertutup
      T6  entry = exit = 2024-02-29, −1R          → in-sample tertutup (exit pada akhir in-sample)
      T4  entry 2024-03-01, exit 2024-03-08, +1R   → holdout tertutup
      T5  entry 2024-03-20, masih terbuka          → holdout & penuh TERBUKA, nilai pos.nilai() = 100 × 990
    """
    t0 = date(2023, 9, 29)
    hari = [t0, *_hari(MULAI, AKHIR)]
    rows = baris_ekuitas([(d, 1_000_000 + 1000 * i) for i, d in enumerate(hari)])
    t3 = buat_posisi("T3", date(2024, 2, 26), date(2024, 3, 5), 2)
    t3.meta["nilai_pada"] = {date(2024, 2, 29): D(103_000)}
    pos = {
        "T1": buat_posisi("T1", date(2023, 10, 10), date(2023, 10, 20), 1),
        "T2": buat_posisi("T2", date(2023, 12, 27), date(2024, 1, 5), -1),
        "T3": t3,
        "T6": buat_posisi("T6", date(2024, 2, 29), date(2024, 2, 29), -1, alasan=ex.STOP_GAP_HARI_ENTRY),
        "T4": buat_posisi("T4", date(2024, 3, 1), date(2024, 3, 8), 1),
        "T5": buat_posisi("T5", date(2024, 3, 20), None, None, close_terakhir=D(990)),
    }
    S = mesin.SinyalDilewati
    dilewati = [
        S(date(2023, 11, 1), "X", "PB", 1.0, "SLOT_PENUH", None, ""),
        S(date(2024, 2, 29), "Y", "PB", 1.0, "BATAS_SEKTOR", None, ""),
        S(date(2024, 3, 4), "Z", "PB", 1.0, "REKOMENDASI", "BATAL_GAP", ""),
    ]
    transaksi = sorted(pos.values(), key=lambda p: p.tanggal_entry)
    h = mesin.HasilSimulasi(
        mesin.SpesifikasiRun.per_strategi("PB"), "edge", D(1_000_000), t0, transaksi, rows, dilewati,
        D(1_000_000), D(1_000_000),
    )
    return h, pos


def _kode(xs) -> list[str]:
    return sorted(getattr(x, "posisi", x).kode for x in xs)


def test_penugasan_periode_berdasarkan_tanggal_entry():
    h, _ = _hasil_periode()
    pb = jd.bagi_periode(h, CFG_PERIODE)
    assert pb.akhir_insample == date(2024, 2, 29) and pb.akhir_data == AKHIR
    assert _kode(pb.in_sample.tertutup) == ["T1", "T2", "T6"]
    assert _kode(pb.in_sample.terbuka) == ["T3"]
    assert _kode(pb.holdout.tertutup) == ["T4"] and _kode(pb.holdout.terbuka) == ["T5"]
    assert _kode(pb.penuh.tertutup) == ["T1", "T2", "T3", "T4", "T6"] and _kode(pb.penuh.terbuka) == ["T5"]
    j = {tp.periode.kunci: tp for tp in pb.jendela}
    assert list(j) == ["2023", "2024"]
    assert _kode(j["2023"].tertutup) == ["T1", "T2"]  # T2 keluar 2024 tetapi masuk 2023
    assert _kode(j["2024"].tertutup) == ["T6"] and _kode(j["2024"].terbuka) == ["T3"]
    assert [s.kode for s in pb.in_sample.dilewati] == ["X", "Y"] and [s.kode for s in pb.holdout.dilewati] == ["Z"]


def test_terbuka_pada_akhir_insample_dinilai_close_hari_itu_dan_melewati_holdout():
    h, pos = _hasil_periode()
    pb = jd.bagi_periode(h, CFG_PERIODE)
    (t3,) = pb.in_sample.terbuka
    assert t3.tanggal_nilai == date(2024, 2, 29) and t3.nilai_idr == D(103_000)
    assert t3.pnl_idr == D(3000) and t3.r == D(3)  # bukan +2R hasil exit di periode holdout
    assert ex.MELEWATI_HOLDOUT in t3.tanda
    assert ex.MELEWATI_HOLDOUT not in pos["T3"].tanda  # bagi_periode tidak mengubah posisi
    m = mt.metrik_transaksi(pb.in_sample.tertutup, KAL, terbuka=pb.in_sample.terbuka)
    assert m.n_tertutup == 3 and m.n_terbuka == 1
    assert m.pf_r.nilai == D("0.5")  # +1 / |−1 − 1|; T3 tidak masuk PF
    assert m.per_tanda[ex.MELEWATI_HOLDOUT] == 1
    (t5,) = pb.holdout.terbuka
    assert t5.tanggal_nilai == AKHIR and t5.nilai_idr == D(99_000) and ex.MELEWATI_HOLDOUT not in t5.tanda
    # Penandaan eksplisit untuk berkas transaksi.
    assert jd.tandai_melewati_holdout(h, CFG_PERIODE) == [pos["T3"]]
    assert ex.MELEWATI_HOLDOUT in pos["T3"].tanda


def test_tanda_terbuka_insample_memakai_tanda_pada_akhir_insample():
    """Tanda yang didapat sesudah akhir in-sample (mis. ANOMALI di holdout) tidak ikut statistik in-sample;
    periode penuh/holdout memakai tanda akhir run."""
    h, pos = _hasil_periode()
    pos["T3"].meta["tanda_pada"] = {date(2024, 2, 29): frozenset()}
    pos["T3"].tanda.add(ex.ANOMALI)  # terjadi 2024-03-04, di holdout
    pb = jd.bagi_periode(h, CFG_PERIODE)
    (t3,) = pb.in_sample.terbuka
    assert t3.tanda == {ex.MELEWATI_HOLDOUT}
    assert mt.metrik_transaksi(pb.in_sample.tertutup, KAL, terbuka=pb.in_sample.terbuka).per_tanda[ex.ANOMALI] == 0
    assert mt.metrik_transaksi(pb.penuh.tertutup, KAL, terbuka=pb.penuh.terbuka).per_tanda[ex.ANOMALI] == 1


def test_nilai_pada_akhir_insample_wajib_dicatat_mesin():
    h, pos = _hasil_periode()
    pos["T3"].meta.pop("nilai_pada")
    with pytest.raises(ValueError, match="tanggal_penilaian"):
        jd.bagi_periode(h, CFG_PERIODE)


def test_titik_awal_setiap_periode_dan_jendela():
    """Titik awal = close hari bursa terakhir sebelum periode: in-sample/penuh/2023 → baris titik awal
    2023-09-29; holdout → 2024-02-29; jendela 2024 → Jumat 2023-12-29."""
    h, _ = _hasil_periode()
    pb = jd.bagi_periode(h, CFG_PERIODE)
    assert pb.in_sample.tanggal_awal == pb.penuh.tanggal_awal == date(2023, 9, 29)
    assert pb.holdout.tanggal_awal == date(2024, 2, 29)
    j = {tp.periode.kunci: tp for tp in pb.jendela}
    assert j["2023"].tanggal_awal == date(2023, 9, 29) and j["2024"].tanggal_awal == date(2023, 12, 29)
    assert j["2024"].ekuitas[-1].tanggal == date(2024, 2, 29) and pb.in_sample.ekuitas[-1].tanggal == date(2024, 2, 29)
    assert pb.holdout.ekuitas[-1].tanggal == AKHIR and pb.holdout.ekuitas[1].tanggal == HOLDOUT
    assert j["2023"].ekuitas[-1].tanggal == date(2023, 12, 29)
    # E_awal holdout = ekuitas strategi pada close 2024-02-29.
    m = mt.metrik_ekuitas(pb.holdout.ekuitas)
    assert m.e_awal == pb.in_sample.ekuitas[-1].ekuitas


def test_label_jendela_parsial_dan_penuh():
    j = jd.daftar_jendela(date(2015, 1, 1), date(2024, 10, 1))
    assert [p.label for p in j] == [str(y) for y in range(2015, 2024)] + ["2024 (parsial)"]
    assert j[-1].mulai == date(2024, 1, 1) and j[-1].akhir == date(2024, 9, 30)
    j2 = jd.daftar_jendela(date(2015, 3, 2), date(2016, 1, 1))
    assert [(p.label, p.parsial) for p in j2] == [("2015 (parsial)", True)]
    pb = jd.bagi_periode(_hasil_periode()[0], CFG_PERIODE)
    assert [tp.periode.label for tp in pb.semua()] == ["In-sample", "Holdout", "Penuh", "2023 (parsial)", "2024 (parsial)"]


def test_label_parsial_memakai_hari_bursa_pertama_dan_terakhir():
    # 2017-01-01 Minggu → hari bursa pertama 2017-01-02; 2016-12-31 Sabtu → hari bursa terakhir 2016-12-30.
    j = jd.daftar_jendela(date(2017, 1, 2), date(2018, 1, 1), KAL)
    assert [(p.label, p.parsial) for p in j] == [("2017", False)]
    assert jd.daftar_jendela(date(2017, 1, 2), date(2018, 1, 1))[0].parsial  # tanpa kalender: tanggal kalender
    j = jd.daftar_jendela(date(2016, 1, 1), date(2017, 6, 1), KAL, akhir_maks=date(2016, 12, 30))
    assert [(p.label, p.akhir) for p in j] == [("2016", date(2016, 12, 30))]


def test_jendela_dipotong_di_akhir_data_bila_holdout_belum_tercapai():
    cfg = cfg_uji({"backtest.mulai": MULAI.isoformat(), "backtest.holdout_mulai": "2025-01-01"})
    pb = jd.bagi_periode(_hasil_periode()[0], cfg, KAL)
    j = {tp.periode.kunci: tp.periode for tp in pb.jendela}
    assert (j["2024"].mulai, j["2024"].akhir, j["2024"].label) == (date(2024, 1, 1), AKHIR, "2024 (parsial)")


def test_sinyal_dilewati_yang_diputuskan_di_open_holdout_tidak_masuk_insample():
    # Sinyal pada akhir in-sample (2024-02-29) dibatalkan di open holdout pertama (2024-03-01): hasilnya memakai
    # harga holdout, jadi hanya masuk periode penuh (menurut tanggal sinyal), bukan in-sample atau jendelanya.
    h, _ = _hasil_periode()
    h.dilewati.append(mesin.SinyalDilewati(date(2024, 2, 29), "W", "PB", 1.0, "REKOMENDASI", "BATAL_GAP", "",
                                           date(2024, 3, 1)))
    pb = jd.bagi_periode(h, CFG_PERIODE, KAL)
    assert [s.kode for s in pb.in_sample.dilewati] == ["X", "Y"]
    assert "W" not in [s.kode for tp in pb.jendela for s in tp.dilewati]
    assert "W" in [s.kode for s in pb.penuh.dilewati]


def test_tanggal_penilaian_untuk_mesin():
    assert jd.akhir_insample(KAL, CFG_PERIODE) == date(2024, 2, 29)
    assert jd.tanggal_penilaian(KAL, CFG_PERIODE) == (date(2024, 2, 29),)


# ---------------------------------------------------------------------------
# Pembanding IHSG
# ---------------------------------------------------------------------------


def test_titik_awal_pembanding_ihsg_periode_pertama_dan_holdout():
    """Periode pertama: modal awal dan close IHSG pada titik awal (sebelum backtest.mulai).
    Close IHSG: titik awal 7.000; 2024-02-29 7.700; akhir 8.085. Modal 1.000.000:
      penuh: ekuitas IHSG akhir = 1.000.000 × 8.085/7.000 = 1.155.000 (imbal hasil 15,5%)
      holdout: mulai dari close 2024-02-29 (7.700) → 8.085/7.700 − 1 = 5%, modal = E_awal strategi holdout."""
    h, _ = _hasil_periode()
    tanggal = [b.tanggal for b in h.ekuitas]
    close = {d: D(7000) for d in tanggal}
    for d in tanggal:
        if d >= date(2024, 2, 29):
            close[d] = D(7700)
    close[AKHIR] = D(8085)
    ek = mt.ekuitas_ihsg(tanggal, close, D(1_000_000))
    assert ek[0] == D(1_000_000) and ek[-1] == D(1_155_000)
    pb = jd.bagi_periode(h, CFG_PERIODE)
    pn = mt.metrik_ihsg(pb.penuh.ekuitas, close)
    assert pn.close_awal == D(7000) and pn.imbal_hasil == pytest.approx(0.155)
    assert pn.seri.e_awal == D(1_000_000)
    ho = mt.metrik_ihsg(pb.holdout.ekuitas, close, cagr_strategi=0.5)
    assert ho.close_awal == D(7700) and ho.imbal_hasil == pytest.approx(0.05)
    assert ho.seri.e_awal == pb.holdout.ekuitas[0].ekuitas
    assert ho.seri.hari_kalender == (AKHIR - date(2024, 2, 29)).days
    assert ho.selisih_cagr == pytest.approx(0.5 - ho.cagr)
    # Return per jendela dari titik awal jendela: 2024 = close 2023-12-29 (7.000) → 2024-02-29 (7.700) = +10%.
    ms = {m.periode.kunci: m for m in jd.metrik_semua_periode(pb, KAL, CFG_PERIODE, close_ihsg=close)}
    assert ms["2024"].ihsg.close_awal == D(7000) and ms["2024"].ihsg.imbal_hasil == pytest.approx(0.10)
    assert ms["2023"].ihsg.imbal_hasil == pytest.approx(0.0)
    assert ms["2024"].ihsg.selisih_cagr == pytest.approx(ms["2024"].ekuitas.cagr - ms["2024"].ihsg.cagr)
    # Tanpa close IHSG pada titik awal → tidak ada pembanding (tidak dikarang).
    tanpa = {d: v for d, v in close.items() if d != date(2024, 2, 29)}
    assert mt.metrik_ihsg(pb.holdout.ekuitas, tanpa) is None
    assert mt.ekuitas_ihsg(tanggal[:2], {}, D(1)) == [None, None]


def test_close_ihsg_dari_pasar_memakai_close_valid_terakhir():
    """IHSG fiktif: close 7.000 + 10·i pada HARI[0..9], bar HARI[5] hilang → pada HARI[5] berlaku close
    valid terakhir (HARI[4] = 7.040); tanggal sebelum bar pertama tidak dimuat."""
    cfg = cfg_uji({"kondisi_pasar.min_saham_breadth": 1})
    ihsg = bar([7000 + 10 * i for i in range(10)], indeks=True, buang=[5])
    p = pasar({"AAAA": bar([1000.0] * 10)}, ihsg, cfg, tgl(9))
    hasil = mt.close_ihsg_dari_pasar(p, [tgl(0) - timedelta(days=3), tgl(4), tgl(5), tgl(6)])
    assert hasil == {tgl(4): D(7040), tgl(5): D(7040), tgl(6): D(7060)}
    # Kolom ihsg_close berkas ekuitas: hanya hari dengan bar valid sendiri; HARI[5] tidak diisi (P1).
    assert mt.close_ihsg_dari_pasar(p, [tgl(4), tgl(5), tgl(6)], hanya_hari_itu=True) == {
        tgl(4): D(7040), tgl(6): D(7060)
    }


# ---------------------------------------------------------------------------
# Integrasi dengan mesin.simulasikan
# ---------------------------------------------------------------------------


def test_integrasi_mesin_periode_dan_metrik_konsisten():
    """Pasar sintetis kecil + ACAK 100%: hasil mesin dibagi per periode; jumlah transaksi dan P&L konsisten
    dengan identitas akuntansi §16.9, jendela membagi in-sample tanpa sisa, dan yang terbuka pada akhir
    in-sample dinilai pada close hari itu."""
    cfg = pasar_integrasi()[0]
    h = simulasi_integrasi("edge")
    pb = jd.bagi_periode(h, cfg)
    assert len(h.transaksi) > 20 and pb.holdout.tertutup and pb.in_sample.terbuka
    n_lintas = len(pb.in_sample.terbuka)
    assert len(pb.in_sample.tertutup) + n_lintas + len(pb.holdout.tertutup) + len(pb.holdout.terbuka) == len(h.transaksi)
    assert sum(len(tp.tertutup) for tp in pb.jendela) == len(pb.in_sample.tertutup)
    assert all(cfg.backtest.mulai <= p.tanggal_entry < cfg.backtest.holdout_mulai for p in pb.in_sample.tertutup)
    assert all(p.tanggal_entry >= cfg.backtest.holdout_mulai for p in pb.holdout.tertutup)
    for t in pb.in_sample.terbuka:
        assert t.tanggal_nilai == pb.akhir_insample and ex.MELEWATI_HOLDOUT in t.tanda
        assert t.nilai_idr == t.posisi.meta["nilai_pada"][pb.akhir_insample]
    ms = {m.periode.kunci: m for m in jd.metrik_semua_periode(pb, KAL, cfg)}
    pn = ms["penuh"]
    # E_akhir − E_awal = Σ P&L tertutup + Σ (nilai TERBUKA − biaya beli)
    assert pn.ekuitas.e_akhir - pn.ekuitas.e_awal == pn.transaksi.total_pnl_idr + pn.transaksi.pnl_terbuka_idr
    assert pn.ekuitas.e_awal == h.modal and pb.penuh.tanggal_awal == h.tanggal_awal
    assert ms["holdout"].ekuitas.e_awal == next(b.ekuitas for b in h.ekuitas if b.tanggal == pb.akhir_insample)
    assert all(m.memenuhi_syarat_g5 == (m.transaksi.n_tertutup >= cfg.gerbang.min_transaksi_per_jendela)
               for k, m in ms.items() if k.isdigit())
    assert sum(ms["penuh"].dilewati.values()) == len(h.dilewati)


def test_integrasi_pembanding_ihsg_dari_titik_awal_dan_deterministik():
    cfg, p, *_ = pasar_integrasi()
    h1, h2 = simulasi_integrasi("edge"), simulasi_integrasi("edge")
    close = mt.close_ihsg_dari_pasar(p, [b.tanggal for b in h1.ekuitas])
    m1 = jd.metrik_semua_periode(jd.bagi_periode(h1, cfg), KAL, cfg, close_ihsg=close)
    m2 = jd.metrik_semua_periode(jd.bagi_periode(h2, cfg), KAL, cfg, close_ihsg=close)
    assert m1 == m2  # murni dan deterministik
    ms = {m.periode.kunci: m for m in m1}
    pn, ho = ms["penuh"].ihsg, ms["holdout"].ihsg
    assert pn.close_awal == close[h1.tanggal_awal] and pn.seri.e_awal == h1.modal
    akhir_is = jd.akhir_insample(KAL, cfg)
    assert ho.close_awal == close[akhir_is]
    assert ho.imbal_hasil == pytest.approx(float(close[h1.ekuitas[-1].tanggal] / close[akhir_is]) - 1)
    assert ms["penuh"].ekuitas.hari_bursa_per_tahun == 252  # tanpa tahun kalender penuh di periode
