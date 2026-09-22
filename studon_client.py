import os
import sys
import json

# --- CRITICAL: respond to --advertise BEFORE any heavy imports ---
# The tools-installer machinery (used by ~/Synced/repos/AutomatedAlchemy/
# installer.py, which re-uses ~/Synced/repos/tools/installer.py as a library)
# probes each candidate script with `--advertise` and a 5s timeout. The
# heavy imports below (requests, BeautifulSoup, browser_cookie3, …) would
# blow the budget, so short-circuit here.
def _advertise_entry() -> dict:
    """The --advertise record. `--install` builds its ToolMetadata from it too,
    so the alias name and alias_args live in one place."""
    # Resolve the configured download folder inline (config.json is loaded much
    # later, after heavy imports) so the digest tool can read course files.
    _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        _dl = os.path.expanduser(json.load(open(_cfg)).get("downloads_path", "studon_downloads"))
    except Exception:
        _dl = "studon_downloads"
    return {
        "name": "StudOn Client",
        "desktop_file": "studon_client.desktop",
        # Bundled StudOn logo (assets/studon-client.png); absolute so the
        # .desktop Icon= key resolves without installing a theme icon.
        "icon": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "studon-client.png"),
        "desc": "FAU StudOn / campo scraper — course downloads, timetable, Notenübersicht-PDFs",
        "terminal": True,
        "args": [],
        "tags": ["CLI"],
        "alias": "studon-client",
        # alias_args auto-injects --clip when the shell alias is invoked
        # (so `studon-client URL` → `python studon_client.py --clip URL`,
        # the preview+confirm quick-fetch flow). The plain `args` stays []
        # so --install / .desktop launches don't pick up --clip.
        "alias_args": ["--clip"],
        # Skill support: --install-skill / --uninstall-skill write
        # ~/.claude/skills/studon-client/SKILL.md from inline SKILL_MD_CONTENT.
        "skill_name": "studon-client",
        # Digest connection: advertise WHERE course PDFs land, but NO digest_run —
        # this client keeps its own download folder fresh via its @reboot
        # --daily-sync / --lecture-sync crons, so the digest must only OBSERVE the
        # directory (its ledger detects new/changed files) and never drive a fetch.
        # (--update-all is interactive/slow and would block an unattended digest run.)
        "digest_output": os.path.join(_dl, "**", "*.pdf"),
    }


if "--advertise" in sys.argv:
    print(json.dumps([_advertise_entry()]))
    sys.exit(0)

import webbrowser
import re
import email.utils
import tempfile
import shutil
import subprocess
import atexit
import time
import requests
import pyperclip
import browser_cookie3
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from bs4.element import Tag
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
import zipfile
import tarfile
import argparse
from dataclasses import dataclass, field
from tabulate import tabulate
from pathlib import Path
import logging
import logging.handlers
import yaml
import platform as platform_module
from html import escape as _html_escape, unescape as _html_unescape

try:
    import py7zr
except ImportError:
    py7zr = None

try:
    import questionary
except ImportError:
    questionary = None

try:
    import keyring
except ImportError:
    keyring = None

try:
    from cli_tools_kit import ToolInstaller, ToolMetadata, CronInstaller
    _HAS_INSTALLER = True
except ImportError:
    ToolInstaller = None  # type: ignore[assignment,misc]
    ToolMetadata = None   # type: ignore[assignment,misc]
    CronInstaller = None  # type: ignore[assignment,misc]
    _HAS_INSTALLER = False

import imaplib
import email as email_mod
from email.header import decode_header
import getpass

# --- LOGGING SETUP ---
# The log sits in CWD, inside the Syncthing tree, so every append is replicated.
# Rotate at 2 MB and keep 3 backups (at most ~8 MB on disk).
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            'studon_sync.log', maxBytes=2 * 1024 * 1024, backupCount=3,
            encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

# --- CUSTOM EXCEPTIONS (BEGINNER-FRIENDLY) ---
class StudOnError(Exception):
    """Base error with helpful suggestions for beginners."""
    def __init__(self, message: str, suggestion: str = ""):
        self.suggestion = suggestion
        full_msg = f"❌ {message}"
        if suggestion:
            full_msg += f"\n💡 Suggestion: {suggestion}"
        super().__init__(full_msg)

class FirefoxCookieError(StudOnError):
    """Cannot load Firefox cookies."""
    def __init__(self, original_error: Exception):
        super().__init__(
            "Could not load Firefox cookies",
            "Make sure Firefox is installed and you're logged into StudOn. Try closing Firefox first."
        )
        self.original_error = original_error

class NetworkError(StudOnError):
    """Network request failed."""
    def __init__(self, url: str, original_error: Exception):
        super().__init__(
            f"Network request failed for: {url}",
            "Check your internet connection and verify the URL is correct."
        )
        self.original_error = original_error

class FileSystemError(StudOnError):
    """File operation failed."""
    def __init__(self, operation: str, path: str, original_error: Exception):
        super().__init__(
            f"File {operation} failed for: {path}",
            f"Check that you have write permissions for this location."
        )
        self.original_error = original_error

# --- DATA MODELS (TYPED OBJECTS) ---
@dataclass
class FileRecord:
    """A downloaded file with metadata."""
    filepath: Path
    timestamp: datetime
    course_name: str
    size_bytes: int
    download_url: Optional[str] = None  # Track source URL to prevent duplicate downloads

    @property
    def timestamp_formatted(self) -> str:
        """Format timestamp for display/markdown."""
        return self.timestamp.strftime('%Y-%m-%d %H:%M:%S')

    @property
    def size_formatted(self) -> str:
        """Human-readable size using existing format_file_size function."""
        return format_file_size(self.size_bytes)

    def get_relative_path(self, base_path: Path) -> str:
        """Get path relative to base."""
        try:
            return str(self.filepath.relative_to(base_path))
        except ValueError:
            return str(self.filepath)

    def to_dict(self, base_path: Optional[Path] = None) -> dict:
        """Convert to dictionary for YAML serialization."""
        return {
            'filepath': self.get_relative_path(base_path) if base_path else str(self.filepath),
            'timestamp': self.timestamp.isoformat(),
            'course_name': self.course_name,
            'size_bytes': self.size_bytes,
            'download_url': self.download_url
        }

    @classmethod
    def from_dict(cls, data: dict, base_path: Optional[Path] = None) -> 'FileRecord':
        """Load from dictionary (YAML deserialization)."""
        filepath_str = data.get('filepath', '')
        if base_path and not Path(filepath_str).is_absolute():
            filepath = base_path / filepath_str
        else:
            filepath = Path(filepath_str)

        timestamp_str = data.get('timestamp', '')
        try:
            timestamp = datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            timestamp = datetime.now()

        return cls(
            filepath=filepath,
            timestamp=timestamp,
            course_name=data.get('course_name', 'Unknown'),
            size_bytes=data.get('size_bytes', 0),
            download_url=data.get('download_url')  # Optional, for backward compatibility with old metadata
        )

@dataclass
class CourseMetadata:
    """Course info with file history."""
    course_title: str
    source_url: str
    last_fetched: datetime
    file_history: List[FileRecord]
    # Verbatim timetable titles (from campo) that map to this course. Used by
    # --lecture-sync to schedule per-lecture single-course fetches.
    timetable_titles: List[str] = field(default_factory=list)

    @property
    def last_fetched_formatted(self) -> str:
        """Format last_fetched for display."""
        return self.last_fetched.strftime('%Y-%m-%d %H:%M:%S')

    def to_markdown(self, course_folder: Path) -> str:
        """Generate markdown representation using tabulate."""
        lines = [
            f"Course: {self.course_title}",
            f"Source: {self.source_url}",
            f"Last fetched: {self.last_fetched_formatted}",
        ]

        if self.file_history:
            lines.append("\n## File History\n")
            table_data = [
                [
                    record.timestamp_formatted,
                    record.get_relative_path(course_folder),
                    record.size_formatted
                ]
                for record in self.file_history
            ]
            table = tabulate(
                table_data,
                headers=["Date/Time", "File Path", "Size"],
                tablefmt="pipe"
            )
            lines.append(table)

        return "\n".join(lines)

    def to_yaml_markdown(self, course_folder: Path) -> str:
        """Generate markdown with YAML frontmatter for programmatic access."""
        # Prepare YAML frontmatter data
        yaml_data: dict = {
            'course_title': self.course_title,
            'source_url': self.source_url,
            'last_fetched': self.last_fetched.isoformat(),
        }
        # Persist timetable_titles only when set, to keep existing files clean.
        if self.timetable_titles:
            yaml_data['timetable_titles'] = list(self.timetable_titles)
        yaml_data['file_history'] = [record.to_dict(course_folder) for record in self.file_history]

        # Generate YAML frontmatter
        yaml_str = yaml.dump(yaml_data, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Generate markdown body (for human readability)
        markdown_body = self.to_markdown(course_folder)

        # Combine frontmatter and body
        return f"---\n{yaml_str}---\n\n{markdown_body}"

    @classmethod
    def from_yaml_markdown(cls, path: str) -> Optional['CourseMetadata']:
        """Load CourseMetadata from METADATA.md file with YAML frontmatter or fallback to markdown parsing.

        Args:
            path: Path to the METADATA.md file

        Returns:
            CourseMetadata object or None if file doesn't exist
        """
        if not os.path.exists(path):
            return None

        course_folder = Path(path).parent

        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Try to parse YAML frontmatter
            if content.startswith('---'):
                # Split by frontmatter delimiters
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    yaml_str = parts[1]
                    try:
                        yaml_data = yaml.safe_load(yaml_str)

                        # Parse file history from YAML
                        file_history = []
                        for record_data in yaml_data.get('file_history', []):
                            file_history.append(FileRecord.from_dict(record_data, course_folder))

                        # Parse last_fetched
                        last_fetched_str = yaml_data.get('last_fetched', '')
                        try:
                            last_fetched = datetime.fromisoformat(last_fetched_str)
                        except (ValueError, TypeError):
                            last_fetched = datetime.now()

                        raw_titles = yaml_data.get('timetable_titles', []) or []
                        timetable_titles = [str(t) for t in raw_titles if isinstance(t, (str, int, float))]
                        return cls(
                            course_title=yaml_data.get('course_title', 'Unknown Course'),
                            source_url=yaml_data.get('source_url', ''),
                            last_fetched=last_fetched,
                            file_history=file_history,
                            timetable_titles=timetable_titles,
                        )
                    except yaml.YAMLError as e:
                        logger.warning(f"Could not parse YAML frontmatter: {e}, falling back to markdown parsing")

            # Fallback: Parse old markdown format
            logger.debug("No YAML frontmatter found, parsing old markdown format")
            lines = content.split('\n')

            # Extract metadata from old format
            course_title = 'Unknown Course'
            source_url = ''
            last_fetched = datetime.now()
            file_history = []

            # Parse header lines
            for line in lines:
                if line.startswith('Course:'):
                    course_title = line.replace('Course:', '').strip()
                elif line.startswith('Source:'):
                    source_url = line.replace('Source:', '').strip()
                elif line.startswith('Last fetched:'):
                    try:
                        date_str = line.replace('Last fetched:', '').strip()
                        last_fetched = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        pass

            # Parse file history table (old format)
            in_history_section = False
            for line in lines:
                if line.strip() == "## File History":
                    in_history_section = True
                    continue
                if in_history_section and line.startswith('|') and 'Date/Time' not in line and '---' not in line:
                    parts = [p.strip() for p in line.split('|') if p.strip()]
                    if len(parts) >= 3:
                        try:
                            timestamp_dt = datetime.strptime(parts[0], '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            timestamp_dt = datetime.now()

                        file_history.append(FileRecord(
                            filepath=course_folder / parts[1],
                            timestamp=timestamp_dt,
                            course_name=course_title,
                            size_bytes=0  # Size not available in old format
                        ))

            return cls(
                course_title=course_title,
                source_url=source_url,
                last_fetched=last_fetched,
                file_history=file_history
            )

        except Exception as e:
            logger.error(f"Could not read metadata file {path}: {e}")
            return None

@dataclass
class UpdateState:
    """State tracking for auto-updater. State is persisted via RECENT_UPDATES.md."""
    last_update: Optional[datetime]
    last_success: bool = False

# --- CONFIGURATION ---
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSET_DIR = os.path.join(_SCRIPT_DIR, "assets")
_ICON_PATH = os.path.join(_ASSET_DIR, "studon-client.png")
CONFIG_FILE = os.path.join(_SCRIPT_DIR, "config.json")

def load_config() -> dict:
    """Load persistent config from config.json next to the script."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def save_config(config: dict) -> None:
    """Write config dict to config.json next to the script."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

_config = load_config()
DOWNLOAD_FOLDER = str(Path(_config.get("downloads_path", "studon_downloads")).expanduser())
STUDON_DOMAIN = 'studon.fau.de'
CAMPO_TIMETABLE_URL = 'https://www.campo.fau.de/qisserver/pages/plan/individualTimetable.xhtml?_flowId=individualTimetableSchedule-flow'
CAMPO_STUDY_PLANNER_URL = 'https://www.campo.fau.de/qisserver/pages/startFlow.xhtml?_flowId=studyPlanner-flow'
CAMPO_EXAMS_OVERVIEW_URL = 'https://www.campo.fau.de/qisserver/pages/sul/examAssessment/personExamsReadonly.xhtml?_flowId=examsOverviewForPerson-flow'
CAMPO_ENROLLMENT_INFO_URL = 'https://www.campo.fau.de/qisserver/pages/cm/exa/enrollment/info/start.xhtml?_flowId=studyservice-flow'
CAMPO_BELEGUNGEN_URL = 'https://www.campo.fau.de/qisserver/pages/cm/exa/enrollment/info/start.xhtml?_flowId=searchOwnEnrollmentInfo-flow'
RECENT_UPDATES_FILE = os.path.join(DOWNLOAD_FOLDER, "RECENT_UPDATES.md")
LECTURE_MAPPING_PATH = os.path.join(_SCRIPT_DIR, "lecture_mapping.json")
SYNC_LOCK_PATH = os.path.join(DOWNLOAD_FOLDER, ".studon_sync.lock")


# --- TITLE NORMALIZATION (used for matching only, never for storage/display) ---

_SEMESTER_PREFIX_RE = re.compile(r'^(?:sose|wise)\s*\d{4}\s*[-–]\s*', re.IGNORECASE)
_YEAR_PREFIX_RE = re.compile(r'^\d{4}\s+')
_TRAILING_MARKER_RE = re.compile(r'\s*[⚠️✅❗❌]+\s*$')


def _normalize_title(s: str) -> str:
    """Normalize a course / lecture title for fuzzy-but-deterministic matching.

    - lowercase
    - strip leading semester ("SoSe 2026 -") or year ("2026 ") prefixes
    - collapse whitespace runs to a single space
    - replace ` / ` (campo style) with a single space (folder-name style)
    - strip trailing emoji markers ("⚠️" etc.)
    """
    if not s:
        return ""
    out = s.strip()
    out = _TRAILING_MARKER_RE.sub('', out)
    out = out.lower()
    out = _SEMESTER_PREFIX_RE.sub('', out)
    out = _YEAR_PREFIX_RE.sub('', out)
    out = out.replace(' / ', ' ')
    out = re.sub(r'\s+', ' ', out).strip()
    return out


# --- LECTURE MAPPING (campo timetable ↔ tracked StudOn courses) ---

def _load_lecture_mapping_json() -> dict:
    """Load lecture_mapping.json. Returns {} when missing."""
    if not os.path.exists(LECTURE_MAPPING_PATH):
        return {"no_course_titles": [], "ignored_titles": []}
    try:
        with open(LECTURE_MAPPING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"lecture_mapping.json unreadable, treating as empty: {e}")
        return {"no_course_titles": [], "ignored_titles": []}
    data.setdefault("no_course_titles", [])
    data.setdefault("ignored_titles", [])
    return data


def _save_lecture_mapping_json(data: dict) -> None:
    """Persist lecture_mapping.json next to config.json."""
    with open(LECTURE_MAPPING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


# --- SYNC MUTEX (shared by --daily-sync and --lecture-sync) ---

def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID is running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_sync_lock(holder: str, wait_seconds: float = 0.0) -> bool:
    """Try to claim the sync lockfile. Returns True on success.

    holder: short string identifying who holds it (logged on contention).
    wait_seconds: poll for at most this long; 0 = single try.
    """
    deadline = time.time() + wait_seconds
    os.makedirs(os.path.dirname(SYNC_LOCK_PATH) or '.', exist_ok=True)
    while True:
        # Stale-lock detection
        if os.path.exists(SYNC_LOCK_PATH):
            try:
                with open(SYNC_LOCK_PATH, 'r', encoding='utf-8') as f:
                    payload = json.load(f)
                pid = int(payload.get('pid', 0))
                other = payload.get('holder', '?')
            except (OSError, json.JSONDecodeError, ValueError):
                pid, other = 0, '?'
            if pid and _pid_alive(pid):
                if time.time() >= deadline:
                    logger.info(f"Sync lock held by {other} (pid {pid}); not acquiring.")
                    return False
                time.sleep(min(2.0, max(0.5, deadline - time.time())))
                continue
            # Stale — replace.
            logger.info(f"Removing stale sync lock (pid {pid}, holder {other}).")
            try:
                os.remove(SYNC_LOCK_PATH)
            except OSError:
                pass
        try:
            fd = os.open(SYNC_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.time() >= deadline:
                return False
            time.sleep(0.5)
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'pid': os.getpid(), 'holder': holder,
                       'acquired_at': datetime.now().isoformat()}, f)
        return True


def _release_sync_lock() -> None:
    """Remove the sync lockfile if we own it. Safe to call when not held."""
    try:
        with open(SYNC_LOCK_PATH, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        if int(payload.get('pid', 0)) != os.getpid():
            return
    except (OSError, json.JSONDecodeError, ValueError):
        return
    try:
        os.remove(SYNC_LOCK_PATH)
    except OSError as e:
        logger.debug(f"Could not remove sync lock: {e}")


@dataclass
class TrackedCourse:
    """One tracked StudOn course as seen from disk."""
    metadata_path: str
    course_folder: str
    course_title: str
    source_url: str
    timetable_titles: List[str]


def _discover_tracked_courses(base_folder: str) -> List[TrackedCourse]:
    """Scan METADATA.md files and load the data needed for bucket resolution."""
    tracked: List[TrackedCourse] = []
    for root, _dirs, files in os.walk(base_folder):
        if "METADATA.md" not in files:
            continue
        # Skip Feedback subfolders — they're per-submission, not courses.
        if os.sep + "Feedback" + os.sep in root + os.sep:
            continue
        metadata_path = os.path.join(root, "METADATA.md")
        cm = CourseMetadata.from_yaml_markdown(metadata_path)
        if cm is None or not cm.source_url or cm.course_title == 'Unknown Course':
            continue
        tracked.append(TrackedCourse(
            metadata_path=metadata_path,
            course_folder=root,
            course_title=cm.course_title,
            source_url=cm.source_url,
            timetable_titles=list(cm.timetable_titles),
        ))
    return tracked


def _add_timetable_title_to_course(metadata_path: str, verbatim_title: str) -> None:
    """Append a verbatim timetable title to a course's METADATA.md (idempotent)."""
    cm = CourseMetadata.from_yaml_markdown(metadata_path)
    if cm is None:
        logger.warning(f"Cannot update {metadata_path}: file unreadable")
        return
    if verbatim_title in cm.timetable_titles:
        return
    cm.timetable_titles.append(verbatim_title)
    try:
        with open(metadata_path, 'w', encoding='utf-8') as f:
            f.write(cm.to_yaml_markdown(Path(metadata_path).parent))
        logger.info(f"Linked timetable title '{verbatim_title}' → {os.path.basename(os.path.dirname(metadata_path))}")
    except OSError as e:
        logger.error(f"Could not write {metadata_path}: {e}")


@dataclass
class ResolvedLecture:
    """A timetable entry resolved against the tracked-course set."""
    entry: Dict
    status: str                     # 'mapped' | 'no_course' | 'ignored' | 'unmapped'
    course: Optional[TrackedCourse] # set when status == 'mapped'
    match_kind: str = ''            # 'explicit' | 'normalized' | ''


def _resolve_timetable_buckets(
    entries: List[Dict],
    tracked: List[TrackedCourse],
    mapping: dict,
    auto_pin_normalized: bool = True,
) -> List[ResolvedLecture]:
    """Bucket each timetable entry against tracked courses + lecture_mapping.json.

    When auto_pin_normalized=True, a normalized match writes the verbatim
    title back into the matched course's METADATA.md to lock the link.
    """
    no_course = set(mapping.get('no_course_titles', []))
    ignored = set(mapping.get('ignored_titles', []))

    # Build lookups in priority order: explicit timetable_titles, then normalized course_title.
    explicit_lookup: Dict[str, TrackedCourse] = {}
    normalized_lookup: Dict[str, TrackedCourse] = {}
    for c in tracked:
        for vt in c.timetable_titles:
            explicit_lookup.setdefault(vt, c)
        normalized_lookup.setdefault(_normalize_title(c.course_title), c)

    results: List[ResolvedLecture] = []
    pinned_paths: set = set()
    for entry in entries:
        title = entry.get('title', '')
        if title in ignored:
            results.append(ResolvedLecture(entry, 'ignored', None))
            continue
        if title in explicit_lookup:
            results.append(ResolvedLecture(entry, 'mapped', explicit_lookup[title], 'explicit'))
            continue
        norm = _normalize_title(title)
        if norm and norm in normalized_lookup:
            course = normalized_lookup[norm]
            if auto_pin_normalized and course.metadata_path not in pinned_paths:
                _add_timetable_title_to_course(course.metadata_path, title)
                course.timetable_titles.append(title)
                explicit_lookup[title] = course
                pinned_paths.add(course.metadata_path)
            results.append(ResolvedLecture(entry, 'mapped', course, 'normalized'))
            continue
        if title in no_course:
            results.append(ResolvedLecture(entry, 'no_course', None))
            continue
        results.append(ResolvedLecture(entry, 'unmapped', None))
    return results

# --- PLATFORM DETECTION ---

def check_platform_compatibility() -> None:
    """
    Checks if running on tested platform and logs warnings if not.
    Only tested on Kubuntu/Ubuntu Linux.
    """
    system = platform_module.system()
    is_tested = False

    if system == "Linux":
        # Try to detect if it's Ubuntu/Kubuntu
        try:
            with open('/etc/os-release', 'r') as f:
                os_release = f.read()
                if 'Ubuntu' in os_release or 'ubuntu' in os_release.lower():
                    is_tested = True
        except (FileNotFoundError, PermissionError):
            pass

    if not is_tested:
        distro_info = f"{system}"
        try:
            distro_info = f"{system} {platform_module.release()}"
        except:
            pass

        logger.warning("=" * 70)
        logger.warning("⚠️  PLATFORM WARNING ⚠️")
        logger.warning("=" * 70)
        logger.warning(f"This script has only been tested on Kubuntu/Ubuntu Linux.")
        logger.warning(f"You are running on: {distro_info}")
        logger.warning("")
        logger.warning("The script may encounter issues with:")
        logger.warning("  • Firefox cookie access")
        logger.warning("  • Process detection")
        logger.warning("  • File paths and permissions")
        logger.warning("")
        logger.warning("If you experience problems, please:")
        logger.warning("  • Try running manually: python3 studon_client.py --update-all")
        logger.warning("  • Check GitHub issues for platform-specific solutions")
        logger.warning("  • Consider contributing platform support!")
        logger.warning("=" * 70)

# --- HELPER FUNCTIONS ---

def is_valid_url(url_string: str) -> bool:
    """Checks if a string is a well-formed URL."""
    if not isinstance(url_string, str) or not url_string:
        return False
    try:
        result = urlparse(url_string)
        return all([result.scheme, result.netloc]) and result.scheme in ['http', 'https']
    except (ValueError, AttributeError):
        return False

def _url_host_matches(url: str, domain: str) -> bool:
    """True if *url*'s host is exactly *domain* or a sub-domain of it.

    A real hostname check on the parsed URL — never a substring test. A
    substring test ('studon.fau.de' in url) is bypassed by hosts such as
    'studon.fau.de.attacker.com' or by 'attacker.com/?studon.fau.de'.
    """
    if not isinstance(url, str):
        return False
    try:
        host = (urlparse(url).hostname or '').lower()
    except (ValueError, AttributeError):
        return False
    domain = domain.lower().strip('.')
    return bool(host) and (host == domain or host.endswith('.' + domain))

def _is_studon_url(url: str) -> bool:
    """True if *url* is a well-formed URL whose host is StudOn (or a sub-domain).

    The canonical entry-point gate: a real host check, never `STUDON_DOMAIN in
    url` — a substring test accepts lookalikes like 'studon.fau.de.attacker.com'.
    """
    return is_valid_url(url) and _url_host_matches(url, STUDON_DOMAIN)

# The link file create_course_link_file() writes next to every METADATA.md.
_COURSE_LINK_FILENAME = "Link to StudOn.html"


def _atomic_write_text(path: str, text: str) -> None:
    """Replace `path` with `text` in one step.

    A plain open(path, 'w') truncates first and writes second; a crash, a kill
    or a serialisation error in between leaves a 0-byte file. For METADATA.md
    that is silent data loss — the course carries its source_url nowhere else
    and drops out of every scan that keys on it. Writing a sibling temp file
    and os.replace()ing it keeps the old content until the new one is complete.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".METADATA-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates 0600. Keep the mode the file already had, else the
        # umask default, so a replaced file does not quietly become private.
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            umask = os.umask(0o022)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def recover_course_from_link_file(course_folder: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort ``(course_title, source_url)`` from a folder's link file.

    "Link to StudOn.html" is written beside METADATA.md by
    create_course_link_file() and carries the same two facts, which makes it
    the repair source when METADATA.md itself is empty or unparsable.
    Returns ``(None, None)`` when nothing trustworthy can be read; the URL is
    only returned once it passes the StudOn host check.
    """
    link_path = os.path.join(course_folder, _COURSE_LINK_FILENAME)
    try:
        with open(link_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return None, None

    source_url = None
    match = (re.search(r'refresh"\s+content="[^"]*?url=([^"]+)"', content)
             or re.search(r'href="([^"]+)"', content))
    if match:
        candidate = _html_unescape(match.group(1)).strip()
        if _is_studon_url(candidate):
            source_url = candidate

    course_title = None
    title_match = re.search(r'<p>Course:\s*(.*?)</p>', content, re.DOTALL)
    if title_match:
        course_title = _html_unescape(title_match.group(1)).strip() or None

    return course_title, source_url
def find_all_metadata_files(base_folder: str) -> List[Tuple[str, str, str]]:
    """
    Finds all METADATA.md files in the download folder.
    Returns a list of tuples: (metadata_file_path, source_url, course_folder_path)

    Skips a METADATA.md that sits directly at base_folder (the download root) — a
    tracked course always lives inside its own subfolder. Entries whose source URL
    fails is_valid_url() (defensive guard against historical garbage like
    `source_url: h`) and empty or unparsable files fall back to the sibling
    "Link to StudOn.html"; a course is only dropped when that fails too, and then
    loudly.
    """
    metadata_files = []
    base_folder_abs = os.path.abspath(base_folder)

    for root, dirs, files in os.walk(base_folder):
        if "METADATA.md" not in files:
            continue
        metadata_path = os.path.join(root, "METADATA.md")
        if os.path.abspath(root) == base_folder_abs:
            logger.warning(
                f"Ignoring stray METADATA.md at download-folder root: {metadata_path}. "
                "A tracked course must live in its own subfolder."
            )
            continue
        source_url = None
        try:
            with open(metadata_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            # The markdown body carries "Source:"; the YAML frontmatter the same
            # fact as "source_url:". Accept either so a half-written file still parses.
            match = (re.search(r'^Source:\s*(.+)$', content, re.MULTILINE)
                     or re.search(r'^source_url:\s*(.+)$', content, re.MULTILINE))
            if match:
                candidate = match.group(1).strip().strip('"\'')
                if is_valid_url(candidate):
                    source_url = candidate
                else:
                    logger.warning(f"{metadata_path}: invalid source_url {candidate!r}.")
        except Exception as e:
            logger.warning(f"Could not read {metadata_path}: {e}")

        if source_url is None:
            # An empty or unparsable METADATA.md used to drop its course out of
            # every scan without a word, so --update-all silently stopped
            # refreshing it. The sibling link file holds the same source URL;
            # recover from it and let the next fetch rewrite the metadata.
            _, recovered_url = recover_course_from_link_file(root)
            if recovered_url:
                logger.warning(
                    f"{metadata_path} is empty or unparsable — recovered source_url from "
                    f"{_COURSE_LINK_FILENAME}. The next fetch rewrites the metadata."
                )
                source_url = recovered_url
            else:
                logger.warning(
                    f"Skipping {metadata_path}: no usable source_url, and no "
                    f"{_COURSE_LINK_FILENAME} beside it to recover one from. "
                    f"Re-add this course by running the scraper on its StudOn URL."
                )
                continue

        metadata_files.append((metadata_path, source_url, root))

    return metadata_files

def get_url_and_download_path_from_sources() -> tuple[Optional[str], Optional[str]]:
    """Tries to get a URL and download path from command-line args, clipboard, or user input."""
    download_path = None

    # Check if URL was passed as command-line argument
    if len(sys.argv) > 1:
        provided_url = sys.argv[1]
        if is_valid_url(provided_url):
            print(f"✅ Using URL from command-line argument: {provided_url}")
            # Check if download path was also provided
            if len(sys.argv) > 2:
                download_path = sys.argv[2]
                print(f"✅ Using download path from command-line argument: {download_path}")
            return provided_url, download_path
        else:
            print(f"❌ Invalid URL provided as argument: {provided_url}")
            return None, None

    try:
        clipboard_content = pyperclip.paste()
        if is_valid_url(clipboard_content):
            print(f"✅ Found valid URL in clipboard: {clipboard_content}")
            return clipboard_content, download_path
    except (pyperclip.PyperclipException, pyperclip.PyperclipWindowsException):
        print("INFO: Could not access clipboard. Please provide a URL manually.")

    while True:
        url_input = input("➡️ Please paste or type the StudOn URL and press Enter (or leave blank to exit): ")
        if not url_input: return None, None
        if is_valid_url(url_input): return url_input, download_path
        print("❌ The entered text is not a valid URL. Please try again.")

def clean_filename(name: str) -> str:
    """Removes characters that are illegal or unsafe in file paths.

    Path separators and shell-illegal characters are stripped. A result of
    '.' or '..' is rejected outright: a remote-controlled link text must
    never become a path-traversal component when joined into a download path.
    """
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    if cleaned in ('.', '..'):
        return ''
    return cleaned

def extract_course_title(page_url: str, session: requests.Session, debug: bool = False) -> Optional[str]:
    """
    Extracts the course title from a StudOn page.
    Tries multiple common StudOn HTML patterns to find the title.
    """
    if not _url_host_matches(page_url, STUDON_DOMAIN):
        logger.warning(f"Refusing to fetch off-domain URL for course title: {page_url}")
        return None
    try:
        response = session.get(page_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Save HTML for debugging if requested
        if debug:
            debug_file = os.path.join(DOWNLOAD_FOLDER, "debug_page.html")
            os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
            with open(debug_file, 'w', encoding='utf-8') as f:
                f.write(response.text)
            logger.debug(f"Saved debug HTML to: {debug_file}")

        # Strategy 1: Try to find h1 tags (with or without classes)
        h1_tags = soup.find_all('h1')
        if debug:
            logger.debug(f"Found {len(h1_tags)} h1 tags")

        for h1 in h1_tags:
            title = h1.get_text(strip=True)
            # Skip navigation/generic headers
            if title and title.lower() not in ['studon', 'home', 'startseite', 'navigation']:
                if debug:
                    logger.debug(f"Found h1 title: {title}")
                return clean_filename(title)

        # Strategy 2: Look for ILIAS-specific title elements
        title_selectors = [
            ('div', {'class': re.compile(r'il.*Title|PageTitle', re.IGNORECASE)}),
            ('span', {'class': re.compile(r'il.*Title', re.IGNORECASE)}),
            ('h2', {}),  # Sometimes course title is in h2
        ]

        for tag_name, attrs in title_selectors:
            elements = soup.find_all(tag_name, attrs) if attrs else soup.find_all(tag_name)
            for element in elements:
                title = element.get_text(strip=True)
                # Skip short or generic titles
                if title and len(title) > 3 and title.lower() not in ['studon', 'home', 'startseite']:
                    if debug:
                        logger.debug(f"Found {tag_name} title: {title}")
                    return clean_filename(title)

        # Strategy 3: Try meta tags
        meta_title = soup.find('meta', attrs={'property': 'og:title'})
        if meta_title and meta_title.get('content'):
            title = meta_title['content'].strip()
            if debug:
                logger.debug(f"Found meta og:title: {title}")
            return clean_filename(title)

        # Strategy 4: Fallback to page title from <title> tag
        page_title = soup.find('title')
        if page_title:
            title_text = page_title.get_text(strip=True)
            # Remove common prefixes like "StudOn - " or "ILIAS - "
            title_text = re.sub(r'^(StudOn|ILIAS)\s*[-:]\s*', '', title_text, flags=re.IGNORECASE).strip()
            if title_text and len(title_text) > 3:
                if debug:
                    logger.debug(f"Using title tag: {title_text}")
                return clean_filename(title_text)

        if debug:
            logger.debug("No title found with any strategy")

        return None
    except Exception as e:
        logger.error(f"Could not extract course title: {e}")
        if debug:
            import traceback
            logger.debug(traceback.format_exc())
        return None

def clear_download_folder(folder_path: str) -> None:
    """Completely removes and recreates the download folder to ensure fresh content."""
    if os.path.exists(folder_path):
        print(f"🗑️ Clearing existing download folder: {folder_path}")
        shutil.rmtree(folder_path)
    os.makedirs(folder_path, exist_ok=True)
    print(f"📁 Created fresh download folder: {folder_path}")

def _is_safe_archive_member(member_name: str, extract_dir: str) -> bool:
    """True if extracting *member_name* lands inside *extract_dir*.

    Rejects absolute paths and '..' traversal. Used to vet archive members
    before extraction so a crafted archive cannot write outside its folder.
    """
    if not member_name:
        return True  # empty / pure-directory entries are harmless
    if os.path.isabs(member_name) or member_name.startswith(('/', '\\')):
        return False
    base = os.path.realpath(extract_dir)
    dest = os.path.realpath(os.path.join(base, member_name))
    return dest == base or dest.startswith(base + os.sep)


def _safe_tar_extract(tar_ref: tarfile.TarFile, extract_dir: str) -> None:
    """Extract a tar archive without escaping *extract_dir*.

    tarfile.extractall() honours '..' members, absolute paths, symlinks and
    hardlinks by default (CVE-2007-4559), so members are vetted here: links
    and device/fifo special files are dropped, traversal members are dropped,
    and the stdlib 'data' filter is applied as a second layer when available.
    """
    safe_members = []
    for m in tar_ref.getmembers():
        if m.issym() or m.islnk():
            logger.warning(f"Archive: dropping link member {m.name!r} from tar.")
            continue
        if m.ischr() or m.isblk() or m.isfifo():
            logger.warning(f"Archive: dropping special-file member {m.name!r} from tar.")
            continue
        if not _is_safe_archive_member(m.name, extract_dir):
            logger.warning(f"Archive: dropping path-traversal member {m.name!r} from tar.")
            continue
        safe_members.append(m)
    if hasattr(tarfile, 'data_filter'):
        tar_ref.extractall(extract_dir, members=safe_members, filter='data')
    else:
        tar_ref.extractall(extract_dir, members=safe_members)


def extract_archive(archive_path: str) -> bool:
    """
    Extracts a single archive file (.zip, .tar, .tar.gz, .tar.bz2, .7z).
    Creates a folder named after the archive file (without extension) and extracts into it.
    Returns True if extraction was successful, False otherwise.
    """
    try:
        parent_dir = os.path.dirname(archive_path)
        filename = os.path.basename(archive_path)

        # Get filename without extension for folder name
        if filename.endswith('.tar.gz'):
            folder_name = filename[:-7]
        elif filename.endswith('.tar.bz2'):
            folder_name = filename[:-8]
        elif filename.endswith(('.tgz', '.tbz2')):
            folder_name = filename[:-4]
        elif filename.endswith(('.zip', '.tar', '.7z')):
            folder_name = filename.rsplit('.', 1)[0]
        else:
            folder_name = filename

        # Create extraction directory with archive name
        extract_dir = os.path.join(parent_dir, folder_name)

        # Check if extraction directory already has content (skip to avoid overwriting)
        if os.path.exists(extract_dir) and os.listdir(extract_dir):
            logger.debug(f"      ⏭️  Skipped extraction (folder already exists): {filename}")
            return False  # Not an error, just already extracted

        os.makedirs(extract_dir, exist_ok=True)

        if archive_path.endswith('.zip'):
            print(f"      📦 Extracting ZIP: {filename}")
            with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                unsafe = [n for n in zip_ref.namelist()
                          if not _is_safe_archive_member(n, extract_dir)]
                if unsafe:
                    logger.warning(f"Refusing ZIP {filename}: {len(unsafe)} member(s) "
                                   f"escape the extraction dir, e.g. {unsafe[0]!r}")
                    print(f"      ❌ Refused unsafe ZIP {filename} (path traversal).")
                    return False
                zip_ref.extractall(extract_dir)
            return True

        elif archive_path.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tgz', '.tbz2')):
            print(f"      📦 Extracting TAR: {filename}")
            with tarfile.open(archive_path, 'r:*') as tar_ref:
                _safe_tar_extract(tar_ref, extract_dir)
            return True

        elif archive_path.endswith('.7z'):
            if py7zr is None:
                print(f"      ⚠️ Skipping 7z file (py7zr not installed): {filename}")
                print(f"         Install it with: pip install py7zr")
                return False
            print(f"      📦 Extracting 7z: {filename}")
            with py7zr.SevenZipFile(archive_path, 'r') as archive:
                unsafe = [n for n in (archive.getnames() or [])
                          if not _is_safe_archive_member(n, extract_dir)]
                if unsafe:
                    logger.warning(f"Refusing 7z {filename}: {len(unsafe)} member(s) "
                                   f"escape the extraction dir, e.g. {unsafe[0]!r}")
                    print(f"      ❌ Refused unsafe 7z {filename} (path traversal).")
                    return False
                archive.extractall(extract_dir)
            return True

    except Exception as e:
        print(f"      ❌ Error extracting {archive_path}: {e}")
        return False

def extract_all_archives(root_path: str) -> int:
    """
    Recursively finds and extracts all archive files in the directory tree.
    Returns the number of successfully extracted archives.
    """
    extracted_count = 0
    archive_extensions = ('.zip', '.tar', '.tar.gz', '.tar.bz2', '.7z', '.tgz', '.tbz2')

    # Use os.walk to traverse directory tree
    for dirpath, dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            if filename.lower().endswith(archive_extensions):
                archive_path = os.path.join(dirpath, filename)
                if extract_archive(archive_path):
                    extracted_count += 1

    return extracted_count

def format_file_size(size_bytes: int) -> str:
    """Convert file size in bytes to human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

def _check_remote_modified(session: requests.Session, file_url: str, last_fetched: datetime, local_path: str) -> bool:
    """
    Returns True if the remote file is newer than last_fetched.
    Conservative: returns False on any error or when the server gives ambiguous info.
    Never overwrites local files — caller saves remote version as .new.
    """
    try:
        lf_http = email.utils.formatdate(last_fetched.timestamp(), usegmt=True)
        head = session.head(file_url, headers={'If-Modified-Since': lf_http},
                            timeout=10, allow_redirects=True)
        if head.status_code == 304:
            return False
        if head.status_code == 200:
            remote_size = head.headers.get('Content-Length')
            if remote_size:
                local_size = os.path.getsize(local_path)
                return int(remote_size) != local_size
        return False
    except Exception:
        return False


def update_recent_files_log(downloaded_files_info: List[FileRecord], base_download_path: str) -> None:
    """
    Updates the RECENT_UPDATES.md file with newly downloaded files.

    Args:
        downloaded_files_info: List of FileRecord objects
        base_download_path: Base path for downloads (to create relative paths)
    """
    if not downloaded_files_info:
        return

    log_file = os.path.join(base_download_path, "RECENT_UPDATES.md")
    base_path = Path(base_download_path)

    # Read existing entries (keep as strings for backward compatibility)
    existing_entries = []
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                # Skip header lines and extract table rows
                for line in lines:
                    if line.startswith('|') and 'Date/Time' not in line and '---' not in line:
                        existing_entries.append(line.strip())
        except Exception as e:
            logger.warning(f"Could not read existing log: {e}")

    # Format new entries as table data for tabulate
    new_table_data = []
    for record in downloaded_files_info:
        rel_path = record.get_relative_path(base_path)
        filename = record.filepath.name
        new_table_data.append([
            record.timestamp_formatted,
            record.course_name,
            filename,
            rel_path,
            record.size_formatted
        ])

    # Convert new entries to markdown table format strings (for sorting with old entries)
    new_entries = []
    for row in new_table_data:
        entry = f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |"
        new_entries.append(entry)

    # Combine all entries (new + existing)
    all_entries = new_entries + existing_entries

    # Sort by timestamp (newest first)
    def get_timestamp(entry_line: str) -> str:
        parts = entry_line.split('|')
        if len(parts) >= 2:
            return parts[1].strip()  # timestamp is second column
        return ""

    all_entries.sort(key=get_timestamp, reverse=True)

    # Write the complete log file
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write("# StudOn Recent Updates\n\n")
            f.write(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            # Use tabulate for clean header/separator
            table_header = tabulate(
                [],
                headers=["Date/Time", "Course", "Filename", "Relative Path", "Size"],
                tablefmt="pipe"
            )
            # Write just the header lines
            f.write(table_header + "\n")
            # Write all entries
            for entry in all_entries:
                f.write(entry + "\n")

        logger.debug(f"Updated recent files log: {log_file}")
    except Exception as e:
        logger.error(f"Could not write log file: {e}")

def create_course_link_file(course_folder: Path, course_title: str, source_url: str) -> None:
    """
    Creates an HTML redirect file to open the course in browser.
    Works universally across all platforms and browsers.

    Args:
        course_folder: Path to the course folder
        course_title: Title of the course (used only for display in HTML)
        source_url: URL of the StudOn course
    """
    try:
        # Always use "Link to StudOn" as filename for consistency
        link_filename = "Link to StudOn.html"
        link_path = course_folder / link_filename

        # Escape both interpolated values. source_url and course_title can
        # carry attacker-influenced content (a crafted course page, a campo
        # redirect target); never inject them raw into HTML.
        safe_url = _html_escape(source_url, quote=True)
        safe_title = _html_escape(course_title, quote=True)

        # Create HTML redirect file with meta-refresh (instant redirect)
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0; url={safe_url}">
    <title>Redirecting to StudOn - {safe_title}</title>
    <style>
        body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
        a {{ color: #0066cc; text-decoration: none; }}
    </style>
</head>
<body>
    <h2>Redirecting to StudOn...</h2>
    <p>Course: {safe_title}</p>
    <p>If you are not redirected automatically, <a href="{safe_url}">click here</a>.</p>
</body>
</html>
"""

        with open(link_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        logger.debug(f"Created course link file: {link_path}")
    except Exception as e:
        logger.warning(f"Could not create course link file: {e}")

def update_course_metadata(metadata_path: str, course_title: Optional[str], source_url: str, downloaded_files_info: List[FileRecord]) -> None:
    """
    Updates a course's METADATA.md file with file history using YAML frontmatter format.

    Args:
        metadata_path: Path to the course's METADATA.md file
        course_title: Title of the course (can be None)
        source_url: Source URL of the course
        downloaded_files_info: List of FileRecord objects
    """
    course_folder = Path(metadata_path).parent

    # Refuse to write a METADATA.md directly at the download-folder root. A tracked
    # course always lives in its own subfolder; writing at the root produces a
    # stray METADATA that find_all_metadata_files() then re-processes forever
    # (see historical "Invalid URL 'h'" loop).
    if course_folder.resolve() == Path(DOWNLOAD_FOLDER).resolve():
        logger.warning(
            f"Refusing to write METADATA at download-folder root ({metadata_path}). "
            f"Course title would have been: {course_title!r}, source: {source_url!r}."
        )
        return

    # Load existing metadata using the new from_yaml_markdown method
    # This handles both YAML frontmatter and old markdown formats
    existing_metadata = CourseMetadata.from_yaml_markdown(metadata_path)

    existing_history: List[FileRecord] = []
    existing_titles: List[str] = []
    if existing_metadata:
        existing_history = existing_metadata.file_history
        existing_titles = existing_metadata.timetable_titles
        # Use existing course title and source URL if not provided
        if not course_title:
            course_title = existing_metadata.course_title
        if not source_url:
            source_url = existing_metadata.source_url

    # Combine new and existing file history
    all_history = downloaded_files_info + existing_history

    # Sort by timestamp (newest first)
    all_history.sort(key=lambda r: r.timestamp, reverse=True)

    # Create CourseMetadata object and write to YAML markdown format
    metadata = CourseMetadata(
        course_title=course_title or 'Unknown Course',
        source_url=source_url,
        last_fetched=datetime.now(),
        file_history=all_history,
        timetable_titles=existing_titles,
    )

    try:
        # Atomic: a failed serialisation or a killed process must not leave a
        # truncated METADATA.md behind (see _atomic_write_text).
        _atomic_write_text(metadata_path, metadata.to_yaml_markdown(course_folder))
    except Exception as e:
        logger.error(f"Could not write metadata file: {e}")

    # Create clickable link file for easy browser access
    create_course_link_file(course_folder, course_title or 'Unknown Course', source_url)

# --- CORE LOGIC ---

def discover_items_recursive(page_url: str, current_path: str, session: requests.Session, file_list: List[Dict[str, str]], course_title: Optional[str] = None, debug: bool = False, _visited: Optional[set] = None) -> None:
    """
    Recursively scans StudOn pages, identifying files and folders.
    Supports classic ILIAS (il_ContainerListItem) and ILIAS 7+ (il-std-item, goto.php).
    """
    if _visited is None:
        _visited = set()
    if page_url in _visited:
        return
    _visited.add(page_url)

    if not _url_host_matches(page_url, STUDON_DOMAIN):
        logger.warning(f"Skipping off-domain page during crawl: {page_url}")
        return

    try:
        response = session.get(page_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as e:
        print(f"   ❌ Could not access {page_url}. Error: {e}. Skipping.")
        return

    # Detect redirect to ILIAS login page — session expired or not logged in
    if 'ilstartupgui' in response.url or '/login.php' in response.url:
        raise StudOnError("Session expired — redirected to login page.", "Log into StudOn in Firefox and retry.")

    if debug:
        debug_file = os.path.join(DOWNLOAD_FOLDER, f"debug_discovery_{abs(hash(page_url)) % 10000}.html")
        os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
        with open(debug_file, 'w', encoding='utf-8') as f:
            f.write(response.text)
        all_links = soup.find_all('a', href=True)
        classic_items = soup.find_all('div', class_='il_ContainerListItem')
        std_items = soup.find_all('div', class_='il-std-item')
        sendfile_links = [l for l in all_links if 'cmd=sendfile' in l.get('href', '')]
        goto_file_links = [l for l in all_links if 'target=file_' in l.get('href', '')]
        goto_fold_links = [l for l in all_links if re.search(r'target=(fold|cat|crs)_', l.get('href', ''))]
        print(f"   [DEBUG] {page_url[:80]}")
        print(f"   [DEBUG]   il_ContainerListItem: {len(classic_items)}  il-std-item: {len(std_items)}")
        print(f"   [DEBUG]   cmd=sendfile: {len(sendfile_links)}  goto file: {len(goto_file_links)}  goto folder: {len(goto_fold_links)}")
        print(f"   [DEBUG]   Saved HTML → {debug_file}")

    def _add_file(url, name):
        if not name:
            return
        if not _url_host_matches(url, STUDON_DOMAIN):
            logger.warning(f"Skipping off-domain file link: {url}")
            return
        file_list.append({'url': url, 'path': current_path, 'name': name, 'course_title': course_title or 'Unknown Course'})
        if debug:
            print(f"   ✓ Found file: {name}")

    def _enter_folder(url, name):
        if name:
            new_path = os.path.join(current_path, name)
            if debug:
                print(f"   ↳ Entering folder: {name}")
            discover_items_recursive(url, new_path, session, file_list, course_title, debug, _visited)

    NAV_TEXTS = {'home', 'back', 'up', 'zurück', 'startseite', 'zur übersicht', 'breadcrumb'}

    # --- Strategy 1: Classic ILIAS (il_ContainerListItem) ---
    classic_items = soup.find_all('div', class_='il_ContainerListItem')
    if classic_items:
        for item in classic_items:
            link_tag = item.find('a', class_='il_ContainerItemTitle')
            if not link_tag:
                continue
            item_url: str = urljoin(page_url, link_tag['href'])
            item_name: str = clean_filename(link_tag.text)
            parent_container = item.find_parent('div', class_='ilContainerListItemOuter')
            is_folder: bool = False
            if parent_container:
                is_folder = bool(parent_container.find('img', alt=re.compile(r'Folder|Ordner', re.IGNORECASE)))
            if is_folder:
                _enter_folder(item_url, item_name)
            elif 'cmd=sendfile' in link_tag.get('href', ''):
                _add_file(item_url, item_name)
        # Supplement: catch il_ContainerItemTitle sendfile links outside any il_ContainerListItem
        _captured = {urljoin(page_url, i.find('a', class_='il_ContainerItemTitle')['href'])
                     for i in classic_items if i.find('a', class_='il_ContainerItemTitle')}
        for link in soup.find_all('a', class_='il_ContainerItemTitle'):
            href = link.get('href', '')
            if 'cmd=sendfile' not in href:
                continue
            url = urljoin(page_url, href)
            if url not in _captured:
                _add_file(url, clean_filename(link.get_text(strip=True)))
        return

    # --- Strategy 2: ILIAS 7+ (il-std-item) ---
    std_items = soup.find_all('div', class_='il-std-item')
    if std_items:
        seen: set = set()
        for item in std_items:
            title_el = item.find(class_='il-item-title') or item.find('h3')
            link_tag = title_el.find('a') if title_el else item.find('a', href=True)
            if not link_tag:
                continue
            href = link_tag.get('href', '')
            if not href or href in seen:
                continue
            seen.add(href)
            item_url = urljoin(page_url, href)
            item_name = clean_filename(link_tag.get_text(strip=True))
            if not item_name or item_name.lower() in NAV_TEXTS:
                continue
            icon = item.find(class_=re.compile(r'\bicon\b'))
            icon_classes = icon.get('class', []) if icon else []
            is_file = ('file' in icon_classes or
                       bool(re.search(r'target=file_', href)) or
                       'cmd=sendfile' in href)
            # Note: `target=crs_` is *intentionally* excluded — those links
            # point to other StudOn courses and must not be recursed into,
            # otherwise sub-course files land nested under the parent's tree
            # (e.g. "Maschinelles Lernen .../Introduction to Machine Learning/").
            is_folder = ('fold' in icon_classes or 'cat' in icon_classes or
                         bool(re.search(r'target=(fold|cat)_', href)) or
                         ('cmd=view' in href and 'ref_id' in href))
            if is_file:
                _add_file(item_url, item_name)
            elif is_folder:
                _enter_folder(item_url, item_name)
        # Supplement: catch il_ContainerItemTitle sendfile links not covered by il-std-item scan
        _captured2 = {f['url'] for f in file_list}
        for link in soup.find_all('a', class_='il_ContainerItemTitle'):
            href = link.get('href', '')
            if 'cmd=sendfile' not in href:
                continue
            url = urljoin(page_url, href)
            if url not in _captured2:
                _add_file(url, clean_filename(link.get_text(strip=True)))
        return

    # --- Strategy 3: Fallback — scan all links ---
    seen = set()
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        link_text = link.get_text(strip=True)
        if not link_text or len(link_text) < 2 or href in seen:
            continue
        if link_text.lower() in NAV_TEXTS:
            continue
        seen.add(href)
        item_url = urljoin(page_url, href)
        item_name = clean_filename(link_text)
        if not item_name:
            continue
        if 'cmd=sendfile' in href or bool(re.search(r'target=file_', href)):
            _add_file(item_url, item_name)
        elif (('cmd=view' in href and 'ref_id' in href) or
              bool(re.search(r'target=(fold|cat)_', href))):
            # `target=crs_` deliberately excluded: it links to another course.
            _enter_folder(item_url, item_name)

def _ref_id_from_url(url: str) -> Optional[str]:
    """The StudOn ref_id in a link, which is unique per item within a course."""
    match = re.search(r'[?&]ref_id=(\d+)', url or '')
    return match.group(1) if match else None


def _disambiguate_filepath(filepath: str, item_title: str, file_url: str,
                           claimed: Dict[str, str]) -> str:
    """A free local path for a download whose filename is already taken.

    StudOn happily serves two different items of one course under the same
    download filename (two exam PDFs both sent as final_exam.pdf). Writing both
    to one path makes each run overwrite the other and re-fetch both forever.
    Prefer the item's own StudOn title — that is what tells them apart on the
    course page — then the ref_id, which is unique per item, then a counter.
    Candidates that already exist on disk are skipped so nothing is clobbered
    or renamed; only a new collision gets a new name.
    """
    folder, name = os.path.split(filepath)
    stem, ext = os.path.splitext(name)

    candidates: List[str] = []
    title_stem = clean_filename(os.path.splitext(item_title or '')[0]).strip()
    if title_stem and title_stem != stem:
        candidates.append(title_stem + ext)
    ref_id = _ref_id_from_url(file_url)
    if ref_id:
        candidates.append(f"{stem}_ref{ref_id}{ext}")
    candidates.extend(f"{stem}_{n}{ext}" for n in range(2, 100))

    for candidate in candidates:
        alt = os.path.join(folder, candidate)
        if os.path.abspath(alt) not in claimed and not os.path.exists(alt):
            return alt
    return filepath


_SEC_MAGIC_EXTENSIONS = ((b'%PDF', '.pdf'), (b'PK\x03\x04', '.zip'))


def _restore_sec_extension(filename: str, head: bytes) -> str:
    """The real extension for a download StudOn served as '<name> .sec'.

    When an upload's extension is not on the ILIAS whitelist (often because the
    uploader dropped it), StudOn serves it as '<name> .sec'. Sniff the first
    bytes and restore .pdf or .zip. Unknown content keeps its .sec name.
    """
    stem, ext = os.path.splitext(filename)
    if ext.lower() != '.sec' or not stem.strip():
        return filename
    for magic, real_ext in _SEC_MAGIC_EXTENSIONS:
        if head.startswith(magic):
            return stem.rstrip() + real_ext
    return filename


def download_all_files(source: str, files_to_download: List[Dict[str, str]], session: requests.Session, course_title: Optional[str] = None, base_path: str = None) -> Tuple[int, List[str]]:
    """Downloads all files from the provided list.

    Returns:
        Tuple of (download_count, list_of_downloaded_filepaths)
    """
    if not files_to_download:
        return 0, []

    download_count: int = 0
    downloaded_files: List[str] = []
    downloaded_files_info: List[FileRecord] = []  # For logging
    first_file_printed: bool = False  # Track if we've moved to a new line for file list

    # Use provided base_path or fall back to DOWNLOAD_FOLDER
    metadata_folder = base_path if base_path else DOWNLOAD_FOLDER
    metadata_path = os.path.join(metadata_folder, "METADATA.md")

    # Load metadata to check last_fetched and which URLs were previously downloaded
    existing_metadata = CourseMetadata.from_yaml_markdown(metadata_path)
    last_fetched = existing_metadata.last_fetched if existing_metadata else None
    tracked_urls: set = {r.download_url for r in (existing_metadata.file_history if existing_metadata else []) if r.download_url}

    # Where each already-downloaded item landed, newest record first. The item
    # title and the download filename often differ, so without this an item
    # whose file is on disk under the download name looks missing every run.
    history_path_by_url: Dict[str, str] = {}
    history_size_by_url: Dict[str, int] = {}
    for record in (existing_metadata.file_history if existing_metadata else []):
        if record.download_url and record.download_url not in history_path_by_url:
            history_path_by_url[record.download_url] = str(record.filepath)
            history_size_by_url[record.download_url] = record.size_bytes or 0

    # Paths a past collision recorded for more than one item. Only one of them
    # is really on disk, so for these the recorded size decides whose it is;
    # everywhere else the path alone is trusted, which keeps the .new flow for
    # files that legitimately changed size upstream.
    contested_paths = {
        os.path.abspath(path)
        for path in history_path_by_url.values()
        if list(history_path_by_url.values()).count(path) > 1
    }

    # Local paths already spoken for in this run, by the item that claimed them.
    claimed_paths: Dict[str, str] = {}

    # Reserve every contested path for its rightful owner up front, so the
    # outcome does not depend on which of the two items the crawler happens to
    # reach first.
    for url, path in history_path_by_url.items():
        abs_path = os.path.abspath(path)
        if abs_path not in contested_paths:
            continue
        recorded_size = history_size_by_url.get(url) or 0
        try:
            if recorded_size and os.path.getsize(path) == recorded_size:
                claimed_paths.setdefault(abs_path, url)
        except OSError:
            pass

    for i, file_info in enumerate(files_to_download):
        file_url: str = file_info['url']
        save_path: str = file_info['path']
        expected_name: str = file_info.get('name', 'unknown_file')

        logger.debug(f"   ({i+1}/{len(files_to_download)}) Checking: {expected_name}")

        try:
            # Ensure the local directory exists
            os.makedirs(save_path, exist_ok=True)

            # Check if file already exists (before downloading)
            # Try with the expected name and also with .pdf extension if no extension
            filepath_candidates = []
            recorded = history_path_by_url.get(file_url)
            if recorded and os.path.abspath(recorded) in contested_paths:
                recorded_size = history_size_by_url.get(file_url) or 0
                try:
                    if recorded_size and os.path.getsize(recorded) != recorded_size:
                        recorded = None  # that file belongs to the other item
                except OSError:
                    pass
            if recorded:
                filepath_candidates.append(recorded)
            filepath_candidates.append(os.path.join(save_path, expected_name))
            if '.' not in expected_name:
                filepath_candidates.append(os.path.join(save_path, expected_name + '.pdf'))

            file_exists = False
            existing_path = None
            for candidate in filepath_candidates:
                # A path another item already claimed in this run is not proof
                # that THIS item is on disk — that is exactly the state a past
                # filename collision left behind in METADATA.md.
                if claimed_paths.get(os.path.abspath(candidate), file_url) != file_url:
                    continue
                if os.path.exists(candidate):
                    file_exists = True
                    existing_path = candidate
                    break

            if file_exists:
                # For script-downloaded files, check if remote has been updated since last fetch.
                # Never overwrite — save remote version as .new so local edits are preserved.
                if last_fetched and file_url in tracked_urls and existing_path:
                    new_path = existing_path + '.new'
                    if not os.path.exists(new_path) and _check_remote_modified(session, file_url, last_fetched, existing_path):
                        if not first_file_printed:
                            print()
                            first_file_printed = True
                        print(f"      ↓ {expected_name} (remote update)", end='', flush=True)
                        update_resp = session.get(file_url, stream=True, timeout=(10, 60))
                        update_resp.raise_for_status()
                        with open(new_path, 'wb') as f:
                            for chunk in update_resp.iter_content(chunk_size=8192):
                                f.write(chunk)
                        print("  ✓ (saved as .new)")
                        download_count += 1
                        downloaded_files.append(new_path)
                        try:
                            file_size = os.path.getsize(new_path)
                            downloaded_files_info.append(FileRecord(
                                filepath=Path(new_path),
                                timestamp=datetime.now(),
                                course_name=file_info.get('course_title', course_title or 'Unknown Course'),
                                size_bytes=file_size,
                                download_url=file_url
                            ))
                        except Exception as e:
                            logger.warning(f"Could not log .new file metadata: {e}")
                # Claim it, so a later item served under the same download
                # filename gets a name of its own instead of overwriting this.
                claimed_paths.setdefault(os.path.abspath(existing_path), file_url)
                logger.debug(f"   ⏭️  Skipped (already exists): {existing_path}")
                continue

            if not first_file_printed:
                print()
                first_file_printed = True
            print(f"      ↓ {expected_name}", end='', flush=True)
            file_response = session.get(file_url, stream=True, timeout=(10, 60))
            file_response.raise_for_status()

            # Try to get filename from Content-Disposition header first
            filename: str = expected_name
            if "Content-Disposition" in file_response.headers:
                content_disposition: str = file_response.headers["Content-Disposition"]
                match = re.search(r'filename="([^"]+)"', content_disposition)
                if match:
                    header_filename: str = clean_filename(match.group(1))
                    if header_filename:  # Only use if not empty
                        filename = header_filename

            # Ensure filename has a proper extension if missing
            if '.' not in filename:
                filename += '.pdf'  # Most StudOn files are PDFs

            chunks = file_response.iter_content(chunk_size=8192)
            head = b''
            if filename.lower().endswith('.sec'):
                head = next(chunks, b'')
                filename = _restore_sec_extension(filename, head)

            filepath: str = os.path.join(save_path, filename)
            owner = claimed_paths.get(os.path.abspath(filepath))
            if owner and owner != file_url:
                collided = filename
                filepath = _disambiguate_filepath(filepath, expected_name, file_url, claimed_paths)
                filename = os.path.basename(filepath)
                logger.warning(
                    f"Two StudOn items in {save_path} are both served as {collided!r}; "
                    f"saving {expected_name!r} as {filename!r} instead."
                )
            claimed_paths.setdefault(os.path.abspath(filepath), file_url)

            with open(filepath, 'wb') as f:
                f.write(head)
                for chunk in chunks:
                    f.write(chunk)

            print(f" → {filename}" if filename != expected_name else "  ✓")
            download_count += 1
            downloaded_files.append(filepath)

            # Collect metadata for logging
            try:
                file_size = os.path.getsize(filepath)
                downloaded_files_info.append(FileRecord(
                    filepath=Path(filepath),
                    timestamp=datetime.now(),
                    course_name=file_info.get('course_title', course_title or 'Unknown Course'),
                    size_bytes=file_size,
                    download_url=file_url  # Track URL to prevent duplicate downloads
                ))
            except Exception as e:
                logger.warning(f"Could not log file metadata: {e}")
        except requests.exceptions.RequestException as e:
            logger.error(f"   ❌ Error downloading {expected_name}: {e}")
        except OSError as e:
            logger.error(f"   ❌ File system error for {save_path}: {e}")

    # Update the recent files log and course metadata
    if downloaded_files_info:
        update_recent_files_log(downloaded_files_info, DOWNLOAD_FOLDER)
        update_course_metadata(metadata_path, course_title, source, downloaded_files_info)
    else:
        # Even if no new files, update the metadata with last fetched time
        update_course_metadata(metadata_path, course_title, source, [])

    return download_count, downloaded_files

# --- MAIN EXECUTION ---

def is_access_denied_title(course_title: Optional[str]) -> bool:
    """
    Checks if the course title indicates access is denied (expired login, no permissions, etc.).

    Args:
        course_title: The extracted course title

    Returns:
        True if the title appears to be an access-denied placeholder, False otherwise
    """
    if not course_title:
        return False

    # Patterns that indicate access issues (case-insensitive)
    access_denied_patterns = [
        'kein zugriffsrecht',  # German: No access right
        'zugriff verweigert',  # German: Access denied
        'no access',
        'access denied',
        'permission denied',
        'nicht berechtigt',  # German: Not authorized
        'anmeldung erforderlich',  # German: Login required
        'login required',
        'dokument',  # Sometimes shows as "Dokument X" when not logged in
        'unknown course',  # Our own placeholder
    ]

    title_lower = course_title.lower().strip()

    for pattern in access_denied_patterns:
        if pattern in title_lower:
            return True

    return False

def show_access_denied_warning(detected_title: str, start_url: str) -> None:
    """Display a helpful warning when access is denied."""
    print("\n" + "="*70)
    print("⚠️  ACCESS DENIED - Login Required")
    print("="*70)
    print(f"\n📌 Placeholder title detected: '{detected_title}'")
    print("\nThis indicates your Firefox login session has expired or you don't")
    print("have permission to access this course.")
    print("\n🔧 HOW TO FIX:")
    print("   1. Open Firefox")
    print("   2. Click on this URL to log in:")
    print(f"      {start_url}")
    print("   3. Log in with your StudOn credentials")
    print("   4. After successful login, run this script again")
    print("\n💡 TIP: Your login cookies will be automatically refreshed once you")
    print("        log in via Firefox. No need to restart Firefox.")
    print("="*70 + "\n")

def process_single_url(start_url: str, session: requests.Session, base_download_path: str = None, create_course_subfolder: bool = True, debug: bool = False) -> Tuple[int, int, List[str]]:
    """
    Processes a single StudOn URL: discovers files, downloads new ones, and extracts archives.

    Args:
        start_url: The StudOn URL to process
        session: The requests session with cookies
        base_download_path: Base path for downloads (defaults to DOWNLOAD_FOLDER)
        create_course_subfolder: If True, creates a subfolder named after the course title
        debug: If True, enables debug output and saves HTML for troubleshooting

    Returns:
        Tuple of (downloaded_count, extracted_count, list_of_downloaded_filepaths)
    """
    course_title = extract_course_title(start_url, session, debug=debug)

    # Check if extracted title is an access-denied placeholder
    if course_title and is_access_denied_title(course_title):
        detected_placeholder = course_title
        show_access_denied_warning(detected_placeholder, start_url)

        # Try to get the real title from existing metadata or base folder
        real_title = None

        # If base_download_path is provided (update mode), check for existing metadata
        if base_download_path:
            metadata_path = os.path.join(base_download_path, "METADATA.md")
            if os.path.exists(metadata_path):
                existing_metadata = CourseMetadata.from_yaml_markdown(metadata_path)
                if existing_metadata and not is_access_denied_title(existing_metadata.course_title):
                    real_title = existing_metadata.course_title

            if not real_title and os.path.exists(base_download_path):
                folder_name = os.path.basename(base_download_path)
                if folder_name and folder_name != DOWNLOAD_FOLDER:
                    real_title = folder_name

        # Use the real title if we found one
        if real_title:
            course_title = real_title
        else:
            # Last resort: keep the placeholder but warn
            print(f"⚠️ No existing course title found, using placeholder: {detected_placeholder}")

    if not course_title:
        if create_course_subfolder:
            print("⚠️ Could not determine course title. Using default folder name.")
        course_title = None

    root_folder = base_download_path if base_download_path else DOWNLOAD_FOLDER
    os.makedirs(root_folder, exist_ok=True)

    if course_title and create_course_subfolder:
        course_folder = os.path.join(root_folder, course_title)
        os.makedirs(course_folder, exist_ok=True)
        final_download_path = course_folder
    else:
        final_download_path = root_folder

    all_files_to_download: List[Dict[str, str]] = []
    discover_items_recursive(start_url, final_download_path, session, all_files_to_download, course_title, debug=debug)

    total_files: int = len(all_files_to_download)

    if total_files == 0:
        metadata_path = os.path.join(final_download_path, "METADATA.md")
        update_course_metadata(metadata_path, course_title, start_url, [])
        return 0, 0, []

    num_downloaded, downloaded_files = download_all_files(start_url, all_files_to_download, session, course_title, final_download_path)

    num_extracted = 0

    # Only extract newly downloaded archives (never re-extract existing ones)
    if downloaded_files:
        archive_extensions = ('.zip', '.tar', '.tar.gz', '.tar.bz2', '.7z', '.tgz', '.tbz2')
        for filepath in downloaded_files:
            if filepath.lower().endswith(archive_extensions):
                if extract_archive(filepath):
                    num_extracted += 1

    return num_downloaded, num_extracted, downloaded_files

# --- GIT REPO MAINTENANCE ---

# Hardened git invocation. A .git/ directory can reach the download folder via
# a downloaded/extracted archive or via Syncthing; running git inside an
# attacker-controlled repo is RCE. These -c flags neutralise the config-driven
# command-execution vectors (ext:: transport, fsmonitor, repo hooks).
_GIT_SAFE_FLAGS = [
    '-c', 'protocol.ext.allow=never',
    '-c', 'protocol.file.allow=never',
    '-c', 'core.fsmonitor=',
    '-c', 'core.hooksPath=/dev/null',
]


def _is_safe_git_remote(url: str) -> bool:
    """True only for plain https:// remotes.

    Blocks the ext:: transport (arbitrary command execution), file:// and
    local paths, scp-style git@host:path, and anything else that could run
    code or reach the local filesystem when git fetches from the repo.
    """
    if not isinstance(url, str):
        return False
    return url.strip().lower().startswith('https://')


# Config / .gitattributes tokens that turn a `git pull` into code execution.
# _GIT_SAFE_FLAGS neutralises ext::/file transports, fsmonitor and hooks, but a
# .gitattributes-assigned clean/smudge filter driver still runs a command on
# checkout and cannot be pre-empted by a `-c` flag (its name is attacker-chosen).
_GIT_UNSAFE_CONFIG_TOKENS = (
    '[filter ', '[diff ', 'fsmonitor', 'hookspath', 'sshcommand',
    'pager', 'command', '[include', 'helper',
)


def _git_repo_is_safe_to_pull(repo_root: str) -> bool:
    """True only if the repo carries no config or attributes that make a
    `git pull` execute a command.

    A repo found in the download tree is untrusted — it can arrive via an
    extracted archive or Syncthing. _GIT_SAFE_FLAGS covers the config keys it
    can override with `-c`; this gate refuses what it cannot, notably a
    .gitattributes-assigned filter/diff driver (the driver name is attacker-
    chosen, so no fixed `-c` flag neutralises it). A refused repo is skipped;
    the user can still pull it by hand if they trust it.
    """
    git_dir = os.path.join(repo_root, '.git')
    if not os.path.isdir(git_dir):
        return False
    try:
        cfg = os.path.join(git_dir, 'config')
        if os.path.exists(cfg):
            with open(cfg, 'r', errors='replace') as fh:
                text = fh.read().lower()
            if any(tok in text for tok in _GIT_UNSAFE_CONFIG_TOKENS):
                return False
        attr_files = [os.path.join(git_dir, 'info', 'attributes')]
        for root, dirs, files in os.walk(repo_root):
            if '.git' in dirs:
                dirs.remove('.git')
            if '.gitattributes' in files:
                attr_files.append(os.path.join(root, '.gitattributes'))
        for attr in attr_files:
            if not os.path.exists(attr):
                continue
            with open(attr, 'r', errors='replace') as fh:
                atext = fh.read().lower()
            if 'filter=' in atext or 'diff=' in atext:
                return False
    except OSError:
        return False
    return True


def pull_git_repos(base_folder: str) -> Tuple[int, int]:
    """
    Walk base_folder, find git repos, and fast-forward any that have a plain
    https remote. Returns (pulled_count, failed_count).

    Security: a repo found here is untrusted input — it can arrive via a
    downloaded/extracted archive or via Syncthing, and running git inside an
    attacker-controlled repo is remote code execution. So a repo is touched
    only when its origin remote is a plain https URL; git runs with hardened
    flags (_GIT_SAFE_FLAGS); updates are restricted to fast-forwards (no
    rebase, no merge driver); and there is no move-aside + re-clone fallback.
    """
    if not shutil.which('git'):
        logger.debug("git not found in PATH — skipping repo pulls")
        return 0, 0

    git_env = os.environ.copy()
    git_env['GIT_TERMINAL_PROMPT'] = '0'  # never block on a credential prompt

    pulled = 0
    failed = 0

    for root, dirs, _ in os.walk(base_folder):
        if '.git' not in dirs:
            continue
        dirs.remove('.git')  # don't recurse inside .git
        rel = os.path.relpath(root, base_folder)
        print(f"  git  {rel}", end='', flush=True)

        # Refuse repos whose config/attributes can run a command on `git pull`
        # (filter drivers, hooks, *Command keys) before invoking git at all.
        if not _git_repo_is_safe_to_pull(root):
            print("  — skipped (declares hooks/filters/command config)")
            logger.warning(f"Skipping git repo {root}: .git config/attributes "
                           f"declare a code-execution vector; pull manually if trusted.")
            continue

        # Read the remote URL with a plain config read (no transport, no
        # hooks, no index refresh) and refuse anything that is not https.
        try:
            url_result = subprocess.run(
                ['git'] + _GIT_SAFE_FLAGS + ['config', '--get', 'remote.origin.url'],
                cwd=root, capture_output=True, text=True, timeout=15, env=git_env,
            )
        except Exception as e:
            print(f"  — skipped ({e})")
            failed += 1
            continue

        remote_url = url_result.stdout.strip()
        if not _is_safe_git_remote(remote_url):
            print("  — skipped (no plain-https remote)")
            logger.info(f"Skipping git repo {root}: remote {remote_url!r} is not a plain https URL.")
            continue

        try:
            result = subprocess.run(
                ['git'] + _GIT_SAFE_FLAGS + ['pull', '--ff-only'],
                cwd=root, capture_output=True, text=True, timeout=60, env=git_env,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
                summary = lines[-1] if lines else 'ok'
                print(f"  — {summary}")
                logger.info(f"git pull ok: {root}")
                pulled += 1
            else:
                err = (result.stderr.strip() or result.stdout.strip())[:80]
                print(f"  — failed: {err}")
                logger.warning(f"git pull failed in {root}: {err}")
                failed += 1
        except subprocess.TimeoutExpired:
            print("  — timed out")
            logger.warning(f"git pull timed out in {root}")
            failed += 1
        except Exception as e:
            print(f"  — error: {e}")
            logger.warning(f"git pull error in {root}: {e}")
            failed += 1

    return pulled, failed

# --- AUTO-UPDATER HELPER FUNCTIONS ---

def can_access_studon() -> bool:
    """
    Verify we can access StudOn with Firefox cookies.
    Tries to access the 3 most recently updated courses.
    Returns True if any course is accessible.
    """
    try:
        # Load Firefox cookies
        cj = browser_cookie3.firefox(domain_name=STUDON_DOMAIN)
        session = requests.Session()
        session.cookies.update(cj)
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        # Find all courses
        if not os.path.exists(DOWNLOAD_FOLDER):
            logger.debug("Download folder doesn't exist yet")
            return False

        metadata_files = find_all_metadata_files(DOWNLOAD_FOLDER)
        if not metadata_files:
            logger.debug("No courses found to verify against")
            return False

        # Parse last_fetched timestamps from each METADATA.md
        courses_with_dates = []
        for metadata_path, source_url, course_folder in metadata_files:
            try:
                with open(metadata_path, 'r') as f:
                    content = f.read()
                    # Extract "Last fetched: YYYY-MM-DD HH:MM:SS"
                    match = re.search(r'^Last fetched:\s*(.+)$', content, re.MULTILINE)
                    if match:
                        timestamp_str = match.group(1).strip()
                        try:
                            last_fetched = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                            courses_with_dates.append((last_fetched, source_url))
                        except ValueError:
                            pass  # Skip courses with invalid timestamps
            except Exception:
                pass  # Skip courses we can't read

        if not courses_with_dates:
            logger.debug("No courses with valid timestamps found")
            return False

        # Sort by most recent and take top 3
        courses_with_dates.sort(reverse=True, key=lambda x: x[0])
        recent_courses = [url for _, url in courses_with_dates[:3]]

        # Try to access each recent course
        for url in recent_courses:
            try:
                response = session.get(url, timeout=10)
                if response.status_code == 200:
                    if 'login.php' in response.url or 'ilstartupgui' in response.url:
                        logger.debug(f"Redirected to login page for {url[:50]}")
                        continue
                    logger.debug(f"✓ Successfully accessed StudOn via: {url[:50]}...")
                    return True  # Valid login!
            except Exception:
                continue  # Try next course

        logger.debug("Could not access any recent courses - login may be unavailable")
        return False

    except Exception as e:
        logger.debug(f"Cannot access StudOn: {e}")
        return False

def can_access_campo() -> bool:
    """
    Verify we can reach campo.fau.de with Firefox cookies (i.e. the session
    is authenticated). Does a lightweight GET to a campo flow page and checks
    we are not bounced to the IdP login. Returns True if campo content serves.
    """
    try:
        cj = browser_cookie3.firefox(domain_name='campo.fau.de')
        session = requests.Session()
        session.cookies.update(cj)
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        r = session.get(CAMPO_STUDY_PLANNER_URL, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return False
        # IdP redirect / login form means we are not authenticated yet.
        if 'idp.fau.de' in r.url or 'login' in r.url.lower():
            return False
        if 'j_security_check' in r.text or 'SAMLRequest' in r.text:
            return False
        return True
    except Exception as e:
        logger.debug(f"Cannot access campo: {e}")
        return False

def load_state() -> UpdateState:
    """Load the last update timestamp from RECENT_UPDATES.md."""
    recent_updates_path = os.path.join(DOWNLOAD_FOLDER, "RECENT_UPDATES.md")
    if os.path.exists(recent_updates_path):
        try:
            with open(recent_updates_path, 'r', encoding='utf-8') as f:
                for line in f:
                    # Look for "Last updated: YYYY-MM-DD HH:MM:SS"
                    if line.startswith('Last updated:'):
                        timestamp_str = line.replace('Last updated:', '').strip()
                        try:
                            last_update = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                            return UpdateState(last_update=last_update, last_success=True)
                        except ValueError:
                            logger.warning(f"Could not parse timestamp from RECENT_UPDATES.md: {timestamp_str}")
                            break
        except Exception as e:
            logger.warning(f"Could not read RECENT_UPDATES.md: {e}")
    return UpdateState(last_update=None, last_success=False)

def was_updated_today(state: UpdateState) -> bool:
    """Check if an update was already performed today."""
    if not state.last_update:
        return False

    today = datetime.now().date()
    last_update_date = state.last_update.date()

    return last_update_date == today

# Grace window for the first-boot-wins guard: how long to wait for Syncthing
# to deliver another fleet host's sync state before deciding to scrape. Used as
# the blind-sleep fallback inside the Syncthing readiness gate below, so it is
# defined here (before that gate) rather than next to _fleet_synced_today().
FLEET_SYNC_GRACE_SECONDS = 120.0

# --- POST-BOOT SYNCTHING READINESS GATE (root-cause fix for both daemons) ---
#
# Both the Workstation and the Ideapad run their sync daemons from `@reboot`,
# which fires *before* Syncthing has connected to its peers. Each host then
# reads stale local state ("nobody synced today" / no fleet marker yet) and so
# every host scrapes — and every shared output file (RECENT_UPDATES.md,
# per-course METADATA.md, "Link to StudOn.html") becomes a Syncthing conflict.
#
# This gate blocks until the *local* Syncthing daemon has (a) connected to at
# least one peer and (b) the folder replicating our DOWNLOAD_FOLDER is in-sync,
# so a sibling host's "already-done" state is actually delivered before any
# first-fire-wins guard decides. It is deliberately best-effort: if Syncthing
# is down, the API key can't be read, the REST call errors, or no peers are
# configured, it falls back to the old blind grace-sleep and lets the scrape
# proceed — it must never crash the scrape and never hang past the timeout.

# Local Syncthing REST API base (GUI address). Localhost-only by config.
SYNCTHING_API_BASE = "http://127.0.0.1:8384"
# Syncthing config.xml carrying the <apikey> and <folder> definitions. On
# syncthing >=1.27 the XDG_STATE_HOME default is ~/.local/state/syncthing/.
SYNCTHING_CONFIG_PATHS = [
    os.path.expanduser("~/.local/state/syncthing/config.xml"),
    os.path.expanduser("~/.config/syncthing/config.xml"),
]
# Hard ceiling on how long the readiness gate may block before giving up and
# falling back to the blind grace-sleep. Sized > FLEET_SYNC_GRACE_SECONDS so
# the gate normally supersedes (rather than stacks on top of) the blind wait.
SYNCTHING_READINESS_TIMEOUT_SECONDS = 180.0
# Poll cadence while waiting for peers + folder completion.
SYNCTHING_READINESS_POLL_SECONDS = 5.0
# A folder is treated as in-sync once REST completion is at or above this.
SYNCTHING_COMPLETION_THRESHOLD = 99.0


def _read_syncthing_config() -> Optional[Tuple[str, Dict[str, str]]]:
    """Read the local Syncthing config.xml at runtime.

    Returns ``(apikey, {abs_folder_path: folder_id})`` or ``None`` if no config
    file exists or it can't be parsed. The API key is read fresh on every call
    and never hardcoded; folder paths are expanded so they can be matched
    against DOWNLOAD_FOLDER.
    """
    import xml.etree.ElementTree as ET

    for cfg_path in SYNCTHING_CONFIG_PATHS:
        if not os.path.exists(cfg_path):
            continue
        try:
            root = ET.parse(cfg_path).getroot()
        except (ET.ParseError, OSError) as e:
            logger.debug(f"Syncthing config {cfg_path} unparseable: {e}")
            continue
        apikey_el = root.find("./gui/apikey")
        apikey = (apikey_el.text or "").strip() if apikey_el is not None else ""
        if not apikey:
            logger.debug(f"Syncthing config {cfg_path} has no apikey.")
            continue
        folders: Dict[str, str] = {}
        for folder_el in root.findall("./folder"):
            fid = folder_el.get("id", "")
            fpath = folder_el.get("path", "")
            if fid and fpath:
                folders[os.path.realpath(os.path.expanduser(fpath))] = fid
        return apikey, folders
    return None


def _syncthing_folder_id_for_path(path: str, folders: Dict[str, str]) -> Optional[str]:
    """Find the Syncthing folder id whose path is `path` or an ancestor of it.

    DOWNLOAD_FOLDER is typically a sub-path of a shared folder (e.g.
    ~/Synced/OneDrive/Studium/KIM4 inside the `OneDrive` folder), so we pick
    the longest matching folder root that contains `path`.
    """
    target = os.path.realpath(os.path.expanduser(path))
    best_id: Optional[str] = None
    best_len = -1
    for froot, fid in folders.items():
        if target == froot or target.startswith(froot + os.sep):
            if len(froot) > best_len:
                best_id, best_len = fid, len(froot)
    return best_id


def _syncthing_get(endpoint: str, apikey: str, timeout: float = 5.0) -> Optional[dict]:
    """GET a Syncthing REST endpoint with the X-API-Key header.

    Returns the parsed JSON dict, or None on any transport/parse error (the
    caller treats None as "not ready yet / can't tell").
    """
    try:
        resp = requests.get(
            SYNCTHING_API_BASE + endpoint,
            headers={"X-API-Key": apikey},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.debug(f"Syncthing GET {endpoint} -> HTTP {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        logger.debug(f"Syncthing GET {endpoint} failed: {e}")
        return None


def _syncthing_is_ready(folder_id: str, apikey: str) -> bool:
    """One readiness probe: is a peer connected AND the folder in-sync?

    (a) /rest/system/connections — at least one peer with connected=True.
    (b) /rest/db/completion?folder=<id> — global completion >= threshold.
    Returns False (not ready) whenever either probe can't be answered.
    """
    conns = _syncthing_get("/rest/system/connections", apikey)
    if not conns:
        return False
    peers = conns.get("connections", {}) or {}
    if not any(info.get("connected") for info in peers.values()):
        logger.debug("Syncthing: no peers connected yet.")
        return False

    completion = _syncthing_get(f"/rest/db/completion?folder={folder_id}", apikey)
    if completion is None:
        return False
    pct = completion.get("completion")
    if pct is None:
        return False
    if pct < SYNCTHING_COMPLETION_THRESHOLD:
        logger.debug(f"Syncthing: folder {folder_id} at {pct:.1f}% (< threshold).")
        return False
    return True


def _wait_for_syncthing_ready(
    folder_path: Optional[str] = None,
    timeout_seconds: float = SYNCTHING_READINESS_TIMEOUT_SECONDS,
    poll_seconds: float = SYNCTHING_READINESS_POLL_SECONDS,
    fallback_grace_seconds: float = FLEET_SYNC_GRACE_SECONDS,
) -> bool:
    """Block until the local Syncthing has delivered fresh fleet state.

    Polls the local Syncthing REST API until a peer is connected and the folder
    replicating `folder_path` reports in-sync, up to `timeout_seconds`. Returns
    True when readiness was confirmed via the API. `folder_path` defaults to the
    live DOWNLOAD_FOLDER global (resolved at call time, so --set-download-path
    and test monkeypatching are honoured).

    Graceful degradation (mandatory) — in every failure mode this falls back to
    the legacy blind `time.sleep(fallback_grace_seconds)` and returns False,
    so the caller's existing state re-check still runs and the scrape proceeds:
      * Syncthing config / API key can't be read,
      * the DOWNLOAD_FOLDER isn't covered by any Syncthing folder,
      * the REST API is unreachable or keeps erroring,
      * readiness isn't reached before the timeout.
    It never raises and never blocks past `timeout_seconds`.
    """
    def _blind_fallback(reason: str) -> bool:
        if fallback_grace_seconds > 0:
            logger.info(
                f"Syncthing readiness gate: {reason} — falling back to a "
                f"{fallback_grace_seconds:.0f}s blind grace-sleep."
            )
            time.sleep(fallback_grace_seconds)
        else:
            logger.info(f"Syncthing readiness gate: {reason} — continuing.")
        return False

    if folder_path is None:
        folder_path = DOWNLOAD_FOLDER

    cfg = _read_syncthing_config()
    if cfg is None:
        return _blind_fallback("Syncthing config/API key unavailable")
    apikey, folders = cfg
    folder_id = _syncthing_folder_id_for_path(folder_path, folders)
    if folder_id is None:
        return _blind_fallback(
            f"no Syncthing folder covers {folder_path}"
        )

    deadline = time.time() + timeout_seconds
    probed_alive = False
    while time.time() < deadline:
        # First probe doubles as an "is Syncthing even up?" check.
        if not probed_alive:
            if _syncthing_get("/rest/system/ping", apikey) is None:
                return _blind_fallback("Syncthing REST API not reachable")
            probed_alive = True
            logger.info(
                f"Syncthing readiness gate: waiting for peer + folder '{folder_id}' "
                f"in-sync (up to {timeout_seconds:.0f}s)."
            )
        if _syncthing_is_ready(folder_id, apikey):
            logger.info(
                f"Syncthing readiness gate: folder '{folder_id}' in-sync with a "
                "connected peer — fleet state is fresh."
            )
            return True
        time.sleep(min(poll_seconds, max(0.0, deadline - time.time())))

    return _blind_fallback(
        f"folder '{folder_id}' not in-sync within {timeout_seconds:.0f}s"
    )


def _fleet_synced_today(grace_seconds: float = FLEET_SYNC_GRACE_SECONDS) -> bool:
    """First-boot-wins guard: has another fleet host already synced today?

    Both the Workstation and the Ideapad run `@reboot --daily-sync`, and the
    sync state (RECENT_UPDATES.md "Last updated:" line) lives inside the
    Syncthing-replicated download folder. Checking the state only once at
    process start is not enough: the @reboot daemon usually starts before
    Syncthing has connected, so both hosts saw "not synced today" and both
    scraped — every shared output file (METADATA.md etc.) then conflicted.

    This guard is meant to be called right before scraping. If the state still
    says "not synced today", it waits for Syncthing to actually deliver a
    fresher state file from a sibling host via the readiness gate
    (`_wait_for_syncthing_ready`, which polls peer-connection + folder
    completion and degrades to a blind `grace_seconds` sleep when the REST API
    is unavailable), then checks once more. Returns True if today's sync is
    already done (caller skips).
    """
    if was_updated_today(load_state()):
        return True
    if grace_seconds > 0:
        logger.info(
            "Daily sync: no fleet sync recorded today — waiting for Syncthing to "
            "deliver a possibly fresher state, then re-checking."
        )
        _wait_for_syncthing_ready(fallback_grace_seconds=grace_seconds)
        if was_updated_today(load_state()):
            return True
    return False


# --- FIRST-FIRE-WINS GUARD FOR --lecture-sync (per-course-per-window) ---
#
# Same idea as _fleet_synced_today() but at finer granularity: --lecture-sync
# fires three times per lecture (start-5m / start / start+5m) on BOTH hosts,
# and each fire writes the same per-course METADATA.md / "Link to StudOn.html".
# That is the root of the recurring METADATA.md conflict cluster. We persist a
# tiny marker INTO the synced tree so a sibling host (and our own later fires)
# can see "this course's window was already serviced today" and skip.
#
# The marker lives in its OWN small file rather than inside METADATA.md on
# purpose: METADATA.md is itself the file that conflicts, and every host
# rewriting it re-creates the churn we are trying to kill. A dedicated
# .lecture_sync_state.json keyed by course+date+window keeps the conflict
# surface tiny (one short JSON dict) and, being append-keyed, merges cleanly.
LECTURE_SYNC_STATE_FILE = ".lecture_sync_state.json"
# Markers older than this many days are pruned on each write so the file
# doesn't grow unbounded (one key per course per lecture per day).
LECTURE_SYNC_STATE_RETENTION_DAYS = 14


def _lecture_sync_state_path() -> str:
    """Path of the synced per-course-per-window marker file."""
    return os.path.join(DOWNLOAD_FOLDER, LECTURE_SYNC_STATE_FILE)


def _lecture_window_key(course: TrackedCourse, lecture_start: datetime) -> str:
    """Stable key for one course's lecture window on one day.

    All three fires of a lecture (start-5m / start / start+5m) share the same
    key because it is built from the lecture's START time, not the fire time —
    so the first fire on either host claims the whole window.
    """
    course_id = os.path.basename(os.path.normpath(course.course_folder))
    return f"{course_id}|{lecture_start.strftime('%Y-%m-%d')}|{lecture_start.strftime('%H:%M')}"


def _load_lecture_sync_state() -> dict:
    """Read .lecture_sync_state.json (synced). Returns {} when missing/broken."""
    path = _lecture_sync_state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"lecture_sync_state unreadable, treating as empty: {e}")
        return {}


def _lecture_already_synced(course: TrackedCourse, lecture_start: datetime) -> bool:
    """True if some fleet host already serviced this course+window today."""
    key = _lecture_window_key(course, lecture_start)
    return key in _load_lecture_sync_state()


def _mark_lecture_synced(course: TrackedCourse, lecture_start: datetime) -> None:
    """Record that this host serviced course+window, so peers/later fires skip.

    Reads-modifies-writes the synced marker file with this host's identity and a
    timestamp, pruning entries older than the retention window. Best-effort:
    any I/O error is logged and swallowed (a failed marker just means a sibling
    might double-scrape once, not a crash).
    """
    import socket
    key = _lecture_window_key(course, lecture_start)
    state = _load_lecture_sync_state()
    # Prune stale keys (date is the middle field of the key).
    cutoff = (datetime.now() - timedelta(days=LECTURE_SYNC_STATE_RETENTION_DAYS)).date()
    pruned = {}
    for k, v in state.items():
        parts = k.split('|')
        try:
            kdate = datetime.strptime(parts[1], '%Y-%m-%d').date()
        except (IndexError, ValueError):
            continue  # drop malformed keys
        if kdate >= cutoff:
            pruned[k] = v
    pruned[key] = {
        "host": socket.gethostname(),
        "ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    path = _lecture_sync_state_path()
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(pruned, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
    except OSError as e:
        logger.warning(f"Could not write lecture-sync marker {path}: {e}")


def update_all_courses(debug: bool = False, session: Optional[requests.Session] = None) -> Tuple[bool, int, int, bool, List[str]]:
    """Update all courses by scanning METADATA.md files.

    Args:
        debug: If True, enables debug output and saves HTML for troubleshooting.
        session: Pre-authenticated requests session. If None, cookies are loaded
                 from Firefox automatically.

    Returns:
        Tuple of (success, total_downloaded, total_extracted, session_expired, downloaded_files).
    """
    try:
        if session is None:
            try:
                cj = browser_cookie3.firefox(domain_name=STUDON_DOMAIN)
                session = requests.Session()
                session.cookies.update(cj)
                session.headers.update({'User-Agent': 'Mozilla/5.0'})
            except Exception as e:
                raise FirefoxCookieError(e)

        metadata_files = find_all_metadata_files(DOWNLOAD_FOLDER)

        if not metadata_files:
            print("No registered courses found.")
            return False, 0, 0, False, []

        n = len(metadata_files)
        print(f"Updating {n} course{'s' if n != 1 else ''}...")

        total_downloaded = 0
        total_extracted = 0
        total_git_pulled = 0
        total_git_failed = 0
        successful_courses = 0
        session_expired = False
        all_downloaded_files: List[str] = []

        for i, (metadata_path, source_url, course_folder) in enumerate(metadata_files, 1):
            name = os.path.basename(course_folder)
            print(f"  [{i}/{n}] {name}", end='', flush=True)

            try:
                downloaded, extracted, downloaded_paths = process_single_url(source_url, session, course_folder, create_course_subfolder=False, debug=debug)
                total_downloaded += downloaded
                total_extracted += extracted
                all_downloaded_files.extend(downloaded_paths)
                successful_courses += 1
                if downloaded:
                    print(f"  — {downloaded} new file{'s' if downloaded != 1 else ''}" +
                          (f", {extracted} extracted" if extracted else ""))
                else:
                    print("  — up to date")
            except StudOnError as e:
                print(f"  — error: {e}")
                logger.error(f"Error processing {source_url}: {e}")
                if "Session expired" in str(e):
                    session_expired = True
                    print("  Stopping: session expired. Will retry after login.")
                    break
                continue
            except Exception as e:
                print(f"  — error: {e}")
                logger.error(f"Error processing {source_url}: {e}")
                continue

        if session_expired:
            return False, 0, 0, True, []

        # Pull all git repos in the entire downloads folder (catches repos not inside any tracked course)
        git_pulled, git_failed = pull_git_repos(DOWNLOAD_FOLDER)
        total_git_pulled += git_pulled
        total_git_failed += git_failed

        parts = []
        if total_downloaded:
            parts.append(f"{total_downloaded} new file{'s' if total_downloaded != 1 else ''} downloaded")
        if total_extracted:
            parts.append(f"{total_extracted} extracted")
        if total_git_pulled:
            parts.append(f"{total_git_pulled} repo{'s' if total_git_pulled != 1 else ''} pulled")
        if total_git_failed:
            parts.append(f"{total_git_failed} git pull error{'s' if total_git_failed != 1 else ''}")
        print("Done." + (f" {', '.join(parts)}." if parts else " Nothing new."))

        return successful_courses > 0, total_downloaded, total_extracted, False, all_downloaded_files

    except Exception as e:
        logger.error(f"Error during update: {e}")
        return False, 0, 0, False, []

def _course_for_path(filepath: str) -> str:
    """Course a downloaded file belongs to: its top folder under DOWNLOAD_FOLDER.

    Falls back to the parent directory name when the file sits outside the
    download root (e.g. a custom --download-path run).
    """
    path = Path(filepath)
    try:
        rel = path.relative_to(Path(DOWNLOAD_FOLDER))
        if len(rel.parts) > 1:
            return rel.parts[0]
    except ValueError:
        pass
    parent = path.parent.name
    return parent or "StudOn"


def _group_files_by_course(files: List[str]) -> List[Tuple[str, List[str]]]:
    """Group file paths by course, keeping first-seen order of both."""
    grouped: Dict[str, List[str]] = {}
    for f in files:
        grouped.setdefault(_course_for_path(f), []).append(f)
    return list(grouped.items())


def _send_desktop_notification(n_downloaded: int, n_extracted: int, files: Optional[List[str]] = None) -> None:
    """Send a desktop notification via notify-send (Linux).

    If `files` is given, lists the basenames (capped) under the summary line
    so the user can see what was freshly fetched without opening the log.
    """
    if not shutil.which("notify-send"):
        return
    if n_downloaded:
        parts = [f"{n_downloaded} new file{'s' if n_downloaded != 1 else ''} downloaded"]
        if n_extracted:
            parts.append(f"{n_extracted} extracted")
        body = ", ".join(parts) + "."
        if files:
            max_list = 10
            listed = 0
            lines: List[str] = []
            for course, course_files in _group_files_by_course(files):
                if listed >= max_list:
                    break
                lines.append(f"{course}:")
                for f in course_files:
                    if listed >= max_list:
                        break
                    lines.append(f"  • {os.path.basename(f)}")
                    listed += 1
            if listed < len(files):
                lines.append(f"… and {len(files) - listed} more")
            body = body + "\n" + "\n".join(lines)
    else:
        body = "Everything already up to date."
    # notify-send needs DBUS_SESSION_BUS_ADDRESS when run from cron.
    # Try to inherit it from a running user session.
    env = os.environ.copy()
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        try:
            uid = os.getuid()
            result = subprocess.run(
                ["grep", "-z", "DBUS_SESSION_BUS_ADDRESS", f"/proc/{uid}/environ"],
                capture_output=True, text=True
            )
            for line in result.stdout.replace('\x00', '\n').splitlines():
                if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                    env["DBUS_SESSION_BUS_ADDRESS"] = line.split("=", 1)[1]
                    break
        except Exception:
            pass
        # Fallback: common socket path
        if "DBUS_SESSION_BUS_ADDRESS" not in env:
            uid = os.getuid()
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    try:
        subprocess.run(
            ["notify-send", "--app-name=StudOn Scraper", "--icon=emblem-downloads",
             "StudOn Sync Complete", body],
            env=env, timeout=5
        )
    except Exception as e:
        logger.debug(f"Desktop notification failed: {e}")


def _notify_env() -> dict:
    """Environment for notify-send, self-discovering DBUS when run from cron.

    The @reboot cron context has no DBUS_SESSION_BUS_ADDRESS, so notify-send
    can't reach the session bus. Inherit it from a running user process, then
    fall back to the well-known per-user socket path.
    """
    env = os.environ.copy()
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        try:
            uid = os.getuid()
            result = subprocess.run(
                ["grep", "-z", "DBUS_SESSION_BUS_ADDRESS", f"/proc/{uid}/environ"],
                capture_output=True, text=True
            )
            for line in result.stdout.replace('\x00', '\n').splitlines():
                if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                    env["DBUS_SESSION_BUS_ADDRESS"] = line.split("=", 1)[1]
                    break
        except Exception:
            pass
        if "DBUS_SESSION_BUS_ADDRESS" not in env:
            uid = os.getuid()
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    return env


# One-click login prompt state. Holds the live notify-send process so a later
# successful login can dismiss the still-open notification, and the key of the
# fetch attempt the last notification belonged to.
_login_prompt: dict = {"proc": None, "last_key": None}


def _notify_login_required(login_url: str, attempt_key: str) -> None:
    """Fire a clickable desktop notification asking the user to log into StudOn.

    Visible, one-click: clicking the "In Firefox einloggen" action opens the
    login URL in the browser. The caller's existing poll loop then picks up the
    refreshed Firefox cookie on its next cycle (no extra wiring needed — polling
    already happens). The (blocking, --action implies --wait) notify-send call
    runs in a daemon thread so the poll loop is never blocked.

    attempt_key identifies the fetch attempt that is blocked by the missing
    login: one popup per attempt, not one per poll cycle. The daily sync passes
    the date, the lecture sync passes course + lecture-window start so all three
    fires of one window share a single popup. A successful login (or a manual
    dismissal) clears the key so the next expiry can notify again.

    Unlike the tray icon, this needs only DBUS (not DISPLAY), so it works even
    from the headless @reboot cron context.
    """
    if not shutil.which("notify-send"):
        return
    if _tray_closed():
        return  # the user closed the tray — stay quiet until the next login
    import threading
    proc = _login_prompt.get("proc")
    if proc is not None and proc.poll() is None:
        return  # a prompt is already on screen
    if attempt_key == _login_prompt.get("last_key"):
        return  # already notified for this fetch attempt
    _login_prompt["last_key"] = attempt_key
    try:
        proc = subprocess.Popen(
            ["notify-send", "--app-name=StudOn Scraper", "--icon=dialog-password",
             "--expire-time=120000", "--action=login=In Firefox einloggen",
             "StudOn-Login abgelaufen",
             "Sync pausiert. Klicken zum Einloggen — verschwindet nach 2 Min "
             "(oder das StudOn-Tray-Icon nutzen); der Sync holt danach automatisch nach."],
            env=_notify_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except Exception as e:
        logger.debug(f"Login notification failed: {e}")
        return
    _login_prompt["proc"] = proc

    def _wait_for_click() -> None:
        # Block until notify-send exits on its own — either the user clicked the
        # action (stdout carries the key) or the notification auto-expired after
        # its --expire-time (2 min). Deliberately NO kill-timeout: killing the
        # process early would leave a dead, unclickable notification on screen
        # (the old timeout=3600 bug — clicking it silently did nothing). This
        # popup is only a transient nudge; the durable login path is the
        # persistent StudOn tray icon.
        try:
            out, _ = proc.communicate()
            if out and "login" in out:
                logger.info("Login notification clicked — opening StudOn login in browser.")
                _open_url_in_browser(login_url)
        except Exception as e:
            logger.debug(f"Login notification click-wait failed: {e}")

    threading.Thread(target=_wait_for_click, daemon=True).start()


def _dismiss_login_prompt() -> None:
    """Close any still-open 'login required' notification after a successful login.

    Also clears the attempt latch, so the next blocked fetch attempt notifies
    again.
    """
    proc = _login_prompt.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    _login_prompt["proc"] = None
    _login_prompt["last_key"] = None
    _clear_tray_closed()


# ── Persistent system-tray icon (AppIndicator / StatusNotifierItem) ─────────
# KDE Plasma renders pystray's XEmbed icon as an invisible ghost slot, so the
# always-on tray runs as a tiny helper under the SYSTEM python3 (which has
# gi/AppIndicator; the Py3EnvShare venv hides it). The helper is driven by a
# per-host JSON status file — kept OUT of the synced download folder so it never
# creates Syncthing churn — and performs login / sync-now / open-downloads on
# its own. Best-effort throughout: no display or no AppIndicator ⇒ silently skip
# and fall back to the notify-send login prompt.
_SYSTEM_PYTHON = "/usr/bin/python3"
_TRAY_SCRIPT_PATH = os.path.join(_SCRIPT_DIR, "_studon_tray.py")
_TRAY_STATE_DIR = os.path.expanduser("~/.local/state/studon-client")
_TRAY_STATUS_PATH = os.path.join(_TRAY_STATE_DIR, "tray_status.json")
_tray_proc: Optional[subprocess.Popen] = None
_appindicator_ok: Optional[bool] = None

_STUDON_TRAY_PY = r'''#!/usr/bin/python3
"""Standalone AppIndicator system-tray for studon-client (generated file).

Runs under the SYSTEM python3, NOT the Py3EnvShare venv: it needs gi/AppIndicator
(a system dist-package the venv hides) and imports ONLY gi + stdlib so it never
pulls in the heavy studon_client module. The --lecture-sync daemon writes a small
JSON status file; this tray polls it and acts on its own.

Usage:  /usr/bin/python3 _studon_tray.py <status_json_path>
"""
import json
import os
import subprocess
import sys
import time

import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3
from gi.repository import GLib, Gtk

STATUS_FILE = sys.argv[1] if len(sys.argv) > 1 else ""

# The StudOn logo ships next to this script in assets/. Indicator.new_with_path
# prepends that folder to the icon search path, so ICON_ACTIVE resolves to the
# bundled PNG by basename while ICON_ATTENTION still comes from the system theme.
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ICON_ACTIVE = ("studon-client" if os.path.exists(os.path.join(ICON_DIR, "studon-client.png"))
               else "applications-education")
# Bundled red-badged variant of the same logo. Breeze-dark on KDE does not
# reliably swap icons on IndicatorStatus.ATTENTION, so the icon itself has to
# change; fall back to the theme name if the PNG was never generated.
ICON_ATTENTION = ("studon-client-attention"
                  if os.path.exists(os.path.join(ICON_DIR, "studon-client-attention.png"))
                  else "dialog-password")


def _read_status():
    try:
        with open(STATUS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_status(**fields):
    """Merge fields into the status JSON, atomically. Silent on failure.

    Same tmp-file + os.replace pattern the parent process uses, so a concurrent
    daemon write never sees a half-written file.
    """
    try:
        data = _read_status()
        data.update(fields)
        tmp = STATUS_FILE + ".tray.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def _humanize_age(epoch):
    """Relative age of a unix timestamp, German short form ('' if unusable)."""
    try:
        delta = int(time.time() - float(epoch))
    except Exception:
        return ""
    if delta < 0:
        delta = 0
    if delta < 60:
        return "gerade eben"
    if delta < 3600:
        return "vor " + str(delta // 60) + " Min"
    if delta < 86400:
        return "vor " + str(delta // 3600) + " Std"
    return "vor " + str(delta // 86400) + " Tg"


def _spawn(argv, **kw):
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, **kw)
    except Exception:
        pass


def _last_sync_label(st):
    """'Letzter Sync: <wallclock> (<relative age>)' from the status dict."""
    human = st.get("last_sync_human")
    age = _humanize_age(st.get("last_sync_epoch")) if st.get("last_sync_epoch") else ""
    if not human and not age:
        return "Letzter Sync: noch keiner"
    text = str(human) if human else ""
    if age:
        text = (text + " (" + age + ")") if text else age
    return "Letzter Sync: " + text


class StudonTray:
    def __init__(self):
        self.ind = AppIndicator3.Indicator.new_with_path(
            "studon-client", ICON_ACTIVE,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS, ICON_DIR)
        self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.ind.set_attention_icon_full(ICON_ATTENTION, "StudOn-Login erforderlich")
        self.ind.set_title("StudOn client")

        self.menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label="StudOn: ...")
        self.status_item.set_sensitive(False)
        self.last_item = Gtk.MenuItem(label="Letzter Sync: ...")
        self.last_item.set_sensitive(False)
        self.login_item = Gtk.MenuItem(label="In StudOn einloggen")
        self.login_item.connect("activate", self.on_login)
        self.sync_item = Gtk.MenuItem(label="Jetzt synchronisieren")
        self.sync_item.connect("activate", self.on_sync)
        self.dl_item = Gtk.MenuItem(label="Download-Ordner oeffnen")
        self.dl_item.connect("activate", self.on_downloads)
        self.quit_item = Gtk.MenuItem(label="Tray schliessen (bis zum naechsten Login)")
        self.quit_item.connect("activate", self.on_quit)

        for it in (self.status_item, self.last_item, self.login_item, self.sync_item,
                   self.dl_item, Gtk.SeparatorMenuItem(), self.quit_item):
            it.show()
            self.menu.append(it)
        self.menu.show_all()
        self.ind.set_menu(self.menu)

        self._refresh()
        GLib.timeout_add_seconds(5, self._refresh)

    @staticmethod
    def _live_state(st):
        """The status state, with a stale waiting_login demoted to idle.

        waiting_login is latched into the file by whichever sync process is
        blocked on the login; only that process clears it. When it exits (the
        daily sync finishes its run, or is killed) the flag would otherwise
        stick and the tray would demand a login forever — the 2026-09-01 bug.
        Trust the flag only while its writer is still alive.
        """
        state = st.get("state", "idle")
        if state != "waiting_login":
            return state
        pid = st.get("state_pid")
        try:
            os.kill(int(pid), 0)
            return state
        except Exception:
            return "idle"

    def _refresh(self):
        st = _read_status()
        state = self._live_state(st)
        nf = st.get("next_fire_human")
        if state == "waiting_login":
            label = "StudOn: Login erforderlich"
        elif state == "syncing":
            label = "StudOn: synchronisiert ..."
        elif nf:
            label = "StudOn: naechster Sync " + str(nf)
        else:
            label = "StudOn: bereit"
        self.status_item.set_label(label)
        self.last_item.set_label(_last_sync_label(st))
        self.login_item.set_sensitive(bool(st.get("login_url")))
        self.sync_item.set_sensitive(bool(st.get("venv_python") and st.get("script_path")))
        self.dl_item.set_sensitive(bool(st.get("downloads_path")))
        try:
            self.ind.set_status(
                AppIndicator3.IndicatorStatus.ATTENTION if state == "waiting_login"
                else AppIndicator3.IndicatorStatus.ACTIVE)
        except Exception:
            pass
        # Swap the main icon too: hosts that ignore NeedsAttention (KDE Plasma
        # with breeze-dark) otherwise show no visible change at all.
        try:
            if state == "waiting_login":
                self.ind.set_icon_full(ICON_ATTENTION, "StudOn-Login erforderlich")
            else:
                self.ind.set_icon_full(ICON_ACTIVE, "StudOn client")
        except Exception:
            pass
        return True  # keep the GLib timer alive

    def on_quit(self, _w):
        """Close the tray and keep it closed until the next successful login.

        tray_closed also silences the login popups — that is what the flag is
        for. The daemons clear it after a login, and `--tray` clears it on
        demand.
        """
        _merge_status(tray_closed=True, tray_closed_epoch=time.time())
        Gtk.main_quit()

    def on_login(self, _w):
        url = _read_status().get("login_url")
        if url:
            _spawn(["xdg-open", url])

    def on_downloads(self, _w):
        path = _read_status().get("downloads_path")
        if path:
            _spawn(["xdg-open", path])

    def on_sync(self, _w):
        st = _read_status()
        py, script = st.get("venv_python"), st.get("script_path")
        if not (py and script):
            return
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)  # do not leak the system path into the venv run
        _spawn([py, script, "--update-all"],
               cwd=os.path.dirname(script) or None, env=env)


def main():
    if not STATUS_FILE:
        print("usage: _studon_tray.py <status_json_path>", file=sys.stderr)
        return 2
    StudonTray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _system_python_has_appindicator() -> bool:
    """True if /usr/bin/python3 can import gi + AppIndicator (probed once)."""
    global _appindicator_ok
    if _appindicator_ok is not None:
        return _appindicator_ok
    _appindicator_ok = False
    if not os.path.exists(_SYSTEM_PYTHON):
        return False
    probe = (
        "import gi; gi.require_version('Gtk','3.0')\n"
        "try:\n"
        "    gi.require_version('AyatanaAppIndicator3','0.1')\n"
        "    from gi.repository import AyatanaAppIndicator3\n"
        "except Exception:\n"
        "    gi.require_version('AppIndicator3','0.1')\n"
        "    from gi.repository import AppIndicator3\n"
    )
    try:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run([_SYSTEM_PYTHON, "-c", probe],
                                capture_output=True, timeout=15, env=env)
        _appindicator_ok = result.returncode == 0
    except Exception:
        _appindicator_ok = False
    return _appindicator_ok


def _read_tray_status() -> dict:
    """The tray status JSON as a dict, {} if missing or unreadable."""
    try:
        with open(_TRAY_STATUS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid) -> bool:
    """True if a process with this pid exists (same probe the tray uses)."""
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _tray_closed() -> bool:
    """True while the user has closed the tray via its 'Tray schliessen' item.

    `tray_closed` in the status JSON means: do not relaunch the tray and do not
    fire login popups. It is cleared by the next successful login and by
    `--tray`.
    """
    return bool(_read_tray_status().get("tray_closed"))


def _clear_tray_closed() -> None:
    """Lift the user's tray-closed flag so the tray and its popups return."""
    if _tray_closed():
        _write_tray_status(tray_closed=False, tray_closed_epoch=0)


def _write_tray_status(**fields) -> None:
    """Merge-write the per-host tray status JSON atomically. Silent on failure.

    A live `waiting_login` written by another running process is never
    overwritten: both daemons write this file, and the lecture sync's routine
    `state="idle"` used to wipe the daily sync's login request. Own-pid writes
    always go through, so a process is never blocked by its own state entry.
    """
    try:
        os.makedirs(_TRAY_STATE_DIR, exist_ok=True)
        data: dict = _read_tray_status()
        if "state" in fields or "login_url" in fields:
            try:
                other_pid = int(data.get("state_pid"))
            except (TypeError, ValueError):
                other_pid = os.getpid()  # unusable pid: treat as our own
            if (data.get("state") == "waiting_login"
                    and other_pid != os.getpid()
                    and _pid_alive(other_pid)):
                # Another live process is waiting for the login. Merge the
                # descriptive fields but leave its request standing.
                fields = {k: v for k, v in fields.items()
                          if k not in ("state", "state_pid", "login_url")}
        data.update(fields)
        if "state" in fields:
            # Lets the tray tell a live waiting_login from one left by a process
            # that has since exited (see StudonTray._live_state).
            data["state_pid"] = os.getpid()
            data["state_epoch"] = time.time()
        tmp = _TRAY_STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _TRAY_STATUS_PATH)
    except Exception as e:
        logger.debug(f"Tray status write failed: {e}")


def _record_tray_sync(detail: str = "") -> None:
    """Stamp the tray status with the moment this host last finished a sync.

    Written by every completion path (lecture fire, daily sync, --update-all),
    including the tray's own "Jetzt synchronisieren" — that runs as a separate
    process, and _write_tray_status merges, so it can't clobber the daemon's
    state/next_fire fields.
    """
    _write_tray_status(
        last_sync_epoch=time.time(),
        last_sync_human=datetime.now().strftime('%a %H:%M')
        + (f" · {detail}" if detail else ""),
    )


def _ensure_tray_script() -> bool:
    """Materialize the embedded tray helper to disk if missing/outdated."""
    try:
        current = ""
        if os.path.exists(_TRAY_SCRIPT_PATH):
            with open(_TRAY_SCRIPT_PATH, encoding="utf-8") as fh:
                current = fh.read()
        if current != _STUDON_TRAY_PY:
            with open(_TRAY_SCRIPT_PATH, "w", encoding="utf-8") as fh:
                fh.write(_STUDON_TRAY_PY)
        return True
    except Exception as e:
        logger.debug(f"Tray script write failed: {e}")
        return False


def _stop_tray() -> None:
    """Terminate the tray helper (called on daemon exit via atexit)."""
    global _tray_proc
    if _tray_proc is not None and _tray_proc.poll() is None:
        try:
            _tray_proc.terminate()
        except Exception:
            pass
    _tray_proc = None


def _launch_tray(login_url: Optional[str] = None) -> None:
    """Start (or restart) the persistent AppIndicator tray helper — idempotent.

    No-op when a helper is already alive, the user closed the tray, there's no
    display, or the system python lacks AppIndicator; the daemon then relies on
    the notify-send login prompt alone.
    """
    global _tray_proc
    if _tray_proc is not None and _tray_proc.poll() is None:
        return  # already running
    if _tray_closed():
        return  # the user closed it — do not bring it back
    if not _has_display() or not _system_python_has_appindicator():
        return
    if not _ensure_tray_script():
        return
    # Descriptive fields only: forcing state="idle" here would wipe a live
    # waiting_login written by the other daemon.
    fields = dict(
        downloads_path=DOWNLOAD_FOLDER,
        venv_python=sys.executable,
        script_path=os.path.abspath(__file__),
    )
    if login_url:
        fields["login_url"] = login_url
    elif not _read_tray_status().get("login_url"):
        fields["login_url"] = ""
    if not _read_tray_status().get("state"):
        fields["state"] = "idle"
    _write_tray_status(**fields)
    try:
        env = _notify_env()
        env.pop("PYTHONPATH", None)
        env.pop("VIRTUAL_ENV", None)
        _tray_proc = subprocess.Popen(
            [_SYSTEM_PYTHON, _TRAY_SCRIPT_PATH, _TRAY_STATUS_PATH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
        atexit.register(_stop_tray)
        logger.info("StudOn tray icon started.")
    except Exception as e:
        logger.debug(f"Tray launch failed: {e}")
        _tray_proc = None


def _poll_until_login(access_check: Callable[[], bool], max_wait_seconds: float,
                      interval: float = 5.0) -> bool:
    """Poll access_check until it passes or the wall-clock budget runs out.

    The fallback for hosts where no login tray is shown (AppIndicator hosts, no
    display, missing pystray). Cheap: each call is a cookie read plus one GET.
    """
    deadline = time.time() + max_wait_seconds
    while True:
        try:
            if access_check():
                return True
        except Exception as e:
            logger.debug(f"Login poll failed: {e}")
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def _wait_for_login_via_tray(login_url: str, max_wait_seconds: Optional[int] = None,
                             access_check: Callable[[], bool] = can_access_studon) -> bool:
    """Show a tray icon while polling for a valid login.

    access_check: the predicate polled to detect a successful login. Defaults
    to can_access_studon; pass can_access_campo for a campo (re-)login.

    Returns True when login is detected, False if the tray library is not
    available / no display, the user quits via the menu, or the optional
    wall-clock timeout elapses. The caller should fall back to silent
    polling on False.

    Polling cadence: every 60s by default; on icon click or "Open login"
    menu, opens the browser and switches to every 5s for 2 minutes.

    max_wait_seconds: when set, the tray closes after this many seconds
    even if the user has not interacted. Used by --lecture-sync to
    avoid blocking a fire window indefinitely.
    """
    if not _has_display():
        return False
    # On KDE (StatusNotifierItem) the pystray XEmbed icon shows up as a second,
    # invisible tray slot whose only trace is the "StudOn: waiting for login"
    # tooltip next to the real "StudOn client" icon. Wherever AppIndicator is
    # available the persistent tray already shows the login state and carries the
    # "In StudOn einloggen" item, so skip the ghost and let the caller poll.
    if _system_python_has_appindicator():
        logger.debug("AppIndicator tray present; skipping the pystray login icon.")
        return False
    try:
        import threading
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as e:
        logger.info(f"Tray icon unavailable ({e}); falling back to silent polling.")
        return False

    try:
        img = Image.open(_ICON_PATH).convert("RGBA")
    except Exception as e:
        logger.debug(f"Tray logo {_ICON_PATH} unusable ({e}); drawing a placeholder.")
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(30, 110, 200, 255), outline=(255, 255, 255, 255), width=3)
        d.text((20, 18), "S", fill=(255, 255, 255, 255))

    state = {
        "fast_until": 0.0,    # epoch until which to poll every 5s
        "logged_in": False,
        "user_quit": False,
    }
    stop_event = threading.Event()
    deadline = time.time() + max_wait_seconds if max_wait_seconds else None

    def open_login(icon=None, item=None):
        logger.info("Tray icon: opening browser for StudOn login")
        _open_url_in_browser(login_url)
        state["fast_until"] = time.time() + 120  # 2 minutes of 5s polling

    def quit_waiter(icon, item):
        state["user_quit"] = True
        stop_event.set()
        icon.stop()

    def poller(icon):
        while not stop_event.is_set():
            try:
                if access_check():
                    state["logged_in"] = True
                    icon.stop()
                    return
            except Exception as e:
                logger.debug(f"Tray waiter login check failed: {e}")
            if deadline is not None and time.time() >= deadline:
                logger.info("Tray icon: max_wait_seconds reached, closing.")
                stop_event.set()
                try:
                    icon.stop()
                except Exception:
                    pass
                return
            interval = 5 if time.time() < state["fast_until"] else 60
            if deadline is not None:
                interval = min(interval, max(1, int(deadline - time.time())))
            stop_event.wait(interval)

    def setup(icon):
        icon.visible = True
        threading.Thread(target=poller, args=(icon,), daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Open StudOn login", open_login, default=True),
        pystray.MenuItem("Check now", lambda icon, item: state.update(fast_until=time.time() + 120)),
        pystray.MenuItem("Quit (skip sync)", quit_waiter),
    )
    icon = pystray.Icon("studon-client", img, "StudOn: waiting for login", menu)

    try:
        icon.run(setup=setup)
    except Exception as e:
        logger.warning(f"Tray icon failed to run: {e}; falling back to silent polling.")
        stop_event.set()
        return False
    stop_event.set()
    return state["logged_in"]


def _has_display() -> bool:
    """True if a graphical session is available to show the login tray icon."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# When --daily-sync runs in a headless/no-display context (the @reboot cron on a
# server with no interactive session) and the StudOn session is expired, there is
# no way for a human to complete the Firefox login — the tray icon can't be shown.
# Polling forever in that case just thrashed the Workstation log on 2026-06-07
# (an expired session at boot spun every few minutes until killed by hand). Bound
# the total no-display wait and exit instead; the @reboot cron retries on next
# boot and the fleet-guard (_fleet_synced_today) covers today. A real desktop
# session (display present) keeps the unbounded tray wait — the user can still log
# in there, so we must not give up on them.
DAILY_SYNC_HEADLESS_MAX_WAIT_SECONDS = float(
    os.getenv('STUDON_DAILY_SYNC_HEADLESS_MAX_WAIT_SECONDS', str(30 * 60))
)


def run_daily_sync(check_interval_seconds: int = 300) -> None:
    """
    Run until a daily sync is performed, then exit.
    Waits for StudOn login (via Firefox cookies) and performs sync once per day.

    In a headless/no-display context the login can never be completed (no tray,
    no human), so the wait is bounded by DAILY_SYNC_HEADLESS_MAX_WAIT_SECONDS and
    the function returns rather than polling forever; @reboot + fleet-guard cover
    the rest. With a graphical session the wait stays unbounded.

    Args:
        check_interval_seconds: How often to check for StudOn access (default: 5 minutes)
    """
    # Check platform compatibility and log warnings
    check_platform_compatibility()

    state = load_state()

    # Check if already updated today
    if was_updated_today(state):
        logger.debug(f"Daily sync: already updated today at {state.last_update}, skipping.")
        return

    logger.debug(f"Daily sync started, checking every {check_interval_seconds // 60}m")

    waiting_logged = False
    headless_wait_started: Optional[float] = None
    while True:
        try:
            if not can_access_studon():
                login_url = f"https://{STUDON_DOMAIN}"
                try:
                    login_url = _get_first_course_url()
                except Exception:
                    pass
                if not waiting_logged:
                    logger.info(
                        "Daily sync: waiting for StudOn login (%s)",
                        "tray icon active" if _has_display()
                        else "no display — desktop notification only",
                    )
                    waiting_logged = True
                    # One popup per blocked sync attempt: the daily sync is one
                    # attempt per day, so the poll loop below stays silent.
                    _notify_login_required(
                        login_url, attempt_key=f"daily:{date.today().isoformat()}")
                _write_tray_status(state="waiting_login", login_url=login_url)
                tray_ok = _wait_for_login_via_tray(login_url)
                if not tray_ok:
                    # Tray unavailable, user quit, or icon errored — poll silently.
                    if not _has_display():
                        # No graphical session: a human can never complete the
                        # Firefox login here, so bound the total wait instead of
                        # spinning forever (the 2026-06-07 Workstation incident).
                        now = time.time()
                        if headless_wait_started is None:
                            headless_wait_started = now
                        elif now - headless_wait_started >= DAILY_SYNC_HEADLESS_MAX_WAIT_SECONDS:
                            logger.warning(
                                "Daily sync: session expired and no display to prompt "
                                f"login — gave up after {int(now - headless_wait_started)}s. "
                                "Will retry on next @reboot; fleet-guard covers today."
                            )
                            return
                    time.sleep(check_interval_seconds)
                # waiting_logged stays set: the wait log line and the login
                # popup belong to the attempt, not to each poll cycle.
                continue
            # Session is accessible again — reset the headless give-up timer so a
            # later mid-run expiry starts its own bounded wait, not a stale one,
            # and dismiss any still-open "login required" notification.
            headless_wait_started = None
            _dismiss_login_prompt()
            _write_tray_status(state="idle")

            # First-boot-wins guard: another fleet host (Workstation/Ideapad)
            # may have completed today's sync while this daemon was waiting
            # for the Firefox login. Re-check the Syncthing-synced state right
            # before scraping — includes a grace wait so a freshly-booted host
            # gives Syncthing time to deliver the sibling's state file.
            if _fleet_synced_today():
                logger.info("Daily sync: already completed today by another fleet host — skipping.")
                return

            if not _acquire_sync_lock('daily-sync', wait_seconds=600):
                logger.info("Daily sync: lock busy, deferring 5 min.")
                time.sleep(300)
                continue
            try:
                # Cheap final re-check inside the lock (no grace wait): closes
                # the race where the sibling's state arrived during lock wait.
                if was_updated_today(load_state()):
                    logger.info("Daily sync: already completed today by another fleet host — skipping.")
                    return
                success, n_downloaded, n_extracted, session_expired, downloaded_files = update_all_courses()
                if success:
                    try:
                        fb_processed, fb_files, fb_paths = check_and_process_feedback()
                        if fb_files:
                            logger.info(f"Feedback sync: downloaded {fb_files} file(s) across {fb_processed} exercise(s).")
                            n_downloaded += fb_files
                            downloaded_files = downloaded_files + fb_paths
                    except Exception as e:
                        logger.warning(f"Feedback check failed (non-fatal): {e}")
                    logger.info("Daily sync complete.")
                    # Clear any latched login flag before this run exits — the
                    # tray must not keep asking for a login we no longer need.
                    _write_tray_status(state="idle")
                    _record_tray_sync("Daily sync")
                    _send_desktop_notification(n_downloaded, n_extracted, downloaded_files)
                    return
            finally:
                _release_sync_lock()

            if session_expired:
                logger.warning("Daily sync: session expired during update, re-entering login wait loop in 2 minutes...")
                waiting_logged = False
                time.sleep(120)
            else:
                logger.warning("Daily sync: update_all_courses failed, will retry...")
                time.sleep(check_interval_seconds)

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Exiting.")
            return
        except Exception as e:
            logger.error(f"Error during daily sync: {e}")
            time.sleep(check_interval_seconds)

_WEEKDAY_PREFIX_TO_INDEX: Dict[str, int] = {
    'mo': 0, 'di': 1, 'mi': 2, 'do': 3, 'fr': 4, 'sa': 5, 'so': 6,
}


def _parse_entry_day_to_weekday(day_str: str) -> Optional[int]:
    """Map a campo day label like 'Mo., 11.05.2026' or 'Mi., 13.05.2026Himmelfahrt' to 0..6."""
    if not day_str:
        return None
    s = day_str.strip().lower()
    return _WEEKDAY_PREFIX_TO_INDEX.get(s[:2])


_TIME_RE = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*(?:bis|-|–)\s*(\d{1,2}):(\d{2})')


def _parse_entry_times(time_str: str) -> Optional[Tuple[int, int, int, int]]:
    """Extract (start_h, start_m, end_h, end_m) from an entry's 'time' field."""
    if not time_str:
        return None
    m = _TIME_RE.match(time_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _next_occurrence(weekday_idx: int, hour: int, minute: int, now: datetime) -> datetime:
    """Return the next datetime matching (weekday, hour, minute) at or after now."""
    days_ahead = (weekday_idx - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate < now:
        candidate += timedelta(days=7)
    return candidate


def _compute_fire_schedule(resolved: List[ResolvedLecture], now: datetime,
                           window_minutes: Tuple[int, int, int] = (-5, 0, 5)) -> List[Tuple[datetime, ResolvedLecture]]:
    """Build (fire_time, lecture) tuples for the next week, only for 'mapped' entries.

    Three fires per lecture: start-5m, start, start+5m. Sorted ascending.
    """
    fires: List[Tuple[datetime, ResolvedLecture]] = []
    for r in resolved:
        if r.status != 'mapped':
            continue
        wd = _parse_entry_day_to_weekday(r.entry.get('day', ''))
        times = _parse_entry_times(r.entry.get('time', ''))
        if wd is None or times is None:
            continue
        start_h, start_m, _eh, _em = times
        # Build all three fires for the next occurrence of this weekday.
        base_now = now - timedelta(minutes=10)  # tolerance for boot catch-up
        next_start = _next_occurrence(wd, start_h, start_m, base_now)
        for offset in window_minutes:
            fires.append((next_start + timedelta(minutes=offset), r))
    fires.sort(key=lambda t: t[0])
    return fires


_MD_DAY_HEADING_RE = re.compile(r'^##\s+(Mo|Di|Mi|Do|Fr|Sa|So)\.,.*$', re.IGNORECASE)
_MD_ROW_RE = re.compile(r'^\|\s*(\d{1,2}:\d{2}\s+bis\s+\d{1,2}:\d{2}(?:\s*\(s\.t\.\))?)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$')


def _parse_timetable_markdown(path: str) -> Optional[Tuple[str, List[Dict]]]:
    """Parse an existing timetable.md back into entries.

    Used as a fallback when both the JSON cache and a live campo fetch are
    unavailable (e.g. stale Firefox cookies but a recent timetable on disk).
    Only the fields the daemon needs are populated (day, title, time, type,
    room, instructors).
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            md = f.read()
    except OSError as e:
        logger.warning(f"Could not read timetable markdown: {e}")
        return None

    page_title = 'Stundenplan'
    first_line = md.splitlines()[0] if md.splitlines() else ''
    if first_line.startswith('# '):
        page_title = first_line[2:].strip()

    entries: List[Dict] = []
    current_day = ''
    in_details = False
    for line in md.splitlines():
        if line.strip() == '## Details':
            in_details = True
            continue
        if in_details:
            continue
        h = _MD_DAY_HEADING_RE.match(line)
        if h:
            # Reconstruct the campo-style day label from the heading
            current_day = line[2:].strip().rstrip()
            continue
        m = _MD_ROW_RE.match(line)
        if not m or 'Zeit' in m.group(1) or '---' in m.group(1):
            continue
        time_str, title, etype, room_col, instructors = m.groups()
        title = _TRAILING_MARKER_RE.sub('', title).strip()
        if not title or not current_day:
            continue
        entries.append({
            'day': current_day, 'col': 0, 'title': title, 'time': time_str.strip(),
            'type': etype.strip(), 'rhythm': '', 'start': '', 'end': '',
            'room': room_col.strip(), 'building': '', 'instructors': instructors.strip(),
            'status': '', 'note': '',
        })
    if not entries:
        return None
    return page_title, entries


def _ensure_timetable_entries(max_age_hours: float = 24.0) -> Optional[Tuple[str, List[Dict]]]:
    """Return cached (page_title, entries) if fresh; else fetch live; else parse timetable.md."""
    cache = _read_timetable_cache()
    if cache is not None:
        fetched_at, page_title, entries = cache
        if (datetime.now() - fetched_at).total_seconds() < max_age_hours * 3600:
            return page_title, entries
    result = _fetch_timetable_entries()
    if result is not None:
        _write_timetable_cache(result[0], result[1])
        return result
    # Fallback: stale or missing cookies, but a timetable.md may still be on disk.
    md_path = os.path.join(DOWNLOAD_FOLDER, 'timetable.md')
    parsed = _parse_timetable_markdown(md_path)
    if parsed is not None:
        logger.info("Lecture sync: using on-disk timetable.md (campo fetch failed).")
    return parsed


def _warn_unmapped_once(resolved: List[ResolvedLecture], warned: set) -> None:
    """Emit one notify-send + log warning per unmapped title per daemon-lifetime."""
    fresh = [r for r in resolved if r.status == 'unmapped' and r.entry.get('title') not in warned]
    if not fresh:
        return
    titles = sorted({r.entry.get('title', '') for r in fresh})
    for t in titles:
        warned.add(t)
        logger.warning(f"Lecture sync: timetable entry '{t}' is unmapped — run --map-lectures.")
    if shutil.which("notify-send"):
        body = "Unmapped: " + ", ".join(titles[:3]) + (" ..." if len(titles) > 3 else "")
        env = os.environ.copy()
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
        try:
            subprocess.run(
                ["notify-send", "--app-name=StudOn Scraper", "--icon=dialog-warning",
                 "StudOn lecture-sync: unmapped lectures", body],
                env=env, timeout=5,
            )
        except Exception:
            pass


def _lecture_fetch_one(course: TrackedCourse) -> Tuple[int, int, List[str]]:
    """Run a single-course fetch via the existing pipeline.

    Returns (downloaded, extracted, downloaded_file_paths).
    """
    try:
        cj = browser_cookie3.firefox(domain_name=STUDON_DOMAIN)
    except Exception as e:
        raise FirefoxCookieError(e)
    session = requests.Session()
    session.cookies.update(cj)
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    downloaded, extracted, downloaded_paths = process_single_url(
        course.source_url, session, course.course_folder,
        create_course_subfolder=False, debug=False,
    )
    return downloaded, extracted, downloaded_paths


def run_lecture_sync(once: bool = False, tray_wait_seconds: int = 120) -> None:
    """Long-running daemon for per-lecture single-course sync.

    Reads the campo timetable (or sidecar cache), resolves lecture →
    course buckets, and schedules fetches at start-5m / start / start+5m
    for each mapped lecture. Boot catch-up: on startup, immediately runs
    any fire whose window covers `now`. Single-shot tray for login on each
    fire — no repeated nagging.

    Args:
        once: When True, prints the resolved buckets and the next 5 fire
              times, then exits without fetching. Used for --lecture-sync --once.
        tray_wait_seconds: Hard cap on the tray-icon wait per fire.
    """
    check_platform_compatibility()
    logger.info(f"Lecture sync starting (once={once}, tray_wait={tray_wait_seconds}s)")

    warned_unmapped: set = set()
    last_timetable_load = 0.0
    resolved: List[ResolvedLecture] = []
    fires: List[Tuple[datetime, ResolvedLecture]] = []

    def reload_schedule() -> None:
        nonlocal resolved, fires, last_timetable_load
        result = _ensure_timetable_entries()
        if result is None:
            logger.warning("Lecture sync: could not load timetable, will retry later.")
            resolved, fires = [], []
            return
        _page_title, entries = result
        tracked = _discover_tracked_courses(DOWNLOAD_FOLDER)
        mapping = _load_lecture_mapping_json()
        resolved = _resolve_timetable_buckets(entries, tracked, mapping, auto_pin_normalized=True)
        _warn_unmapped_once(resolved, warned_unmapped)
        now = datetime.now()
        fires = _compute_fire_schedule(resolved, now)
        last_timetable_load = time.time()
        mapped_count = sum(1 for r in resolved if r.status == 'mapped')
        unmapped_count = sum(1 for r in resolved if r.status == 'unmapped')
        logger.info(f"Lecture sync: resolved {len(resolved)} entries — {mapped_count} mapped, "
                    f"{unmapped_count} unmapped; {len(fires)} fires scheduled.")

    reload_schedule()

    if once:
        print(f"\nResolved {len(resolved)} timetable entries:")
        for r in resolved:
            tag = r.status if r.status != 'mapped' else f"mapped({r.match_kind})"
            course = r.course.course_title if r.course else ''
            print(f"  [{tag:20s}] {r.entry.get('title', '')[:60]:60s}  -> {course}")
        now = datetime.now()
        upcoming = [f for f in fires if f[0] >= now][:5]
        print(f"\nNext {len(upcoming)} fires:")
        for fire_time, r in upcoming:
            course_name = r.course.course_title if r.course else ''
            print(f"  {fire_time.strftime('%a %Y-%m-%d %H:%M')}  → {course_name}")
        return

    # Persistent system-tray icon (login / sync-now / open-downloads) — best
    # effort; silently skipped without a display or AppIndicator.
    try:
        _tray_login_url = _get_first_course_url()
    except Exception:
        _tray_login_url = f"https://{STUDON_DOMAIN}"
    _launch_tray(login_url=_tray_login_url)

    while True:
        try:
            now = datetime.now()
            _launch_tray()  # idempotent: restart the tray helper if it died
            # Reload schedule daily, or if file changed.
            if time.time() - last_timetable_load > 6 * 3600:
                reload_schedule()
                now = datetime.now()

            # Find the next fire (boot catch-up: a fire in the last 10 minutes still counts).
            due: Optional[Tuple[datetime, ResolvedLecture]] = None
            for fire_time, r in fires:
                if fire_time >= now - timedelta(minutes=10):
                    due = (fire_time, r)
                    break

            if due is None:
                # No fires left this week — refresh in 1h.
                logger.info("Lecture sync: no upcoming fires; sleeping 1h before reload.")
                time.sleep(3600)
                reload_schedule()
                continue

            fire_time, lecture = due
            wait = (fire_time - now).total_seconds()
            if wait > 0:
                course_name = lecture.course.course_title if lecture.course else '?'
                logger.info(f"Lecture sync: next fire {fire_time.isoformat()} for '{course_name}' "
                            f"(sleeping {int(wait)}s)")
                _write_tray_status(
                    state="idle",
                    next_fire_human=f"{fire_time.strftime('%a %H:%M')} · {course_name}")
                time.sleep(min(wait, 1800))  # cap sleep at 30 min so we recheck schedule
                if time.time() - last_timetable_load > 6 * 3600:
                    reload_schedule()
                continue

            # Fire time. Drop it from the queue so we don't refire.
            # `due` was rebuilt as a fresh tuple above, so identity (`is not`)
            # never matches the original list element; tuples are value-equal.
            fires = [f for f in fires if f != due]
            course = lecture.course
            if course is None:
                continue

            # The lecture's START time (not the fire time) keys the per-window
            # marker, so all three fires (−5m / start / +5m) on either host
            # share one key. Derive it from the entry's parsed start time on the
            # fire's date; fall back to the fire time itself if unparseable.
            lecture_start = fire_time
            _times = _parse_entry_times(lecture.entry.get('time', ''))
            if _times is not None:
                lecture_start = fire_time.replace(
                    hour=_times[0], minute=_times[1], second=0, microsecond=0)

            # First-fire-wins: gate on Syncthing readiness so a sibling host's
            # marker is actually delivered, then check it. If this course+window
            # was already serviced today (by the other host or an earlier fire),
            # skip — this is the per-lecture analogue of _fleet_synced_today().
            _wait_for_syncthing_ready()
            if _lecture_already_synced(course, lecture_start):
                logger.info(
                    f"Lecture sync: '{course.course_title}' window "
                    f"{lecture_start.strftime('%Y-%m-%d %H:%M')} already serviced by "
                    "another fleet host (or an earlier fire); skipping."
                )
                continue

            if not can_access_studon():
                login_url = course.source_url or f"https://{STUDON_DOMAIN}"
                _write_tray_status(state="waiting_login", login_url=login_url)
                logger.info(
                    f"Lecture sync: not logged in at fire {fire_time.isoformat()}; "
                    + (f"opening tray (max {tray_wait_seconds}s)." if _has_display()
                       else "firing desktop notification (no display for tray).")
                )
                # Visible, one-click login prompt (works headless via DBUS).
                # Keyed by course + lecture-window start, so all three fires of
                # one window (−5m / start / +5m) share a single popup.
                _notify_login_required(
                    login_url,
                    attempt_key=(f"lecture:{os.path.basename(course.course_folder)}:"
                                 f"{lecture_start.isoformat()}"))
                tray_ok = _wait_for_login_via_tray(login_url, max_wait_seconds=tray_wait_seconds)
                if not tray_ok:
                    # No tray shown (AppIndicator host, headless, or pystray
                    # missing) — keep the same fire window open by polling.
                    tray_ok = _poll_until_login(can_access_studon, tray_wait_seconds)
                if not tray_ok or not can_access_studon():
                    logger.info("Lecture sync: still not logged in after tray/notification window; skipping this fire.")
                    continue
                _dismiss_login_prompt()

            if not _acquire_sync_lock(f'lecture-sync:{os.path.basename(course.course_folder)}', wait_seconds=0):
                logger.info("Lecture sync: another sync is running; skipping this fire.")
                continue
            try:
                # Re-check the marker inside the lock (no extra wait): closes the
                # race where the sibling's marker arrived during the steps above.
                if _lecture_already_synced(course, lecture_start):
                    logger.info(
                        f"Lecture sync: '{course.course_title}' window serviced by a "
                        "sibling while acquiring the lock; skipping."
                    )
                    continue
                logger.info(f"Lecture sync: fetching '{course.course_title}'")
                _write_tray_status(state="syncing")
                try:
                    downloaded, extracted, downloaded_paths = _lecture_fetch_one(course)
                    # Claim the window so the other host (and our own later
                    # fires) skip — write the marker even when nothing new was
                    # downloaded, because the scrape itself is what conflicts.
                    _mark_lecture_synced(course, lecture_start)
                    _record_tray_sync(course.course_title)
                    if downloaded:
                        _send_desktop_notification(downloaded, extracted, downloaded_paths)
                        logger.info(f"Lecture sync: {downloaded} new, {extracted} extracted.")
                    else:
                        logger.info("Lecture sync: nothing new.")
                except FirefoxCookieError as e:
                    logger.warning(f"Lecture sync: cookies unavailable — {e}")
                except StudOnError as e:
                    logger.warning(f"Lecture sync: {e}")
                except Exception as e:
                    logger.error(f"Lecture sync: unexpected error fetching '{course.course_title}': {e}")
            finally:
                _release_sync_lock()

        except KeyboardInterrupt:
            logger.info("Lecture sync: interrupted, exiting.")
            _release_sync_lock()
            return
        except Exception as e:
            logger.error(f"Lecture sync loop error: {e}")
            time.sleep(60)


def run_map_lectures_interactive() -> None:
    """Interactive helper for inspecting and editing the lecture mapping.

    Loads the timetable cache (or fetches if missing/stale), runs the bucket
    resolver, prints a summary, and for each Unmapped entry asks the user
    to either link it to a tracked course or mark it as no-course.

    Refuses to run when stdin is not a TTY — questionary auto-selects the
    first option on a non-interactive stream, which would silently produce
    wrong mappings.
    """
    if not sys.stdin.isatty():
        print("❌ --map-lectures requires an interactive terminal. Run it from a shell.")
        return
    print("📅 Loading timetable...")
    result = _ensure_timetable_entries(max_age_hours=24.0)
    if result is None:
        print("❌ Could not load timetable. Make sure you're logged into campo in Firefox.")
        return
    _page_title, entries = result
    tracked = _discover_tracked_courses(DOWNLOAD_FOLDER)
    if not tracked:
        print("❌ No tracked courses found in the download folder. Register a course first.")
        return
    mapping = _load_lecture_mapping_json()
    # Resolve once with auto-pin so cosmetic mismatches stick.
    resolved = _resolve_timetable_buckets(entries, tracked, mapping, auto_pin_normalized=True)

    buckets = {'mapped': 0, 'no_course': 0, 'ignored': 0, 'unmapped': 0}
    for r in resolved:
        buckets[r.status] = buckets.get(r.status, 0) + 1

    print()
    print("Mapping summary:")
    print(f"  ✅ Mapped:    {buckets['mapped']}")
    print(f"  🚫 No course: {buckets['no_course']}")
    print(f"  ⏭️  Ignored:  {buckets['ignored']}")
    print(f"  ❓ Unmapped:  {buckets['unmapped']}")
    print()

    if buckets['mapped']:
        print("Mapped lectures:")
        for r in resolved:
            if r.status != 'mapped' or r.course is None:
                continue
            kind = r.match_kind
            tag = "📌" if kind == 'explicit' else "🔗"
            print(f"  {tag} {r.entry.get('title', ''):60s} → {r.course.course_title}")
        print()

    unmapped = [r for r in resolved if r.status == 'unmapped']
    if not unmapped:
        print("✅ All timetable entries are accounted for.")
        return

    print(f"{len(unmapped)} unmapped lecture(s) need attention.")
    print()

    # Build picker options for each unmapped entry.
    course_choices = [(c.course_title, c) for c in tracked]
    course_choices.sort(key=lambda x: x[0])

    for r in unmapped:
        title = r.entry.get('title', '')
        day = r.entry.get('day', '')
        ttime = r.entry.get('time', '')
        instructors = r.entry.get('instructors', '')
        print(f"\n— Unmapped — '{title}'")
        if day or ttime:
            print(f"    {day}  {ttime}  {instructors}")
        if questionary:
            choices = [questionary.Choice(f"Link to: {name}", value=('link', course)) for name, course in course_choices]
            choices.append(questionary.Choice("Mark as 'no StudOn course' (skip silently)", value=('no_course', None)))
            choices.append(questionary.Choice("Skip for now (remains Unmapped, will warn)", value=('skip', None)))
            action = questionary.select(f"What should '{title}' map to?", choices=choices).ask()
        else:
            print("  1. Link to a tracked course")
            print("  2. Mark as 'no StudOn course'")
            print("  3. Skip for now")
            try:
                resp = (input("  Choice [1/2/3]: ") or '3').strip()
            except EOFError:
                resp = '3'
            if resp == '1':
                for i, (name, _c) in enumerate(course_choices, 1):
                    print(f"    {i}. {name}")
                try:
                    idx = int(input("    Course #: ").strip()) - 1
                    action = ('link', course_choices[idx][1]) if 0 <= idx < len(course_choices) else ('skip', None)
                except (ValueError, EOFError, IndexError):
                    action = ('skip', None)
            elif resp == '2':
                action = ('no_course', None)
            else:
                action = ('skip', None)

        if action is None:
            action = ('skip', None)

        kind, payload = action
        if kind == 'link' and payload is not None:
            _add_timetable_title_to_course(payload.metadata_path, title)
            print(f"  ✅ Linked to '{payload.course_title}'.")
        elif kind == 'no_course':
            if title not in mapping['no_course_titles']:
                mapping['no_course_titles'].append(title)
                _save_lecture_mapping_json(mapping)
            print(f"  🚫 Marked '{title}' as no-course.")
        else:
            print(f"  ⏭️  Left '{title}' unmapped.")

    print()
    print("Done. Re-run --map-lectures any time to revisit, or edit:")
    print(f"  - lecture_mapping.json: {LECTURE_MAPPING_PATH}")
    print(f"  - METADATA.md per course (timetable_titles list)")


def run_discover_from_timetable(debug: bool = False) -> None:
    """Walk Unmapped campo timetable entries, resolve each to its StudOn URL via the
    'Detailansicht' button, and register the resulting courses as tracked courses.
    """
    if not sys.stdin.isatty():
        print("❌ --discover-from-timetable requires an interactive terminal.")
        return

    print("📅 Fetching campo timetable...")
    try:
        campo_session = requests.Session()
        campo_session.cookies.update(browser_cookie3.firefox(domain_name='fau.de'))
        campo_session.cookies.update(browser_cookie3.firefox(domain_name='campo.fau.de'))
        campo_session.headers.update({'User-Agent': 'Mozilla/5.0'})
    except Exception as e:
        print(f"❌ Could not load Firefox cookies for campo: {e}")
        return

    result = _fetch_timetable_entries()
    if result is None:
        return
    _page_title, entries = result

    tracked = _discover_tracked_courses(DOWNLOAD_FOLDER)
    mapping = _load_lecture_mapping_json()
    resolved = _resolve_timetable_buckets(entries, tracked, mapping, auto_pin_normalized=True)

    unmapped = [r for r in resolved if r.status == 'unmapped']
    if not unmapped:
        print("✅ Every timetable entry is already mapped — nothing to discover.")
        return

    print(f"🔍 {len(unmapped)} unmapped entry/entries to investigate.")
    print()

    # Separate session for the StudOn downloader (different cookie domain).
    try:
        studon_session = requests.Session()
        studon_session.cookies.update(browser_cookie3.firefox(domain_name=STUDON_DOMAIN))
        studon_session.headers.update({'User-Agent': 'Mozilla/5.0'})
    except Exception as e:
        print(f"❌ Could not load Firefox cookies for {STUDON_DOMAIN}: {e}")
        return

    registered = 0
    skipped = 0
    failed = 0
    seen_titles: set = set()

    for r in unmapped:
        entry = r.entry
        title = entry.get('title', '')
        if title in seen_titles:
            continue
        seen_titles.add(title)
        button_name = entry.get('detail_button_name', '')
        if not button_name:
            print(f"⚠️  '{title}': no detail button found in timetable, skipping.")
            failed += 1
            continue

        # Re-fetch the timetable each iteration so the JSF ViewState is fresh.
        try:
            page = campo_session.get(CAMPO_TIMETABLE_URL, timeout=30)
        except Exception as e:
            print(f"❌ Could not re-fetch timetable: {e}")
            failed += 1
            continue
        if page.status_code != 200:
            print(f"❌ Timetable re-fetch HTTP {page.status_code}, aborting.")
            return

        print(f"➡️  '{title}': POSTing detail button...")
        detail_html = _post_jsf_detail_button(campo_session, page.text, page.url, button_name)
        if detail_html is None:
            print(f"   ❌ JSF POST failed for '{title}'.")
            failed += 1
            continue

        link = _extract_studon_link(detail_html)
        if not link:
            print(f"   ⚠️  No StudOn link found on the detail page for '{title}'.")
            failed += 1
            continue

        print(f"   🔗 Found StudOn link: {link}")
        final_url = _resolve_studon_course_url(studon_session, link)
        if not final_url:
            print(f"   ❌ Could not resolve final ILIAS URL for '{title}'.")
            failed += 1
            continue
        print(f"   ✅ Resolved to: {final_url}")

        if questionary:
            confirm = questionary.confirm(
                f"Register '{title}' as a new tracked course?", default=True
            ).ask()
        else:
            try:
                confirm = (input(f"   Register '{title}' as a new tracked course? [Y/n]: ") or 'y').strip().lower() != 'n'
            except EOFError:
                confirm = False

        if not confirm:
            print(f"   ⏭️  Skipped '{title}'.")
            skipped += 1
            continue

        try:
            n_dl, n_ex, _ = process_single_url(
                final_url, studon_session,
                base_download_path=DOWNLOAD_FOLDER,
                create_course_subfolder=True,
                debug=debug,
            )
        except Exception as e:
            print(f"   ❌ Download failed for '{title}': {e}")
            failed += 1
            continue

        # Pin the verbatim timetable title onto the freshly-created course.
        course_title = extract_course_title(final_url, studon_session, debug=debug)
        if course_title:
            meta_path = os.path.join(DOWNLOAD_FOLDER, course_title, "METADATA.md")
            if os.path.exists(meta_path):
                _add_timetable_title_to_course(meta_path, title)

        print(f"   📦 Registered '{title}' ({n_dl} downloaded, {n_ex} extracted).")
        registered += 1

    print()
    print(f"Done. Registered: {registered}  Skipped: {skipped}  Failed: {failed}")


def _course_folder_stats(folder_path: str) -> Tuple[int, int]:
    """Return (file_count, total_bytes) for a course folder, skipping meta files."""
    skip_names = {"METADATA.md", "RECENT_UPDATES.md"}
    count = 0
    total = 0
    for root, _dirs, files in os.walk(folder_path):
        for f in files:
            if f not in skip_names and not f.endswith('.html'):
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    count += 1
                except OSError:
                    pass
    return count, total


def show_startup_overview(download_folder: str) -> None:
    """Render a TUI overview of registered courses and the configured download directory."""
    # ── ANSI helpers ──────────────────────────────────────────────────────────
    R  = "\033[0m"
    B  = "\033[1m"
    DIM = "\033[2m"
    CY = "\033[96m"    # bright cyan
    GR = "\033[92m"    # bright green
    YE = "\033[93m"    # bright yellow
    RE = "\033[91m"    # bright red
    BL = "\033[94m"    # bright blue

    # ── Layout constants ──────────────────────────────────────────────────────
    try:
        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        term_w = 80
    W = min(max(term_w - 2, 60), 90)   # total inner+border width, clamped

    # Column widths (status | course | last-sync | files | size)
    COL_ST = 2
    COL_SY = 10
    COL_FI = 5
    COL_SZ = 7
    # course name gets the remaining space
    COL_CO = W - COL_ST - COL_SY - COL_FI - COL_SZ - 6 - 2  # 6 separators, 2 outer walls

    # ── Box-drawing helpers ───────────────────────────────────────────────────
    def hline(left, mid, sep, right, widths):
        parts = [mid * (w + 2) for w in widths]
        return left + sep.join(parts) + right

    HDR_TOP  = hline('╔', '═', '╦', '╗', [COL_ST, COL_CO, COL_SY, COL_FI, COL_SZ])
    HDR_SEP  = hline('╠', '═', '╬', '╣', [COL_ST, COL_CO, COL_SY, COL_FI, COL_SZ])
    HDR_MID  = hline('╠', '═', '╦', '╣', [COL_ST, COL_CO, COL_SY, COL_FI, COL_SZ])
    HDR_BOT  = hline('╚', '═', '╩', '╝', [COL_ST, COL_CO, COL_SY, COL_FI, COL_SZ])
    WIDE_TOP = '╔' + '═' * (W - 2) + '╗'
    WIDE_SEP = '╠' + '═' * (W - 2) + '╣'
    WIDE_BOT = '╚' + '═' * (W - 2) + '╝'

    def wide_row(text, color='', align='<'):
        inner = W - 4  # two border chars + two spaces
        truncated = text[:inner]
        padded = f'{truncated:{align}{inner}}'
        return f'║ {color}{padded}{R} ║'

    def data_row(st_col, co_col, sy_col, fi_col, sz_col, colors=None):
        colors = colors or {}
        def cell(text, width, color='', align='>'):
            t = str(text)[:width]
            return f' {color}{t:{align}{width}}{R} '
        return (
            '║'
            + cell(st_col, COL_ST, colors.get('st', ''), '^')
            + '║'
            + cell(co_col, COL_CO, colors.get('co', ''), '<')
            + '║'
            + cell(sy_col, COL_SY, colors.get('sy', ''), '^')
            + '║'
            + cell(fi_col, COL_FI, colors.get('fi', ''), '>')
            + '║'
            + cell(sz_col, COL_SZ, colors.get('sz', ''), '>')
            + '║'
        )

    # ── Collect course data ───────────────────────────────────────────────────
    abs_folder = str(Path(download_folder).resolve())

    courses = []  # list of dicts
    if os.path.isdir(download_folder):
        for entry in sorted(os.scandir(download_folder), key=lambda e: e.name.lower()):
            if not entry.is_dir():
                continue
            meta_path = os.path.join(entry.path, "METADATA.md")
            if not os.path.exists(meta_path):
                continue
            meta = CourseMetadata.from_yaml_markdown(meta_path)
            # An empty or unparsable METADATA.md parses into a nameless record
            # with no source_url and a last_fetched of "now", which reads in the
            # table as a healthy course called "Unknown Course". Name it after
            # its folder and mark it repairable instead.
            broken = not (meta and meta.source_url)
            name = entry.name
            if meta and meta.source_url:
                name = meta.course_title or entry.name
            elif broken:
                recovered_title, _ = recover_course_from_link_file(entry.path)
                name = f"{recovered_title or entry.name}  (METADATA.md unreadable)"
            folder_exists = os.path.isdir(entry.path)
            file_count, total_bytes = _course_folder_stats(entry.path) if folder_exists else (0, 0)
            courses.append({
                'name':    name,
                'folder':  entry.path,
                'exists':  folder_exists,
                'synced':  meta.last_fetched_formatted[:10] if (meta and not broken) else '—',
                'files':   file_count,
                'size':    format_file_size(total_bytes) if total_bytes else '—',
            })

    ok_count      = sum(1 for c in courses if c['exists'])
    missing_count = len(courses) - ok_count

    # ── Render ────────────────────────────────────────────────────────────────
    print()
    print(WIDE_TOP)
    title = f'{B}{CY}StudOn Scraper{R}'
    print(wide_row(f'StudOn Scraper', CY + B))
    print(WIDE_SEP)

    # Directory line
    dir_display = abs_folder
    inner = W - 4
    dir_label = 'Directory: '
    max_path = inner - len(dir_label)
    if len(dir_display) > max_path:
        dir_display = '…' + dir_display[-(max_path - 1):]
    print(wide_row(f'{dir_label}{dir_display}', DIM))

    print(WIDE_SEP)

    if not courses:
        print(wide_row('No registered courses found.', YE))
        print(wide_row(f'Add a course:  python studon_client.py <URL>', DIM))
        print(WIDE_BOT)
        print()
        return

    # Courses header
    course_header = f'Registered Courses  ({ok_count} OK' + (f'  •  {RE}{missing_count} missing{R}' if missing_count else '') + ')'
    # strip ANSI for width calculation, use raw string for display
    print(wide_row(f'Registered Courses  ({ok_count} OK' + (f'  •  {missing_count} missing' if missing_count else '') + ')', B))

    # Table header row
    print(HDR_MID)
    print(data_row('', 'Course', 'Last sync', 'Files', 'Size',
                   colors={'co': B, 'sy': B, 'fi': B, 'sz': B}))
    print(HDR_SEP)

    for c in courses:
        if c['exists']:
            st_icon  = '✓'
            st_color = GR
            co_color = ''
        else:
            st_icon  = '✗'
            st_color = RE
            co_color = DIM

        name = c['name']
        if len(name) > COL_CO:
            name = name[:COL_CO - 1] + '…'

        print(data_row(
            st_icon,
            name,
            c['synced'],
            str(c['files']) if c['exists'] else '—',
            c['size'],
            colors={'st': st_color, 'co': co_color, 'sy': DIM, 'fi': '', 'sz': ''},
        ))

    print(HDR_BOT)
    print()


# --- Install / remove: cron via CronInstaller, shell alias via ToolInstaller ---
# Contract: ../cli-tools-kit/PROTOCOL.md. Versions before this change wrote the
# cron lines with a raw `crontab -` and a studon-client() function straight
# into ~/.bashrc. Both --install and --remove strip those legacy entries, so a
# host converges on the kit-managed ones without duplicates.

_CRON = CronInstaller("studon-client") if CronInstaller else None

# Flags of the cron lines --install writes.
_CRON_FLAGS = ('--daily-sync', '--lecture-sync', '--campo-bescheinigungen')
_LEGACY_BASHRC_MARKER = '# studon-client quick-fetch'


def _cron_lines(check_interval: int = 5) -> List[str]:
    """The cron lines --install registers (without the kit's marker tag)."""
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)
    python = sys.executable
    daily_cmd = f"@reboot cd {script_dir} && {python} {script_path} --daily-sync"
    if check_interval != 5:
        daily_cmd += f" --interval {check_interval}"
    lecture_cmd = f"@reboot cd {script_dir} && {python} {script_path} --lecture-sync"
    # Weekly Prüfungsamt-PDFs (Mondays 06:30) — keeps Notenübersicht.pdf fresh so
    # --reconcile always has a canonical ECTS source.
    bescheinigungen_cmd = (
        f"30 6 * * 1 cd {script_dir} && {python} {script_path} --campo-bescheinigungen "
        f">> {os.path.join(DOWNLOAD_FOLDER, 'studon_bescheinigungen.log')} 2>&1"
    )
    return [daily_cmd, lecture_cmd, bescheinigungen_cmd]


def _is_legacy_cron_line(line: str) -> bool:
    """A hand-written studon-client cron line from before CronInstaller: active
    (not commented out), no cli-tool-kit tag, one of our flags. Matches on the
    script name rather than the full path so lines from a moved checkout go too."""
    s = line.strip()
    return (bool(s) and not s.startswith('#')
            and '# cli-tool-kit:' not in s
            and 'studon_client.py' in s
            and any(flag in s for flag in _CRON_FLAGS))


def _remove_legacy_cron_lines() -> int:
    """Strip legacy untagged cron lines. Returns how many were removed.

    Goes through the kit's own crontab read/write so a failing `crontab -l`
    raises instead of being read as an empty crontab (which would wipe it)."""
    if _CRON is None:
        return 0
    lines = _CRON._read().splitlines()
    kept = [l for l in lines if not _is_legacy_cron_line(l)]
    if len(kept) == len(lines):
        return 0
    _CRON._write('\n'.join(kept))
    return len(lines) - len(kept)


def _remove_legacy_bashrc_function() -> bool:
    """Remove the studon-client() function older --install versions wrote into
    ~/.bashrc. Returns True if ~/.bashrc changed.

    It has to go before the alias takes over: bash expands an alias in a
    function definition's name, so `studon-client() {...}` after the alias is
    sourced becomes a syntax error at shell start."""
    bashrc = Path.home() / '.bashrc'
    if not bashrc.exists():
        return False
    lines = bashrc.read_text().splitlines(keepends=True)
    out: List[str] = []
    for l in lines:
        if l.strip() == _LEGACY_BASHRC_MARKER:
            # The old installer put a blank line in front of the marker.
            if out and not out[-1].strip():
                out.pop()
            continue
        if l.lstrip().startswith('studon-client()'):
            continue
        out.append(l)
    if len(out) == len(lines):
        return False
    bashrc.write_text(''.join(out))
    return True


def _alias_installer():
    """ToolInstaller for the `studon-client` alias, built from the advertise record."""
    import dataclasses
    known = {f.name for f in dataclasses.fields(ToolMetadata)}
    entry = {k: v for k, v in _advertise_entry().items() if k in known}
    return ToolInstaller(script_path=os.path.abspath(__file__),
                         metadata=ToolMetadata(**entry))


def _install_alias() -> None:
    """Write the alias through the kit, without the kit's pip step.

    _run_install checks dependencies itself, and a parent installer provisions
    the venv before calling --install. ToolInstaller.install() would otherwise
    pip-install requirements.txt, whose kit pin can replace a newer kit already
    installed in a shared interpreter."""
    prev = os.environ.get("TOOLS_INSTALLER_SKIP_DEPS")
    os.environ["TOOLS_INSTALLER_SKIP_DEPS"] = "1"
    try:
        _alias_installer().install()
    finally:
        if prev is None:
            os.environ.pop("TOOLS_INSTALLER_SKIP_DEPS", None)
        else:
            os.environ["TOOLS_INSTALLER_SKIP_DEPS"] = prev


def _is_installed() -> bool:
    """Return True if the kit-managed cron lines for this tool are registered.

    Legacy hand-written lines do not count, so the TUI offers Install on such a
    host, which migrates them."""
    if _CRON is None:
        return False
    try:
        return _CRON.is_installed()
    except RuntimeError:
        return False


def _run_uninstall() -> None:
    """Remove the cron jobs and the shell alias installed by --install, plus the
    hand-written entries of older versions."""
    # --- Cron ---
    if _CRON is None:
        print("  cli-tools-kit not importable — cannot edit the crontab.")
    else:
        try:
            n = _remove_legacy_cron_lines()
            if n:
                print(f"  Removed {n} hand-written cron line(s) from an older --install.")
            _CRON.remove()
        except (RuntimeError, subprocess.CalledProcessError) as e:
            print(f"  Cron cleanup failed: {e}")

    # --- Shell alias (and the old bashrc function) ---
    if _remove_legacy_bashrc_function():
        print("  Removed the old studon-client() function from ~/.bashrc.")
    if _HAS_INSTALLER:
        _alias_installer().remove()
    else:
        print("  cli-tools-kit not importable — alias left in place.")


# --- Claude Code skill registration ---------------------------------------
# Single source of truth for ~/.claude/skills/studon-client/SKILL.md. Update
# this when CLI flags change so `python3 studon_client.py --install-skill` re-
# registers a fresh manifest. Kept inline so the script stays self-contained.
# Renamed twice: `studon` → `search-studon` on 2026-06-07 (legibility, grouping
# with sibling source-fetcher skills like `search-youtube`), then
# `search-studon` → `studon-client` on 2026-09-02 to satisfy the one-identity
# naming rule (repo name = bash alias = skill name).
SKILL_DIR  = Path.home() / '.claude' / 'skills' / 'studon-client'
SKILL_FILE = SKILL_DIR / 'SKILL.md'
# Both earlier names left a skill dir behind; prune them on (un)install so other
# fleet hosts converge when they next run --install / --install-skill.
LEGACY_SKILL_DIRS = ('studon', 'search-studon')
SKILL_MD_CONTENT = '''---
name: studon-client
description: Drive the StudOn / Campo scraper at ~/Synced/repos/AutomatedAlchemy/studon-client/. Use when the user asks to download FAU StudOn course material, register a new course, refresh tracked courses, inspect the campo timetable, dump prüfungs-Anmeldefristen, export the studyPlanner Modulplan (status/ECTS/Versuch per module), list current Belegungen (angemeldete Prüfungen + Veranstaltungen mit Termin/Raum/Prüfer), reconcile Modulplan ↔ Belegungen for an honest ECTS-Bilanz, or bulk-download campo Notenübersicht / Bescheinigungen PDFs (Notenübersicht, BAföG §48, ord. Studium, angemeldete Prüfungen). Triggers: "studon course holen", "alle kurse aktualisieren", "campo timetable export", "bescheinigung ziehen", "notenübersicht pdf", "studienfortschritt", "modulplan", "wieviele ects hab ich", "belegungen", "wo bin ich angemeldet", "klausurtermin", "reconcile", "ects bilanz", "stimmt meine ects", "studon scrape", "FAU course download". NOT for the QuizHub daily-quiz (that's the `quizhub-client` cron).
---

# studon-client

Wrapper for the StudOn / Campo scraper at
`~/Synced/repos/AutomatedAlchemy/studon-client/studon_client.py`.
Authenticates to FAU StudOn + campo via Firefox cookies (`browser-cookie3`),
crawls course pages, and downloads materials into the configured downloads
folder (`~/Synced/OneDrive/Studium/KIM4/` on this fleet).

The scraper is already installed and self-running:
- `@reboot studon_client.py --daily-sync` — once-per-day full sync of all tracked courses
- `@reboot studon_client.py --lecture-sync` — per-lecture fetcher driven by the campo timetable
- `30 6 * * 1 studon_client.py --campo-bescheinigungen` — weekly (Mo 06:30) Prüfungsamt-PDF refresh so `--reconcile` always has the canonical ECTS source

This skill is for **ad-hoc invocations** from a Claude session — anything the
cron daemons don't already do automatically.

## When to use

- "Fetch new uploads for THIS course" (cwd is inside a tracked course folder) → run the scraper **bare, no args** — it detects the course from the folder's `METADATA.md` and refreshes only that one. Non-interactive shells auto-fetch; a TTY gets it pre-selected as the first TUI option. Pass `--dry-run` to preview only. Prefer this over `<URL>` when you're already sitting in the course folder.
- "Download this StudOn course" → has a URL → `<URL>` mode below.
- "Update all my courses now" → `--update-all`.
- "Was kommt diese Woche an Vorlesungen?" → `--timetable` → reads `timetable.md`.
- "Wie steht mein Studienfortschritt?" / "welche Module hab ich bestanden?" → `--modulplan` (deterministic, no Pre-Click) → `Modulplan.md` with Nr/Titel/Status/Semester/Versuch/ECTS for every module in the Studienplan + ECTS-Bilanz footer.
- "Welche Prüfungsanmeldungen laufen?" → `--campo-pruefungen` (requires Prüfungs-Detailansichten pre-opened in Firefox — Zeiträume live only on per-Prüfung Detail views, not the deterministic Modul-Detail views).
- "Wo bin ich diesen Semester angemeldet?" / "wann ist Klausur X?" / "welche Räume hat Vorlesung Y?" → `--belegungen` (deterministic, no Pre-Click) → `Belegungen.md` + `Belegungen.json` with angemeldete Prüfungen (Nr/Form/Prüfer/Termin/Status) + Veranstaltungen (Typ/Titel/Termin+Raum/Dozent). Beleg im Streitfall. Pure data — change-detection/notification lives in the sibling **belegungen-watcher** tool (`~/Synced/repos/AutomatedAlchemy/belegungen-watcher/main.py`), which consumes `Belegungen.json` and fires `notify-send` on Termin-Konkretisierung / Status-Flip.
- "Stimmt meine ECTS-Bilanz?" / "warum sagen `lernplan.md` und Modulplan unterschiedliche ECTS?" / "welche Modul-Anmeldungen haben keine Belegung?" / "BAföG-relevante ECTS-Zahl" → `--reconcile` → `Reconciliation.md`. Wenn `Bescheinigungen/Notenübersicht*Module*.pdf` existiert (lade einmalig via `--campo-bescheinigungen`), wird **die PDF zur kanonischen ECTS-Quelle** (Prüfungsamt-signiert, BAföG-relevant) — der Front-Page-Undercount wird automatisch sichtbar gemacht. Sections: (1) Belegungen-Prüfung → Modulplan-Modul-Match, (2) Modulplan-Angemeldet ohne Belegung, (3) Bestanden-Module ohne `X/Y`-Suffix, (4) PDF-Inhalt mit Gap-zu-Modulplan pro Modul, (5) Lücken die der Modulplan unterläuft.
- "Suche Kurs X in campo" / "wie viele ECTS hat …" → `--campo-search "<query>" [--term 'eq|1|2026']` (prints LV-Treffer + ECTS to stdout; default Semester = aktuelles).
- "Lade meine Notenübersicht / BAföG-Bescheinigung / Transcript" → `--campo-bescheinigungen` (downloads all 12 PDFs from `personExamsReadonly.xhtml` into `<downloads>/Bescheinigungen/`).
- "Register a new course from a campo timetable entry I don't have yet" → `--discover-from-timetable`.

**Do NOT use this skill for:**
- The QuizHub daily-quiz pipeline — that's `~/Synced/repos/AutomatedAlchemy/quizhub-client/` and runs from cron.
- The `lecture-prep` skill — that builds a *pre-lecture concept quiz*, not a course download.
- Anything outside FAU StudOn / campo.

## How to invoke

Bash tool alias `studon-client` is installed but **not callable from
Claude's non-interactive Bash** (alias expansion is disabled). Use the direct
script path:

```bash
PY=/home/prob/Synced/repos/prob_ubuntu_environment/Py3EnvShare/bin/python3
SCRAPER=/home/prob/Synced/repos/AutomatedAlchemy/studon-client/studon_client.py

$PY $SCRAPER --help
```

(Plain `python3` also works on this host — the fleet venv at `Py3EnvShare`
already has `browser-cookie3`, `beautifulsoup4`, `requests`, `questionary`.)

## Cheat-sheet (subset of `--help`)

| Intent | Command |
|---|---|
| (Re-)login Firefox & wait until authenticated | `$PY $SCRAPER --login campo` (or `studon` / `both`; bare `--login` == studon) |
| Refresh just the course whose folder you're in (bare, no args) | `cd "<course folder>" && $PY $SCRAPER` (add `--dry-run` to preview) |
| Download one course by URL | `$PY $SCRAPER <studon_course_url>` |
| Preview without downloading | `$PY $SCRAPER <url> --dry-run` |
| Refresh every tracked course | `$PY $SCRAPER --update-all` |
| Export campo timetable → `timetable.md` | `$PY $SCRAPER --timetable` |
| Map unmapped timetable entries → courses | `$PY $SCRAPER --map-lectures` |
| Auto-register new courses from timetable | `$PY $SCRAPER --discover-from-timetable` |
| Scan Studienplan-Modulplan (deterministisch) → `Modulplan.md` | `$PY $SCRAPER --modulplan` |
| Scan Belegungen (Prüfungen + Veranstaltungen, deterministisch) → `Belegungen.md` + Termin-Watcher | `$PY $SCRAPER --belegungen` |
| Cross-Check Modulplan ↔ Belegungen → `Reconciliation.md` | `$PY $SCRAPER --reconcile` |
| Dump Prüfungs-Anmeldefristen → `pruefungen.md` | `$PY $SCRAPER --campo-pruefungen` |
| Search campo courses (+ECTS) | `$PY $SCRAPER --campo-search "<query>" [--term 'eq|1|2026']` |
| Download all Notenübersicht/Bescheinigungen PDFs | `$PY $SCRAPER --campo-bescheinigungen` |
| Show next 5 lecture-sync fires (debug) | `$PY $SCRAPER --lecture-sync-once` |
| Set default download path | `$PY $SCRAPER --set-download-path ~/path` |
| Bring the tray icon back after "Tray schliessen" | `$PY $SCRAPER --tray` |
| (Re)install this Claude skill | `$PY $SCRAPER --install-skill` |

Full architecture & dataclasses: `~/Synced/repos/AutomatedAlchemy/studon-client/CLAUDE.md`.

## Preconditions

- **Firefox must be logged into both StudOn and campo.** The scraper reads
  Firefox cookies via `browser-cookie3`. If the cookie is stale, the scraper
  prints `❌ Could not load Firefox cookies`, `make sure you're logged in`, or
  e.g. `❌ Campo personExamsReadonly not reachable. Log into campo.fau.de`.
  Fastest fix: run `$PY $SCRAPER --login campo` (or `--login studon` /
  `--login both`) — it auto-opens Firefox at the right login page and blocks
  until the session is authenticated, then re-run the data command. Bare
  `--login` defaults to `studon`. Alternatively just refresh the relevant tab
  in Firefox manually and re-run.
- For `--campo-pruefungen`: the user must have **manually opened each
  Prüfungs-Detailansicht in Firefox** beforehand (the `_flowExecutionKey`
  is server-side per-session state — the scraper can only iterate keys that
  already exist in the current campo flow stack). The scan walks flows
  `e1..e99` (adaptive, stops after 12 consecutive empty flows). Zeiträume
  (Anmelde-/Abmelde-/Prüfungszeitraum) only render on per-Prüfung Detail
  pages — the deterministic `--modulplan` route reaches *Modul-Detail* pages
  which do **not** carry Zeiträume.
- For `--modulplan`: no Pre-Click required. The studyPlanner-flow front page
  is fetched fresh; campo mints a `_flowExecutionKey` automatically. ECTS,
  Status, Semester, Versuch are all extracted from the front-page HTML
  (`X/Y`-suffix at the end of each `modulePlanItem` div). Output has a
  Studienfortschritt-Header (Bestanden vs. `modulplan_ects_soll` aus
  `config.json`, Default 180) plus 3 status-grouped tables (Bestanden /
  Angemeldet / Offen).
- For `--belegungen`: no Pre-Click required. The searchOwnEnrollmentInfo-flow
  front page lists all `Belegung<N>:tableGroup` blocks for the *currently
  selected* semester (campo defaults to the running one). Termin-spalte
  carries `<br>`-separated multi-Termine; Prüfer-/Dozent-Spalte ist deduped.
  Emits `Belegungen.md` (human) + `Belegungen.json` (machine). Change-
  detection + `notify-send` is owned by the sibling `belegungen-watcher`
  tool — invoke it separately after `--belegungen`.
- For `--reconcile`: no Pre-Click required, runs both fetches and writes
  `Reconciliation.md`. Title-Match ist exakt nach normalisiertem Titel
  (lowercase, „Praktikum:" Prefix gestrippt, Punctuation entfernt) mit
  Jaccard-Fallback ab 0.6. Beide Detail-Quellen (Belegungen-Prüfungs-Nr ≠
  Modulplan-Modul-Nr) sind unabhängig — Match nur über Titel möglich.
  Wenn eine `Notenübersicht*Module*.pdf` unter `Bescheinigungen/` liegt,
  parst sie `pdftotext -layout` automatisch und behandelt die PDF-Zahl
  als kanonisch (Prüfungsamt-Signatur).
- For `--campo-bescheinigungen`: only requires being logged into campo — the
  page enumerates its own 12 PDF buttons and the scraper re-GETs the form per
  button to refresh the `_flowExecutionKey`.

## Output layout

```
~/Synced/OneDrive/Studium/KIM4/         # configured downloads_path
├── timetable.md                        # --timetable
├── .timetable_entries.json             # cache consumed by --lecture-sync
├── Modulplan.md                        # --modulplan (Studienplan + Status + ECTS)
├── Belegungen.md                       # --belegungen human-readable
├── Belegungen.json                     # --belegungen machine-readable (consumed by belegungen-watcher)
├── .belegungen_snapshot.json           # belegungen-watcher state (separate tool, not studon-client)
├── Belegungen_changes.log              # belegungen-watcher audit-trail
├── Reconciliation.md                   # --reconcile (Modulplan ↔ Belegungen cross-check + ECTS-Bilanz)
├── Reconciliation.json                 # --reconcile (maschinen-lesbarer Export für Digest/Lernplan/…)
├── studon_bescheinigungen.log          # weekly --campo-bescheinigungen cron output
├── pruefungen.md                       # --campo-pruefungen (Anmelde-Zeiträume)
├── RECENT_UPDATES.md                   # last sync's download log
├── Bescheinigungen/                    # --campo-bescheinigungen
│   ├── Notenübersicht.pdf
│   ├── Leistungsbescheinigung nach §48 BAföG.pdf
│   └── ... (12 total)
└── <Course Name>/
    ├── METADATA.md                     # course state + timetable_titles
    └── <lecture folders>/
```

## Notes

- Cron-installed daemons share a PID-lock at `<downloads>/.studon_sync.lock`
  — ad-hoc invocations queue politely behind a running daemon (`--update-all`
  waits up to 10 min; everything else exits if the lock is held).
- Logs land in CWD (`studon_sync.log`), not the script dir. Syncthing has been
  known to mint `studon_sync.sync-conflict-*.log` copies across hosts — those
  are noise, not errors.
- Per-file confirmation kicks in above 50 files (`CONFIRMATION_THRESHOLD` env
  var). For a non-interactive Claude session, prefer `--dry-run` first to
  preview, then re-run without it.
'''


def _is_skill_installed() -> bool:
    return SKILL_FILE.exists()


def _prune_legacy_skill_dirs() -> None:
    """Remove the skill dirs left by the two earlier skill names.

    2026-06-07 renamed `studon` → `search-studon`, 2026-09-02 renamed
    `search-studon` → `studon-client` so repo, bash alias and skill share one
    name. Only a dir that holds our SKILL.md is touched; anything else with the
    same name is left alone.
    """
    for legacy in LEGACY_SKILL_DIRS:
        legacy_dir = Path.home() / '.claude' / 'skills' / legacy
        legacy_file = legacy_dir / 'SKILL.md'
        if not legacy_file.is_file():
            continue  # not ours — never remove it
        legacy_file.unlink()
        print(f"  ✅ Removed legacy skill {legacy_file}")
        try:
            legacy_dir.rmdir()  # only if now empty
        except OSError:
            pass


def _run_install_skill() -> None:
    """Write (or refresh) ~/.claude/skills/studon-client/SKILL.md from the inline source."""
    _prune_legacy_skill_dirs()
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    pre_existed = SKILL_FILE.exists()
    if pre_existed and SKILL_FILE.read_text(encoding='utf-8') == SKILL_MD_CONTENT:
        print(f"  • Skill already up-to-date: {SKILL_FILE}")
        return
    SKILL_FILE.write_text(SKILL_MD_CONTENT, encoding='utf-8')
    verb = "Refreshed" if pre_existed else "Installed"
    print(f"  ✅ {verb} Claude skill at {SKILL_FILE}")
    print(f"     Claude Code picks this up live — no restart needed.")


def _run_uninstall_skill() -> None:
    """Remove ~/.claude/skills/studon-client/SKILL.md (and the empty dir, plus the legacy `studon` / `search-studon` dirs)."""
    _prune_legacy_skill_dirs()
    if SKILL_FILE.exists():
        SKILL_FILE.unlink()
        print(f"  ✅ Removed {SKILL_FILE}")
    else:
        print(f"  • No skill file at {SKILL_FILE}")
    try:
        SKILL_DIR.rmdir()  # only succeeds if empty
        print(f"  ✅ Removed empty {SKILL_DIR}")
    except OSError:
        pass  # non-empty (user added other files) — leave it


def _run_install(check_interval: int = 5) -> None:
    """
    Unified installer: replaces setup_daily_sync.sh.
    Registers the cron jobs (CronInstaller) and the 'studon-client' shell alias
    (ToolInstaller), and removes the hand-written entries of older versions.
    """
    import importlib.util

    print("╔════════════════════════════════════════════════════════════╗")
    print("║          StudOn Daily Sync Setup                          ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()

    # --- Platform ---
    system     = platform_module.system()
    distro     = system
    is_ubuntu  = False
    if system == "Linux":
        try:
            content = Path('/etc/os-release').read_text()
            for line in content.splitlines():
                if line.startswith('NAME='):
                    distro = line.split('=', 1)[1].strip('"\'')
                    break
            if 'ubuntu' in content.lower():
                is_ubuntu = True
        except OSError:
            pass

    print(f"Platform: {distro}")
    print()

    if not is_ubuntu:
        print("WARNING: Only tested on Kubuntu/Ubuntu Linux.")
        print(f"  Crontab, Firefox cookies, and path conventions may differ on {distro}.")
        print()
        if input("Continue anyway? [y/N]: ").strip().lower() != 'y':
            print("Setup cancelled.")
            return
        print()
    else:
        print(f"Running on tested platform: {distro}")
        print()

    # --- Dependencies ---
    print("Checking Python dependencies...")
    REQUIRED = {
        'requests':       'requests',
        'bs4':            'beautifulsoup4',
        'pyperclip':      'pyperclip',
        'browser_cookie3':'browser-cookie3',
        'tabulate':       'tabulate',
        'yaml':           'pyyaml',
        'keyring':        'keyring',
    }
    missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"  Missing: {', '.join(missing)}")
        print(f"  Install: pip install {' '.join(missing)}")
        if input("  Continue anyway? [y/N]: ").strip().lower() != 'y':
            print("Setup cancelled.")
            return
    else:
        print("  All dependencies present.")
    print()

    # --- Download path ---
    cfg          = load_config()
    current_path = cfg.get("downloads_path")
    if current_path:
        print(f"Download path: {current_path}  (from config.json)")
    else:
        print("No download path configured (will use ./studon_downloads).")
        answer = input("Set a persistent download path now? (leave blank to skip): ").strip()
        if answer:
            expanded = str(Path(answer).expanduser().resolve())
            cfg["downloads_path"] = expanded
            save_config(cfg)
            print(f"  Saved: {expanded}")
    print()

    # --- Cron jobs (cli-tools-kit CronInstaller) ---
    desired_cmds = _cron_lines(check_interval)

    print("Cron entries:")
    for c in desired_cmds:
        print(f"  {c}")
    print()

    cron_ok = False
    if _CRON is None:
        print("  ERROR: cli-tools-kit not importable — pip install -r requirements.txt")
    else:
        try:
            n = _remove_legacy_cron_lines()
            if n:
                print(f"  Replaced {n} hand-written cron line(s) from an older --install.")
            _CRON.install(desired_cmds)
            cron_ok = True
        except (RuntimeError, subprocess.CalledProcessError) as e:
            print(f"  Failed: {e}")
    print()

    # --- Shell alias (cli-tools-kit ToolInstaller) ---
    print("Installing 'studon-client' shell alias...")
    if _remove_legacy_bashrc_function():
        print("  Removed the old studon-client() function from ~/.bashrc.")
    if _HAS_INSTALLER:
        _install_alias()
    else:
        print("  ERROR: cli-tools-kit not importable — alias not installed.")
    print()

    # --- Summary ---
    print("╔════════════════════════════════════════════════════════════╗")
    if cron_ok:
        print("║              ✅ Setup Completed Successfully!              ║")
    else:
        print("║           ⚠️  Setup Completed (cron needs attention)      ║")
    print("╚════════════════════════════════════════════════════════════╝")
    if not cron_ok:
        print()
        print("To add the cron jobs manually:")
        print("   crontab -e")
        for c in desired_cmds:
            print(f"   # Add: {c}")
        print()

    # --- Lecture mapping (interactive) ---
    print()
    print("Running --map-lectures to verify your timetable ↔ course mapping...")
    print("(You can re-run this any time with: python studon_client.py --map-lectures)")
    print()
    try:
        run_map_lectures_interactive()
    except Exception as e:
        logger.warning(f"Could not run lecture mapping: {e}")
        print(f"⚠️  Lecture mapping skipped: {e}")

    # --- Claude Code skill (best-effort; safe if ~/.claude doesn't exist) ---
    print()
    print("→ Registering Claude Code skill...")
    try:
        _run_install_skill()
    except Exception as e:
        print(f"⚠️  Skill registration skipped: {e}")


# --- FEEDBACK MAIL CHECKER (FAUmail IMAP → StudOn exc page → PDF download) ---

FAUMAIL_IMAP_HOST = "faumail.fau.de"
FAUMAIL_IMAP_PORT = 993
KEYRING_SERVICE = "studon-scraper-faumail"
FEEDBACK_STATE_FILE = os.path.join(_SCRIPT_DIR, ".studon_feedback_state.json")
FEEDBACK_SUBJECT_PATTERN = re.compile(r"Es wurde eine neue Feedback-Datei", re.IGNORECASE)
EXC_URL_PATTERN = re.compile(r"https://www\.studon\.fau\.de/studon/goto?\.php\?[^\s]+|https://www\.studon\.fau\.de/studon/go/exc/\d+/\d+")
UEBUNGSEINHEIT_PATTERN = re.compile(r"Übungseinheit:\s*(.+)", re.IGNORECASE)
UEBUNG_PATTERN = re.compile(r"^Übung:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _is_genuine_feedback_email(subject: str, from_header: str) -> bool:
    """True if a message looks like a real StudOn feedback notification.

    Requires both the subject pattern and a sender address within fau.de.
    IMAP messages are unauthenticated input: without the sender check,
    anyone who emails the user could trigger an authenticated StudOn fetch
    just by sending a message with the expected subject line. FAU's own
    mail server rejects spoofed @fau.de senders, so an in-domain sender is
    a meaningful authenticity signal here.
    """
    if not FEEDBACK_SUBJECT_PATTERN.search(subject or ''):
        return False
    _name, addr = email.utils.parseaddr(from_header or '')
    addr = addr.lower().strip()
    if '@' not in addr:
        return False
    host = addr.rsplit('@', 1)[1]
    return host == 'fau.de' or host.endswith('.fau.de')


def _load_feedback_state() -> dict:
    """Load feedback queue + processed message-ids."""
    if not os.path.exists(FEEDBACK_STATE_FILE):
        return {"processed_message_ids": [], "queue": []}
    try:
        with open(FEEDBACK_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("processed_message_ids", [])
            data.setdefault("queue", [])
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read feedback state file ({e}); starting fresh.")
        return {"processed_message_ids": [], "queue": []}


def _save_feedback_state(state: dict) -> None:
    with open(FEEDBACK_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


_IMAP_FOLDER_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)')
_IMAP_SKIP_FLAGS = {b"\\Noselect", b"\\NoSelect"}
_IMAP_SKIP_NAMES = {"trash", "junk", "spam", "drafts", "sent", "templates", "outbox"}


def _list_imap_folders(M: imaplib.IMAP4) -> List[str]:
    """Return all selectable folder names on the server (skipping Trash/Spam/Sent etc)."""
    try:
        typ, data = M.list()
    except Exception:
        return ["INBOX"]
    if typ != "OK" or not data:
        return ["INBOX"]
    out: List[str] = []
    for raw in data:
        if not raw:
            continue
        if isinstance(raw, tuple):
            raw = b"".join(raw)
        m = _IMAP_FOLDER_LIST_RE.match(raw)
        if not m:
            continue
        flags = m.group("flags").split()
        if any(f in _IMAP_SKIP_FLAGS for f in flags):
            continue
        name_bytes = m.group("name")
        if name_bytes.startswith(b'"') and name_bytes.endswith(b'"'):
            name_bytes = name_bytes[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        try:
            name = name_bytes.decode("ascii")
        except UnicodeDecodeError:
            name = name_bytes.decode("utf-8", errors="replace")
        leaf = name.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
        if leaf in _IMAP_SKIP_NAMES:
            continue
        out.append(name)
    if "INBOX" not in out:
        out.insert(0, "INBOX")
    return out


def _get_imap_password(email_addr: str) -> Optional[str]:
    """Return password from keyring, or None if missing."""
    if keyring is None:
        logger.error("'keyring' package not installed. Run: pip install keyring")
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, email_addr)
    except Exception as e:
        logger.error(f"Could not read password from keyring: {e}")
        return None


def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_message_text(msg: "email_mod.message.Message") -> str:
    """Return the plain-text body of an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="replace")
                    except (LookupError, TypeError):
                        return payload.decode("utf-8", errors="replace")
        # Fallback: HTML stripped
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return BeautifulSoup(html, "html.parser").get_text("\n")
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _extract_studon_exc_url(text: str) -> Optional[str]:
    """Find the first studon.fau.de exc/goto link in the email body."""
    m = re.search(r"https://www\.studon\.fau\.de/studon/(?:go/exc/\d+/\d+|goto[^\s<>\"']+)", text)
    return m.group(0) if m else None


def fetch_feedback_emails(days_back: int = 30, verbose: bool = False) -> List[dict]:
    """
    Connect to FAUmail IMAP, find unprocessed feedback notification emails,
    extract their StudOn URLs and metadata. Returns new entries to queue.
    Does NOT mark messages as read; that happens after the PDF is downloaded.
    """
    cfg = load_config()
    email_addr = cfg.get("imap_email")
    if not email_addr:
        logger.info("No IMAP email configured. Run --install-imap to set it up.")
        return []
    password = _get_imap_password(email_addr)
    if not password:
        logger.warning(f"No IMAP password in keyring for {email_addr}. Run --install-imap.")
        return []

    state = _load_feedback_state()
    processed = set(state.get("processed_message_ids", []))
    queued_ids = {q.get("message_id") for q in state.get("queue", []) if q.get("message_id")}

    new_entries: List[dict] = []
    try:
        M = imaplib.IMAP4_SSL(FAUMAIL_IMAP_HOST, FAUMAIL_IMAP_PORT)
        M.login(email_addr, password)
    except (imaplib.IMAP4.error, OSError) as e:
        logger.error(f"FAUmail IMAP login failed: {e}")
        return []

    def _say(msg: str) -> None:
        if verbose:
            print(msg)
        logger.debug(msg)

    try:
        since = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        folders = _list_imap_folders(M)
        _say(f"FAUmail: scanning {len(folders)} folder(s): {folders}")
        total_subject_matches = 0

        for folder in folders:
            try:
                typ, _ = M.select(f'"{folder}"', readonly=False)
                if typ != "OK":
                    _say(f"  cannot select folder {folder!r}, skipping")
                    continue
            except Exception as e:
                _say(f"  select failed for {folder!r}: {e}")
                continue

            typ, data = M.search(None, f'(SINCE "{since}")')
            if typ != "OK" or not data or not data[0]:
                _say(f"  [{folder}] 0 messages since {since}")
                continue
            uids = data[0].split()
            if not uids:
                _say(f"  [{folder}] 0 messages since {since}")
                continue
            _say(f"  [{folder}] {len(uids)} message(s) since {since}")

            for uid in uids:
                try:
                    typ, msg_data = M.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID FROM)])")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    header_bytes = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                    header_msg = email_mod.message_from_bytes(header_bytes)
                    subject = _decode_header(header_msg.get("Subject", ""))
                    if not FEEDBACK_SUBJECT_PATTERN.search(subject):
                        continue
                    from_hdr = _decode_header(header_msg.get("From", ""))
                    if not _is_genuine_feedback_email(subject, from_hdr):
                        logger.warning(f"Feedback email '{subject[:60]}' rejected: "
                                       f"sender {from_hdr!r} is not within fau.de — possible spoof.")
                        continue
                    total_subject_matches += 1
                    early_message_id = (header_msg.get("Message-ID") or "").strip()
                    if early_message_id and early_message_id in processed:
                        _say(f"  · skip [{folder}] '{subject[:60]}' — already processed previously")
                        continue
                    if early_message_id and early_message_id in queued_ids:
                        _say(f"  · skip [{folder}] '{subject[:60]}' — already in queue")
                        continue
                    typ, msg_data = M.fetch(uid, "(BODY.PEEK[])")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    msg = email_mod.message_from_bytes(msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0])
                    message_id = (msg.get("Message-ID") or "").strip()
                    if not message_id:
                        message_id = f"fallback-{folder}-{uid.decode()}-{subject[:40]}"
                    if message_id in processed or message_id in queued_ids:
                        continue

                    body = _extract_message_text(msg)
                    url = _extract_studon_exc_url(body)
                    if not url:
                        logger.warning(f"Feedback email '{subject[:60]}' had no StudOn URL — skipping.")
                        continue

                    ueb_match = UEBUNG_PATTERN.search(body)
                    sheet_match = UEBUNGSEINHEIT_PATTERN.search(body)
                    entry = {
                        "url": url,
                        "subject": subject,
                        "message_id": message_id,
                        "imap_folder": folder,
                        "imap_uid": uid.decode(),
                        "uebung": ueb_match.group(1).strip() if ueb_match else "",
                        "sheet": sheet_match.group(1).strip() if sheet_match else "",
                        "first_seen": datetime.now().isoformat(timespec="seconds"),
                        "attempts": 0,
                    }
                    new_entries.append(entry)
                    queued_ids.add(message_id)
                    _say(f"  ✓ queued: [{folder}] {entry['sheet'] or subject[:50]} → {url}")
                except Exception as e:
                    logger.warning(f"Could not parse message UID {uid!r} in {folder}: {e}")
                    continue
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass

    if verbose:
        print(f"FAUmail: {total_subject_matches} subject match(es), {len(new_entries)} new (rest already queued/processed).")
    if new_entries:
        state["queue"].extend(new_entries)
        _save_feedback_state(state)
    return new_entries


_FEEDBACK_DOWNLOAD_HREF = re.compile(
    r"(cmd=(sendfile|download|downloadFile|downloadFeedbackFile|downloadGlobalFeedbackFile|deliverFile))"
    r"|(target=file_)"
    r"|(/download/)",
    re.IGNORECASE,
)


def discover_feedback_files(exc_url: str, session: requests.Session) -> List[Dict[str, str]]:
    """
    Aggressively discover feedback-file download links on an ILIAS exercise page.
    Recurses one level into linked sub-pages (assignment views, file-feedback subpages).
    """
    found: List[Dict[str, str]] = []
    seen_pages: set = set()
    seen_dl_urls: set = set()

    def _scan(url: str, depth: int = 0) -> None:
        if url in seen_pages or depth > 2:
            return
        seen_pages.add(url)
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.debug(f"feedback discover: GET {url} failed: {e}")
            return
        if "ilstartupgui" in resp.url or "/login.php" in resp.url:
            raise StudOnError("Session expired — redirected to login page.", "Log into StudOn in Firefox and retry.")
        soup = BeautifulSoup(resp.text, "html.parser")

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            full = urljoin(resp.url, href)
            if not _url_host_matches(full, STUDON_DOMAIN):
                continue
            if _FEEDBACK_DOWNLOAD_HREF.search(href) and full not in seen_dl_urls:
                seen_dl_urls.add(full)
                name = clean_filename(link.get_text(strip=True)) or f"feedback_{len(found)+1}"
                found.append({"url": full, "name": name})

        if depth < 2:
            for link in soup.find_all("a", href=True):
                href = link.get("href", "")
                if not href:
                    continue
                full = urljoin(resp.url, href)
                if not _url_host_matches(full, STUDON_DOMAIN) or full in seen_pages:
                    continue
                # Recurse into exercise/assignment sub-views
                if re.search(r"(cmdClass=ilexercise|ass_id=|cmd=showAssignment|cmd=submissionFeedback|cmd=showOverview|exc_listfeedback|listFeedback)", href, re.IGNORECASE):
                    _scan(full, depth + 1)

    _scan(exc_url, 0)
    return found


def _resolve_course_name(exc_url: str, session: requests.Session) -> Tuple[str, Optional[str]]:
    """
    Fetch the exc page, derive the course name from breadcrumb / page header.
    Returns (course_name, breadcrumb_course_url_if_found).
    """
    try:
        resp = session.get(exc_url, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Could not fetch exc page {exc_url}: {e}")
        return "Unknown Course", None

    if "ilstartupgui" in resp.url or "/login.php" in resp.url:
        raise StudOnError("Session expired — redirected to login page.", "Log into StudOn in Firefox and retry.")

    soup = BeautifulSoup(resp.text, "html.parser")
    course_url = None
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if "target=crs_" in href or re.search(r"/go/crs/\d+", href):
            text = link.get_text(strip=True)
            if text:
                return clean_filename(text), urljoin(resp.url, href)
    title = extract_course_title(exc_url, session)
    return clean_filename(title) if title else "Unknown Course", course_url


def _process_feedback_queue(session: requests.Session, mark_seen: bool = True, verbose: bool = False) -> Tuple[int, int, List[str]]:
    """
    Walk the feedback queue, download PDFs for any URL we can now reach.
    On success, mark the IMAP message as read and move the entry to processed.
    Returns (n_processed, n_downloaded_files, downloaded_paths).
    """
    state = _load_feedback_state()
    queue = state.get("queue", [])
    if not queue:
        return 0, 0, []

    cfg = load_config()
    email_addr = cfg.get("imap_email")
    password = _get_imap_password(email_addr) if email_addr else None
    M = None
    if mark_seen and email_addr and password:
        try:
            M = imaplib.IMAP4_SSL(FAUMAIL_IMAP_HOST, FAUMAIL_IMAP_PORT)
            M.login(email_addr, password)
        except Exception as e:
            logger.warning(f"Could not connect to IMAP to mark messages seen: {e}")
            M = None
    selected_folder: Optional[str] = None

    feedback_root = (Path(DOWNLOAD_FOLDER) / "Feedback").resolve()
    feedback_root.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"Feedback output root: {feedback_root}")
    logger.info(f"Feedback output root: {feedback_root}")

    remaining: List[dict] = []
    processed_ids = list(state.get("processed_message_ids", []))
    n_processed = 0
    n_files = 0
    all_paths: List[str] = []

    for entry in queue:
        url = entry["url"]
        try:
            course_name, _ = _resolve_course_name(url, session)
        except StudOnError as e:
            logger.warning(f"Feedback queue: {e}. Leaving in queue.")
            remaining.append(entry)
            continue
        except Exception as e:
            logger.warning(f"Feedback queue: error resolving {url}: {e}. Leaving in queue.")
            entry["attempts"] = entry.get("attempts", 0) + 1
            remaining.append(entry)
            continue

        sheet = clean_filename(entry.get("sheet", "")) or "Feedback"
        target_path = (feedback_root / course_name / sheet).resolve()
        target_path.mkdir(parents=True, exist_ok=True)
        if verbose:
            print(f"  → {course_name} / {sheet}: {target_path}")
        logger.info(f"Feedback target: {target_path}  (from {url})")

        try:
            raw = discover_feedback_files(url, session)
        except StudOnError as e:
            logger.warning(f"Feedback queue: {e}. Leaving '{entry.get('sheet', url)}' in queue.")
            remaining.append(entry)
            continue
        except Exception as e:
            logger.warning(f"Feedback queue: discovery failed for {url}: {e}")
            entry["attempts"] = entry.get("attempts", 0) + 1
            remaining.append(entry)
            continue

        files_to_download: List[Dict[str, str]] = [
            {"url": f["url"], "path": str(target_path), "name": f["name"], "course_title": course_name}
            for f in raw
        ]

        if not files_to_download:
            logger.info(f"Feedback queue: no files yet on {url} (Übung '{entry.get('sheet', '')}'). Leaving in queue.")
            entry["attempts"] = entry.get("attempts", 0) + 1
            if entry["attempts"] >= 3:
                try:
                    debug_html = target_path / f"_debug_exc_page.html"
                    debug_html.write_text(session.get(url, timeout=15).text, encoding="utf-8")
                    logger.warning(f"  Saved page HTML to {debug_html} for inspection (3 failed attempts).")
                except Exception:
                    pass
            remaining.append(entry)
            continue

        downloaded, downloaded_paths = download_all_files(url, files_to_download, session, course_title=course_name, base_path=str(target_path))
        n_files += downloaded
        all_paths.extend(downloaded_paths)
        logger.info(f"Feedback: downloaded {downloaded} file(s) for '{course_name}/{sheet}' → {target_path}")
        if verbose:
            print(f"    ✓ {downloaded} file(s) downloaded (from {len(files_to_download)} candidate link(s))")
            for p in downloaded_paths:
                print(f"      • {Path(p).resolve()}")

        # Only mark as processed if we actually got file(s). Otherwise leave in queue for retry.
        if downloaded == 0:
            entry["attempts"] = entry.get("attempts", 0) + 1
            if entry["attempts"] >= 3:
                try:
                    debug_html = target_path / f"_debug_exc_page.html"
                    debug_html.write_text(session.get(url, timeout=15).text, encoding="utf-8")
                    logger.warning(f"  Saved page HTML to {debug_html} for inspection (3 failed attempts).")
                    if verbose:
                        print(f"    ⚠️  No actual files downloaded from {len(files_to_download)} candidate link(s). Saved HTML: {debug_html}")
                except Exception:
                    pass
            remaining.append(entry)
            continue

        n_processed += 1

        if M is not None:
            folder = entry.get("imap_folder", "INBOX")
            try:
                if folder != selected_folder:
                    typ, _ = M.select(f'"{folder}"', readonly=False)
                    if typ != "OK":
                        raise RuntimeError(f"select {folder!r} failed: {typ}")
                    selected_folder = folder
                M.store(entry["imap_uid"].encode(), "+FLAGS", "\\Seen")
            except Exception as e:
                logger.warning(f"Could not mark UID {entry['imap_uid']} in {folder!r} as seen: {e}")

        mid = entry.get("message_id")
        if mid and mid not in processed_ids:
            processed_ids.append(mid)

    if M is not None:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass

    state["queue"] = remaining
    state["processed_message_ids"] = processed_ids[-500:]  # cap history
    _save_feedback_state(state)
    return n_processed, n_files, all_paths


def check_and_process_feedback(session: Optional[requests.Session] = None, verbose: bool = False) -> Tuple[int, int, List[str]]:
    """High-level entry: scan inbox for new notifications, then process the queue."""
    new = fetch_feedback_emails(verbose=verbose)
    if new:
        logger.info(f"Queued {len(new)} new feedback notification(s).")
    if session is None:
        session = _make_session()
    if session is None:
        logger.info("StudOn session not available; feedback URLs remain queued.")
        return 0, 0, []
    return _process_feedback_queue(session, verbose=verbose)


def _run_install_imap() -> None:
    """Interactive setup for FAUmail IMAP credentials (stored in keyring)."""
    global keyring
    if keyring is None:
        print("The 'keyring' package is required to securely store the FAUmail password.")
        if input("Install it now via pip? [Y/n]: ").strip().lower() == "n":
            print("Aborted. Run 'pip install keyring' manually, then retry.")
            return
        result = subprocess.run([sys.executable, "-m", "pip", "install", "keyring"])
        if result.returncode != 0:
            print("❌ pip install failed.")
            return
        try:
            import keyring as _kr
            keyring = _kr
        except ImportError as e:
            print(f"❌ Still cannot import keyring after install: {e}")
            return
        print("✅ keyring installed.\n")

    cfg = load_config()
    default_email = cfg.get("imap_email", "steffen.probst@fau.de")
    print("╔════════════════════════════════════════════════════════════╗")
    print("║          FAUmail IMAP Setup (feedback checker)            ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()
    print(f"Server: {FAUMAIL_IMAP_HOST}:{FAUMAIL_IMAP_PORT} (SSL)")
    print()
    answer = input(f"FAU email address [{default_email}]: ").strip() or default_email
    password = getpass.getpass(f"IDM password for {answer} (input hidden): ").strip()
    if not password:
        print("No password entered, aborting.")
        return

    print("\nVerifying credentials...")
    try:
        M = imaplib.IMAP4_SSL(FAUMAIL_IMAP_HOST, FAUMAIL_IMAP_PORT)
        M.login(answer, password)
        M.select("INBOX")
        M.logout()
    except Exception as e:
        print(f"❌ Login failed: {e}")
        print("   Credentials NOT saved.")
        return

    try:
        keyring.set_password(KEYRING_SERVICE, answer, password)
    except Exception as e:
        print(f"❌ Could not store password in keyring: {e}")
        return

    cfg["imap_email"] = answer
    save_config(cfg)
    print(f"\n✅ Saved. Email in {CONFIG_FILE}, password in keyring service '{KEYRING_SERVICE}'.")
    print("   Feedback checks will now run as part of --daily-sync.")
    print("   Manual trigger: python3 studon_client.py --check-feedback")


def _is_imap_installed() -> bool:
    """Return True if FAUmail credentials are configured (email + keyring entry)."""
    cfg = load_config()
    email_addr = cfg.get("imap_email")
    if not email_addr or keyring is None:
        return False
    try:
        return keyring.get_password(KEYRING_SERVICE, email_addr) is not None
    except Exception:
        return False


def _run_uninstall_imap() -> None:
    """Remove FAUmail credentials and clear the feedback queue state."""
    cfg = load_config()
    email_addr = cfg.get("imap_email")
    removed = False
    if email_addr and keyring is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, email_addr)
            print(f"  ✅ Password removed from keyring for {email_addr}.")
            removed = True
        except keyring.errors.PasswordDeleteError:
            print(f"  No keyring entry found for {email_addr}.")
        except Exception as e:
            print(f"  Could not remove keyring entry: {e}")
    if "imap_email" in cfg:
        cfg.pop("imap_email", None)
        save_config(cfg)
        print("  ✅ Removed imap_email from config.json.")
        removed = True
    if os.path.exists(FEEDBACK_STATE_FILE):
        try:
            os.remove(FEEDBACK_STATE_FILE)
            print(f"  ✅ Cleared feedback state ({FEEDBACK_STATE_FILE}).")
        except OSError as e:
            print(f"  Could not remove state file: {e}")
    if not removed:
        print("  Nothing to uninstall — feedback checker was not configured.")


def _make_session() -> Optional[requests.Session]:
    """Load Firefox cookies and return an authenticated session, or None on failure."""
    try:
        cj = browser_cookie3.firefox(domain_name=STUDON_DOMAIN)
        session = requests.Session()
        session.cookies.update(cj)
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        return session
    except Exception as e:
        print(f"❌ Could not load Firefox cookies: {e}")
        print("   Make sure you are logged into StudOn in Firefox.")
        return None


# --- INTERACTIVE BROWSER LOGIN RECOVERY ---

# Mapping of user-facing browser names to browser_cookie3 loader functions
# and common Linux binary names for launching.
_BROWSER_REGISTRY: List[Tuple[str, str, str]] = [
    # (display_name, browser_cookie3_function_name, linux_binary_name)
    ("Firefox",  "firefox",  "firefox"),
    ("Chrome",   "chrome",   "google-chrome"),
    ("Chromium", "chromium", "chromium-browser"),
    ("Brave",    "brave",    "brave-browser"),
    ("Edge",     "edge",     "microsoft-edge"),
    ("Opera",    "opera",    "opera"),
    ("Vivaldi",  "vivaldi",  "vivaldi"),
]


def _get_first_course_url() -> str:
    """Return the StudOn source URL of the first registered course.

    Falls back to the Campo timetable URL if no courses are registered.

    Returns:
        A URL string suitable for triggering an SSO login.
    """
    metadata_files = find_all_metadata_files(DOWNLOAD_FOLDER)
    if metadata_files:
        # metadata_files is List[Tuple[path, source_url, folder]]
        return metadata_files[0][1]
    return CAMPO_TIMETABLE_URL


def _try_load_cookies_from_browser(browser_name: str) -> Optional[requests.Session]:
    """Attempt to load StudOn cookies from a specific browser and validate the session.

    Args:
        browser_name: The browser_cookie3 function name (e.g. 'firefox', 'chrome').

    Returns:
        A valid, authenticated requests.Session, or None if cookies are
        unavailable or the session is expired.
    """
    loader_func = getattr(browser_cookie3, browser_name, None)
    if loader_func is None:
        logger.debug(f"browser_cookie3 has no loader for '{browser_name}'")
        return None
    try:
        cookie_jar = loader_func(domain_name=STUDON_DOMAIN)
        session = requests.Session()
        session.cookies.update(cookie_jar)
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        # Validate: try to access a StudOn page and check we're not redirected to login
        metadata_files = find_all_metadata_files(DOWNLOAD_FOLDER)
        if not metadata_files:
            # No courses to validate against — accept the session optimistically
            return session

        test_url = metadata_files[0][1]
        try:
            response = session.get(test_url, timeout=10, allow_redirects=True)
            if 'login.php' in response.url or 'ilstartupgui' in response.url:
                logger.debug(f"Cookies from {browser_name} led to login redirect")
                return None
            return session
        except requests.RequestException as request_error:
            logger.debug(f"Validation request failed for {browser_name}: {request_error}")
            return None
    except Exception as cookie_error:
        logger.debug(f"Could not load cookies from {browser_name}: {cookie_error}")
        return None


def _open_url_in_browser(url: str, browser_binary: Optional[str] = None) -> bool:
    """Open a URL in a browser and wait for the user to finish logging in.

    Uses the system default browser when *browser_binary* is None, otherwise
    launches the specified binary directly.

    Args:
        url: The URL to open.
        browser_binary: Optional Linux binary name (e.g. 'google-chrome').
            When None, ``webbrowser.open()`` is used (delegates to xdg-open).

    Returns:
        True if the browser was launched successfully, False otherwise.
    """
    try:
        if browser_binary is None:
            webbrowser.open(url)
            return True
        else:
            proc = subprocess.Popen(
                [browser_binary, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait briefly to catch immediate launch failures (e.g. binary not found)
            try:
                proc.wait(timeout=2)
                # If the process exits within 2s with non-zero, the binary likely doesn't exist
                if proc.returncode and proc.returncode != 0:
                    return False
            except subprocess.TimeoutExpired:
                pass  # Still running — that's expected for a GUI browser
            return True
    except FileNotFoundError:
        return False
    except Exception as launch_error:
        logger.debug(f"Failed to open browser '{browser_binary}': {launch_error}")
        return False


def _interactive_login_recovery() -> Optional[requests.Session]:
    """Orchestrate an interactive browser-based login recovery flow.

    Called when a manual 'update all' detects an expired session. The flow is:

    1. Open the first registered course URL in the **system default browser**.
    2. Wait for the user to press Enter after logging in.
    3. Try loading cookies from every known browser.
    4. If still no valid session, present a browser selection list (with the
       full URL displayed for manual copy-paste).
    5. On success with a non-default browser, save ``preferred_browser`` to
       ``config.json`` for automatic reuse in future runs.
    6. On repeated failure, re-show the list up to 3 times.

    Returns:
        A valid, authenticated requests.Session, or None if recovery failed.
    """
    login_url = _get_first_course_url()
    config = load_config()
    preferred_browser = config.get("preferred_browser")

    # ── Step 1: Open default browser ──────────────────────────────────────────
    print(f"\n🔑 Opening login page in your default browser...")
    print(f"   URL: {login_url}")
    _open_url_in_browser(login_url)
    input("\n   Press Enter after you have logged in...")

    # ── Step 2: Try preferred browser first, then all known browsers ─────────
    browser_load_order: List[str] = []
    if preferred_browser:
        browser_load_order.append(preferred_browser)
    for _display, bc3_name, _binary in _BROWSER_REGISTRY:
        if bc3_name not in browser_load_order:
            browser_load_order.append(bc3_name)

    print("   Checking for valid session cookies...", end='', flush=True)
    for bc3_name in browser_load_order:
        session = _try_load_cookies_from_browser(bc3_name)
        if session is not None:
            display = next((d for d, b, _ in _BROWSER_REGISTRY if b == bc3_name), bc3_name)
            print(f" ✓ (found in {display})")
            # Save preference if it wasn't already the preferred one
            if bc3_name != preferred_browser:
                config["preferred_browser"] = bc3_name
                save_config(config)
                print(f"   💾 Saved '{display}' as preferred browser for future logins.")
            return session
    print(" ✗ (no valid cookies found)")

    # ── Step 3: Browser selection list ────────────────────────────────────────
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        print(f"\n⚠️  Could not find valid StudOn cookies in any browser.")
        print(f"   Please select a browser to open for login (attempt {attempt}/{max_attempts}):")
        print(f"   URL: {login_url}")
        print()

        # Build choices
        browser_choices: List[str] = []
        for display_name, _bc3, _binary in _BROWSER_REGISTRY:
            browser_choices.append(display_name)
        browser_choices.append("Skip (use URL above manually)")

        if questionary:
            selected = questionary.select(
                "Select browser:",
                choices=browser_choices,
            ).ask()
        else:
            for idx, label in enumerate(browser_choices, 1):
                print(f"  {idx}. {label}")
            try:
                choice_idx = int(input("Choice: ").strip()) - 1
                selected = browser_choices[choice_idx] if 0 <= choice_idx < len(browser_choices) else None
            except (ValueError, EOFError, IndexError):
                selected = None

        if selected is None or selected == "Skip (use URL above manually)":
            print(f"\n📋 Please open this URL manually in any browser and log in:")
            print(f"   {login_url}")
            input("\n   Press Enter after you have logged in...")
            # Try all browsers one more time
            for bc3_name in browser_load_order:
                session = _try_load_cookies_from_browser(bc3_name)
                if session is not None:
                    display = next((d for d, b, _ in _BROWSER_REGISTRY if b == bc3_name), bc3_name)
                    print(f"   ✓ Found valid session in {display}!")
                    config["preferred_browser"] = bc3_name
                    save_config(config)
                    print(f"   💾 Saved '{display}' as preferred browser.")
                    return session
            continue

        # Find the matching registry entry
        registry_match = next(
            ((d, bc3, binary) for d, bc3, binary in _BROWSER_REGISTRY if d == selected),
            None,
        )
        if registry_match is None:
            continue

        display_name, bc3_name, binary_name = registry_match
        print(f"   Opening {display_name}...")
        launched = _open_url_in_browser(login_url, browser_binary=binary_name)
        if not launched:
            print(f"   ❌ Could not launch {display_name} ('{binary_name}' not found).")
            print(f"   📋 Copy this URL into any browser: {login_url}")
            continue

        input(f"\n   Press Enter after you have logged in via {display_name}...")

        # Try the selected browser first, then all others
        session = _try_load_cookies_from_browser(bc3_name)
        if session is not None:
            print(f"   ✓ Login successful via {display_name}!")
            config["preferred_browser"] = bc3_name
            save_config(config)
            print(f"   💾 Saved '{display_name}' as preferred browser for future logins.")
            return session

        # Try remaining browsers in case user logged in via a different one
        for other_bc3 in browser_load_order:
            if other_bc3 == bc3_name:
                continue
            session = _try_load_cookies_from_browser(other_bc3)
            if session is not None:
                other_display = next((d for d, b, _ in _BROWSER_REGISTRY if b == other_bc3), other_bc3)
                print(f"   ✓ Found valid session in {other_display}!")
                config["preferred_browser"] = other_bc3
                save_config(config)
                print(f"   💾 Saved '{other_display}' as preferred browser.")
                return session

        print(f"   ❌ Still no valid cookies found after {display_name} login.")

    # All attempts exhausted
    print(f"\n❌ Could not establish a valid StudOn session after {max_attempts} attempts.")
    print(f"   📋 You can try logging in manually at: {login_url}")
    print(f"   Then re-run the scraper.")
    return None


def _print_discovery_preview(url: str, session: requests.Session, base_path: str, debug: bool = False) -> None:
    """
    Run discovery (no downloads) and print a grouped file preview.
    Shows what would be fetched and to which local path.
    """
    print("\n--- Discovery Preview (no files will be downloaded) ---")
    course_title = extract_course_title(url, session, debug=debug)

    root_folder = base_path
    if course_title:
        dest = os.path.join(root_folder, course_title)
    else:
        dest = root_folder

    print(f"📚 Course  : {course_title or '(unknown)'}")
    print(f"📁 Dest    : {dest}")
    print("🔎 Scanning course pages...")

    all_files: List[Dict[str, str]] = []
    discover_items_recursive(url, dest, session, all_files, course_title, debug=debug)

    if not all_files:
        print("   (no downloadable files found)")
        return

    # Group files by their subfolder relative to dest
    by_folder: Dict[str, List[str]] = {}
    for f in all_files:
        folder = os.path.relpath(f['path'], dest) if f['path'] != dest else "."
        by_folder.setdefault(folder, []).append(f['name'])

    total = len(all_files)
    print(f"\n{'─'*52}")
    for folder in sorted(by_folder):
        label = folder if folder != "." else "(root)"
        print(f"  {label}/")
        for name in by_folder[folder]:
            print(f"    • {name}")
    print(f"{'─'*52}")
    print(f"  {total} file(s) total → {dest}")


def _run_clip_mode(debug: bool = False) -> None:
    """
    Clipboard quick-fetch mode (invoked by the 'studon-client' shell function).
    1. Read clipboard — exit silently if no StudOn URL.
    2. Ask user to confirm fetch.
    3. Run discovery preview.
    4. Ask user to confirm download.
    5. Download.
    """
    # 1. Read clipboard
    try:
        clip = pyperclip.paste().strip()
    except Exception:
        print("❌ Could not read clipboard.")
        return

    if not _is_studon_url(clip):
        if clip:
            print(f"Clipboard does not contain a StudOn URL:\n  {clip[:80]}")
        else:
            print("Clipboard is empty.")
        return

    print(f"StudOn URL detected:\n  {clip}")
    answer = input("\nFetch this course? [Y/n]: ").strip().lower()
    if answer == 'n':
        print("Aborted.")
        return

    # 2. Load cookies
    session = _make_session()
    if session is None:
        return

    # 3. Discovery preview
    _print_discovery_preview(clip, session, DOWNLOAD_FOLDER, debug=debug)

    # 4. Confirm download
    answer = input("\nProceed with download? [Y/n]: ").strip().lower()
    if answer == 'n':
        print("Aborted. No files downloaded.")
        return

    # 5. Download
    print()
    process_single_url(clip, session, DOWNLOAD_FOLDER, debug=debug)


def _tui_prompt_url() -> Optional[str]:
    """Prompt for a StudOn URL, validating inline. Returns URL or None if cancelled."""
    if questionary:
        url = questionary.text(
            "StudOn course URL:",
            validate=lambda v: True if (v.strip() == "" or _is_studon_url(v.strip()))
                               else "Enter a valid StudOn URL (or leave blank to cancel)",
        ).ask()
        return url.strip() if url and url.strip() else None
    while True:
        url = input("StudOn course URL: ").strip()
        if not url:
            return None
        if _is_studon_url(url):
            return url
        print("Enter a valid StudOn URL (or leave blank to cancel).")


def _tui_prompt_download_path() -> Optional[str]:
    """Prompt for a directory path. Returns resolved path or None if blank."""
    if questionary:
        path = questionary.path("Download folder (blank = keep current):").ask()
        return str(Path(path).expanduser().resolve()) if path and path.strip() else None
    path = input("Download folder (blank = keep current): ").strip()
    return str(Path(path).expanduser().resolve()) if path else None


def _fetch_timetable_entries() -> Optional[Tuple[str, List[Dict]]]:
    """Fetch and parse the personal campo timetable.

    Returns (page_title, entries) on success, None on failure. Requires
    Firefox cookies for both fau.de and campo.fau.de.
    """
    import re as _re

    print("🔄 Loading campo timetable...")
    try:
        s = requests.Session()
        s.cookies.update(browser_cookie3.firefox(domain_name='fau.de'))
        s.cookies.update(browser_cookie3.firefox(domain_name='campo.fau.de'))
        s.headers.update({'User-Agent': 'Mozilla/5.0'})
        r = s.get(CAMPO_TIMETABLE_URL)
        if r.status_code != 200:
            print(f"❌ campo returned HTTP {r.status_code}. Make sure you are logged in via Firefox.")
            return None
    except Exception as e:
        print(f"❌ Could not fetch timetable: {e}")
        return None

    return _parse_timetable_html(r.text)


def _parse_timetable_html(html: str) -> Optional[Tuple[str, List[Dict]]]:
    """Parse a campo timetable page (Wochen- or Vorlesungszeitansicht) into
    (page_title, entries).

    Shared by the current-semester fetch (`_fetch_timetable_entries`) and the
    non-current-semester fetch (`_fetch_timetable_entries_for_term`) so the
    span-parsing lives in exactly one place. Returns None if no entries parse.
    """
    import re as _re

    soup = BeautifulSoup(html, 'html.parser')
    title_tag = soup.title
    raw_title = title_tag.get_text(strip=True) if title_tag else "Stundenplan"
    page_title = _re.sub(r'\s*[-–]\s*campo\.fau\.de.*$', '', _re.sub(r'\s+', ' ', raw_title)).strip()

    days = [c.get_text(strip=True) for c in soup.find_all('div', class_='colhead')]

    # Build a (col, termin) → JSF button name lookup. The course-detail buttons
    # live outside the schedulePanel divs (smallscreen/mobile section) but share
    # the same scheduleColumn/termin indexing.
    detail_buttons: Dict[Tuple[int, int], str] = {}
    for b in soup.find_all(['button', 'input']):
        bid = b.get('id') or ''
        if not bid.endswith(':course_detail_link_smallscreen'):
            continue
        mcol = _re.search(r'scheduleColumn:(\d+)', bid)
        mtrm = _re.search(r'termin:(\d+)', bid)
        if not (mcol and mtrm):
            continue
        detail_buttons[(int(mcol.group(1)), int(mtrm.group(1)))] = b.get('name') or bid

    entries: List[Dict] = []
    for panel in soup.find_all('div', class_='schedulePanel'):
        pid = panel.get('id', '')
        m = _re.search(r'scheduleColumn:(\d+)', pid)
        col = int(m.group(1)) if m else 0
        m_term = _re.search(r'termin:(\d+)', pid)
        termin = int(m_term.group(1)) if m_term else 0
        day = days[col] if col < len(days) else f"Tag {col+1}"
        detail_button_name = detail_buttons.get((col, termin), '')

        def span(suffix: str) -> str:
            el = panel.find('span', id=lambda x: x and x.endswith(suffix))
            return el.get_text(strip=True) if el else ''

        title_el = panel.find('h3', class_='scheduleTitle')
        title = title_el.get_text(strip=True) if title_el else ''
        times = span(':times')
        time_note = span(':academictimespecificationDefaulttext')  # e.g. "s.t."
        etype = span(':eventtypeShorttext')
        rhythm = span(':rhythmDefaulttext')
        start_date = span(':scheduleStartDate')
        end_date = span(':scheduleEndDate')
        building = span(':buildingDefaulttext')
        room_span = panel.find('span', id='')
        room = room_span.get_text(strip=True) if room_span else ''
        instructor_spans = panel.find_all('span', id=lambda x: x and 'instructorLink' in (x or ''))
        instructors = ', '.join(s.get_text(strip=True) for s in instructor_spans)
        status = span(':workstatusLongtext')
        note_div = panel.find('div', class_='note')
        note = note_div.get_text(strip=True) if note_div else ''

        if not title:
            continue

        time_str = times
        if time_note:
            time_str += f" ({time_note})"

        entries.append({
            'day': day, 'col': col, 'termin': termin, 'title': title, 'time': time_str,
            'type': etype, 'rhythm': rhythm, 'start': start_date, 'end': end_date,
            'room': room, 'building': building, 'instructors': instructors,
            'status': status, 'note': note,
            'detail_button_name': detail_button_name,
        })

    if not entries:
        print("⚠️  No timetable entries found. Are you logged into campo in Firefox?")
        return None

    entries.sort(key=lambda e: (e['col'], e['time']))
    return page_title, entries


def _short_term_label(full_label: str) -> str:
    """Compact filename token for a campo term label.

    'Wintersemester 2026/27' → 'WS2627'; 'Sommersemester 2026' → 'SS26'.
    Falls back to a slug of the raw label for anything unexpected.
    """
    m = re.match(r'\s*(Sommer|Winter)semester\s+(\d{4})(?:/(\d{2,4}))?', full_label)
    if not m:
        return re.sub(r'\W+', '', full_label) or 'term'
    season, y1, y2 = m.group(1), m.group(2), m.group(3)
    if season == 'Sommer':
        return f"SS{y1[-2:]}"
    y2s = y2[-2:] if y2 else f"{int(y1) + 1:04d}"[-2:]
    return f"WS{y1[-2:]}{y2s}"


def _resolve_timetable_term(page_html: str, term_spec: str) -> Optional[Tuple[str, str, str]]:
    """Resolve a --term specifier against the changeTerm select on a campo
    timetable page. Returns (term_id, full_label, short_label) or None.

    Accepts the campo-search style ``eq|<season>|<year>`` (season 1 = Sommer-,
    2 = Wintersemester; the label is matched, not a hardcoded id) as well as a
    raw numeric option id (e.g. ``590``). Matching the select's option *labels*
    keeps the resolution robust across years without pinning IDs.
    """
    soup = BeautifulSoup(page_html, 'html.parser')
    sel = soup.find('select', id=lambda x: x and x.endswith(':changeTerm_input'))
    if sel is None:
        sel = soup.find('select', attrs={'name': lambda x: x and x.endswith(':changeTerm_input')})
    if sel is None:
        return None

    options: List[Tuple[str, str]] = []
    for opt in sel.find_all('option'):
        value = (opt.get('value') or '').strip()
        label = re.sub(r'\s+', ' ', opt.get_text(strip=True))
        if value:
            options.append((value, label))

    target_label: Optional[str] = None
    m = re.match(r'\s*eq\|([12])\|(\d{4})\s*$', term_spec)
    if m:
        season, year = m.group(1), int(m.group(2))
        if season == '1':
            target_label = f"Sommersemester {year}"
        else:
            target_label = f"Wintersemester {year}/{(year + 1) % 100:02d}"
        for value, label in options:
            if label == target_label:
                return value, label, _short_term_label(label)
        return None

    if term_spec.strip().isdigit():
        want = term_spec.strip()
        for value, label in options:
            if value == want:
                return value, label, _short_term_label(label)
        return None

    # Last resort: case-insensitive label match (accepts a full label string).
    want_l = re.sub(r'\s+', ' ', term_spec.strip()).lower()
    for value, label in options:
        if label.lower() == want_l:
            return value, label, _short_term_label(label)
    return None


def _post_timetable_form(
    session: requests.Session, page_html: str, page_url: str,
    overrides: Dict[str, str], button_name: str
) -> Optional[str]:
    """Full-form POST of campo's ``form#plan``: collect every non-submit input
    plus each select's current value, apply *overrides*, trigger *button_name*.

    Mirrors ``_post_jsf_detail_button`` but also carries ``<select>`` values,
    which the timesheet's InputRefresh buttons (changeTerm / auswahlZeitraum)
    need — a JSF AJAX partial to those buttons does not work headlessly, but a
    plain full-form POST re-renders the page with the term/view switched.
    Returns the response HTML on success, or None on failure.
    """
    from urllib.parse import urljoin
    soup = BeautifulSoup(page_html, 'html.parser')
    form = soup.find('form', id='plan') or soup.find('form')
    if form is None:
        return None
    action = form.get('action') or page_url
    post_url = urljoin(page_url, action)
    data: List[Tuple[str, str]] = []
    for inp in form.find_all('input'):
        name = inp.get('name')
        if not name or name in overrides:
            continue
        itype = (inp.get('type') or 'text').lower()
        if itype in ('submit', 'button', 'image'):
            continue
        data.append((name, inp.get('value', '') or ''))
    for sel in form.find_all('select'):
        name = sel.get('name')
        if not name or name in overrides:
            continue
        chosen = None
        for opt in sel.find_all('option'):
            if opt.has_attr('selected'):
                chosen = opt.get('value', '') or ''
                break
        if chosen is None:
            first = sel.find('option')
            chosen = (first.get('value', '') or '') if first else ''
        data.append((name, chosen))
    for name, value in overrides.items():
        data.append((name, value))
    data.append((button_name, ''))
    try:
        r = session.post(post_url, data=data, allow_redirects=True)
    except Exception as e:
        logger.warning(f"Timetable form POST failed: {e}")
        return None
    if r.status_code != 200:
        logger.warning(f"Timetable form POST returned HTTP {r.status_code}.")
        return None
    return r.text


def _find_form_control_name(html: str, suffix: str) -> Optional[str]:
    """Return the name of the form control whose name/id ends with *suffix*."""
    soup = BeautifulSoup(html, 'html.parser')
    el = soup.find(attrs={'name': lambda x: x and x.endswith(suffix)})
    if el is not None:
        return el.get('name')
    el = soup.find(id=lambda x: x and x.endswith(suffix))
    return el.get('name') if el is not None else None


def _fetch_timetable_entries_for_term(term_spec: str) -> Optional[Tuple[str, List[Dict], str]]:
    """Fetch the personal campo timetable for a non-current semester.

    Resolves *term_spec* to a changeTerm option id, then issues two full-form
    POSTs (switch term, then switch to the Vorlesungszeitansicht) and parses the
    resulting page with the shared span-parser. Returns
    (page_title, entries, short_label) on success, None on failure. Requires
    Firefox cookies for both fau.de and campo.fau.de.
    """
    print(f"🔄 Loading campo timetable for term '{term_spec}'...")
    session = _campo_session()
    if session is None:
        return None
    try:
        r = session.get(CAMPO_TIMETABLE_URL)
        if r.status_code != 200:
            print(f"❌ campo returned HTTP {r.status_code}. Make sure you are logged in via Firefox.")
            return None
    except Exception as e:
        print(f"❌ Could not fetch timetable: {e}")
        return None

    resolved = _resolve_timetable_term(r.text, term_spec)
    if resolved is None:
        print(f"❌ Could not resolve term '{term_spec}' against campo's semester list.")
        return None
    term_id, full_label, short_label = resolved
    print(f"   → {full_label} (id {term_id})")

    change_name = _find_form_control_name(r.text, ':changeTerm_input')
    refresh_term = _find_form_control_name(r.text, ':refreshChangeTerm')
    if not (change_name and refresh_term):
        print("❌ campo timetable page is missing the term-switch controls.")
        return None

    html_term = _post_timetable_form(
        session, r.text, r.url, {change_name: term_id}, refresh_term)
    if html_term is None:
        print("❌ Term switch POST failed.")
        return None

    zeitraum_name = _find_form_control_name(html_term, ':auswahl_zeitraum_input')
    refresh_zeit = _find_form_control_name(html_term, ':refreshAuswahlZeitraum')
    change_name2 = _find_form_control_name(html_term, ':changeTerm_input') or change_name
    if not (zeitraum_name and refresh_zeit):
        print("❌ campo timetable page is missing the view-switch controls.")
        return None

    html_view = _post_timetable_form(
        session, html_term, r.url,
        {change_name2: term_id, zeitraum_name: 'vorlesungszeit'}, refresh_zeit)
    if html_view is None:
        print("❌ Vorlesungszeit view POST failed.")
        return None

    parsed = _parse_timetable_html(html_view)
    if parsed is None:
        return None
    page_title, entries = parsed
    return page_title, entries, short_label


def _post_jsf_detail_button(
    session: requests.Session, page_html: str, page_url: str, button_name: str
) -> Optional[str]:
    """Programmatically 'click' a JSF submit button by POSTing the enclosing form.

    Returns the response HTML on success, or None on failure.
    """
    from urllib.parse import urljoin
    soup = BeautifulSoup(page_html, 'html.parser')
    form = soup.find('form', id='plan') or soup.find('form')
    if form is None:
        return None
    action = form.get('action') or page_url
    post_url = urljoin(page_url, action)
    data: List[Tuple[str, str]] = []
    for inp in form.find_all('input'):
        name = inp.get('name')
        if not name:
            continue
        itype = (inp.get('type') or 'text').lower()
        if itype in ('submit', 'button', 'image'):
            continue
        data.append((name, inp.get('value', '') or ''))
    # Trigger the specific command button by name.
    data.append((button_name, ''))
    try:
        r = session.post(post_url, data=data, allow_redirects=True)
    except Exception as e:
        logger.warning(f"JSF detail-button POST failed: {e}")
        return None
    if r.status_code != 200:
        logger.warning(f"JSF detail-button POST returned HTTP {r.status_code}.")
        return None
    return r.text


def _extract_studon_link(detail_html: str) -> Optional[str]:
    """Pick out the 'Link zur Lehrveranstaltung auf StudOn' anchor from a campo detail page."""
    soup = BeautifulSoup(detail_html, 'html.parser')
    for label in soup.find_all('label'):
        if 'StudOn' in label.get_text() and 'Lehrveranstaltung' in label.get_text():
            for_id = label.get('for')
            if for_id:
                answer = soup.find(id=for_id)
                if answer:
                    a = answer.find('a', href=True)
                    if a and _url_host_matches(a['href'], STUDON_DOMAIN):
                        return a['href']
    # Fallback: any campo→studon proxy link on the page.
    for a in soup.find_all('a', href=True):
        href = a['href']
        if _url_host_matches(href, STUDON_DOMAIN) and (
                '/campo/course/' in href or '/studon/' in href):
            return href
    return None


def _resolve_studon_course_url(session: requests.Session, link_url: str) -> Optional[str]:
    """Follow a campo→studon proxy link to its final ILIAS URL."""
    try:
        r = session.get(link_url, allow_redirects=True, timeout=30)
    except Exception as e:
        logger.warning(f"Could not resolve StudOn link {link_url}: {e}")
        return None
    final = r.url
    if not _url_host_matches(final, STUDON_DOMAIN):
        logger.warning(f"Resolved URL is not on studon.fau.de: {final}")
        return None
    return final


def _render_timetable_markdown(page_title: str, entries: List[Dict]) -> str:
    """Render parsed timetable entries to the existing Markdown layout."""
    from datetime import datetime as _dt
    lines = [
        f"# {page_title}",
        f"",
        f"> Generated {_dt.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
    ]

    by_day: Dict[str, List[Dict]] = {}
    for e in entries:
        by_day.setdefault(e['day'], []).append(e)

    for day, day_entries in by_day.items():
        lines.append(f"## {day}")
        lines.append("")
        lines.append("| Zeit | Veranstaltung | Typ | Raum / Gebäude | Dozent |")
        lines.append("|------|--------------|-----|----------------|--------|")
        for e in day_entries:
            room_col = ' / '.join(filter(None, [e['room'], e['building']]))
            status_flag = " ⚠️" if e['note'] else ""
            title_cell = e['title'] + status_flag
            lines.append(f"| {e['time']} | {title_cell} | {e['type']} | {room_col} | {e['instructors']} |")
        lines.append("")

    lines += ["---", "", "## Details", ""]
    for e in entries:
        lines.append(f"### {e['title']}")
        lines.append(f"- **Tag:** {e['day']}")
        lines.append(f"- **Zeit:** {e['time']}")
        if e['type']:
            lines.append(f"- **Typ:** {e['type']}")
        if e['rhythm']:
            lines.append(f"- **Rhythmus:** {e['rhythm']}")
        if e['start'] and e['end']:
            lines.append(f"- **Zeitraum:** {e['start']} – {e['end']}")
        if e['room'] or e['building']:
            room_str = ' / '.join(filter(None, [e['room'], e['building']]))
            lines.append(f"- **Raum:** {room_str}")
        if e['instructors']:
            lines.append(f"- **Dozent:** {e['instructors']}")
        if e['status']:
            lines.append(f"- **Status:** {e['status']}")
        if e['note']:
            lines.append(f"- **Hinweis:** {e['note']}")
        lines.append("")

    return '\n'.join(lines)


def _timetable_cache_path() -> str:
    """Sidecar JSON path for structured timetable entries (consumed by --lecture-sync)."""
    return os.path.join(DOWNLOAD_FOLDER, '.timetable_entries.json')


def _write_timetable_cache(page_title: str, entries: List[Dict]) -> None:
    """Persist parsed entries next to timetable.md for daemon consumption."""
    path = _timetable_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                'fetched_at': datetime.now().isoformat(),
                'page_title': page_title,
                'entries': entries,
            }, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"Could not write timetable cache: {e}")


def _read_timetable_cache() -> Optional[Tuple[datetime, str, List[Dict]]]:
    """Return (fetched_at, page_title, entries) from sidecar cache, or None."""
    path = _timetable_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        fetched_at = datetime.fromisoformat(data['fetched_at'])
        return fetched_at, data.get('page_title', 'Stundenplan'), data.get('entries', [])
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Could not read timetable cache: {e}")
        return None


def fetch_timetable_markdown(output_path: Optional[str] = None,
                             term: Optional[str] = None) -> Optional[str]:
    """Fetch campo timetable, write Markdown (+ structured JSON cache).

    Without *term*, behaviour is unchanged: the current semester is written to
    ``timetable.md`` and the ``.timetable_entries.json`` cache consumed by
    --lecture-sync. With *term* (e.g. ``eq|2|2026`` for WiSe 2026/27, or a raw
    numeric option id), a non-current semester is fetched via the term-switch
    POSTs and written to ``timetable_<label>.md`` (e.g. ``timetable_WS2627.md``)
    — the current-semester ``timetable.md`` and cache are left untouched.

    Returns the output Markdown path on success, None on failure. Requires
    Firefox cookies for both fau.de and campo.fau.de.
    """
    short_label: Optional[str] = None
    if term is not None:
        result = _fetch_timetable_entries_for_term(term)
        if result is None:
            return None
        page_title, entries, short_label = result
    else:
        result = _fetch_timetable_entries()
        if result is None:
            return None
        page_title, entries = result

    md = _render_timetable_markdown(page_title, entries)
    if output_path is None:
        filename = f'timetable_{short_label}.md' if short_label else 'timetable.md'
        output_path = os.path.join(DOWNLOAD_FOLDER, filename)
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"✅ Timetable written to {output_path}")
    # The structured cache feeds --lecture-sync and must reflect the CURRENT
    # semester only — never overwrite it with a non-current export.
    if short_label is None:
        _write_timetable_cache(page_title, entries)
    return output_path


# --- CAMPO PRÜFUNGEN (study-planner detail-view scraper) ---

_CAMPO_PERIOD_RE = re.compile(
    r'(Prüfungs[a-zäöüß]+zeitraum|Anmeldezeitraum|Abmeldezeitraum|Belegungszeitraum)\s+(\S+)\s+von\s+'
    r'([\d.]+)\s+([\d:]+)\s+bis\s+([\d.]+)\s+([\d:]+)(?:\s*-\s*(.+))?',
    re.IGNORECASE,
)


def _parse_campo_pruefung_detail(html: str) -> Optional[Dict]:
    """Parse a Campo studyPlanner Detailansicht page. Returns dict with module
    name and Zeiträume, or None if not a usable Prüfung/module detail.
    """
    soup = BeautifulSoup(html, 'html.parser')
    title_tag = soup.title
    if not title_tag or 'Detailansicht' not in title_tag.get_text():
        return None

    module_name: Optional[str] = None
    for h in soup.find_all('h3'):
        t = h.get_text(' ', strip=True)
        if t.startswith('Permalink: Elementdaten '):
            module_name = t[len('Permalink: Elementdaten '):].strip()
            break
    if not module_name:
        return None

    periods: List[Dict[str, str]] = []
    for ul in soup.find_all('ul', class_='listStyleIconSimple'):
        for li in ul.find_all('li'):
            m = _CAMPO_PERIOD_RE.search(li.get_text(' ', strip=True))
            if m:
                periods.append({
                    'type': m.group(1),
                    'semester': m.group(2),
                    'start': f"{m.group(3)} {m.group(4)}",
                    'end': f"{m.group(5)} {m.group(6)}",
                    'status': (m.group(7) or '').strip(),
                })

    if not periods:
        return None
    return {'module_name': module_name, 'periods': periods}


def _iter_campo_detail_pages(session: requests.Session,
                              max_flow: int = 99,
                              max_step: int = 30,
                              max_empty_flows: int = 12) -> List[Tuple[str, Dict]]:
    """Iterate Campo studyPlanner flowExecutionKeys (e<f>s<s>) reachable in the
    current Firefox session and return (flow_key, parsed_detail) for each
    Detailansicht with Zeiträume. Dedupes by module name.

    The student must have opened the relevant Prüfungs-Detailansichten in
    Firefox beforehand — flow execution keys are server-side per-session state,
    and Zeiträume (Anmelde-/Abmelde-/Prüfungszeitraum) only render on the
    *Prüfung*-side Detail page, not the *Modul*-side detail (which can be reached
    deterministically via unitId/periodId from --modulplan).

    The scan walks up to *max_flow* flows and stops early after *max_empty_flows*
    consecutive flows that yielded no Detail-hit. A fresh session probe on
    2026-06-02 minted key `e63s1`, so the old `max_flow=12` silently missed
    valid Detailansichten in the e13..e60 range — hence the bumped default.
    """
    base = CAMPO_STUDY_PLANNER_URL + '&_flowExecutionKey='
    seen_modules: set = set()
    results: List[Tuple[str, Dict]] = []
    consecutive_empty_flows = 0
    for f in range(1, max_flow + 1):
        flow_hit_before = len(results)
        consecutive_misses = 0
        for s in range(1, max_step + 1):
            key = f'e{f}s{s}'
            try:
                r = session.get(base + key, allow_redirects=False, timeout=15)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                consecutive_misses += 1
                if consecutive_misses >= 6:
                    break
                continue
            consecutive_misses = 0
            parsed = _parse_campo_pruefung_detail(r.text)
            if parsed and parsed['module_name'] not in seen_modules:
                seen_modules.add(parsed['module_name'])
                results.append((key, parsed))
        if len(results) == flow_hit_before:
            consecutive_empty_flows += 1
            if consecutive_empty_flows >= max_empty_flows:
                break
        else:
            consecutive_empty_flows = 0
    return results


def _render_campo_pruefungen_markdown(results: List[Tuple[str, Dict]]) -> str:
    from datetime import datetime as _dt
    lines = [
        "# Prüfungen & Anmeldefristen",
        "",
        f"> Auto-generated {_dt.now().strftime('%Y-%m-%d %H:%M')} by `studon-client --campo-pruefungen`.",
        "> Quelle: campo.fau.de StudyPlanner Detailansichten. Damit ein Modul hier erscheint,",
        "> muss seine Detailansicht vorher in Firefox geöffnet worden sein "
        "(`_flowExecutionKey` ist server-seitig pro Sitzung).",
        "",
        "| Modul | Typ | Semester | Von | Bis | Status |",
        "|-------|-----|----------|-----|-----|--------|",
    ]
    for _key, parsed in sorted(results, key=lambda kv: kv[1]['module_name']):
        mod = parsed['module_name']
        for p in parsed['periods']:
            lines.append(
                f"| {mod} | {p['type']} | {p['semester']} | "
                f"{p['start']} | {p['end']} | {p['status']} |"
            )
    lines.append("")
    return '\n'.join(lines)


def fetch_campo_pruefungen_markdown(output_path: Optional[str] = None) -> Optional[str]:
    """Scan Campo studyPlanner Detailansichten reachable via current Firefox
    session and write Prüfungs-Anmeldefristen to `pruefungen.md`.
    """
    print("🔄 Scanning Campo studyPlanner detail views...")
    try:
        session = requests.Session()
        session.cookies.update(browser_cookie3.firefox(domain_name='fau.de'))
        session.cookies.update(browser_cookie3.firefox(domain_name='campo.fau.de'))
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
    except Exception as e:
        print(f"❌ Could not load Firefox cookies: {e}")
        return None

    probe = session.get(CAMPO_STUDY_PLANNER_URL, allow_redirects=True, timeout=15)
    if probe.status_code != 200 or 'Studienplaner' not in probe.text:
        print("❌ Campo studyPlanner not reachable. Log into campo.fau.de in Firefox and retry.")
        return None

    results = _iter_campo_detail_pages(session)
    if not results:
        print("⚠️  No Detailansicht pages found in current Campo session.")
        print("    Open module/Prüfung Detailansichten in Firefox first, then re-run.")
        return None

    md = _render_campo_pruefungen_markdown(results)
    if output_path is None:
        output_path = os.path.join(DOWNLOAD_FOLDER, 'pruefungen.md')
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"✅ Prüfungen written to {output_path} ({len(results)} module(s))")
    return output_path


# --- CAMPO MODULPLAN (deterministic studyPlanner front-page scraper) ---

_MODULPLAN_STATUS_RE = re.compile(r'Ihr aktueller Status:\s*\|?\s*([^|]+?)(?:\s*\||\s*$)', re.IGNORECASE)
_MODULPLAN_SEM_RE = re.compile(r'Semester der Leistung:\s*\|?\s*([^|]+?)(?:\s*\||\s*$)', re.IGNORECASE)
_MODULPLAN_VERSUCH_RE = re.compile(r'Aktueller Versuch:\s*\|?\s*(\d+)', re.IGNORECASE)
_MODULPLAN_ECTS_RE = re.compile(r'\|\s*(\d+|-)\s*/\s*(\d+(?:[,.]\d+)?)\s*$')
_DETAIL_URL_RE = re.compile(r'unitId=(\d+).*?periodId=(\d+)')


def _parse_modulplan_module(item: Tag) -> Optional[Dict]:
    """Given a `<div id="...:modulePlanItem">` (the per-module container on the
    studyPlanner front page), extract structured fields. Each item corresponds
    to exactly one module slot, with its own detail-link and status block.
    Returns None if the item lacks the minimum identifiers.
    """
    text = item.get_text(' | ', strip=True)

    title_a = item.find('a', id=re.compile(r':showPopup$'))
    title_raw = title_a.get_text(' ', strip=True) if isinstance(title_a, Tag) else ''

    detail_a = item.find('a', href=re.compile(r'_flowId=detailView-flow'))
    unit_id = period_id = None
    detail_url = None
    if isinstance(detail_a, Tag):
        href = detail_a.get('href') or ''
        m = _DETAIL_URL_RE.search(str(href))
        if m:
            unit_id, period_id = m.group(1), m.group(2)
            detail_url = 'https://www.campo.fau.de' + str(href)

    nm = re.search(r'\b(\d{3,6})\s*[-–]\s*', text)
    module_nr = nm.group(1) if nm else None

    s_status = _MODULPLAN_STATUS_RE.search(text)
    s_sem = _MODULPLAN_SEM_RE.search(text)
    s_versuch = _MODULPLAN_VERSUCH_RE.search(text)
    s_ects = _MODULPLAN_ECTS_RE.search(text)
    ects_earned = ects_total = ''
    if s_ects:
        e = s_ects.group(1).strip()
        ects_earned = '' if e == '-' else e
        ects_total = s_ects.group(2).strip().replace(',', '.')

    if not module_nr and not unit_id:
        return None

    return {
        'module_nr': module_nr,
        'title': title_raw,
        'status': (s_status.group(1).strip() if s_status else '') or '',
        'semester_leistung': (s_sem.group(1).strip() if s_sem else '') or '',
        'versuch': (s_versuch.group(1) if s_versuch else '') or '',
        'ects_earned': ects_earned,
        'ects_total': ects_total,
        'unit_id': unit_id,
        'period_id': period_id,
        'detail_url': detail_url,
    }


# Studien-Soll in ECTS — default 180 (B.Sc.); überschreibbar via config.json key
# `modulplan_ects_soll` für Master (typisch 120) oder andere Studiengänge.
_MODULPLAN_ECTS_SOLL = float(_config.get('modulplan_ects_soll', 180))

_MODULPLAN_STATUS_GROUPS = [
    ('bestanden', 'Bestanden', ['bestand']),
    ('angemeldet', 'Angemeldet / Prüfung vorhanden', ['angemeldet', 'prüfung vorhanden', 'pruefung vorhanden', 'in bearbeit', 'zugelassen']),
    ('offen', 'Offen / Sonstige', []),
]


def _modulplan_group_for(status: str) -> str:
    s = (status or '').lower()
    for key, _label, needles in _MODULPLAN_STATUS_GROUPS:
        for n in needles:
            if n in s:
                return key
    return 'offen'


def _render_modulplan_markdown(study_program: str,
                                modules: List[Dict]) -> str:
    from datetime import datetime as _dt

    groups: Dict[str, List[Dict]] = {key: [] for key, _, _ in _MODULPLAN_STATUS_GROUPS}
    for m in modules:
        groups[_modulplan_group_for(m.get('status') or '')].append(m)

    def in_group_sort(m: Dict) -> tuple:
        return (m.get('semester_leistung') or 'zzz', m.get('module_nr') or '')
    for k in groups:
        groups[k].sort(key=in_group_sort)

    total_ects = 0.0
    earned_ects = 0.0
    bestanden_ects = 0.0
    listed_unit_ids: set = set()
    for m in modules:
        uid = m.get('unit_id')
        if not uid or uid in listed_unit_ids:
            continue
        listed_unit_ids.add(uid)
        ects_total_s = m.get('ects_total') or ''
        ects_earned_s = m.get('ects_earned') or ''
        try:
            if ects_total_s:
                total_ects += float(ects_total_s)
            if ects_earned_s:
                earned_ects += float(ects_earned_s)
            if ects_earned_s and 'bestand' in (m.get('status') or '').lower():
                bestanden_ects += float(ects_earned_s)
        except ValueError:
            pass

    lines = [
        f"# Modulplan — {study_program}",
        '',
        f"> Auto-generated {_dt.now().strftime('%Y-%m-%d %H:%M')} by `studon-client --modulplan`.",
        "> Quelle: campo.fau.de studyPlanner-flow Front-Page (deterministisch — kein Pre-Click nötig).",
        '',
    ]

    soll = _MODULPLAN_ECTS_SOLL
    pct = (bestanden_ects / soll * 100.0) if soll else 0.0
    gap = max(0.0, soll - bestanden_ects)
    bonus_pool = max(0.0, total_ects - soll)
    lines.extend([
        '## Studienfortschritt',
        '',
        f"- **Bestanden:** {bestanden_ects:.1f} / {soll:.0f} ECTS  ({pct:.1f} %)",
        f"- **Fehlend bis Studien-Soll:** {gap:.1f} ECTS",
        f"- **Erreicht inkl. anlaufender Prüfungen:** {earned_ects:.1f} ECTS",
        f"- **Modulplan-Summe gelistet:** {total_ects:.1f} ECTS  (Bonus-Pool über Soll: {bonus_pool:.1f} ECTS — entsteht durch Wahlpflicht-Alternativen, von denen pro Slot nur eine belegt wird)",
        '',
        f"> Das Studien-Soll ({soll:.0f} ECTS) kommt aus `config.json` (`modulplan_ects_soll`, Default 180); ECTS werden aus dem `X/Y`-Suffix jedes Modulplan-Items extrahiert. Slot-genaue Pflicht/Wahlpflicht-Auflösung ist im HTML nicht abrufbar (flache Modulliste).",
        '',
    ])

    header = ['Nr', 'Titel', 'Semester', 'Versuch', 'ECTS', 'Status']
    sep = '|' + '|'.join(['---'] * len(header)) + '|'

    for key, label, _ in _MODULPLAN_STATUS_GROUPS:
        bucket = groups.get(key) or []
        if not bucket:
            continue
        bucket_ects = 0.0
        bucket_uids: set = set()
        for m in bucket:
            uid = m.get('unit_id')
            if uid and uid not in bucket_uids:
                bucket_uids.add(uid)
                try:
                    if key == 'bestanden':
                        bucket_ects += float(m.get('ects_earned') or 0)
                    else:
                        bucket_ects += float(m.get('ects_total') or 0)
                except ValueError:
                    pass
        lines.append(f'## {label} ({len(bucket)} Module · {bucket_ects:.1f} ECTS)')
        lines.append('')
        lines.append('| ' + ' | '.join(header) + ' |')
        lines.append(sep)
        for m in bucket:
            ects_total_s = m.get('ects_total') or ''
            ects_earned_s = m.get('ects_earned') or ''
            ects_cell = f"{ects_earned_s or '–'}/{ects_total_s}" if ects_total_s else ''
            row = [
                m.get('module_nr') or '',
                (m.get('title') or '').replace('|', '/'),
                m.get('semester_leistung') or '',
                m.get('versuch') or '',
                ects_cell,
                m.get('status') or '',
            ]
            lines.append('| ' + ' | '.join(row) + ' |')
        lines.append('')

    return '\n'.join(lines)


def _fetch_modulplan_data(session: requests.Session) -> Optional[Tuple[str, List[Dict]]]:
    """Fetch + parse the studyPlanner front page. Returns (study_program, modules) or None."""
    try:
        r = session.get(CAMPO_STUDY_PLANNER_URL, allow_redirects=True, timeout=20)
    except requests.RequestException as e:
        print(f"❌ studyPlanner not reachable: {e}")
        return None
    if r.status_code != 200 or 'Studienplaner' not in r.text:
        print("❌ Campo studyPlanner not reachable. Log into campo.fau.de in Firefox and retry.")
        return None
    soup = BeautifulSoup(r.text, 'html.parser')
    title_tag = soup.find('h1')
    study_program = title_tag.get_text(' ', strip=True) if title_tag else 'Modulplan'
    study_program = study_program.replace('Studienplaner mit Modulplan', '').strip() or 'Modulplan'
    modules: List[Dict] = []
    seen_keys: set = set()
    for item in soup.find_all('div', id=re.compile(r':modulePlanItem$')):
        if not isinstance(item, Tag):
            continue
        parsed = _parse_modulplan_module(item)
        if not parsed:
            continue
        key = (parsed.get('unit_id'), parsed.get('module_nr'))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        modules.append(parsed)
    return study_program, modules


def fetch_campo_modulplan(output_path: Optional[str] = None) -> Optional[str]:
    """Scan the campo studyPlanner-flow front page and write a per-module
    Modulplan markdown with status, Versuch, Semester der Leistung, and
    ECTS (earned/total) — all extracted from the deterministic front-page HTML.
    """
    print("🔄 Scanning campo studyPlanner front page (deterministic)...")
    session = _campo_session()
    if session is None:
        return None
    data = _fetch_modulplan_data(session)
    if data is None:
        return None
    study_program, modules = data
    if not modules:
        print("⚠️  No modules parsed from studyPlanner front page.")
        return None
    print(f"✅ Parsed {len(modules)} module(s) from Modulplan.")

    md = _render_modulplan_markdown(study_program, modules)
    if output_path is None:
        output_path = os.path.join(DOWNLOAD_FOLDER, 'Modulplan.md')
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"✅ Modulplan written to {output_path}")
    return output_path


# --- CAMPO BELEGUNGEN (deterministic searchOwnEnrollmentInfo-flow scraper) ---

_BELEGUNG_GROUP_ID_RE = re.compile(r':unit-Belegung(\d+):tableGroup$')
_LV_TYPE_PREFIXES = ('Vorlesung mit Übung ', 'Vorlesung ', 'Übung ', 'Seminar ', 'Praktikum ', 'Tutorium ', 'Kolloquium ', 'Projekt ')
_WEEKDAY_RE = re.compile(r'\b(?:jeden\s+)?(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b')
_NAME_TILL_WEEKDAY_RE = re.compile(r'^(.+?)(?=\s+(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag|jeden\b|Keine Uhrzeit|Ihr aktueller Status:|Semester der Leistung:|$))', re.DOTALL)
_STATUS_LINE_RE = re.compile(r'Ihr aktueller Status:\s*(.+?)(?=\s*(?:Semester der Leistung:|Aktueller Versuch:|$))', re.IGNORECASE | re.DOTALL)
_SEMESTER_LINE_RE = re.compile(r'Semester der Leistung:\s*(.+?)(?=\s*(?:Aktueller Versuch:|$))', re.IGNORECASE | re.DOTALL)
_VERSUCH_LINE_RE = re.compile(r'Aktueller Versuch:\s*(\d+)', re.IGNORECASE)
_PRUEFFORM_RE = re.compile(r'Prüfungsform:\s*(.+?)(?=\s+(?:Prüfer/-in:|Dozent/-in:|Ihr aktueller Status:|$))', re.IGNORECASE | re.DOTALL)


def _ws(s: str) -> str:
    return ' '.join(s.split())


def _split_personen(text: str, label: str) -> List[str]:
    """Find all occurrences of `<label>: <name>` and return names trimmed to the
    next weekday/label boundary, deduped while preserving order."""
    parts = re.split(rf'{re.escape(label)}\s*:?\s*', text)
    out: List[str] = []
    seen: set = set()
    for seg in parts[1:]:
        m = _NAME_TILL_WEEKDAY_RE.match(seg)
        name = _ws(m.group(1) if m else seg.split(' Ihr aktueller')[0])
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _extract_termine(info_text: str, title: str) -> str:
    """Strip the Parallelgruppe header and label-spans, leaving only the Termin
    description(s). Multiple Termine are joined with `<br>` so they render
    cleanly inside a markdown table cell."""
    # Drop "N. Parallelgruppe <title-echo>" prefix
    t = re.sub(r'^\s*\d+\.\s*Parallelgruppe\s*', '', info_text)
    # Drop the title echo if it appears once at the start
    if title and t.startswith(title):
        t = t[len(title):].strip()
    # Cut off everything past status/semester labels
    cut = re.search(r'\s+(?:Ihr aktueller Status:|Semester der Leistung:|Aktueller Versuch:)', t)
    if cut:
        t = t[:cut.start()]
    # Drop Prüfungsform/Prüfer/Dozent labels and their values (already captured separately)
    for label in ('Prüfungsform:', 'Prüfer/-in:', 'Dozent/-in:'):
        # Strip "<label> <value-until-next-weekday>" segments
        t = re.sub(rf'\s*{re.escape(label)}\s*.+?(?=\s+(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag|jeden\b|$))', '', t, flags=re.DOTALL)
        # And trailing form-name only (no weekday follows)
        t = re.sub(rf'\s*{re.escape(label)}\s*[^|]*$', '', t)
    t = _ws(t)
    # Capture each Termin starting at optional "jeden" + Weekday, running until the next such anchor
    termin_re = re.compile(
        r'(?:jeden\s+)?(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b'
        r'.*?(?=(?:\s|^)(?:jeden\s+)?(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b|$)',
        re.DOTALL,
    )
    chunks = [_ws(m) for m in termin_re.findall(t) if m.strip()]
    if len(chunks) > 1:
        return '<br>'.join(chunks)
    return t


def _parse_belegung_group(group: Tag) -> Optional[Dict]:
    """Parse one `div.dataTableTableGroup` block from the Belegungen page."""
    h2 = group.find('h2')
    if not isinstance(h2, Tag):
        return None
    heading = h2.get_text(' ', strip=True)
    if ':' not in heading:
        return None
    kind_raw, _, title = heading.partition(':')
    kind = kind_raw.strip().lower()
    title = title.strip()

    lv_type = ''
    pruef_nr = ''
    if kind == 'prüfung':
        m = re.match(r'(\d{4,6})\s+(.*)', title)
        if m:
            pruef_nr = m.group(1)
            title = m.group(2).strip()
    else:  # veranstaltung
        for prefix in _LV_TYPE_PREFIXES:
            if title.startswith(prefix):
                lv_type = prefix.strip()
                title = title[len(prefix):].strip()
                break

    table = group.find('table', class_='belegungen') or group.find('table')
    if not isinstance(table, Tag):
        return None
    body = table.find('tbody')
    if not isinstance(body, Tag):
        return None
    rows = body.find_all('tr', recursive=False)
    if not rows:
        return None

    parallelgruppen: List[Dict] = []
    for row in rows:
        if not isinstance(row, Tag):
            continue
        cells = row.find_all('td', recursive=False)
        if len(cells) < 2:
            continue
        info_text = cells[0].get_text(' ', strip=True)
        status_text = cells[1].get_text(' ', strip=True)

        m_form = _PRUEFFORM_RE.search(info_text)
        m_status = _STATUS_LINE_RE.search(status_text)
        m_sem = _SEMESTER_LINE_RE.search(status_text)
        m_versuch = _VERSUCH_LINE_RE.search(status_text)

        pg = {
            'termine': _extract_termine(info_text, title),
            'pruefungsform': _ws(m_form.group(1)) if m_form else '',
            'pruefer': ', '.join(_split_personen(info_text, 'Prüfer/-in')),
            'dozent': ', '.join(_split_personen(info_text, 'Dozent/-in')),
            'status': _ws(m_status.group(1)) if m_status else '',
            'semester': _ws(m_sem.group(1)) if m_sem else '',
            'versuch': m_versuch.group(1) if m_versuch else '',
        }
        parallelgruppen.append(pg)

    return {
        'kind': kind,
        'title': title,
        'lv_type': lv_type,
        'pruef_nr': pruef_nr,
        'parallelgruppen': parallelgruppen,
    }


def _render_belegungen_markdown(term_label: str, blocks: List[Dict]) -> str:
    from datetime import datetime as _dt
    pruefungen = [b for b in blocks if b.get('kind') == 'prüfung']
    veranstaltungen = [b for b in blocks if b.get('kind') == 'veranstaltung']

    lines = [
        f"# Belegungen — {term_label}",
        '',
        f"> Auto-generated {_dt.now().strftime('%Y-%m-%d %H:%M')} by `studon-client --belegungen`.",
        "> Quelle: campo.fau.de searchOwnEnrollmentInfo-flow (deterministisch — kein Pre-Click nötig).",
        "> Beleg im Streitfall, dass eine Anmeldung systemseitig durchging.",
        '',
        f"## Prüfungen ({len(pruefungen)})",
        '',
    ]
    if pruefungen:
        lines.append('| Nr | Titel | Termin | Form | Prüfer/-in | Status | Semester | Versuch |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for b in sorted(pruefungen, key=lambda x: x.get('pruef_nr') or ''):
            pg = b['parallelgruppen'][0] if b['parallelgruppen'] else {}
            lines.append('| ' + ' | '.join([
                b.get('pruef_nr') or '',
                (b.get('title') or '').replace('|', '/'),
                (pg.get('termine') or '').replace('|', '/'),
                (pg.get('pruefungsform') or '').replace('|', '/'),
                (pg.get('pruefer') or '').replace('|', '/'),
                (pg.get('status') or '').replace('|', '/'),
                (pg.get('semester') or '').replace('|', '/'),
                (pg.get('versuch') or '').replace('|', '/'),
            ]) + ' |')
    else:
        lines.append('_keine_')

    lines.extend(['', f"## Veranstaltungen ({len(veranstaltungen)})", ''])
    if veranstaltungen:
        lines.append('| Typ | Titel | Termin / Raum | Dozent/-in | Status |')
        lines.append('|---|---|---|---|---|')
        for b in sorted(veranstaltungen, key=lambda x: (x.get('title') or '').lower()):
            pg = b['parallelgruppen'][0] if b['parallelgruppen'] else {}
            lines.append('| ' + ' | '.join([
                (b.get('lv_type') or '').replace('|', '/'),
                (b.get('title') or '').replace('|', '/'),
                (pg.get('termine') or '').replace('|', '/'),
                (pg.get('dozent') or '').replace('|', '/'),
                (pg.get('status') or '').replace('|', '/'),
            ]) + ' |')
    else:
        lines.append('_keine_')

    lines.append('')
    return '\n'.join(lines)


def _fetch_belegungen_data(session: requests.Session) -> Optional[Tuple[str, List[Dict]]]:
    """Fetch + parse the Belegungen page. Returns (term_label, blocks) or None."""
    try:
        r = session.get(CAMPO_BELEGUNGEN_URL, allow_redirects=True, timeout=20)
    except requests.RequestException as e:
        print(f"❌ Belegungen not reachable: {e}")
        return None
    if r.status_code != 200 or 'Belegungen' not in r.text:
        print("❌ Campo Belegungen not reachable. Log into campo.fau.de in Firefox and retry.")
        return None
    soup = BeautifulSoup(r.text, 'html.parser')
    term_label = ''
    for sel in soup.find_all('select'):
        if not isinstance(sel, Tag):
            continue
        sid = str(sel.get('id') or '')
        if 'termPeriod' in sid:
            opt = sel.find('option', selected=True)
            if isinstance(opt, Tag):
                term_label = opt.get_text(' ', strip=True)
            break
    if not term_label:
        term_label = 'Aktuelles Semester'
    blocks: List[Dict] = []
    for group in soup.find_all('div', class_='dataTableTableGroup'):
        if not isinstance(group, Tag):
            continue
        gid = str(group.get('id') or '')
        if not _BELEGUNG_GROUP_ID_RE.search(gid):
            continue
        parsed = _parse_belegung_group(group)
        if parsed:
            blocks.append(parsed)
    return term_label, blocks


def fetch_campo_belegungen(output_path: Optional[str] = None) -> Optional[str]:
    """Scrape the campo Belegungen page (searchOwnEnrollmentInfo-flow) and write
    a per-Belegung markdown + structured JSON of angemeldete Prüfungen +
    Veranstaltungen for the currently selected semester. Pure data fetch — any
    diff/notify behaviour lives in the sibling `belegungen-watcher` tool that
    consumes Belegungen.json.
    """
    print("🔄 Scanning campo Belegungen page (deterministic)...")
    session = _campo_session()
    if session is None:
        return None
    data = _fetch_belegungen_data(session)
    if data is None:
        return None
    term_label, blocks = data
    if not blocks:
        print("⚠️  No Belegungen-Blöcke gefunden.")
        return None
    print(f"✅ Parsed {len(blocks)} Belegungs-Block(s) ({sum(1 for b in blocks if b['kind']=='prüfung')} Prüfungen + {sum(1 for b in blocks if b['kind']=='veranstaltung')} Veranstaltungen).")

    md = _render_belegungen_markdown(term_label, blocks)
    if output_path is None:
        output_path = os.path.join(DOWNLOAD_FOLDER, 'Belegungen.md')
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"✅ Belegungen written to {output_path}")

    # Maschinen-lesbarer JSON-Export — konsumiert vom belegungen-watcher Tool
    from datetime import datetime as _dt
    json_path = os.path.splitext(output_path)[0] + '.json'
    payload = {
        'generated_at': _dt.now().isoformat(timespec='seconds'),
        'term_label': term_label,
        'blocks': blocks,
    }
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"✅ Belegungen JSON written to {json_path}")
    except OSError as e:
        print(f"⚠️  Could not write JSON export: {e}")
    return output_path


# --- PDF: NOTENÜBERSICHT PARSER (Prüfungsamt-kanonisch) ---

_PDF_ROW_RE = re.compile(
    r'^\s*(\d{4,6})\s+(.+?)\s{2,}(?:(\d{2}\.\d{2}\.\d{4}))?\s*(?:([\d,]+))?\s+(bestanden|BE|EB|AR)\s+([\d,]+)\s*$'
)
_PDF_TOTAL_RE = re.compile(r'(\d+(?:,\d+)?)\s+ECTS\s+von\s+insgesamt\s+(\d+)')


def _find_latest_notenuebersicht_pdf() -> Optional[str]:
    """Return the path to the most recently mtime'd 'Notenübersicht*Module*.pdf'
    under <downloads>/Bescheinigungen/, preferring the German bestandene-Module
    variant. Returns None if no candidate exists."""
    bescheinigungen = os.path.join(DOWNLOAD_FOLDER, 'Bescheinigungen')
    if not os.path.isdir(bescheinigungen):
        return None
    candidates: List[Tuple[float, int, str]] = []
    for name in os.listdir(bescheinigungen):
        if not name.lower().endswith('.pdf'):
            continue
        if 'notenübersicht' not in name.lower() and 'notenubersicht' not in name.lower():
            continue
        if 'englisch' in name.lower():
            continue
        priority = 0  # higher = preferred
        if 'bestandene module' in name.lower():
            priority = 2
        elif 'bestandene leistungen' in name.lower():
            priority = 1
        path = os.path.join(bescheinigungen, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        candidates.append((mtime, priority, path))
    if not candidates:
        return None
    # Sort by (priority desc, mtime desc) — preferred variant first, then newest
    candidates.sort(key=lambda t: (-t[1], -t[0]))
    return candidates[0][2]


def _parse_notenuebersicht_pdf(pdf_path: str) -> Optional[Dict]:
    """Run pdftotext -layout and parse the resulting text into module rows +
    overall ECTS total. Returns {modules: [...], total_earned, total_soll,
    pdf_path, pdf_mtime_iso, generation_date} or None on failure."""
    try:
        out = subprocess.run(
            ['pdftotext', '-layout', pdf_path, '-'],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"⚠️  pdftotext failed for {os.path.basename(pdf_path)}: {e}")
        return None
    if out.returncode != 0:
        print(f"⚠️  pdftotext exit {out.returncode} for {os.path.basename(pdf_path)}")
        return None

    modules: List[Dict] = []
    for line in out.stdout.splitlines():
        m = _PDF_ROW_RE.match(line)
        if not m:
            continue
        prnr, title, date, note, status, ects = m.groups()
        # Skip the synthetic "10000 Bachelorprüfung" summary row
        if prnr == '10000':
            continue
        modules.append({
            'module_nr': prnr,
            'title': title.strip(),
            'pruef_datum': date or '',
            'note': (note or '').replace(',', '.'),
            'status': status,
            'ects': ects.replace(',', '.'),
        })

    total_earned = total_soll = ''
    m_total = _PDF_TOTAL_RE.search(out.stdout)
    if m_total:
        total_earned = m_total.group(1).replace(',', '.')
        total_soll = m_total.group(2)

    if not modules:
        return None

    from datetime import datetime as _dt
    return {
        'modules': modules,
        'total_earned': total_earned,
        'total_soll': total_soll,
        'pdf_path': pdf_path,
        'pdf_basename': os.path.basename(pdf_path),
        'pdf_mtime_iso': _dt.fromtimestamp(os.path.getmtime(pdf_path)).isoformat(timespec='seconds'),
    }


# --- J: BELEGUNGEN <-> MODULPLAN RECONCILIATION ---

_RECONCILE_PRAKTIKUM_RE = re.compile(r'^praktikum:?\s*', re.IGNORECASE)


def _reconcile_normalize(title: str) -> str:
    """Lowercase, strip 'Praktikum:' prefix, drop punctuation, collapse ws."""
    s = (title or '').lower()
    s = _RECONCILE_PRAKTIKUM_RE.sub('', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _reconcile_match(title: str, index: Dict[str, Dict], threshold: float = 0.6) -> Tuple[Optional[Dict], float]:
    """Find best match for `title` in `index` (keyed by normalized title).
    Returns (matched_record_or_None, jaccard_score). Exact match → score 1.0."""
    n = _reconcile_normalize(title)
    if not n:
        return None, 0.0
    if n in index:
        return index[n], 1.0
    toks = set(n.split())
    if not toks:
        return None, 0.0
    best: Optional[Dict] = None
    best_score = 0.0
    for k, rec in index.items():
        k_toks = set(k.split())
        if not k_toks:
            continue
        overlap = len(toks & k_toks)
        union = len(toks | k_toks)
        score = overlap / union
        if score > best_score:
            best_score = score
            best = rec
    if best_score >= threshold:
        return best, best_score
    return None, best_score


def _reconcile_data(modules: List[Dict], blocks: List[Dict], pdf_data: Optional[Dict] = None) -> Dict:
    """Compute the cross-check between Modulplan, Belegungen, and optionally the
    Prüfungsamt-PDF Notenübersicht. If pdf_data is given, the PDF is treated as
    canonical for bestanden-ECTS (signed by Prüfungsamt; BAföG-relevant)."""
    pruefungen = [b for b in blocks if b.get('kind') == 'prüfung']
    mp_index = {_reconcile_normalize(m.get('title', '')): m for m in modules if m.get('title')}
    bg_index: Dict[str, List[Dict]] = {}
    for b in pruefungen:
        n = _reconcile_normalize(b.get('title', ''))
        bg_index.setdefault(n, []).append(b)

    # Section 1: each Belegungen-Prüfung → Modulplan-Modul
    pruefung_to_modul: List[Dict] = []
    for b in pruefungen:
        match, score = _reconcile_match(b.get('title', ''), mp_index)
        pruefung_to_modul.append({
            'pruef_nr': b.get('pruef_nr', ''),
            'pruef_title': b.get('title', ''),
            'modul_nr': (match.get('module_nr') if match else '') or '',
            'modul_title': (match.get('title') if match else '') or '',
            'modul_status': (match.get('status') if match else '') or '',
            'modul_ects_total': (match.get('ects_total') if match else '') or '',
            'match_score': score,
        })

    # Section 2: Modulplan-Angemeldet/Prüfung-Vorhanden ohne Belegungs-Match
    modul_angemeldet_ohne_belegung: List[Dict] = []
    for m in modules:
        status = (m.get('status') or '').lower()
        if not ('prüfung vorhanden' in status or 'pruefung vorhanden' in status or 'angemeldet' in status):
            continue
        n = _reconcile_normalize(m.get('title', ''))
        if any(_reconcile_normalize(b.get('title', '')) == n for b in pruefungen):
            continue
        # Fuzzy fallback: skip if any Belegung scores ≥0.6 against this Modul
        toks = set(n.split())
        if toks and any(
            len(toks & set(_reconcile_normalize(b.get('title', '')).split())) / max(len(toks | set(_reconcile_normalize(b.get('title', '')).split())), 1) >= 0.6
            for b in pruefungen
        ):
            continue
        modul_angemeldet_ohne_belegung.append(m)

    # Section 3: Status=bestanden aber ects_earned leer (= ECTS-undercount)
    bestanden_ohne_ects: List[Dict] = []
    for m in modules:
        if 'bestand' in (m.get('status') or '').lower() and not (m.get('ects_earned') or '').strip():
            bestanden_ohne_ects.append(m)

    # ECTS-Bilanz korrigiert
    base_bestanden = 0.0
    listed_unit_ids: set = set()
    for m in modules:
        uid = m.get('unit_id')
        if not uid or uid in listed_unit_ids:
            continue
        listed_unit_ids.add(uid)
        if 'bestand' in (m.get('status') or '').lower() and (m.get('ects_earned') or '').strip():
            try:
                base_bestanden += float(m['ects_earned'])
            except ValueError:
                pass
    corrected_min = base_bestanden
    corrected_max = base_bestanden
    for m in bestanden_ohne_ects:
        try:
            corrected_max += float(m.get('ects_total') or 0)
        except ValueError:
            pass

    # Optional: PDF-Cross-Check
    pdf_section: Optional[Dict] = None
    if pdf_data:
        # Map Modulplan modules by Modul-Nr for fast lookup
        mp_by_nr = {m.get('module_nr'): m for m in modules if m.get('module_nr')}
        pdf_rows: List[Dict] = []
        pdf_total = 0.0
        for pr in pdf_data['modules']:
            nr = pr['module_nr']
            try:
                ects = float(pr['ects'])
            except ValueError:
                ects = 0.0
            pdf_total += ects
            mp_match = mp_by_nr.get(nr)
            mp_ects_earned = ''
            mp_status = ''
            if mp_match:
                mp_ects_earned = mp_match.get('ects_earned') or ''
                mp_status = mp_match.get('status') or ''
            # Diskrepanz: PDF zeigt ECTS, Modulplan-Front-Page liefert leeren oder anderen Wert
            try:
                mp_e = float(mp_ects_earned) if mp_ects_earned else None
            except ValueError:
                mp_e = None
            gap = None if mp_e is None else round(ects - mp_e, 2)
            pdf_rows.append({
                'module_nr': nr,
                'title': pr['title'],
                'pruef_datum': pr['pruef_datum'],
                'note': pr['note'],
                'ects': ects,
                'mp_in_modulplan': mp_match is not None,
                'mp_status': mp_status,
                'mp_ects_earned': mp_ects_earned,
                'gap': gap,  # None if no MP match or MP-suffix empty
            })
        # Modules in PDF but not in Modulplan (shouldn't happen, but report)
        pdf_only = [r for r in pdf_rows if not r['mp_in_modulplan']]
        # Modules in PDF where Modulplan undercounts (gap > 0 or MP-suffix empty)
        gap_rows = [r for r in pdf_rows if (r['gap'] is None and r['ects'] > 0 and r['mp_in_modulplan']) or (r['gap'] is not None and r['gap'] != 0)]
        try:
            pdf_soll = float(pdf_data['total_soll']) if pdf_data['total_soll'] else _MODULPLAN_ECTS_SOLL
        except ValueError:
            pdf_soll = _MODULPLAN_ECTS_SOLL
        try:
            pdf_total_declared = float(pdf_data['total_earned']) if pdf_data['total_earned'] else pdf_total
        except ValueError:
            pdf_total_declared = pdf_total
        pdf_section = {
            'rows': pdf_rows,
            'pdf_only': pdf_only,
            'gap_rows': gap_rows,
            'pdf_total_summed': pdf_total,
            'pdf_total_declared': pdf_total_declared,
            'pdf_soll': pdf_soll,
            'pdf_basename': pdf_data['pdf_basename'],
            'pdf_mtime_iso': pdf_data['pdf_mtime_iso'],
            'mp_undercount': round(pdf_total_declared - base_bestanden, 2),
        }

    return {
        'pruefung_to_modul': pruefung_to_modul,
        'modul_angemeldet_ohne_belegung': modul_angemeldet_ohne_belegung,
        'bestanden_ohne_ects': bestanden_ohne_ects,
        'ects_summary': {
            'base_bestanden': base_bestanden,
            'corrected_min': corrected_min,
            'corrected_max': corrected_max,
            'soll': _MODULPLAN_ECTS_SOLL,
        },
        'pdf_section': pdf_section,
    }


def _render_reconciliation_markdown(study_program: str, term_label: str, recon: Dict) -> str:
    from datetime import datetime as _dt
    lines = [
        f"# Reconciliation — {study_program} ({term_label})",
        '',
        f"> Auto-generated {_dt.now().strftime('%Y-%m-%d %H:%M')} by `studon-client --reconcile`.",
        "> Cross-Check zwischen `Modulplan.md` (Studienplan-Sicht) und `Belegungen.md` (angemeldete Prüfungen für das laufende Semester).",
        '',
    ]

    es = recon['ects_summary']
    pdf = recon.get('pdf_section')

    if pdf:
        lines.extend([
            '## ECTS-Bilanz (Prüfungsamt-PDF = kanonisch)',
            '',
            f"- **Bestanden lt. PDF:** {pdf['pdf_total_declared']:.1f} / {pdf['pdf_soll']:.0f} ECTS  ({pdf['pdf_total_declared']/pdf['pdf_soll']*100:.1f} %)",
            f"- **Modulplan-Front-Page zählt:** {es['base_bestanden']:.1f} ECTS  (Undercount: {pdf['mp_undercount']:+.1f} ECTS)",
            f"- **Quelle:** `Bescheinigungen/{pdf['pdf_basename']}`  (mtime {pdf['pdf_mtime_iso']})",
            '',
            "> Die PDF-Zahl ist die offizielle Prüfungsamt-Berechnung — relevant für BAföG §48, Bundeskindergeld, Stipendien. Bei Diskrepanz zum Modulplan-Front-Page gewinnt die PDF.",
            "> PDF wird nicht automatisch aktualisiert — frische Zahl via `--campo-bescheinigungen`.",
            '',
        ])
    else:
        lines.extend([
            '## ECTS-Bilanz korrigiert (ohne PDF — schätzt nur ab Modulplan-Front-Page)',
            '',
            f"- **Aus `X/Y`-Suffix gezählt (Modulplan-Front-Page):** {es['base_bestanden']:.1f} ECTS",
            f"- **Untergrenze nach Korrektur:** {es['corrected_min']:.1f} ECTS  *(identisch — keine Korrektur möglich ohne ECTS-Soll je bestandenem Modul ohne X/Y-Suffix)*",
            f"- **Obergrenze nach Korrektur:** {es['corrected_max']:.1f} ECTS  *(Wenn jedes Bestanden-ohne-ECTS Modul mit seinem listed-Soll zählt)*",
            f"- **Studien-Soll:** {es['soll']:.0f} ECTS",
            '',
            "> Hinweis: für die offizielle Zahl wäre die Prüfungsamt-PDF kanonisch. `--campo-bescheinigungen` einmal ausführen, dann liest `--reconcile` die Zahl automatisch.",
            '',
        ])

    lines.append(f"## Belegungen → Modulplan ({len(recon['pruefung_to_modul'])})")
    lines.append('')
    lines.append('| Prüfungs-Nr | Prüfungs-Titel | Modul-Nr | Modul-Titel | Modul-Status | Modul-ECTS-Soll | Match |')
    lines.append('|---|---|---|---|---|---|---|')
    for r in recon['pruefung_to_modul']:
        match_marker = '✅' if r['match_score'] >= 1.0 else (f"≈ {r['match_score']:.0%}" if r['match_score'] >= 0.6 else '❌')
        lines.append('| ' + ' | '.join([
            r['pruef_nr'],
            (r['pruef_title'] or '').replace('|', '/'),
            r['modul_nr'],
            (r['modul_title'] or '').replace('|', '/'),
            r['modul_status'],
            r['modul_ects_total'],
            match_marker,
        ]) + ' |')
    lines.append('')

    sec2 = recon['modul_angemeldet_ohne_belegung']
    lines.append(f"## Modulplan-Angemeldet ohne Belegungs-Match ({len(sec2)})")
    lines.append('')
    if sec2:
        lines.append('> Modulplan zeigt „Prüfung Vorhanden" / „angemeldet", aber `Belegungen.md` listet *keine* zugehörige Prüfung. Mögliche Gründe: Anmeldung steht noch aus, Praktikums-/Übungsleistung ohne separate Prüfung, oder administrative Sondermodule (Zusatzleistungen, Schlüsselqualifikationen).')
        lines.append('')
        lines.append('| Modul-Nr | Titel | Status | ECTS-Soll | Semester |')
        lines.append('|---|---|---|---|---|')
        for m in sec2:
            lines.append('| ' + ' | '.join([
                m.get('module_nr') or '',
                (m.get('title') or '').replace('|', '/'),
                m.get('status') or '',
                m.get('ects_total') or '',
                m.get('semester_leistung') or '',
            ]) + ' |')
    else:
        lines.append('_alle Modulplan-Angemeldungen haben Belegungs-Match_')
    lines.append('')

    sec3 = recon['bestanden_ohne_ects']
    lines.append(f"## Bestanden ohne ECTS-Suffix ({len(sec3)})")
    lines.append('')
    if sec3:
        lines.append('> Modulplan-Status = „bestanden", aber das `X/Y`-Suffix auf der Front-Page ist leer. Diese Module fehlen daher in der gemessenen ECTS-Summe und erklären die `lernplan.md` / `Prüfungen.md` Diskrepanz.')
        if pdf:
            lines.append('> Die echten ECTS-Werte stehen im PDF-Section weiter unten.')
        lines.append('')
        lines.append('| Modul-Nr | Titel | Listed-Soll | Semester |')
        lines.append('|---|---|---|---|')
        for m in sec3:
            lines.append('| ' + ' | '.join([
                m.get('module_nr') or '',
                (m.get('title') or '').replace('|', '/'),
                m.get('ects_total') or '',
                m.get('semester_leistung') or '',
            ]) + ' |')
    else:
        lines.append('_keine Bestanden-Module ohne ECTS-Suffix_')
    lines.append('')

    if pdf:
        lines.append(f"## Prüfungsamt-PDF Inhalt ({len(pdf['rows'])} Module)")
        lines.append('')
        lines.append('| PrNr | Titel | Prüf.-Datum | Note | ECTS-PDF | Modulplan-Status | Modulplan-ECTS | Gap |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for r in pdf['rows']:
            gap_cell = '—' if r['gap'] is None else (f"+{r['gap']:.1f}" if r['gap'] > 0 else f"{r['gap']:.1f}")
            mp_status = r['mp_status'] if r['mp_in_modulplan'] else '⚠️ nicht im Modulplan'
            lines.append('| ' + ' | '.join([
                r['module_nr'],
                (r['title'] or '').replace('|', '/'),
                r['pruef_datum'],
                r['note'] or '—',
                f"{r['ects']:.1f}",
                mp_status,
                r['mp_ects_earned'] or '—',
                gap_cell,
            ]) + ' |')
        lines.append('')

        if pdf['gap_rows']:
            lines.append(f"### Lücken die der Modulplan unterläuft ({len(pdf['gap_rows'])})")
            lines.append('')
            lines.append('> Module wo PDF eine ECTS-Zahl hat aber das Modulplan-`X/Y`-Suffix leer ist (oder abweicht). Diese Zeilen sind die *konkrete Auflösung* der ECTS-Diskrepanz.')
            lines.append('')
            for r in pdf['gap_rows']:
                lines.append(f"- **{r['module_nr']} {r['title']}** — PDF: {r['ects']:.1f} ECTS · Modulplan: {r['mp_ects_earned'] or '(leer)'}")
            lines.append('')

        if pdf['pdf_only']:
            lines.append(f"### PDF-Module ohne Modulplan-Eintrag ({len(pdf['pdf_only'])})")
            lines.append('')
            for r in pdf['pdf_only']:
                lines.append(f"- {r['module_nr']} {r['title']} ({r['ects']:.1f} ECTS)")
            lines.append('')

    return '\n'.join(lines)


def fetch_campo_reconciliation(output_path: Optional[str] = None) -> Optional[str]:
    """Fetch Modulplan + Belegungen in one go and write Reconciliation.md
    with the cross-check that resolves the lernplan ↔ Prüfungen ECTS gap."""
    print("🔄 Reconciling Modulplan ↔ Belegungen ...")
    session = _campo_session()
    if session is None:
        return None
    mp = _fetch_modulplan_data(session)
    if mp is None:
        return None
    study_program, modules = mp
    if not modules:
        print("⚠️  No modules parsed from Modulplan.")
        return None
    bg = _fetch_belegungen_data(session)
    if bg is None:
        return None
    term_label, blocks = bg
    if not blocks:
        print("⚠️  No Belegungen-Blöcke gefunden.")
        return None

    pdf_data: Optional[Dict] = None
    pdf_path = _find_latest_notenuebersicht_pdf()
    if pdf_path:
        print(f"📄 Notenübersicht-PDF gefunden: {os.path.basename(pdf_path)}")
        pdf_data = _parse_notenuebersicht_pdf(pdf_path)
        if pdf_data:
            print(f"   → {len(pdf_data['modules'])} Modul(e) aus PDF · Total {pdf_data['total_earned']}/{pdf_data['total_soll']} ECTS")
    else:
        print("ℹ️  Keine Notenübersicht-PDF unter Bescheinigungen/ — für kanonische ECTS-Zahl: `--campo-bescheinigungen` einmal laufen lassen.")

    recon = _reconcile_data(modules, blocks, pdf_data=pdf_data)
    print(f"✅ Reconciled {len(modules)} Module ↔ {sum(1 for b in blocks if b['kind']=='prüfung')} Prüfungen.")
    print(f"   - Belegungen→Modul matches: {sum(1 for r in recon['pruefung_to_modul'] if r['match_score']>=1.0)} exakt + {sum(1 for r in recon['pruefung_to_modul'] if 0.6<=r['match_score']<1.0)} fuzzy + {sum(1 for r in recon['pruefung_to_modul'] if r['match_score']<0.6)} ohne Match")
    print(f"   - Modulplan-Angemeldet ohne Belegung: {len(recon['modul_angemeldet_ohne_belegung'])}")
    print(f"   - Bestanden ohne ECTS-Suffix: {len(recon['bestanden_ohne_ects'])}")
    es = recon['ects_summary']
    print(f"   - ECTS bestanden (Modulplan): {es['base_bestanden']:.1f}; (PDF kanonisch): {recon['pdf_section']['pdf_total_declared'] if recon['pdf_section'] else 'n/a'} / Soll {es['soll']:.0f}")

    md = _render_reconciliation_markdown(study_program, term_label, recon)
    if output_path is None:
        output_path = os.path.join(DOWNLOAD_FOLDER, 'Reconciliation.md')
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"✅ Reconciliation written to {output_path}")

    # Maschinen-lesbarer JSON-Export für nachgelagerte Tools (Digest, Lernplan, …)
    from datetime import datetime as _dt
    json_path = os.path.splitext(output_path)[0] + '.json'
    payload = {
        'generated_at': _dt.now().isoformat(timespec='seconds'),
        'study_program': study_program,
        'term_label': term_label,
        'reconciliation': recon,
        'modules': modules,
        'belegungen': blocks,
    }
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"✅ Reconciliation JSON written to {json_path}")
    except OSError as e:
        print(f"⚠️  Could not write JSON export: {e}")

    return output_path


def _campo_session() -> Optional[requests.Session]:
    """Build a requests.Session populated with current Firefox cookies for campo."""
    try:
        s = requests.Session()
        s.cookies.update(browser_cookie3.firefox(domain_name='fau.de'))
        s.cookies.update(browser_cookie3.firefox(domain_name='campo.fau.de'))
        s.headers.update({'User-Agent': 'Mozilla/5.0'})
        return s
    except Exception as e:
        print(f"❌ Could not load Firefox cookies: {e}")
        return None


def _run_campo_search(query: str, term: Optional[str] = None) -> None:
    """Search campo's Lehrveranstaltungssuche for *query* and print hits + ECTS.

    Delegates the flow mechanics to the campo_search module (imported lazily so
    the --advertise fast-path stays light) and reuses _campo_session() so cookie
    handling lives in one place. ECTS is always fetched per hit (one extra GET
    each). Stdout only — nothing is written to disk.
    """
    from campo_search import search_courses, fetch_detail_ects

    session = _campo_session()
    if session is None:
        return
    try:
        term_label, hits = search_courses(query, term=term, session=session)
    except RuntimeError as e:
        print(f"❌ {e}")
        return

    if not hits:
        print(f"⚠️  Keine Treffer für '{query}' (Semester: {term_label}).")
        return

    print(f"✅ Semester: {term_label} — {len(hits)} Treffer\n")
    for h in hits:
        ects = fetch_detail_ects(h.get("detail_url"), session=session)
        line = f"• {h['title']}  [{h['art']}]  — {h['dozent']}"
        if ects:
            line += f"  ({ects} ECTS)"
        print(line)
        if h.get("unitId"):
            print(f"    unitId={h['unitId']} periodId={h['periodId']}")


def _filename_from_content_disposition(cd: str, fallback: str) -> str:
    """Extract filename from a Content-Disposition header (prefers RFC 5987 filename*=)."""
    from urllib.parse import unquote
    if not cd:
        return fallback
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        # form: charset''percent-encoded
        if "''" in val:
            _, _, encoded = val.partition("''")
            return unquote(encoded)
        return unquote(val.strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip())
    return fallback


def fetch_campo_exam_documents(output_dir: Optional[str] = None, dry_run: bool = False) -> Optional[List[str]]:
    """Download every PDF offered on campo's `personExamsReadonly` page
    (`examsOverviewForPerson-flow`) — Notenübersicht, Bescheinigungen,
    BAföG-§48, Angemeldete Prüfungen, etc.

    Each PDF button is a MyFaces non-AJAX form submit. The flow-execution
    key advances on every interaction, so we re-GET the form before each
    POST to get a fresh ViewState + key.

    Returns list of saved file paths (or button labels in dry-run), or None on failure.
    """
    print("🔄 Fetching campo Notenübersicht / Bescheinigungen page...")
    s = _campo_session()
    if s is None:
        return None

    # Initial GET to enumerate buttons.
    try:
        r = s.get(CAMPO_EXAMS_OVERVIEW_URL, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        print(f"❌ Campo request failed: {e}")
        return None
    if r.status_code != 200 or 'Notenübersicht' not in r.text:
        print("❌ Campo personExamsReadonly not reachable. Log into campo.fau.de in Firefox and retry.")
        return None

    soup = BeautifulSoup(r.text, 'html.parser')
    buttons = soup.find_all('button', {'name': re.compile(r':printReport_\d+$')})
    if not buttons:
        print("⚠️  No printReport_* buttons found on the page.")
        return None

    reports = [(b.get('name'), b.get('value', '').strip()) for b in buttons]
    print(f"📄 Found {len(reports)} report(s):")
    for i, (_, label) in enumerate(reports):
        print(f"   [{i:2d}] {label}")

    if dry_run:
        print("🔎 Dry-run: not downloading.")
        return [label for _, label in reports]

    if output_dir is None:
        output_dir = os.path.join(DOWNLOAD_FOLDER, 'Bescheinigungen')
    os.makedirs(output_dir, exist_ok=True)

    from urllib.parse import urljoin
    saved: List[str] = []
    for idx, (btn_name, label) in enumerate(reports):
        # Re-GET to get a fresh ViewState + flowExecutionKey for each submit.
        try:
            page = s.get(CAMPO_EXAMS_OVERVIEW_URL, timeout=30, allow_redirects=True)
        except requests.RequestException as e:
            print(f"   ✗ [{idx}] {label}: GET failed ({e})")
            continue
        psoup = BeautifulSoup(page.text, 'html.parser')
        btn = psoup.find('button', {'name': btn_name})
        if not btn:
            print(f"   ✗ [{idx}] {label}: button vanished from page")
            continue
        form = btn.find_parent('form')
        action = urljoin(page.url, form.get('action') or page.url)
        payload: Dict[str, str] = {}
        for inp in form.find_all(['input', 'select', 'textarea']):
            n = inp.get('name')
            if not n:
                continue
            payload[n] = inp.get('value', '')
        # MyFaces OAM convention: hidden field name=name marks which button fired.
        payload[btn_name] = btn_name

        try:
            post = s.post(action, data=payload, timeout=120,
                          allow_redirects=True, headers={'Referer': page.url})
        except requests.RequestException as e:
            print(f"   ✗ [{idx}] {label}: POST failed ({e})")
            continue

        ct = (post.headers.get('Content-Type') or '').lower()
        is_pdf = 'pdf' in ct or post.content[:4] == b'%PDF'
        if not is_pdf:
            print(f"   ✗ [{idx}] {label}: response was {ct or '?'} ({len(post.content)} bytes), not PDF")
            continue
        fname = _filename_from_content_disposition(
            post.headers.get('Content-Disposition', ''),
            fallback=f"report_{idx}.pdf")
        # Sanitize: strip path separators.
        fname = os.path.basename(fname).replace('/', '_').replace('\\', '_')
        out_path = os.path.join(output_dir, fname)
        with open(out_path, 'wb') as fh:
            fh.write(post.content)
        saved.append(out_path)
        print(f"   ✓ [{idx}] {label}  →  {fname} ({len(post.content):,} B)")

    print(f"✅ Saved {len(saved)}/{len(reports)} PDF(s) to {output_dir}")
    return saved


_ENROLLMENT_JOB_BUTTON_RE = re.compile(
    r'^studyserviceForm:report:reports:reportButtons:'
    r'jobConfigurationButtons:0:jobConfigurationButtons:\d+:job2$'
)


def _form_payload(form) -> Dict[str, str]:
    """Collect all named input/select/textarea values from a form into a dict."""
    payload: Dict[str, str] = {}
    for inp in form.find_all(['input', 'select', 'textarea']):
        n = inp.get('name')
        if not n:
            continue
        payload[n] = inp.get('value', '') or ''
    return payload


def _parse_partial_response(xml_text: str) -> Tuple[Optional[str], Dict[str, str]]:
    """Parse a JSF <partial-response> envelope.

    Returns (view_state, updates) where updates maps update-id → inner HTML/CDATA.
    """
    try:
        from xml.etree import ElementTree as ET
        root = ET.fromstring(xml_text)
    except Exception:
        return None, {}
    view_state: Optional[str] = None
    updates: Dict[str, str] = {}
    for upd in root.iter('update'):
        uid = upd.get('id') or ''
        text = upd.text or ''
        if uid.startswith('j_id') and 'ViewState' in uid:
            view_state = text
        elif uid == 'javax.faces.ViewState' or uid.endswith(':javax.faces.ViewState'):
            view_state = text
        else:
            updates[uid] = text
    # Fallback: search any element whose id contains ViewState.
    if view_state is None:
        for el in root.iter():
            if 'ViewState' in (el.get('id') or '') and el.text:
                view_state = el.text
                break
    return view_state, updates


def fetch_campo_enrollment_documents(output_dir: Optional[str] = None, dry_run: bool = False) -> Optional[List[str]]:
    """Download every PDF offered on campo's enrollment-info `studyservice-flow` page.

    Yields 7 PDFs (Immatrikulationsbescheinigung, BAföG §9, Studienverlauf,
    Datenkontrollblatt, Benutzerinfobrief, Semesterbeiträge × 2). Each PDF is
    queued via a three-step JSF dance:

      1. Tab POST navigates from "Meine Studiengänge" to "Bescheinigungen".
      2. JSF AJAX `:job2` request opens a per-report config overlay
         (`Faces-Request: partial/ajax`, parses `<partial-response>` for the
         overlay update + fresh ViewState).
      3. Regular form submit of the overlay's `startJob` button enqueues the
         print job; polling `jobDownloadPoll:poll` until `data-stop="true"`
         yields an `asyncDownload` anchor in a partial-response update —
         GET that to receive the PDF.

    A fresh GET anchors a new `_flowExecutionKey` before each iteration so
    sequential job submissions don't run on stale flow state.

    Returns list of saved file paths (or button labels in dry-run), or None on failure.
    """
    print("🔄 Fetching campo Bescheinigungen (enrollment-info) page...")
    s = _campo_session()
    if s is None:
        return None

    from urllib.parse import urljoin

    def _navigate_to_bescheinigungen() -> Optional[Tuple[str, BeautifulSoup]]:
        """GET start.xhtml, POST the Bescheinigungen tab, return (final_url, soup)."""
        try:
            r = s.get(CAMPO_ENROLLMENT_INFO_URL, timeout=30, allow_redirects=True)
        except requests.RequestException as e:
            print(f"❌ Campo request failed: {e}")
            return None
        if r.status_code != 200:
            print(f"❌ Campo enrollment-info GET returned HTTP {r.status_code}.")
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        tab = soup.find('button', {'name': 'studyserviceForm:content.10'})
        if not tab:
            print("❌ Campo enrollment-info not reachable. Log into campo.fau.de in Firefox and retry.")
            return None
        form = tab.find_parent('form')
        if not form:
            print("❌ Bescheinigungen tab has no enclosing form.")
            return None
        action = urljoin(r.url, form.get('action') or r.url)
        payload = _form_payload(form)
        payload['studyserviceForm:content.10'] = 'studyserviceForm:content.10'
        try:
            post = s.post(action, data=payload, timeout=60,
                          allow_redirects=True, headers={'Referer': r.url})
        except requests.RequestException as e:
            print(f"❌ Bescheinigungen tab POST failed: {e}")
            return None
        if post.status_code != 200:
            print(f"❌ Bescheinigungen tab POST returned HTTP {post.status_code}.")
            return None
        return post.url, BeautifulSoup(post.text, 'html.parser')

    nav = _navigate_to_bescheinigungen()
    if nav is None:
        return None
    page_url, soup = nav
    buttons = soup.find_all('button', {'name': _ENROLLMENT_JOB_BUTTON_RE})
    if not buttons:
        print("⚠️  No :job2 buttons found on the Bescheinigungen tab.")
        return None

    reports = [(b.get('name'), (b.get('value') or '').strip() or b.get_text(strip=True)) for b in buttons]
    print(f"📄 Found {len(reports)} enrollment report(s):")
    for i, (_, label) in enumerate(reports):
        print(f"   [{i:2d}] {label}")

    if dry_run:
        print("🔎 Dry-run: not downloading.")
        return [label for _, label in reports]

    if output_dir is None:
        output_dir = os.path.join(DOWNLOAD_FOLDER, 'Bescheinigungen', 'Enrollment')
    os.makedirs(output_dir, exist_ok=True)

    saved: List[str] = []
    for idx, (btn_name, label) in enumerate(reports):
        result = _fetch_one_enrollment_pdf(s, btn_name, label, idx, output_dir)
        if result:
            saved.append(result)

    print(f"✅ Saved {len(saved)}/{len(reports)} PDF(s) to {output_dir}")
    return saved


_DOWNLOAD_HREF_RE = re.compile(r'state=docdownload|asyncDownload|AsyncDownload', re.IGNORECASE)


def _find_download_href(updates: Dict[str, str]) -> Optional[str]:
    """Search partial-response update bodies for a campo doc-download anchor."""
    for body in updates.values():
        if not body:
            continue
        usoup = BeautifulSoup(body, 'html.parser')
        for a in usoup.find_all('a', href=True):
            if _DOWNLOAD_HREF_RE.search(a['href']):
                return a['href']
    return None


def _download_enrollment_pdf(s: requests.Session, poll_url: str, download_href: str,
                             label: str, idx: int, output_dir: str) -> Optional[str]:
    """GET the resolved docdownload anchor → save the PDF. Returns the saved path or None.

    The anchor is lifted from a server-controlled JSF partial-response, so the
    resolved host is re-validated before the GET — an absolute (or protocol-
    relative) off-campo href would otherwise exfiltrate the campo session
    cookies. Mirrors the _url_host_matches gates used on the StudOn side.
    """
    full_href = urljoin(poll_url, download_href)
    if not _url_host_matches(full_href, 'campo.fau.de'):
        print(f"   ✗ [{idx}] {label}: refusing off-campo download host: {full_href}")
        return None
    try:
        pdf_resp = s.get(full_href, timeout=120, allow_redirects=True,
                         headers={'Referer': poll_url})
    except requests.RequestException as e:
        print(f"   ✗ [{idx}] {label}: PDF GET failed ({e})")
        return None
    ct = (pdf_resp.headers.get('Content-Type') or '').lower()
    is_pdf = 'pdf' in ct or pdf_resp.content[:4] == b'%PDF'
    if not is_pdf:
        print(f"   ✗ [{idx}] {label}: download response was {ct or '?'} ({len(pdf_resp.content)} B), not PDF")
        return None
    fname = _filename_from_content_disposition(
        pdf_resp.headers.get('Content-Disposition', ''),
        fallback=f"enrollment_{idx}_{re.sub(r'[^A-Za-z0-9._-]+', '_', label)[:60] or 'report'}.pdf",
    )
    fname = os.path.basename(fname).replace('/', '_').replace('\\', '_')
    out_path = os.path.join(output_dir, fname)
    with open(out_path, 'wb') as fh:
        fh.write(pdf_resp.content)
    print(f"   ✓ [{idx}] {label}  →  {fname} ({len(pdf_resp.content):,} B)")
    return out_path


def _fetch_one_enrollment_pdf(
    s: requests.Session,
    btn_name: str,
    label: str,
    idx: int,
    output_dir: str,
) -> Optional[str]:
    """Run one :job2 → (optionally startJob → poll →) GET docdownload cycle.

    For simple reports (no config needed) the :job2 AJAX POST already enqueues
    the job and returns a `rds?state=docdownload&docId=…` anchor in the
    `jobDownload` update of the `<partial-response>`. For parameterized
    reports the response instead carries a `startJob` button (form submit)
    inside the `jobConfigurationButtonsOverlay` update, which we then submit
    and poll until the download anchor materializes.

    Each cycle anchors a fresh `_flowExecutionKey` via re-navigation from the
    start URL through the Bescheinigungen tab.
    """
    from urllib.parse import urljoin

    # Fresh navigation (resets _flowExecutionKey).
    try:
        r = s.get(CAMPO_ENROLLMENT_INFO_URL, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        print(f"   ✗ [{idx}] {label}: start GET failed ({e})")
        return None
    if r.status_code != 200:
        print(f"   ✗ [{idx}] {label}: start GET HTTP {r.status_code}")
        return None
    soup = BeautifulSoup(r.text, 'html.parser')
    tab = soup.find('button', {'name': 'studyserviceForm:content.10'})
    if not tab:
        print(f"   ✗ [{idx}] {label}: Bescheinigungen tab vanished")
        return None
    form = tab.find_parent('form')
    if not form:
        print(f"   ✗ [{idx}] {label}: tab has no enclosing form")
        return None
    action = urljoin(r.url, form.get('action') or r.url)
    payload = _form_payload(form)
    payload['studyserviceForm:content.10'] = 'studyserviceForm:content.10'
    try:
        tab_resp = s.post(action, data=payload, timeout=60,
                          allow_redirects=True, headers={'Referer': r.url})
    except requests.RequestException as e:
        print(f"   ✗ [{idx}] {label}: tab POST failed ({e})")
        return None
    if tab_resp.status_code != 200:
        print(f"   ✗ [{idx}] {label}: tab POST HTTP {tab_resp.status_code}")
        return None

    tab_soup = BeautifulSoup(tab_resp.text, 'html.parser')
    btn = tab_soup.find('button', {'name': btn_name})
    if not btn:
        print(f"   ✗ [{idx}] {label}: button {btn_name} vanished")
        return None
    job_form = btn.find_parent('form')
    if not job_form:
        print(f"   ✗ [{idx}] {label}: button has no enclosing form")
        return None
    job_action = urljoin(tab_resp.url, job_form.get('action') or tab_resp.url)
    form_payload = _form_payload(job_form)
    view_state = form_payload.get('javax.faces.ViewState', '')
    if not view_state:
        print(f"   ✗ [{idx}] {label}: no ViewState on tab page")
        return None

    # Step 2: JSF AJAX :job2 — opens the per-report config overlay server-side.
    overlay_render = (
        'studyserviceForm:report:reports:reportButtons:jobConfigurationButtonsOverlay '
        'studyserviceForm:report:reports:reportButtons:jobDownload '
        'studyserviceForm:messages-infobox'
    )
    ajax_headers = {
        'Faces-Request': 'partial/ajax',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/xml, text/xml, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
        'Referer': tab_resp.url,
    }
    ajax_payload = {
        'javax.faces.partial.ajax': 'true',
        'javax.faces.source': btn_name,
        'javax.faces.partial.execute': btn_name,
        'javax.faces.partial.render': overlay_render,
        'javax.faces.behavior.event': 'action',
        'javax.faces.partial.event': 'action',
        btn_name: btn_name,
        'studyserviceForm': 'studyserviceForm',
        'javax.faces.ViewState': view_state,
    }
    try:
        ajax_resp = s.post(job_action, data=ajax_payload, headers=ajax_headers, timeout=60)
    except requests.RequestException as e:
        print(f"   ✗ [{idx}] {label}: :job2 AJAX failed ({e})")
        return None
    if ajax_resp.status_code != 200:
        print(f"   ✗ [{idx}] {label}: :job2 AJAX HTTP {ajax_resp.status_code}")
        return None
    new_vs, updates = _parse_partial_response(ajax_resp.text)
    if new_vs:
        view_state = new_vs

    # The :job2 AJAX response itself runs the job for simple reports — the
    # `jobDownload` update will already contain a docdownload anchor. For
    # parameterized reports it instead carries a non-empty config overlay
    # with a startJob button that we then submit.
    download_href = _find_download_href(updates)
    poll_url = job_action

    if not download_href:
        overlay_html = updates.get(
            'studyserviceForm:report:reports:reportButtons:jobConfigurationButtonsOverlay', ''
        )
        if not overlay_html.strip() or 'startJob' not in overlay_html:
            print(f"   ✗ [{idx}] {label}: :job2 returned no download anchor and no startJob overlay")
            return None
        # JSF lazy-renders the actual form into `overlayPlaceholder` only
        # after the user clicks the overlayShowButton. Trigger that AJAX call
        # so the per-report inputs (semester selectors, date pickers, BAföG
        # period selectors, …) appear in the response we then submit.
        show_btn_name = (
            'studyserviceForm:report:reports:reportButtons:'
            'jobConfigurationButtonsOverlay:overlayShowButton'
        )
        placeholder_render = (
            'studyserviceForm:report:reports:reportButtons:'
            'jobConfigurationButtonsOverlay:overlayPlaceholder'
        )
        show_payload = {
            'javax.faces.partial.ajax': 'true',
            'javax.faces.source': show_btn_name,
            'javax.faces.partial.execute': '@this',
            'javax.faces.partial.render': placeholder_render,
            'javax.faces.behavior.event': 'action',
            'javax.faces.partial.event': 'action',
            show_btn_name: show_btn_name,
            'studyserviceForm': 'studyserviceForm',
            'javax.faces.ViewState': view_state,
        }
        try:
            show_resp = s.post(job_action, data=show_payload, headers=ajax_headers, timeout=60)
        except requests.RequestException as e:
            print(f"   ✗ [{idx}] {label}: overlayShowButton AJAX failed ({e})")
            return None
        if show_resp.status_code == 200:
            svs, supdates = _parse_partial_response(show_resp.text)
            if svs:
                view_state = svs
            placeholder_html = supdates.get(placeholder_render, '')
            if placeholder_html.strip():
                overlay_html = overlay_html + placeholder_html

        overlay_soup = BeautifulSoup(overlay_html, 'html.parser')
        start_btn = overlay_soup.find(
            'button',
            {'name': re.compile(r'jobConfigurationButtonsOverlay:.*startJob$')},
        )
        if not start_btn:
            print(f"   ✗ [{idx}] {label}: startJob button not found in overlay")
            return None
        start_btn_name = start_btn.get('name')

        # The overlay carries the per-report form fields (date pickers,
        # semester selectors, etc.) that the server validates when startJob
        # fires. Merge them into the page-form payload so the POST is
        # complete; pre-checked checkbox/radio inputs get their values.
        start_payload = dict(form_payload)
        for inp in overlay_soup.find_all(['input', 'select', 'textarea']):
            n = inp.get('name')
            if not n:
                continue
            itype = (inp.get('type') or '').lower()
            if itype in ('checkbox', 'radio'):
                if inp.has_attr('checked'):
                    start_payload[n] = inp.get('value', 'on') or 'on'
                continue
            if itype in ('submit', 'button', 'image', 'reset'):
                continue
            if inp.name == 'select':
                # Prefer an explicitly-selected option; otherwise pick the
                # first option with a non-empty value (skipping the empty
                # placeholder "" option that JSF cmselect widgets always lead
                # with). This defaults parameterized reports — semester /
                # period selectors — to the current semester, which is what
                # campo lists first after the empty placeholder.
                chosen = inp.find('option', selected=True)
                if not chosen:
                    for o in inp.find_all('option'):
                        if (o.get('value') or '').strip():
                            chosen = o
                            break
                if not chosen:
                    chosen = inp.find('option')
                start_payload[n] = (chosen.get('value', '') if chosen else '') or ''
            else:
                start_payload[n] = inp.get('value', '') or ''
        start_payload['javax.faces.ViewState'] = view_state
        start_payload[start_btn_name] = start_btn_name
        try:
            start_resp = s.post(
                job_action, data=start_payload, timeout=60,
                allow_redirects=True,
                headers={'Referer': tab_resp.url},
            )
        except requests.RequestException as e:
            print(f"   ✗ [{idx}] {label}: startJob POST failed ({e})")
            return None
        if start_resp.status_code != 200:
            print(f"   ✗ [{idx}] {label}: startJob POST HTTP {start_resp.status_code}")
            return None

        start_soup = BeautifulSoup(start_resp.text, 'html.parser')
        vs_input = start_soup.find('input', {'name': 'javax.faces.ViewState'})
        if vs_input and vs_input.get('value'):
            view_state = vs_input['value']
        poll_url = start_resp.url

        # The startJob response often already embeds the docdownload anchor
        # (the job runs synchronously enough for fast reports). Use it
        # directly and skip the poll loop.
        for a in start_soup.find_all('a', href=True):
            if _DOWNLOAD_HREF_RE.search(a['href']):
                download_href = a['href']
                break

    if not download_href:
        # Poll jobDownloadPoll:poll until a download anchor appears.
        poll_name = 'studyserviceForm:report:reports:reportButtons:jobDownloadPoll:poll'
        poll_render = 'studyserviceForm:report:reports:reportButtons:jobDownloadPoll'
        poll_payload = {
            'javax.faces.partial.ajax': 'true',
            'javax.faces.source': poll_name,
            'javax.faces.partial.execute': '@none',
            'javax.faces.partial.render': poll_render,
            poll_name: poll_name,
            'javax.faces.behavior.event': 'poll',
            'javax.faces.partial.event': 'poll',
            'studyserviceForm': 'studyserviceForm',
            'javax.faces.ViewState': view_state,
        }
        poll_headers = {
            'Faces-Request': 'partial/ajax',
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/xml, text/xml, */*; q=0.01',
            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
            'Referer': poll_url,
        }
        last_envelope: Optional[str] = None
        identical_streak = 0
        for attempt in range(30):
            time.sleep(3)
            try:
                poll_resp = s.post(poll_url, data=poll_payload, headers=poll_headers, timeout=30)
            except requests.RequestException as e:
                print(f"   ✗ [{idx}] {label}: poll #{attempt+1} failed ({e})")
                return None
            if poll_resp.status_code != 200:
                print(f"   ✗ [{idx}] {label}: poll #{attempt+1} HTTP {poll_resp.status_code}")
                return None
            envelope = poll_resp.text
            if envelope == last_envelope:
                identical_streak += 1
                if identical_streak >= 3:
                    print(f"   ✗ [{idx}] {label}: poll stalled (3× identical envelope)")
                    return None
            else:
                identical_streak = 0
                last_envelope = envelope
            pvs, pupdates = _parse_partial_response(envelope)
            if pvs:
                poll_payload['javax.faces.ViewState'] = pvs
            href = _find_download_href(pupdates)
            if href:
                download_href = href
                break
        if not download_href:
            print(f"   ✗ [{idx}] {label}: polling exhausted without a download anchor")
            return None

    # GET the docdownload anchor → PDF (host-validated against campo).
    return _download_enrollment_pdf(s, poll_url, download_href, label, idx, output_dir)


def _detect_current_course(base_folder: str) -> Optional[Tuple[str, str, str]]:
    """If the current working directory is inside a tracked course folder, return
    ``(course_title, source_url, course_folder)`` for that course; otherwise None.

    Walks up from CWD toward the download root, returning the first ancestor that
    carries a METADATA.md with a valid StudOn source_url. The download root itself
    is skipped (its METADATA.md would be a stray — see find_all_metadata_files).
    Returns None when CWD is not under the download tree, so a bare invocation from
    an unrelated directory keeps the old clipboard/TUI behaviour.
    """
    try:
        cwd = os.path.abspath(os.getcwd())
    except Exception:
        return None
    base_abs = os.path.abspath(base_folder)
    try:
        # CWD must live inside the download tree (or be the root itself).
        if os.path.commonpath([cwd, base_abs]) != base_abs:
            return None
    except ValueError:
        # Different mount/drive → not comparable → not inside the tree.
        return None

    d = cwd
    while os.path.abspath(d) != base_abs:
        meta_path = os.path.join(d, "METADATA.md")
        if os.path.isfile(meta_path):
            cm = CourseMetadata.from_yaml_markdown(meta_path)
            if cm and cm.source_url and _is_studon_url(cm.source_url):
                return (cm.course_title, cm.source_url, d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _fetch_single_course(title: str, source_url: str, course_folder: str,
                         *, dry_run: bool = False, debug: bool = False) -> None:
    """Download (or dry-run preview) just one course, resolving into its base folder."""
    base = os.path.dirname(course_folder)
    session = _make_session()
    if session is None:
        return
    print(f"📁 Course folder detected: {title}")
    if dry_run:
        _print_discovery_preview(source_url, session, base, debug=debug)
        return
    downloaded, extracted, files_list = process_single_url(source_url, session, base, debug=debug)
    print(f"\n🎉 Done. Downloaded {downloaded} new file(s), extracted {extracted} archive(s).")
    if files_list:
        for filepath in files_list:
            print(f"   • {os.path.relpath(filepath, base)}")


def _run_tui_menu(debug: bool = False, current_course: Optional[Tuple[str, str, str]] = None) -> None:
    """Interactive arrow-key menu — shown when no URL/flag is provided and stdin is a TTY.

    When ``current_course`` is set (CWD sits inside a tracked course folder), an
    "Update this course" action is prepended and pre-selected as the default, so a
    bare invocation from a course folder needs only a single Enter to refresh it.
    """
    global DOWNLOAD_FOLDER

    if not sys.stdin.isatty():
        print("No URL provided. Exiting.")
        return

    installed = _is_installed()
    install_label = ("✅ Uninstall cron jobs & shell alias"
                     if installed else
                     "❗ Install cron jobs & shell alias")
    install_value = "uninstall" if installed else "install"

    imap_installed = _is_imap_installed()
    imap_label = ("✅ Uninstall feedback-mail checker (FAUmail)"
                  if imap_installed else
                  "❗ Install feedback-mail checker (FAUmail)")
    imap_value = "uninstall_imap" if imap_installed else "install_imap"

    current_label = None
    if current_course:
        current_label = f"Update this course: {current_course[0]}"

    choices = []
    if current_course:
        choices.append(
            questionary.Choice(current_label, value="current_course") if questionary else current_label
        )
    choices += [
        questionary.Choice("Register & download a course URL", value="url") if questionary else "Register & download a course URL",
        questionary.Choice("Dry-run all registered courses (preview new files)", value="dry_run") if questionary else "Dry-run all registered courses (preview new files)",
        questionary.Choice("Update all tracked courses", value="update_all") if questionary else "Update all tracked courses",
        questionary.Choice("Check FAUmail for feedback files now", value="check_feedback") if questionary else "Check FAUmail for feedback files now",
        questionary.Choice("Fetch timetable → timetable.md", value="timetable") if questionary else "Fetch timetable → timetable.md",
        questionary.Choice("Map lecture schedule → tracked courses", value="map_lectures") if questionary else "Map lecture schedule → tracked courses",
        questionary.Choice("Discover & register new courses from timetable", value="discover_timetable") if questionary else "Discover & register new courses from timetable",
        questionary.Choice("Set default download path", value="set_path") if questionary else "Set default download path",
        questionary.Choice(install_label, value=install_value) if questionary else install_label,
        questionary.Choice(imap_label, value=imap_value) if questionary else imap_label,
        questionary.Choice("Exit", value="exit") if questionary else "Exit",
    ]

    if questionary:
        action = questionary.select(
            "What would you like to do?",
            choices=choices,
            default="current_course" if current_course else None,
        ).ask()
    else:
        labels = ["Register & download a course URL", "Dry-run all registered courses (preview new files)",
                  "Update all tracked courses", "Check FAUmail for feedback files now",
                  "Fetch timetable → timetable.md", "Map lecture schedule → tracked courses",
                  "Discover & register new courses from timetable",
                  "Set default download path", install_label, imap_label, "Exit"]
        values = ["url", "dry_run", "update_all", "check_feedback", "timetable", "map_lectures",
                  "discover_timetable",
                  "set_path", install_value, imap_value, "exit"]
        if current_course:
            labels.insert(0, current_label)
            values.insert(0, "current_course")
        for i, label in enumerate(labels, 1):
            print(f"  {i}. {label}")
        try:
            idx = int(input("Choice: ").strip()) - 1
            action = values[idx] if 0 <= idx < len(values) else "exit"
        except (ValueError, EOFError):
            action = "exit"

    if action is None or action == "exit":
        return

    if action == "current_course" and current_course:
        title, source_url, course_folder = current_course
        _fetch_single_course(title, source_url, course_folder, debug=debug)
        return

    if action == "install":
        _run_install()
        run_daily_sync()
        _run_tui_menu(debug=debug)
        return

    if action == "uninstall":
        _run_uninstall()
        return

    if action == "install_imap":
        _run_install_imap()
        return

    if action == "uninstall_imap":
        _run_uninstall_imap()
        return

    if action == "check_feedback":
        n_processed, n_files, _ = check_and_process_feedback(verbose=True)
        print(f"Feedback: processed {n_processed} exercise(s), downloaded {n_files} file(s).")
        return

    if action == "set_path":
        new_path = _tui_prompt_download_path()
        if new_path:
            cfg = load_config()
            cfg["downloads_path"] = new_path
            save_config(cfg)
            DOWNLOAD_FOLDER = new_path
            print(f"✅ Download path saved: {new_path}")
        return

    if action == "update_all":
        success, n_downloaded, n_extracted, session_expired, _ = update_all_courses(debug=debug)
        if success:
            _record_tray_sync("Alle Kurse")
        if session_expired:
            recovered_session = _interactive_login_recovery()
            if recovered_session is not None:
                print("\n🔄 Retrying update with new session...\n")
                if update_all_courses(debug=debug, session=recovered_session)[0]:
                    _record_tray_sync("Alle Kurse")
            else:
                print("\n⏭️  Update skipped — no valid session available.")
        return

    if action == "timetable":
        fetch_timetable_markdown()
        return

    if action == "map_lectures":
        run_map_lectures_interactive()
        return

    if action == "discover_timetable":
        run_discover_from_timetable(debug=debug)
        return

    if action == "dry_run":
        session = _make_session()
        if session is None:
            return
        metadata_files = find_all_metadata_files(DOWNLOAD_FOLDER)
        if not metadata_files:
            print("No registered courses found.")
            return
        for _, source_url, course_folder in metadata_files:
            _print_discovery_preview(source_url, session, os.path.dirname(course_folder), debug=debug)
        return

    # action == "url": preview first, then confirm download
    url = _tui_prompt_url()
    if not url:
        print("No URL provided.")
        return

    session = _make_session()
    if session is None:
        return

    _print_discovery_preview(url, session, DOWNLOAD_FOLDER, debug=debug)

    if questionary:
        confirmed = questionary.confirm("Proceed with download?", default=True).ask()
    else:
        confirmed = input("\nProceed with download? [Y/n]: ").strip().lower() != "n"

    if not confirmed:
        print("Aborted.")
        return

    downloaded, extracted, files_list = process_single_url(url, session, DOWNLOAD_FOLDER, debug=debug)
    print(f"\n🎉 Done. Downloaded {downloaded} new file(s), extracted {extracted} archive(s).")
    if files_list:
        for filepath in files_list:
            print(f"   • {os.path.relpath(filepath, DOWNLOAD_FOLDER)}")


def main() -> None:
    """Main execution loop."""
    global DOWNLOAD_FOLDER

    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='StudOn Recursive File Downloader & Auto-Updater')
    parser.add_argument('url', nargs='?', help='StudOn URL to download from')
    parser.add_argument('download_path', nargs='?', help='Custom download path (one-time override)')
    parser.add_argument('--update-all', '-u', action='store_true',
                        help='Update all courses by scanning existing METADATA.md files')
    parser.add_argument('--daily-sync', action='store_true',
                       help='Wait for Firefox and perform daily sync, then exit (for @reboot cron)')
    parser.add_argument('--lecture-sync', action='store_true',
                       help='Run the per-lecture sync daemon (fetches the current course at start-5m/start/start+5m). Intended for @reboot cron.')
    parser.add_argument('--lecture-sync-once', action='store_true',
                       help='Print resolved timetable buckets and next 5 fires, then exit (testing).')
    parser.add_argument('--map-lectures', action='store_true',
                       help='Interactively map campo timetable entries to tracked StudOn courses.')
    parser.add_argument('--discover-from-timetable', action='store_true',
                       help='For each Unmapped campo timetable entry, follow the Detailansicht button to its StudOn course and register it as a tracked course.')
    parser.add_argument('--interval', '-i', type=int, default=5,
                       help='Check interval in minutes for --daily-sync (default: 5)')
    parser.add_argument('--debug', '-d', action='store_true',
                       help='Enable debug mode (saves HTML and shows detailed logging)')
    parser.add_argument('--set-download-path', metavar='PATH',
                       help='Persist a default download path to config.json and exit')
    parser.add_argument('--clip', action='store_true',
                       help='Read clipboard, detect StudOn URL, preview files, and confirm before downloading')
    parser.add_argument('--dry-run', action='store_true',
                       help='Discover files without downloading (preview mode)')
    parser.add_argument('--install', action='store_true',
                       help='Install the cron jobs and the studon-client shell alias (replaces setup_daily_sync.sh)')
    parser.add_argument('--remove', action='store_true',
                       help='Remove the cron jobs and the shell alias, including entries written by older versions')
    parser.add_argument('--install-skill', action='store_true',
                       help='(Re)write ~/.claude/skills/studon-client/SKILL.md from the inline source so Claude Code surfaces this scraper as a skill')
    parser.add_argument('--uninstall-skill', action='store_true',
                       help='Remove ~/.claude/skills/studon-client/SKILL.md')
    parser.add_argument('--tray', action='store_true',
                       help='Show the StudOn tray icon again after "Tray schliessen" and exit')
    parser.add_argument('--timetable', action='store_true',
                       help='Fetch personal campo timetable and write to timetable.md')
    parser.add_argument('--modulplan', action='store_true',
                        help='Scan campo studyPlanner front page (deterministic) → Modulplan.md with status/Versuch/Semester/ECTS')
    parser.add_argument('--belegungen', action='store_true',
                        help='Scan campo Belegungen page (searchOwnEnrollmentInfo-flow, deterministic) → Belegungen.md + Belegungen.json. Pure fetch; change-detection lives in sibling belegungen-watcher tool.')
    parser.add_argument('--reconcile', action='store_true',
                        help='Cross-check Modulplan ↔ Belegungen and write Reconciliation.md (resolves ECTS-Bilanz-Diskrepanz, lists Prüfungen ohne Modul-Match, Modulplan-Angemeldet ohne Belegung, Bestanden ohne ECTS-Suffix)')
    parser.add_argument('--campo-pruefungen', action='store_true',
                       help='Scan campo studyPlanner Detailansichten (opened in Firefox) and write pruefungen.md')
    parser.add_argument('--campo-search', metavar='QUERY',
                       help='Search campo courses for QUERY in a semester (default: current) and print hits + ECTS to stdout')
    parser.add_argument('--term', metavar='TERMID',
                       help="Semester for --campo-search or --timetable, e.g. 'eq|1|2026' (SoSe26) / "
                            "'eq|2|2026' (WiSe26/27) or a raw campo option id (e.g. 590); "
                            "default = current semester. For --timetable a non-current term writes "
                            "timetable_<label>.md (e.g. timetable_WS2627.md) and leaves timetable.md untouched.")
    parser.add_argument('--campo-bescheinigungen', action='store_true',
                       help='Download all PDFs from campo Notenübersicht / personExamsReadonly page (Notenübersicht, BAföG-§48, ord. Studium, angemeldete Prüfungen, ...) into <downloads>/Bescheinigungen/')
    parser.add_argument('--campo-enrollment-bescheinigungen', action='store_true',
                       help='Download all 7 PDFs from campo enrollment-info studyservice-flow page (Immatrikulationsbescheinigung, BAföG §9, Studienverlauf, Datenkontrollblatt, Benutzerinfobrief, Semesterbeiträge ×2) into <downloads>/Bescheinigungen/Enrollment/')
    parser.add_argument('--with-enrollment', action='store_true',
                       help='When combined with --campo-bescheinigungen, also fetch the 7 enrollment-side PDFs (total 19).')
    parser.add_argument('--install-imap', action='store_true',
                       help='Configure FAUmail IMAP credentials (for feedback-file auto-download)')
    parser.add_argument('--uninstall-imap', action='store_true',
                       help='Remove FAUmail credentials and feedback queue state')
    parser.add_argument('--check-feedback', action='store_true',
                       help='Scan FAUmail inbox for StudOn feedback notifications and download any reachable PDFs')
    parser.add_argument('--reset-feedback-state', action='store_true',
                       help='Delete .studon_feedback_state.json (forces reprocessing of all matching emails)')
    parser.add_argument('--login', nargs='?', const='studon', default=None,
                       choices=['studon', 'campo', 'both'], metavar='TARGET',
                       help="Open Firefox at the login page and wait until the session is "
                            "authenticated. TARGET selects which service: 'studon' (default), "
                            "'campo' (campo.fau.de — for --modulplan/--belegungen/--reconcile/"
                            "--campo-bescheinigungen), or 'both'. Bare --login == --login studon.")

    args = parser.parse_args()

    # --- Open Firefox for (re-)login and wait ---
    if args.login:
        targets = ['studon', 'campo'] if args.login == 'both' else [args.login]
        for target in targets:
            if target == 'campo':
                login_url = CAMPO_STUDY_PLANNER_URL
                access_check = can_access_campo
                label = 'campo'
            else:
                login_url = _get_first_course_url()
                access_check = can_access_studon
                label = 'StudOn'
            print(f"Opening Firefox for {label} login…")
            print(f"   URL: {login_url}")
            _open_url_in_browser(login_url)
            ok = _wait_for_login_via_tray(login_url, access_check=access_check)
            if not ok:
                # Tray unavailable — fall back to silent polling
                print(f"Waiting for {label} login (checking every 5 s)…")
                while not access_check():
                    time.sleep(5)
            print(f"✅ {label} session is active.")
        return

    # --- Timetable export ---
    if args.timetable:
        fetch_timetable_markdown(term=args.term)
        return

    # --- Campo Modulplan (deterministic studyPlanner scan) ---
    if args.modulplan:
        fetch_campo_modulplan()
        return

    # --- Campo Belegungen (deterministic searchOwnEnrollmentInfo scan) ---
    if args.belegungen:
        fetch_campo_belegungen()
        return

    # --- Campo Reconciliation (Modulplan ↔ Belegungen cross-check) ---
    if args.reconcile:
        fetch_campo_reconciliation()
        return

    # --- Campo Prüfungstermine / Anmeldefristen ---
    if args.campo_pruefungen:
        fetch_campo_pruefungen_markdown()
        return

    # --- Campo course search (Lehrveranstaltungssuche) ---
    if args.campo_search:
        _run_campo_search(args.campo_search, term=args.term)
        return

    # --- Campo Notenübersicht / Bescheinigungen (PDF bulk download) ---
    if args.campo_bescheinigungen:
        fetch_campo_exam_documents(dry_run=args.dry_run)
        if args.with_enrollment:
            fetch_campo_enrollment_documents(dry_run=args.dry_run)
        return

    # --- Campo Bescheinigungen — enrollment side only (studyservice-flow) ---
    if args.campo_enrollment_bescheinigungen:
        fetch_campo_enrollment_documents(dry_run=args.dry_run)
        return

    # --- Claude Code skill registration ---
    if args.install_skill:
        _run_install_skill()
        return

    if args.uninstall_skill:
        _run_uninstall_skill()
        return

    # --- Bring the tray back after the user closed it ---
    if args.tray:
        global _tray_proc
        _clear_tray_closed()
        _launch_tray()
        if _tray_proc is not None:
            # The tray must outlive this short-lived process, so drop the
            # atexit terminate hook _launch_tray registered.
            atexit.unregister(_stop_tray)
            _tray_proc = None
            print("✅ StudOn tray icon started.")
        else:
            print("Tray not started: no display, or the system python lacks AppIndicator.")
        return

    # --- IMAP setup ---
    if args.install_imap:
        _run_install_imap()
        return

    if args.uninstall_imap:
        _run_uninstall_imap()
        return

    if args.reset_feedback_state:
        if os.path.exists(FEEDBACK_STATE_FILE):
            os.remove(FEEDBACK_STATE_FILE)
            print(f"✅ Deleted {FEEDBACK_STATE_FILE}. Next --check-feedback will reprocess all matching emails.")
        else:
            print("No feedback state file to delete.")
        return

    # --- Feedback inbox scan ---
    if args.check_feedback:
        n_processed, n_files, _ = check_and_process_feedback(verbose=True)
        print(f"Feedback: processed {n_processed} exercise(s), downloaded {n_files} file(s).")
        return

    # --- Install / remove cron + shell alias ---
    if args.install:
        _run_install(check_interval=args.interval)
        return

    if args.remove:
        _run_uninstall()
        return

    # --- Persist download path to config.json and exit ---
    if args.set_download_path:
        new_path = str(Path(args.set_download_path).expanduser().resolve())
        cfg = load_config()
        cfg["downloads_path"] = new_path
        save_config(cfg)
        print(f"✅ Download path saved to {CONFIG_FILE}")
        print(f"   downloads_path = {new_path}")
        print("   This path will be used by all future runs, including the cron daily sync.")
        return

    # Enable debug logging if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)
        logger.debug("Debug mode enabled")

    # --- Clipboard quick-fetch mode ---
    if args.clip:
        _run_clip_mode(debug=args.debug)
        return

    # Handle daily sync mode (silent — runs as background cron)
    if args.daily_sync:
        check_interval_seconds = args.interval * 60
        run_daily_sync(check_interval_seconds=check_interval_seconds)
        return

    # Lecture sync (per-lecture daemon)
    if args.lecture_sync:
        run_lecture_sync(once=False)
        return
    if args.lecture_sync_once:
        run_lecture_sync(once=True)
        return
    if args.map_lectures:
        run_map_lectures_interactive()
        return
    if args.discover_from_timetable:
        run_discover_from_timetable(debug=args.debug)
        return

    effective_folder = args.download_path if (args.update_all and args.download_path) else DOWNLOAD_FOLDER
    show_startup_overview(effective_folder)

    if args.update_all:
        if args.download_path:
            DOWNLOAD_FOLDER = args.download_path
        success, n_downloaded, n_extracted, session_expired, _ = update_all_courses(debug=args.debug)
        if success:
            _record_tray_sync("Alle Kurse")
        if session_expired and sys.stdin.isatty():
            recovered_session = _interactive_login_recovery()
            if recovered_session is not None:
                print("\n🔄 Retrying update with new session...\n")
                if update_all_courses(debug=args.debug, session=recovered_session)[0]:
                    _record_tray_sync("Alle Kurse")
            else:
                print("\n⏭️  Update skipped — no valid session available.")
        return

    if args.url:
        session = _make_session()
        if session is None:
            return
        if args.download_path:
            DOWNLOAD_FOLDER = args.download_path
        if args.dry_run:
            _print_discovery_preview(args.url, session, DOWNLOAD_FOLDER, debug=args.debug)
            return
        downloaded, extracted, files_list = process_single_url(args.url, session, DOWNLOAD_FOLDER, debug=args.debug)
        print(f"\n🎉 Done. Downloaded {downloaded} new file(s), extracted {extracted} archive(s).")
        if files_list:
            for filepath in files_list:
                print(f"   • {os.path.relpath(filepath, DOWNLOAD_FOLDER)}")
        return

    # No explicit URL/flag. If we're sitting inside a tracked course folder, that
    # course is the obvious target: auto-fetch it when headless, or pre-select it
    # in the TUI so a single Enter refreshes just this course.
    current_course = _detect_current_course(DOWNLOAD_FOLDER)
    if current_course and not sys.stdin.isatty():
        title, source_url, course_folder = current_course
        _fetch_single_course(title, source_url, course_folder,
                             dry_run=args.dry_run, debug=args.debug)
        return

    # Otherwise: check clipboard, then TUI
    try:
        clip = pyperclip.paste().strip()
    except Exception:
        clip = ""
    if clip and _is_studon_url(clip):
        _run_clip_mode(debug=args.debug)
        return

    _run_tui_menu(debug=args.debug, current_course=current_course)


if __name__ == "__main__":
    main()