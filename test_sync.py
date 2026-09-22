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
