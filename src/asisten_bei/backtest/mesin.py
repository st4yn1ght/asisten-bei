"""Mesin backtest (SPEC §16.2–§16.3, §16.7 jalur per sinyal).

Dua lapis:

1. `siapkan_keputusan`: untuk setiap hari bursa `d`, keputusan yang **tidak bergantung portofolio** —
   status pasar (§10), eligibilitas (§11), sinyal semua strategi (§12), dan skor (§13.1) — dihitung sekali
   dari `DataView(as_of=d)` dan dipakai bersama oleh semua run dan mode.
2. `simulasikan`: linimasa §16.2 untuk satu run × mode, memakai mesin exit bersama (`exit.py`), seleksi
   & sizing yang sama dengan live (`seleksi.py`, §13.2), dan konteks risiko mode itu.

Uang memakai `Decimal`; posisi disimpan sebagai `lembar_adj` dan semua arus Rupiah = `lembar_adj × harga_adj`
(§8.4). Tidak ada transaksi sebelum `backtest.mulai`; sinyal pada `d` menjadi order untuk open hari bursa
berikutnya.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from .. import exit as ex
from ..aturan_bei import AturanBEI
from ..kondisi_pasar import HasilKondisiPasar, hitung_kondisi_pasar
from ..seleksi import REKOMENDASI, KeadaanPortofolio, PosisiTerbuka, seleksi
from ..skor import SinyalBerskor, beri_skor
from ..strategi.dasar import Strategi, hitung_sinyal
from ..universe import AturanUniverse, HasilEligibilitas, kode_eligible, nilai_eligibilitas

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config, KonteksRisiko
    from ..data.tampilan import DataPasar

BATAL_SLOT_SAAT_OPEN = "BATAL_SLOT_SAAT_OPEN"
BATAL_SEKTOR_SAAT_OPEN = "BATAL_SEKTOR_SAAT_OPEN"
BATAL_KAS_SAAT_OPEN = "BATAL_KAS_SAAT_OPEN"
MENUNGGU = "MENUNGGU"  # status_isi: bar t+1 belum tersedia (order dari close hari terakhir)

TOLERANSI_IDENTITAS_PER_TRANSAKSI = Decimal("0.01")  # §16.9


class IdentitasAkuntansiGagal(Exception):
    """Identitas akuntansi §16.9 tidak terpenuhi (kode keluar 1)."""


# ---------------------------------------------------------------------------
# Keputusan harian (tidak bergantung portofolio)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeputusanHari:
    tanggal: date
    kondisi: HasilKondisiPasar
    eligibilitas: HasilEligibilitas
    sinyal: tuple[SinyalBerskor, ...]  # semua strategi yang diminta (termasuk ACAK), urut kode lalu strategi

    @property
    def status_pasar(self) -> str:
        return self.kondisi.status

    @property
    def eligible(self) -> list[str]:
        return kode_eligible(self.eligibilitas)


def siapkan_keputusan(
    pasar: "DataPasar",
    hari: Sequence[date],
    aturan: AturanUniverse,
    kode_breadth: Iterable[str],
    strategi: Sequence[Strategi],
    kemajuan: Callable[[int, int], None] | None = None,
) -> list[KeputusanHari]:
    """Keputusan close untuk setiap `d` di `hari` — hanya dari `DataView(as_of=d)` (P3)."""
    breadth = list(kode_breadth)
    bei = AturanBEI(pasar.cfg)
    hasil = []
    for i, d in enumerate(hari):
        dv = pasar.tampilan(d)
        kondisi = hitung_kondisi_pasar(dv, breadth)
        el = nilai_eligibilitas(dv, aturan)
        eligible = kode_eligible(el)
        sinyal = hitung_sinyal(dv, eligible, strategi, bei)
        hasil.append(KeputusanHari(d, kondisi, el, tuple(beri_skor(dv, sinyal, eligible))))
        if kemajuan is not None:
            kemajuan(i + 1, len(hari))
    return hasil


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpesifikasiRun:
    """Satu run §16.3. `strategi`: sinyal yang ikut; `strategi_lolos`: None = langkah gerbang dilewati."""

    nama: str  # enum run_backtest: pb, bo, mr, acak, gabungan_aktif, gabungan_lolos
    strategi: frozenset[str]
    deduplikasi: bool
    strategi_lolos: frozenset[str] | None = None

    @classmethod
    def per_strategi(cls, kode: str) -> "SpesifikasiRun":
        return cls(kode.lower(), frozenset({kode.upper()}), False, None)

    @classmethod
    def gabungan_aktif(cls, aktif: Iterable[str]) -> "SpesifikasiRun":
        return cls("gabungan_aktif", frozenset(aktif), True, None)

    @classmethod
    def gabungan_lolos(cls, aktif: Iterable[str], lolos: Iterable[str]) -> "SpesifikasiRun":
        """Sinyal strategi aktif yang tidak lolos tetap dicatat sebagai `BAYANGAN_GERBANG` (§13.2 langkah 3)."""
        return cls("gabungan_lolos", frozenset(aktif), True, frozenset(lolos))


@dataclass
class BarisEkuitas:
    tanggal: date
    kas: Decimal
    nilai_posisi: Decimal
    ekuitas: Decimal
    n_posisi: int
    perkiraan: bool  # ≈: ada posisi tanpa close valid pada tanggal ini (§14.1)
    status_pasar: str | None


@dataclass
class SinyalDilewati:
    tanggal: date  # tanggal sinyal
    kode: str
    strategi: str
    skor: float
    status_sinyal: str
    status_isi: str | None
    alasan: str
    tanggal_isi: date | None = None


@dataclass
class HasilSimulasi:
    run: SpesifikasiRun
    mode: str
    modal: Decimal
    tanggal_awal: date  # close hari bursa terakhir sebelum hari pertama simulasi (E_awal)
    transaksi: list[ex.Posisi]  # urut tanggal entry; `keluar` None = masih terbuka di akhir
    ekuitas: list[BarisEkuitas]
    dilewati: list[SinyalDilewati]
    kas_awal: Decimal
    kas_akhir: Decimal

    @property
    def tertutup(self) -> list[ex.Posisi]:
        return [p for p in self.transaksi if p.keluar is not None]

    @property
    def terbuka(self) -> list[ex.Posisi]:
        return [p for p in self.transaksi if p.keluar is None]


def _order(k, sektor: str, tanggal: date, ekuitas: Decimal, status_pasar: str) -> ex.Order:
    sg = k.sinyal.sinyal
    hs = k.sizing
    return ex.Order(
        kode=sg.kode, strategi=sg.strategi, sektor=sektor, tanggal_sinyal=tanggal, lembar=int(hs.lembar),
        harga_maks=sg.harga_maks, stop=sg.stop, target_r=sg.target_r, faktor_sinyal=ex._f(sg.faktor_split),
        skor=k.sinyal.skor,
        meta={
            "skor": k.sinyal.skor, "lot": hs.lot, "pembatas": hs.pembatas, "status_pasar": status_pasar,
            "ekuitas_sinyal": ekuitas, "harga_maks_nominal": sg.harga_maks, "stop_nominal": sg.stop,
            "risiko_di_atas_anggaran": hs.risiko_di_atas_anggaran, "fraksi_dipakai": sg.fraksi_dipakai,
        },
    )


def simulasikan(
    keputusan: Sequence[KeputusanHari],
    pasar: "DataPasar",
    run: SpesifikasiRun,
    *,
    mode: str,
    konteks: "KonteksRisiko",
    sektor_dari: Callable[[str], str],
    tanggal_awal: date,
    tanggal_penilaian: Iterable[date] = (),
    lolos_pada: Callable[[date], Collection[str] | None] | None = None,
    sektor_pada: Callable[[date], Callable[[str], str]] | None = None,
) -> HasilSimulasi:
    """Linimasa §16.2 untuk satu run × mode. `tanggal_penilaian`: tanggal yang nilai posisi terbukanya
    dicatat (`pos.meta["nilai_pada"][d]`, mis. hari bursa terakhir in-sample untuk `MELEWATI_HOLDOUT`).

    Jurnal kertas (§17.2): `lolos_pada(d)` menggantikan `run.strategi_lolos` dengan gerbang yang berlaku pada
    `d`, dan `sektor_pada(d)` menggantikan `sektor_dari` dengan sektor dari `snapshot_input` milik `d`."""
    cfg = pasar.cfg
    bei = AturanBEI(cfg)
    kal = pasar.kalender
    nilai_pada = set(tanggal_penilaian)
    modal = Decimal(konteks.modal_idr)
    kas = modal
    maks_sektor = konteks.maks_per_sektor
    posisi: list[ex.Posisi] = []
    semua: list[ex.Posisi] = []
    ekuitas_rows = [BarisEkuitas(tanggal_awal, kas, Decimal(0), kas, 0, False, None)]
    dilewati: list[SinyalDilewati] = []
    tertunda: list[ex.Order] = []
    for kh in keputusan:
        d = kh.tanggal
        dv = pasar.tampilan(d)
        cache: dict[str, ex.BarHari] = {}

        def bar(kode: str) -> ex.BarHari:
            if kode not in cache:
                cache[kode] = ex.bar_hari(dv, kode)
            return cache[kode]

        # 1. Pembukaan: posisi lama (langkah 0–4).
        aktif_hari_ini = list(posisi)  # semua posisi yang terbuka pada suatu saat di hari d (untuk tanda)
        hasil_jual_open = Decimal(0)
        for pos in list(posisi):
            ex.sesuaikan_posisi_split(pos, dv)
            k = ex.buka(pos, bar(pos.kode), cfg, bei, kal)
            if k is not None:
                kas += k.hasil_jual_idr
                hasil_jual_open += k.hasil_jual_idr
                posisi.remove(pos)
        # 2. Order beli tertunda, berurutan sesuai skor.
        tersedia = kas if cfg.risiko.dana_jual_langsung_dipakai else kas - hasil_jual_open
        sektor = Counter(p.sektor for p in posisi)
        for order in tertunda:
            order = ex.sesuaikan_order_split(order, dv)
            b = bar(order.kode)
            status = ex.periksa_isi(order, b, bei)
            total = Decimal(0)
            if status is None:
                if len(posisi) >= konteks.maks_posisi:
                    status = BATAL_SLOT_SAAT_OPEN
                elif maks_sektor is not None and sektor[order.sektor] >= maks_sektor:
                    status = BATAL_SEKTOR_SAAT_OPEN
                else:
                    total = ex.biaya_isi(order, b, cfg)[3]
                    if tersedia < total:
                        status = BATAL_KAS_SAAT_OPEN
            if status is not None:
                dilewati.append(SinyalDilewati(order.tanggal_sinyal, order.kode, order.strategi, order.skor,
                                               REKOMENDASI, status, "", d))
                continue
            pos = ex.isi_order(order, b, cfg, bei)
            kas -= pos.biaya_beli_idr
            tersedia -= pos.biaya_beli_idr
            semua.append(pos)
            aktif_hari_ini.append(pos)
            if ex.keluar_hari_entry(pos, b, cfg, bei) is not None:
                kas += pos.keluar.hasil_jual_idr
                continue
            posisi.append(pos)
            sektor[pos.sektor] += 1
        tertunda = []
        # 3. Sepanjang hari (langkah 5–6).
        for pos in list(posisi):
            k = ex.intraday(pos, bar(pos.kode), cfg, hari_entry=(pos.tanggal_entry == d))
            if k is not None:
                kas += k.hasil_jual_idr
                posisi.remove(pos)
        # Tanda transaksi untuk setiap posisi yang terbuka pada suatu saat di hari d (§8.2, §8.5).
        for pos in aktif_hari_ini:
            _tandai(pos, bar(pos.kode), dv)
        # 4. Ekuitas (mark-to-market).
        perkiraan = False
        for pos in posisi:
            b = bar(pos.kode)
            ex.perbarui_nilai(pos, b)
            perkiraan |= not b.valid
        nilai = sum((p.nilai() for p in posisi), Decimal(0))
        ekuitas = kas + nilai
        status_pasar = kh.status_pasar
        ekuitas_rows.append(BarisEkuitas(d, kas, nilai, ekuitas, len(posisi), perkiraan, status_pasar))
        if d in nilai_pada:  # penilaian akhir in-sample (§16.4): nilai dan tanda pada hari itu
            for pos in posisi:
                pos.meta.setdefault("nilai_pada", {})[d] = pos.nilai()
                pos.meta.setdefault("tanda_pada", {})[d] = frozenset(pos.tanda)
        # 6. Langkah 7: trailing & jadwal jual berbasis close.
        for pos in posisi:
            ex.tutup(pos, bar(pos.kode), dv, cfg, bei, kal, status_pasar)
        # 7. Sinyal → seleksi & sizing → order untuk hari berikutnya.
        sinyal = [s for s in kh.sinyal if s.strategi in run.strategi]
        if not sinyal:
            continue
        sektor_hari = sektor_pada(d) if sektor_pada is not None else sektor_dari
        lolos_hari = lolos_pada(d) if lolos_pada is not None else run.strategi_lolos
        porto = KeadaanPortofolio(
            posisi=tuple(PosisiTerbuka(p.kode, p.sektor, p.jual_terjadwal is not None) for p in posisi),
            kas_idr=kas,
            ekuitas_idr=ekuitas,
            estimasi_hasil_jual_terjadwal_idr=sum(
                (p.estimasi_hasil_stop(cfg) for p in posisi if p.jual_terjadwal is not None), Decimal(0)
            ),
        )
        hs = seleksi(
            sinyal, status_pasar=status_pasar, portofolio=porto, konteks=konteks, cfg=cfg,
            strategi_lolos=lolos_hari, deduplikasi=run.deduplikasi, sektor_dari=sektor_hari,
            hitung_ketergantungan=False,
        )
        for k in hs.kandidat:
            if k.status == REKOMENDASI:
                tertunda.append(_order(k, sektor_hari(k.kode), d, ekuitas, status_pasar))
            else:
                dilewati.append(SinyalDilewati(d, k.kode, k.strategi, k.sinyal.skor, k.status, None, k.alasan))
    for order in tertunda:  # order dari close hari terakhir: bar t+1 belum ada
        dilewati.append(SinyalDilewati(order.tanggal_sinyal, order.kode, order.strategi, order.skor,
                                       REKOMENDASI, MENUNGGU, ""))
    hasil = HasilSimulasi(run, mode, modal, tanggal_awal, semua, ekuitas_rows, dilewati, modal, kas)
    periksa_identitas(hasil)
    return hasil


def _tandai(pos: ex.Posisi, b: ex.BarHari, dv) -> None:
    """`SUMBER_MANUAL` bila bar hari itu dari CSV manual; `ANOMALI` bila anomali terjadi pada hari itu."""
    if b.ada and b.sumber.startswith("csv_manual"):
        pos.tanda.add(ex.SUMBER_MANUAL)
    t = dv.tanda(pos.kode)
    if t is not None and str(t.get("anomali_terakhir", ""))[:10] == b.tanggal.isoformat():
        pos.tanda.add(ex.ANOMALI)


def periksa_identitas(h: HasilSimulasi) -> None:
    """§16.9: `E_akhir − E_awal = Σ P&L tertutup + Σ (nilai TERBUKA − biaya beli)` dan
    `kas_akhir = kas_awal − Σ biaya beli + Σ hasil jual`. Toleransi Rp 0,01 × jumlah transaksi."""
    n = max(len(h.transaksi), 1)
    tol = TOLERANSI_IDENTITAS_PER_TRANSAKSI * n
    e_akhir = h.ekuitas[-1].ekuitas
    pnl = sum((p.keluar.pnl_idr for p in h.tertutup), Decimal(0))
    terbuka = sum((p.nilai() - p.biaya_beli_idr for p in h.terbuka), Decimal(0))
    selisih_e = (e_akhir - h.modal) - (pnl + terbuka)
    beli = sum((p.biaya_beli_idr for p in h.transaksi), Decimal(0))
    jual = sum((p.keluar.hasil_jual_idr for p in h.tertutup), Decimal(0))
    selisih_kas = h.kas_akhir - (h.kas_awal - beli + jual)
    if abs(selisih_e) > tol or abs(selisih_kas) > tol:
        raise IdentitasAkuntansiGagal(
            f"identitas akuntansi gagal ({h.run.nama}/{h.mode}): selisih ekuitas {selisih_e}, selisih kas "
            f"{selisih_kas} (toleransi {tol})"
        )


# ---------------------------------------------------------------------------
# Jalur per sinyal (§16.7)
# ---------------------------------------------------------------------------


@dataclass
class JalurSinyal:
    tanggal_sinyal: date
    kode: str
    strategi: str
    skor: float
    status_pasar: str
    status_isi: str | None = None  # TERISI / BATAL_* / MENUNGGU
    tanggal_entry: date | None = None
    tanggal_exit: date | None = None
    alasan_exit: str | None = None
    r: Decimal | None = None
    hari: int | None = None
    tanda: tuple[str, ...] = ()
    harga_entry: Decimal | None = None  # nominal pada tanggal entry (jurnal `hasil_sinyal`, §17.1)
    harga_exit: Decimal | None = None  # nominal pada tanggal exit (TERBUKA: close valid terakhir × F(akhir))


def cfg_per_unit(cfg: "Config") -> "Config":
    """Config jalur per sinyal: fee persentase saja (tanpa fee minimum & pembulatan), sehingga R per
    lembar tidak bergantung ukuran posisi (§16.7, "1 unit independen")."""
    biaya_baru = cfg.biaya.model_copy(update={"fee_minimum_idr": Decimal(0), "pembulatan_fee": None})
    return cfg.model_copy(update={"biaya": biaya_baru})


def jalur_per_sinyal(
    keputusan: Sequence[KeputusanHari],
    pasar: "DataPasar",
    strategi: str,
    sektor_dari: Callable[[str], str],
) -> list[JalurSinyal]:
    """Setiap sinyal `strategi` (semua status kecuali `STOP_TERLALU_JAUH`) disimulasikan sebagai satu
    lembar dengan §15.1, tanpa batas slot atau modal."""
    cfg = cfg_per_unit(pasar.cfg)
    bei = AturanBEI(cfg)
    kal = pasar.kalender
    hasil: list[JalurSinyal] = []
    tertunda: list[tuple[ex.Order, JalurSinyal]] = []
    terbuka: list[tuple[ex.Posisi, JalurSinyal]] = []
    unit: list[tuple[ex.Posisi, JalurSinyal]] = []  # semua unit yang terisi
    for kh in keputusan:
        d = kh.tanggal
        dv = pasar.tampilan(d)
        cache: dict[str, ex.BarHari] = {}

        def bar(kode: str) -> ex.BarHari:
            if kode not in cache:
                cache[kode] = ex.bar_hari(dv, kode)
            return cache[kode]

        def selesai(pos: ex.Posisi, j: JalurSinyal) -> None:
            k = pos.keluar
            j.tanggal_exit, j.alasan_exit, j.r, j.harga_exit = k.tanggal, k.alasan, k.r, k.harga_nominal
            j.hari = kal.nomor_hari(pos.tanggal_entry, k.tanggal)

        aktif_hari_ini = [pos for pos, _j in terbuka]  # untuk tanda, seperti simulasikan
        lanjut = []
        for pos, j in terbuka:
            ex.sesuaikan_posisi_split(pos, dv)
            if ex.buka(pos, bar(pos.kode), cfg, bei, kal) is not None:
                selesai(pos, j)
            else:
                lanjut.append((pos, j))
        terbuka = lanjut
        for order, j in tertunda:
            order = ex.sesuaikan_order_split(order, dv)
            b = bar(order.kode)
            status = ex.periksa_isi(order, b, bei)
            j.status_isi = status or ex.TERISI
            if status is not None:
                continue
            pos = ex.isi_order(order, b, cfg, bei)
            j.tanggal_entry = d
            j.harga_entry = pos.harga_beli * pos.faktor_entry
            unit.append((pos, j))
            aktif_hari_ini.append(pos)
            if ex.keluar_hari_entry(pos, b, cfg, bei) is not None:
                selesai(pos, j)
                continue
            terbuka.append((pos, j))
        tertunda = []
        lanjut = []
        for pos, j in terbuka:
            b = bar(pos.kode)
            if ex.intraday(pos, b, cfg, hari_entry=(pos.tanggal_entry == d)) is not None:
                selesai(pos, j)
                continue
            lanjut.append((pos, j))
        terbuka = lanjut
        for pos in aktif_hari_ini:
            _tandai(pos, bar(pos.kode), dv)
        for pos, _j in terbuka:
            b = bar(pos.kode)
            ex.perbarui_nilai(pos, b)
            ex.tutup(pos, b, dv, cfg, bei, kal, kh.status_pasar)
        for s in kh.sinyal:
            if s.strategi != strategi or not s.sinyal.valid:
                continue
            sg = s.sinyal
            j = JalurSinyal(d, sg.kode, sg.strategi, s.skor, kh.status_pasar)
            hasil.append(j)
            order = ex.Order(sg.kode, sg.strategi, sektor_dari(sg.kode), d, 1, sg.harga_maks, sg.stop,
                             sg.target_r, ex._f(sg.faktor_split), s.skor)
            tertunda.append((order, j))
    for _order_t, j in tertunda:
        j.status_isi = MENUNGGU
    if keputusan:
        akhir = keputusan[-1].tanggal
        dv = pasar.tampilan(akhir)
        for pos, j in terbuka:  # masih terbuka di akhir: dinilai pada close valid terakhir (seperti transaksi_*)
            k = ex.tutup_terbuka(pos, akhir, cfg, ex._f(dv.faktor_split(pos.kode)))
            j.tanggal_exit, j.alasan_exit, j.r, j.harga_exit = akhir, ex.TERBUKA, k.r, k.harga_nominal
            j.hari = kal.nomor_hari(pos.tanggal_entry, akhir)
    for pos, j in unit:
        j.tanda = tuple(t for t in ex.URUTAN_TANDA if t in pos.tanda)
    return hasil


def hari_simulasi(pasar: "DataPasar", mulai: date, akhir: date) -> tuple[date, list[date]]:
    """(tanggal awal E_awal = hari bursa terakhir sebelum `mulai`, hari bursa [mulai, akhir])."""
    kal = pasar.kalender
    hari = kal.daftar_hari_bursa(mulai, akhir)
    return kal.hari_bursa_sebelum(mulai), hari
