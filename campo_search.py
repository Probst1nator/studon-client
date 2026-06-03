#!/usr/bin/env python3
"""Reusable driver for FAU campo course search (searchCourseNonStaff-flow).

campo is Apache-MyFaces + Spring WebFlow. Every render mints a fresh
_flowExecutionKey and the field client-ids carry per-render hashes, so a
reusable client MUST discover field names at runtime rather than hardcode them.

Flow:
  1. GET startFlow?_flowId=searchCourseNonStaff-flow   (plain GET, NO ajax header)
       -> parse form#genericSearchMask: action URL, all hidden inputs,
          the "Suchbegriffe" field (basic_data inputField_0), the Semester select.
  2. POST the whole form back to its action with:
       - <suchbegriffe field> = query
       - <termSelect>          = e.g. "eq|1|2026" (SoSe 2026) ; default = current
       - genericSearchMask:search = "Suchen"   (the clicked MyFaces command button)
       - SCROLL_TO_ANCHOR="" , DISABLE_AUTOSCROLL="true"  (oam.submitForm params)
  3. Parse table id="genSearchRes:...Table" rows -> course hits, each with a
       detailView-flow link (unitId, periodId).

Importable: studon_client.py reuses search_courses()/fetch_detail_ects() and
passes in its own requests.Session (its _campo_session()), so cookie handling
stays in one place. Run directly for standalone use (see __main__).
"""
import re
import argparse
import json
from typing import Optional, List, Dict
from urllib.parse import urlparse

import requests
from requests.cookies import RequestsCookieJar
import browser_cookie3
from bs4 import BeautifulSoup
from bs4.element import Tag

CAMPO = "https://www.campo.fau.de"
CAMPO_HOST = "campo.fau.de"
SEARCH_URL = CAMPO + "/qisserver/pages/startFlow.xhtml?_flowId=searchCourseNonStaff-flow"


def _is_campo_url(url: str) -> bool:
    """True only if *url*'s host is exactly campo.fau.de or a sub-domain.

    A real hostname check (never a substring test) so a crafted detail href in
    the JSF response can't redirect a cookie-bearing GET to another host.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return bool(host) and (host == CAMPO_HOST or host.endswith("." + CAMPO_HOST))


def _campo_session() -> requests.Session:
    """Build a requests.Session populated with current Firefox cookies for campo.

    SSO cookies live on fau.de; the app cookies on campo.fau.de — load both.
    """
    jar = RequestsCookieJar()
    got = 0
    for dom in ("campo.fau.de", "fau.de"):
        try:
            c = browser_cookie3.firefox(domain_name=dom)
            n = len(list(c))
            if n:
                jar.update(c)
                got += n
        except Exception:
            pass
    if not got:
        raise RuntimeError("No campo/fau.de Firefox cookies — log into campo.fau.de in Firefox.")
    s = requests.Session()
    s.cookies.update(jar)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def _find_field(form: Tag, fieldset: str, idx: int) -> Optional[str]:
    """Locate a free-text input by fieldset + inputField_<idx>, skipping
    the autocomplete companion inputs (_focus/_filter/_input) and help buttons."""
    for i in form.find_all("input", {"type": "text"}):
        if not isinstance(i, Tag):
            continue
        nm = str(i.get("name") or "")
        if fieldset in nm and f"inputField_{idx}_" in nm \
           and not nm.endswith(("_focus", "_filter", "_input")) and "help" not in nm:
            return nm
    return None


def _collect_form(form: Tag) -> Dict[str, str]:
    """All replayable name->value pairs (hidden inputs, text inputs, selects)."""
    data: Dict[str, str] = {}
    for i in form.find_all("input"):
        if not isinstance(i, Tag):
            continue
        nm = i.get("name")
        if not nm:
            continue
        nm = str(nm)
        if i.get("type") in ("checkbox", "radio"):
            if i.has_attr("checked"):
                data[nm] = str(i.get("value") or "on")
        else:
            data[nm] = str(i.get("value") or "")
    for sel in form.find_all("select"):
        if not isinstance(sel, Tag):
            continue
        nm = sel.get("name")
        if not nm:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        data[str(nm)] = str(opt.get("value") or "") if isinstance(opt, Tag) else ""
    return data


def search_courses(query: str, term: Optional[str] = None,
                   session: Optional[requests.Session] = None):
    """Return (term_label, [hits]). term e.g. 'eq|1|2026'(SoSe) / 'eq|2|2026'(WiSe);
    None keeps the page default (current semester).

    Pass *session* to reuse an existing campo-authenticated requests.Session
    (e.g. studon_client._campo_session()); otherwise one is built here.
    """
    s = session or _campo_session()
    soup = BeautifulSoup(s.get(SEARCH_URL, timeout=30).text, "html.parser")
    form = soup.find("form", {"id": "genericSearchMask"})
    if not isinstance(form, Tag):
        raise RuntimeError("genericSearchMask form not found (session expired / not logged in?).")
    action = form.get("action")
    post_url = CAMPO + str(action)

    such = _find_field(form, "cm_exa_eventprocess_basic_data", 0)
    if such is None:
        raise RuntimeError("Suchbegriffe field not found on search mask.")
    term_field = None
    for sel in form.find_all("select"):
        if isinstance(sel, Tag) and "termSelect" in str(sel.get("name") or ""):
            term_field = str(sel.get("name"))
            break
    term_label = None
    if term_field:
        sel = form.find("select", {"name": term_field})
        cur = sel.find("option", selected=True) if isinstance(sel, Tag) else None
        term_label = cur.get_text(strip=True) if isinstance(cur, Tag) else None

    data = _collect_form(form)
    data[such] = query
    if term and term_field:
        data[term_field] = term
        sel = form.find("select", {"name": term_field})
        opt = sel.find("option", value=term) if isinstance(sel, Tag) else None
        term_label = opt.get_text(strip=True) if isinstance(opt, Tag) else term
    data["SCROLL_TO_ANCHOR"] = ""
    data["DISABLE_AUTOSCROLL"] = "true"
    data["genericSearchMask:search"] = "Suchen"

    pr = s.post(post_url, data=data, timeout=60)
    res = BeautifulSoup(pr.text, "html.parser")
    if "keine Daten" in res.get_text(" ", strip=True):
        return term_label, []

    hits: List[Dict[str, Optional[str]]] = []
    table = res.find("table", id=re.compile(r"genSearchRes.*Table"))
    if not isinstance(table, Tag):
        return term_label, []
    for tr in table.find_all("tr"):
        if not isinstance(tr, Tag):
            continue
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        cells = [td.get_text(" ", strip=True) for td in tds]
        # column layout: [marker, SemUnabhTitel, Kurztext, SemAbhTitel, Art, DozVerantw, DozDurchf, Org, Aktionen]
        detail = None
        for a in tr.find_all("a", href=True):
            if isinstance(a, Tag) and "detailView-flow" in str(a.get("href")):
                detail = CAMPO + str(a.get("href"))
                break
        uid = pid = None
        if detail:
            m = re.search(r"unitId=(\d+).*?periodId=(\d+)", detail)
            if m:
                uid, pid = m.group(1), m.group(2)
        hits.append({
            "title": cells[1] or (cells[3] if len(cells) > 3 else ""),
            "kurztext": cells[2] if len(cells) > 2 else "",
            "art": cells[4] if len(cells) > 4 else "",
            "dozent": cells[5] if len(cells) > 5 else "",
            "org": cells[7] if len(cells) > 7 else "",
            "unitId": uid, "periodId": pid, "detail_url": detail,
        })
    return term_label, hits


def fetch_detail_ects(detail_url: Optional[str],
                      session: Optional[requests.Session] = None) -> Optional[str]:
    """Follow a detailView-flow link; pull the ECTS value (best-effort).

    Validates the host first — only campo.fau.de URLs are fetched.
    """
    if not detail_url or not _is_campo_url(detail_url):
        return None
    s = session or _campo_session()
    txt = re.sub(r"\s+", " ", BeautifulSoup(s.get(detail_url, timeout=30).text, "html.parser").get_text(" ", strip=True))
    m = re.search(r"ECTS[- ]?(?:Credits|Punkte)?\s*:?\s*([0-9]+(?:[.,][0-9]+)?)", txt)
    return m.group(1) if m else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FAU campo course search")
    ap.add_argument("query")
    ap.add_argument("--term", help="e.g. 'eq|1|2026' (SoSe26) or 'eq|2|2026' (WiSe26)")
    ap.add_argument("--ects", action="store_true", help="follow each hit's detail page for ECTS")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    term, hits = search_courses(a.query, term=a.term)
    if a.ects:
        for h in hits:
            if h["detail_url"]:
                h["ects"] = fetch_detail_ects(h["detail_url"])
    if a.json:
        print(json.dumps({"term": term, "hits": hits}, ensure_ascii=False, indent=2))
    else:
        print(f"Semester: {term} — {len(hits)} Treffer\n")
        for h in hits:
            line = f"• {h['title']}  [{h['art']}]  — {h['dozent']}"
            if h.get("ects"):
                line += f"  ({h['ects']} ECTS)"
            print(line)
            if h["unitId"]:
                print(f"    unitId={h['unitId']} periodId={h['periodId']}")
