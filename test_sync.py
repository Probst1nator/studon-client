"""Sync-robustness regression tests for studon_client.

Covers the failure modes that make a course silently stop syncing: a
METADATA.md that got truncated to zero bytes, and two StudOn items whose
download filenames collide on one local path. Run with: pytest -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import studon_client as s  # noqa: E402


COURSE_URL = "https://www.studon.fau.de/studon/ilias.php?baseClass=ilrepositorygui&ref_id=6732198"


def _make_course(tmp_path, name="Some Course", link=True, metadata=""):
    """A course folder with a METADATA.md of the given content (empty by default)."""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "METADATA.md").write_text(metadata, encoding="utf-8")
    if link:
        s.create_course_link_file(folder, name, COURSE_URL)
    return folder


# --- METADATA.md must be written atomically --------------------------------

def test_atomic_write_leaves_no_partial_file(tmp_path):
    """A failing write keeps the previous content instead of truncating it."""
    target = tmp_path / "METADATA.md"
    target.write_text("old content", encoding="utf-8")

    # Anything that makes the write fail after the old code would already have
    # truncated the file — here a value the text stream refuses to write.
    try:
        s._atomic_write_text(str(target), object())  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("expected the write to fail")
    assert target.read_text(encoding="utf-8") == "old content"
    # No stray temp file left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["METADATA.md"]


def test_atomic_write_replaces_content(tmp_path):
    target = tmp_path / "METADATA.md"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o664)
    s._atomic_write_text(str(target), "new")
    assert target.read_text(encoding="utf-8") == "new"
    # The replacement must not inherit mkstemp's private 0600 mode.
    assert target.stat().st_mode & 0o777 == 0o664


# --- An empty METADATA.md is repairable, not a silent drop-out --------------

def test_link_file_recovers_title_and_url(tmp_path):
    folder = _make_course(tmp_path, "Wissenschaftliches Rechnen")
    title, url = s.recover_course_from_link_file(str(folder))
    assert title == "Wissenschaftliches Rechnen"
    assert url == COURSE_URL


def test_link_file_recovery_rejects_off_domain_url(tmp_path):
    folder = tmp_path / "Evil"
    folder.mkdir()
    s.create_course_link_file(folder, "Evil", "https://studon.fau.de.attacker.com/x")
    assert s.recover_course_from_link_file(str(folder)) == ("Evil", None)


def test_link_file_recovery_is_silent_without_a_link_file(tmp_path):
    folder = tmp_path / "Bare"
    folder.mkdir()
    assert s.recover_course_from_link_file(str(folder)) == (None, None)


def test_empty_metadata_still_yields_a_course_to_sync(tmp_path):
    """The 0-byte METADATA.md case: recovered from the link file, not skipped."""
    folder = _make_course(tmp_path, "Empty Metadata Course")
    found = s.find_all_metadata_files(str(tmp_path))
    assert [(url, root) for _, url, root in found] == [(COURSE_URL, str(folder))]


def test_empty_metadata_without_link_file_is_dropped(tmp_path):
    """Nothing to recover from: the course is skipped, and loudly (a warning)."""
    _make_course(tmp_path, "No Link", link=False)
    assert s.find_all_metadata_files(str(tmp_path)) == []


def test_intact_metadata_is_read_from_the_file_not_the_link(tmp_path):
    other = "https://www.studon.fau.de/studon/ilias.php?baseClass=ilrepositorygui&ref_id=1"
    folder = _make_course(tmp_path, "Intact", metadata=f"Source: {other}\n")
    found = s.find_all_metadata_files(str(tmp_path))
    assert [url for _, url, _ in found] == [other]
    assert found[0][2] == str(folder)


# --- Two StudOn items must not overwrite each other locally -----------------

def _claim(*paths):
    return {os.path.abspath(p): "some-other-url" for p in paths}


def test_ref_id_is_read_from_a_studon_link():
    assert s._ref_id_from_url(COURSE_URL) == "6732198"
    assert s._ref_id_from_url("https://x/y?a=1") is None
    assert s._ref_id_from_url("") is None


def test_collision_prefers_the_studon_item_title(tmp_path):
    taken = str(tmp_path / "final_exam.pdf")
    alt = s._disambiguate_filepath(taken, "Exam_SS21", COURSE_URL, _claim(taken))
    assert alt == str(tmp_path / "Exam_SS21.pdf")


def test_collision_falls_back_to_the_ref_id_when_the_title_matches(tmp_path):
    """A title equal to the download name cannot tell the two items apart."""
    taken = str(tmp_path / "final_exam.pdf")
    alt = s._disambiguate_filepath(taken, "final_exam.pdf", COURSE_URL, _claim(taken))
    assert alt == str(tmp_path / "final_exam_ref6732198.pdf")


def test_collision_never_lands_on_an_existing_file(tmp_path):
    """An existing file is neither overwritten nor renamed — the newcomer moves."""
    taken = tmp_path / "final_exam.pdf"
    taken.write_bytes(b"x")
    (tmp_path / "Exam_SS21.pdf").write_bytes(b"y")
    alt = s._disambiguate_filepath(str(taken), "Exam_SS21", COURSE_URL, _claim(str(taken)))
    assert alt == str(tmp_path / "final_exam_ref6732198.pdf")


def test_collision_is_deterministic(tmp_path):
    taken = str(tmp_path / "final_exam.pdf")
    first = s._disambiguate_filepath(taken, "Exam_SS21", COURSE_URL, _claim(taken))
    second = s._disambiguate_filepath(taken, "Exam_SS21", COURSE_URL, _claim(taken))
    assert first == second


def test_no_collision_leaves_the_download_name_alone(tmp_path):
    """The first claimant of a name keeps it; only later ones are renamed."""
    path = str(tmp_path / "final_exam.pdf")
    # download_all_files only calls the helper on a real clash, but the helper
    # itself must still hand back a usable path when the name is free.
    assert s._disambiguate_filepath(path, "Exam_SS21", COURSE_URL, {}) == str(
        tmp_path / "Exam_SS21.pdf")


class _FakeDownloadSession:
    """Serves each URL a body under a fixed Content-Disposition filename."""

    def __init__(self, bodies):
        self._bodies = bodies  # url -> (filename, bytes)

    def get(self, url, stream=False, timeout=None):
        filename, body = self._bodies[url]
        return _FakeDownload(filename, body)


class _FakeDownload:
    def __init__(self, filename, body):
        self.headers = {"Content-Disposition": f'filename="{filename}"'}
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self._body


def _two_colliding_items(course):
    """Two StudOn items with distinct titles, both served as final_exam.pdf."""
    base = "https://www.studon.fau.de/studon/ilias.php?cmd=sendfile&ref_id="
    urls = [base + "6891449", base + "6891450"]
    files = [
        {"url": urls[0], "path": str(course / "Old Exams"), "name": "Exam_ws2021",
         "course_title": "IML"},
        {"url": urls[1], "path": str(course / "Old Exams"), "name": "Exam_SS21",
         "course_title": "IML"},
    ]
    session = _FakeDownloadSession({
        urls[0]: ("final_exam.pdf", b"a" * 10),
        urls[1]: ("final_exam.pdf", b"b" * 20),
    })
    return urls, files, session


def test_colliding_items_get_separate_files_and_metadata_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    course = tmp_path / "IML"
    course.mkdir()
    urls, files, session = _two_colliding_items(course)

    count, downloaded = s.download_all_files(
        COURSE_URL, files, session, course_title="IML", base_path=str(course))

    assert count == 2
    assert sorted(os.path.basename(p) for p in downloaded) == [
        "Exam_SS21.pdf", "final_exam.pdf"]
    # Both survive: the second no longer overwrites the first.
    assert (course / "Old Exams" / "final_exam.pdf").read_bytes() == b"a" * 10
    assert (course / "Old Exams" / "Exam_SS21.pdf").read_bytes() == b"b" * 20

    meta = s.CourseMetadata.from_yaml_markdown(str(course / "METADATA.md"))
    by_url = {r.download_url: r.filepath.name for r in meta.file_history}
    assert by_url == {urls[0]: "final_exam.pdf", urls[1]: "Exam_SS21.pdf"}


def test_a_second_sync_downloads_nothing_and_renames_nothing(tmp_path, monkeypatch):
    """The recurring re-download: both items are recognised on disk next run."""
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    course = tmp_path / "IML"
    course.mkdir()
    _, files, session = _two_colliding_items(course)
    s.download_all_files(COURSE_URL, files, session, course_title="IML",
                         base_path=str(course))
    before = sorted(p.name for p in (course / "Old Exams").iterdir())

    count, downloaded = s.download_all_files(
        COURSE_URL, files, session, course_title="IML", base_path=str(course))

    assert (count, downloaded) == (0, [])
    assert sorted(p.name for p in (course / "Old Exams").iterdir()) == before


def test_an_unknown_local_file_is_left_alone(tmp_path, monkeypatch):
    """A file the user placed by hand is neither deleted nor overwritten."""
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    course = tmp_path / "IML"
    (course / "Old Exams").mkdir(parents=True)
    manual = course / "Old Exams" / "final_exam_SS21_ref6891450.pdf"
    manual.write_bytes(b"mine")
    _, files, session = _two_colliding_items(course)

    s.download_all_files(COURSE_URL, files, session, course_title="IML",
                         base_path=str(course))

    assert manual.read_bytes() == b"mine"


def test_metadata_recording_two_items_at_one_path_is_repaired(tmp_path, monkeypatch):
    """The state a past collision left behind: both items recorded at one path.

    The shared path must not count as "already downloaded" for both, or the
    second item would stay missing forever.
    """
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    course = tmp_path / "IML"
    (course / "Old Exams").mkdir(parents=True)
    (course / "Old Exams" / "final_exam.pdf").write_bytes(b"a" * 10)
    urls, files, session = _two_colliding_items(course)
    (course / "METADATA.md").write_text(
        "---\n"
        "course_title: IML\n"
        f"source_url: {COURSE_URL}\n"
        "last_fetched: '2026-08-31T19:05:08'\n"
        "file_history:\n"
        + "".join(
            f"- filepath: Old Exams/final_exam.pdf\n"
            f"  timestamp: '2026-08-31T19:05:08'\n"
            f"  course_name: IML\n"
            f"  size_bytes: 10\n"
            f"  download_url: {u}\n"
            for u in urls
        )
        + "---\n\n# IML\n",
        encoding="utf-8",
    )

    count, downloaded = s.download_all_files(
        COURSE_URL, files, session, course_title="IML", base_path=str(course))

    assert count == 1
    assert [os.path.basename(p) for p in downloaded] == ["Exam_SS21.pdf"]
    assert (course / "Old Exams" / "final_exam.pdf").read_bytes() == b"a" * 10
    assert (course / "Old Exams" / "Exam_SS21.pdf").read_bytes() == b"b" * 20


def test_the_recorded_size_decides_who_owns_a_contested_path(tmp_path, monkeypatch):
    """The item processed first must not claim a file that is not its own.

    Both items are recorded at Old Exams/final_exam.pdf, but only one of the
    two recorded sizes matches what is on disk.
    """
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    course = tmp_path / "IML"
    (course / "Old Exams").mkdir(parents=True)
    (course / "Old Exams" / "final_exam.pdf").write_bytes(b"a" * 10)
    urls, files, session = _two_colliding_items(course)
    # Process the item that does NOT own the file first.
    files = [files[1], files[0]]
    sizes = {urls[0]: 10, urls[1]: 20}
    (course / "METADATA.md").write_text(
        "---\ncourse_title: IML\n"
        f"source_url: {COURSE_URL}\n"
        "last_fetched: '2026-08-31T19:05:08'\n"
        "file_history:\n"
        + "".join(
            "- filepath: Old Exams/final_exam.pdf\n"
            "  timestamp: '2026-08-31T19:05:08'\n"
            "  course_name: IML\n"
            f"  size_bytes: {sizes[u]}\n"
            f"  download_url: {u}\n"
            for u in urls
        )
        + "---\n\n# IML\n",
        encoding="utf-8",
    )

    count, downloaded = s.download_all_files(
        COURSE_URL, files, session, course_title="IML", base_path=str(course))

    assert count == 1
    assert [os.path.basename(p) for p in downloaded] == ["Exam_SS21.pdf"]
    # final_exam.pdf stays the ws2021 file it always was.
    assert (course / "Old Exams" / "final_exam.pdf").read_bytes() == b"a" * 10
    meta = s.CourseMetadata.from_yaml_markdown(str(course / "METADATA.md"))
    newest = {}
    for r in meta.file_history:
        newest.setdefault(r.download_url, r.filepath.name)
    assert newest[urls[1]] == "Exam_SS21.pdf"


# --- '<name> .sec' downloads get their real extension back -----------------

def test_sec_downloads_are_renamed_by_their_magic_bytes(tmp_path, monkeypatch):
    """PDF and zip content is renamed; unknown content keeps its .sec name."""
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    course = tmp_path / "IML"
    course.mkdir()
    base = "https://www.studon.fau.de/studon/ilias.php?cmd=sendfile&ref_id="
    bodies = {
        base + "1": ("Blatt 1 .sec", b"%PDF-1.7 ..."),
        base + "2": ("Code .sec", b"PK\x03\x04 ..."),
        base + "3": ("Notes .sec", b"plain text"),
    }
    files = [{"url": u, "path": str(course), "name": name, "course_title": "IML"}
             for u, (name, _) in bodies.items()]

    count, downloaded = s.download_all_files(
        COURSE_URL, files, _FakeDownloadSession(bodies), course_title="IML",
        base_path=str(course))

    assert count == 3
    assert sorted(os.path.basename(p) for p in downloaded) == [
        "Blatt 1.pdf", "Code.zip", "Notes .sec"]
    assert (course / "Blatt 1.pdf").read_bytes() == b"%PDF-1.7 ..."


# --- Tests, exercises and forums are listed, not silently skipped ----------

def test_interactive_kind_reads_the_link():
    """The kind comes from the link, since the item icons load lazily."""
    base = "https://www.studon.fau.de/studon/"
    assert s._interactive_kind(base + "ilias.php?baseClass=ilrepositorygui&cmdClass=ilTestScreenGUI&cmd=testScreen&ref_id=1") == "test"
    assert s._interactive_kind(base + "go/exc/7143570") == "exercise"
    assert s._interactive_kind(base + "go/frm/7144476") == "forum"
    assert s._interactive_kind(base + "ilias.php?baseClass=ilObjPluginDispatchGUI&cmd=forward&ref_id=2") == "plugin"
    assert s._interactive_kind(base + "go/fold/7154930") is None
    assert s._interactive_kind(base + "ilias.php?cmdClass=ilObjFileGUI&cmd=sendfile&ref_id=3") is None


def test_interactive_permalink_keeps_only_the_item():
    """The folder's ref_id and the per-render cmdNode drop out; plugins keep their link."""
    link = "https://www.studon.fau.de/studon/ilias.php?cmdNode=13a:wm:1a7&cmdClass=ilTestScreenGUI&ref_id=7154930&ref_id=7154932"
    assert s._interactive_permalink("test", link) == "https://www.studon.fau.de/studon/go/tst/7154932"
    plugin = "https://www.studon.fau.de/studon/ilias.php?baseClass=ilObjPluginDispatchGUI&ref_id=2"
    assert s._interactive_permalink("plugin", plugin) == plugin


class _FakePage:
    def __init__(self, url, text):
        self.url, self.text = url, text

    def raise_for_status(self):
        pass


class _FakeCrawlSession:
    """Serves fixed HTML per URL, like StudOn's classic container pages."""

    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **kwargs):
        final_url, text = self.pages[url]
        return _FakePage(final_url, text)


def _container(*items):
    rows = "".join(
        f'<div class="ilContainerListItemOuter">{icon}<div class="il_ContainerListItem">'
        f'<a class="il_ContainerItemTitle" href="{href}">{title}</a></div></div>'
        for title, href, icon in items)
    return f"<html><body>{rows}</body></html>"


def test_crawl_collects_tests_in_subfolders(tmp_path):
    """A test inside a lecture folder is reported with its folder; files still download."""
    base = "https://www.studon.fau.de/studon/"
    root, part2 = base + "go/crs/1", base + "go/fold/2"
    # As StudOn renders it: relative, folder ref_id first, the test's own last.
    test_href = "ilias.php?baseClass=ilrepositorygui&cmdNode=13a:wm:1a7&cmdClass=ilTestScreenGUI&cmd=testScreen&ref_id=2&ref_id=5"
    file_url = base + "ilias.php?cmdClass=ilObjFileGUI&cmd=sendfile&ref_id=3"
    pages = {
        root: (root, _container(("Teil II", part2, '<img alt="Ordner">'))),
        # /go/fold/2 redirects, so relative links resolve against ilias.php
        part2: (base + "ilias.php?baseClass=ilrepositorygui&ref_id=2",
                _container(("Folien", file_url, ""), ("02. Test zu Teil 2", test_href, ""))),
    }
    files, found = [], []
    s.discover_items_recursive(root, str(tmp_path), _FakeCrawlSession(pages), files, "WWSim",
                               interactive_list=found)

    assert [f["name"] for f in files] == ["Folien"]
    items = s._interactive_for_metadata(found, str(tmp_path))
    assert items == [{"kind": "test", "title": "02. Test zu Teil 2", "folder": "Teil II",
                      "url": "https://www.studon.fau.de/studon/go/tst/5"}]


def test_metadata_keeps_interactive_items_until_the_next_crawl(tmp_path, monkeypatch):
    """None (no crawl) keeps the list, a fresh crawl replaces it, and it is readable on disk."""
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    course = _make_course(tmp_path, "WWSim")
    path = str(course / "METADATA.md")
    item = {"kind": "test", "title": "Test zu Teil 1", "folder": "Teil I",
            "url": "https://www.studon.fau.de/studon/go/tst/1"}

    s.update_course_metadata(path, "WWSim", COURSE_URL, [], [item])
    s.update_course_metadata(path, "WWSim", COURSE_URL, [])
    assert s.CourseMetadata.from_yaml_markdown(path).interactive_items == [item]
    assert "Test zu Teil 1" in (course / "METADATA.md").read_text(encoding="utf-8")

    s.update_course_metadata(path, "WWSim", COURSE_URL, [], [])
    assert s.CourseMetadata.from_yaml_markdown(path).interactive_items == []


# --- Open button on the sync notification ----------------------------------

def test_notification_targets_one_file_opens_the_file(tmp_path):
    f = tmp_path / "Course" / "a.pdf"
    f.parent.mkdir()
    f.write_bytes(b"x")
    assert s._notification_open_targets([str(f)]) == [("Open", str(f))]


def test_notification_targets_one_folder_opens_the_folder(tmp_path):
    folder = tmp_path / "Course"
    folder.mkdir()
    files = [folder / "a.pdf", folder / "b.pdf"]
    for f in files:
        f.write_bytes(b"x")
    assert s._notification_open_targets([str(f) for f in files]) == [("Open folder", str(folder))]


def test_notification_targets_several_folders(tmp_path):
    folders = [tmp_path / n for n in ("A", "B", "C", "D")]
    files = []
    for d in folders:
        d.mkdir()
        files.append(d / "x.pdf")
        files[-1].write_bytes(b"x")
    paths = [str(f) for f in files]
    assert s._notification_open_targets(paths[:2]) == [("Open A", str(folders[0])), ("Open B", str(folders[1]))]
    assert s._notification_open_targets(paths) == [("Open folder", str(tmp_path))]


def test_notification_targets_skip_missing_files(tmp_path):
    assert s._notification_open_targets([str(tmp_path / "gone.zip")]) == []


# --- The bot-protection page is not a course -------------------------------

_BOT_PAGE = ("<html><head><title>Making sure you&#39;re not a bot!</title></head>"
             '<body><h1 id="title">Making sure you&#39;re not a bot!</h1></body></html>')


class _BotCheckSession:
    """Answers every request with HTTP 200 and the bot-protection page."""

    def __init__(self):
        self.calls, self.cookies, self.headers = 0, {}, {}

    def get(self, url, **kwargs):
        self.calls += 1
        page = _FakePage(url, _BOT_PAGE)
        page.status_code = 200
        return page


def test_bot_check_page_is_recognised():
    course = ('<html><body><h1 class="il-page-content-header">Werkstoffsimulation</h1>'
              "<p>Forum: Making sure you're not a bot!</p></body></html>")
    assert s._is_bot_check_page(_BOT_PAGE)
    assert not s._is_bot_check_page(course)
    assert not s._is_bot_check_page("")


def test_bot_check_page_stops_the_sync_and_keeps_metadata(tmp_path, monkeypatch):
    """Its <h1> was once written into every METADATA.md as the course title."""
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    folders = [_make_course(tmp_path, name) for name in ("Some Course", "Other Course")]
    session = _BotCheckSession()

    for call in (lambda: s.extract_course_title(COURSE_URL, session),
                 lambda: s.discover_items_recursive(COURSE_URL, str(tmp_path), session, [])):
        try:
            call()
        except s.BotCheckError:
            pass
        else:
            raise AssertionError("the bot-protection page was taken for a course page")

    session.calls = 0
    assert s.update_all_courses(session=session) == (False, 0, 0, False, [])
    assert session.calls == 1, "the remaining courses must not be tried"
    assert [(f / "METADATA.md").read_text(encoding="utf-8") for f in folders] == ["", ""]


def test_bot_check_after_a_synced_course_returns_that_course_s_files(tmp_path, monkeypatch):
    """The run stops, but the files of the courses before the block are reported."""
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    for name in ("Some Course", "Other Course"):
        _make_course(tmp_path, name)
    calls = []

    def process(url, session, folder, **kwargs):
        calls.append(folder)
        if len(calls) == 1:
            return 1, 0, [os.path.join(folder, "new.pdf")]
        raise s.BotCheckError(url)

    def no_git_pull(folder):
        raise AssertionError("a stopped run must not go on to the git pulls")

    monkeypatch.setattr(s, "process_single_url", process)
    monkeypatch.setattr(s, "pull_git_repos", no_git_pull)

    result = s.update_all_courses(session=_BotCheckSession())

    assert len(calls) == 2
    assert result == (False, 1, 0, False, [os.path.join(calls[0], "new.pdf")])


def test_expired_login_after_a_synced_course_returns_that_course_s_files(tmp_path, monkeypatch):
    """Same as the bot check: the files fetched before the stop are reported."""
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    for name in ("Some Course", "Other Course"):
        _make_course(tmp_path, name)
    calls = []

    def process(url, session, folder, **kwargs):
        calls.append(folder)
        if len(calls) == 1:
            return 1, 0, [os.path.join(folder, "new.pdf")]
        raise s.StudOnError("Session expired — redirected to login page.")

    monkeypatch.setattr(s, "process_single_url", process)

    result = s.update_all_courses(session=_BotCheckSession())

    assert result == (False, 1, 0, True, [os.path.join(calls[0], "new.pdf")])


def test_bot_check_page_is_not_a_valid_login(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(s, "_bot_check_state", {"seen": False, "last_warned": 0.0})
    monkeypatch.setattr(s.browser_cookie3, "firefox", lambda **kwargs: {})
    monkeypatch.setattr(s.requests, "Session", _BotCheckSession)
    _make_course(tmp_path, metadata="Last fetched: 2026-11-16 14:02:00\n")

    assert s.can_access_studon() is False
    assert s._bot_check_state["seen"] is True


def test_an_expired_login_after_a_bot_check_asks_for_the_login_again(tmp_path, monkeypatch):
    """The flag describes the latest probe only."""

    class _LoginRedirectSession(_BotCheckSession):
        def get(self, url, **kwargs):
            page = _FakePage("https://www.studon.fau.de/studon/login.php", "<html></html>")
            page.status_code = 200
            return page

    monkeypatch.setattr(s, "DOWNLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(s, "_bot_check_state", {"seen": True, "last_warned": 0.0})
    monkeypatch.setattr(s.browser_cookie3, "firefox", lambda **kwargs: {})
    monkeypatch.setattr(s.requests, "Session", _LoginRedirectSession)
    _make_course(tmp_path, metadata="Last fetched: 2026-11-16 14:02:00\n")

    assert s.can_access_studon() is False
    assert s._bot_check_state["seen"] is False


def test_daily_sync_waits_for_firefox_on_the_bot_check_page(monkeypatch):
    """Handled like an expired login: one popup, the wait, then the sync goes on."""
    popups, probes, waits = [], [], []

    def probe():
        probes.append(1)
        s._bot_check_state["seen"] = len(probes) == 1
        return len(probes) > 1

    monkeypatch.setattr(s, "_bot_check_state", {"seen": False, "last_warned": 0.0})
    monkeypatch.setattr(s, "can_access_studon", probe)
    monkeypatch.setattr(s, "check_platform_compatibility", lambda: None)
    monkeypatch.setattr(s, "load_state", lambda: s.UpdateState(last_update=None))
    monkeypatch.setattr(s, "was_updated_today", lambda state: False)
    monkeypatch.setattr(s, "_get_first_course_url", lambda: COURSE_URL)
    monkeypatch.setattr(s, "_notify_login_required", lambda url, attempt_key: popups.append(attempt_key))
    monkeypatch.setattr(s, "_wait_for_login_via_tray", lambda url: waits.append(url) or True)
    monkeypatch.setattr(s, "_write_tray_status", lambda **kwargs: None)
    monkeypatch.setattr(s, "_dismiss_login_prompt", lambda: None)
    monkeypatch.setattr(s, "_fleet_synced_today", lambda: True)  # ends the run after the wait

    s.run_daily_sync()

    assert (len(probes), len(popups), len(waits)) == (2, 1, 1)


def test_daily_sync_announces_files_fetched_before_a_stopped_run(monkeypatch):
    """A run that stops part-way reports its files once; the retry would not."""
    notes, runs = [], []

    def update():
        runs.append(1)
        return False, 2, 0, False, ["a.pdf", "b.pdf"]

    monkeypatch.setattr(s, "can_access_studon", lambda: True)
    monkeypatch.setattr(s, "check_platform_compatibility", lambda: None)
    monkeypatch.setattr(s, "load_state", lambda: s.UpdateState(last_update=None))
    monkeypatch.setattr(s, "was_updated_today", lambda state: False)
    monkeypatch.setattr(s, "_write_tray_status", lambda **kwargs: None)
    monkeypatch.setattr(s, "_dismiss_login_prompt", lambda: None)
    monkeypatch.setattr(s, "_fleet_synced_today", lambda: bool(runs))  # ends the run on the retry
    monkeypatch.setattr(s, "_acquire_sync_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(s, "_release_sync_lock", lambda: None)
    monkeypatch.setattr(s, "update_all_courses", update)
    monkeypatch.setattr(s, "_send_desktop_notification", lambda n, e, files: notes.append((n, files)))
    monkeypatch.setattr(s.time, "sleep", lambda seconds: None)

    s.run_daily_sync()

    assert (len(runs), notes) == (1, [(2, ["a.pdf", "b.pdf"])])
