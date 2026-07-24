"""Security regression tests for studon_client.

Covers the path-traversal, archive-extraction, domain-validation and
git-handling hardening from the 2026-05 security audit. Run with: pytest -q
"""
import io
import os
import sys
import subprocess
import tarfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import studon_client as s  # noqa: E402


# --- F6: clean_filename must not emit path-traversal components ------------

def test_clean_filename_rejects_traversal_components():
    """A remote-controlled link text of '.' or '..' must not survive as a name."""
    assert s.clean_filename('..') == ''
    assert s.clean_filename('.') == ''


def test_clean_filename_keeps_normal_names():
    """Ordinary filenames are untouched by the traversal guard."""
    assert s.clean_filename('Lecture 1.pdf') == 'Lecture 1.pdf'
    assert s.clean_filename('Übung_3') == 'Übung_3'


# --- F3: host validation must use real parsing, not substring matching -----

def test_url_host_matches_accepts_studon_and_subdomains():
    assert s._url_host_matches('https://studon.fau.de/x', 'studon.fau.de')
    assert s._url_host_matches('https://www.studon.fau.de/studon/x', 'studon.fau.de')


def test_url_host_matches_rejects_substring_lookalikes():
    """The exact attacker bypasses the old `domain in url` check missed."""
    assert not s._url_host_matches('https://studon.fau.de.attacker.com/x', 'studon.fau.de')
    assert not s._url_host_matches('https://attacker.com/?studon.fau.de', 'studon.fau.de')
    assert not s._url_host_matches('https://evilstudon.fau.de/x', 'studon.fau.de')
    assert not s._url_host_matches('not-a-url', 'studon.fau.de')


class _FakeResponse:
    def __init__(self, text, url):
        self.text = text
        self.url = url

    def raise_for_status(self):
        pass


class _FakeSession:
    """Test double for the network boundary: serves canned HTML per URL."""
    def __init__(self, pages):
        self._pages = pages
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return _FakeResponse(self._pages.get(url, '<html></html>'), url)


def test_crawler_ignores_off_domain_file_links():
    """A crafted off-domain `cmd=sendfile` link must not be queued for download."""
    start = 'https://www.studon.fau.de/course'
    page = ('<html><body>'
            '<a href="https://studon.fau.de.attacker.com/payload.zip?cmd=sendfile">payload</a>'
            '</body></html>')
    files = []
    s.discover_items_recursive(start, '/tmp/x', _FakeSession({start: page}), files)
    assert files == []


def test_crawler_keeps_on_domain_file_links():
    """A genuine StudOn file link is still queued (no regression)."""
    start = 'https://www.studon.fau.de/course'
    page = ('<html><body>'
            '<a href="https://www.studon.fau.de/studon/goto.php?cmd=sendfile&x=1">slides</a>'
            '</body></html>')
    files = []
    s.discover_items_recursive(start, '/tmp/x', _FakeSession({start: page}), files)
    assert len(files) == 1


def test_feedback_discovery_ignores_off_domain_links():
    """discover_feedback_files used a substring check — verify it is host-based now."""
    exc = 'https://www.studon.fau.de/studon/go/exc/1/2'
    page = ('<html><body>'
            '<a href="https://studon.fau.de.attacker.com/f.pdf?cmd=sendfile">feedback</a>'
            '</body></html>')
    found = s.discover_feedback_files(exc, _FakeSession({exc: page}))
    assert found == []


def test_crawler_refuses_off_domain_start_url():
    """An off-domain start URL is never even fetched by the crawler."""
    evil = 'https://studon.fau.de.attacker.com/course'
    sess = _FakeSession({evil: '<html><body>x</body></html>'})
    files = []
    s.discover_items_recursive(evil, '/tmp/x', sess, files)
    assert files == []
    assert sess.requested == []


def test_extract_course_title_refuses_off_domain():
    """An off-domain URL is not fetched when extracting a course title."""
    evil = 'https://studon.fau.de.attacker.com/course'
    sess = _FakeSession({evil: '<html><h1>Pwned Title</h1></html>'})
    assert s.extract_course_title(evil, sess) is None
    assert sess.requested == []


def test_resolve_studon_course_url_rejects_off_domain_final():
    """A campo->studon link that redirects off-domain resolves to None."""
    evil = 'https://studon.fau.de.attacker.com/redir'
    assert s._resolve_studon_course_url(_FakeSession({evil: ''}), evil) is None


# --- F5: the generated "Link to StudOn.html" must escape interpolated data --

def test_course_link_file_escapes_source_url(tmp_path):
    """A crafted source_url must not inject raw HTML/JS into the link file."""
    evil = 'https://x/"><script>alert(1)</script>'
    s.create_course_link_file(tmp_path, 'Course', evil)
    html = (tmp_path / 'Link to StudOn.html').read_text(encoding='utf-8')
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_course_link_file_escapes_course_title(tmp_path):
    """A crafted course title must not break out of the HTML either."""
    s.create_course_link_file(tmp_path, '"><script>x</script>', 'https://www.studon.fau.de/c')
    html = (tmp_path / 'Link to StudOn.html').read_text(encoding='utf-8')
    assert '<script>x</script>' not in html


# --- F2: archive extraction must not escape the extraction directory ------

def test_is_safe_archive_member():
    base = '/tmp/extract'
    assert s._is_safe_archive_member('notes/a.txt', base)
    assert s._is_safe_archive_member('a.txt', base)
    assert not s._is_safe_archive_member('../escaped.txt', base)
    assert not s._is_safe_archive_member('../../escaped.txt', base)
    assert not s._is_safe_archive_member('/etc/passwd', base)
    assert not s._is_safe_archive_member('nested/../../escaped.txt', base)


def test_extract_archive_blocks_tar_path_traversal(tmp_path):
    """A tar member with '../' must not write outside the extraction dir."""
    course = tmp_path / 'course'
    course.mkdir()
    archive = course / 'malware.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        data = b'pwned'
        info = tarfile.TarInfo('../../escaped.txt')
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    s.extract_archive(str(archive))
    assert not (tmp_path / 'escaped.txt').exists()


def test_extract_archive_blocks_tar_symlink_escape(tmp_path):
    """A tar symlink pointing outside the dir must not be followed."""
    course = tmp_path / 'course'
    course.mkdir()
    secret = tmp_path / 'secret'
    secret.mkdir()
    archive = course / 'm.tar'
    with tarfile.open(archive, 'w') as tf:
        link = tarfile.TarInfo('escape')
        link.type = tarfile.SYMTYPE
        link.linkname = str(secret)
        tf.addfile(link)
        data = b'owned'
        f = tarfile.TarInfo('escape/owned.txt')
        f.size = len(data)
        tf.addfile(f, io.BytesIO(data))
    s.extract_archive(str(archive))
    assert not (secret / 'owned.txt').exists()


def test_extract_archive_blocks_zip_path_traversal(tmp_path):
    """A zip member with '../' must not write outside the extraction dir."""
    course = tmp_path / 'course'
    course.mkdir()
    archive = course / 'm.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('../../escaped_zip.txt', 'pwned')
    s.extract_archive(str(archive))
    assert not (tmp_path / 'escaped_zip.txt').exists()


def test_extract_archive_extracts_safe_zip(tmp_path):
    """A normal archive still extracts correctly (no regression)."""
    course = tmp_path / 'course'
    course.mkdir()
    archive = course / 'good.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('notes/lecture.txt', 'hello')
    assert s.extract_archive(str(archive)) is True
    assert (course / 'good' / 'notes' / 'lecture.txt').read_text() == 'hello'


def test_extract_archive_extracts_safe_tar(tmp_path):
    """A normal tar archive still extracts correctly via the hardened path."""
    course = tmp_path / 'course'
    course.mkdir()
    archive = course / 'good.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        data = b'tar-hello'
        info = tarfile.TarInfo('notes/lecture.txt')
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    assert s.extract_archive(str(archive)) is True
    assert (course / 'good' / 'notes' / 'lecture.txt').read_bytes() == b'tar-hello'


def test_extract_archive_extracts_safe_7z(tmp_path):
    """A normal 7z archive still extracts correctly (exercises the 7z branch)."""
    import pytest
    py7zr = pytest.importorskip('py7zr')
    course = tmp_path / 'course'
    course.mkdir()
    archive = course / 'good.7z'
    with py7zr.SevenZipFile(archive, 'w') as a:
        a.writestr('hello7z', 'notes/x.txt')
    assert s.extract_archive(str(archive)) is True
    assert (course / 'good' / 'notes' / 'x.txt').read_text() == 'hello7z'


# --- F1: pull_git_repos must not run git inside attacker-controlled repos --

def test_is_safe_git_remote():
    assert s._is_safe_git_remote('https://github.com/u/r.git')
    assert s._is_safe_git_remote('HTTPS://github.com/u/r.git')
    assert not s._is_safe_git_remote('ext::sh -c "touch /tmp/x"')
    assert not s._is_safe_git_remote('git@github.com:u/r.git')
    assert not s._is_safe_git_remote('file:///etc')
    assert not s._is_safe_git_remote('http://github.com/u/r.git')
    assert not s._is_safe_git_remote('')


def test_git_safe_flags_are_valid(tmp_path):
    """The hardened -c flags must be accepted by git, not error out (128)."""
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    r = subprocess.run(['git'] + s._GIT_SAFE_FLAGS + ['config', '--get', 'core.bare'],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stderr


def test_pull_git_repos_does_not_execute_ext_remote(tmp_path):
    """A repo whose origin uses the ext:: transport must never be fetched."""
    downloads = tmp_path / 'downloads'
    repo = downloads / 'course' / 'malicious'
    repo.mkdir(parents=True)
    sentinel = tmp_path / 'PWNED'
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    evil = f'ext::sh -c "touch {sentinel}"'
    subprocess.run(['git', 'remote', 'add', 'origin', evil], cwd=repo, check=True)
    s.pull_git_repos(str(downloads))
    assert not sentinel.exists(), "ext:: remote transport command was executed"


def test_pull_git_repos_does_not_run_fsmonitor(tmp_path):
    """A planted repo's malicious core.fsmonitor must not be executed.

    core.fsmonitor runs as a command on git index operations (incl. git
    pull), so a .git/config planted via an extracted archive is RCE.
    """
    downloads = tmp_path / 'downloads'
    repo = downloads / 'course' / 'malicious'
    repo.mkdir(parents=True)
    sentinel = tmp_path / 'FSMON_PWNED'
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'core.fsmonitor', f'touch {sentinel}'],
                   cwd=repo, check=True)
    # An https remote that resolves nowhere: takes the code down the
    # `git pull` path without a successful fetch.
    subprocess.run(['git', 'remote', 'add', 'origin',
                    'https://127.0.0.1:1/nope.git'], cwd=repo, check=True)
    s.pull_git_repos(str(downloads))
    assert not sentinel.exists(), "core.fsmonitor command was executed"


def _git(cwd, *args):
    subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=test',
                    *args], cwd=str(cwd), check=True, capture_output=True)


def test_pull_git_repos_skips_non_https_remote(tmp_path):
    """A repo whose remote is a local path must be left untouched (not pulled)."""
    upstream = tmp_path / 'up.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(upstream)], check=True)
    seed = tmp_path / 'seed'
    subprocess.run(['git', 'clone', '-q', str(upstream), str(seed)], check=True)
    _git(seed, 'commit', '--allow-empty', '-q', '-m', 'one')
    _git(seed, 'push', '-q', 'origin', 'HEAD')

    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    repo = downloads / 'lecture'
    subprocess.run(['git', 'clone', '-q', str(upstream), str(repo)], check=True)

    _git(seed, 'commit', '--allow-empty', '-q', '-m', 'two')
    _git(seed, 'push', '-q', 'origin', 'HEAD')

    def head():
        return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip()

    before = head()
    s.pull_git_repos(str(downloads))
    assert head() == before, "repo with a local (non-https) remote was pulled"


# --- F4: feedback emails must come from a fau.de sender -------------------

def test_feedback_email_genuine_requires_fau_sender():
    """A real-looking subject is not enough — the sender must be within fau.de."""
    subj = 'Es wurde eine neue Feedback-Datei hochgeladen'
    assert s._is_genuine_feedback_email(subj, 'StudOn <studon@fau.de>')
    assert s._is_genuine_feedback_email(subj, 'noreply@www.studon.fau.de')
    assert not s._is_genuine_feedback_email(subj, 'attacker@evil.com')
    assert not s._is_genuine_feedback_email(subj, 'attacker@fau.de.evil.com')
    assert not s._is_genuine_feedback_email(subj, '')


def test_feedback_email_genuine_requires_subject():
    """A fau.de sender with the wrong subject is not a feedback notification."""
    assert not s._is_genuine_feedback_email('Hallo', 'studon@fau.de')


# === 2026-05 follow-up review (F-01) =========================================

# --- F-01: pull_git_repos must refuse repos whose config/attributes run code ---
# _GIT_SAFE_FLAGS neutralises ext::/file/fsmonitor/hooks but NOT a
# .gitattributes-assigned smudge/clean filter driver — that still runs a
# command on checkout and cannot be pre-empted by a `-c` flag (attacker-named).

def test_git_repo_unsafe_when_gitattributes_assigns_filter(tmp_path):
    """A .gitattributes filter driver is RCE on `git pull` (runs on checkout)."""
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    (repo / '.git' / 'config').write_text('[core]\n\tbare = false\n')
    (repo / '.gitattributes').write_text('* filter=pwn\n')
    assert s._git_repo_is_safe_to_pull(str(repo)) is False


def test_git_repo_unsafe_when_config_declares_filter_driver(tmp_path):
    """A [filter "x"] section in .git/config (smudge runs a command) is unsafe."""
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    (repo / '.git' / 'config').write_text(
        '[core]\n[filter "pwn"]\n\tsmudge = touch /tmp/pwned\n')
    assert s._git_repo_is_safe_to_pull(str(repo)) is False


def test_git_repo_safe_when_config_is_minimal(tmp_path):
    """A plain repo (core + https remote + branch) carries no code-exec vector."""
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    (repo / '.git' / 'config').write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n\turl = https://github.com/u/r.git\n'
        '[branch "main"]\n\tremote = origin\n')
    assert s._git_repo_is_safe_to_pull(str(repo)) is True


# --- F-03: the clipboard/TUI URL gate must be host-based, not a substring test ---

def test_is_studon_url_rejects_substring_lookalikes():
    """`STUDON_DOMAIN in url` accepts studon.fau.de.attacker.com — the gate
    must parse the host instead."""
    assert s._is_studon_url('https://www.studon.fau.de/studon/x')
    assert not s._is_studon_url('https://studon.fau.de.attacker.com/x')
    assert not s._is_studon_url('https://attacker.com/?studon.fau.de')
    assert not s._is_studon_url('not-a-url')


def test_extract_studon_link_rejects_substring_lookalikes():
    """_extract_studon_link is the entry point for --discover-from-timetable;
    its href filter must use the same host-parsing gate as everything else
    (was a bare `'studon.fau.de' in href` substring check)."""
    labelled = (
        '<form><label for="x">Link zur Lehrveranstaltung auf StudOn</label>'
        '<span id="x"><a href="https://studon.fau.de.attacker.com/payload">x</a>'
        '</span></form>'
    )
    assert s._extract_studon_link(labelled) is None

    fallback = '<a href="https://studon.fau.de.attacker.com/studon/go/exc/1">x</a>'
    assert s._extract_studon_link(fallback) is None

    legit = '<a href="https://www.studon.fau.de/studon/go/exc/1">x</a>'
    assert s._extract_studon_link(legit) == 'https://www.studon.fau.de/studon/go/exc/1'


# --- First-boot-wins guard: daily sync must not run twice across fleet hosts ---
# The Workstation and the Ideapad both run `@reboot --daily-sync`; the sync
# state (RECENT_UPDATES.md "Last updated:" line) lives in the Syncthing-synced
# download folder. Without re-checking that state right before scraping, both
# hosts scrape on the same morning and every shared output file conflicts.

def _write_recent_updates(folder, when):
    """Write a RECENT_UPDATES.md carrying the given 'Last updated' timestamp."""
    path = os.path.join(str(folder), "RECENT_UPDATES.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write("# Recent updates\n\n")
        f.write(f"Last updated: {when.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    return path


def test_fleet_synced_today_true_when_state_is_from_today(tmp_path, monkeypatch):
    """A today-dated state (e.g. synced in from the other host) means: skip."""
    from datetime import datetime
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path))
    _write_recent_updates(tmp_path, datetime.now())
    assert s._fleet_synced_today(grace_seconds=0) is True


def test_fleet_synced_today_false_when_state_is_stale(tmp_path, monkeypatch):
    """A yesterday-or-older state must not suppress today's sync."""
    from datetime import datetime, timedelta
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path))
    _write_recent_updates(tmp_path, datetime.now() - timedelta(days=1))
    assert s._fleet_synced_today(grace_seconds=0) is False


def test_fleet_synced_today_false_when_no_state_exists(tmp_path, monkeypatch):
    """First ever run (no RECENT_UPDATES.md): sync must proceed."""
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path))
    assert s._fleet_synced_today(grace_seconds=0) is False


def test_fleet_synced_today_rechecks_after_grace_window(tmp_path, monkeypatch):
    """The guard re-reads state after the grace sleep — simulating Syncthing
    delivering another host's freshly completed sync during that window."""
    from datetime import datetime, timedelta
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path))
    _write_recent_updates(tmp_path, datetime.now() - timedelta(days=1))

    def deliver_other_hosts_state(seconds):
        # what Syncthing would do while we sleep
        _write_recent_updates(tmp_path, datetime.now())

    monkeypatch.setattr(s.time, 'sleep', deliver_other_hosts_state)
    assert s._fleet_synced_today(grace_seconds=90) is True


# --- 7z symlink containment (verified handled by py7zr >=1.0) ----------------

def test_extract_archive_refuses_7z_with_symlink_member(tmp_path):
    """A 7z carrying an out-of-tree symlink member must not extract. py7zr >=1.0
    rejects this itself (Bad7zFile 'Symlink point out of target directory') and
    extract_archive surfaces it as a refusal. Regression guard: catches a py7zr
    downgrade below the symlink-aware version, or a 7z-branch change that would
    let such an archive through."""
    import pytest
    py7zr = pytest.importorskip('py7zr')
    course = tmp_path / 'course'
    course.mkdir()
    src = tmp_path / 'src'
    src.mkdir()
    os.symlink('/etc/hostname', str(src / 'evil_link'))
    (src / 'normal.txt').write_text('hi')
    archive = course / 'mal.7z'
    with py7zr.SevenZipFile(str(archive), 'w') as a:
        a.writeall(str(src), 'data')
    assert s.extract_archive(str(archive)) is False
    assert not os.path.lexists(str(course / 'mal' / 'data' / 'evil_link'))


# --- Enrollment-PDF download: host-validate the server-supplied anchor -------
# The campo docdownload anchor is lifted from a server-controlled JSF
# partial-response, then GETted with the campo session cookies. An absolute
# off-campo href in that anchor would otherwise exfiltrate the session — so
# _download_enrollment_pdf re-validates the *resolved* host before the GET,
# mirroring the _url_host_matches gates already used on the StudOn side.

class _FakePdfResponse:
    def __init__(self, content=b'%PDF-1.4 fake pdf bytes', headers=None):
        self.content = content
        self.headers = headers if headers is not None else {'Content-Type': 'application/pdf'}


class _FakeGetRecorder:
    """Session double recording every .get() URL; returns a canned PDF."""
    def __init__(self, response=None):
        self.calls = []
        self._response = response if response is not None else _FakePdfResponse()

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self._response


def test_enrollment_download_accepts_on_campo_anchor(tmp_path):
    """A relative anchor resolves to campo.fau.de → GET fires and the PDF saves."""
    sess = _FakeGetRecorder()
    poll = 'https://campo.fau.de/qisserver/pages/cm/exa/enrollment/info/start.xhtml'
    out = s._download_enrollment_pdf(
        sess, poll, '/qisserver/rds?state=docdownload&docId=1',
        'Immatrikulationsbescheinigung', 1, str(tmp_path))
    assert out is not None
    assert os.path.exists(out)
    assert sess.calls == ['https://campo.fau.de/qisserver/rds?state=docdownload&docId=1']


def test_enrollment_download_refuses_off_campo_anchor(tmp_path):
    """An absolute off-campo anchor (server-controlled partial-response) must be
    refused BEFORE the cookie-bearing GET fires — else the campo session leaks."""
    sess = _FakeGetRecorder()
    poll = 'https://campo.fau.de/qisserver/pages/cm/exa/enrollment/info/start.xhtml'
    out = s._download_enrollment_pdf(
        sess, poll, 'https://attacker.example/rds?state=docdownload&docId=1',
        'Immatrikulationsbescheinigung', 1, str(tmp_path))
    assert out is None
    assert sess.calls == []          # the cookie-bearing GET never fired off-host
    assert os.listdir(str(tmp_path)) == []   # nothing written


def test_enrollment_download_refuses_protocol_relative_anchor(tmp_path):
    """A protocol-relative '//host' anchor also resolves off campo and is refused."""
    sess = _FakeGetRecorder()
    poll = 'https://campo.fau.de/qisserver/pages/cm/exa/enrollment/info/start.xhtml'
    out = s._download_enrollment_pdf(
        sess, poll, '//attacker.example/rds?state=docdownload',
        'Immatrikulationsbescheinigung', 1, str(tmp_path))
    assert out is None
    assert sess.calls == []


# --- Post-boot Syncthing readiness gate -------------------------------------
# The @reboot daemons start before Syncthing has connected, so both fleet hosts
# see stale state and all scrape — the root cause of the METADATA.md conflict
# cluster. _wait_for_syncthing_ready() blocks until a peer is connected and the
# DOWNLOAD_FOLDER's Syncthing folder is in-sync, with mandatory graceful
# degradation to the legacy blind grace-sleep on any failure. All REST calls
# are mocked here — no live Syncthing is required.

_SYNCTHING_CONFIG_XML = """<configuration>
  <gui enabled="true"><address>127.0.0.1:8384</address><apikey>TESTKEY123</apikey></gui>
  <folder id="OneDrive" path="{base}"></folder>
  <folder id="other" path="/some/other/path"></folder>
</configuration>
"""


def _write_syncthing_config(tmp_path, base):
    cfg = tmp_path / "config.xml"
    cfg.write_text(_SYNCTHING_CONFIG_XML.format(base=base))
    return str(cfg)


class _FakeRestResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _make_rest_router(responses):
    """Build a fake requests.get that routes by endpoint substring.

    `responses` maps an endpoint substring -> payload dict (or None to simulate
    a transport error / unreachable endpoint).
    """
    def fake_get(url, headers=None, timeout=None):
        assert headers and headers.get("X-API-Key") == "TESTKEY123"
        for needle, payload in responses.items():
            if needle in url:
                if payload is None:
                    raise OSError("simulated unreachable")
                return _FakeRestResponse(payload)
        raise AssertionError(f"unexpected Syncthing endpoint: {url}")
    return fake_get


def test_read_syncthing_config_parses_apikey_and_folders(tmp_path, monkeypatch):
    cfg = _write_syncthing_config(tmp_path, str(tmp_path / "Sync" / "OneDrive"))
    monkeypatch.setattr(s, 'SYNCTHING_CONFIG_PATHS', [cfg])
    apikey, folders = s._read_syncthing_config()
    assert apikey == "TESTKEY123"
    # The OneDrive path is keyed by its realpath; id resolves correctly.
    assert "OneDrive" in folders.values()


def test_read_syncthing_config_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'SYNCTHING_CONFIG_PATHS', [str(tmp_path / "nope.xml")])
    assert s._read_syncthing_config() is None


def test_syncthing_folder_id_picks_longest_prefix(tmp_path):
    base = str(tmp_path / "Sync" / "OneDrive")
    deep = base + "/Studium/KIM4"
    folders = {
        os.path.realpath(str(tmp_path / "Sync")): "Synced",
        os.path.realpath(base): "OneDrive",
    }
    # KIM4 is under both Synced and OneDrive — the longer (OneDrive) wins.
    assert s._syncthing_folder_id_for_path(deep, folders) == "OneDrive"


def test_syncthing_folder_id_none_when_uncovered(tmp_path):
    folders = {"/completely/unrelated": "x"}
    assert s._syncthing_folder_id_for_path(str(tmp_path), folders) is None


def test_wait_for_syncthing_ready_true_when_peer_and_folder_in_sync(tmp_path, monkeypatch):
    """Happy path: a peer is connected and the folder is at 100% — returns True
    without ever falling back to the blind sleep."""
    base = str(tmp_path / "Sync" / "OneDrive")
    os.makedirs(base)
    cfg = _write_syncthing_config(tmp_path, base)
    monkeypatch.setattr(s, 'SYNCTHING_CONFIG_PATHS', [cfg])
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', os.path.join(base, "Studium", "KIM4"))
    monkeypatch.setattr(s.requests, 'get', _make_rest_router({
        "/rest/system/ping": {"ping": "pong"},
        "/rest/system/connections": {"connections": {"PEER1": {"connected": True}}},
        "/rest/db/completion": {"completion": 100.0},
    }))
    slept = []
    monkeypatch.setattr(s.time, 'sleep', lambda secs: slept.append(secs))
    assert s._wait_for_syncthing_ready() is True
    assert slept == []  # readiness confirmed via REST, no blind fallback


def test_wait_for_syncthing_ready_falls_back_when_no_config(tmp_path, monkeypatch):
    """No config.xml: must blind-sleep the grace window and return False (so the
    caller's own state re-check still runs and the scrape proceeds)."""
    monkeypatch.setattr(s, 'SYNCTHING_CONFIG_PATHS', [str(tmp_path / "absent.xml")])
    slept = []
    monkeypatch.setattr(s.time, 'sleep', lambda secs: slept.append(secs))
    assert s._wait_for_syncthing_ready(fallback_grace_seconds=42) is False
    assert slept == [42]


def test_wait_for_syncthing_ready_falls_back_when_folder_uncovered(tmp_path, monkeypatch):
    """DOWNLOAD_FOLDER not covered by any Syncthing folder → blind fallback."""
    cfg = _write_syncthing_config(tmp_path, "/some/other/onedrive")
    monkeypatch.setattr(s, 'SYNCTHING_CONFIG_PATHS', [cfg])
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path / "elsewhere"))
    slept = []
    monkeypatch.setattr(s.time, 'sleep', lambda secs: slept.append(secs))
    # requests.get must never be called when there is no folder to probe.
    monkeypatch.setattr(s.requests, 'get', _make_rest_router({}))
    assert s._wait_for_syncthing_ready(fallback_grace_seconds=7) is False
    assert slept == [7]


def test_wait_for_syncthing_ready_falls_back_when_api_unreachable(tmp_path, monkeypatch):
    """Syncthing config exists but the REST API ping errors → blind fallback."""
    base = str(tmp_path / "Sync" / "OneDrive")
    os.makedirs(base)
    cfg = _write_syncthing_config(tmp_path, base)
    monkeypatch.setattr(s, 'SYNCTHING_CONFIG_PATHS', [cfg])
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', base)
    monkeypatch.setattr(s.requests, 'get', _make_rest_router({
        "/rest/system/ping": None,  # simulate unreachable daemon
    }))
    slept = []
    monkeypatch.setattr(s.time, 'sleep', lambda secs: slept.append(secs))
    assert s._wait_for_syncthing_ready(fallback_grace_seconds=9) is False
    assert slept == [9]


def test_wait_for_syncthing_ready_times_out_then_falls_back(tmp_path, monkeypatch):
    """Daemon is up but never reaches in-sync within the timeout → blind
    fallback. The poll-sleep is patched out so the timeout is reached fast."""
    base = str(tmp_path / "Sync" / "OneDrive")
    os.makedirs(base)
    cfg = _write_syncthing_config(tmp_path, base)
    monkeypatch.setattr(s, 'SYNCTHING_CONFIG_PATHS', [cfg])
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', base)
    monkeypatch.setattr(s.requests, 'get', _make_rest_router({
        "/rest/system/ping": {"ping": "pong"},
        "/rest/system/connections": {"connections": {}},  # no peers ever
        "/rest/db/completion": {"completion": 0.0},
    }))
    # Drive the clock past the deadline on the first poll-sleep; record the
    # final blind grace-sleep separately.
    import time as _t
    t0 = _t.time()
    fake_now = {"v": t0}
    monkeypatch.setattr(s.time, 'time', lambda: fake_now["v"])
    blind = []

    def fake_sleep(secs):
        fake_now["v"] += 1000  # jump past the deadline so the loop exits
        blind.append(secs)
    monkeypatch.setattr(s.time, 'sleep', fake_sleep)
    assert s._wait_for_syncthing_ready(timeout_seconds=10, fallback_grace_seconds=3) is False
    # The last sleep is the blind grace-sleep with the configured fallback.
    assert blind[-1] == 3


# --- First-fire-wins lecture-sync marker (per-course-per-window) -------------
# --lecture-sync fires three times per lecture on BOTH hosts and each fire
# rewrites the same per-course METADATA.md — the root of a recurring conflict
# cluster. A small synced .lecture_sync_state.json marker lets the first fire
# (on either host) claim the window so the others skip.

def _make_tracked_course(folder):
    return s.TrackedCourse(
        metadata_path=os.path.join(folder, "METADATA.md"),
        course_folder=folder,
        course_title="Test Course",
        source_url="https://studon.fau.de/course/1",
        timetable_titles=[],
    )


def test_lecture_window_key_shared_across_three_fires(tmp_path):
    """All three fires (−5m / start / +5m) of one lecture share ONE key, so the
    first fire on either host claims the whole window."""
    from datetime import datetime, timedelta
    course = _make_tracked_course(str(tmp_path / "MyCourse"))
    start = datetime(2026, 6, 8, 10, 0, 0)
    # The marker is keyed by the lecture START, not the fire time — so callers
    # always derive `lecture_start` and pass that; here we assert the key is
    # identical regardless of which fire produced that start.
    k = s._lecture_window_key(course, start)
    assert s._lecture_window_key(course, start) == k
    # A different day or a different start time is a different key.
    assert s._lecture_window_key(course, start + timedelta(days=1)) != k
    assert s._lecture_window_key(course, start.replace(hour=12)) != k


def test_lecture_already_synced_roundtrip(tmp_path, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path))
    course = _make_tracked_course(str(tmp_path / "MyCourse"))
    start = datetime(2026, 6, 8, 10, 0, 0)
    assert s._lecture_already_synced(course, start) is False
    s._mark_lecture_synced(course, start)
    assert s._lecture_already_synced(course, start) is True


def test_lecture_marker_records_host_and_timestamp(tmp_path, monkeypatch):
    import json as _json
    from datetime import datetime
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path))
    course = _make_tracked_course(str(tmp_path / "MyCourse"))
    start = datetime(2026, 6, 8, 10, 0, 0)
    s._mark_lecture_synced(course, start)
    state = _json.loads((tmp_path / s.LECTURE_SYNC_STATE_FILE).read_text())
    (entry,) = state.values()
    assert entry["host"]            # this host's identity is recorded
    assert entry["ts"]              # and a timestamp the sibling can read


def test_lecture_marker_prunes_stale_entries(tmp_path, monkeypatch):
    """Old entries (beyond the retention window) are dropped on each write so
    the synced marker file can't grow without bound."""
    import json as _json
    from datetime import datetime, timedelta
    monkeypatch.setattr(s, 'DOWNLOAD_FOLDER', str(tmp_path))
    course = _make_tracked_course(str(tmp_path / "MyCourse"))
    # Seed a marker far older than the retention window, by its key's date.
    old_date = (datetime.now() - timedelta(days=s.LECTURE_SYNC_STATE_RETENTION_DAYS + 5))
    old_key = s._lecture_window_key(course, old_date)
    path = tmp_path / s.LECTURE_SYNC_STATE_FILE
    path.write_text(_json.dumps({old_key: {"host": "ancient", "ts": "x"}}))
    # A fresh write must prune the ancient key and keep the new one.
    now_start = datetime.now().replace(second=0, microsecond=0)
    s._mark_lecture_synced(course, now_start)
    state = _json.loads(path.read_text())
    assert old_key not in state
    assert s._lecture_window_key(course, now_start) in state


# --- Headless daily-sync login-wait bound -----------------------------------
# run_daily_sync waits for a Firefox login when the StudOn session is expired.
# In a headless @reboot/cron context (no DISPLAY) no human can ever complete
# that login, so the wait must be bounded and the daemon must exit — otherwise
# it polls forever (the 2026-06-07 Workstation incident: an expired session at
# boot spun the log every few minutes until killed by hand). With a graphical
# session the wait must stay unbounded so the user can still log in.

def _stub_daily_sync_preamble(monkeypatch):
    """Neuter everything before the wait loop so the loop is what we exercise:
    platform check, state load, and 'already updated today' all pass through to
    'session expired, keep waiting'."""
    monkeypatch.setattr(s, 'check_platform_compatibility', lambda: None)
    monkeypatch.setattr(s, 'load_state', lambda: None)
    monkeypatch.setattr(s, 'was_updated_today', lambda state: False)
    monkeypatch.setattr(s, 'can_access_studon', lambda: False)   # session never returns
    monkeypatch.setattr(s, '_wait_for_login_via_tray', lambda *a, **k: False)
    monkeypatch.setattr(s, '_get_first_course_url', lambda: 'https://studon.fau.de/')


def test_daily_sync_headless_gives_up_instead_of_looping_forever(monkeypatch):
    """No display + expired session → bound the wait and return, don't spin."""
    _stub_daily_sync_preamble(monkeypatch)
    monkeypatch.setattr(s, '_has_display', lambda: False)         # headless
    monkeypatch.setattr(s, 'DAILY_SYNC_HEADLESS_MAX_WAIT_SECONDS', 1800.0)

    import time as _t
    fake_now = {"v": _t.time()}
    monkeypatch.setattr(s.time, 'time', lambda: fake_now["v"])

    calls = {"n": 0}

    def fake_sleep(secs):
        calls["n"] += 1
        if calls["n"] > 50:
            raise AssertionError("run_daily_sync did not terminate — headless bound is broken")
        fake_now["v"] += 10_000  # jump past the give-up deadline
    monkeypatch.setattr(s.time, 'sleep', fake_sleep)

    assert s.run_daily_sync() is None          # returns, does not hang
    assert calls["n"] <= 3                      # gives up within a couple polls


def test_daily_sync_with_display_does_not_give_up(monkeypatch):
    """A graphical session is present → the wait stays unbounded even well past
    the headless cap; the give-up branch must never fire. Proven by raising a
    sentinel out of sleep after a few iterations and asserting it propagates
    (the function kept looping instead of returning)."""
    class _LoopStop(BaseException):
        pass

    _stub_daily_sync_preamble(monkeypatch)
    monkeypatch.setattr(s, '_has_display', lambda: True)          # graphical session
    monkeypatch.setattr(s, 'DAILY_SYNC_HEADLESS_MAX_WAIT_SECONDS', 1.0)  # tiny cap

    import time as _t
    t0 = _t.time()
    # Clock is already far past the (tiny) cap on every poll — a headless run
    # would give up at once; a display run must keep looping regardless.
    monkeypatch.setattr(s.time, 'time', lambda: t0 + 10_000)

    calls = {"n": 0}

    def fake_sleep(secs):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise _LoopStop()
    monkeypatch.setattr(s.time, 'sleep', fake_sleep)

    raised = False
    try:
        s.run_daily_sync()
    except _LoopStop:
        raised = True
    assert raised, "with a display present, run_daily_sync must NOT give up — it returned"
    assert calls["n"] == 3                       # kept looping past the cap


# --- Timetable non-current-term export (--timetable --term) ----------------
#
# Offline coverage for the term-label resolution and shared span-parsing that
# back a non-current-semester timetable export. No network: the campo pages are
# mocked as minimal HTML mirroring the changeTerm select + schedulePanel shape.

_MOCK_CHANGETERM_HTML = """
<html><body><form id="plan" action="/qisserver/pages/plan/x.xhtml">
  <select name="plan:scheduleConfiguration:anzeigeoptionen:changeTerm_input"
          id="plan:scheduleConfiguration:anzeigeoptionen:changeTerm_input">
    <option value="595">Sommersemester 2027</option>
    <option value="590">Wintersemester 2026/27</option>
    <option value="589" selected="selected">Sommersemester 2026</option>
    <option value="565">Wintersemester 2025/26</option>
  </select>
</form></body></html>
"""

_MOCK_TIMETABLE_VIEW_HTML = """
<html><head><title>Stundenplan für  Muster, Max  - campo.fau.de</title></head>
<body><form id="plan">
  <div class="colhead">Montag</div>
  <div class="colhead">Dienstag</div>
  <div id="plan:schedule:scheduleColumn:0:termin:0:scheduleItem:schedulePanelGroup"
       class="schedulePanel">
    <h3 class="scheduleTitle">Biomaterialien</h3>
    <span id="a:eventtypeShorttext">Vorlesung mit Übung</span>
    <span id="a:times">12:15 bis 13:45</span>
    <span id="a:rhythmDefaulttext">wöchentlich</span>
    <span id="a:scheduleStartDate">19.10.2026</span>
    <span id="a:scheduleEndDate">01.02.2027</span>
    <span id="a:buildingDefaulttext">Verbundlabor</span>
    <span id="a:instructorLink1">Prof. Dr. Boccaccini</span>
    <div class="note">Diese Veranstaltung ist in Ihrem Stundenplan nur vorgemerkt und noch nicht belegt.</div>
  </div>
</form></body></html>
"""


def test_short_term_label_forms():
    assert s._short_term_label('Wintersemester 2026/27') == 'WS2627'
    assert s._short_term_label('Sommersemester 2026') == 'SS26'
    assert s._short_term_label('Sommersemester 2027') == 'SS27'


def test_resolve_timetable_term_eq_style_winter():
    """'eq|2|2026' resolves to the Wintersemester 2026/27 option by label."""
    resolved = s._resolve_timetable_term(_MOCK_CHANGETERM_HTML, 'eq|2|2026')
    assert resolved == ('590', 'Wintersemester 2026/27', 'WS2627')


def test_resolve_timetable_term_eq_style_summer():
    resolved = s._resolve_timetable_term(_MOCK_CHANGETERM_HTML, 'eq|1|2027')
    assert resolved == ('595', 'Sommersemester 2027', 'SS27')


def test_resolve_timetable_term_raw_numeric_id():
    """A raw campo option id resolves to that option and derives its label."""
    resolved = s._resolve_timetable_term(_MOCK_CHANGETERM_HTML, '590')
    assert resolved == ('590', 'Wintersemester 2026/27', 'WS2627')


def test_resolve_timetable_term_unknown_returns_none():
    assert s._resolve_timetable_term(_MOCK_CHANGETERM_HTML, 'eq|1|1999') is None
    assert s._resolve_timetable_term(_MOCK_CHANGETERM_HTML, '99999') is None


def test_parse_timetable_html_extracts_entry():
    """The shared span-parser reads a schedulePanel into a normalized entry."""
    parsed = s._parse_timetable_html(_MOCK_TIMETABLE_VIEW_HTML)
    assert parsed is not None
    page_title, entries = parsed
    assert page_title == 'Stundenplan für Muster, Max'
    assert len(entries) == 1
    e = entries[0]
    assert e['day'] == 'Montag'
    assert e['title'] == 'Biomaterialien'
    assert e['time'] == '12:15 bis 13:45'
    assert e['start'] == '19.10.2026'
    assert e['end'] == '01.02.2027'
    assert e['note']  # vorgemerkt-Hinweis captured


def test_parse_timetable_html_empty_returns_none():
    assert s._parse_timetable_html('<html><body>nothing</body></html>') is None
