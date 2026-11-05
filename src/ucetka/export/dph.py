"""Přiznání k DPH (DPHDP3) a kontrolní hlášení (DPHKH1) ve formátu XML pro EPO / Moje daně.

Struktura a zaokrouhlení odpovídají podáním vytvořeným v EPO (verzePis 03.01): řádky přiznání
v celých korunách, kontrolní hlášení v haléřích.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from importlib.metadata import version
from pathlib import Path

from lxml import etree

from ucetka import expenses as expenses_mod
from ucetka import ledger
from ucetka.config import reports_dir
from ucetka.expenses import Expense
from ucetka.models import Invoice
from ucetka.payments import EpoIdentity, load_settings

ZERO = Decimal(0)
KH_LIMIT = Decimal(10_000)
EU_STATES = {
    "AT",
    "BE",
    "BG",
    "CY",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "GR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
}
# EPO pojmenovává sazby historicky: „23“ = základní (21 %), „5“ = snížená (12 %).
RATE_SUFFIX = {Decimal(21): "23", Decimal(12): "5"}
KH_RATE_SUFFIX = {Decimal(21): "1", Decimal(12): "2"}


class VatExportError(ValueError):
    pass


@dataclass
class KhRow:
    doc_number: str
    tax_point_date: date
    bases: dict[Decimal, Decimal]
    vats: dict[Decimal, Decimal]
    partner_dic: str | None = None
    country: str | None = None


@dataclass
class VatReturn:
    year: int
    quarter: int
    output: dict[str, Decimal] = field(default_factory=dict)
    a2: list[KhRow] = field(default_factory=list)
    a4: list[KhRow] = field(default_factory=list)
    a5: dict[Decimal, list[Decimal]] = field(default_factory=dict)
    b2: list[KhRow] = field(default_factory=list)
    b3: dict[Decimal, list[Decimal]] = field(default_factory=dict)
    eu_services_supplied: Decimal = ZERO
    eu_supplies: dict[tuple[str, str], list] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, key: str, amount: Decimal) -> None:
        self.output[key] = self.output.get(key, ZERO) + amount


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    if quarter not in (1, 2, 3, 4):
        raise VatExportError(f"Čtvrtletí musí být 1–4, ne {quarter}")
    start = date(year, 3 * quarter - 2, 1)
    end = date(year + (quarter == 4), (3 * quarter) % 12 + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def _suffix(rate: Decimal, table: dict[Decimal, str], what: str) -> str:
    if rate not in table:
        raise VatExportError(
            f"{what}: sazba {rate} % není v přiznání podporovaná (jen 21 a 12 %)"
        )
    return table[rate]


def _strip_country(dic: str) -> str:
    return dic[2:] if dic[:2].isalpha() else dic


def _kh_value(inv: Invoice) -> Decimal:
    """Dobropis jde do A.4/A.5 podle hodnoty opravovaného dokladu, je-li v evidenci."""
    if inv.is_credit_note and inv.original and ledger.exists(inv.original.number):
        inv = ledger.get(inv.original.number).invoice
    return inv.total_net_czk + inv.total_vat_czk


def _add_issued(ret: VatReturn, start: date, end: date) -> None:
    for record in ledger.list_records():
        inv = record.invoice
        if record.status == "cancelled" or not start <= inv.tax_point_date <= end:
            continue
        if not inv.customer.name:
            raise VatExportError(f"Faktura {inv.number} nemá odběratele — doplň ho, jinak nejde zařadit do KH")
        if not inv.is_tax_document:
            raise VatExportError(
                f"Faktura {inv.number} je od neplátce — do přiznání nepatří"
            )
        if inv.reverse_charge:
            ret.eu_services_supplied += inv.sign * inv.total_net_czk
            dic = (inv.customer.dic or "").upper().replace(" ", "")
            key = (dic[:2], dic[2:])
            agg = ret.eu_supplies.setdefault(key, [ZERO, 0])
            agg[0] += inv.sign * inv.total_net_czk
            agg[1] += inv.sign
            continue
        bases: dict[Decimal, Decimal] = {}
        vats: dict[Decimal, Decimal] = {}
        for row in inv.vat_breakdown():
            if row.rate == 0:
                raise VatExportError(
                    f"Faktura {inv.number}: osvobozené plnění (0 %) export nepodporuje"
                )
            s = _suffix(row.rate, RATE_SUFFIX, f"Faktura {inv.number}")
            ret.add(f"obrat{s}", inv.sign * row.base_czk)
            ret.add(f"dan{s}", inv.sign * row.vat_czk)
            bases[row.rate] = inv.sign * row.base_czk
            vats[row.rate] = inv.sign * row.vat_czk
        dic = inv.customer.dic or ""
        if (
            dic.upper().startswith("CZ")
            and _kh_value(inv) > KH_LIMIT
        ):
            ret.a4.append(
                KhRow(
                    inv.number,
                    inv.tax_point_date,
                    bases,
                    vats,
                    partner_dic=_strip_country(dic),
                )
            )
        else:
            for rate in bases:
                agg = ret.a5.setdefault(rate, [ZERO, ZERO])
                agg[0] += bases[rate]
                agg[1] += vats[rate]


def _add_expense(ret: VatReturn, e: Expense) -> None:
    rows = [r for r in e.vat_rows if not (r.rate == 0 and r.vat == 0)]
    if not rows:
        return
    if e.business_share < 100:
        raise VatExportError(
            f"Výdaj {e.id}: odpočet krácený podle soukromého použití (§ 75) export zatím neumí — zadej DPH ručně v EPO"
        )
    bases = {r.rate: r.base for r in rows}
    vats = {r.rate: r.vat for r in rows}
    if e.reverse_charge:
        eu = e.supplier_country in EU_STATES
        if eu and not e.supplier_dic:
            raise VatExportError(f"Výdaj {e.id}: dodavatel z EU bez DIČ — kontrolní hlášení A.2 ho vyžaduje")
        if e.goods and not eu:
            raise VatExportError(
                f"Výdaj {e.id}: zboží mimo EU je dovoz přes celní úřad, ne reverse charge"
            )
        kind = ("p_zb", "dan_pzb") if e.goods else ("p_sl", "dan_psl")
        tail = "" if e.goods else ("_e" if eu else "_z")
        for r in rows:
            s = _suffix(r.rate, RATE_SUFFIX, f"Výdaj {e.id}")
            ret.add(f"{kind[0]}{s}{tail}", r.base)
            ret.add(f"{kind[1]}{s}{tail}", r.vat)
        ret.a2.append(
            KhRow(
                e.document_number,
                e.tax_point_date,
                bases,
                vats,
                partner_dic=_strip_country(e.supplier_dic)
                if eu and e.supplier_dic
                else None,
                country=e.supplier_country if eu else None,
            )
        )
        return
    for r in rows:
        s = _suffix(r.rate, RATE_SUFFIX, f"Výdaj {e.id}")
        ret.add(f"pln{s}", r.base)
        ret.add(f"odp_tuz{s}_nar", r.vat)
    dic = e.supplier_dic or ""
    if dic.upper().startswith("CZ") and e.base_total + e.vat_total > KH_LIMIT:
        ret.b2.append(
            KhRow(
                e.document_number,
                e.tax_point_date,
                bases,
                vats,
                partner_dic=_strip_country(dic),
            )
        )
    else:
        for rate in bases:
            agg = ret.b3.setdefault(rate, [ZERO, ZERO])
            agg[0] += bases[rate]
            agg[1] += vats[rate]


def collect(year: int, quarter: int) -> VatReturn:
    if load_settings().vat_period != "quarterly":
        raise VatExportError("Export umí jen čtvrtletní plátce")
    start, end = _quarter_bounds(year, quarter)
    ret = VatReturn(year, quarter)
    _add_issued(ret, start, end)
    for e in sorted(
        expenses_mod.list_expenses(), key=lambda x: (x.tax_point_date, x.id)
    ):
        if start <= e.vat_date <= end:
            _add_expense(ret, e)
    ret.a4.sort(key=lambda r: (r.tax_point_date, r.doc_number))
    return ret


def _kc(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def dp3_lines(ret: VatReturn) -> dict[str, dict[str, int]]:
    o = {k: _kc(v) for k, v in ret.output.items()}

    def g(key: str) -> int:
        return o.get(key, 0)

    veta1 = {
        k: g(k)
        for k in (
            "obrat23",
            "dan23",
            "obrat5",
            "dan5",
            "p_zb23",
            "dan_pzb23",
            "p_zb5",
            "dan_pzb5",
            "p_sl23_e",
            "dan_psl23_e",
            "p_sl5_e",
            "dan_psl5_e",
            "dov_zb23",
            "dov_zb5",
            "dan_dzb23",
            "dan_dzb5",
            "p_dop_nrg",
            "dan_pdop_nrg",
            "p_sl23_z",
            "dan_psl23_z",
            "p_sl5_z",
            "dan_psl5_z",
        )
    }
    veta2 = {
        k: 0
        for k in (
            "dod_zb",
            "pln_sluzby",
            "pln_vyvoz",
            "dod_dop_nrg",
            "pln_zaslani",
            "pln_ost",
        )
    }
    veta2["pln_sluzby"] = _kc(ret.eu_services_supplied)
    veta3 = {k: 0 for k in ("tri_pozb", "tri_dozb", "dov_osv", "opr_verit", "opr_dluz")}
    veta4 = {
        k: 0
        for k in (
            "pln23",
            "odp_tuz23_nar",
            "odp_tuz23",
            "pln5",
            "odp_tuz5_nar",
            "odp_tuz5",
            "dov_cu",
            "odp_cu_nar",
            "odp_cu",
            "nar_zdp23",
            "od_zdp23",
            "odkr_zdp23",
            "nar_zdp5",
            "od_zdp5",
            "odkr_zdp5",
            "odp_rez_nar",
            "odp_rezim",
            "nar_maj",
            "od_maj",
            "odkr_maj",
            "odp_sum_nar",
            "odp_sum_kr",
        )
    }
    for k in ("pln23", "odp_tuz23_nar", "pln5", "odp_tuz5_nar"):
        veta4[k] = g(k)
    # Ř. 43 je v EPO součtem už zaokrouhlených ř. 3, 5 a 12, ne zaokrouhleným součtem dokladů.
    for s in ("23", "5"):
        veta4[f"nar_zdp{s}"] = veta1[f"p_zb{s}"] + veta1[f"p_sl{s}_e"] + veta1[f"p_sl{s}_z"]
        veta4[f"od_zdp{s}"] = veta1[f"dan_pzb{s}"] + veta1[f"dan_psl{s}_e"] + veta1[f"dan_psl{s}_z"]
    veta4["odp_sum_nar"] = (
        veta4["odp_tuz23_nar"]
        + veta4["odp_tuz5_nar"]
        + veta4["od_zdp23"]
        + veta4["od_zdp5"]
    )
    veta5 = {"plnosv_kf": 0, "plnosv_nkf": 0, "odp_uprav_kf": 0}
    output_tax = sum(veta1[k] for k in veta1 if k.startswith("dan"))
    deduction = veta4["odp_sum_nar"]
    veta6 = {
        "dan_zocelk": output_tax,
        "odp_zocelk": deduction,
        "dano_da": max(output_tax - deduction, 0),
        "dano_no": max(deduction - output_tax, 0),
        "dan_vrac": 0,
    }
    return {
        "Veta1": veta1,
        "Veta2": veta2,
        "Veta3": veta3,
        "Veta4": veta4,
        "Veta5": veta5,
        "Veta6": veta6,
    }


def _num(value: Decimal | int) -> str:
    text = format(Decimal(value).normalize(), "f")
    return "0" if text in ("-0", "0") else text


def _d(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _root() -> etree._Element:
    return etree.Element(
        "Pisemnost", nazevSW="ucetka-mcp", verzeSW=version("ucetka-mcp")
    )


def _veta_p(parent: etree._Element, ident: EpoIdentity) -> None:
    attrs = {
        "c_ufo": ident.c_ufo,
        "c_pracufo": ident.c_pracufo,
        "dic": ident.dic,
        "typ_ds": "F",
        "jmeno": ident.jmeno,
        "prijmeni": ident.prijmeni,
        "zkrobchjm": ident.zkrobchjm,
        "ulice": ident.ulice,
        "c_pop": ident.c_pop,
        "naz_obce": ident.naz_obce,
        "psc": ident.psc,
        "email": ident.email,
        "c_telef": ident.c_telef,
    }
    etree.SubElement(parent, "VetaP", {k: v for k, v in attrs.items() if v})


def _identity() -> EpoIdentity:
    ident = load_settings().epo
    if ident is None:
        raise VatExportError(
            "V tax.yaml chybí sekce epo (finanční úřad, pracoviště, adresa)"
        )
    return ident


def _xml(root: etree._Element) -> bytes:
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )


def render_dp3(ret: VatReturn, filed_on: date) -> bytes:
    ident = _identity()
    root = _root()
    doc = etree.SubElement(root, "DPHDP3", verzePis="03.01")
    etree.SubElement(
        doc,
        "VetaD",
        {
            "c_okec": ident.c_okec,
            "ctvrt": str(ret.quarter),
            "d_poddp": _d(filed_on),
            "dapdph_forma": "B",
            "dokument": "DP3",
            "k_uladis": "DPH",
            "rok": str(ret.year),
            "trans": "A",
            "typ_platce": "P",
        },
    )
    _veta_p(doc, ident)
    for name, values in dp3_lines(ret).items():
        etree.SubElement(doc, name, {k: str(values[k]) for k in sorted(values)})
    return _xml(root)


def _amounts(
    row_bases: dict[Decimal, Decimal], row_vats: dict[Decimal, Decimal], what: str
) -> dict[str, str]:
    out: dict[str, str] = {}
    for rate in sorted(row_bases, reverse=True):
        s = _suffix(rate, KH_RATE_SUFFIX, what)
        out[f"zakl_dane{s}"] = _num(row_bases[rate])
        out[f"dan{s}"] = _num(row_vats[rate])
    return out


def render_kh(ret: VatReturn, filed_on: date) -> bytes:
    ident = _identity()
    root = _root()
    doc = etree.SubElement(root, "DPHKH1", verzePis="03.01")
    etree.SubElement(
        doc,
        "VetaD",
        {
            "dokument": "KH1",
            "k_uladis": "DPH",
            "rok": str(ret.year),
            "ctvrt": str(ret.quarter),
            "d_poddp": _d(filed_on),
            "khdph_forma": "B",
        },
    )
    _veta_p(doc, ident)
    for i, r in enumerate(ret.a2, 1):
        attrs = {"c_radku": str(i)}
        if r.country:
            attrs |= {"k_stat": r.country, "vatid_dod": r.partner_dic or ""}
        attrs |= {"c_evid_dd": r.doc_number, "dppd": _d(r.tax_point_date)}
        etree.SubElement(doc, "VetaA2", attrs | _amounts(r.bases, r.vats, r.doc_number))
    for i, r in enumerate(ret.a4, 1):
        attrs = {
            "c_radku": str(i),
            "dic_odb": r.partner_dic or "",
            "c_evid_dd": r.doc_number,
            "dppd": _d(r.tax_point_date),
        }
        etree.SubElement(
            doc,
            "VetaA4",
            attrs
            | _amounts(r.bases, r.vats, r.doc_number)
            | {"kod_rezim_pl": "0", "zdph_44": "N"},
        )
    if ret.a5:
        etree.SubElement(
            doc,
            "VetaA5",
            _amounts(
                {k: v[0] for k, v in ret.a5.items()},
                {k: v[1] for k, v in ret.a5.items()},
                "A.5",
            ),
        )
    for i, r in enumerate(ret.b2, 1):
        attrs = {
            "c_radku": str(i),
            "dic_dod": r.partner_dic or "",
            "c_evid_dd": r.doc_number,
            "dppd": _d(r.tax_point_date),
        }
        etree.SubElement(
            doc,
            "VetaB2",
            attrs
            | _amounts(r.bases, r.vats, r.doc_number)
            | {"pomer": "N", "zdph_44": "N"},
        )
    if ret.b3:
        etree.SubElement(
            doc,
            "VetaB3",
            _amounts(
                {k: v[0] for k, v in ret.b3.items()},
                {k: v[1] for k, v in ret.b3.items()},
                "B.3",
            ),
        )
    etree.SubElement(doc, "VetaC", _kh_control(ret))
    return _xml(root)


def _kh_control(ret: VatReturn) -> dict[str, str]:
    def total(
        rows: list[KhRow], agg: dict[Decimal, list[Decimal]], rate: Decimal
    ) -> Decimal:
        return (
            sum((r.bases.get(rate, ZERO) for r in rows), ZERO)
            + agg.get(rate, [ZERO])[0]
        )

    c: dict[str, Decimal] = {
        "obrat23": total(ret.a4, ret.a5, Decimal(21)),
        "obrat5": total(ret.a4, ret.a5, Decimal(12)),
        "pln23": total(ret.b2, ret.b3, Decimal(21)),
        "pln5": total(ret.b2, ret.b3, Decimal(12)),
        "celk_zd_a2": sum((sum(r.bases.values(), ZERO) for r in ret.a2), ZERO),
    }
    return {k: _num(v) for k, v in c.items() if v}


def render_shv(ret: VatReturn, filed_on: date) -> bytes:
    """Souhrnné hlášení (DPHSHV 02.01.05) — jen poskytnuté služby do EU (kód plnění 3)."""
    if not ret.eu_supplies:
        raise VatExportError("Žádné služby do EU — souhrnné hlášení se nepodává")
    ident = _identity()
    root = _root()
    doc = etree.SubElement(root, "DPHSHV", verzePis="02.01.05")
    etree.SubElement(doc, "VetaD", {
        "dokument": "SHV", "k_uladis": "DPH", "rok": str(ret.year), "ctvrt": str(ret.quarter),
        "shvies_forma": "R", "d_poddp": _d(filed_on),
    })
    attrs = {
        "c_ufo": ident.c_ufo, "c_pracufo": ident.c_pracufo, "dic": ident.dic, "typ_ds": "F",
        "jmeno": ident.jmeno, "prijmeni": ident.prijmeni, "zkrobchjm": ident.zkrobchjm,
        "ulice": ident.ulice, "c_pop": ident.c_pop, "naz_obce": ident.naz_obce, "psc": ident.psc,
    }
    etree.SubElement(doc, "VetaP", attrs)
    for i, ((country, vat), (value, count)) in enumerate(sorted(ret.eu_supplies.items()), 1):
        etree.SubElement(doc, "VetaR", {
            "c_rad": str(i), "por_c_stran": "1", "k_stat": country, "c_vat": vat, "k_pln_eu": "3",
            "pln_hodnota": str(_kc(value)), "pln_pocet": str(count),
        })
    return _xml(root)


def export(
    year: int,
    quarter: int,
    *,
    filed_on: date | None = None,
    out_dir: Path | None = None,
) -> tuple[list[Path], VatReturn]:
    """Uloží přiznání + KH, a souhrnné hlášení jen pokud byly služby do EU."""
    ret = collect(year, quarter)
    filed_on = filed_on or date.today()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dic = _identity().dic
    folder = out_dir or reports_dir() / str(year) / f"{year}_Q{quarter}_přehledy"
    files = [
        (folder / "DPH" / f"DPHDP3-{dic}-{stamp}.xml", render_dp3(ret, filed_on)),
        (folder / "KH" / f"DPHKH1-{dic}-{stamp}.xml", render_kh(ret, filed_on)),
    ]
    if ret.eu_supplies:
        files.append((folder / "SH" / f"DPHSHV-{dic}-{stamp}.xml", render_shv(ret, filed_on)))
    for path, data in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return [path for path, _ in files], ret


def describe(ret: VatReturn) -> list[str]:
    lines = dp3_lines(ret)
    v1, v4, v6 = lines["Veta1"], lines["Veta4"], lines["Veta6"]
    out = [
        f"DPH {ret.quarter}. čtvrtletí {ret.year} (Kč, zaokrouhleno jako v přiznání):",
        f"ř. 1 tuzemsko 21 %: základ {v1['obrat23']}, daň {v1['dan23']}",
        f"ř. 5 služby z EU: základ {v1['p_sl23_e']}, daň {v1['dan_psl23_e']}",
        f"ř. 12 služby ze třetích zemí: základ {v1['p_sl23_z']}, daň {v1['dan_psl23_z']}",
        f"ř. 40 odpočet tuzemsko: základ {v4['pln23']}, daň {v4['odp_tuz23_nar']}",
        f"ř. 43 odpočet z reverse charge: základ {v4['nar_zdp23']}, daň {v4['od_zdp23']}",
        f"ř. 64 vlastní daňová povinnost: {v6['dano_da']}" if v6["dano_da"] else f"ř. 65 nadměrný odpočet: {v6['dano_no']}",
        f"Kontrolní hlášení: A.2 {len(ret.a2)} řádků, A.4 {len(ret.a4)}, A.5 {'ano' if ret.a5 else 'ne'}, "
        f"B.2 {len(ret.b2)}, B.3 {'ano' if ret.b3 else 'ne'}",
    ]
    if ret.eu_supplies:
        out.append(
            f"ř. 21 služby do EU: {_kc(ret.eu_services_supplied)} → souhrnné hlášení, "
            f"{len(ret.eu_supplies)} odběratelů"
        )
    if v1["p_zb23"]:
        out.insert(2, f"ř. 3 zboží z EU: základ {v1['p_zb23']}, daň {v1['dan_pzb23']}")
    return out + [f"⚠ {w}" for w in ret.warnings]
