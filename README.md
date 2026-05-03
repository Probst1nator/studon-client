# studon-scraper

Authenticates to FAU's StudOn LMS via Firefox cookies, crawls course pages, and downloads/organises all subscribed course materials.
Runs as a daily background agent via cron.

---

## Platform Compatibility

| Platform | Status |
|----------|--------|
| Kubuntu / Ubuntu Linux | Fully tested |
| Other Linux distros | Should work; cron setup may differ |
| macOS | Manual launchd config required |
| Windows | Manual Task Scheduler config required |

Manual download mode works on all platforms. Automatic daily sync is verified on Ubuntu only.

---

## Prerequisites

- Firefox, logged into StudOn
- Python 3.8+

```bash
pip install -r requirements.txt
```

Optional — 7z archive support:
```bash
pip install py7zr
```

---

## Quick Setup

```bash
# 1. Clone into your preferred location
cd ~/Studium
git clone <repository-url> .

# 2. Install cron job + shell function
python3 studon_scraper.py --install
```

`--install` registers an `@reboot` cron entry, adds a `studon-scraper` shell
function to `~/.bashrc` (clipboard quick-fetch), and optionally persists a
download path. Re-run any time you move the directory.

Optional — set up the FAUmail feedback auto-downloader (see [Feedback files](#feedback-files)):

```bash
python3 studon_scraper.py --install-imap
```

---

## Usage

### First course download

Make sure Firefox is open and logged into StudOn, then:

```bash
# Interactive — detects StudOn URL from clipboard, or prompts
python studon_scraper.py

# Explicit URL
python studon_scraper.py "https://www.studon.fau.de/..."
```

### Daily auto-sync

Once the cron job is installed:

1. Log in and open Firefox when convenient.
2. The scraper detects Firefox, syncs all tracked courses, then exits.
3. Repeat next day — state is persisted in `.studon_updater_state.json`.

### Manual operations

```bash
# Refresh all tracked courses
python studon_scraper.py --update-all

# Preview new files without downloading
python studon_scraper.py <URL> --dry-run

# Clipboard quick-fetch (also available as the 'studon-scraper' shell function)
python studon_scraper.py --clip

# Export campo timetable to timetable.md
python studon_scraper.py --timetable

# Scan FAUmail for new feedback notifications and download PDFs
python studon_scraper.py --check-feedback

# Persist a default download path
python studon_scraper.py --set-download-path ~/Studium

# Check sync log
cat studon_sync.log
```

### Full command reference

Run `python studon_scraper.py --help` for the complete and current list. Key flags:

| Flag | Purpose |
|------|---------|
| `--update-all` | Refresh every tracked course |
| `--daily-sync` | Cron mode: wait for Firefox login, sync once, exit |
| `--clip` | Read clipboard, preview, confirm, download |
| `--dry-run` | Discover files without downloading |
| `--timetable` | Export personal campo timetable |
| `--install` / `--uninstall` | Install / remove cron entry + bashrc function |
| `--install-imap` / `--uninstall-imap` | Configure / remove FAUmail feedback checker |
| `--check-feedback` | Scan inbox now and download any reachable feedback PDFs |
| `--reset-feedback-state` | Clear `.studon_feedback_state.json` to reprocess all matching mails |
| `--set-download-path PATH` | Persist download path to `config.json` |
| `--debug` | Verbose logging, save discovery HTML |

### Interactive TUI

Run `python studon_scraper.py` with no arguments to get an arrow-key menu for all common operations (register course, update all, check feedback, install/uninstall, etc.).

---

## Configuration

Settings live in `config.json` next to the script (auto-created, gitignored):

```json
{
  "downloads_path": "/home/user/Studium",
  "imap_email": "you@fau.de"
}
```

| Key | Purpose |
|-----|---------|
| `downloads_path` | Output directory. Set via `--set-download-path` or during `--install`. |
| `imap_email` | FAUmail address for the feedback checker. Set via `--install-imap`. The matching password lives in your system keyring under service `studon-scraper-faumail`. |

Environment variable `CONFIRMATION_THRESHOLD` (default `50`) — prompts before batch-downloading more than N files.

---

## Output layout

```
studon_downloads/
├── .studon_updater_state.json   # sync state (cloud-sync safe)
├── RECENT_UPDATES.md            # last-run download log
├── <Course Name>/
│   ├── METADATA.md              # source URL + file history (YAML frontmatter)
│   └── <lecture folders>/
└── Feedback/                    # populated by --check-feedback
    └── <Course Name>/
        └── <Übungseinheit>/     # e.g. "Blatt 02"
            └── <feedback files>
```

The scraper **never deletes or overwrites** existing files. To re-download a file, remove or rename the local copy first.

---

## Feedback files

When an instructor uploads a feedback PDF on StudOn, the LMS sends an email of
the form `[StudOn] Es wurde eine neue Feedback-Datei zur Übung „…" hinzugefügt.`
The scraper can pick those up automatically:

1. `python3 studon_scraper.py --install-imap` — prompts for your FAU email and
   IDM password (stored in the system keyring, never on disk).
2. From then on, `--daily-sync` and `--check-feedback` scan all FAUmail folders
   for matching messages, queue their StudOn exc URLs in
   `.studon_feedback_state.json`, and download the PDFs into
   `<DOWNLOAD_FOLDER>/Feedback/<Course>/<Übungseinheit>/` whenever StudOn is
   reachable. Processed mails are flagged `\Seen`; URLs that can't be reached
   yet stay queued for the next run.

Remove with `--uninstall-imap` (clears the keyring entry, the email from
`config.json`, and the feedback queue state).

## Multi-device setup

Store this folder in any cloud sync service (Syncthing, OneDrive, Dropbox, etc.). Then on each device:

```bash
python3 studon_scraper.py --install
```

`studon_downloads/.studon_updater_state.json` syncs across devices — if one device has already synced today, others will skip.

---

## New semester

1. Log into StudOn in Firefox and enrol in new courses.
2. Download each new course once: `python studon_scraper.py "<url>"`
3. Daily sync tracks them automatically from then on.

Old course files from prior semesters are never touched.

---

## Manual cron setup

If `--install` doesn't fit your platform:

```bash
crontab -e
# Add:
@reboot cd /path/to/studon-scraper && /usr/bin/python3 studon_scraper.py --daily-sync >> studon_sync.log 2>&1
```

Or as a systemd user service — create `~/.config/systemd/user/studon-sync.service`:

```ini
[Unit]
Description=StudOn Daily Sync
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/studon-scraper
ExecStart=/usr/bin/python3 /path/to/studon-scraper/studon_scraper.py --daily-sync
StandardOutput=append:/path/to/studon-scraper/studon_sync.log
StandardError=append:/path/to/studon-scraper/studon_sync.log

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable studon-sync.service
systemctl --user start studon-sync.service
```

---

## Troubleshooting

**No files downloaded**
- Confirm Firefox is open and you are logged into StudOn.
- Try logging out of StudOn and back in to refresh cookies.
- Verify the course URL is accessible in your browser.

**Cron job not running**
```bash
crontab -l           # confirm entry exists
cat studon_sync.log  # check for errors
which python3        # confirm Python path matches cron entry
```

**Non-Ubuntu platform issues**
- Test manual mode first: `python studon_scraper.py <URL>`
- For macOS/Windows: use manual mode and configure scheduling separately.

**Run sync manually in background**
```bash
nohup python studon_scraper.py --daily-sync > studon_sync.log 2>&1 &
ps aux | grep studon_scraper          # check running
pkill -f "studon_scraper.py --daily-sync"  # stop
```

**Feature requests or unresolvable problems**
Open an issue on the [GitHub repository](https://github.com/AutomatedAlchemy/studon-scraper/issues).
