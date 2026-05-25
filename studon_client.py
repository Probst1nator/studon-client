import os
import sys
import json

# --- CRITICAL: respond to --advertise BEFORE any heavy imports ---
# The tools-installer machinery (used by ~/Synced/repos/AutomatedAlchemy/
# installer.py, which re-uses ~/Synced/repos/tools/installer.py as a library)
# probes each candidate script with `--advertise` and a 5s timeout. The
# heavy imports below (requests, BeautifulSoup, browser_cookie3, …) would
# blow the budget, so short-circuit here.
if "--advertise" in sys.argv:
    print(json.dumps([{
        "name": "StudOn Client",
        "desktop_file": "studon_client.desktop",
        "icon": "applications-internet",
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
        # ~/.claude/skills/studon/SKILL.md from inline SKILL_MD_CONTENT.
        "skill_name": "studon",
    }]))
    sys.exit(0)

import webbrowser
import re
import email.utils
import shutil
import subprocess
import time
import requests
import pyperclip
import browser_cookie3
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import zipfile
import tarfile
import argparse
from dataclasses import dataclass, field
from tabulate import tabulate
from pathlib import Path
import logging
import yaml
import platform as platform_module
from html import escape as _html_escape

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
    from cli_tool_kit import ToolInstaller, ToolMetadata, CronInstaller
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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('studon_sync.log', encoding='utf-8'),
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

def find_all_metadata_files(base_folder: str) -> List[Tuple[str, str, str]]:
    """
    Finds all METADATA.md files in the download folder.
    Returns a list of tuples: (metadata_file_path, source_url, course_folder_path)

    Skips a METADATA.md that sits directly at base_folder (the download root) — a
    tracked course always lives inside its own subfolder. Also skips entries whose
    source URL fails is_valid_url() (defensive guard against historical garbage
    like `source_url: h`).
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
        try:
            with open(metadata_path, 'r') as f:
                content = f.read()
                match = re.search(r'^Source:\s*(.+)$', content, re.MULTILINE)
                if not match:
                    continue
                source_url = match.group(1).strip()
                if not is_valid_url(source_url):
                    logger.warning(
                        f"Skipping {metadata_path}: invalid source_url {source_url!r}."
                    )
                    continue
                metadata_files.append((metadata_path, source_url, root))
        except Exception as e:
            logger.warning(f"Could not read {metadata_path}: {e}")

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
        with open(metadata_path, 'w', encoding='utf-8') as f:
            f.write(metadata.to_yaml_markdown(course_folder))
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
            filepath_candidates = [os.path.join(save_path, expected_name)]
            if '.' not in expected_name:
                filepath_candidates.append(os.path.join(save_path, expected_name + '.pdf'))

            file_exists = False
            existing_path = None
            for candidate in filepath_candidates:
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

            filepath: str = os.path.join(save_path, filename)

            with open(filepath, 'wb') as f:
                for chunk in file_response.iter_content(chunk_size=8192):
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

def update_all_courses(debug: bool = False, session: Optional[requests.Session] = None) -> Tuple[bool, int, int, bool]:
    """Update all courses by scanning METADATA.md files.

    Args:
        debug: If True, enables debug output and saves HTML for troubleshooting.
        session: Pre-authenticated requests session. If None, cookies are loaded
                 from Firefox automatically.

    Returns:
        Tuple of (success, total_downloaded, total_extracted, session_expired).
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
            return False, 0, 0, False

        n = len(metadata_files)
        print(f"Updating {n} course{'s' if n != 1 else ''}...")

        total_downloaded = 0
        total_extracted = 0
        total_git_pulled = 0
        total_git_failed = 0
        successful_courses = 0
        session_expired = False

        for i, (metadata_path, source_url, course_folder) in enumerate(metadata_files, 1):
            name = os.path.basename(course_folder)
            print(f"  [{i}/{n}] {name}", end='', flush=True)

            try:
                downloaded, extracted, _ = process_single_url(source_url, session, course_folder, create_course_subfolder=False, debug=debug)
                total_downloaded += downloaded
                total_extracted += extracted
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
            return False, 0, 0, True

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

        return successful_courses > 0, total_downloaded, total_extracted, False

    except Exception as e:
        logger.error(f"Error during update: {e}")
        return False, 0, 0, False

def _send_desktop_notification(n_downloaded: int, n_extracted: int) -> None:
    """Send a desktop notification via notify-send (Linux)."""
    if not shutil.which("notify-send"):
        return
    if n_downloaded:
        parts = [f"{n_downloaded} new file{'s' if n_downloaded != 1 else ''} downloaded"]
        if n_extracted:
            parts.append(f"{n_extracted} extracted")
        body = ", ".join(parts) + "."
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


def _wait_for_login_via_tray(login_url: str, max_wait_seconds: Optional[int] = None) -> bool:
    """Show a tray icon while polling for a valid StudOn login.

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
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import threading
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as e:
        logger.info(f"Tray icon unavailable ({e}); falling back to silent polling.")
        return False

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
        icon.visible = True
        while not stop_event.is_set():
            try:
                if can_access_studon():
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

    menu = pystray.Menu(
        pystray.MenuItem("Open StudOn login", open_login, default=True),
        pystray.MenuItem("Check now", lambda icon, item: state.update(fast_until=time.time() + 120)),
        pystray.MenuItem("Quit (skip sync)", quit_waiter),
    )
    icon = pystray.Icon("studon-client", img, "StudOn: waiting for login", menu)

    thread = threading.Thread(target=poller, args=(icon,), daemon=True)
    thread.start()
    try:
        icon.run()
    except Exception as e:
        logger.warning(f"Tray icon failed to run: {e}; falling back to silent polling.")
        stop_event.set()
        return False
    stop_event.set()
    return state["logged_in"]


def run_daily_sync(check_interval_seconds: int = 300) -> None:
    """
    Run until a daily sync is performed, then exit.
    Waits for StudOn login (via Firefox cookies) and performs sync once per day.

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
    while True:
        try:
            if not can_access_studon():
                if not waiting_logged:
                    logger.info("Daily sync: waiting for StudOn login (tray icon active)")
                    waiting_logged = True
                login_url = f"https://{STUDON_DOMAIN}"
                try:
                    login_url = _get_first_course_url()
                except Exception:
                    pass
                tray_ok = _wait_for_login_via_tray(login_url)
                if not tray_ok:
                    # Tray unavailable, user quit, or icon errored — poll silently.
                    time.sleep(check_interval_seconds)
                waiting_logged = False
                continue

            if not _acquire_sync_lock('daily-sync', wait_seconds=600):
                logger.info("Daily sync: lock busy, deferring 5 min.")
                time.sleep(300)
                continue
            try:
                success, n_downloaded, n_extracted, session_expired = update_all_courses()
                if success:
                    try:
                        fb_processed, fb_files = check_and_process_feedback()
                        if fb_files:
                            logger.info(f"Feedback sync: downloaded {fb_files} file(s) across {fb_processed} exercise(s).")
                            n_downloaded += fb_files
                    except Exception as e:
                        logger.warning(f"Feedback check failed (non-fatal): {e}")
                    logger.info("Daily sync complete.")
                    _send_desktop_notification(n_downloaded, n_extracted)
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


def _lecture_fetch_one(course: TrackedCourse) -> Tuple[int, int]:
    """Run a single-course fetch via the existing pipeline. Returns (downloaded, extracted)."""
    try:
        cj = browser_cookie3.firefox(domain_name=STUDON_DOMAIN)
    except Exception as e:
        raise FirefoxCookieError(e)
    session = requests.Session()
    session.cookies.update(cj)
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    downloaded, extracted, _ = process_single_url(
        course.source_url, session, course.course_folder,
        create_course_subfolder=False, debug=False,
    )
    return downloaded, extracted


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

    while True:
        try:
            now = datetime.now()
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

            if not can_access_studon():
                logger.info(f"Lecture sync: not logged in at fire {fire_time.isoformat()}; "
                            f"opening tray (max {tray_wait_seconds}s).")
                login_url = course.source_url or f"https://{STUDON_DOMAIN}"
                tray_ok = _wait_for_login_via_tray(login_url, max_wait_seconds=tray_wait_seconds)
                if not tray_ok or not can_access_studon():
                    logger.info("Lecture sync: still not logged in after tray window; skipping this fire.")
                    continue

            if not _acquire_sync_lock(f'lecture-sync:{os.path.basename(course.course_folder)}', wait_seconds=0):
                logger.info("Lecture sync: another sync is running; skipping this fire.")
                continue
            try:
                logger.info(f"Lecture sync: fetching '{course.course_title}'")
                try:
                    downloaded, extracted = _lecture_fetch_one(course)
                    if downloaded:
                        _send_desktop_notification(downloaded, extracted)
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
            folder_exists = os.path.isdir(entry.path)
            file_count, total_bytes = _course_folder_stats(entry.path) if folder_exists else (0, 0)
            courses.append({
                'name':    meta.course_title if meta else entry.name,
                'folder':  entry.path,
                'exists':  folder_exists,
                'synced':  meta.last_fetched_formatted[:10] if meta else '—',
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


def _is_installed() -> bool:
    """Return True if a cron job for this script is already registered."""
    script_path = os.path.abspath(__file__)
    try:
        proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if proc.returncode != 0:
            return False
        return any(
            script_path in line and ('--daily-sync' in line or '--lecture-sync' in line)
            for line in proc.stdout.splitlines()
        )
    except FileNotFoundError:
        return False


def _run_uninstall() -> None:
    """Remove the cron jobs and bashrc function installed by --install."""
    script_path = os.path.abspath(__file__)
    # --- Cron ---
    try:
        proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        existing = proc.stdout if proc.returncode == 0 else ''
        clean = [l for l in existing.splitlines()
                 if not (script_path in l and ('--daily-sync' in l or '--lecture-sync' in l))]
        if len(clean) < len(existing.splitlines()):
            subprocess.run(['crontab', '-'], input='\n'.join(clean) + '\n',
                           capture_output=True, text=True)
            print("  ✅ Cron jobs removed.")
        else:
            print("  No matching cron entries found.")
    except FileNotFoundError:
        print("  crontab not available — skipping.")

    # --- Bashrc ---
    bashrc = Path.home() / '.bashrc'
    marker = '# studon-client quick-fetch'
    if bashrc.exists():
        lines = bashrc.read_text().splitlines(keepends=True)
        filtered = [l for l in lines if marker not in l and 'studon-client()' not in l]
        if len(filtered) < len(lines):
            bashrc.write_text(''.join(filtered))
            print("  ✅ Shell function removed from ~/.bashrc.")
        else:
            print("  No shell function found in ~/.bashrc.")


# --- Claude Code skill registration ---------------------------------------
# Single source of truth for ~/.claude/skills/studon/SKILL.md. Update this
# when CLI flags change so `python3 studon_client.py --install-skill` re-
# registers a fresh manifest. Kept inline so the script stays self-contained.
SKILL_DIR  = Path.home() / '.claude' / 'skills' / 'studon'
SKILL_FILE = SKILL_DIR / 'SKILL.md'
SKILL_MD_CONTENT = '''---
name: studon
description: Drive the StudOn / Campo scraper at ~/Synced/repos/AutomatedAlchemy/studon-client/. Use when the user asks to download FAU StudOn course material, register a new course, refresh tracked courses, inspect the campo timetable, dump prüfungs-Anmeldefristen, or bulk-download campo Notenübersicht / Bescheinigungen PDFs (Notenübersicht, BAföG §48, ord. Studium, angemeldete Prüfungen). Triggers: "studon course holen", "alle kurse aktualisieren", "campo timetable export", "bescheinigung ziehen", "notenübersicht pdf", "studon scrape", "FAU course download". NOT for the QuizHub daily-quiz (that's the `quizhub-client` cron).
---

# studon

Wrapper for the StudOn / Campo scraper at
`~/Synced/repos/AutomatedAlchemy/studon-client/studon_client.py`.
Authenticates to FAU StudOn + campo via Firefox cookies (`browser-cookie3`),
crawls course pages, and downloads materials into the configured downloads
folder (`~/Synced/OneDrive/Studium/KIM4/` on this fleet).

The scraper is already installed and self-running:
- `@reboot studon_client.py --daily-sync` — once-per-day full sync of all tracked courses
- `@reboot studon_client.py --lecture-sync` — per-lecture fetcher driven by the campo timetable

This skill is for **ad-hoc invocations** from a Claude session — anything the
cron daemons don't already do automatically.

## When to use

- "Download this StudOn course" → has a URL → `<URL>` mode below.
- "Update all my courses now" → `--update-all`.
- "Was kommt diese Woche an Vorlesungen?" → `--timetable` → reads `timetable.md`.
- "Welche Prüfungsanmeldungen laufen?" → `--campo-pruefungen` (requires Detailansichten pre-opened in Firefox).
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
| Download one course by URL | `$PY $SCRAPER <studon_course_url>` |
| Preview without downloading | `$PY $SCRAPER <url> --dry-run` |
| Refresh every tracked course | `$PY $SCRAPER --update-all` |
| Export campo timetable → `timetable.md` | `$PY $SCRAPER --timetable` |
| Map unmapped timetable entries → courses | `$PY $SCRAPER --map-lectures` |
| Auto-register new courses from timetable | `$PY $SCRAPER --discover-from-timetable` |
| Dump Prüfungs-Anmeldefristen → `pruefungen.md` | `$PY $SCRAPER --campo-pruefungen` |
| Download all Notenübersicht/Bescheinigungen PDFs | `$PY $SCRAPER --campo-bescheinigungen` |
| Show next 5 lecture-sync fires (debug) | `$PY $SCRAPER --lecture-sync-once` |
| Set default download path | `$PY $SCRAPER --set-download-path ~/path` |
| (Re)install this Claude skill | `$PY $SCRAPER --install-skill` |

Full architecture & dataclasses: `~/Synced/repos/AutomatedAlchemy/studon-client/CLAUDE.md`.

## Preconditions

- **Firefox must be logged into both StudOn and campo.** The scraper reads
  Firefox cookies via `browser-cookie3`. If the cookie is stale, the scraper
  prints `❌ Could not load Firefox cookies` or `make sure you're logged in`.
  Tell the user to refresh both tabs in Firefox, then re-run.
- For `--campo-pruefungen`: the user must have **manually opened each
  Modul/Prüfungs-Detailansicht in Firefox** beforehand (the `_flowExecutionKey`
  is server-side per-session state — the scraper can only iterate keys that
  already exist in the current campo flow stack).
- For `--campo-bescheinigungen`: only requires being logged into campo — the
  page enumerates its own 12 PDF buttons and the scraper re-GETs the form per
  button to refresh the `_flowExecutionKey`.

## Output layout

```
~/Synced/OneDrive/Studium/KIM4/         # configured downloads_path
├── timetable.md                        # --timetable
├── .timetable_entries.json             # cache consumed by --lecture-sync
├── pruefungen.md                       # --campo-pruefungen
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


def _run_install_skill() -> None:
    """Write (or refresh) ~/.claude/skills/studon/SKILL.md from the inline source."""
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
    """Remove ~/.claude/skills/studon/SKILL.md (and the empty dir)."""
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
    Installs the @reboot cron job and the 'studon-client' bashrc function.
    """
    import importlib.util

    script_path = os.path.abspath(__file__)
    script_dir  = os.path.dirname(script_path)
    python      = sys.executable

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

    # --- Cron jobs ---
    daily_cmd = f"@reboot cd {script_dir} && {python} {script_path} --daily-sync"
    if check_interval != 5:
        daily_cmd += f" --interval {check_interval}"
    lecture_cmd = f"@reboot cd {script_dir} && {python} {script_path} --lecture-sync"
    desired_cmds = [daily_cmd, lecture_cmd]

    print("Cron entries:")
    for c in desired_cmds:
        print(f"  {c}")
    print()

    cron_ok = False
    try:
        proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        existing_tab = proc.stdout if proc.returncode == 0 else ''
    except FileNotFoundError:
        print("  ERROR: crontab not found — install it manually.")
        existing_tab = None

    def _is_studon_line(l: str) -> bool:
        return 'studon' in l and ('--daily-sync' in l or '--lecture-sync' in l)

    if existing_tab is not None:
        existing_lines = existing_tab.splitlines()
        studon_lines = [l for l in existing_lines if _is_studon_line(l)]
        already_ok = sorted(studon_lines) == sorted(desired_cmds)
        if already_ok:
            print("  Cron entries already up to date.")
            cron_ok = True
        else:
            if studon_lines:
                print(f"  Replacing existing entries:")
                for l in studon_lines:
                    print(f"    {l}")
                if input("  Replace? [Y/n]: ").strip().lower() == 'n':
                    print("  Keeping existing cron entries.")
                    cron_ok = True
                else:
                    studon_lines = []  # signal rewrite
            if not cron_ok:
                clean = [l for l in existing_lines if not _is_studon_line(l)]
                clean.extend(desired_cmds)
                new_tab = '\n'.join(clean) + '\n'
                result = subprocess.run(['crontab', '-'], input=new_tab,
                                        capture_output=True, text=True)
                cron_ok = result.returncode == 0
                if cron_ok:
                    print("  Cron entries installed.")
                else:
                    print(f"  Failed: {result.stderr.strip()}")
    print()

    # --- Bashrc function ---
    print("Installing 'studon-client' shell function...")
    bashrc   = Path.home() / '.bashrc'
    marker   = '# studon-client quick-fetch'
    func_line = f'studon-client() {{ {python} {script_path} --clip "$@"; }}'

    if bashrc.exists():
        content = bashrc.read_text()
    else:
        content = ''

    if marker in content:
        lines     = content.splitlines()
        new_lines = [func_line if l.startswith('studon-client()') else l for l in lines]
        new_content = '\n'.join(new_lines) + '\n'
        if new_content == content:
            print("  Already up to date in ~/.bashrc")
        else:
            bashrc.write_text(new_content)
            print("  Updated in ~/.bashrc")
    else:
        bashrc.write_text(content.rstrip('\n') + f'\n\n{marker}\n{func_line}\n')
        print("  Added to ~/.bashrc")
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


def _process_feedback_queue(session: requests.Session, mark_seen: bool = True, verbose: bool = False) -> Tuple[int, int]:
    """
    Walk the feedback queue, download PDFs for any URL we can now reach.
    On success, mark the IMAP message as read and move the entry to processed.
    Returns (n_processed, n_downloaded_files).
    """
    state = _load_feedback_state()
    queue = state.get("queue", [])
    if not queue:
        return 0, 0

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
    return n_processed, n_files


def check_and_process_feedback(session: Optional[requests.Session] = None, verbose: bool = False) -> Tuple[int, int]:
    """High-level entry: scan inbox for new notifications, then process the queue."""
    new = fetch_feedback_emails(verbose=verbose)
    if new:
        logger.info(f"Queued {len(new)} new feedback notification(s).")
    if session is None:
        session = _make_session()
    if session is None:
        logger.info("StudOn session not available; feedback URLs remain queued.")
        return 0, 0
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

    soup = BeautifulSoup(r.text, 'html.parser')
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


def fetch_timetable_markdown(output_path: Optional[str] = None) -> Optional[str]:
    """Fetch campo timetable, write Markdown + structured JSON cache.

    Returns the output Markdown path on success, None on failure. Requires
    Firefox cookies for both fau.de and campo.fau.de.
    """
    result = _fetch_timetable_entries()
    if result is None:
        return None
    page_title, entries = result

    md = _render_timetable_markdown(page_title, entries)
    if output_path is None:
        output_path = os.path.join(DOWNLOAD_FOLDER, 'timetable.md')
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"✅ Timetable written to {output_path}")
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
                              max_flow: int = 12,
                              max_step: int = 30) -> List[Tuple[str, Dict]]:
    """Iterate Campo studyPlanner flowExecutionKeys (e<f>s<s>) reachable in the
    current Firefox session and return (flow_key, parsed_detail) for each
    Detailansicht with Zeiträume. Dedupes by module name.

    The student must have opened the relevant Modul/Prüfungs-Detailansichten in
    Firefox beforehand — flow execution keys are server-side per-session state.
    """
    base = CAMPO_STUDY_PLANNER_URL + '&_flowExecutionKey='
    seen_modules: set = set()
    results: List[Tuple[str, Dict]] = []
    for f in range(1, max_flow + 1):
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


def fetch_campo_exam_documents(output_dir: Optional[str] = None) -> Optional[List[str]]:
    """Download every PDF offered on campo's `personExamsReadonly` page
    (`examsOverviewForPerson-flow`) — Notenübersicht, Bescheinigungen,
    BAföG-§48, Angemeldete Prüfungen, etc.

    Each PDF button is a MyFaces non-AJAX form submit. The flow-execution
    key advances on every interaction, so we re-GET the form before each
    POST to get a fresh ViewState + key.

    Returns list of saved file paths, or None on failure.
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


def _run_tui_menu(debug: bool = False) -> None:
    """Interactive arrow-key menu — shown when no URL/flag is provided and stdin is a TTY."""
    global DOWNLOAD_FOLDER

    if not sys.stdin.isatty():
        print("No URL provided. Exiting.")
        return

    installed = _is_installed()
    install_label = ("✅ Uninstall cron job & shell function"
                     if installed else
                     "❗ Install cron job & shell function")
    install_value = "uninstall" if installed else "install"

    imap_installed = _is_imap_installed()
    imap_label = ("✅ Uninstall feedback-mail checker (FAUmail)"
                  if imap_installed else
                  "❗ Install feedback-mail checker (FAUmail)")
    imap_value = "uninstall_imap" if imap_installed else "install_imap"

    choices = [
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
        action = questionary.select("What would you like to do?", choices=choices).ask()
    else:
        labels = ["Register & download a course URL", "Dry-run all registered courses (preview new files)",
                  "Update all tracked courses", "Check FAUmail for feedback files now",
                  "Fetch timetable → timetable.md", "Map lecture schedule → tracked courses",
                  "Discover & register new courses from timetable",
                  "Set default download path", install_label, imap_label, "Exit"]
        values = ["url", "dry_run", "update_all", "check_feedback", "timetable", "map_lectures",
                  "discover_timetable",
                  "set_path", install_value, imap_value, "exit"]
        for i, label in enumerate(labels, 1):
            print(f"  {i}. {label}")
        try:
            idx = int(input("Choice: ").strip()) - 1
            action = values[idx] if 0 <= idx < len(values) else "exit"
        except (ValueError, EOFError):
            action = "exit"

    if action is None or action == "exit":
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
        n_processed, n_files = check_and_process_feedback(verbose=True)
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
        success, n_downloaded, n_extracted, session_expired = update_all_courses(debug=debug)
        if session_expired:
            recovered_session = _interactive_login_recovery()
            if recovered_session is not None:
                print("\n🔄 Retrying update with new session...\n")
                update_all_courses(debug=debug, session=recovered_session)
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
                       help='Install cron job and shell function (replaces setup_daily_sync.sh)')
    parser.add_argument('--install-skill', action='store_true',
                       help='(Re)write ~/.claude/skills/studon/SKILL.md from the inline source so Claude Code surfaces this scraper as a skill')
    parser.add_argument('--uninstall-skill', action='store_true',
                       help='Remove ~/.claude/skills/studon/SKILL.md')
    parser.add_argument('--timetable', action='store_true',
                       help='Fetch personal campo timetable and write to timetable.md')
    parser.add_argument('--campo-pruefungen', action='store_true',
                       help='Scan campo studyPlanner Detailansichten (opened in Firefox) and write pruefungen.md')
    parser.add_argument('--campo-bescheinigungen', action='store_true',
                       help='Download all PDFs from campo Notenübersicht / personExamsReadonly page (Notenübersicht, BAföG-§48, ord. Studium, angemeldete Prüfungen, ...) into <downloads>/Bescheinigungen/')
    parser.add_argument('--install-imap', action='store_true',
                       help='Configure FAUmail IMAP credentials (for feedback-file auto-download)')
    parser.add_argument('--uninstall-imap', action='store_true',
                       help='Remove FAUmail credentials and feedback queue state')
    parser.add_argument('--check-feedback', action='store_true',
                       help='Scan FAUmail inbox for StudOn feedback notifications and download any reachable PDFs')
    parser.add_argument('--reset-feedback-state', action='store_true',
                       help='Delete .studon_feedback_state.json (forces reprocessing of all matching emails)')

    args = parser.parse_args()

    # --- Timetable export ---
    if args.timetable:
        fetch_timetable_markdown()
        return

    # --- Campo Prüfungstermine / Anmeldefristen ---
    if args.campo_pruefungen:
        fetch_campo_pruefungen_markdown()
        return

    # --- Campo Notenübersicht / Bescheinigungen (PDF bulk download) ---
    if args.campo_bescheinigungen:
        fetch_campo_exam_documents()
        return

    # --- Claude Code skill registration ---
    if args.install_skill:
        _run_install_skill()
        return

    if args.uninstall_skill:
        _run_uninstall_skill()
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
        n_processed, n_files = check_and_process_feedback(verbose=True)
        print(f"Feedback: processed {n_processed} exercise(s), downloaded {n_files} file(s).")
        return

    # --- Install cron + bashrc ---
    if args.install:
        _run_install(check_interval=args.interval)
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
        success, n_downloaded, n_extracted, session_expired = update_all_courses(debug=args.debug)
        if session_expired and sys.stdin.isatty():
            recovered_session = _interactive_login_recovery()
            if recovered_session is not None:
                print("\n🔄 Retrying update with new session...\n")
                update_all_courses(debug=args.debug, session=recovered_session)
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

    # No explicit URL/flag: check clipboard, then TUI
    try:
        clip = pyperclip.paste().strip()
    except Exception:
        clip = ""
    if clip and _is_studon_url(clip):
        _run_clip_mode(debug=args.debug)
        return

    _run_tui_menu(debug=args.debug)


if __name__ == "__main__":
    main()