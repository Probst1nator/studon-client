"""Course pages as data: access state and lecture dates.

Pure parsing, no network. `studon_client.py` fetches the pages and calls these
functions for `--courses`, `--read` and `--add`; the tests feed them synthetic
HTML.

Access states, as StudOn shows them to the logged-in user:

| state     | meaning                                                      |
|-----------|--------------------------------------------------------------|
| member    | you are in the course; the client can download it            |
| pending   | a membership request is waiting for the course admin         |
| password  | joining needs the course password                            |
| request   | joining needs a request the admin has to accept              |
| direct    | you can join with one click                                  |
| campo     | StudOn takes no members; membership comes from campo         |
| blocked   | the course lists prerequisites you do not meet               |
| login     | the Firefox cookies are not logged into StudOn               |
| unknown   | none of the above matched                                    |
"""
import re
from datetime import date, timedelta
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

STUDON_COURSE_URL = 'https://www.studon.fau.de/studon/ilias.php?baseClass=ilrepositorygui&ref_id={ref}'
STUDON_INFO_URL = STUDON_COURSE_URL + '&cmd=infoScreen'

STATES = ('member', 'pending', 'password', 'request', 'direct', 'campo',
          'blocked', 'login', 'unknown')

_MONTHS = {
    'jan': 1, 'feb': 2, 'mär': 3, 'mrz': 3, 'mar': 3, 'apr': 4, 'mai': 5,
    'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'oct': 10,
    'nov': 11, 'dez': 12, 'dec': 12,
}
_RELATIVE_DAYS = {'Gestern': -1, 'Heute': 0, 'Morgen': 1}
_WEEKDAY = r'(?:Mo|Di|Mi|Do|Fr|Sa|So)'
_DATE = r'\d{1,2}\. [A-Za-zäÄ]+\.? \d{4}'
_DAY = r'(?:Gestern|Heute|Morgen|' + _DATE + r')'
_ACADEMIC = r'(?: [cs]\.t\.)?'  # "c.t." / "s.t." after a time


def ref_id_of(url: str) -> Optional[str]:
    """The StudOn ref_id in a course link (`ref_id=N` or `target=crs_N`)."""
    match = (re.search(r'[?&]ref_id=(\d+)', url or '')
             or re.search(r'target=(?:crs|grp)_(\d+)', url or ''))
    return match.group(1) if match else None


def course_url(ref_id: str) -> str:
    """The canonical StudOn link for a course; it opens the content tab for members."""
    return STUDON_COURSE_URL.format(ref=ref_id)


def page_text(html: str) -> str:
    """The main column of a StudOn page as one whitespace-collapsed line."""
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find(id='il_center_col') or soup.body or soup
    for tag in main.find_all(['script', 'style']):
        tag.decompose()
    return re.sub(r'\s+', ' ', main.get_text(' ', strip=True))


def page_lines(html: str) -> List[str]:
    """The main column of a StudOn page as text lines, for reading and grepping."""
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find(id='il_center_col') or soup.body or soup
    for tag in main.find_all(['script', 'style']):
        tag.decompose()
    lines = (re.sub(r'\s+', ' ', line).strip() for line in main.get_text('\n').splitlines())
    return [line for line in lines if line]


def page_header(html: str) -> Dict[str, str]:
    """Title and description from the ILIAS page header."""
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.find('h1', class_='il-page-content-header')
    desc = soup.select_one('.ilHeaderDesc')
    return {
        'title': title.get_text(' ', strip=True) if title else '',
        'description': desc.get_text(' ', strip=True) if desc else '',
    }


def classify_course_page(html: str, final_url: str) -> Dict[str, str]:
    """Access state of a course page, plus the join deadline when one is shown.

    `html` is the page a GET on the course link ends on, `final_url` its URL
    after redirects. The order of the checks matters: the page of a pending
    request still carries the "Sie müssen einen Aufnahmeantrag stellen" text.
    """
    result = {'state': 'unknown', 'deadline': ''}
    url = final_url or ''
    if 'ilstartupgui' in url.lower() or 'login.php' in url.lower():
        result['state'] = 'login'
        return result
    if re.search(r'(Kurs|Gruppen)mitgliedschaft beenden', html):
        result['state'] = 'member'
        return result
    if 'cmd=leaveWaitList' in html:
        result['state'] = 'pending'
        return result

    text = page_text(html)
    deadline = re.search(r'Anmeldungsende:?\s*(' + _DATE + r'(?:,\s*\d{1,2}:\d{2})?)', text)
    if deadline:
        result['deadline'] = deadline.group(1)

    if re.search(r'Voraussetzungen\s*»?\s*Nicht erfüllt', text):
        result['state'] = 'blocked'
    elif 'Beitritt mit Kurspasswort' in text:
        result['state'] = 'password'
    elif 'Beitritt nach Bestätigung' in text or 'Sie müssen einen Aufnahmeantrag stellen' in text:
        result['state'] = 'request'
    elif 'Direkter Beitritt' in text:
        result['state'] = 'direct'
    elif 'Zur Zeit ist keine Aufnahme möglich' in text:
        result['state'] = 'campo'
    return result


def _parse_date(token: str, today: date) -> Optional[date]:
    """A StudOn date ("19. Okt 2026", "Heute", "Morgen") as a date."""
    if token in _RELATIVE_DAYS:
        return today + timedelta(days=_RELATIVE_DAYS[token])
    match = re.match(r'(\d{1,2})\. ([A-Za-zäÄ]+)\.? (\d{4})$', token)
    if not match:
        return None
    month = _MONTHS.get(match.group(2)[:3].lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def termine_section(text: str) -> str:
    """The "Termine" section of an info screen, taken from `page_text()` output."""
    match = re.search(r'Termine (.*?)(?: Für Kursadministration| Zusätzliche Informationen'
                      r'| Nutzer mit Zugriff|$)', text)
    return match.group(1) if match else ''


def parse_termine(text: str, today: date) -> Dict:
    """Weekly rules and single dates from an info screen's "Termine" section.

    `text` is the `page_text()` of the info screen. Returns
    `{'rules': [...], 'dates': [...], 'first_held': ..., 'next_held': ...}`.
    A date counts as cancelled when ", Termin fällt aus" follows it. StudOn
    writes yesterday, today and tomorrow as "Gestern", "Heute" and "Morgen";
    `today` turns those into dates.
    """
    section = termine_section(text)
    plan, _, single = section.partition('Einzeltermine')

    rules = []
    for m in re.finditer(r'([A-ZÄÖÜ][\w-]*), (' + _WEEKDAY + r') (\d{1,2}:\d{2}) - (\d{1,2}:\d{2})'
                         + _ACADEMIC + r', (' + _DAY + r') - (' + _DAY + r')', plan):
        start, end = _parse_date(m.group(5), today), _parse_date(m.group(6), today)
        rules.append({
            'rhythm': m.group(1), 'weekday': m.group(2),
            'start': m.group(3), 'end': m.group(4),
            'from': start.isoformat() if start else m.group(5),
            'until': end.isoformat() if end else m.group(6),
        })

    dates = []
    for m in re.finditer(r'(' + _WEEKDAY + r') (' + _DAY + r'), (\d{1,2}:\d{2}) - (\d{1,2}:\d{2})'
                         + _ACADEMIC + r'(\s*,\s*Termin fällt aus)?', single):
        day = _parse_date(m.group(2), today)
        if day is None:
            continue
        dates.append({
            'date': day.isoformat(), 'weekday': m.group(1),
            'start': m.group(3), 'end': m.group(4),
            'cancelled': bool(m.group(5)),
        })

    held = [d for d in dates if not d['cancelled']]
    upcoming = [d for d in held if d['date'] >= today.isoformat()]
    return {
        'rules': rules,
        'dates': dates,
        'first_held': held[0] if held else None,
        'next_held': upcoming[0] if upcoming else None,
    }
