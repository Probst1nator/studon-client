"""Tests for the campo Modulhandbuch parsers (campo_modulhandbuch.py).

All pages are synthetic: invented Fächer, ids and module names, shaped like the
campo markup the parsers look for. Run with: pytest -q
"""
import json
import os
import sys

import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campo_modulhandbuch as mh  # noqa: E402

MASK = """<form id="genericSearchMask">
<label for="mask:inputField_2_ab:id9_focus">Fach</label>
<input type="text" id="mask:inputField_2_ab:id9_focus" name="mask:inputField_2_ab:id9_focus">
<select id="mask:inputField_2_ab:id9_input" name="mask:inputField_2_ab:id9_input">
  <option value=""></option><option value="isnull">(nicht gefüllt)</option>
  <option value="11">Beispiel - Technik</option><option value="12">Beispieltechnik Plus</option>
  <option value="13">Anderes Fach</option>
</select></form>"""

LINK = ("/qisserver/pages/startFlow.xhtml?_flowId=searchCourseOfStudyForModuleDescription-flow"
        "&amp;_flowExecutionKey=e1s2&amp;railsUri=/emb/unit/show_module_description_struct/{id}")
RESULTS = f"""<table id="res"><tr><th>Studiengang</th></tr>
<tr><td><a href="{LINK.format(id=1)}">x</a></td>
    <td><ol><li>B.Sc. Beispiel (PO 20232)</li><li>B.Sc. Beispiel (PO 20232, Zweitstudium)</li></ol></td>
    <td><a href="{LINK.format(id=1)}">x</a></td></tr>
<tr><td><a href="{LINK.format(id=2)}">x</a></td><td><ol><li>B.Sc. Beispiel (PO 2009)</li></ol></td></tr>
<tr><td><a href="https://evil.example/x?railsUri=/emb/3">x</a></td><td><ol><li>B.Sc. Beispiel (PO 20242)</li></ol></td></tr>
</table>"""


def _el(id_, parent, kind, text, nr=None, sort=0):
    return {"id": id_, "parent_id": parent, "k_elementtype_id": kind, "defaulttext": text,
            "elementnr": nr, "sortorder": sort}


TREE = [
    _el(1, None, 4, "Bachelor of Science Beispiel PO-Version 20232"),
    _el(2, 1, 3, "Bachelorprüfung"),
    _el(3, 2, 3, "Gesamtkonto"),
    _el(4, 3, 8, "Grundlagen A", "100", 1),
    _el(5, 3, 3, "Wahlpflichtmodul Daten", sort=2),
    _el(6, 5, 8, "Option B", "200"),
    _el(7, 6, 2, "Option B (Klausur)", "201"),
    _el(8, 2, 3, "Zusatzleistungen", sort=9),
    _el(9, 8, 8, "Sprachkurs", "900"),
]

RELATIONS = """<table id="detailViewData:x:unitRelationDataTableTable"><thead><tr>
<th>Konto</th><th>ECTS-Punkte</th><th>Pflicht-Kennzeichen</th><th>Empfohlenes Fachsemester</th><th>Studiengänge</th>
</tr></thead><tbody>
<tr><td>Gesamtkonto</td><td>7,5</td><td>Pflicht</td><td>2,0</td><td><ol><li>B.Sc. Andere (PO 20232)</li></ol></td></tr>
<tr><td>Wahlpflicht</td><td>5,0</td><td>Wahlpflicht</td><td>3,0</td><td><ol><li>B.Sc. Beispiel (PO 20232)</li></ol></td></tr>
</tbody></table>"""


def _fach_options():
    form = BeautifulSoup(MASK, "html.parser").find("form")
    assert isinstance(form, Tag)
    sel = mh.select_for_label(form, "Fach")
    assert sel is not None
    return mh.select_options(sel)


def test_fach_select_behind_the_label():
    assert _fach_options() == [("11", "Beispiel - Technik"), ("12", "Beispieltechnik Plus"),
                               ("13", "Anderes Fach")]


def test_fach_match_ignores_hyphens_spaces_and_case():
    opts = _fach_options()
    assert mh.match_fach(opts, "beispiel-technik") == [("11", "Beispiel - Technik")]
    assert [o[0] for o in mh.match_fach(opts, "beispiel")] == ["11", "12"]
    assert mh.match_fach(opts, "Physik") == []


def test_result_rows_one_per_po_on_campo_only():
    rows = mh.parse_result_rows(RESULTS)
    assert [(r["label"], r["po"]) for r in rows] == [("B.Sc. Beispiel (PO 20232)", "20232"),
                                                     ("B.Sc. Beispiel (PO 2009)", "2009")]
    url = str(rows[0]["url"])
    assert url.startswith("https://www.campo.fau.de/qisserver/")
    assert "&_flowExecutionKey=e1s2&" in url


def test_filter_latest_compares_four_and_five_digit_versions():
    rows = [{"label": "B.Sc. X (PO 2009)", "po": "2009"}, {"label": "B.Sc. X (PO 20232)", "po": "20232"},
            {"label": "M.Sc. X (PO 20252)", "po": "20252"}]
    assert [r["po"] for r in mh.filter_rows(rows, po="latest", abschluss="b.sc.")] == ["20232"]
    assert [r["po"] for r in mh.filter_rows(rows, po="2009")] == ["2009"]


def test_tree_json_after_the_form():
    page = '<form id="f"><input name="a" value="1"/></form>' + json.dumps(TREE) + "<div>tail</div>"
    assert mh.extract_tree(page) == TREE
    with pytest.raises(ValueError):
        mh.extract_tree("<html>no tree</html>")


def test_modules_skip_catalogues_and_keep_the_wahlpflicht_konto():
    mods = mh.modules_from_tree(TREE)
    assert [(m["nr"], m["name"], m["konto"]) for m in mods] == [
        ("100", "Grundlagen A", ""), ("200", "Option B", "Wahlpflichtmodul Daten")]
    assert mods[0]["unit_id"] == 4


def test_relation_for_this_programme():
    rel = mh.parse_relations(RELATIONS)
    assert [r["ects"] for r in rel] == [7.5, 5.0]
    picked = mh.pick_relation(rel, "B.Sc. Beispiel (PO 20232)")
    assert picked is not None
    assert (picked["ects"], picked["pflicht"], picked["fs"]) == (5.0, "Wahlpflicht", 3.0)
    fallback = mh.pick_relation(rel, "B.Sc. Unbekannt (PO 20232)")
    assert fallback is not None and fallback["ects"] == 7.5


def test_comparison_puts_shared_modules_first():
    a = {"label": "B.Sc. Alpha (PO 20232)", "modules": [
        {"nr": "1", "name": "Nur Alpha", "konto": "", "ects": 5.0, "pflicht": "Pflicht"},
        {"nr": "2", "name": "Bachelorarbeit (B.Sc. Alpha 20232)", "konto": "", "ects": 15.0, "pflicht": "Pflicht"}]}
    b = {"label": "B.Sc. Beta (PO 20242)", "modules": [
        {"nr": "2", "name": "Bachelorarbeit (B.Sc. Beta 20242)", "konto": "", "ects": 15.0, "pflicht": "Pflicht"},
        {"nr": "3", "name": "Wahl Beta", "konto": "Wahlpflicht", "ects": 7.5, "pflicht": "Wahlpflicht"}]}
    lines = mh.render_comparison([a, b]).splitlines()
    assert lines[0] == "| Nr | Modul | Alpha 20232 | Beta 20242 |"
    assert lines[2] == "| 2 | Bachelorarbeit | 15 | 15 |"
    assert lines[3] == "| 1 | Nur Alpha | 5 |  |"
    assert lines[4] == "| 3 | Wahl Beta |  | 7,5 WP |"
