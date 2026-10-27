"""Model laporan harian (SPEC §18.1): semua isi laporan dihitung sekali di sini, lalu dirender apa adanya oleh
`terminal`, `markdown`, `html`, dan `tabel`. Angka untuk dibaca pengguna memakai `teks` (gaya Indonesia).

Bagian berurutan (§18.1): kepala & banner, ringkasan aksi (maks 8 baris), status pasar (tabel K0–K4), status
strategi, akun, posisi dipegang (§15.4), rencana beli (§14.3), kandidat lain, daftar pantau, kualitas data,
item belum diverifikasi, jurnal kertas, penutup (disclaimer §18.4, keterbatasan singkat, glosarium).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .. import DISCLAIMER, biaya, teks
from ..aturan_bei import AturanBEI
from ..kondisi_pasar import JANGAN_BELI, TAMPILAN_STATUS, HasilKondisiPasar
from ..seleksi import BAYANGAN_PASAR, REKOMENDASI, KandidatSeleksi
from ..skor import CATATAN_SKOR
from ..strategi.dasar import hitung_target

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..jurnal import RingkasanJurnal
    from ..portofolio import HasilPortofolio, SaranPosisi

MAKS_RINGKASAN_AKSI = 8

TEKS_TARIK = "tarik jika tidak terisi saat pembukaan"
TEKS_BULATKAN = "bulatkan limit beli dan target ke BAWAH, stop ke ATAS ke fraksi harga valid"
BANNER_BELUM_TERSEDIA = "DATA BELUM TERSEDIA"
BANNER_TIDAK_LENGKAP = "DATA TIDAK LENGKAP"

KETERBATASAN_SINGKAT: tuple[str, ...] = (
    "Data publik (yfinance/CSV) bisa bolong, salah, terlambat, atau direvisi; dividen dan sebagian aksi korporasi "
    "tidak disesuaikan.",
    "Rencana mengasumsikan order beli terisi di pembukaan (atau ditarik bila tidak) dan order otomatis stop terisi "
    "di level stop dikurangi slippage; gap turun bisa membuat kerugian melebihi anggaran risiko.",
    "Skor adalah heuristik pemeringkat, bukan peluang keberhasilan. Hasil backtest dan jurnal kertas tidak menjamin "
    "hasil di masa depan.",
    "Aturan BEI (fraksi, ARA/ARB, jadwal sesi) hanya dipakai setelah Anda verifikasi sendiri dari idx.co.id.",
)

GLOSARIUM: tuple[tuple[str, str], ...] = (
    ("R", "Kerugian yang direncanakan bila stop tersentuh. +2R berarti untung dua kali risiko awal; −1R berarti rugi sebesar rencana."),
    ("ATR", "Rata-rata rentang gerak harian saham. Dipakai untuk menentukan jarak stop."),
    ("SMA20/50/200", "Rata-rata harga penutupan 20/50/200 hari bursa terakhir."),
    ("RSI(2)", "Indikator jenuh jual/beli jangka sangat pendek (0–100). Nilai di bawah 10 berarti sangat jenuh jual."),
    ("Breadth", "Persentase saham dalam daftar breadth yang harganya di atas SMA50. Menggambarkan seberapa luas pasar naik."),
    ("Drawdown", "Penurunan dari nilai puncak ekuitas ke titik terendah setelahnya."),
    ("CAGR", "Pertumbuhan tahunan rata-rata yang dimajemukkan."),
    ("Win rate", "Persentase transaksi yang untung setelah fee."),
    ("Profit factor (PF)", "Total untung ÷ total rugi. Di atas 1 berarti untung bersih. PF_R menghitungnya dalam satuan R."),
    ("Ekspektansi", "Rata-rata hasil per transaksi, dalam Rupiah atau R."),
    ("In-sample / holdout", "Periode yang dipakai untuk menilai aturan, dan periode terakhir yang disimpan untuk pengujian akhir."),
    ("Slippage", "Selisih antara harga yang diharapkan dan harga eksekusi sebenarnya."),
    ("Fraksi harga", "Kelipatan harga yang diizinkan bursa. Nilainya Anda isi sendiri dari aturan resmi."),
    ("ARA/ARB", "Batas atas/bawah perubahan harga harian. Nilainya Anda isi sendiri dari aturan resmi."),
    ("Gerbang", "Syarat backtest yang harus dipenuhi sebelum strategi boleh menghasilkan rekomendasi beli."),
    ("Bayangan", "Sinyal yang dicatat untuk evaluasi tetapi tidak direkomendasikan (strategi belum lolos gerbang atau pasar JANGAN BELI)."),
    ("Jurnal kertas", "Simulasi transaksi tanpa uang sungguhan (paper trading), memakai aturan yang sama dengan backtest."),
    ("Stop waktu", "Jual bila posisi belum keluar setelah N hari bursa."),
    ("Trailing stop", "Stop yang naik mengikuti harga dan tidak pernah turun."),
)


def h(x: Decimal | float | int | None) -> str:
    """Harga untuk dibaca: bulat tanpa desimal, selain itu 2 desimal."""
    if x is None:
        return teks.STRIP
    d = biaya.ke_decimal(x)
    return teks.angka(d, 0 if d == d.to_integral_value() else 2)


def rp(x: Decimal | float | int | None) -> str:
    return teks.STRIP if x is None else teks.rupiah(x)


def pct(bagian: Decimal | None, total: Decimal | None, desimal: int = 2) -> str:
    if bagian is None or total is None or total <= 0:
        return teks.STRIP
    return teks.persen(Decimal(bagian) * 100 / Decimal(total), desimal)


# ---------------------------------------------------------------------------
# Bagian
# ---------------------------------------------------------------------------


@dataclass
class StatusStrategi:
    kode: str
    aktif: bool
    lolos: bool
    alasan: str  # "" bila lolos
    tanggal_backtest: str  # tanggal data terakhir backtest gerbang
    pf_r_holdout: str
    subset_lemah: bool


@dataclass
class RencanaBeli:
    """Isi rencana §14.3 untuk satu `REKOMENDASI`."""

    kandidat: KandidatSeleksi
    nama: str
    sektor: str
    alasan_sinyal: list[str]
    harga_ref: Decimal
    harga_maks: Decimal
    stop: Decimal
    metode_stop: str
    jarak_stop_pct: Decimal
    target: list[tuple[str, Decimal, Decimal]]  # (label, harga isi, target) — PB
    teks_target: str
    lot: int
    lembar: int
    nilai_idr: Decimal
    fee_beli_idr: Decimal
    biaya_beli_idr: Decimal
    risiko_idr: Decimal
    risiko_pct: str
    sisa_anggaran_per_lembar: Decimal | None
    rr_bersih: Decimal | None
    instruksi_masuk: str
    ketergantungan: list[str]
    langkah_setelah: str
    peringatan: list[str]

    @property
    def kode(self) -> str:
        return self.kandidat.kode

    @property
    def strategi(self) -> str:
        return self.kandidat.strategi

    @property
    def skor(self) -> str:
        return self.kandidat.sinyal.komponen.tampilan

    @property
    def tampilan_target(self) -> str:
        if self.target:
            return h(self.target[-1][2])
        return self.teks_target


@dataclass
class Kualitas:
    status_ambil: dict[str, int]
    basi: int
    anomali: int
    sumber_manual: int
    dikeluarkan: list[tuple[str, str, str]]  # (kode, alasan enum, rincian)
    dikeluarkan_total: int
    revisi: list[str]
    catatan: list[str]


@dataclass
class LaporanHarian:
    cfg: "Config"
    t: date
    t1: date | None
    dibuat_pada: datetime
    hash8: str
    versi_program: str
    status_data_run: str
    banner: list[str] = field(default_factory=list)
    peringatan: list[str] = field(default_factory=list)
    ringkasan_aksi: list[str] = field(default_factory=list)
    kondisi: HasilKondisiPasar | None = None
    status_strategi: list[StatusStrategi] = field(default_factory=list)
    portofolio: "HasilPortofolio | None" = None
    anggaran_risiko: Decimal | None = None
    alokasi_maks: Decimal | None = None
    slot_kosong: int | None = None
    akun_diperbarui: date | None = None
    rencana: list[RencanaBeli] = field(default_factory=list)
    kandidat: list[KandidatSeleksi] = field(default_factory=list)  # semua sinyal t, urutan seleksi
    nama: dict[str, str] = field(default_factory=dict)
    sektor: dict[str, str] = field(default_factory=dict)
    kualitas: Kualitas | None = None
    belum_verifikasi: list[str] = field(default_factory=list)
    jurnal: "RingkasanJurnal | None" = None
    jurnal_peringatan: str | None = None
    replay: bool = False
    catatan: list[str] = field(default_factory=list)
    grafik: dict[str, Any] = field(default_factory=dict)  # data grafik (html)

    # -- turunan ----------------------------------------------------------------------

    @property
    def judul(self) -> str:
        return f"Laporan harian {self.t.isoformat()}"

    @property
    def status_pasar(self) -> str | None:
        return None if self.kondisi is None else self.kondisi.status

    @property
    def tampilan_status(self) -> str:
        if self.kondisi is None:
            return teks.STRIP
        return TAMPILAN_STATUS.get(self.kondisi.status, self.kondisi.status)

    @property
    def rekomendasi(self) -> list[KandidatSeleksi]:
        return [k for k in self.kandidat if k.status == REKOMENDASI]

    @property
    def kandidat_lain(self) -> list[KandidatSeleksi]:
        lain = [k for k in self.kandidat if k.status not in (REKOMENDASI, BAYANGAN_PASAR)]
        return lain[: int(self.cfg.laporan.maks_kandidat_ditampilkan)]

    @property
    def n_kandidat_lain(self) -> int:
        return len([k for k in self.kandidat if k.status not in (REKOMENDASI, BAYANGAN_PASAR)])

    @property
    def daftar_pantau(self) -> list[KandidatSeleksi]:
        return [k for k in self.kandidat if k.status == BAYANGAN_PASAR]

    @property
    def posisi(self) -> list["SaranPosisi"]:
        return [] if self.portofolio is None else self.portofolio.saran

    @property
    def disclaimer(self) -> str:
        return DISCLAIMER

    @property
    def catatan_skor(self) -> str:
        return CATATAN_SKOR

    def kepala(self) -> list[str]:
        """Baris kepala (§18.1 #1) tanpa banner."""
        untuk = self.t1.isoformat() if self.t1 else teks.STRIP
        return [
            "BUKAN SARAN INVESTASI",
            f"Tanggal data t: {self.t.isoformat()} — untuk hari bursa t+1: {untuk}",
            f"Dibuat: {self.dibuat_pada:%Y-%m-%d %H:%M:%S} WIB — hash8 {self.hash8} — versi {self.versi_program}"
            + (" — REPLAY (--tanggal)" if self.replay else ""),
            f"Status data run: {self.status_data_run}",
        ]

    def akun_baris(self) -> list[tuple[str, str]]:
        p = self.portofolio
        if p is None:
            return []
        approx = "≈ " if p.perkiraan else ""
        baris = [
            ("Kas (akun.yaml)", rp(p.kas_idr) + ("" if p.akun_dari_berkas else " (akun.yaml tidak ada: modal awal)")),
            ("Nilai posisi", approx + rp(p.nilai_posisi_idr)),
            ("Ekuitas", approx + rp(p.ekuitas_idr)),
            ("Anggaran risiko per transaksi", rp(self.anggaran_risiko) + (
                f" ({pct(self.anggaran_risiko, p.ekuitas_idr)} ekuitas, status {self.tampilan_status})"
                if self.anggaran_risiko is not None else "")),
            ("Alokasi maks per posisi", rp(self.alokasi_maks)),
            ("Slot kosong", teks.STRIP if self.slot_kosong is None else str(self.slot_kosong)),
        ]
        if self.akun_diperbarui is not None:
            baris.append(("akun.yaml diperbarui", self.akun_diperbarui.isoformat()))
        return baris


# ---------------------------------------------------------------------------
# Pembangun
# ---------------------------------------------------------------------------


def metode_stop(strategi: str, cfg: "Config") -> str:
    s = cfg.strategi
    if strategi == "PB":
        return (f"LLV{s.pb.lookback_swing_low} − {teks.angka(s.pb.penyangga_stop_atr, 1)} × ATR"
                f"{cfg.indikator.atr_periode}")
    if strategi == "BO":
        return f"close − {teks.angka(s.bo.stop_atr, 1)} × ATR{cfg.indikator.atr_periode}; lalu trailing"
    if strategi == "MR":
        return f"close − {teks.angka(s.mr.stop_atr, 1)} × ATR{cfg.indikator.atr_periode}"
    return strategi


def rencana_beli(
    k: KandidatSeleksi,
    *,
    nama: str,
    sektor: str,
    ekuitas: Decimal,
    cfg: "Config",
    bei: AturanBEI,
    sumber_manual: bool,
) -> RencanaBeli:
    """Rencana §14.3 untuk satu REKOMENDASI (harga nominal t, sudah dibulatkan)."""
    sg = k.sinyal.sinyal
    sz = k.sizing
    assert sz is not None
    P, S = sg.harga_maks, sg.stop
    target: list[tuple[str, Decimal, Decimal]] = []
    teks_target = ""
    rr = None
    if sg.target_r is not None:
        tengah = bei.bulat_bawah((sg.harga_ref + P) / 2, sg.tanggal).nilai
        for label, isi in (("harga_ref", sg.harga_ref), ("tengah", tengah), ("harga_maks", P)):
            if isi > S:
                target.append((label, isi, hitung_target(isi, S, sg.target_r, bei, sg.tanggal)))
        r = teks.angka(sg.target_r, 0 if float(sg.target_r).is_integer() else 1)
        teks_target = f"target = harga isi + {r} × (harga isi − stop), dibulatkan ke bawah"
        if target:
            rr = biaya.rr_bersih(target[-1][2], P, S, cfg)
    elif sg.strategi == "BO":
        teks_target = "tanpa target: trailing stop, diperbarui setiap sore (lihat laporan harian)"
    elif sg.strategi == "MR":
        teks_target = (f"tanpa target: jual di pembukaan setelah close > SMA{cfg.strategi.mr.sma_keluar} "
                       f"(mulai hari ke-{cfg.strategi.mr.min_hari_tahan})")
    instruksi = (
        f"Pasang order BELI limit di {h(P)}, {sz.lot} lot, sebelum pra-pembukaan t+1. Segera setelah harga "
        "pembukaan terbentuk, cek order Anda. Jika belum terisi (open > harga_maks), tarik (withdraw) order; jangan "
        "biarkan antre sepanjang hari, karena bisa terisi belakangan di harga yang tidak diuji backtest. Jika hanya "
        "terisi sebagian, tarik sisanya dan catat lot yang terisi saja."
    )
    ketergantungan = [
        "Jika jual " + " dan ".join(g) + " di pembukaan tidak terisi, tarik order beli ini." for g in k.bergantung_jual
    ]
    order_target = f" (dan target di {h(target[-1][2])})" if target else ""
    kapan = "hari itu juga" if cfg.entry.stop_aktif_hari_entry else "mulai besok (broker tidak mendukung hari entry)"
    langkah = (f"Pasang order otomatis stop di {h(S)}{order_target} {kapan}. Jika harga isi sudah ≤ stop, stop telah "
               "terlewati: jual segera. Perbarui portfolio.csv dan akun.yaml (§19.6).")
    peringatan = []
    if not sg.fraksi_dipakai:
        peringatan.append(f"Harga belum dibulatkan ke fraksi: {TEKS_BULATKAN}.")
    if sumber_manual:
        peringatan.append("Bar t saham ini berasal dari CSV manual.")
    if sz.risiko_di_atas_anggaran:
        peringatan.append("Risiko 1 lot di atas anggaran (izinkan_satu_lot_melebihi_risiko).")
    return RencanaBeli(
        kandidat=k, nama=nama, sektor=sektor, alasan_sinyal=[c.teks() for c in sg.cek],
        harga_ref=sg.harga_ref, harga_maks=P, stop=S, metode_stop=metode_stop(sg.strategi, cfg),
        jarak_stop_pct=(P - S) * 100 / P, target=target, teks_target=teks_target, lot=sz.lot, lembar=sz.lembar,
        nilai_idr=sz.nilai_idr, fee_beli_idr=sz.fee_beli_idr, biaya_beli_idr=sz.biaya_beli_idr,
        risiko_idr=sz.risiko_idr, risiko_pct=pct(sz.risiko_idr, ekuitas),
        sisa_anggaran_per_lembar=sz.sisa_anggaran_per_lembar, rr_bersih=rr, instruksi_masuk=instruksi,
        ketergantungan=ketergantungan, langkah_setelah=langkah, peringatan=peringatan,
    )


def ringkasan_aksi(lap: LaporanHarian) -> list[str]:
    """Maks 8 baris (§18.1 #2): jual/cek order/perbarui stop untuk posisi, lalu beli."""
    from ..portofolio import CEK_ORDER, JUAL, TAHAN, TIDAK_DAPAT_DIEVALUASI

    aksi: list[str] = []
    for s in lap.posisi:
        if s.saran == JUAL:
            aksi.append(f"JUAL {s.kode} di pembukaan ({s.alasan.split('.')[0]})")
        elif s.saran == CEK_ORDER:
            aksi.append(f"CEK ORDER {s.kode}: order otomatis seharusnya terisi di {h(s.level_order)}; "
                        "jika masih dipegang, JUAL di pembukaan")
        elif s.saran == TIDAK_DAPAT_DIEVALUASI:
            aksi.append(f"PERIKSA {s.kode}: tidak dapat dievaluasi")
        elif s.saran == TAHAN and s.stop_hari_ini is not None and s.stop_besok is not None \
                and s.stop_besok != s.stop_hari_ini:
            aksi.append(f"Perbarui stop {s.strategi} {s.kode} ke {h(s.stop_besok)}")
    for r in lap.rencana:
        aksi.append(f"BELI {r.kode} {r.lot} lot, limit {h(r.harga_maks)} — {TEKS_TARIK}")
    if not aksi:
        return ["Tidak ada aksi untuk t+1."]
    if len(aksi) > MAKS_RINGKASAN_AKSI:
        sisa = len(aksi) - (MAKS_RINGKASAN_AKSI - 1)
        aksi = aksi[: MAKS_RINGKASAN_AKSI - 1] + [f"… dan {sisa} aksi lain (lihat bagian Posisi dan Rencana beli)"]
    return aksi


def teks_kandidat(k: KandidatSeleksi) -> str:
    alasan = k.alasan or ""
    return f"{k.kode} {k.strategi} skor {k.sinyal.komponen.tampilan}: {k.status}" + (f" — {alasan}" if alasan else "")


def status_pasar_ringkas(kondisi: HasilKondisiPasar | None) -> str:
    if kondisi is None:
        return teks.STRIP
    return f"{TAMPILAN_STATUS[kondisi.status]} ({kondisi.aturan}): {kondisi.alasan}"


def jangan_beli(lap: LaporanHarian) -> bool:
    return lap.status_pasar == JANGAN_BELI
