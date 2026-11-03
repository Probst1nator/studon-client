#!/usr/bin/env python3
"""Module lists of FAU degree programmes per PO version, from campo's
Modulhandbuch search (searchCourseOfStudyForModuleDescription-flow).

Flow:
  1. GET startFlow?_flowId=searchCourseOfStudyForModuleDescription-flow
       -> form#genericSearchMask. Unlike the Lehrveranstaltungssuche, its
          Abschluss / Fach / Prüfungsordnungsversion fields are real <select>s.
  2. POST the form with the Fach select set and genericSearchMask:search=Suchen.
       -> one result row per Prüfungsordnung, e.g.
          "B.Sc. Materialwissenschaft und Werkstofftechnik (PO 20232)".
  3. GET the row's link (it carries railsUri=/emb/unit/show_module_description_struct/<id>)
     in the same session. The page holds the whole tree as one JSON list right
     after a </form>. k_elementtype_id: 4 = Studiengang, 3 = Konto, 8 = Modul,
     2 = Prüfung; parent_id links them. The tree carries no ECTS.
  4. ECTS (optional, two requests per module): GET detailView-flow?unitId=<module id>
     and POST its tab "Zuordnungsspezifische Leistungsinformationen" -> table with
     Konto, ECTS-Punkte, Pflicht-Kennzeichen, Empfohlenes Fachsemester, Studiengänge.
     Each programme has its own module units, so the ECTS belong to that programme.

All of this is public and needs no login. The session is anonymous on purpose:
with a stale campo JSESSIONID from Firefox, step 3 redirects to an empty page shell.

Importable: studon_client.py --modulhandbuch calls fetch_programmes().
Run directly for standalone use (see __main__).
"""
import argparse
import json
import re
import sys
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from campo_search import CAMPO, _collect_form, _is_campo_url

SEARCH_URL = CAMPO + "/qisserver/pages/startFlow.xhtml?_flowId=searchCourseOfStudyForModuleDescription-flow"
DETAIL_URL = (CAMPO + "/qisserver/pages/startFlow.xhtml?_flowId=detailView-flow&unitId={uid}"
              "&navigationPosition=studiesOffered,searchCourses")
RELATION_TAB = "detailViewData:tabContainer:term-planning-container:tabs:unitRelationDataTab"

ELEMENT_PROGRAMME, ELEMENT_KONTO, ELEMENT_MODULE = 4, 3, 8

# Konten that hold catalogues open to every programme (language courses, key
# qualifications, any FAU module) rather than the programme's own modules.
CATALOGUE_KONTO = re.compile(r"Zusatzleistungen|Schlüsselqualifikation|Gesamtangebot der FAU", re.I)


def _norm(text: str) -> str:
    """Casefold and treat hyphens and spaces alike: 'KI-Material…' == 'KI - Material…'."""
    return re.sub(r"[\s\-]+", " ", text).strip().casefold()


def _num(text: Optional[str]) -> Optional[float]:
    m = re.search(r"\d+(?:[.,]\d+)?", text or "")
    return float(m.group(0).replace(",", ".")) if m else None


# --- pure parsers (no network; tested in test_modulhandbuch.py) ---------------

def select_for_label(form: Tag, label: str) -> Optional[Tag]:
    """The <select> behind a search-mask label ('Fach', 'Abschluss', …).

    The label points at the '<base>_focus' input; the select is '<base>_input'.
    """
    for lab in form.find_all("label"):
        if not isinstance(lab, Tag) or lab.get_text(strip=True) != label:
            continue
        target = str(lab.get("for") or "")
        if not target:
            continue
        base = re.sub(r"_focus$", "", target)
        sel = form.find("select", id=base + "_input") or form.find("select", attrs={"name": base + "_input"})
        if isinstance(sel, Tag):
            return sel
    return None


def select_options(sel: Tag) -> List[Tuple[str, str]]:
    """(value, label) pairs, without the empty and '(nicht gefüllt)' entries."""
    out = []
    for o in sel.find_all("option"):
        if not isinstance(o, Tag):
            continue
        value, label = str(o.get("value") or ""), o.get_text(strip=True)
        if value and label and not label.startswith("("):
            out.append((value, label))
    return out


def match_fach(options: List[Tuple[str, str]], query: str) -> List[Tuple[str, str]]:
    """Exact match (hyphen/space/case-insensitive) wins; else every substring match."""
    q = _norm(query)
    exact = [o for o in options if _norm(o[1]) == q]
    return exact or [o for o in options if q in _norm(o[1])]


def po_of(label: str) -> Optional[str]:
    m = re.search(r"\(PO (\d{4,5})", label)
    return m.group(1) if m else None


def po_sort_key(po: Optional[str]) -> int:
    """'2009' and '20232' on one scale: a 4-digit year counts as <year>0."""
    if not po:
        return -1
    return int(po) * 10 if len(po) == 4 else int(po)


def parse_result_rows(html: str) -> List[Dict[str, Optional[str]]]:
    """One dict per Prüfungsordnung: label (first Studiengang), po, url."""
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"railsUri=")):
        if not isinstance(a, Tag):
            continue
        href = str(a.get("href"))
        url = CAMPO + href if href.startswith("/") else href
        if url in seen or not _is_campo_url(url):
            continue
        seen.add(url)
        tr = a.find_parent("tr")
        li = tr.find("li") if isinstance(tr, Tag) else None
        label = li.get_text(" ", strip=True) if isinstance(li, Tag) else ""
        rows.append({"label": label, "po": po_of(label), "url": url})
    return rows


def extract_tree(html: str) -> List[Dict]:
    """The JSON element list of a curriculum page."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r'\[\{"id":', html):
        try:
            tree, _ = decoder.raw_decode(html, m.start())
        except ValueError:
            continue
        if any(isinstance(x, dict) and x.get("k_elementtype_id") == ELEMENT_PROGRAMME for x in tree):
            return tree
    raise ValueError("no curriculum tree in page")


def modules_from_tree(tree: List[Dict]) -> List[Dict]:
    """The programme's own modules in tree order: unit_id, nr, name, konto.

    Catalogue Konten (CATALOGUE_KONTO) are skipped. konto is the Konto path
    below the part every module shares (e.g. 'Bachelorprüfung > Gesamtkonto'),
    so a Wahlpflicht catalogue shows as its own name and a Pflicht module as ''.
    """
    by_id = {x["id"]: x for x in tree}
    kids: Dict[int, List[Dict]] = {}
    for x in tree:
        if x.get("parent_id") in by_id:
            kids.setdefault(x["parent_id"], []).append(x)
    found: List[Tuple[List[str], Dict]] = []

    def walk(node: Dict, path: List[str]) -> None:
        if CATALOGUE_KONTO.search(node.get("defaulttext") or ""):
            return
        if node.get("k_elementtype_id") == ELEMENT_MODULE:
            found.append((path, node))
            return
        if node.get("k_elementtype_id") == ELEMENT_KONTO:
            path = path + [(node.get("defaulttext") or "").strip()]
        for k in sorted(kids.get(node["id"], []), key=lambda k: k.get("sortorder") or 0):
            walk(k, path)

    for root in (x for x in tree if x.get("k_elementtype_id") == ELEMENT_PROGRAMME):
        walk(root, [])
    if not found:
        return []
    common = found[0][0]
    for path, _ in found[1:]:
        n = 0
        while n < min(len(common), len(path)) and common[n] == path[n]:
            n += 1
        common = common[:n]
    return [{"unit_id": node["id"], "nr": node.get("elementnr") or "",
             "name": (node.get("defaulttext") or "").strip(),
             "konto": " > ".join(path[len(common):])} for path, node in found]


def parse_relations(html: str) -> List[Dict]:
    """Rows of the 'Zuordnungsspezifische Leistungsinformationen' table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r"unitRelationDataTableTable$"))
    if not isinstance(table, Tag):
        return []
    heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    out = []
    for tr in table.find_all("tr"):
        if not isinstance(tr, Tag):
            continue
        tds = [td for td in tr.find_all("td") if isinstance(td, Tag)]
        if len(tds) != len(heads):
            continue
        row = dict(zip(heads, tds))
        prog = row.get("Studiengänge")
        programmes = ([li.get_text(" ", strip=True) for li in prog.find_all("li")] or [prog.get_text(" ", strip=True)]) \
            if isinstance(prog, Tag) else []
        text = {h: td.get_text(" ", strip=True) for h, td in row.items()}
        out.append({"konto": text.get("Konto") or "", "ects": _num(text.get("ECTS-Punkte")),
                    "pflicht": text.get("Pflicht-Kennzeichen") or "",
                    "fs": _num(text.get("Empfohlenes Fachsemester")), "programmes": programmes})
    return out


def pick_relation(relations: List[Dict], programme_label: str) -> Optional[Dict]:
    """The relation that names this programme; else the first one."""
    for r in relations:
        if any(_norm(p) == _norm(programme_label) for p in r["programmes"]):
            return r
    return relations[0] if relations else None


def filter_rows(rows: List[Dict], po: Optional[str] = None, abschluss: Optional[str] = None) -> List[Dict]:
    """Keep rows whose label contains *abschluss*; *po* is a version or 'latest'."""
    if abschluss:
        rows = [r for r in rows if _norm(abschluss) in _norm(r["label"] or "")]
    if po == "latest":
        if not rows:
            return rows
        best = max(po_sort_key(r["po"]) for r in rows)
        return [r for r in rows if po_sort_key(r["po"]) == best]
    if po:
        return [r for r in rows if r["po"] == po]
    return rows


# --- network -------------------------------------------------------------------

def _session() -> requests.Session:
    """Anonymous session: no Firefox cookies (see the module docstring)."""
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def _search_mask(s: requests.Session) -> Tag:
    resp = s.get(SEARCH_URL, timeout=60)
    form = BeautifulSoup(resp.text, "html.parser").find("form", {"id": "genericSearchMask"})
    if not isinstance(form, Tag):
        raise RuntimeError(f"Modulhandbuch search mask not found (HTTP {resp.status_code}).")
    return form


def list_faecher(session: Optional[requests.Session] = None) -> List[Tuple[str, str]]:
    sel = select_for_label(_search_mask(session or _session()), "Fach")
    return select_options(sel) if sel is not None else []


def search_programmes(fach: str, s: requests.Session) -> Tuple[str, List[Dict]]:
    """Resolve *fach* against the Fach select and return (fach label, result rows)."""
    form = _search_mask(s)
    sel = select_for_label(form, "Fach")
    if sel is None:
        raise RuntimeError("Fach field not found on the Modulhandbuch search mask.")
    hits = match_fach(select_options(sel), fach)
    if not hits:
        raise RuntimeError(f"Kein Fach passt zu '{fach}'.")
    if len(hits) > 1:
        raise RuntimeError(f"'{fach}' passt auf mehrere Fächer: " + "; ".join(h[1] for h in hits))
    value, label = hits[0]
    data = _collect_form(form)
    data[str(sel.get("name"))] = value
    data["SCROLL_TO_ANCHOR"] = ""
    data["DISABLE_AUTOSCROLL"] = "true"
    data["genericSearchMask:search"] = "Suchen"
    resp = s.post(CAMPO + str(form.get("action")), data=data, timeout=120)
    return label, parse_result_rows(resp.text)


def fetch_module_relations(unit_id: int, s: requests.Session) -> List[Dict]:
    """Two requests: the module's detail page, then its relation tab."""
    resp = s.get(DETAIL_URL.format(uid=int(unit_id)), timeout=60)
    form = BeautifulSoup(resp.text, "html.parser").find("form", {"id": "detailViewData"})
    if not isinstance(form, Tag):
        return []
    url = CAMPO + str(form.get("action"))
    if not _is_campo_url(url):
        return []
    data = _collect_form(form)
    data[RELATION_TAB] = "Zuordnungsspezifische Leistungsinformationen"
    data["DISABLE_VALIDATION"] = "true"
    return parse_relations(s.post(url, data=data, timeout=60).text)


def fetch_programmes(fach: str, po: Optional[str] = None, abschluss: Optional[str] = None,
                     ects: bool = False, session: Optional[requests.Session] = None) -> Tuple[str, List[Dict]]:
    """Return (fach label, programmes). Each programme: label, po, modules.

    With ects=True every module also gets ects, pflicht and fs from its relation
    tab (two extra requests per module).
    """
    s = session or _session()
    fach_label, rows = search_programmes(fach, s)
    programmes = []
    for row in sorted(filter_rows(rows, po, abschluss), key=lambda r: (r["label"][:5], po_sort_key(r["po"]))):
        resp = s.get(str(row["url"]), timeout=180)
        modules = modules_from_tree(extract_tree(resp.text))
        if ects:
            for m in modules:
                rel = pick_relation(fetch_module_relations(m["unit_id"], s), str(row["label"]))
                m.update({"ects": rel["ects"], "pflicht": rel["pflicht"], "fs": rel["fs"]} if rel
                         else {"ects": None, "pflicht": "", "fs": None})
        programmes.append({"label": row["label"], "po": row["po"], "modules": modules})
    return fach_label, programmes


# --- rendering -------------------------------------------------------------------

def _fmt(x: Optional[float]) -> str:
    if x is None:
        return ""
    return (f"{x:.1f}" if x % 1 else f"{x:.0f}").replace(".", ",")


def _pflicht_sum(modules: List[Dict]) -> Optional[float]:
    vals = [m.get("ects") for m in modules if m.get("pflicht") == "Pflicht"]
    return sum(v for v in vals if v is not None) if vals else None


def render_programme(p: Dict) -> str:
    mods = p["modules"]
    with_ects = any("ects" in m for m in mods)
    head = f"## {p['label']}\n\n{len(mods)} Module"
    if with_ects and _pflicht_sum(mods) is not None:
        head += f", davon Pflicht {_fmt(_pflicht_sum(mods))} ECTS"
    lines = [head, ""]
    if with_ects:
        lines += ["| Nr | Modul | Bereich | ECTS | Art | FS |", "|---|---|---|---|---|---|"]
        lines += [f"| {m['nr']} | {m['name']} | {m['konto']} | {_fmt(m.get('ects'))} | {m.get('pflicht', '')} | {_fmt(m.get('fs'))} |"
                  for m in mods]
    else:
        lines += ["| Nr | Modul | Bereich |", "|---|---|---|"]
        lines += [f"| {m['nr']} | {m['name']} | {m['konto']} |" for m in mods]
    return "\n".join(lines)


def short_label(label: str) -> str:
    """'B.Sc. Nanotechnologie (PO 20232)' -> 'Nanotechnologie 20232'."""
    name = re.sub(r"^[A-Z]\.\s?[A-Za-z]+\.\s*", "", label)
    name = re.sub(r"\s*\(PO.*$", "", name)
    po = po_of(label)
    return f"{name} {po}" if po else name


def render_comparison(programmes: List[Dict]) -> str:
    """One row per module number, one column per programme (ECTS or 'x').

    Modules in every programme come first, then the rest in first-seen order.
    A Wahlpflicht option is marked 'WP'.
    """
    cols = [short_label(p["label"]) for p in programmes]
    order: List[str] = []
    names: Dict[str, str] = {}
    cells: Dict[str, Dict[int, str]] = {}
    for i, p in enumerate(programmes):
        for m in p["modules"]:
            key = f"nr: {m['nr']}" if m["nr"] else m["name"]
            if key not in names:
                order.append(key)
                # 'Bachelorarbeit (B.Sc. X 20242)' is one row for every programme.
                names[key] = re.sub(r"\s*\([^()]*\d{4,5}\)$", "", m["name"])
            cell = _fmt(m.get("ects")) if m.get("ects") is not None else "x"
            if m.get("konto") or (m.get("pflicht") and m.get("pflicht") != "Pflicht"):
                cell += " WP"
            cells.setdefault(key, {})[i] = cell
    order.sort(key=lambda k: -len(cells[k]) if len(cells[k]) == len(programmes) else 0)
    lines = ["| Nr | Modul | " + " | ".join(cols) + " |", "|---|---|" + "---|" * len(cols)]
    for key in order:
        nr = key[4:] if key.startswith("nr: ") else ""
        lines.append(f"| {nr} | {names[key]} | " + " | ".join(cells[key].get(i, "") for i in range(len(cols))) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Module lists of FAU programmes per PO version (campo Modulhandbuch).")
    ap.add_argument("fach", nargs="*", help="Fach as campo names it, e.g. 'KI - Materialtechnologie' "
                                            "(hyphens/spaces/case don't matter; a unique substring works)")
    ap.add_argument("--po", help="PO version, e.g. 20232, or 'latest' (default: all versions)")
    ap.add_argument("--abschluss", help="keep programmes whose label contains this, e.g. 'B.Sc.'")
    ap.add_argument("--ects", action="store_true", help="add ECTS, Pflicht/Wahlpflicht and FS per module (slow)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list-faecher", metavar="TEXT", nargs="?", const="",
                    help="list the Fach names campo offers (optionally only those containing TEXT)")
    args = ap.parse_args()

    s = _session()
    if args.list_faecher is not None:
        for _, label in list_faecher(s):
            if _norm(args.list_faecher) in _norm(label):
                print(label)
        return 0
    if not args.fach:
        ap.error("give at least one Fach, or --list-faecher")
    programmes: List[Dict] = []
    for fach in args.fach:
        try:
            _, progs = fetch_programmes(fach, po=args.po, abschluss=args.abschluss, ects=args.ects, session=s)
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        programmes += progs
    if args.json:
        print(json.dumps(programmes, ensure_ascii=False, indent=1))
        return 0
    print("\n\n".join(render_programme(p) for p in programmes))
    if len(programmes) > 1:
        print("\n## Vergleich\n")
        print(render_comparison(programmes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
