import os
import re
import sys
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
from dataclasses import dataclass
from tabulate import tabulate
from pathlib import Path
import logging
import yaml
import platform as platform_module

try:
    import py7zr
except ImportError:
    py7zr = None

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('studon_sync.log', encoding='utf-8'),
        logging.StreamHandler()
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
        yaml_data = {
            'course_title': self.course_title,
            'source_url': self.source_url,
            'last_fetched': self.last_fetched.isoformat(),
            'file_history': [record.to_dict(course_folder) for record in self.file_history]
        }

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

                        return cls(
                            course_title=yaml_data.get('course_title', 'Unknown Course'),
                            source_url=yaml_data.get('source_url', ''),
                            last_fetched=last_fetched,
                            file_history=file_history
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
DOWNLOAD_FOLDER = "studon_downloads"
STUDON_DOMAIN = 'studon.fau.de'
CONFIRMATION_THRESHOLD = int(os.getenv("CONFIRMATION_THRESHOLD", "50"))  # Ask for confirmation if more than this many files are found
RECENT_UPDATES_FILE = os.path.join(DOWNLOAD_FOLDER, "RECENT_UPDATES.md")

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
        logger.warning("  • Try running manually: python3 studon_scraper.py --update-all")
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

def find_all_metadata_files(base_folder: str) -> List[Tuple[str, str, str]]:
    """
    Finds all METADATA.md files in the download folder.
    Returns a list of tuples: (metadata_file_path, source_url, course_folder_path)
    """
    metadata_files = []

    for root, dirs, files in os.walk(base_folder):
        if "METADATA.md" in files:
            metadata_path = os.path.join(root, "METADATA.md")
            try:
                with open(metadata_path, 'r') as f:
                    content = f.read()
                    # Extract source URL from metadata
                    match = re.search(r'^Source:\s*(.+)$', content, re.MULTILINE)
                    if match:
                        source_url = match.group(1).strip()
                        course_folder = root
                        metadata_files.append((metadata_path, source_url, course_folder))
                        print(f"   Found: {metadata_path}")
                        print(f"      Source: {source_url}")
            except Exception as e:
                print(f"   ⚠️ Could not read {metadata_path}: {e}")

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
    """Removes characters that are illegal in file paths."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def extract_course_title(page_url: str, session: requests.Session, debug: bool = False) -> Optional[str]:
    """
    Extracts the course title from a StudOn page.
    Tries multiple common StudOn HTML patterns to find the title.
    """
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
                zip_ref.extractall(extract_dir)
            return True

        elif archive_path.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tgz', '.tbz2')):
            print(f"      📦 Extracting TAR: {filename}")
            with tarfile.open(archive_path, 'r:*') as tar_ref:
                tar_ref.extractall(extract_dir)
            return True

        elif archive_path.endswith('.7z'):
            if py7zr is None:
                print(f"      ⚠️ Skipping 7z file (py7zr not installed): {filename}")
                print(f"         Install it with: pip install py7zr")
                return False
            print(f"      📦 Extracting 7z: {filename}")
            with py7zr.SevenZipFile(archive_path, 'r') as archive:
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

        # Create HTML redirect file with meta-refresh (instant redirect)
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0; url={source_url}">
    <title>Redirecting to StudOn - {course_title}</title>
    <style>
        body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
        a {{ color: #0066cc; text-decoration: none; }}
    </style>
</head>
<body>
    <h2>Redirecting to StudOn...</h2>
    <p>Course: {course_title}</p>
    <p>If you are not redirected automatically, <a href="{source_url}">click here</a>.</p>
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

    # Load existing metadata using the new from_yaml_markdown method
    # This handles both YAML frontmatter and old markdown formats
    existing_metadata = CourseMetadata.from_yaml_markdown(metadata_path)

    existing_history: List[FileRecord] = []
    if existing_metadata:
        existing_history = existing_metadata.file_history
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
        file_history=all_history
    )

    try:
        with open(metadata_path, 'w', encoding='utf-8') as f:
            f.write(metadata.to_yaml_markdown(course_folder))
    except Exception as e:
        logger.error(f"Could not write metadata file: {e}")

    # Create clickable link file for easy browser access
    create_course_link_file(course_folder, course_title or 'Unknown Course', source_url)

# --- CORE LOGIC ---

def discover_items_recursive(page_url: str, current_path: str, session: requests.Session, file_list: List[Dict[str, str]], course_title: Optional[str] = None, debug: bool = False) -> None:
    """
    Recursively scans StudOn pages, identifying files and folders.
    It populates the `file_list` with all files it finds.
    course_title is stored in file_info for later use in metadata.
    """
    print(f"🔎 Scanning: {current_path}")
    try:
        response = session.get(page_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as e:
        print(f"   ❌ Could not access {page_url}. Error: {e}. Skipping.")
        return

    # Try multiple strategies to find items
    items = soup.find_all('div', class_='il_ContainerListItem')

    if debug or not items:
        print(f"   [DEBUG] Found {len(items)} items with class 'il_ContainerListItem'")
        # Try alternative selectors
        alt_items = soup.find_all('div', class_=re.compile(r'il.*ListItem'))
        print(f"   [DEBUG] Found {len(alt_items)} items matching 'il.*ListItem' pattern")

        # Look for any links that might be files
        all_links = soup.find_all('a', href=True)
        file_links = [link for link in all_links if 'cmd=sendfile' in link.get('href', '')]
        print(f"   [DEBUG] Found {len(file_links)} direct file download links")
        folder_links = [link for link in all_links if 'cmd=view' in link.get('href', '') and 'ref_id' in link.get('href', '')]
        print(f"   [DEBUG] Found {len(folder_links)} potential folder links")

    # If no items found with standard selector, try to find any downloadable files directly
    if not items:
        print(f"   [INFO] Using fallback strategy to find files and folders")
        all_links = soup.find_all('a', href=True)

        for link in all_links:
            href = link.get('href', '')
            link_text = link.get_text(strip=True)

            # Skip empty links or navigation links
            if not link_text or len(link_text) < 2:
                continue

            # Check if it's a file download link
            if 'cmd=sendfile' in href:
                item_url = urljoin(page_url, href)
                item_name = clean_filename(link_text)
                if item_name:
                    file_info: Dict[str, str] = {
                        'url': item_url,
                        'path': current_path,
                        'name': item_name,
                        'course_title': course_title or 'Unknown Course'
                    }
                    file_list.append(file_info)
                    print(f"   ✓ Found file: {item_name}")

            # Check if it's a folder link
            elif 'cmd=view' in href and 'ref_id' in href and link_text:
                # Skip if it looks like a navigation element
                if link_text.lower() in ['home', 'back', 'up', 'zurück', 'startseite']:
                    continue
                item_url = urljoin(page_url, href)
                item_name = clean_filename(link_text)
                if item_name:
                    new_path = os.path.join(current_path, item_name)
                    print(f"   ↳ Entering folder: {item_name}")
                    discover_items_recursive(item_url, new_path, session, file_list, course_title, debug)
        return

    for item in items:
        # Find the main link for the item
        link_tag = item.find('a', class_='il_ContainerItemTitle')
        if not link_tag:
            continue

        item_url: str = urljoin(page_url, link_tag['href'])
        item_name: str = clean_filename(link_tag.text)

        # Check if it's a folder by looking for the folder icon
        # StudOn uses an image with alt text 'Folder' or 'Ordner'
        # Look in the parent container for the icon
        parent_container = item.find_parent('div', class_='ilContainerListItemOuter')
        is_folder: bool = False
        if parent_container:
            is_folder = parent_container.find('img', alt=re.compile(r'Folder|Ordner', re.IGNORECASE))

        if is_folder:
            new_path: str = os.path.join(current_path, item_name)
            discover_items_recursive(item_url, new_path, session, file_list, course_title, debug)
        else:
            # Check if it's a file download link
            is_file: bool = "cmd=sendfile" in link_tag['href']
            if is_file:
                file_info: Dict[str, str] = {
                    'url': item_url,
                    'path': current_path,
                    'name': item_name,
                    'course_title': course_title or 'Unknown Course'
                }
                file_list.append(file_info)

def download_all_files(source: str, files_to_download: List[Dict[str, str]], session: requests.Session, course_title: Optional[str] = None, base_path: str = None) -> Tuple[int, List[str]]:
    """Downloads all files from the provided list.

    Returns:
        Tuple of (download_count, list_of_downloaded_filepaths)
    """
    if not files_to_download:
        return 0, []

    logger.info("-" * 50)
    logger.info(f"🚀 Starting download of {len(files_to_download)} files...")
    download_count: int = 0
    downloaded_files: List[str] = []
    downloaded_files_info: List[FileRecord] = []  # For logging

    # Use provided base_path or fall back to DOWNLOAD_FOLDER
    metadata_folder = base_path if base_path else DOWNLOAD_FOLDER
    metadata_path = os.path.join(metadata_folder, "METADATA.md")

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
                logger.debug(f"   ⏭️  Skipped (already exists): {existing_path}")
                continue

            # File doesn't exist, proceed with download
            logger.info(f"      Downloading: {expected_name}")
            file_response = session.get(file_url, stream=True)
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

            logger.info(f"   ✅ Downloaded: {filename}")
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
    # --- Extract Course Title ---
    print("\n--- Extracting Course Title ---")
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
                    print(f"✓ Using existing course title from metadata: {real_title}")

            # If still no title, use the folder name
            if not real_title and os.path.exists(base_download_path):
                folder_name = os.path.basename(base_download_path)
                if folder_name and folder_name != DOWNLOAD_FOLDER:
                    real_title = folder_name
                    print(f"✓ Using folder name as course title: {real_title}")

        # Use the real title if we found one
        if real_title:
            course_title = real_title
        else:
            # Last resort: keep the placeholder but warn
            print(f"⚠️ No existing course title found, using placeholder: {detected_placeholder}")

    if course_title:
        print(f"📚 Course: {course_title}")
    else:
        print("⚠️ Could not determine course title. Using default folder name.")
        if debug:
            logger.debug("Enable --debug to save HTML and see detailed title extraction attempts")
        course_title = None

    # --- Prepare download folder ---
    print("\n--- Preparing Download Folder ---")
    # Use provided base path or DOWNLOAD_FOLDER
    root_folder = base_download_path if base_download_path else DOWNLOAD_FOLDER
    os.makedirs(root_folder, exist_ok=True)
    print(f"📁 Download folder: {root_folder}")

    # Create course-specific subfolder if title was found and requested
    if course_title and create_course_subfolder:
        course_folder = os.path.join(root_folder, course_title)
        os.makedirs(course_folder, exist_ok=True)
        print(f"📁 Course folder: {course_folder}")
        final_download_path = course_folder
    else:
        final_download_path = root_folder

    # --- 1. Discovery Pass ---
    print("\n--- Starting Discovery Phase ---")
    all_files_to_download: List[Dict[str, str]] = []
    discover_items_recursive(start_url, final_download_path, session, all_files_to_download, course_title, debug=False)

    total_files: int = len(all_files_to_download)
    print("\n--- Discovery Complete ---")
    print(f"✅ Found a total of {total_files} file(s) across all folders.")

    if total_files == 0:
        print("No downloadable files found.")
        # Still update metadata and create link file even if no files found
        metadata_path = os.path.join(final_download_path, "METADATA.md")
        update_course_metadata(metadata_path, course_title, start_url, [])
        return 0, 0, []

    # --- 2. Download Pass ---
    num_downloaded, downloaded_files = download_all_files(start_url, all_files_to_download, session, course_title, final_download_path)

    print("-" * 50)
    print(f"🎉 Download completed. Successfully downloaded {num_downloaded}/{total_files} file(s).")

    # --- 3. Extract Archives Pass ---
    print("\n--- Starting Extraction Phase ---")
    num_extracted = 0

    # Only extract newly downloaded archives (never re-extract existing ones)
    if downloaded_files:
        archive_extensions = ('.zip', '.tar', '.tar.gz', '.tar.bz2', '.7z', '.tgz', '.tbz2')
        for filepath in downloaded_files:
            if filepath.lower().endswith(archive_extensions):
                if extract_archive(filepath):
                    num_extracted += 1

    if num_extracted > 0:
        print(f"✅ Successfully extracted {num_extracted} archive file(s).")
    else:
        print("ℹ️ No archives found to extract.")

    return num_downloaded, num_extracted, downloaded_files

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

def update_all_courses(debug: bool = False) -> bool:
    """Update all courses by scanning METADATA.md files. Returns True if successful."""
    try:
        logger.info("🔄 Update All Mode: Scanning for existing courses...")

        # Setup session with cookies
        try:
            cj = browser_cookie3.firefox(domain_name=STUDON_DOMAIN)
            session = requests.Session()
            session.cookies.update(cj)
            session.headers.update({'User-Agent': 'Mozilla/5.0'})
            logger.info("🍪 Firefox cookies loaded successfully.")
        except Exception as e:
            raise FirefoxCookieError(e)

        metadata_files = find_all_metadata_files(DOWNLOAD_FOLDER)

        if not metadata_files:
            logger.warning("No METADATA.md files found. Nothing to update.")
            return False

        logger.info(f"📚 Found {len(metadata_files)} course(s) to update.")

        total_downloaded = 0
        total_extracted = 0

        for i, (metadata_path, source_url, course_folder) in enumerate(metadata_files, 1):
            logger.info(f"📖 Course {i}/{len(metadata_files)}: {os.path.basename(course_folder)}")
            logger.info(f"   Source: {source_url}")

            try:
                downloaded, extracted, _ = process_single_url(source_url, session, course_folder, create_course_subfolder=False, debug=debug)
                total_downloaded += downloaded
                total_extracted += extracted
            except Exception as e:
                logger.error(f"Error processing {source_url}: {e}")
                continue

        logger.info(f"🎉 Update All Complete! Downloaded: {total_downloaded}, Extracted: {total_extracted}")

        # Return success if we processed at least one course
        return len(metadata_files) > 0

    except Exception as e:
        logger.error(f"Error during update: {e}")
        return False

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
        logger.info(f"Already updated today at {state.last_update}")
        logger.info("Exiting.")
        return

    logger.info("Daily sync mode started")
    logger.info(f"  Will check for StudOn login every {check_interval_seconds // 60} minutes")
    logger.info("  Will exit after successful daily sync")

    while True:
        try:
            if not can_access_studon():
                logger.debug("Waiting for StudOn login...")
                time.sleep(check_interval_seconds)
                continue

            logger.info("StudOn access verified, starting sync...")

            if update_all_courses():
                logger.info("Daily sync completed successfully!")
                logger.info("Exiting.")
                return
            else:
                logger.warning("Sync failed, will retry when StudOn login is available...")
                time.sleep(check_interval_seconds)

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Exiting.")
            return
        except Exception as e:
            logger.error(f"Error during daily sync: {e}")
            time.sleep(check_interval_seconds)

def main() -> None:
    """Main execution loop."""
    global DOWNLOAD_FOLDER

    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='StudOn Recursive File Downloader & Auto-Updater')
    parser.add_argument('url', nargs='?', help='StudOn URL to download from')
    parser.add_argument('download_path', nargs='?', help='Custom download path')
    parser.add_argument('--update-all', '-u', action='store_true',
                        help='Update all courses by scanning existing METADATA.md files')
    parser.add_argument('--daily-sync', action='store_true',
                       help='Wait for Firefox and perform daily sync, then exit (for @reboot cron)')
    parser.add_argument('--interval', '-i', type=int, default=5,
                       help='Check interval in minutes for --daily-sync (default: 5)')
    parser.add_argument('--debug', '-d', action='store_true',
                       help='Enable debug mode (saves HTML and shows detailed logging)')

    args = parser.parse_args()

    # Enable debug logging if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)
        logger.debug("Debug mode enabled")

    # Handle daily sync mode
    if args.daily_sync:
        check_interval_seconds = args.interval * 60
        run_daily_sync(check_interval_seconds=check_interval_seconds)
        return

    print("--- StudOn Recursive File Downloader ---")

    # --- Update All Mode ---
    if args.update_all:
        # Override DOWNLOAD_FOLDER if custom path was provided
        if args.download_path:
            DOWNLOAD_FOLDER = args.download_path
            logger.info(f"📁 Using custom download folder: {DOWNLOAD_FOLDER}")

        update_all_courses(debug=args.debug)
        return

    # --- Single URL Mode ---
    # --- Setup Session with Cookies ---
    try:
        print("\nAttempting to load browser cookies...")
        cj = browser_cookie3.firefox(domain_name=STUDON_DOMAIN)
        session = requests.Session()
        session.cookies.update(cj)
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        print("🍪 Firefox cookies loaded successfully.")
    except Exception as e:
        print(f"❌ An error occurred while loading Firefox cookies: {e}")
        print("   Please ensure you are logged into StudOn in Firefox.")
        return
    start_url = args.url
    custom_download_path = args.download_path

    if not start_url:
        start_url, custom_download_path = get_url_and_download_path_from_sources()

    if not start_url:
        print("No URL provided. Exiting.")
        return

    # Override DOWNLOAD_FOLDER if custom path was provided
    if custom_download_path:
        DOWNLOAD_FOLDER = custom_download_path
        print(f"📁 Using custom download folder: {DOWNLOAD_FOLDER}")

    # Process the single URL
    downloaded, extracted, files_list = process_single_url(start_url, session, DOWNLOAD_FOLDER, debug=args.debug)

    print("-" * 50)
    print(f"🎉 Process completed. Downloaded {downloaded} new file(s), extracted {extracted} archive(s).")
    print(f"📁 Files are organized in the '{DOWNLOAD_FOLDER}' folder with the same structure as StudOn.")

    # Display downloaded files
    if files_list:
        print("\n📥 Downloaded Files:")
        print("-" * 50)
        for filepath in files_list:
            # Show relative path from download folder for readability
            rel_path = os.path.relpath(filepath, DOWNLOAD_FOLDER)
            print(f"   • {rel_path}")


if __name__ == "__main__":
    main()