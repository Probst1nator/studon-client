# studon-client

Authenticates to FAU's StudOn LMS via Firefox cookies, crawls course pages, and downloads/organises all subscribed course materials.

Runs two background agents via `@reboot` cron:
- **Daily sync** — waits for Firefox login, refreshes *all* tracked courses once per day.
- **Lecture sync** — long-running daemon that fetches *only* the course relevant to each lecture at start − 5 min, start, and start + 5 min, driven by your personal campo timetable. Force-opens Firefox via a tray icon if cookies are missing. Avoids rate-limiting by never touching more than the one course you're about to walk into.

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
python3 studon_client.py --install
```

`--install` registers both `@reboot` cron entries (`--daily-sync` and
`--lecture-sync`), adds a `studon-client` shell function to `~/.bashrc`
(clipboard quick-fetch), optionally persists a download path, and runs the
interactive `--map-lectures` wizard so every campo timetable entry is paired
with a tracked course (or explicitly marked "no StudOn course") before the
lecture-sync daemon starts. Re-run any time you move the directory or
register a new course.

Optional — set up the FAUmail feedback auto-downloader (see [Feedback files](#feedback-files)):

```bash
python3 studon_client.py --install-imap
```

---

## Usage

### First course download

Make sure Firefox is open and logged into StudOn, then:

```bash
# Interactive — detects StudOn URL from clipboard, or prompts
python studon_client.py

# Explicit URL
python studon_client.py "https://www.studon.fau.de/..."
```

### Daily auto-sync

Once the cron job is installed:

1. Log in and open Firefox when convenient.
2. The scraper detects Firefox, syncs all tracked courses, then exits.
3. Repeat next day — state is persisted in `.studon_updater_state.json`.

### Per-lecture sync

`--install` also registers the `--lecture-sync` daemon. It reads your campo
timetable (refreshed daily) and fires three single-course fetches per
lecture: 5 min before start, at start, and 5 min after. If Firefox cookies
have expired, a tray icon appears once per fire with an **Open StudOn
login** option — click it to launch Firefox at StudOn; the daemon picks
back up automatically. If you don't log in within ~2 min the fire is
skipped silently and the next fire tries again.

Each campo timetable entry is bucketed in priority order:

| Bucket | What it means |
|---|---|
| **Mapped (explicit)** | The verbatim campo title is listed in some course's `METADATA.md` `timetable_titles`. |
| **Mapped (normalized)** | Folder names with cosmetic differences (e.g. `SoSe 2026 - <name>`) auto-match the campo title; the verbatim title is then pinned into that course's METADATA so the next run is `explicit`. |
| **No-course** | Listed in `lecture_mapping.json::no_course_titles` — silently skipped. |
| **Unmapped** | None of the above — emits a daily `notify-send` warning. Run `--map-lectures` to fix. |

Run `--map-lectures` whenever you enroll in a new course, change semester,
or see an "unmapped" warning:

```bash
python studon_client.py --map-lectures
```

For each unmapped entry it asks: **Link to which tracked course?**,
**Mark as no StudOn course?**, or **Skip for now?** — and writes either the
course's METADATA.md or `lecture_mapping.json` accordingly.

If the StudOn course isn't tracked yet at all, use `--discover-from-timetable`
instead: it walks every Unmapped campo entry, programmatically "clicks" the
campo *Detailansicht* button (JSF form POST), follows the campo→studon proxy
link to its final ILIAS URL, and offers to register that course as a new
tracked course — saving you from pasting URLs by hand.

```bash
python studon_client.py --discover-from-timetable
```

The verbatim timetable title is pinned into the new course's METADATA so the
next `--map-lectures` / `--lecture-sync` run sees it as `Mapped (explicit)`.

For a quick sanity check without running the daemon:

```bash
python studon_client.py --lecture-sync-once
# Prints resolved buckets and the next 5 fires, then exits.
```

### Manual operations

```bash
# Refresh all tracked courses
python studon_client.py --update-all

# Preview new files without downloading
python studon_client.py <URL> --dry-run

# Clipboard quick-fetch (also available as the 'studon-client' shell function)
python studon_client.py --clip

# Export campo timetable to timetable.md
python studon_client.py --timetable

# Export a NON-current semester's timetable (e.g. Wintersemester 2026/27) →
# timetable_WS2627.md; --term accepts the campo-search style 'eq|<season>|<year>'
# (season 1 = Sommer-, 2 = Wintersemester) or a raw campo option id (e.g. 590).
# The current-semester timetable.md and the --lecture-sync cache are left untouched.
python studon_client.py --timetable --term 'eq|2|2026'

# Scan campo studyPlanner front page (deterministic, no Pre-Click) → Modulplan.md
# Lists every module in the Studienplan with Status / Semester / Versuch / ECTS-erreicht / ECTS-Soll,
# Studienfortschritt-Header (Bestanden/180 ECTS) + 3 status-grouped tables.
python studon_client.py --modulplan

# Scan campo Belegungen page (deterministic, no Pre-Click) → Belegungen.md + Belegungen.json
# Lists all angemeldeten Prüfungen (Nr/Titel/Termin/Form/Prüfer/Status) + Veranstaltungen
# (Typ/Titel/Termin+Raum/Dozent) for the currently selected semester. Pure data fetch.
# Change-detection + notify-send lives in the sibling `belegungen-watcher` tool.
python studon_client.py --belegungen

# Cross-check Modulplan ↔ Belegungen → Reconciliation.md
# Resolves the lernplan.md / Prüfungen.md ECTS-Diskrepanz by listing:
# (1) Belegungen-Prüfungen mit Modulplan-Modul-Match,
# (2) Modulplan-Angemeldet ohne Belegung,
# (3) Bestanden ohne ECTS-Suffix (= ECTS-Undercount).
python studon_client.py --reconcile

# Search campo's Lehrveranstaltungssuche for a term (default: current semester) → stdout
# Prints LV-Treffer + ECTS. Also runnable standalone: python campo_search.py "<query>" [--ects] [--json]
python studon_client.py --campo-search "Künstliche Intelligenz" [--term 'eq|1|2026']

# Scan campo studyPlanner Prüfungs-Detailansichten (opened in Firefox) → pruefungen.md
# Zeiträume live nur auf Prüfungs-Detail views, daher Pre-Click required.
python studon_client.py --campo-pruefungen

# Bulk-download Notenübersicht / Bescheinigungen PDFs (exam-side, 12 PDFs)
python studon_client.py --campo-bescheinigungen
# Combo: 12 + 7 = 19 PDFs (exam-side + enrollment-side)
python studon_client.py --campo-bescheinigungen --with-enrollment
# Enrollment-side only (7 PDFs)
python studon_client.py --campo-enrollment-bescheinigungen [--dry-run]

# Scan FAUmail for new feedback notifications and download PDFs
python studon_client.py --check-feedback

# Persist a default download path
python studon_client.py --set-download-path ~/Studium

# Check sync log
cat studon_sync.log
```

### Full command reference

Run `python studon_client.py --help` for the complete and current list. Key flags:

| Flag | Purpose |
|------|---------|
| `--update-all` | Refresh every tracked course |
| `--daily-sync` | Cron mode: wait for Firefox login, sync all courses once, exit |
| `--lecture-sync` | Long-running daemon: per-lecture single-course fetches driven by campo timetable |
| `--lecture-sync-once` | Print resolved campo↔course buckets and the next 5 fires, then exit (testing) |
| `--map-lectures` | Interactive wizard: link unmapped campo entries to courses, or mark them as no-course |
| `--discover-from-timetable` | For each Unmapped campo entry, follow the JSF "Detailansicht" button → final StudOn URL and offer to register it as a tracked course |
| `--clip` | Read clipboard, preview, confirm, download |
| `--dry-run` | Discover files without downloading |
| `--timetable` | Export personal campo timetable → `timetable.md` + `.timetable_entries.json` cache (current semester). |
| `--timetable --term '<TERMID>'` | Export a **non-current** semester's timetable → `timetable_<label>.md` (e.g. `timetable_WS2627.md`). `<TERMID>` is the campo-search style `eq\|<season>\|<year>` (season 1 = Sommer-, 2 = Wintersemester; e.g. `eq\|2\|2026` = WiSe 2026/27) or a raw campo option id (e.g. `590`). Resolves season+year against the changeTerm select's option labels (no hardcoded IDs), then switches to the Vorlesungszeitansicht via two full-form POSTs. Leaves the current-semester `timetable.md` and the `--lecture-sync` cache untouched. |
| `--modulplan` | Scan studyPlanner-flow front page (deterministic, no Pre-Click) → `Modulplan.md`: every module in the Studienplan with Nr / Titel / Status / Semester / Versuch / ECTS-erreicht/-Soll, Studienfortschritt-Header (Bestanden gegen 180 ECTS-Soll), und drei Status-gruppierte Tabellen (Bestanden / Angemeldet / Offen). ECTS comes from the `X/Y`-suffix on each `modulePlanItem` div — no extra HTTP request. |
| `--belegungen` | Scan searchOwnEnrollmentInfo-flow front page (deterministic, no Pre-Click) → `Belegungen.md` + `Belegungen.json`: angemeldete Prüfungen (Nr / Titel / Termin / Form / Prüfer/-in / Status) + Veranstaltungen (Typ / Titel / Termin+Raum / Dozent/-in) für das aktuell ausgewählte Semester. Multi-Termin-Vorlesungen werden mit `<br>` getrennt. **Pure data fetch** — Change-Detection + `notify-send` ist in das Schwester-Tool `~/Synced/repos/AutomatedAlchemy/belegungen-watcher/main.py` ausgelagert, das die JSON konsumiert. |
| `--reconcile` | Cross-Check Modulplan ↔ Belegungen → `Reconciliation.md` + `Reconciliation.json` (gleicher Inhalt maschinen-lesbar) mit (1) Belegungen-Prüfungen → Modulplan-Modul-Match (Title-Normalize + Jaccard ≥ 0.6), (2) Modulplan-Angemeldet ohne Belegungs-Eintrag, (3) Bestanden-Module ohne `X/Y`-Suffix (= ECTS-Undercount-Quelle). Wenn eine `Notenübersicht*Module*.pdf` unter `Bescheinigungen/` existiert (Auto-Detection via `pdftotext -layout`), wird sie als **kanonische ECTS-Quelle** integriert (Prüfungsamt-Berechnung, BAföG-/Kindergeld-relevant) — Diskrepanz zum Modulplan-Front-Page wird automatisch sichtbar gemacht. Der `--install`-Cron pflegt die PDF wöchentlich (Mo 06:30) via `--campo-bescheinigungen`. |
| `--campo-search "<query>" [--term 'eq\|1\|2026']` | Search campo's Lehrveranstaltungssuche (searchCourseNonStaff-flow) for a term (default: current semester) and print LV-Treffer + ECTS to stdout. Flow mechanics live in the standalone, unit-runnable `campo_search.py` (`python campo_search.py "<query>" [--ects] [--json]`). |
| `--campo-pruefungen` | Parse campo studyPlanner Prüfungs-Detailansichten (must be pre-opened in Firefox; flow-key scan now adaptive up to e99) → `pruefungen.md`. Zeiträume (Anmelde-/Abmelde-/Prüfungszeitraum) only render on per-Prüfung Detail views, not on the deterministic Modul-Detail views. |
| `--campo-bescheinigungen` | Download all 12 exam-side PDFs from `personExamsReadonly.xhtml` into `<downloads>/Bescheinigungen/` |
| `--campo-bescheinigungen --with-enrollment` | 12 + 7 = 19 PDFs (combo with the enrollment-side) |
| `--campo-enrollment-bescheinigungen` | Download all 7 enrollment-side PDFs via `studyservice-flow` into `<downloads>/Bescheinigungen/Enrollment/`: Benutzerinfobrief, Bescheinigung §9 BAföG, Datenkontrollblatt, Quittung (einzelnes Semester), Beitragskonto, Immatrikulationsbescheinigung, Studienverlaufsbescheinigung. Parameterized reports default to the current semester. |
| `--install` / `--uninstall` | Install / remove cron entries + bashrc function |
| `--install-imap` / `--uninstall-imap` | Configure / remove FAUmail feedback checker |
| `--check-feedback` | Scan inbox now and download any reachable feedback PDFs |
| `--reset-feedback-state` | Clear `.studon_feedback_state.json` to reprocess all matching mails |
| `--set-download-path PATH` | Persist download path to `config.json` |
| `--tray` | Show the StudOn tray icon again after "Tray schliessen" and exit. Closing the tray sets `tray_closed` in `~/.local/state/studon-client/tray_status.json`, which stops both daemons from relaunching it and silences the login popups until the next successful StudOn login. |
| `--debug` | Verbose logging, save discovery HTML |

### Interactive TUI

Run `python studon_client.py` with no arguments to get an arrow-key menu for all common operations (register course, update all, check feedback, install/uninstall, etc.).

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
├── .studon_updater_state.json     # daily-sync state (cloud-sync safe)
├── .studon_sync.lock              # PID-file shared by --daily-sync and --lecture-sync
├── .timetable_entries.json        # structured campo cache consumed by --lecture-sync
├── timetable.md                   # human-readable campo timetable (current semester)
├── timetable_<label>.md           # non-current semester (--timetable --term), e.g. timetable_WS2627.md
├── pruefungen.md                  # Prüfungs-Anmeldefristen aus campo studyPlanner Detailansichten
├── RECENT_UPDATES.md              # last-run download log
├── <Course Name>/
│   ├── METADATA.md                # source URL + file history + timetable_titles (YAML frontmatter)
│   └── <lecture folders>/
└── Feedback/                      # populated by --check-feedback
    └── <Course Name>/
        └── <Übungseinheit>/       # e.g. "Blatt 02"
            └── <feedback files>
```

`lecture_mapping.json` lives next to the script (alongside `config.json`),
not in the download folder. It stores only the *negative* side of the
campo↔course mapping (`no_course_titles`, `ignored_titles`) — the positive
side is each course's `timetable_titles` inside its own `METADATA.md`.

The scraper **never deletes or overwrites** existing files. To re-download a file, remove or rename the local copy first.

---

## Feedback files

When an instructor uploads a feedback PDF on StudOn, the LMS sends an email of
the form `[StudOn] Es wurde eine neue Feedback-Datei zur Übung „…" hinzugefügt.`
The scraper can pick those up automatically:

1. `python3 studon_client.py --install-imap` — prompts for your FAU email and
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
python3 studon_client.py --install
```

`studon_downloads/.studon_updater_state.json` syncs across devices — if one device has already synced today, others will skip.

---

## New semester

1. Log into StudOn in Firefox and enrol in new courses.
2. Download each new course once: `python studon_client.py "<url>"`
3. Refresh campo timetable: `python studon_client.py --timetable`
   - For Studienfortschritt / ECTS-Bilanz: `python studon_client.py --modulplan` → `Modulplan.md` (deterministic, no Pre-Click).
   - For exam-registration deadlines: open each Prüfungs-Detailansicht in Firefox once, then `python studon_client.py --campo-pruefungen` → `pruefungen.md`.
4. Run `python studon_client.py --map-lectures` to link new timetable
   entries to the new course folders (or mark them as no-course).
5. Daily sync + lecture sync track them automatically from then on.

Old course files from prior semesters are never touched.

---

## Manual cron setup

If `--install` doesn't fit your platform:

```bash
crontab -e
# Add both:
@reboot cd /path/to/studon-client && /usr/bin/python3 studon_client.py --daily-sync >> studon_sync.log 2>&1
@reboot cd /path/to/studon-client && /usr/bin/python3 studon_client.py --lecture-sync >> studon_sync.log 2>&1
```

Or as a systemd user service — create `~/.config/systemd/user/studon-sync.service`:

```ini
[Unit]
Description=StudOn Daily Sync
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/studon-client
ExecStart=/usr/bin/python3 /path/to/studon-client/studon_client.py --daily-sync
StandardOutput=append:/path/to/studon-client/studon_sync.log
StandardError=append:/path/to/studon-client/studon_sync.log

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
- Test manual mode first: `python studon_client.py <URL>`
- For macOS/Windows: use manual mode and configure scheduling separately.

**Run sync manually in background**
```bash
nohup python studon_client.py --daily-sync > studon_sync.log 2>&1 &
ps aux | grep studon_client          # check running
pkill -f "studon_client.py --daily-sync"  # stop
```

**Feature requests or unresolvable problems**
Open an issue on the [GitHub repository](https://github.com/AutomatedAlchemy/studon-client/issues).
