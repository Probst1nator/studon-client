# StudOn Auto-Downloader Setup

Complete installation and usage guide for the StudOn Auto-Downloader.

---

## ⚠️ Platform Compatibility Warning

### 🟢 Fully Tested & Supported
- **Kubuntu Linux** (Ubuntu with KDE Plasma desktop)
- **Ubuntu Linux** (all official flavors)

### 🟡 Untested - May Work With Manual Configuration
- **Other Linux distributions** (Debian, Arch, Fedora, etc.)
- **macOS** (requires manual launchd configuration)
- **Windows** (requires Task Scheduler or alternative scheduling)

### Important Notes
- ✅ **Manual download mode** (`python studon_scraper.py <URL>`) should work on all platforms
- ⚠️ **Automatic daily sync** requires platform-specific setup (only verified on Ubuntu)
- 🔧 The `setup_daily_sync.sh` script will detect your platform and warn you if untested
- 📝 For untested platforms, manual scheduling configuration may be needed

**New to Linux?** Check out [Kubuntu](https://kubuntu.org/getkubuntu/) - it's open-source, user-friendly, and provides a well-featured KDE desktop environment!

---

## Prerequisites

1. **Firefox Browser** - Must be logged into StudOn
2. **Python 3.8+**
3. **Required Python packages**:

```bash
pip install requests beautifulsoup4 pyperclip browser-cookie3
```

Optional (for 7z support):
```bash
pip install py7zr
```

## Quick Setup

1. Clone or download this repository to your preferred location:
```bash
cd ~/Studium  # Or wherever you want to store course materials
git clone <repository-url> .
```

2. Run the setup script:
```bash
bash setup_daily_sync.sh
```

3. That's it! The script will now run automatically when you log in.

## Manual Setup

If you prefer to set up manually, add this to your crontab:

```bash
# Edit crontab
crontab -e

# Add this line:
@reboot cd /path/to/Studium && /usr/bin/python3 studon_scraper.py --daily-sync >> studon_sync.log 2>&1
```

**Important**: Replace `/path/to/Studium` with your actual path.

## Usage

### First Time Setup

1. Make sure you're logged into StudOn in Firefox
2. Run the scraper manually to download your first course:

```bash
python studon_scraper.py
```

Paste a StudOn course URL when prompted, or copy it to clipboard first.

### Automatic Daily Sync

Once you've run the setup script, the sync happens automatically:

1. Log in to your computer
2. Open Firefox (when convenient)
3. The script detects Firefox and syncs all courses
4. New files are downloaded automatically
5. The script exits until tomorrow

### Manual Operations

**Download a specific course:**
```bash
python studon_scraper.py "https://www.studon.fau.de/..."
```

**Update all courses now:**
```bash
python studon_scraper.py --update-all
```

**View sync logs:**
```bash
cat studon_sync.log
```

## File Structure

```
Studium/
├── studon_scraper.py              # Main scraper script
├── setup_daily_sync.sh            # Setup script for cron
├── studon_sync.log                # Sync logs
├── studon_downloads/              # Downloaded course materials
│   ├── .studon_updater_state.json # Auto-updater state (synced across devices)
│   ├── Course Name 1/
│   │   ├── METADATA.md            # Course info and source URL
│   │   ├── Lecture 1/
│   │   └── Lecture 2/
│   └── Course Name 2/
│       ├── METADATA.md
│       └── ...
├── README.md                      # General FAU student resources
└── SETUP.md                       # Setup guide (this file)
```

## Multi-Device Setup

If you store this folder in a cloud sync service (Syncthing, OneDrive, Dropbox, Google Drive, etc.), the state and downloaded files can sync across devices automatically.

### On Each Device:

1. Wait for your cloud service to sync the folder
2. Run `bash setup_daily_sync.sh` on each device
3. Each device will now sync independently

**Note**: The `studon_downloads/.studon_updater_state.json` file syncs across devices, so if you sync on one device, other devices will know about it and won't re-download the same day.

## Command Reference
#### Manual Download Modes

```bash
# Interactive mode: Detects StudOn URL in clipboard (alternatively prompts for URL)
python studon_scraper.py
# → Downloads to: studon_downloads/Course Name/

# Update all existing courses (only fetches new files, never overwrites)
python studon_scraper.py --update-all
# → Updates courses in: studon_downloads/

# Display help page (perfect for manual customization)
python studon_scraper.py -h
```

#### Help Page

Run `python studon_scraper.py -h` to see all available options:

```
usage: studon_scraper.py [-h] [--update-all] [--daily-sync]
                         [--interval INTERVAL] [--debug]
                         [url] [download_path]

StudOn Recursive File Downloader & Auto-Updater

positional arguments:
  url                   StudOn URL to download from
  download_path         Custom download path

options:
  -h, --help            show this help message and exit
  --update-all, -u      Update all courses by scanning existing METADATA.md
                        files
  --daily-sync          Wait for Firefox and perform daily sync, then exit
                        (for @reboot cron)
  --interval INTERVAL, -i INTERVAL
                        Check interval in minutes for --daily-sync (default:
                        5)
  --debug, -d           Enable debug mode (saves HTML and shows detailed
                        logging)
```

## Troubleshooting

### Platform-specific issues

**Problem**: Script runs but doesn't work as expected on non-Ubuntu systems.

**Solutions**:
1. Run the manual download mode first to test: `python studon_scraper.py <URL>`
2. Check the log file for platform warnings: `cat studon_sync.log`
3. For Windows/macOS: Focus on manual mode, set up scheduling separately
4. For other Linux distros: The script should work, but cron setup may differ
5. Consider contributing platform-specific fixes to the project!

### No files are being downloaded

**Problem**: The script runs but finds 0 files.

**Solutions**:
1. Make sure you're logged into StudOn in Firefox
2. Try logging out and back in to refresh cookies
3. Check if the course URL is accessible in your browser

### Cron job not running

**Problem**: Script doesn't start on login.

**Solutions**:
1. Check crontab: `crontab -l`
2. Check logs: `cat studon_sync.log`
3. Verify the path in the cron job is correct
4. Make sure Python 3 is at `/usr/bin/python3` (check with `which python3`)

## Advanced Usage

### Run in Background Manually

```bash
# Start daily sync in background
nohup python studon_scraper.py --daily-sync > studon_sync.log 2>&1 &

# Check if it's running
ps aux | grep "studon_scraper.py --daily-sync"

# Kill it
pkill -f "studon_scraper.py --daily-sync"
```

### Systemd Service (Alternative to Cron)

Create `/etc/systemd/user/studon-sync.service`:

```ini
[Unit]
Description=StudOn Daily Sync
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/Studium
ExecStart=/usr/bin/python3 /path/to/Studium/studon_scraper.py --daily-sync
StandardOutput=append:/path/to/Studium/studon_sync.log
StandardError=append:/path/to/Studium/studon_sync.log

[Install]
WantedBy=default.target
```

Enable it:
```bash
systemctl --user enable studon-sync.service
systemctl --user start studon-sync.service
```

## New Semester Setup

At the start of each semester:
1. Log into StudOn in Firefox and enroll in your new courses
2. Download each new course once: `python studon_scraper.py "<course-url>"`
3. From then on, the daily sync will automatically check all courses for new files

**Note:** The scraper NEVER deletes old files. It only downloads files that don't already exist locally at the target filepath. Your old course materials from previous semesters stay untouched. (Warning: This means files are *never updated* once downloaded — even if modified on StudOn. To redownload a file, rename, move or delete the old one before running the scraper again.)