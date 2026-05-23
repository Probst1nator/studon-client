"""Security regression tests for studon_scraper.

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
import studon_scraper as s  # noqa: E402


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
