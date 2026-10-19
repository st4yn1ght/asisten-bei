"""T-EX: mesin exit (SPEC §15.1, §15.2, §16.2 langkah 1–3, §12 baris exit, §4.1 pembulatan) dan
T-BEI bagian ARB terkunci (§15.1 langkah 1), ditambah T-SP bagian mesin (§8.4: posisi melewati split 1:5).

Semua kode saham, harga, dan sektor di berkas ini FIKTIF. Tabel fraksi dan ARA/ARB di sini berlabel
"FIKTIF – KHUSUS UJI": angkanya sengaja buatan dan BUKAN aturan BEI (P4). Nilai rilis tetap
`terverifikasi: false` dan kosong.

Cara uji:

- `jalankan` memutar satu order lewat `exit.py` dengan urutan yang sama seperti mesin backtest (§16.2):
  hari entry = `periksa_isi` → `isi_order` → `keluar_hari_entry` → `intraday(hari_entry=True)` →
  `perbarui_nilai` → `tutup`; hari berikutnya = `buka` → `intraday` → `perbarui_nilai` → `tutup`.
- Skenario yang butuh slot/sektor/kas memakai `backtest.mesin.simulasikan` dengan `KeputusanHari`
  buatan (sinyal fiktif, status pasar ditetapkan), sehingga seleksi & sizing tetap yang asli.
- Angka harapan dihitung tangan dengan `Decimal` dari default rilis: fee beli 0,15 %, fee jual 0,25 %,
  slippage 0,10 % per sisi, fee minimum 0, tanpa pembulatan fee.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from asisten_bei import exit as ex
from asisten_bei.aturan_bei import AturanBEI
from asisten_bei.backtest.mesin import (
    BATAL_KAS_SAAT_OPEN,
    BATAL_SEKTOR_SAAT_OPEN,
    BATAL_SLOT_SAAT_OPEN,
    HasilSimulasi,
    KeputusanHari,
    SpesifikasiRun,
    simulasikan,
)
from asisten_bei.config import Config, resolusi_konteks
from asisten_bei.kondisi_pasar import BOLEH_BELI, JANGAN_BELI, HasilKondisiPasar, RingkasanBreadth
from asisten_bei.seleksi import REKOMENDASI
from asisten_bei.skor import KomponenSkor, SinyalBerskor
from asisten_bei.strategi.dasar import Sinyal
from asisten_bei.universe import HasilEligibilitas
from bantu_pasar import HARI, cfg_uji, pasar, tgl

D = Decimal
LABEL_FIKTIF = "FIKTIF – KHUSUS UJI"
SUMBER_FIKTIF = f"{LABEL_FIKTIF} (bukan aturan BEI, jangan dipakai)"

# Default rilis (§7 `biaya`) dalam pecahan — dipakai untuk hitung tangan.
FB, FJ, SLIP = D("0.0015"), D("0.0025"), D("0.001")

KODE = "ZXQA"  # fiktif
KODE_B = "ZXQB"  # fiktif
SEKTOR_1 = "SEKTOR_FIKTIF_1"
SEKTOR_2 = "SEKTOR_FIKTIF_2"
VOL = 1_000_000.0

I0 = 300  # HARI[300] = Senin 2025-02-24 (kalender uji Senin–Jumat tanpa libur)
CFG = cfg_uji()

# --- Tabel fraksi FIKTIF – KHUSUS UJI ----------------------------------------------------------------
# [0, 500) fraksi 1; [500, 2000) fraksi 5; [2000, ∞) fraksi 25. Bukan aturan BEI.
TABEL_FRAKSI_FIKTIF = [
    {"harga_min": 0, "harga_maks": 500, "fraksi": 1},
    {"harga_min": 500, "harga_maks": 2000, "fraksi": 5},
    {"harga_min": 2000, "harga_maks": None, "fraksi": 25},
]
# --- ARA/ARB FIKTIF – KHUSUS UJI: ARA 11 %, ARB 13 % dari close kemarin, satu baris untuk semua harga ---
ARA_FIKTIF = [{"harga_min": 0, "harga_maks": None, "batas_pct": 11}]
ARB_FIKTIF = [{"harga_min": 0, "harga_maks": None, "batas_pct": 13}]
BERLAKU_FIKTIF = date(2025, 1, 6)  # berlaku_mulai FIKTIF (sebelum semua tanggal skenario)
BERLAKU_NANTI = date(2026, 6, 1)  # berlaku_mulai FIKTIF sesudah semua tanggal skenario


def ubah_fraksi(*, terverifikasi: bool = True, berlaku: date = BERLAKU_FIKTIF) -> dict:
    return {
        "pasar.fraksi_harga.terverifikasi": terverifikasi,
        "pasar.fraksi_harga.sumber": SUMBER_FIKTIF,
        "pasar.fraksi_harga.tanggal_cek": date(2025, 1, 6),
        "pasar.fraksi_harga.berlaku_mulai": berlaku,
        "pasar.fraksi_harga.tabel": copy.deepcopy(TABEL_FRAKSI_FIKTIF),
    }


def ubah_ar(*, terverifikasi: bool = True, berlaku: date = BERLAKU_FIKTIF) -> dict:
    return {
        "pasar.auto_reject.terverifikasi": terverifikasi,
        "pasar.auto_reject.sumber": SUMBER_FIKTIF,
        "pasar.auto_reject.tanggal_cek": date(2025, 1, 6),
        "pasar.auto_reject.berlaku_mulai": berlaku,
        "pasar.auto_reject.papan_berlaku": LABEL_FIKTIF,
        "pasar.auto_reject.harga_acuan": "close_kemarin",
        "pasar.auto_reject.ara": copy.deepcopy(ARA_FIKTIF),
        "pasar.auto_reject.arb": copy.deepcopy(ARB_FIKTIF),
    }


def cfg_dengan(*ubah: dict) -> Config:
    gabung: dict = {}
    for u in ubah:
        gabung.update(u)
    return cfg_uji(gabung)


CFG_AR = cfg_dengan(ubah_ar())  # ARA/ARB fiktif terverifikasi
CFG_AR_TIDAK = cfg_dengan(ubah_ar(terverifikasi=False))  # tabel terisi, tetapi belum terverifikasi
CFG_AR_NANTI = cfg_dengan(ubah_ar(berlaku=BERLAKU_NANTI))  # terverifikasi, belum berlaku


# =====================================================================================================
# Pembantu data dan pemutar order
# =====================================================================================================

Bar = tuple  # (open, high, low, close) atau (open, high, low, close, volume); None = tidak ada baris


def seri(baris: Sequence[Bar | None], mulai: int = I0) -> pd.DataFrame:
    """Bar eksplisit pada `HARI[mulai + i]`. None = tidak ada baris (bar hilang)."""
    rows, idx = [], []
    for i, b in enumerate(baris):
        if b is None:
            continue
        o, h, l, c, *v = b
        rows.append((float(o), float(h), float(l), float(c), float(v[0]) if v else VOL))
        idx.append(HARI[mulai + i])
    df = pd.DataFrame(
        rows, columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(idx, name="tanggal").as_unit("ns"),
    )
    df["sumber"] = "yfinance"
    return df


def buat_pasar(
    data: dict[str, Sequence[Bar | None]],
    cfg=CFG,
    *,
    mulai: int = I0,
    split: dict[str, pd.Series] | None = None,
):
    akhir = tgl(mulai + max(len(b) for b in data.values()) - 1)
    return pasar({k: seri(b, mulai) for k, b in data.items()}, None, cfg, akhir, split=split)


def order(
    *,
    i_sinyal: int,
    strategi: str = "PB",
    harga_maks: str = "1050",
    stop: str = "950",
    lembar: int = 100,
    target_r: float | None = 2.0,
    faktor: str = "1",
    kode: str = KODE,
    sektor: str = SEKTOR_1,
) -> ex.Order:
    return ex.Order(
        kode=kode, strategi=strategi, sektor=sektor, tanggal_sinyal=tgl(i_sinyal), lembar=lembar,
        harga_maks=D(harga_maks), stop=D(stop), target_r=target_r, faktor_sinyal=D(faktor),
    )


@dataclass
class Jejak:
    status_isi: str | None = None
    pos: ex.Posisi | None = None
    stop: dict[date, Decimal] = field(default_factory=dict)  # S untuk hari berikutnya, setelah close d
    jadwal: dict[date, str | None] = field(default_factory=dict)  # jual terjadwal setelah close d
    nilai: dict[date, Decimal] = field(default_factory=dict)  # nilai posisi pada close d

    @property
    def keluar(self) -> ex.Keluar | None:
        return None if self.pos is None else self.pos.keluar


def jalankan(
    pp,
    od: ex.Order,
    i_entry: int,
    i_akhir: int,
    status: str | Callable[[date], str] = BOLEH_BELI,
) -> Jejak:
    """Satu order lewat §15.1 dengan urutan §16.2 (sama seperti `simulasikan` untuk satu posisi)."""
    cfg, bei, kal = pp.cfg, AturanBEI(pp.cfg), pp.kalender
    j = Jejak()
    pos = None
    for i in range(i_entry, i_akhir + 1):
        d = tgl(i)
        dv = pp.tampilan(d)
        b = ex.bar_hari(dv, od.kode)
        if pos is None:
            st = ex.periksa_isi(od, b, bei)
            j.status_isi = st or ex.TERISI
            if st is not None:
                return j
            pos = j.pos = ex.isi_order(od, b, cfg, bei)
            if ex.keluar_hari_entry(pos, b, cfg, bei) is not None:
                return j
            hari_entry = True
        else:
            if ex.buka(pos, b, cfg, bei, kal) is not None:
                return j
            hari_entry = False
        if ex.intraday(pos, b, cfg, hari_entry) is not None:
            return j
        ex.perbarui_nilai(pos, b)
        j.nilai[d] = pos.nilai()
        ex.tutup(pos, b, dv, cfg, bei, kal, status(d) if callable(status) else status)
        j.stop[d] = pos.stop
        j.jadwal[d] = pos.jual_terjadwal
    return j


def hasil_jual(lembar, harga) -> Decimal:
    """Hitung tangan: lembar × harga − fee jual 0,25 %."""
    nilai = D(lembar) * D(harga)
    return nilai - nilai * FJ


def biaya_beli(lembar, harga) -> Decimal:
    """Hitung tangan: lembar × harga + fee beli 0,15 %."""
    nilai = D(lembar) * D(harga)
    return nilai + nilai * FB


def risiko_rencana(lembar, harga_maks, stop) -> Decimal:
    """§4.1: lembar × (harga_maks × (1 + fb) − stop × (1 − slip) × (1 − fj))."""
    return D(lembar) * (D(harga_maks) * (1 + FB) - D(stop) * (1 - SLIP) * (1 - FJ))


# Skenario dasar PB: sinyal pada I0+2, entry I0+3. Order 100 lembar, harga_maks 1.050, stop 950, 2R.
#   harga isi = min(1.000 × 1,001; 1.050) = 1.001
#   biaya beli = 100.100 + 150,15 = 100.250,15
#   risiko rencana = 100 × (1.050 × 1,0015 − 950 × 0,999 × 0,9975) = 100 × (1.051,575 − 946,677375)
#                  = 10.489,7625
#   target = 1.001 + 2 × (1.001 − 950) = 1.103 (sudah rupiah bulat)
S0 = I0 + 2
E0 = I0 + 3
NORMAL = (1000, 1010, 990, 1000)
AWAL = [NORMAL, NORMAL, NORMAL]  # pemanasan; bar S0 (close 1.000) = acuan ARA/ARB hari entry
BIAYA_DASAR = D("100250.15")
RISIKO_DASAR = D("10489.7625")
TARGET_DASAR = D("1103")


def pb_dasar(*setelah: Bar | None, cfg=CFG, entry: Bar | None = NORMAL, strategi: str = "PB"):
    """(pasar, order) skenario dasar dengan bar hari entry dan hari-hari sesudahnya."""
    pp = buat_pasar({KODE: [*AWAL, entry, *setelah]}, cfg)
    return pp, order(i_sinyal=S0, strategi=strategi)


def cek_keluar(k: ex.Keluar, *, tanggal: date, alasan: str, harga: str, lembar=100, biaya=BIAYA_DASAR,
               risiko=RISIKO_DASAR) -> None:
    assert k is not None
    assert k.tanggal == tanggal
    assert k.alasan == alasan
    assert k.harga == D(harga)
    hasil = hasil_jual(lembar, harga)
    assert k.hasil_jual_idr == hasil
    assert k.pnl_idr == hasil - biaya
    assert k.r == (hasil - biaya) / risiko


# =====================================================================================================
# Pembatalan di open (§16.2 langkah 2): BATAL_DATA, BATAL_GAP, BATAL_ARA
# =====================================================================================================


@pytest.mark.parametrize(
    "bar_entry",
    [
        pytest.param(None, id="bar-tidak-ada"),
        pytest.param((1000, 1010, 990, 1000, 0), id="volume-0"),
        pytest.param((1000, 990, 1010, 1000), id="rusak-high<low"),
        pytest.param((1000, 1010, 990, float("nan")), id="rusak-close-NaN"),
        pytest.param((1000, 1010, 990, 1000, float("nan")), id="rusak-volume-NaN"),
    ],
)
def test_batal_data(bar_entry):
    pp, od = pb_dasar(NORMAL, entry=bar_entry)
    j = jalankan(pp, od, E0, E0 + 1)
    assert j.status_isi == ex.BATAL_DATA
    assert j.pos is None


def test_batal_gap_open_di_atas_harga_maks():
    pp, od = pb_dasar(entry=(1051, 1060, 1040, 1050))
    assert jalankan(pp, od, E0, E0).status_isi == ex.BATAL_GAP


def test_open_sama_dengan_harga_maks_terisi_di_harga_maks():
    # open = harga_maks = 1.050 → harga isi = min(1.050 × 1,001 = 1.051,05; 1.050) = 1.050 (tidak di atas limit)
    pp, od = pb_dasar(entry=(1050, 1060, 1040, 1050))
    j = jalankan(pp, od, E0, E0)
    assert j.status_isi == ex.TERISI
    p = j.pos
    assert p.harga_beli == D("1050")
    assert p.biaya_beli_idr == D("105157.5")  # 105.000 + 157,5
    assert p.slippage_beli_idr == 0  # acuan = min(open, harga_maks) = 1.050
    # Risiko rencana tidak berubah oleh harga isi: 10.489,7625.
    assert p.risiko_awal_idr == RISIKO_DASAR == risiko_rencana(100, 1050, 950)
    # Target dari harga isi aktual: 1.050 + 2 × 100 = 1.250.
    assert p.target == D("1250")


def test_harga_isi_dengan_slippage_dan_nilai_posisi():
    pp, od = pb_dasar()
    p = jalankan(pp, od, E0, E0).pos
    assert p.harga_beli == D("1001")  # 1.000 × 1,001
    assert p.lembar == 100
    assert p.fee_beli_idr == D("150.15")
    assert p.biaya_beli_idr == BIAYA_DASAR
    assert p.slippage_beli_idr == D("100")  # 100 × (1.001 − 1.000)
    assert p.stop == p.stop_awal == D("950")
    assert p.target == TARGET_DASAR
    assert p.risiko_awal_idr == RISIKO_DASAR
    assert p.nilai() == D("100000")  # 100 × close 1.000


# --- BATAL_ARA (tabel FIKTIF – KHUSUS UJI, ARA 11 %) --------------------------------------------------
# close kemarin 1.000 → batas ARA = bulat_bawah(1.000 × 1,11) = 1.110. Bar terkunci: o = h = l = c ≥ 1.110.
# harga_maks order = 1.120 agar open 1.110 ≤ harga_maks (bukan BATAL_GAP).
ARA_TERKUNCI = (1110, 1110, 1110, 1110)


def _order_ara() -> ex.Order:
    return order(i_sinyal=S0, harga_maks="1120", stop="950")


def test_batal_ara_terverifikasi_fiktif():
    pp = buat_pasar({KODE: [*AWAL, ARA_TERKUNCI]}, CFG_AR)
    assert jalankan(pp, _order_ara(), E0, E0).status_isi == ex.BATAL_ARA


@pytest.mark.parametrize(
    ("cfg", "bar_entry"),
    [
        pytest.param(CFG, ARA_TERKUNCI, id="config-rilis"),
        pytest.param(CFG_AR_TIDAK, ARA_TERKUNCI, id="belum-terverifikasi"),
        pytest.param(CFG_AR_NANTI, ARA_TERKUNCI, id="sebelum-berlaku-mulai"),
        pytest.param(CFG_AR, (1110, 1110, 1105, 1110), id="tidak-datar"),
        pytest.param(CFG_AR, (1109, 1109, 1109, 1109), id="datar-di-bawah-batas"),
    ],
)
def test_ara_tidak_dimodelkan_atau_tidak_terkunci(cfg, bar_entry):
    pp = buat_pasar({KODE: [*AWAL, bar_entry]}, cfg)
    j = jalankan(pp, _order_ara(), E0, E0)
    assert j.status_isi == ex.TERISI
    assert j.pos.harga_beli == min(D(str(bar_entry[0])) * (1 + SLIP), D("1120"))


def test_urutan_pembatalan_data_lalu_gap_lalu_ara():
    # Bar rusak dengan open > harga_maks → BATAL_DATA (data dicek lebih dulu).
    pp = buat_pasar({KODE: [*AWAL, (1200, 1100, 1150, 1180)]}, CFG_AR)
    assert jalankan(pp, _order_ara(), E0, E0).status_isi == ex.BATAL_DATA
    # Terkunci ARA di 1.150 (≥ 1.110) tetapi open 1.150 > harga_maks 1.120 → BATAL_GAP lebih dulu.
    pp = buat_pasar({KODE: [*AWAL, (1150, 1150, 1150, 1150)]}, CFG_AR)
    assert jalankan(pp, _order_ara(), E0, E0).status_isi == ex.BATAL_GAP


@pytest.mark.parametrize(
    ("cfg", "bar_entry", "status"),
    [
        # Datar di atas batas (1.115 ≥ 1.110) juga terkunci ARA.
        pytest.param(CFG_AR, (1115, 1115, 1115, 1115), ex.BATAL_ARA, id="datar-di-atas-batas"),
        # berlaku_mulai FIKTIF = tanggal entry → dimodelkan (d ≥ berlaku_mulai, inklusif).
        pytest.param(cfg_dengan(ubah_ar(berlaku=tgl(E0))), ARA_TERKUNCI, ex.BATAL_ARA, id="berlaku-tepat-hari-entry"),
        # berlaku_mulai FIKTIF = sehari sesudah entry → belum dimodelkan, order terisi.
        pytest.param(cfg_dengan(ubah_ar(berlaku=tgl(E0 + 1))), ARA_TERKUNCI, ex.TERISI, id="berlaku-besok"),
    ],
)
def test_batal_ara_batas_harga_dan_tanggal_berlaku(cfg, bar_entry, status):
    pp = buat_pasar({KODE: [*AWAL, bar_entry]}, cfg)
    assert jalankan(pp, _order_ara(), E0, E0).status_isi == status


def test_harga_isi_dibatasi_harga_maks_saat_slippage_melewati_limit():
    # open 1.049 ≤ harga_maks 1.050, tetapi 1.049 × 1,001 = 1.050,049 > 1.050 → harga isi = min(...) = 1.050.
    # Slippage beli = 100 × (1.050 − min(1.049; 1.050)) = 100. Target = 1.050 + 2 × 100 = 1.250.
    pp, od = pb_dasar(entry=(1049, 1060, 1040, 1050))
    p = jalankan(pp, od, E0, E0).pos
    assert p.harga_beli == D("1050")
    assert p.biaya_beli_idr == D("105157.5")
    assert p.slippage_beli_idr == D("100")
    assert p.target == D("1250")
    assert p.risiko_awal_idr == RISIKO_DASAR  # risiko rencana tidak bergantung harga isi


# =====================================================================================================
# Hari entry: STOP_GAP_HARI_ENTRY, stop_aktif_hari_entry
# =====================================================================================================


def test_stop_gap_hari_entry_r_terhadap_risiko_rencana():
    # Order 200 lembar, harga_maks 1.050, stop 950; open 940 ≤ stop.
    #   isi  = min(940 × 1,001; 1.050) = 940,94 → biaya = 188.188 + 282,282 = 188.470,282
    #   jual = 940 × 0,999 = 939,06 → hasil = 187.812 − 469,53 = 187.342,47
    #   P&L  = −1.127,812
    #   risiko rencana = 200 × 104,897625 = 20.979,525 → R = −1.127,812 / 20.979,525 ≈ −0,0538
    pp = buat_pasar({KODE: [*AWAL, (940, 960, 930, 950)]})
    od = order(i_sinyal=S0, lembar=200)
    j = jalankan(pp, od, E0, E0)
    p, k = j.pos, j.keluar
    assert j.status_isi == ex.TERISI  # tetap terisi karena open ≤ limit
    assert p.harga_beli == D("940.94")
    assert p.biaya_beli_idr == D("188470.282")
    assert p.target is None  # harga isi ≤ stop: tidak ada target
    assert k.tanggal == tgl(E0) and k.alasan == ex.STOP_GAP_HARI_ENTRY
    assert k.harga == D("939.06")
    assert k.hasil_jual_idr == D("187342.47")
    assert k.pnl_idr == D("-1127.812")
    assert p.risiko_awal_idr == D("20979.525")
    assert k.r == D("-1127.812") / D("20979.525")
    assert D("-1") < k.r < 0
    assert k.slippage_idr == D("188")  # 200 × (940 − 939,06)


def test_stop_gap_hari_entry_batas_open_sama_dengan_stop():
    # open = stop = 950 → tetap STOP_GAP_HARI_ENTRY (≤). isi 950,95; jual 949,05.
    pp = buat_pasar({KODE: [*AWAL, (950, 960, 945, 955)]})
    k = jalankan(pp, order(i_sinyal=S0), E0, E0).keluar
    assert k.alasan == ex.STOP_GAP_HARI_ENTRY
    assert k.harga == D("949.05")
    assert k.pnl_idr == hasil_jual(100, "949.05") - biaya_beli(100, "950.95")


def test_stop_gap_hari_entry_tetap_berlaku_saat_stop_tidak_aktif_hari_entry():
    # §15.1 "Hari entry": aturan open ≤ S tidak bergantung pada stop_aktif_hari_entry (hanya langkah 5–6).
    cfg = cfg_uji({"entry.stop_aktif_hari_entry": False})
    pp = buat_pasar({KODE: [*AWAL, (940, 960, 930, 950)]}, cfg)
    k = jalankan(pp, order(i_sinyal=S0), E0, E0).keluar
    assert k is not None and k.alasan == ex.STOP_GAP_HARI_ENTRY and k.harga == D("939.06")


# Bar hari entry: open 1.000 (> stop), low 940 (≤ stop 950), high 1.110 (≥ target 1.103).
ENTRY_LEBAR = (1000, 1110, 940, 1000)


def test_stop_aktif_hari_entry_true_stop_didahulukan_di_hari_entry():
    pp, od = pb_dasar(entry=ENTRY_LEBAR)
    k = jalankan(pp, od, E0, E0).keluar
    cek_keluar(k, tanggal=tgl(E0), alasan=ex.STOP, harga="949.05")


def test_stop_aktif_hari_entry_false_tanpa_stop_target_hari_entry():
    cfg = cfg_uji({"entry.stop_aktif_hari_entry": False})
    pp, od = pb_dasar(NORMAL, (1000, 1010, 945, 990), cfg=cfg, entry=ENTRY_LEBAR)
    j = jalankan(pp, od, E0, E0 + 2)
    assert j.jadwal[tgl(E0)] is None  # tidak keluar di hari entry walau low ≤ stop dan high ≥ target
    assert j.keluar is not None
    # Hari ke-2 normal; hari ke-3 low 945 ≤ 950 → STOP di 949,05.
    cek_keluar(j.keluar, tanggal=tgl(E0 + 2), alasan=ex.STOP, harga="949.05")


def test_stop_aktif_hari_entry_false_langkah7_tetap_berlaku():
    # PAKSA_JUAL_PASAR dijadwalkan pada close hari entry walau stop belum aktif → jual di open hari ke-2.
    cfg = cfg_uji({"entry.stop_aktif_hari_entry": False, "kondisi_pasar.jangan_beli_paksa_jual": True})
    pp, od = pb_dasar((1004, 1010, 990, 1000), cfg=cfg, entry=ENTRY_LEBAR)
    j = jalankan(pp, od, E0, E0 + 1, status=JANGAN_BELI)
    assert j.jadwal[tgl(E0)] == ex.PAKSA_JUAL_PASAR
    # 1.004 × 0,999 = 1.002,996
    cek_keluar(j.keluar, tanggal=tgl(E0 + 1), alasan=ex.PAKSA_JUAL_PASAR, harga="1002.996")


def test_stop_aktif_hari_entry_false_trailing_bo_tetap_diperbarui_di_hari_entry():
    # Pemanasan ATR(14) datar: TR = 10 setiap bar → ATR = 10. Entry BO: high 1.060, TR 60 →
    # ATR = (13 × 10 + 60)/14 = 190/14; trail = 1.060 − 3 × 190/14 = 1.019,2857 → ke atas 1.020.
    cfg = cfg_uji({"entry.stop_aktif_hari_entry": False})
    datar = (1000, 1005, 995, 1000)
    pp = buat_pasar({KODE: [datar] * 15 + [(1000, 1060, 1000, 1060)]}, cfg)
    od = order(i_sinyal=I0 + 14, strategi="BO", harga_maks="1010", stop="980", target_r=None)
    j = jalankan(pp, od, I0 + 15, I0 + 15)
    assert j.stop[tgl(I0 + 15)] == D("1020")


# Bar hari entry: open 1.000 (isi 1.001, target 1.103), high 1.105 ≥ target, low 995 > stop 950.
ENTRY_TARGET = (1000, 1105, 995, 1050)


def test_target_intraday_di_hari_entry_bila_stop_aktif():
    # Langkah 6 berlaku di hari entry (stop_aktif_hari_entry: true): TARGET di 1.103 pada hari entry.
    pp, od = pb_dasar(entry=ENTRY_TARGET)
    cek_keluar(jalankan(pp, od, E0, E0).keluar, tanggal=tgl(E0), alasan=ex.TARGET, harga="1103")


def test_target_tidak_dieksekusi_di_hari_entry_bila_stop_tidak_aktif():
    # stop_aktif_hari_entry: false → order target baru dipasang besok. E+1 open 1.050 < 1.103, high 1.104 ≥ 1.103
    # → TARGET di 1.103 pada E+1 (P&L 9.774,10, sama dengan test_target_intraday_tepat_di_target).
    cfg = cfg_uji({"entry.stop_aktif_hari_entry": False})
    pp, od = pb_dasar((1050, 1104, 1040, 1060), cfg=cfg, entry=ENTRY_TARGET)
    j = jalankan(pp, od, E0, E0 + 1)
    assert tgl(E0) in j.nilai and j.jadwal[tgl(E0)] is None  # masih terbuka setelah close hari entry
    cek_keluar(j.keluar, tanggal=tgl(E0 + 1), alasan=ex.TARGET, harga="1103")
    assert j.keluar.pnl_idr == D("9774.10")


# =====================================================================================================
# Stop, gap, target (§15.1 langkah 3–6)
# =====================================================================================================


def test_stop_intraday_di_stop_dikurangi_slippage():
    # low 940 ≤ 950 → 950 × 0,999 = 949,05; hasil = 94.905 − 237,2625 = 94.667,7375; P&L −5.582,4125.
    pp, od = pb_dasar((990, 995, 940, 960))
    k = jalankan(pp, od, E0, E0 + 1).keluar
    cek_keluar(k, tanggal=tgl(E0 + 1), alasan=ex.STOP, harga="949.05")
    assert k.pnl_idr == D("-5582.4125")
    assert k.slippage_idr == D("95")  # 100 × (950 − 949,05)
    assert k.harga_nominal == D("949.05")


def test_stop_intraday_batas_low_sama_dengan_stop():
    pp, od = pb_dasar((990, 995, 950, 960))
    cek_keluar(jalankan(pp, od, E0, E0 + 1).keluar, tanggal=tgl(E0 + 1), alasan=ex.STOP, harga="949.05")


def test_low_di_atas_stop_tidak_keluar():
    pp, od = pb_dasar((990, 995, 951, 960))
    assert jalankan(pp, od, E0, E0 + 1).keluar is None


def test_gap_di_bawah_stop_posisi_lama_keluar_di_open():
    # open 930 ≤ 950 → 930 × 0,999 = 929,07 (bukan di level stop).
    pp, od = pb_dasar((930, 940, 920, 935))
    k = jalankan(pp, od, E0, E0 + 1).keluar
    cek_keluar(k, tanggal=tgl(E0 + 1), alasan=ex.STOP_GAP, harga="929.07")
    assert k.pnl_idr == D("-7575.4175")  # 92.674,7325 − 100.250,15
    assert k.slippage_idr == D("93")  # 100 × (930 − 929,07)


def test_gap_batas_open_sama_dengan_stop():
    pp, od = pb_dasar((950, 960, 945, 955))
    cek_keluar(jalankan(pp, od, E0, E0 + 1).keluar, tanggal=tgl(E0 + 1), alasan=ex.STOP_GAP, harga="949.05")


def test_stop_dan_target_hari_sama_stop_didahulukan():
    # low 945 ≤ 950 dan high 1.110 ≥ 1.103 pada hari yang sama → STOP.
    pp, od = pb_dasar((1000, 1110, 945, 1050))
    cek_keluar(jalankan(pp, od, E0, E0 + 1).keluar, tanggal=tgl(E0 + 1), alasan=ex.STOP, harga="949.05")


def test_gap_di_atas_target_keluar_di_open_tanpa_slippage():
    # open 1.110 ≥ 1.103 → TARGET_GAP di 1.110; hasil 111.000 − 277,5 = 110.722,5; P&L 10.472,35.
    pp, od = pb_dasar((1110, 1120, 1100, 1115))
    k = jalankan(pp, od, E0, E0 + 1).keluar
    cek_keluar(k, tanggal=tgl(E0 + 1), alasan=ex.TARGET_GAP, harga="1110")
    assert k.pnl_idr == D("10472.35")
    assert k.slippage_idr == 0


def test_target_gap_batas_open_sama_dengan_target():
    pp, od = pb_dasar((1103, 1110, 1100, 1105))
    cek_keluar(jalankan(pp, od, E0, E0 + 1).keluar, tanggal=tgl(E0 + 1), alasan=ex.TARGET_GAP, harga="1103")


def test_target_intraday_tepat_di_target():
    # high = 1.103 = T → keluar di 1.103 (limit, tanpa slippage); hasil 110.300 − 275,75; P&L 9.774,10.
    pp, od = pb_dasar((1050, 1103, 1040, 1060))
    k = jalankan(pp, od, E0, E0 + 1).keluar
    cek_keluar(k, tanggal=tgl(E0 + 1), alasan=ex.TARGET, harga="1103")
    assert k.pnl_idr == D("9774.10")
    assert k.slippage_idr == 0


def test_high_di_bawah_target_tidak_keluar():
    pp, od = pb_dasar((1050, 1102, 1040, 1060))
    assert jalankan(pp, od, E0, E0 + 1).keluar is None


def test_jual_terjadwal_didahulukan_dari_gap_target():
    # STOP_WAKTU dijadwalkan pada close hari ke-10; open hari ke-11 1.110 ≥ target 1.103. Langkah 2 (jual
    # terjadwal) sebelum langkah 4: STOP_WAKTU di 1.110 × 0,999 = 1.108,89, bukan TARGET_GAP di 1.110.
    pp, od = pb_dasar(*([NORMAL] * 9), (1110, 1120, 1100, 1110))
    j = jalankan(pp, od, E0, E0 + 10)
    cek_keluar(j.keluar, tanggal=tgl(E0 + 10), alasan=ex.STOP_WAKTU, harga="1108.89")


@pytest.mark.parametrize(
    "bar_e1",
    [
        pytest.param((1110, 1200, 900, 1000, 0), id="tanpa-transaksi"),
        pytest.param((1110, 1200, 900, 1300), id="rusak-close>high"),
    ],
)
def test_bar_tanpa_transaksi_atau_rusak_tidak_mengeksekusi_stop_maupun_target(bar_e1):
    # E+1: low 900 ≤ stop 950, open/high ≥ target 1.103, tetapi bar tidak valid → tidak ada eksekusi apa pun
    # (§15.1 langkah 0; bar tanpa transaksi tidak pernah menjadi harga eksekusi, §8.5). Nilai posisi memakai
    # close valid terakhir (1.000). Tidak ada jual terjadwal → tidak ada tanda tertunda.
    pp, od = pb_dasar(bar_e1, NORMAL)
    j = jalankan(pp, od, E0, E0 + 2)
    assert j.keluar is None
    assert j.nilai[tgl(E0 + 1)] == D("100000")
    assert j.jadwal[tgl(E0 + 1)] is None
    assert j.pos.tanda == set()


# --- Target PB dibulatkan ke BAWAH (§12.1, §4.1) --------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "open_entry", "target"),
    [
        # Fraksi belum terverifikasi → rupiah bulat ke bawah.
        pytest.param(CFG, 1000, "1103", id="rilis-1001"),  # 1.001 + 2 × 51 = 1.103
        pytest.param(CFG, 1003, "1112", id="rilis-1004,003"),  # 1.004,003 + 2 × 54,003 = 1.112,009 → 1.112
        # Fraksi FIKTIF terverifikasi (fraksi 5 pada [500, 2000)) → kelipatan 5 ke bawah.
        pytest.param(cfg_dengan(ubah_fraksi()), 1000, "1100", id="fiktif-1103→1100"),
        pytest.param(cfg_dengan(ubah_fraksi()), 1003, "1110", id="fiktif-1112,009→1110"),
        # Terverifikasi tetapi tanggal entry < berlaku_mulai → rupiah bulat saja.
        pytest.param(cfg_dengan(ubah_fraksi(berlaku=BERLAKU_NANTI)), 1003, "1112", id="fiktif-belum-berlaku"),
    ],
)
def test_target_pb_dari_harga_isi_dibulatkan_ke_bawah(cfg, open_entry, target):
    pp = buat_pasar({KODE: [*AWAL, (open_entry, 1010, 990, 1000)]}, cfg)
    p = jalankan(pp, order(i_sinyal=S0), E0, E0).pos
    isi = D(open_entry) * (1 + SLIP)
    assert p.harga_beli == isi
    assert p.target == D(target)
    assert p.target <= isi + 2 * (isi - D(950))  # selalu ke bawah


def test_target_pb_memakai_target_r_order():
    # target_r 3 → 1.001 + 3 × 51 = 1.154.
    pp = buat_pasar({KODE: [*AWAL, NORMAL]})
    p = jalankan(pp, order(i_sinyal=S0, target_r=3.0), E0, E0).pos
    assert p.target == D("1154")


# =====================================================================================================
# BO: trailing (§12.2) — 5 hari hitung tangan, dibulatkan ke atas, tidak pernah turun, berlaku besok
# =====================================================================================================

CFG_ATR3 = cfg_uji({"indikator.atr_periode": 3})
# ATR Wilder n = 3 (hitung tangan; TR = max(h − l, |h − c_kemarin|, |l − c_kemarin|)):
#   W1 (1.000/1.010/990/1.000)  TR 20
#   W2 (1.000/1.015/995/1.010)  TR 20
#   S  (1.010/1.040/1.005/1.035) TR 35 → ATR = (20 + 20 + 35)/3 = 25
#      stop BO = 1.035 − 2 × 25 = 985; harga_maks = 1.035 + 12,5 → 1.047
#   E   (1.040/1.060/1.030/1.055) TR 30 → ATR = (2·25 + 30)/3      = 80/3     ≈ 26,667
#   E+1 (1.055/1.080/1.050/1.075) TR 30 → ATR = (2·80/3 + 30)/3    = 250/9    ≈ 27,778
#   E+2 (1.075/1.078/1.040/1.045) TR 38 → ATR = (2·250/9 + 38)/3   = 842/27   ≈ 31,185
#   E+3 (1.045/1.100/1.045/1.095) TR 55 → ATR = (2·842/27 + 55)/3  = 3169/81  ≈ 39,123
#   E+4 (1.095/1.120/1.090/1.110) TR 30 → ATR = (2·3169/81 + 30)/3 = 8768/243 ≈ 36,082
# Trail (trailing_atr 3), HHV sejak entry (bar sinyal tidak termasuk):
#   close E  : 1.060 − 3 × 80/3     = 980       → 980  < 985 → tetap 985 (trail awal = stop_awal)
#   close E+1: 1.080 − 3 × 250/9    = 996,667   → ke atas 997
#   close E+2: 1.080 − 3 × 842/27   = 986,444   → 987  < 997 → tetap 997 (tidak pernah turun)
#   close E+3: 1.100 − 3 × 3169/81  = 982,630   → 983  < 997 → tetap 997
#   close E+4: 1.120 − 3 × 8768/243 = 1.011,753 → ke atas 1.012
#   E+5 (1.100/1.105/1.011/1.020): low 1.011 ≤ 1.012 → TRAILING di 1.012 × 0,999 = 1.010,988
BO_BAR = [
    (1000, 1010, 990, 1000),
    (1000, 1015, 995, 1010),
    (1010, 1040, 1005, 1035),
    (1040, 1060, 1030, 1055),
    (1055, 1080, 1050, 1075),
    (1075, 1078, 1040, 1045),
    (1045, 1100, 1045, 1095),
    (1095, 1120, 1090, 1110),
    (1100, 1105, 1011, 1020),
]
BO_S, BO_E = I0 + 2, I0 + 3


def _order_bo() -> ex.Order:
    return order(i_sinyal=BO_S, strategi="BO", harga_maks="1047", stop="985", target_r=None)


def test_trailing_bo_lima_hari_hitung_tangan():
    pp = buat_pasar({KODE: BO_BAR}, CFG_ATR3)
    j = jalankan(pp, _order_bo(), BO_E, BO_E + 5)
    assert j.pos.target is None  # BO tanpa target tetap
    assert j.pos.harga_beli == D("1041.04")  # min(1.040 × 1,001; 1.047)
    trail = [j.stop[tgl(BO_E + k)] for k in range(5)]
    assert trail == [D("985"), D("997"), D("997"), D("997"), D("1012")]
    assert all(b >= a for a, b in zip(trail, trail[1:]))  # tidak pernah turun
    assert j.pos.stop_awal == D("985")
    k = j.keluar
    assert k.tanggal == tgl(BO_E + 5) and k.alasan == ex.TRAILING
    assert k.harga == D("1010.988")
    biaya = biaya_beli(100, "1041.04")  # 104.104 + 156,156 = 104.260,156
    assert k.pnl_idr == hasil_jual(100, "1010.988") - biaya
    assert k.r == k.pnl_idr / risiko_rencana(100, 1047, 985)


def test_trailing_bo_dibulatkan_ke_atas_ke_fraksi_fiktif():
    # Fraksi FIKTIF 5 pada [500, 2000): 996,667 → 1.000; 986,444 → 990; 982,630 → 985; 1.011,753 → 1.015.
    pp = buat_pasar({KODE: BO_BAR}, cfg_dengan(ubah_fraksi(), {"indikator.atr_periode": 3}))
    j = jalankan(pp, _order_bo(), BO_E, BO_E + 5)
    trail = [j.stop[tgl(BO_E + k)] for k in range(5)]
    assert trail == [D("985"), D("1000"), D("1000"), D("1000"), D("1015")]
    assert j.keluar.alasan == ex.TRAILING and j.keluar.harga == D("1015") * (1 - SLIP)  # 1.013,985


def test_trailing_bo_berlaku_mulai_hari_berikutnya():
    # ATR(14) datar = 10 (TR 10 setiap bar pemanasan). Stop awal 980.
    #   E   (1.000/1.005/995/1.000): trail = 1.005 − 30 = 975 → tetap 980.
    #   E+1 (1.000/1.060/1.000/1.060): TR 60 → ATR = 190/14; trail = 1.060 − 3 × 190/14 = 1.019,29 → 1.020.
    #       low E+1 = 1.000 < 1.020 tetapi > 980: tidak keluar pada E+1 (level baru berlaku besok).
    #   E+2 (1.030/1.035/1.019/1.025): low 1.019 ≤ 1.020 → TRAILING di 1.020 × 0,999 = 1.018,98.
    datar = (1000, 1005, 995, 1000)
    pp = buat_pasar({KODE: [datar] * 15 + [datar, (1000, 1060, 1000, 1060), (1030, 1035, 1019, 1025)]})
    od = order(i_sinyal=I0 + 14, strategi="BO", harga_maks="1010", stop="980", target_r=None)
    j = jalankan(pp, od, I0 + 15, I0 + 17)
    assert j.stop[tgl(I0 + 15)] == D("980")
    assert j.stop[tgl(I0 + 16)] == D("1020")
    k = j.keluar
    assert k.tanggal == tgl(I0 + 17) and k.alasan == ex.TRAILING and k.harga == D("1018.98")


def test_trailing_bo_hhv_sejak_entry_tanpa_bar_sinyal():
    # §9: HHV_sejak_entry dimulai dari hari entry; bar hari sinyal tidak termasuk.
    # Pemanasan ATR(14) datar (TR 10). Bar sinyal (1.000/1.040/995/1.000): TR 45 → ATR = (13·10 + 45)/14 = 12,5.
    # E (1.000/1.005/995/1.000): TR 10 → ATR = (13·12,5 + 10)/14 = 172,5/14 ≈ 12,3214.
    #   trail = 1.005 − 3 × 172,5/14 = 968,036 → 969 < 980 → tetap 980.
    #   (Bila high bar sinyal 1.040 ikut: 1.040 − 36,964 = 1.003,036 → 1.004, dan E+1 low 990 akan keluar.)
    datar = (1000, 1005, 995, 1000)
    pp = buat_pasar({KODE: [datar] * 14 + [(1000, 1040, 995, 1000), datar, (1000, 1005, 990, 1000)]})
    od = order(i_sinyal=I0 + 14, strategi="BO", harga_maks="1010", stop="980", target_r=None)
    j = jalankan(pp, od, I0 + 15, I0 + 16)
    assert j.stop[tgl(I0 + 15)] == D("980")
    assert j.keluar is None


def test_bo_keluar_di_trailing_awal_berlabel_trailing():
    # E+1 low 980 ≤ trail awal 985 (= stop_awal) → TRAILING (bukan STOP), di 985 × 0,999 = 984,015.
    bar = BO_BAR[:4] + [(1050, 1060, 980, 1000)]
    pp = buat_pasar({KODE: bar}, CFG_ATR3)
    k = jalankan(pp, _order_bo(), BO_E, BO_E + 1).keluar
    assert k.alasan == ex.TRAILING and k.harga == D("984.015")


def test_bo_gap_di_bawah_trailing_berlabel_trailing_gap():
    # E+1 open 970 ≤ 985 → TRAILING_GAP di 970 × 0,999 = 969,03.
    bar = BO_BAR[:4] + [(970, 990, 960, 980)]
    pp = buat_pasar({KODE: bar}, CFG_ATR3)
    k = jalankan(pp, _order_bo(), BO_E, BO_E + 1).keluar
    assert k.alasan == ex.TRAILING_GAP and k.harga == D("969.03")


def test_bo_hari_entry_open_di_bawah_stop_berlabel_stop_gap_hari_entry():
    # Label hari entry sama untuk semua strategi (§15.1 "Hari entry"), bukan TRAILING_GAP.
    # open 980 ≤ 985 → isi 980,98; jual 980 × 0,999 = 979,02.
    pp = buat_pasar({KODE: BO_BAR[:3] + [(980, 990, 975, 985)]}, CFG_ATR3)
    j = jalankan(pp, _order_bo(), BO_E, BO_E)
    k = j.keluar
    assert k.tanggal == tgl(BO_E) and k.alasan == ex.STOP_GAP_HARI_ENTRY and k.harga == D("979.02")
    assert k.pnl_idr == hasil_jual(100, "979.02") - biaya_beli(100, "980.98")
    assert k.r == k.pnl_idr / risiko_rencana(100, 1047, 985)


def test_bo_stop_intraday_hari_entry_berlabel_trailing():
    # stop_aktif_hari_entry: true; open 1.040 > 985, low 984 ≤ 985 (trailing awal = stop_awal) → TRAILING di
    # 985 × 0,999 = 984,015 pada hari entry.
    pp = buat_pasar({KODE: BO_BAR[:3] + [(1040, 1060, 984, 1000)]}, CFG_ATR3)
    k = jalankan(pp, _order_bo(), BO_E, BO_E).keluar
    assert k.tanggal == tgl(BO_E) and k.alasan == ex.TRAILING and k.harga == D("984.015")


def test_bo_gap_di_bawah_trailing_yang_sudah_naik():
    # Trail 1.012 setelah close E+4; E+5 open 1.005 ≤ 1.012 → TRAILING_GAP di 1.005 × 0,999 = 1.003,995.
    bar = BO_BAR[:8] + [(1005, 1010, 1000, 1005)]
    pp = buat_pasar({KODE: bar}, CFG_ATR3)
    k = jalankan(pp, _order_bo(), BO_E, BO_E + 5).keluar
    assert k.tanggal == tgl(BO_E + 5) and k.alasan == ex.TRAILING_GAP and k.harga == D("1003.995")


@pytest.mark.parametrize(
    "bar_e3",
    [
        pytest.param(None, id="bar-hilang"),
        pytest.param((1045, 1500, 1045, 1095, 0), id="tanpa-transaksi-high-1500"),
    ],
)
def test_trailing_tidak_diperbarui_tanpa_bar_valid(bar_e3):
    # E+3 tanpa bar valid: trail tidak diperbarui pada E+3, dan high bar tanpa transaksi tidak ikut HHV.
    # E+4 (bar valid berikutnya): ATR dari E+2 → TR E+4 = max(30, |1.120 − 1.045|, |1.090 − 1.045|) = 75,
    #   ATR = (2 × 842/27 + 75)/3 = 3709/81; trail = 1.120 − 3 × 3709/81 = 982,630 → 983 < 997 → tetap 997.
    #   (Bila high 1.500 ikut HHV, trail akan 1.500 − 137,37 = 1.362,63.)
    bar = BO_BAR[:6] + [bar_e3, BO_BAR[7]]
    pp = buat_pasar({KODE: bar}, CFG_ATR3)
    j = jalankan(pp, _order_bo(), BO_E, BO_E + 4)
    assert j.keluar is None
    assert j.stop[tgl(BO_E + 3)] == D("997")  # tanpa bar valid: langkah 7 tidak memperbarui trailing
    assert j.pos.stop == D("997")
    assert j.stop[tgl(BO_E + 4)] == D("997")


def test_bo_tanpa_stop_waktu_default_null():
    # stop_waktu_hari BO = null: posisi BO yang diam 15 hari tidak pernah dijadwalkan jual.
    datar = (1000, 1005, 995, 1000)
    pp = buat_pasar({KODE: [datar] * 15 + [datar] * 15})
    od = order(i_sinyal=I0 + 14, strategi="BO", harga_maks="1010", stop="980", target_r=None)
    j = jalankan(pp, od, I0 + 15, I0 + 29)
    assert j.keluar is None and all(v is None for v in j.jadwal.values())


def test_bo_stop_waktu_dari_config():
    cfg = cfg_uji({"strategi.bo.stop_waktu_hari": 3})
    datar = (1000, 1005, 995, 1000)
    pp = buat_pasar({KODE: [datar] * 15 + [datar] * 3 + [(1002, 1005, 995, 1000)]}, cfg)
    od = order(i_sinyal=I0 + 14, strategi="BO", harga_maks="1010", stop="980", target_r=None)
    j = jalankan(pp, od, I0 + 15, I0 + 18)
    assert j.jadwal[tgl(I0 + 16)] is None and j.jadwal[tgl(I0 + 17)] == ex.STOP_WAKTU
    assert j.keluar.tanggal == tgl(I0 + 18) and j.keluar.alasan == ex.STOP_WAKTU
    assert j.keluar.harga == D("1002") * (1 - SLIP)


# =====================================================================================================
# MR: keluar SMA5 mulai hari ke-2, stop waktu hari ke-5 (§12.3)
# =====================================================================================================


def _b(c: float) -> Bar:
    return (c, c + 3, c - 3, c)


# Pemanasan 5 bar (close 1.000, 990, 980, 970, 960); sinyal pada bar ke-5 (close 960).
MR_AWAL = [_b(1000), _b(990), _b(980), _b(970), _b(960)]
MR_S, MR_E = I0 + 4, I0 + 5


def _order_mr() -> ex.Order:
    return order(i_sinyal=MR_S, strategi="MR", harga_maks="970", stop="900", target_r=None)


def test_mr_keluar_sma_hanya_mulai_hari_ke_2():
    # Hari ke-1 (E): close 1.000 > SMA5 = (990+980+970+960+1.000)/5 = 980 → tidak dijadwalkan (hari ke-1).
    # Hari ke-2 (E+1): close 1.010 > SMA5 = (980+970+960+1.000+1.010)/5 = 984 → KELUAR_SMA.
    # E+2: jual di open 1.012 × 0,999 = 1.010,988.
    bar = MR_AWAL + [(965, 1005, 960, 1000), (1000, 1015, 995, 1010), (1012, 1020, 1005, 1015)]
    pp = buat_pasar({KODE: bar})
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 2)
    assert j.pos.harga_beli == D("965.965")  # min(965 × 1,001; 970)
    assert j.pos.target is None
    assert j.jadwal[tgl(MR_E)] is None
    assert j.jadwal[tgl(MR_E + 1)] == ex.KELUAR_SMA
    k = j.keluar
    assert k.tanggal == tgl(MR_E + 2) and k.alasan == ex.KELUAR_SMA and k.harga == D("1010.988")
    biaya = biaya_beli(100, "965.965")
    assert k.pnl_idr == hasil_jual(100, "1010.988") - biaya
    assert k.r == k.pnl_idr / risiko_rencana(100, 970, 900)


def test_mr_min_hari_tahan_dari_config():
    # min_hari_tahan 3: hari ke-2 belum; hari ke-3 (E+2): close 1.015 > SMA5 = (970+960+1.000+1.010+1.015)/5
    # = 991 → KELUAR_SMA; jual di open E+3.
    cfg = cfg_uji({"strategi.mr.min_hari_tahan": 3})
    bar = MR_AWAL + [(965, 1005, 960, 1000), (1000, 1015, 995, 1010), (1012, 1020, 1005, 1015),
                     (1016, 1020, 1010, 1018)]
    pp = buat_pasar({KODE: bar}, cfg)
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 3)
    assert j.jadwal[tgl(MR_E + 1)] is None
    assert j.jadwal[tgl(MR_E + 2)] == ex.KELUAR_SMA
    assert j.keluar.tanggal == tgl(MR_E + 3) and j.keluar.harga == D("1016") * (1 - SLIP)


def test_mr_close_sama_dengan_sma_tidak_keluar():
    # Hari ke-2 dengan close tepat = SMA5: c = (980 + 970 + 960 + 1.000 + c)/5 → c = 977,5.
    bar = MR_AWAL + [(965, 1005, 960, 1000), (980, 985, 975, 977.5)]
    pp = buat_pasar({KODE: bar})
    baris = pp.tampilan(tgl(MR_E + 1)).baris(KODE)
    assert float(baris["sma_5"]) == float(baris["close"]) == 977.5  # batas tepat, tanpa derau float
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 1)
    assert j.jadwal[tgl(MR_E + 1)] is None  # syarat ketat: close_d > SMA5_d


def test_mr_stop_waktu_hari_ke_5_jual_open_hari_ke_6():
    # Close turun pelan (955, 950, 945, 940, 935), selalu < SMA5, low ≥ 932 > stop 900.
    #   SMA5 hari ke-5 = (955+950+945+940+935)/5 = 945 > 935 → hanya STOP_WAKTU (hari ke-5).
    bar = MR_AWAL + [_b(955), _b(950), _b(945), _b(940), _b(935), (936, 940, 930, 935)]
    pp = buat_pasar({KODE: bar})
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 5)
    assert [j.jadwal[tgl(MR_E + k)] for k in range(4)] == [None] * 4
    assert j.jadwal[tgl(MR_E + 4)] == ex.STOP_WAKTU
    k = j.keluar
    assert k.tanggal == tgl(MR_E + 5) and k.alasan == ex.STOP_WAKTU
    assert k.harga == D("935.064")  # 936 × 0,999
    assert k.pnl_idr == hasil_jual(100, "935.064") - biaya_beli(100, "955.955")


def test_mr_stop_intraday_berlabel_stop():
    bar = MR_AWAL + [_b(955), (930, 935, 899, 910)]
    pp = buat_pasar({KODE: bar})
    k = jalankan(pp, _order_mr(), MR_E, MR_E + 1).keluar
    assert k.alasan == ex.STOP and k.harga == D("899.1")  # 900 × 0,999


# =====================================================================================================
# PB & ACAK: stop waktu hari ke-10 → open hari ke-11 (§12.1, §12.4)
# =====================================================================================================


@pytest.mark.parametrize("strategi", ["PB", "ACAK"])
def test_stop_waktu_hari_ke_10_jual_open_hari_ke_11(strategi):
    # 10 hari bar normal (tanpa stop/target), hari ke-11 open 1.005 → 1.005 × 0,999 = 1.003,995;
    # hasil = 100.399,5 − 250,99875 = 100.148,50125; P&L = −101,64875.
    pp, od = pb_dasar(*([NORMAL] * 9), (1005, 1010, 990, 1000), strategi=strategi)
    j = jalankan(pp, od, E0, E0 + 10)
    assert j.jadwal[tgl(E0 + 8)] is None  # hari ke-9
    assert j.jadwal[tgl(E0 + 9)] == ex.STOP_WAKTU  # close hari ke-10
    cek_keluar(j.keluar, tanggal=tgl(E0 + 10), alasan=ex.STOP_WAKTU, harga="1003.995")
    assert j.keluar.pnl_idr == D("-101.64875")


def test_stop_waktu_acak_dan_pb_memakai_kunci_masing_masing():
    cfg = cfg_uji({"strategi.acak.stop_waktu_hari": 7, "strategi.pb.stop_waktu_hari": 4})
    hasil = {}
    for strategi in ("PB", "ACAK"):
        pp, od = pb_dasar(*([NORMAL] * 10), cfg=cfg, strategi=strategi)
        j = jalankan(pp, od, E0, E0 + 10)
        hasil[strategi] = j.keluar.tanggal
    assert hasil == {"PB": tgl(E0 + 4), "ACAK": tgl(E0 + 7)}


@pytest.mark.parametrize(
    ("bar_e1", "alasan", "harga"),
    [
        pytest.param((1050, 1105, 1040, 1060), ex.TARGET, "1103", id="target"),
        pytest.param((990, 995, 940, 960), ex.STOP, "949.05", id="stop"),
        pytest.param((930, 940, 920, 935), ex.STOP_GAP, "929.07", id="stop-gap"),
    ],
)
def test_acak_target_dan_stop(bar_e1, alasan, harga):
    pp, od = pb_dasar(bar_e1, strategi="ACAK")
    j = jalankan(pp, od, E0, E0 + 1)
    assert j.pos.target == TARGET_DASAR  # 1.001 + 2 × 51
    cek_keluar(j.keluar, tanggal=tgl(E0 + 1), alasan=alasan, harga=harga)


# =====================================================================================================
# Bar hilang / tanpa transaksi menunda jual terjadwal (§15.1 langkah 0)
# =====================================================================================================

# MR: KELUAR_SMA dijadwalkan pada close E+1 (lihat test_mr_keluar_sma_hanya_mulai_hari_ke_2).
MR_JADWAL = MR_AWAL + [(965, 1005, 960, 1000), (1000, 1015, 995, 1010)]


@pytest.mark.parametrize(
    "bar_e2",
    [
        pytest.param(None, id="bar-hilang"),
        pytest.param((1012, 1020, 1005, 1015, 0), id="tanpa-transaksi"),
        pytest.param((1012, 1000, 1005, 1015), id="rusak"),
    ],
)
def test_jual_terjadwal_ditunda_tanpa_bar_valid(bar_e2):
    # E+2 tanpa bar valid → tidak ada eksekusi, tanda TERTUNDA_TANPA_TRANSAKSI; E+3 open 1.008 → KELUAR_SMA
    # (label asli) di 1.008 × 0,999 = 1.006,992.
    pp = buat_pasar({KODE: MR_JADWAL + [bar_e2, (1008, 1012, 1000, 1010)]})
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 3)
    k = j.keluar
    assert k.tanggal == tgl(MR_E + 3) and k.alasan == ex.KELUAR_SMA and k.harga == D("1006.992")
    assert ex.TERTUNDA_TANPA_TRANSAKSI in j.pos.tanda
    assert ex.TERTUNDA_ARB not in j.pos.tanda


def test_jual_terjadwal_tertunda_tetap_label_asli_walau_open_di_bawah_stop():
    # Langkah 2 sebelum langkah 3: open 880 ≤ stop 900 tetapi jual terjadwal → KELUAR_SMA di 880 × 0,999.
    pp = buat_pasar({KODE: MR_JADWAL + [None, (880, 890, 870, 885)]})
    k = jalankan(pp, _order_mr(), MR_E, MR_E + 3).keluar
    assert k.alasan == ex.KELUAR_SMA and k.harga == D("879.12")


def test_nilai_posisi_tanpa_bar_valid_memakai_close_valid_terakhir():
    pp = buat_pasar({KODE: MR_JADWAL + [None]})
    cfg, bei, kal = pp.cfg, AturanBEI(pp.cfg), pp.kalender
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 1)
    pos = j.pos
    b = ex.bar_hari(pp.tampilan(tgl(MR_E + 2)), KODE)
    assert not b.ada and not b.valid
    assert ex.buka(pos, b, cfg, bei, kal) is None
    assert ex.intraday(pos, b, cfg, hari_entry=False) is None
    ex.perbarui_nilai(pos, b)
    assert ex.tutup(pos, b, pp.tampilan(tgl(MR_E + 2)), cfg, bei, kal, BOLEH_BELI) is None
    assert pos.close_valid_terakhir == D("1010") and pos.tanggal_valid_terakhir == tgl(MR_E + 1)
    assert pos.nilai() == D("101000")  # 100 × close valid terakhir 1.010


@pytest.mark.parametrize(
    "bar_h10",
    [pytest.param(None, id="bar-hilang"), pytest.param((1000, 1010, 990, 1000, 0), id="tanpa-transaksi")],
)
def test_stop_waktu_jatuh_tempo_tanpa_bar_dievaluasi_pada_close_bar_valid_berikutnya(bar_h10):
    # Hari ke-10 tanpa bar valid → tidak ada jadwal pada hari ke-10; hari ke-11 bar valid: tidak dijual di
    # open (belum terjadwal), STOP_WAKTU dijadwalkan pada close hari ke-11, dijual di open hari ke-12.
    pp, od = pb_dasar(*([NORMAL] * 8), bar_h10, NORMAL, (1006, 1010, 990, 1000))
    j = jalankan(pp, od, E0, E0 + 11)
    assert j.jadwal[tgl(E0 + 8)] is None
    assert j.jadwal[tgl(E0 + 9)] is None  # hari ke-10 tanpa bar valid: belum dievaluasi
    assert j.jadwal[tgl(E0 + 10)] == ex.STOP_WAKTU
    cek_keluar(j.keluar, tanggal=tgl(E0 + 11), alasan=ex.STOP_WAKTU, harga=str(D(1006) * (1 - SLIP)))


def test_hitungan_hari_tetap_berjalan_saat_bar_hilang():
    # Bar hilang hari ke-3..5 tidak menghentikan hitungan: stop waktu tetap pada close hari ke-10.
    pp, od = pb_dasar(NORMAL, None, None, None, *([NORMAL] * 5), (1005, 1010, 990, 1000))
    j = jalankan(pp, od, E0, E0 + 10)
    assert j.jadwal[tgl(E0 + 9)] == ex.STOP_WAKTU
    assert j.keluar.tanggal == tgl(E0 + 10)


# =====================================================================================================
# T-BEI: ARB terkunci (tabel FIKTIF – KHUSUS UJI, ARB 13 %) menunda jual (§15.1 langkah 1)
# =====================================================================================================
# close kemarin 1.000 → batas ARB = bulat_atas(1.000 × 0,87) = 870. Bar terkunci: o = h = l = c ≤ 870.
ARB_TERKUNCI = (870, 870, 870, 870)


def test_arb_terkunci_menunda_stop_gap_dan_stop_intraday():
    # E+1 terkunci di 870 (open ≤ stop 950 dan low ≤ stop): tidak ada jual, tanda TERTUNDA_ARB.
    # E+2 open 860 ≤ 950 → STOP_GAP di 860 × 0,999 = 859,14.
    pp, od = pb_dasar(ARB_TERKUNCI, (860, 880, 850, 870), cfg=CFG_AR)
    j = jalankan(pp, od, E0, E0 + 2)
    assert tgl(E0 + 1) in j.jadwal  # posisi masih terbuka setelah close E+1
    assert ex.TERTUNDA_ARB in j.pos.tanda
    cek_keluar(j.keluar, tanggal=tgl(E0 + 2), alasan=ex.STOP_GAP, harga="859.14")


def test_arb_terkunci_intraday_tidak_mengeksekusi_stop():
    pp, od = pb_dasar(ARB_TERKUNCI, cfg=CFG_AR)
    cfg, bei, kal = pp.cfg, AturanBEI(pp.cfg), pp.kalender
    pos = jalankan(pp, od, E0, E0).pos
    b = ex.bar_hari(pp.tampilan(tgl(E0 + 1)), KODE)
    assert ex.terkunci_arb(b, bei)
    assert ex.buka(pos, b, cfg, bei, kal) is None
    assert ex.intraday(pos, b, cfg, hari_entry=False) is None  # low 870 ≤ 950, tetapi terkunci
    assert pos.terbuka and ex.TERTUNDA_ARB in pos.tanda


def test_arb_terkunci_menunda_jual_terjadwal():
    # MR: KELUAR_SMA terjadwal setelah close E+1 (close 1.010). E+2 terkunci: batas ARB = bulat_atas(1.010 ×
    # 0,87 = 878,7) = 879 → bar 879 datar terkunci. E+3 open 870 → KELUAR_SMA di 870 × 0,999 = 869,13.
    pp = buat_pasar({KODE: MR_JADWAL + [(879, 879, 879, 879), (870, 880, 860, 875)]}, CFG_AR)
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 3)
    assert j.jadwal[tgl(MR_E + 2)] == ex.KELUAR_SMA  # tetap terjadwal dengan alasan asli
    k = j.keluar
    assert k.tanggal == tgl(MR_E + 3) and k.alasan == ex.KELUAR_SMA and k.harga == D("869.13")
    assert ex.TERTUNDA_ARB in j.pos.tanda


def test_jual_terjadwal_tertunda_arb_tidak_diberi_label_baru_pada_close_hari_terkunci():
    # KELUAR_SMA terjadwal setelah close E+1. E+2 terkunci ARB (bar valid → langkah 7 tetap jalan) dan status
    # pasar JANGAN BELI dengan paksa jual aktif: jual yang tertunda tetap memakai alasan aslinya (§15.1
    # langkah 2), bukan PAKSA_JUAL_PASAR.
    cfg = cfg_dengan(ubah_ar(), {"kondisi_pasar.jangan_beli_paksa_jual": True})
    pp = buat_pasar({KODE: MR_JADWAL + [(879, 879, 879, 879), (870, 880, 860, 875)]}, cfg)
    hari_kunci = tgl(MR_E + 2)
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 3, status=lambda d: JANGAN_BELI if d == hari_kunci else BOLEH_BELI)
    assert j.jadwal[hari_kunci] == ex.KELUAR_SMA
    assert j.keluar.alasan == ex.KELUAR_SMA and j.keluar.tanggal == tgl(MR_E + 3)


@pytest.mark.parametrize(
    "cfg",
    [
        pytest.param(CFG, id="config-rilis"),
        pytest.param(CFG_AR_TIDAK, id="belum-terverifikasi"),
        pytest.param(CFG_AR_NANTI, id="sebelum-berlaku-mulai"),
    ],
)
def test_arb_tidak_dimodelkan_bila_belum_terverifikasi_atau_belum_berlaku(cfg):
    # Bar yang sama 870 datar → STOP_GAP langsung di 870 × 0,999 = 869,13 pada E+1.
    pp, od = pb_dasar(ARB_TERKUNCI, (860, 880, 850, 870), cfg=cfg)
    j = jalankan(pp, od, E0, E0 + 2)
    cek_keluar(j.keluar, tanggal=tgl(E0 + 1), alasan=ex.STOP_GAP, harga="869.13")
    assert ex.TERTUNDA_ARB not in j.pos.tanda


@pytest.mark.parametrize(
    ("bar_e1", "harga"),
    [
        pytest.param((871, 871, 871, 871), "870.129", id="datar-di-atas-batas-871"),
        pytest.param((870, 880, 870, 870), "869.13", id="tidak-datar"),
    ],
)
def test_bar_tidak_terkunci_arb_dieksekusi(bar_e1, harga):
    pp, od = pb_dasar(bar_e1, cfg=CFG_AR)
    cek_keluar(jalankan(pp, od, E0, E0 + 1).keluar, tanggal=tgl(E0 + 1), alasan=ex.STOP_GAP, harga=harga)


def test_arb_terkunci_di_hari_entry_menunda_stop_gap_hari_entry():
    # Entry terisi di open 870 (≤ harga_maks), bar entry terkunci ARB (close kemarin 1.000 → batas 870),
    # open ≤ stop 950 → jual ditunda (TERTUNDA_ARB). E+1 open 860 → keluar di 860 × 0,999 = 859,14.
    pp, od = pb_dasar((860, 880, 850, 870), cfg=CFG_AR, entry=ARB_TERKUNCI)
    j = jalankan(pp, od, E0, E0 + 1)
    assert j.pos.harga_beli == D("870.87")  # min(870 × 1,001; 1.050)
    assert ex.TERTUNDA_ARB in j.pos.tanda
    assert tgl(E0) in j.jadwal  # tidak ada jual pada hari entry yang terkunci
    k = j.keluar
    assert k is not None and k.tanggal == tgl(E0 + 1)
    assert k.harga == D("859.14")
    assert k.alasan in (ex.STOP_GAP_HARI_ENTRY, ex.STOP_GAP)


# Bar sesudah hari terkunci: open 960 kembali di atas stop 950, low 955 > stop (tidak memicu stop baru).
PULIH = (960, 970, 955, 965)


def test_arb_terkunci_di_hari_entry_jual_tertunda_dieksekusi_di_open_berikutnya():
    # Bar entry terkunci di 870 ≤ stop → jual ditunda (bukan dibatalkan). E+1 open 960 > stop: jual yang tertunda
    # tetap dieksekusi di open E+1: 960 × 0,999 = 959,04.
    pp, od = pb_dasar(PULIH, cfg=CFG_AR, entry=ARB_TERKUNCI)
    j = jalankan(pp, od, E0, E0 + 1)
    k = j.keluar
    assert k is not None and k.tanggal == tgl(E0 + 1) and k.harga == D("959.04")
    assert ex.TERTUNDA_ARB in j.pos.tanda
    assert k.pnl_idr == hasil_jual(100, "959.04") - biaya_beli(100, "870.87")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: exit.buka langkah 1 hanya memberi tanda TERTUNDA_ARB untuk stop yang terlewati pada bar terkunci ARB, "
        "tetapi tidak menjadwalkan jualnya; bila open berikutnya kembali di atas stop, jual tidak pernah terjadi "
        "(§15.1 langkah 1: 'Semua jual ... ditunda ke hari berikutnya'; hari entry: 'jual ditunda seperti biasa', "
        "dan keluar_hari_entry memang menjadwalkannya)"
    ),
)
def test_arb_terkunci_posisi_lama_jual_stop_tertunda_dieksekusi_di_open_berikutnya():
    # E+1 terkunci ARB di 870 ≤ stop 950: jual stop (gap) tidak dapat dieksekusi dan DITUNDA ke E+2. E+2 open 960
    # (> stop, low 955 > stop) → jual yang tertunda dieksekusi di open E+2: 960 × 0,999 = 959,04, label asli
    # STOP_GAP (open E+1 ≤ S). Sama dengan perilaku hari entry di atas.
    pp, od = pb_dasar(ARB_TERKUNCI, PULIH, NORMAL, cfg=CFG_AR)
    j = jalankan(pp, od, E0, E0 + 3)
    assert ex.TERTUNDA_ARB in j.pos.tanda
    cek_keluar(j.keluar, tanggal=tgl(E0 + 2), alasan=ex.STOP_GAP, harga="959.04")


def test_arb_terkunci_di_bawah_batas_juga_terkunci():
    # Datar 860 ≤ batas ARB 870 → terkunci; E+2 open 850 → STOP_GAP di 850 × 0,999 = 849,15.
    pp, od = pb_dasar((860, 860, 860, 860), (850, 870, 840, 860), cfg=CFG_AR)
    j = jalankan(pp, od, E0, E0 + 2)
    assert ex.TERTUNDA_ARB in j.pos.tanda
    cek_keluar(j.keluar, tanggal=tgl(E0 + 2), alasan=ex.STOP_GAP, harga="849.15")


@pytest.mark.parametrize(
    ("berlaku", "ditunda"),
    [
        pytest.param(tgl(E0 + 1), True, id="berlaku-tepat-hari-terkunci"),
        pytest.param(tgl(E0 + 2), False, id="berlaku-besoknya"),
    ],
)
def test_arb_berlaku_mulai_batas_tanggal(berlaku, ditunda):
    # d ≥ berlaku_mulai (inklusif). Dimodelkan → jual E+1 ditunda, STOP_GAP di open E+2 (860 × 0,999 = 859,14).
    # Belum berlaku → STOP_GAP langsung di open E+1 (870 × 0,999 = 869,13).
    pp, od = pb_dasar(ARB_TERKUNCI, (860, 880, 850, 870), cfg=cfg_dengan(ubah_ar(berlaku=berlaku)))
    j = jalankan(pp, od, E0, E0 + 2)
    if ditunda:
        cek_keluar(j.keluar, tanggal=tgl(E0 + 2), alasan=ex.STOP_GAP, harga="859.14")
        assert ex.TERTUNDA_ARB in j.pos.tanda
    else:
        cek_keluar(j.keluar, tanggal=tgl(E0 + 1), alasan=ex.STOP_GAP, harga="869.13")
        assert ex.TERTUNDA_ARB not in j.pos.tanda


def test_arb_terkunci_tanpa_jual_jatuh_tempo_tidak_diberi_tanda():
    # Stop 850 < harga terkunci 870 dan tidak ada jual terjadwal: tidak ada jual yang tertunda → tanpa TERTUNDA_ARB,
    # posisi tetap terbuka.
    pp = buat_pasar({KODE: [*AWAL, NORMAL, ARB_TERKUNCI, (875, 880, 860, 870)]}, CFG_AR)
    j = jalankan(pp, order(i_sinyal=S0, stop="850"), E0, E0 + 2)
    assert j.keluar is None
    assert ex.TERTUNDA_ARB not in j.pos.tanda


def test_jual_terjadwal_ditunda_beruntun_arb_lalu_bar_hilang():
    # MR: KELUAR_SMA terjadwal setelah close E+1 (1.010). E+2 terkunci ARB (batas = bulat_atas(1.010 × 0,87) = 879),
    # E+3 terkunci lagi (acuan 879 → batas = bulat_atas(764,73) = 765), E+4 bar hilang, E+5 open 760 →
    # KELUAR_SMA (label asli) di 760 × 0,999 = 759,24, dengan kedua tanda tertunda.
    bar = MR_JADWAL + [(879, 879, 879, 879), (765, 765, 765, 765), None, (760, 770, 750, 765)]
    pp = buat_pasar({KODE: bar}, CFG_AR)
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 5)
    assert [j.jadwal[tgl(MR_E + k)] for k in (1, 2, 3, 4)] == [ex.KELUAR_SMA] * 4
    k = j.keluar
    assert k.tanggal == tgl(MR_E + 5) and k.alasan == ex.KELUAR_SMA and k.harga == D("759.24")
    assert {ex.TERTUNDA_ARB, ex.TERTUNDA_TANPA_TRANSAKSI} <= j.pos.tanda


# =====================================================================================================
# DATA_BERAKHIR (§15.1 "Seri berhenti")
# =====================================================================================================


@pytest.mark.parametrize(
    "kosong",
    [
        pytest.param(None, id="bar-hilang"),
        pytest.param((1004, 1010, 990, 1004, 0), id="tanpa-transaksi"),
        pytest.param((1004, 1000, 990, 1004), id="rusak-open>high"),
    ],
)
def test_data_berakhir_setelah_maks_hari_tanpa_bar(kosong):
    # maks_hari_tanpa_bar 5: close valid terakhir 1.004 (E+1); E+2..E+6 tanpa bar valid.
    # E+6 = hari ke-5 tanpa bar → DATA_BERAKHIR di 1.004 × (1 − 50 %) = 502 (tanpa slippage):
    # hasil = 50.200 − 125,5 = 50.074,5; P&L = 50.074,5 − 100.250,15 = −50.175,65.
    cfg = cfg_uji({"backtest.maks_hari_tanpa_bar": 5})
    pp, od = pb_dasar((1000, 1010, 990, 1004), *([kosong] * 5), cfg=cfg)
    j = jalankan(pp, od, E0, E0 + 6)
    cek_keluar(j.keluar, tanggal=tgl(E0 + 6), alasan=ex.DATA_BERAKHIR, harga="502")
    assert j.keluar.pnl_idr == D("-50175.65")
    assert j.keluar.slippage_idr == 0
    # Sehari sebelumnya (4 hari tanpa bar) posisi masih terbuka.
    j5 = jalankan(pp, od, E0, E0 + 5)
    assert j5.keluar is None and j5.pos.terbuka


def test_data_berakhir_memakai_potongan_dari_config():
    # potongan 30 % → 1.004 × 0,7 = 702,8.
    cfg = cfg_uji({"backtest.maks_hari_tanpa_bar": 5, "backtest.potongan_data_berakhir_pct": 30})
    pp, od = pb_dasar((1000, 1010, 990, 1004), *([None] * 5), cfg=cfg)
    cek_keluar(jalankan(pp, od, E0, E0 + 6).keluar, tanggal=tgl(E0 + 6), alasan=ex.DATA_BERAKHIR, harga="702.8")


def test_data_berakhir_default_60_hari_bursa():
    # Default rilis 60: hari ke-59 tanpa bar masih terbuka, hari ke-60 ditutup.
    assert CFG.backtest.maks_hari_tanpa_bar == 60
    pp, od = pb_dasar((1000, 1010, 990, 1004), *([None] * 60))
    j59 = jalankan(pp, od, E0, E0 + 60)
    assert j59.keluar is None
    j60 = jalankan(pp, od, E0, E0 + 61)
    cek_keluar(j60.keluar, tanggal=tgl(E0 + 61), alasan=ex.DATA_BERAKHIR, harga="502")


def test_data_berakhir_as_of_dan_tidak_dibatalkan_bila_bar_muncul_lagi():
    # Seri muncul lagi pada E+7 (dan bar sesudahnya). Keputusan pada E+6 sama persis dengan data yang
    # berhenti, dan posisi tidak dibuka kembali (P3: tidak melihat masa depan).
    cfg = cfg_uji({"backtest.maks_hari_tanpa_bar": 5})
    pp_henti, od = pb_dasar((1000, 1010, 990, 1004), *([None] * 5), cfg=cfg)
    pp_lagi, _ = pb_dasar((1000, 1010, 990, 1004), *([None] * 5), (1200, 1300, 1150, 1250), NORMAL, cfg=cfg)
    a = jalankan(pp_henti, od, E0, E0 + 6).keluar
    b = jalankan(pp_lagi, od, E0, E0 + 8).keluar
    assert a == b
    assert b.tanggal == tgl(E0 + 6) and b.alasan == ex.DATA_BERAKHIR


def test_data_berakhir_saat_jual_terjadwal_tertunda():
    # MR: KELUAR_SMA terjadwal setelah close E+1 (close 1.010), lalu 5 hari tanpa bar (maks 5). Pada E+6 posisi
    # ditutup DATA_BERAKHIR di 1.010 × (1 − 50 %) = 505 (bukan KELUAR_SMA), tanpa slippage; tanda
    # TERTUNDA_TANPA_TRANSAKSI tetap tercatat.
    cfg = cfg_uji({"backtest.maks_hari_tanpa_bar": 5})
    pp = buat_pasar({KODE: MR_JADWAL + [None] * 5}, cfg)
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 6)
    k = j.keluar
    assert k.tanggal == tgl(MR_E + 6) and k.alasan == ex.DATA_BERAKHIR and k.harga == D("505")
    assert k.pnl_idr == hasil_jual(100, "505") - biaya_beli(100, "965.965")
    assert ex.TERTUNDA_TANPA_TRANSAKSI in j.pos.tanda


def test_data_berakhir_lewat_simulasikan_tidak_dibatalkan():
    cfg = cfg_uji({"backtest.maks_hari_tanpa_bar": 5})
    bar_a = [*AWAL, NORMAL, (1000, 1010, 990, 1004), *([None] * 5), (1200, 1300, 1150, 1250), NORMAL]
    pp = buat_pasar({KODE: bar_a}, cfg)
    h = sim(pp, {S0: [sinyal(KODE)]}, I0, I0 + len(bar_a) - 1)
    (p,) = h.transaksi
    assert p.keluar.alasan == ex.DATA_BERAKHIR and p.keluar.tanggal == tgl(E0 + 6)
    assert p.keluar.harga == D("502")


# =====================================================================================================
# Prioritas label jual terjadwal: PAKSA_JUAL_PASAR > KELUAR_SMA > STOP_WAKTU (§15.1 langkah 7)
# =====================================================================================================

# MR: hari ke-1..4 close turun (selalu < SMA5); hari ke-5 close 990 > SMA5 = (955+950+945+940+990)/5 = 956.
MR_HARI5_SMA = MR_AWAL + [_b(955), _b(950), _b(945), _b(940), (945, 995, 940, 990), (992, 1000, 985, 995)]
MR_HARI5_TANPA_SMA = MR_AWAL + [_b(955), _b(950), _b(945), _b(940), _b(935), (936, 940, 930, 935)]


@pytest.mark.parametrize(
    ("bar", "paksa", "status", "harapan"),
    [
        pytest.param(MR_HARI5_SMA, True, JANGAN_BELI, ex.PAKSA_JUAL_PASAR, id="paksa>sma>waktu"),
        pytest.param(MR_HARI5_SMA, True, BOLEH_BELI, ex.KELUAR_SMA, id="sma>waktu"),
        pytest.param(MR_HARI5_SMA, False, JANGAN_BELI, ex.KELUAR_SMA, id="paksa-nonaktif"),
        pytest.param(MR_HARI5_TANPA_SMA, True, JANGAN_BELI, ex.PAKSA_JUAL_PASAR, id="paksa>waktu"),
        pytest.param(MR_HARI5_TANPA_SMA, False, JANGAN_BELI, ex.STOP_WAKTU, id="waktu-saja"),
    ],
)
def test_prioritas_label_jual_terjadwal(bar, paksa, status, harapan):
    cfg = cfg_uji({"kondisi_pasar.jangan_beli_paksa_jual": paksa})
    hari5 = tgl(MR_E + 4)

    def status_pasar(d: date) -> str:
        return status if d == hari5 else BOLEH_BELI

    pp = buat_pasar({KODE: bar}, cfg)
    j = jalankan(pp, _order_mr(), MR_E, MR_E + 5, status=status_pasar)
    assert [j.jadwal[tgl(MR_E + k)] for k in range(4)] == [None] * 4
    assert j.jadwal[hari5] == harapan
    k = j.keluar
    assert k.tanggal == tgl(MR_E + 5) and k.alasan == harapan
    assert k.harga == D(str(bar[-1][0])) * (1 - SLIP)


def test_paksa_jual_pasar_hanya_saat_jangan_beli():
    cfg = cfg_uji({"kondisi_pasar.jangan_beli_paksa_jual": True})
    pp, od = pb_dasar(NORMAL, NORMAL, cfg=cfg)
    j = jalankan(pp, od, E0, E0 + 2, status=lambda d: JANGAN_BELI if d == tgl(E0 + 1) else BOLEH_BELI)
    assert j.jadwal[tgl(E0)] is None
    assert j.jadwal[tgl(E0 + 1)] == ex.PAKSA_JUAL_PASAR
    cek_keluar(j.keluar, tanggal=tgl(E0 + 2), alasan=ex.PAKSA_JUAL_PASAR, harga="999")


# =====================================================================================================
# T-SP bagian mesin: posisi melewati split 1:5 (§8.4)
# =====================================================================================================
# Split 1:5 (rasio 5) pada X = E+2. Cache disesuaikan: harga sebelum X = nominal ÷ 5.
# Nominal: S 1.000, E open 1.000 (isi 1.001), E+1 close 1.010; X (pasca-split) close 202 (= 1.010 ÷ 5);
# X+1 low 190 ≤ stop 950 ÷ 5 = 190 → STOP di 190 × 0,999 = 189,81 untuk 500 lembar.
#   biaya beli = 500 × 200,2 = 100.100 + 150,15 = 100.250,15 (= 100 lembar × 1.001 nominal)
#   hasil jual = 500 × 189,81 = 94.905 − 237,2625 = 94.667,7375 (= 100 × 949,05 nominal)
#   P&L = −5.582,4125 — sama dengan skenario tanpa split (test_stop_intraday_di_stop_dikurangi_slippage).
SPLIT_X = E0 + 2
SPLIT = {KODE: pd.Series([5.0], index=pd.DatetimeIndex([HARI[SPLIT_X]]))}
NOMINAL_SPLIT = [*AWAL, NORMAL, (1010, 1020, 1000, 1010), (202, 204, 200, 202), (200, 201, 190, 195)]
NOMINAL_TANPA_SPLIT = [*AWAL, NORMAL, (1010, 1020, 1000, 1010), (1010, 1020, 1000, 1010),
                       (1000, 1005, 950, 975)]


def _ke_cache(bar: list[Bar], i_split: int, rasio: float) -> list[Bar]:
    return [tuple(x / rasio for x in b[:4]) + tuple(b[4:]) if i < i_split else b for i, b in enumerate(bar)]


def test_split_dalam_masa_tahan_pnl_rupiah_sama_dengan_hitung_tangan_nominal():
    cache = _ke_cache(NOMINAL_SPLIT, SPLIT_X - I0, 5.0)
    pp = buat_pasar({KODE: cache}, split=SPLIT)
    od = order(i_sinyal=S0, faktor="5")  # nominal pada tanggal sinyal, F(S) = 5
    j = jalankan(pp, od, E0, E0 + 3)
    p, k = j.pos, j.keluar
    assert p.lembar == 500  # lembar_adj = 100 × F(E) = 500
    assert p.harga_beli == D("200.2")  # nominal 1.001 ÷ 5
    assert p.biaya_beli_idr == BIAYA_DASAR
    assert p.stop_awal == D("190") and p.target == D("1103") / 5
    assert p.risiko_awal_idr == RISIKO_DASAR
    # Nilai posisi tidak melompat saat split: close E+1 (adj 202 = nominal 1.010 × 100) dan close X
    # (nominal 202 × 500) sama-sama 101.000.
    assert j.nilai[tgl(E0 + 1)] == j.nilai[tgl(SPLIT_X)] == D("101000")
    assert k.tanggal == tgl(SPLIT_X + 1) and k.alasan == ex.STOP
    assert k.harga == D("189.81") and k.harga_nominal == D("189.81")
    assert k.nilai_idr == D("94905")
    assert k.hasil_jual_idr == D("94667.7375")
    assert k.pnl_idr == D("-5582.4125")
    # Sama persis dengan skenario tanpa split dalam Rupiah dan R.
    pp0 = buat_pasar({KODE: NOMINAL_TANPA_SPLIT})
    k0 = jalankan(pp0, order(i_sinyal=S0), E0, E0 + 3).keluar
    assert k0.alasan == ex.STOP
    assert (k.nilai_idr, k.hasil_jual_idr, k.pnl_idr, k.r) == (k0.nilai_idr, k0.hasil_jual_idr, k0.pnl_idr, k0.r)


def test_split_sebelum_entry_harga_maks_nominal_dibandingkan_di_ruang_adj():
    # Sinyal pada S (F = 5), entry pada E = hari split (F(E) = 1): open nominal pasca-split 210 = 1.050 ÷ 5
    # → tidak gap (210 ≤ 210), harga isi = min(210,21; 210) = 210; 500 lembar.
    x = E0
    split = {KODE: pd.Series([5.0], index=pd.DatetimeIndex([HARI[x]]))}
    cache = _ke_cache([*AWAL, (210, 212, 205, 210)], x - I0, 5.0)
    pp = buat_pasar({KODE: cache}, split=split)
    j = jalankan(pp, order(i_sinyal=S0, faktor="5"), E0, E0)
    assert j.status_isi == ex.TERISI
    assert j.pos.lembar == 500 and j.pos.harga_beli == D("210")
    assert j.pos.biaya_beli_idr == D("105157.5")  # sama dengan 100 lembar × 1.050 nominal
    cache_gap = _ke_cache([*AWAL, (211, 212, 205, 210)], x - I0, 5.0)
    pp_gap = buat_pasar({KODE: cache_gap}, split=split)
    assert jalankan(pp_gap, order(i_sinyal=S0, faktor="5"), E0, E0).status_isi == ex.BATAL_GAP


def test_split_lewat_simulasikan_ekuitas_dan_pnl_sama_dengan_tanpa_split():
    cache = _ke_cache(NOMINAL_SPLIT, SPLIT_X - I0, 5.0)
    pp = buat_pasar({KODE: cache}, split=SPLIT)
    pp0 = buat_pasar({KODE: NOMINAL_TANPA_SPLIT})
    akhir = I0 + len(NOMINAL_SPLIT) - 1
    h = sim(pp, {S0: [sinyal(KODE, faktor_split=5.0)]}, I0, akhir, mode="realistis")
    h0 = sim(pp0, {S0: [sinyal(KODE)]}, I0, akhir, mode="realistis")
    (p,), (p0,) = h.transaksi, h0.transaksi
    assert p.lembar == p0.lembar * 5  # sizing nominal identik (lot sama), lembar_adj × 5
    assert p.keluar.alasan == p0.keluar.alasan == ex.STOP
    assert p.keluar.pnl_idr == p0.keluar.pnl_idr
    assert [r.ekuitas for r in h.ekuitas] == [r.ekuitas for r in h0.ekuitas]
    assert h.kas_akhir == h0.kas_akhir


# =====================================================================================================
# Pembatalan yang butuh slot/sektor/kas: lewat backtest.mesin.simulasikan (§16.2 langkah 2)
# =====================================================================================================

RN = 20e9  # RataNilai Rp 20 miliar (likuiditas tidak membatasi)


def sinyal(
    kode: str,
    *,
    i: int = S0,
    strategi: str = "PB",
    harga_maks: str = "1050",
    stop: str = "950",
    faktor_split: float = 1.0,
    skor: float = 50.0,
) -> SinyalBerskor:
    sg = Sinyal(
        tanggal=tgl(i), kode=kode, strategi=strategi, cek=(), harga_ref=D(harga_maks), harga_maks=D(harga_maks),
        stop=D(stop), atr=D(25), faktor_split=faktor_split, k_sinyal=0.5,
        target_r=2.0 if strategi in ("PB", "ACAK") else None, rata_nilai=RN, fraksi_dipakai=False,
    )
    return SinyalBerskor(sg, KomponenSkor(0.5, 0.5, 0.5, 0.5, skor))


def keputusan(i: int, sinyal_hari: Sequence[SinyalBerskor], status: str = BOLEH_BELI) -> KeputusanHari:
    d = tgl(i)
    kondisi = HasilKondisiPasar(d, status, "K3", "uji (fiktif)", (), RingkasanBreadth(0, 0, 0), None, None, None, ())
    return KeputusanHari(d, kondisi, HasilEligibilitas(d, {}), tuple(sinyal_hari))


SEKTOR = {KODE: SEKTOR_1, KODE_B: SEKTOR_2}


def sim(
    pp,
    per_hari: dict[int, list[SinyalBerskor]],
    i_awal: int,
    i_akhir: int,
    *,
    mode: str = "edge",
    sektor: dict[str, str] | None = None,
    strategi: str = "PB",
) -> HasilSimulasi:
    sek = sektor or SEKTOR
    kh = [keputusan(i, per_hari.get(i, [])) for i in range(i_awal, i_akhir + 1)]
    return simulasikan(
        kh, pp, SpesifikasiRun.per_strategi(strategi), mode=mode, konteks=resolusi_konteks(pp.cfg, mode),
        sektor_dari=lambda k: sek[k], tanggal_awal=tgl(i_awal - 1),
    )


def dilewati(h: HasilSimulasi, kode: str) -> list:
    return [x for x in h.dilewati if x.kode == kode]


# Skenario jual terjadwal yang tertunda: A (ZXQA) sinyal S0, entry E0 (hari ke-1); stop waktu PB = 2 →
# STOP_WAKTU dijadwalkan pada close E0+1 (hari ke-2). Pada close yang sama ada sinyal B (ZXQB), dan seleksi
# membebaskan slot/sektor A (slot_bebas_dari_jual_terjadwal). Pada open E0+2 jual A tertunda (A tanpa bar
# valid / terkunci ARB) → slot/sektor tetap penuh → order B batal di open. Jual A terjadi di open E0+3.
I_JADWAL = E0 + 1
I_OPEN = E0 + 2


def _penunda(jenis: str) -> Bar | None:
    return {"hilang": None, "volume0": (1000, 1010, 990, 1000, 0), "arb": ARB_TERKUNCI}[jenis]


def _data_tunda(jenis: str | None, b_open: Bar = NORMAL) -> dict[str, list]:
    a = [*AWAL, NORMAL, NORMAL, NORMAL if jenis is None else _penunda(jenis), NORMAL]
    b = [*AWAL, NORMAL, NORMAL, b_open, NORMAL]
    return {KODE: a, KODE_B: b}


PENUNDA = [
    pytest.param("hilang", ex.TERTUNDA_TANPA_TRANSAKSI, id="bar-hilang"),
    pytest.param("volume0", ex.TERTUNDA_TANPA_TRANSAKSI, id="tanpa-transaksi"),
    pytest.param("arb", ex.TERTUNDA_ARB, id="terkunci-arb-fiktif"),
]


def _cfg_tunda(**ubah) -> Config:
    return cfg_dengan(ubah_ar(), {"strategi.pb.stop_waktu_hari": 2, **ubah})


def _cek_a_tertunda(h: HasilSimulasi, tanda: str) -> None:
    pa = next(p for p in h.transaksi if p.kode == KODE)
    assert pa.keluar.alasan == ex.STOP_WAKTU and pa.keluar.tanggal == tgl(I_OPEN + 1)
    assert pa.keluar.harga == D("999")  # 1.000 × 0,999
    assert tanda in pa.tanda


@pytest.mark.parametrize(("jenis", "tanda"), PENUNDA)
def test_batal_slot_saat_open_karena_jual_terjadwal_tertunda(jenis, tanda):
    cfg = _cfg_tunda(**{"risiko.maks_posisi": 1})
    pp = buat_pasar(_data_tunda(jenis), cfg)
    h = sim(pp, {S0: [sinyal(KODE)], I_JADWAL: [sinyal(KODE_B, i=I_JADWAL)]}, I0, I_OPEN + 1)
    (b,) = dilewati(h, KODE_B)
    assert (b.status_sinyal, b.status_isi, b.tanggal, b.tanggal_isi) == (
        REKOMENDASI, BATAL_SLOT_SAAT_OPEN, tgl(I_JADWAL), tgl(I_OPEN))
    assert [p.kode for p in h.transaksi] == [KODE]
    _cek_a_tertunda(h, tanda)


def test_slot_kontrol_jual_terisi_order_terisi():
    cfg = _cfg_tunda(**{"risiko.maks_posisi": 1})
    pp = buat_pasar(_data_tunda(None), cfg)
    h = sim(pp, {S0: [sinyal(KODE)], I_JADWAL: [sinyal(KODE_B, i=I_JADWAL)]}, I0, I_OPEN + 1)
    assert not dilewati(h, KODE_B)
    pb = next(p for p in h.transaksi if p.kode == KODE_B)
    assert pb.tanggal_entry == tgl(I_OPEN)
    pa = next(p for p in h.transaksi if p.kode == KODE)
    assert pa.keluar.tanggal == tgl(I_OPEN) and pa.keluar.alasan == ex.STOP_WAKTU


@pytest.mark.parametrize(("jenis", "tanda"), PENUNDA)
def test_batal_sektor_saat_open_karena_jual_terjadwal_tertunda(jenis, tanda):
    # maks_posisi 3 (slot cukup), maks_per_sektor 1, A dan B satu sektor.
    cfg = _cfg_tunda()
    pp = buat_pasar(_data_tunda(jenis), cfg)
    sektor = {KODE: SEKTOR_1, KODE_B: SEKTOR_1}
    h = sim(pp, {S0: [sinyal(KODE)], I_JADWAL: [sinyal(KODE_B, i=I_JADWAL)]}, I0, I_OPEN + 1, sektor=sektor)
    (b,) = dilewati(h, KODE_B)
    assert (b.status_sinyal, b.status_isi, b.tanggal_isi) == (REKOMENDASI, BATAL_SEKTOR_SAAT_OPEN, tgl(I_OPEN))
    _cek_a_tertunda(h, tanda)


def test_sektor_kontrol_jual_terisi_order_terisi():
    cfg = _cfg_tunda()
    pp = buat_pasar(_data_tunda(None), cfg)
    sektor = {KODE: SEKTOR_1, KODE_B: SEKTOR_1}
    h = sim(pp, {S0: [sinyal(KODE)], I_JADWAL: [sinyal(KODE_B, i=I_JADWAL)]}, I0, I_OPEN + 1, sektor=sektor)
    assert not dilewati(h, KODE_B)
    assert {p.kode for p in h.transaksi} == {KODE, KODE_B}


@pytest.mark.parametrize(
    ("b_open", "status"),
    [
        pytest.param(None, ex.BATAL_DATA, id="data-sebelum-slot"),
        pytest.param((1060, 1070, 1050, 1060), ex.BATAL_GAP, id="gap-sebelum-slot"),
    ],
)
def test_pembatalan_data_dan_gap_didahulukan_dari_slot(b_open, status):
    cfg = _cfg_tunda(**{"risiko.maks_posisi": 1})
    pp = buat_pasar(_data_tunda("hilang", b_open), cfg)
    h = sim(pp, {S0: [sinyal(KODE)], I_JADWAL: [sinyal(KODE_B, i=I_JADWAL)]}, I0, I_OPEN + 1)
    (b,) = dilewati(h, KODE_B)
    assert b.status_isi == status


# --- BATAL_KAS_SAAT_OPEN ------------------------------------------------------------------------------
# dana_jual_langsung_dipakai: true; modal Rp 260.000 (mode edge), alokasi 100 % ekuitas, maks_posisi 3.
#   A: harga_maks 1.000, stop 990 → risiko/lembar = 1.001,5 − 986,537475 = 14,962525 → 1 lot (anggaran 2.600).
#      Isi di open 1.000 → biaya 100.150 → kas 159.850.
#   Close E0+1: ekuitas = 159.850 + 100 × 1.000 = 259.850 → anggaran 2.598,5.
#   B: harga_maks 2.000, stop 1.985 → risiko/lembar = 2.003 − 1.978,0574625 = 24,9425375 → 1 lot (2.494,25).
#      biaya 1 lot = 200.300 > kas 159.850, tetapi kas tersedia = 159.850 + estimasi jual A di stop
#      (100 × 990 × 0,999 × 0,9975 = 98.653,7475) = 258.503,7475 → REKOMENDASI 1 lot.
#   Open E0+2: jual A tertunda → kas tetap 159.850 < biaya B (isi 1.991,99 → 199.497,7985) → BATAL_KAS.
def _cfg_kas() -> Config:
    return _cfg_tunda(**{
        "risiko.dana_jual_langsung_dipakai": True,
        "risiko.alokasi_maks_pct": 100,
        "backtest.mode.edge.modal_idr": 260000,
    })


def _sinyal_kas() -> dict[int, list[SinyalBerskor]]:
    return {
        S0: [sinyal(KODE, harga_maks="1000", stop="990")],
        I_JADWAL: [sinyal(KODE_B, i=I_JADWAL, harga_maks="2000", stop="1985")],
    }


KETAT = (1000, 1004, 996, 1000)  # low 996 > stop A 990


def _data_kas(jenis: str | None) -> dict[str, list]:
    b = [*AWAL, NORMAL, NORMAL, (1990, 2000, 1988, 1995), (1995, 2000, 1990, 1995)]
    a = [*AWAL, KETAT, KETAT, KETAT if jenis is None else _penunda(jenis), KETAT]
    return {KODE: a, KODE_B: b}


@pytest.mark.parametrize(("jenis", "tanda"), PENUNDA)
def test_batal_kas_saat_open_karena_jual_terjadwal_tertunda(jenis, tanda):
    pp = buat_pasar(_data_kas(jenis), _cfg_kas())
    h = sim(pp, _sinyal_kas(), I0, I_OPEN + 1)
    (b,) = dilewati(h, KODE_B)
    assert (b.status_sinyal, b.status_isi, b.tanggal_isi) == (REKOMENDASI, BATAL_KAS_SAAT_OPEN, tgl(I_OPEN))
    pa = next(p for p in h.transaksi if p.kode == KODE)
    assert pa.biaya_beli_idr == D("100150")
    assert tanda in pa.tanda
    assert all(r.kas >= 0 for r in h.ekuitas)


def test_kas_kontrol_hasil_jual_open_membiayai_beli():
    # Jual A terisi di open E0+2: 999 × 100 = 99.900 − 249,75 → kas 259.500,25 ≥ biaya B 199.497,7985.
    pp = buat_pasar(_data_kas(None), _cfg_kas())
    h = sim(pp, _sinyal_kas(), I0, I_OPEN + 1)
    assert not dilewati(h, KODE_B)
    pb = next(p for p in h.transaksi if p.kode == KODE_B)
    assert pb.tanggal_entry == tgl(I_OPEN)
    assert pb.harga_beli == D("1991.99")  # min(1.990 × 1,001; 2.000)
    assert pb.biaya_beli_idr == D("199497.7985")


# --- Lewat simulasikan: pembatalan data/gap/ARA dicatat, open = harga_maks dan jaminan risiko ---------


@pytest.mark.parametrize(
    ("cfg", "bar_entry", "status"),
    [
        pytest.param(CFG, None, ex.BATAL_DATA, id="bar-tidak-ada"),
        pytest.param(CFG, (1000, 1010, 990, 1000, 0), ex.BATAL_DATA, id="volume-0"),
        pytest.param(CFG, (1000, 990, 1010, 1000), ex.BATAL_DATA, id="rusak"),
        pytest.param(CFG, (1051, 1060, 1040, 1050), ex.BATAL_GAP, id="gap"),
        pytest.param(CFG_AR, (1110, 1110, 1110, 1110), ex.BATAL_ARA, id="ara-fiktif"),
    ],
)
def test_simulasikan_mencatat_pembatalan_di_open(cfg, bar_entry, status):
    pp = buat_pasar({KODE: [*AWAL, bar_entry, NORMAL]}, cfg)
    h = sim(pp, {S0: [sinyal(KODE, harga_maks="1120" if status == ex.BATAL_ARA else "1050")]}, I0, E0 + 1)
    (x,) = dilewati(h, KODE)
    assert (x.status_sinyal, x.status_isi, x.tanggal, x.tanggal_isi) == (REKOMENDASI, status, tgl(S0), tgl(E0))
    assert h.transaksi == []
    assert h.kas_akhir == h.kas_awal


def test_open_sama_dengan_harga_maks_risiko_rencana_dalam_anggaran_dan_stop_tepat_minus_1r():
    # Mode realistis: ekuitas Rp 3.000.000 → anggaran 30.000; risiko/lot = 10.489,7625 → lot_risiko 2.
    # Isi di open 1.050 = harga_maks → biaya = 210.000 + 315 = 210.315. risiko rencana = 20.979,525 ≤ 30.000.
    # Stop intraday hari berikutnya: 200 × 949,05 = 189.810 − 474,525 = 189.335,475 → P&L = −20.979,525
    # = tepat −1R (§14.1 "Jaminan": open ≤ harga_maks → kerugian di stop ≤ anggaran).
    pp = buat_pasar({KODE: [*AWAL, (1050, 1060, 1040, 1050), (1000, 1005, 940, 960)]})
    h = sim(pp, {S0: [sinyal(KODE)]}, I0, E0 + 1, mode="realistis")
    (p,) = h.transaksi
    assert p.lembar == 200 and p.harga_beli == D("1050")
    assert p.risiko_awal_idr == D("20979.525") <= D("30000")
    assert p.keluar.alasan == ex.STOP and p.keluar.harga == D("949.05")
    assert p.keluar.pnl_idr == -p.risiko_awal_idr
    assert p.keluar.r == -1


@pytest.mark.parametrize(
    ("bar", "s", "e", "sg", "n"),
    [
        pytest.param([*AWAL, NORMAL, (990, 995, 940, 960)], S0, E0, sinyal(KODE), 1, id="pb-stop"),
        pytest.param(BO_BAR, BO_S, BO_E, sinyal(KODE, i=BO_S, strategi="BO", harga_maks="1047", stop="985"), 5,
                     id="bo-trailing"),
        pytest.param(MR_AWAL + [(965, 1005, 960, 1000), (1000, 1015, 995, 1010), (1012, 1020, 1005, 1015)],
                     MR_S, MR_E, sinyal(KODE, i=MR_S, strategi="MR", harga_maks="970", stop="900"), 2,
                     id="mr-keluar-sma"),
    ],
)
def test_simulasikan_sama_dengan_pemutar_exit(bar, s, e, sg, n):
    # Pemutar `jalankan` di berkas ini harus sama dengan mesin backtest untuk satu posisi (alasan, tanggal,
    # harga isi jual, dan R — R tidak bergantung jumlah lembar karena fee minimum 0).
    cfg = CFG_ATR3 if sg.strategi == "BO" else CFG
    pp = buat_pasar({KODE: bar}, cfg)
    h = sim(pp, {s: [sg]}, I0, I0 + len(bar) - 1, strategi=sg.strategi)
    (p,) = h.transaksi
    od = order(i_sinyal=s, strategi=sg.strategi, harga_maks=str(sg.sinyal.harga_maks), stop=str(sg.sinyal.stop),
               target_r=sg.sinyal.target_r)
    k = jalankan(pp, od, e, e + n).keluar
    assert (p.keluar.alasan, p.keluar.tanggal, p.keluar.harga) == (k.alasan, k.tanggal, k.harga)
    assert p.keluar.r == k.r
