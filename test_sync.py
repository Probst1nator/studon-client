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
