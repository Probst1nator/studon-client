"""Tests for course status, lecture dates and registration by ref_id.

All pages are synthetic: invented titles, ref_ids and dates, shaped like the
StudOn markup the parsers look for. Run with: pytest -q
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import studon_client as s  # noqa: E402
import studon_courses as c  # noqa: E402


COURSE = "https://www.studon.fau.de/studon/ilias.php?baseClass=ilrepositorygui&ref_id=1000001"
REGISTRATION = ("https://www.studon.fau.de/studon/ilias.php?baseClass=ilrepositorygui"
                "&cmdNode=1a:b2:cd&cmdClass=ilCourseRegistrationGUI&cmd=show&ref_id=1000001")
INFO = COURSE + "&cmd=infoScreen"


def _page(body, title="Beispielkurs", tabs=""):
    return (f'<html><body><div id="ilTab">{tabs}</div>'
            f'<h1 class="il-page-content-header">{title}</h1>'
            f'<div class="ilHeaderDesc">Vorlesung, BSP, 2 SWS</div>'
            f'<div id="il_center_col">{body}<script>var x = "Direkter Beitritt";</script></div>'
            f'</body></html>')


# --- access state ------------------------------------------------------------

def test_member_page():
    html = _page("<p>Ordner</p>", tabs='<a href="#">Inhalt</a><a href="#">Kursmitgliedschaft beenden</a>')
    assert c.classify_course_page(html, COURSE)["state"] == "member"


def test_pending_request_wins_over_the_request_text():
    html = _page('<p>Aufnahmeverfahren Sie müssen einen Aufnahmeantrag stellen.</p>',
                 tabs='<a href="ilias.php?cmdClass=ilcourseregistrationgui&cmd=leaveWaitList&ref_id=1000001">'
                      'Anmeldung ändern</a>')
    assert c.classify_course_page(html, INFO)["state"] == "pending"


def test_password_with_deadline():
    html = _page("<p>Anmeldungszeitraum Anmeldungsende: 3. Nov 2031, 08:00</p>"
                 "<p>Aufnahmeverfahren Beitritt mit Kurspasswort</p>")
    assert c.classify_course_page(html, REGISTRATION) == {"state": "password", "deadline": "3. Nov 2031, 08:00"}


def test_password_without_deadline():
    html = _page("<p>Anmeldungszeitraum Unbegrenzt Aufnahmeverfahren Beitritt mit Kurspasswort</p>")
    assert c.classify_course_page(html, REGISTRATION) == {"state": "password", "deadline": ""}


def test_request_after_confirmation():
    html = _page("<p>Aufnahmeverfahren Beitritt nach Bestätigung</p>")
    assert c.classify_course_page(html, REGISTRATION)["state"] == "request"


def test_direct_join_for_other_programmes_is_a_request():
    html = _page("<p>Aufnahmeverfahren Direkter Beitritt (sonst Aufnahmeantrag) für folgende Studierende: "
                 "Beispielfach, 1. bis 3. Fachsemester</p><p>Sie müssen einen Aufnahmeantrag stellen.</p>")
    assert c.classify_course_page(html, REGISTRATION)["state"] == "request"


def test_direct_join():
    html = _page("<p>Anmeldungszeitraum Unbegrenzt Aufnahmeverfahren Direkter Beitritt</p>")
    assert c.classify_course_page(html, REGISTRATION)["state"] == "direct"


def test_campo_managed():
    html = _page("<p>Aufnahmeverfahren Zur Zeit ist keine Aufnahme möglich.</p>"
                 "<p>Voraussetzungen » Erfüllt</p>")
    assert c.classify_course_page(html, INFO)["state"] == "campo"


def test_blocked_by_prerequisites():
    html = _page("<p>Aufnahmeverfahren Zur Zeit ist keine Aufnahme möglich.</p>"
                 "<p>Voraussetzungen » Nicht erfüllt</p>")
    assert c.classify_course_page(html, INFO)["state"] == "blocked"


def test_login_redirect():
    url = "https://www.studon.fau.de/studon/ilias.php?baseClass=ilstartupgui&cmd=jumpToLogin"
    assert c.classify_course_page(_page("<p>Anmelden</p>"), url)["state"] == "login"


def test_script_text_does_not_count():
    """The test page carries "Direkter Beitritt" only inside a <script>."""
    assert c.classify_course_page(_page("<p>Willkommen</p>"), COURSE)["state"] == "unknown"


# --- dates -------------------------------------------------------------------

TERMINE = ("Termine Terminplan Wöchentlich, Di 10:15 - 11:45 c.t., Heute - 2. Feb 2032, A. Muster (amuster) "
           "Wöchentlich, Do 10:15 - 11:45, 14. Okt 2031 - 5. Feb 2032 "
           "Einzeltermine Di Heute, 10:15 - 11:45 , Termin fällt aus "
           "Do Morgen, 10:15 - 11:45 "
           "Di 19. Okt 2031, 10:15 - 11:45 "
           "Di 28. Dez 2031, 10:15 - 11:45 , Termin fällt aus "
           "Zusätzliche Informationen StudOn-ID 1000001")


def test_rules_with_relative_dates_and_academic_quarter():
    result = c.parse_termine(TERMINE, today=date(2031, 10, 12))
    assert [(r["weekday"], r["start"], r["from"], r["until"]) for r in result["rules"]] == [
        ("Di", "10:15", "2031-10-12", "2032-02-02"),
        ("Do", "10:15", "2031-10-14", "2032-02-05"),
    ]


def test_dates_cancellations_and_first_held():
    result = c.parse_termine(TERMINE, today=date(2031, 10, 12))
    assert [(d["date"], d["cancelled"]) for d in result["dates"]] == [
        ("2031-10-12", True), ("2031-10-13", False), ("2031-10-19", False), ("2031-12-28", True),
    ]
    assert result["first_held"]["date"] == "2031-10-13"
    assert result["next_held"]["date"] == "2031-10-13"


def test_next_held_skips_past_dates():
    result = c.parse_termine(TERMINE, today=date(2031, 10, 15))
    # "Heute"/"Morgen" now resolve against the 15th, the absolute dates stay.
    assert result["first_held"]["date"] == "2031-10-16"
    assert result["next_held"]["date"] == "2031-10-16"
    assert c.parse_termine(TERMINE, today=date(2031, 10, 20))["next_held"]["date"] == "2031-10-21"


def test_no_dates_section():
    result = c.parse_termine("Lehrveranstaltung Titel Praktikum", today=date(2031, 10, 12))
    assert result == {"rules": [], "dates": [], "first_held": None, "next_held": None}


# --- links -------------------------------------------------------------------

def test_ref_id_of():
    assert c.ref_id_of(COURSE) == "1000001"
    assert c.ref_id_of("https://www.studon.fau.de/studon/goto.php?target=crs_1000002") == "1000002"
    assert c.ref_id_of("https://www.studon.fau.de/campo/course/123") is None


def test_course_target_url():
    assert s._course_target_url("1000001") == COURSE
    assert s._course_target_url("https://www.studon.fau.de/campo/course/123") == \
        "https://www.studon.fau.de/campo/course/123"
    assert s._course_target_url("https://www.campo.fau.de/qisserver/pages/x") is None
    assert s._course_target_url("https://studon.fau.de.example.com/?ref_id=1") is None


# --- --add -------------------------------------------------------------------

def _fake_probe(states):
    def probe(_session, target):
        return {"target": target, "ref_id": target, "title": f"Kurs {target}", "state": states[target],
                "deadline": "", "url": c.course_url(target), "error": ""}
    return probe


def test_add_tracks_members_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(s, "_make_session", lambda: object())
    monkeypatch.setattr(s, "_probe_course", _fake_probe({"1000001": "member", "1000002": "password"}))
    monkeypatch.setattr(s, "extract_course_title", lambda url, session, debug=False: f"Kurs {c.ref_id_of(url)}")

    s.run_add_courses(["1000001", "1000002"], fetch=False)

    assert [(src, os.path.basename(folder)) for _meta, src, folder in s.find_all_metadata_files(str(tmp_path))] \
        == [(c.course_url("1000001"), "Kurs 1000001")]
    assert not (tmp_path / "Kurs 1000002").exists()
    out = capsys.readouterr().out
    assert "1000002" in out and "join on StudOn first" in out


def test_add_skips_a_course_that_is_already_tracked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(s, "_make_session", lambda: object())
    monkeypatch.setattr(s, "_probe_course", _fake_probe({"1000001": "member"}))
    monkeypatch.setattr(s, "extract_course_title", lambda url, session, debug=False: "Kurs 1000001")
    s.run_add_courses(["1000001"], fetch=False)
    s.run_add_courses(["1000001"], fetch=False)
    assert "already tracked" in capsys.readouterr().out
    assert len(s.find_all_metadata_files(str(tmp_path))) == 1
