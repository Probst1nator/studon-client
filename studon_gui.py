"""Tkinter course dashboard for studon-client.

Opened by `studon_client.py --gui`, by a bare invocation in a graphical session,
by the tray's "Fenster oeffnen" item and by the KDE launcher that `--install`
writes.

The caller passes its own module object in: `run(sys.modules[__name__])`.
This file must not `import studon_client`. When the script runs as __main__,
that import would load it a second time, with a second logging setup and a
second DOWNLOAD_FOLDER global.

Jobs call the existing functions of studon_client unchanged. Their print()
output and log records are routed into the Log tab. Actions that read from
stdin (--map-lectures, --discover-from-timetable, --install, --install-imap)
open in a terminal window instead.
"""
import configparser
import contextlib
import io
import logging
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_TITLE = "StudOn Client"
NEW_FILE_DAYS = 7
STATUS_REFRESH_MS = 10_000
QUEUE_POLL_MS = 100
LOG_MAX_LINES = 5000
FILES_MAX_ROWS = 500

LIGHT = {"bg": "#f5f6f8", "panel": "#ffffff", "fg": "#1f2328", "muted": "#5b6470",
         "accent": "#1f6feb", "accent_fg": "#ffffff", "select": "#cfe0fb", "border": "#d0d7de"}
DARK = {"bg": "#1e2126", "panel": "#262a30", "fg": "#e6e8eb", "muted": "#9aa3ad",
        "accent": "#4c9aff", "accent_fg": "#0d1117", "select": "#2d4a73", "border": "#3a3f46"}


class _SessionExpired(Exception):
    """No usable StudOn session in Firefox; the dashboard offers a login."""


@dataclass
class _Course:
    title: str
    source_url: str
    folder: str
    last_fetched: Optional[datetime]
    records: list = field(default_factory=list)  # FileRecord, newest first
    timetable_titles: List[str] = field(default_factory=list)
    n_new: int = 0


class _QueueWriter(io.StringIO):
    """File-like object that forwards writes to the log queue."""

    def __init__(self, q):
        super().__init__()
        self._q = q

    def write(self, s):
        if s:
            self._q.put(s)
        return len(s)


class _QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__(level=logging.INFO)
        self._q = q
        self.setFormatter(logging.Formatter("· %(message)s"))

    def emit(self, record):
        try:
            self._q.put(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


def _naive(ts: datetime) -> datetime:
    return ts.astimezone().replace(tzinfo=None) if ts.tzinfo else ts


def _humanize_age(epoch) -> str:
    """Relative age of a unix timestamp, German short form (same as the tray)."""
    try:
        delta = max(0, int(time.time() - float(epoch)))
    except (TypeError, ValueError):
        return ""
    if delta < 60:
        return "gerade eben"
    if delta < 3600:
        return f"vor {delta // 60} Min"
    if delta < 86400:
        return f"vor {delta // 3600} Std"
    return f"vor {delta // 86400} Tg"


def _kde_is_dark() -> bool:
    """True when the KDE colour scheme has a dark window background."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(os.path.expanduser("~/.config/kdeglobals"), encoding="utf-8")
        r, g, b = (int(x) for x in cp.get("Colors:Window", "BackgroundNormal").split(",")[:3])
    except Exception:
        return False
    return 0.299 * r + 0.587 * g + 0.114 * b < 128


def _open_path(path: str) -> None:
    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _scan_courses(sc) -> List[_Course]:
    """Read every tracked course's METADATA.md (takes a few seconds; run off the Tk thread)."""
    cutoff = datetime.now() - timedelta(days=NEW_FILE_DAYS)
    courses = []
    for meta_path, source_url, folder in sc.find_all_metadata_files(sc.DOWNLOAD_FOLDER):
        cm = sc.CourseMetadata.from_yaml_markdown(meta_path)
        if cm is None:
            courses.append(_Course(os.path.basename(folder), source_url, folder, None))
            continue
        title = cm.course_title if cm.course_title != "Unknown Course" else os.path.basename(folder)
        records = sorted(cm.file_history, key=lambda r: _naive(r.timestamp), reverse=True)
        courses.append(_Course(
            title=title,
            source_url=source_url,
            folder=folder,
            last_fetched=_naive(cm.last_fetched) if cm.last_fetched else None,
            records=records,
            timetable_titles=list(cm.timetable_titles),
            n_new=sum(1 for r in records if _naive(r.timestamp) >= cutoff),
        ))
    # Feedback folders all carry the exercise's title ("Hausaufgabenabgabe"),
    # so a shared title is replaced by the folder path below the download root,
    # innermost folder first ("Blatt 01 · <course> · Feedback").
    counts = {}
    for c in courses:
        counts[c.title] = counts.get(c.title, 0) + 1
    for c in courses:
        if counts[c.title] > 1:
            parts = os.path.relpath(c.folder, sc.DOWNLOAD_FOLDER).split(os.sep)
            c.title = " · ".join(reversed(parts))
    return courses


class Dashboard:
    # (key, heading, sample text that sets the initial column width)
    COLUMNS = (("title", "Kurs", "M" * 22), ("fetched", "Letzter Abruf", "00.00.0000 00:00"),
               ("files", "Dateien", "0000"), ("new", f"Neu ({NEW_FILE_DAYS} Tage)", "000"),
               ("timetable", "Stundenplan-Titel", "M" * 16))

    def __init__(self, root: tk.Tk, sc, preselect_folder: Optional[str] = None):
        self.root = root
        self.sc = sc
        self.q: "queue.Queue" = queue.Queue()
        self.busy = False
        self.holding_lock = False
        self.courses: List[_Course] = []
        self._by_iid = {}
        self._preselect = os.path.abspath(preselect_folder) if preselect_folder else None
        self._sort = ("title", False)
        self.pal = DARK if _kde_is_dark() else LIGHT

        self._apply_style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.reload_courses()
        self._poll_queue()
        self._refresh_status()

    # ------------------------------------------------------------------ layout

    def _apply_style(self):
        p = self.pal
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(bg=p["bg"])
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.title_font = tkfont.nametofont("TkDefaultFont").copy()
        self.title_font.configure(weight="bold", size=self.title_font.cget("size") + 3)
        style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["panel"],
                        bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                        troughcolor=p["bg"], focuscolor=p["accent"],
                        selectbackground=p["select"], selectforeground=p["fg"],
                        insertcolor=p["fg"])
        style.configure("TButton", padding=(10, 4), background=p["panel"])
        style.map("TButton", background=[("disabled", p["bg"]), ("active", p["select"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"])
        style.map("Accent.TButton", background=[("disabled", p["bg"]), ("active", p["accent"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("TMenubutton", padding=(10, 4), background=p["panel"])
        style.map("TMenubutton", background=[("disabled", p["bg"]), ("active", p["select"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("Treeview", background=p["panel"], fieldbackground=p["panel"],
                        foreground=p["fg"], rowheight=24, bordercolor=p["border"])
        style.map("Treeview", background=[("selected", p["select"])],
                  foreground=[("selected", p["fg"])])
        style.configure("Treeview.Heading", background=p["bg"], foreground=p["muted"],
                        relief="flat", padding=(6, 4))
        style.map("Treeview.Heading", background=[("active", p["select"])])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"], padding=(12, 4))
        style.map("TNotebook.Tab", background=[("selected", p["panel"])],
                  foreground=[("selected", p["fg"])])
        style.configure("Muted.TLabel", foreground=p["muted"])
        style.configure("Title.TLabel", font=self.title_font)
        style.configure("Bold.TLabel", font=self.bold)
        style.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["bg"])
        style.configure("Vertical.TScrollbar", background=p["border"], troughcolor=p["panel"],
                        bordercolor=p["panel"], arrowcolor=p["muted"], gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", p["muted"])])

    def _menu(self, parent, **kw) -> tk.Menu:
        p = self.pal
        return tk.Menu(parent, tearoff=False, bg=p["panel"], fg=p["fg"],
                       activebackground=p["select"], activeforeground=p["fg"],
                       disabledforeground=p["muted"], **kw)

    def _build(self):
        root = self.root
        root.title(APP_TITLE)
        root.geometry("1150x740")
        root.minsize(820, 520)
        icon = Path(self.sc.__file__).resolve().parent / "assets" / "studon-client.png"
        if icon.exists():
            try:
                self._icon = tk.PhotoImage(file=str(icon))
                root.iconphoto(True, self._icon)
            except tk.TclError:
                pass

        # --- Header: title, background-daemon status, download folder ---
        head = ttk.Frame(root, padding=(12, 10, 12, 4))
        head.pack(fill="x")
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        info = ttk.Frame(head, padding=(16, 0, 0, 0))
        info.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar()
        self.last_var = tk.StringVar()
        ttk.Label(info, textvariable=self.status_var, style="Bold.TLabel").pack(anchor="w")
        ttk.Label(info, textvariable=self.last_var, style="Muted.TLabel").pack(anchor="w")
        self.login_btn = ttk.Button(head, text="In StudOn einloggen", command=self._open_login)

        # --- Toolbar: actions over all courses ---
        bar = ttk.Frame(root, padding=(12, 4, 12, 8))
        bar.pack(fill="x")
        self.btn_update_all = ttk.Button(bar, text="Alle aktualisieren", style="Accent.TButton",
                                         command=self.update_all)
        self.btn_preview_all = ttk.Button(bar, text="Vorschau", command=self.preview_all)
        self.btn_add = ttk.Button(bar, text="Kurs hinzufügen …", command=self.add_course)
        self.btn_feedback = ttk.Button(bar, text="Feedback prüfen", command=self.check_feedback)
        for b in (self.btn_update_all, self.btn_preview_all, self.btn_add, self.btn_feedback):
            b.pack(side="left", padx=(0, 6))

        self.mb_timetable = ttk.Menubutton(bar, text="Stundenplan")
        m = self._menu(self.mb_timetable)
        m.add_command(label="Stundenplan von campo holen", command=self.fetch_timetable)
        m.add_command(label="timetable.md öffnen", command=self._open_timetable)
        m.add_separator()
        m.add_command(label="Vorlesungen zuordnen (Terminal)",
                      command=lambda: self._run_in_terminal("--map-lectures"))
        m.add_command(label="Kurse aus Stundenplan entdecken (Terminal)",
                      command=lambda: self._run_in_terminal("--discover-from-timetable"))
        self.mb_timetable["menu"] = m
        self.mb_timetable.pack(side="left", padx=(0, 6))

        self.mb_settings = ttk.Menubutton(bar, text="Einstellungen")
        self.settings_menu = self._menu(self.mb_settings, postcommand=self._fill_settings_menu)
        self.mb_settings["menu"] = self.settings_menu
        self.mb_settings.pack(side="left")

        ttk.Button(bar, text="Neu laden", command=self.reload_courses).pack(side="right")

        # --- Footer: current job + progress ---
        foot = ttk.Frame(root, padding=(12, 4, 12, 8))
        foot.pack(side="bottom", fill="x")
        self.job_var = tk.StringVar(value="Bereit")
        ttk.Label(foot, textvariable=self.job_var, style="Muted.TLabel").pack(side="left")
        self.folder_btn = ttk.Button(foot, command=lambda: _open_path(self.sc.DOWNLOAD_FOLDER))
        self.folder_btn.pack(side="right")
        self.progress = ttk.Progressbar(foot, mode="indeterminate", length=160)

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=12)

        # --- Course table ---
        top = ttk.Frame(paned)
        cols = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse", height=12)
        font = tkfont.nametofont("TkDefaultFont")
        for key, label, sample in self.COLUMNS:
            anchor = "e" if key in ("files", "new") else "w"
            self.tree.heading(key, text=label, anchor=anchor, command=lambda k=key: self._sort_by(k))
            width = max(font.measure(label), font.measure(sample)) + 24
            self.tree.column(key, width=width, minwidth=width // 2, anchor=anchor,
                             stretch=key in ("title", "timetable"))
        self.tree.tag_configure("new", foreground=self.pal["accent"])
        ysb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.tree.bind("<Double-1>", lambda _e: self._open_course_folder())
        self.tree.bind("<Button-3>", self._on_right_click)
        self.course_menu = self._menu(self.tree)
        self.course_menu.add_command(label="Aktualisieren", command=self.update_selected)
        self.course_menu.add_command(label="Vorschau", command=self.preview_selected)
        self.course_menu.add_separator()
        self.course_menu.add_command(label="Ordner öffnen", command=self._open_course_folder)
        self.course_menu.add_command(label="In StudOn öffnen", command=self._open_course_studon)
        paned.add(top, weight=3)

        # --- Selected course actions + files/log notebook ---
        bottom = ttk.Frame(paned)
        sel = ttk.Frame(bottom, padding=(0, 8, 0, 4))
        sel.pack(fill="x")
        self.sel_var = tk.StringVar(value="Kein Kurs ausgewählt")
        ttk.Label(sel, textvariable=self.sel_var, style="Bold.TLabel").pack(side="left")
        self.btn_studon = ttk.Button(sel, text="In StudOn öffnen", command=self._open_course_studon)
        self.btn_folder = ttk.Button(sel, text="Ordner öffnen", command=self._open_course_folder)
        self.btn_preview = ttk.Button(sel, text="Vorschau", command=self.preview_selected)
        self.btn_update = ttk.Button(sel, text="Aktualisieren", command=self.update_selected)
        for b in (self.btn_studon, self.btn_folder, self.btn_preview, self.btn_update):
            b.pack(side="right", padx=(6, 0))

        self.notebook = ttk.Notebook(bottom)
        self.notebook.pack(fill="both", expand=True)
        files_tab = ttk.Frame(self.notebook)
        self.files = ttk.Treeview(files_tab, columns=("date", "path", "size"), show="headings",
                                  height=6)
        for key, label, sample, stretch in (("date", "Datum", "00.00.0000 00:00", False),
                                            ("path", "Datei", "M" * 30, True),
                                            ("size", "Größe", "000.0 MB", False)):
            anchor = "e" if key == "size" else "w"
            self.files.heading(key, text=label, anchor=anchor)
            self.files.column(key, width=font.measure(sample) + 24, stretch=stretch, anchor=anchor)
        fsb = ttk.Scrollbar(files_tab, orient="vertical", command=self.files.yview)
        self.files.configure(yscrollcommand=fsb.set)
        self.files.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        self.files.bind("<Double-1>", lambda _e: self._open_selected_file())
        self.notebook.add(files_tab, text="Dateien")

        self.log_tab = ttk.Frame(self.notebook)
        p = self.pal
        self.log = tk.Text(self.log_tab, wrap="word", state="disabled", font="TkFixedFont", height=8,
                           bg=p["panel"], fg=p["fg"], insertbackground=p["fg"],
                           selectbackground=p["select"], relief="flat", padx=8, pady=6,
                           highlightthickness=0)
        lsb = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.notebook.add(self.log_tab, text="Log")
        paned.add(bottom, weight=2)

        self._global_buttons = [self.btn_update_all, self.btn_preview_all, self.btn_add,
                                self.btn_feedback, self.mb_timetable, self.mb_settings]
        self._update_folder_button()
        self._update_course_buttons()

    # ------------------------------------------------------------ course table

    def reload_courses(self):
        if not self.busy:
            self.job_var.set("Lade Kurse …")

        def scan():
            try:
                self.q.put(("courses", _scan_courses(self.sc), None))
            except Exception as e:  # shown in the footer, table keeps its old rows
                self.q.put(("courses", None, e))

        threading.Thread(target=scan, daemon=True).start()

    def _sort_key(self, c: _Course):
        key = self._sort[0]
        if key == "fetched":
            return c.last_fetched or datetime.min
        if key == "files":
            return len(c.records)
        if key == "new":
            return c.n_new
        if key == "timetable":
            return ", ".join(c.timetable_titles).lower()
        return c.title.lower()

    def _sort_by(self, key):
        cur, rev = self._sort
        # A new column starts descending for dates and counts, ascending for text.
        rev = (not rev) if key == cur else key in ("fetched", "files", "new")
        self._sort = (key, rev)
        self._fill_courses()

    def _fill_courses(self):
        keep = self._selected()
        keep_folder = self._preselect or (keep.folder if keep else None)
        self.tree.delete(*self.tree.get_children())
        self._by_iid = {}
        select_iid = None
        for i, c in enumerate(sorted(self.courses, key=self._sort_key, reverse=self._sort[1])):
            iid = f"c{i}"
            self._by_iid[iid] = c
            fetched = c.last_fetched.strftime("%d.%m.%Y %H:%M") if c.last_fetched else "–"
            self.tree.insert("", "end", iid=iid, tags=("new",) if c.n_new else (), values=(
                c.title, fetched, len(c.records), c.n_new or "", ", ".join(c.timetable_titles)))
            if keep_folder and os.path.abspath(c.folder) == keep_folder:
                select_iid = iid
        self._preselect = None
        if select_iid:
            self.tree.selection_set(select_iid)
            self.tree.focus(select_iid)
            self.tree.see(select_iid)
        self._on_select()

    def _selected(self) -> Optional[_Course]:
        sel = self.tree.selection()
        return self._by_iid.get(sel[0]) if sel else None

    def _on_select(self):
        c = self._selected()
        self.sel_var.set(c.title if c else "Kein Kurs ausgewählt")
        self.files.delete(*self.files.get_children())
        if c:
            base = Path(c.folder)
            for i, r in enumerate(c.records[:FILES_MAX_ROWS]):
                self.files.insert("", "end", iid=f"f{i}", values=(
                    _naive(r.timestamp).strftime("%d.%m.%Y %H:%M"),
                    r.get_relative_path(base), r.size_formatted))
        self._update_course_buttons()

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        state = "disabled" if self.busy else "normal"
        self.course_menu.entryconfigure("Aktualisieren", state=state)
        self.course_menu.entryconfigure("Vorschau", state=state)
        self.course_menu.tk_popup(event.x_root, event.y_root)

    def _open_course_folder(self):
        c = self._selected()
        if c:
            _open_path(c.folder)

    def _open_course_studon(self):
        c = self._selected()
        if c:
            self.sc._open_url_in_browser(c.source_url)

    def _open_selected_file(self):
        c, sel = self._selected(), self.files.selection()
        if not (c and sel):
            return
        path = str(c.records[int(sel[0][1:])].filepath)
        if os.path.exists(path):
            _open_path(path)
        else:
            messagebox.showinfo(APP_TITLE, f"Datei nicht gefunden:\n{path}", parent=self.root)

    # ----------------------------------------------------------------- status

    def _refresh_status(self):
        st = self.sc._read_tray_status()
        state = st.get("state", "idle")
        if state == "waiting_login" and not self.sc._pid_alive(st.get("state_pid")):
            state = "idle"  # left behind by a process that has exited (see the tray)
        next_fire = st.get("next_fire_human")
        if state == "waiting_login":
            text = "Hintergrund-Sync wartet auf den StudOn-Login"
        elif state == "syncing":
            text = "Hintergrund-Sync läuft …"
        elif next_fire:
            text = f"Nächster Vorlesungs-Sync: {next_fire}"
        else:
            text = "Hintergrund-Sync bereit"
        self.status_var.set(text)
        human, epoch = st.get("last_sync_human"), st.get("last_sync_epoch")
        age = _humanize_age(epoch) if epoch else ""
        if human or age:
            self.last_var.set("Letzter Sync: " + " ".join(
                x for x in (str(human or ""), f"({age})" if age and human else age) if x))
        else:
            self.last_var.set("Letzter Sync: noch keiner")
        self._login_url = st.get("login_url")
        if state == "waiting_login" and self._login_url:
            self.login_btn.pack(side="right", padx=(0, 6))
        else:
            self.login_btn.pack_forget()
        self.root.after(STATUS_REFRESH_MS, self._refresh_status)

    def _open_login(self):
        self.sc._open_url_in_browser(self._login_url or self.sc._get_first_course_url())

    def _update_folder_button(self):
        folder = self.sc.DOWNLOAD_FOLDER
        short = folder.replace(os.path.expanduser("~"), "~", 1)
        self.folder_btn.configure(text=f"Download-Ordner: {short}")

    # ------------------------------------------------------------------- jobs

    def _set_busy(self, busy: bool, title: str = ""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for w in self._global_buttons:
            w.configure(state=state)
        self._update_course_buttons()
        if busy:
            self.job_var.set(f"Läuft: {title} …")
            self.progress.pack(side="right", padx=(0, 8))
            self.progress.start(12)
        else:
            self.job_var.set("Bereit")
            self.progress.stop()
            self.progress.pack_forget()

    def _update_course_buttons(self):
        has = self._selected() is not None
        for b in (self.btn_studon, self.btn_folder):
            b.configure(state="normal" if has else "disabled")
        for b in (self.btn_preview, self.btn_update):
            b.configure(state="normal" if has and not self.busy else "disabled")

    def _run_job(self, title, fn, *, lock=False, on_done=None):
        """Run fn in a worker thread with its output routed into the Log tab.

        lock=True claims the sync lock the two daemons share, so a manual
        download never overlaps a background one."""
        if self.busy:
            return
        if lock:
            if not self.sc._acquire_sync_lock("gui"):
                messagebox.showinfo(APP_TITLE, "Ein Hintergrund-Sync läuft gerade.\n"
                                    "Bitte in ein paar Minuten erneut versuchen.", parent=self.root)
                return
            self.holding_lock = True
        self._set_busy(True, title)
        self.notebook.select(self.log_tab)
        self._append_log(f"\n▶ {title} ({datetime.now():%H:%M:%S})\n")
        job = (title, fn, lock, on_done)
        threading.Thread(target=self._worker, args=(job,), daemon=True).start()

    def _worker(self, job):
        _title, fn, lock, _on_done = job
        writer = _QueueWriter(self.q)
        handler = _QueueLogHandler(self.q)
        self.sc.logger.addHandler(handler)
        result = err = None
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                result = fn()
        except (Exception, SystemExit) as e:
            err = e
        finally:
            self.sc.logger.removeHandler(handler)
            if lock:
                self.sc._release_sync_lock()
        self.q.put(("done", job, result, err))

    def _job_done(self, job, result, err):
        title, fn, lock, on_done = job
        if lock:
            self.holding_lock = False
        self._set_busy(False)
        self.reload_courses()
        if err is None:
            self._append_log(f"✔ {title} fertig\n")
            if on_done:
                on_done(result)
            return
        if isinstance(err, _SessionExpired) or "Session expired" in str(err):
            detail = f" ({err})" if str(err) else ""
            self._append_log(f"❌ Keine gültige StudOn-Sitzung{detail}\n")
            self._offer_login(lambda: self._run_job(title, fn, lock=lock, on_done=on_done))
        else:
            self._append_log(f"❌ {type(err).__name__}: {err}\n")

    def _offer_login(self, retry):
        if not messagebox.askyesno(
                "StudOn-Login", "Keine gültige StudOn-Sitzung in Firefox.\n\n"
                "StudOn im Browser öffnen, um dich einzuloggen?", parent=self.root):
            return
        self.sc._open_url_in_browser(self.sc._get_first_course_url())
        if messagebox.askokcancel("StudOn-Login", "Nach dem Login auf OK klicken,\n"
                                  "um es erneut zu versuchen.", parent=self.root):
            retry()

    def _session(self):
        """A requests session with the Firefox cookies (worker thread only)."""
        s = self.sc._make_session()
        if s is None:
            raise _SessionExpired()
        return s

    def _poll_queue(self):
        chunks = []
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, str):
                    chunks.append(item)
                    continue
                if chunks:
                    self._append_log("".join(chunks))
                    chunks = []
                if item[0] == "done":
                    self._job_done(*item[1:])
                elif item[0] == "courses":
                    self._courses_loaded(*item[1:])
        except queue.Empty:
            pass
        if chunks:
            self._append_log("".join(chunks))
        self.root.after(QUEUE_POLL_MS, self._poll_queue)

    def _courses_loaded(self, courses, err):
        if err is not None:
            self._append_log(f"❌ Kurse konnten nicht geladen werden: {err}\n")
        else:
            self.courses = courses
            self._fill_courses()
        if not self.busy:
            n_new = sum(c.n_new for c in self.courses)
            self.job_var.set(f"{len(self.courses)} Kurse · {n_new} neue Dateien "
                             f"in den letzten {NEW_FILE_DAYS} Tagen")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        excess = int(self.log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------------- actions

    def update_all(self):
        sc = self.sc

        def job():
            success, n_dl, n_ex, expired, _files = sc.update_all_courses()
            if success:
                sc._record_tray_sync("Alle Kurse")
            if expired:
                # update_all_courses stops at the first "Session expired". A /go/exc/
                # short link reports that while the session is fine (TODOS.md §4),
                # so only ask for a login when a fresh probe fails too.
                if not sc.can_access_studon():
                    raise _SessionExpired()
                print("\n⚠️  Die Sitzung ist gültig, aber ein Kurs-Link meldete "
                      "'Session expired'. Die Kurse danach wurden übersprungen (TODOS.md §4).")
            print(f"\n🎉 Fertig: {n_dl} neue Datei(en), {n_ex} Archiv(e) entpackt.")

        self._run_job("Alle Kurse aktualisieren", job, lock=True)

    def preview_all(self):
        sc = self.sc

        def job():
            session = self._session()
            for _meta, source_url, folder in sc.find_all_metadata_files(sc.DOWNLOAD_FOLDER):
                sc._print_discovery_preview(source_url, session, os.path.dirname(folder))

        self._run_job("Vorschau aller Kurse", job)

    def update_selected(self):
        c = self._selected()
        if c:
            self._download(c.source_url, os.path.dirname(c.folder), c.title)

    def preview_selected(self):
        c = self._selected()
        if not c:
            return

        def job():
            self.sc._print_discovery_preview(c.source_url, self._session(), os.path.dirname(c.folder))

        self._run_job(f"Vorschau: {c.title}", job)

    def _download(self, url, base, label):
        sc = self.sc

        def job():
            n_dl, n_ex, files = sc.process_single_url(url, self._session(), base)
            print(f"\n🎉 Fertig: {n_dl} neue Datei(en), {n_ex} Archiv(e) entpackt.")
            for f in files:
                print(f"   • {os.path.relpath(f, base)}")
            sc._record_tray_sync(label)

        self._run_job(f"Aktualisieren: {label}", job, lock=True)

    def add_course(self):
        initial = ""
        try:
            clip = self.root.clipboard_get().strip()
            if self.sc._is_studon_url(clip):
                initial = clip
        except tk.TclError:
            pass
        url = simpledialog.askstring("Kurs hinzufügen", "StudOn-Kurs-URL:" + " " * 80,
                                     initialvalue=initial, parent=self.root)
        if not url:
            return
        url = url.strip()
        if not self.sc._is_studon_url(url):
            messagebox.showerror(APP_TITLE, "Das ist keine StudOn-URL.", parent=self.root)
            return
        base = self.sc.DOWNLOAD_FOLDER

        def preview():
            self.sc._print_discovery_preview(url, self._session(), base)

        def confirm(_result):
            if messagebox.askyesno("Kurs hinzufügen", "Die Vorschau steht im Log.\n\n"
                                   "Dateien jetzt herunterladen?", parent=self.root):
                self._download(url, base, "Neuer Kurs")

        self._run_job("Vorschau: neuer Kurs", preview, on_done=confirm)

    def check_feedback(self):
        if not self.sc._is_imap_installed():
            if messagebox.askyesno(APP_TITLE, "Der FAUmail-Checker ist nicht eingerichtet.\n\n"
                                   "Jetzt im Terminal einrichten?", parent=self.root):
                self._run_in_terminal("--install-imap")
            return

        def job():
            n_ex, n_files, _ = self.sc.check_and_process_feedback(verbose=True)
            print(f"\nFeedback: {n_ex} Übung(en) verarbeitet, {n_files} Datei(en) geladen.")

        self._run_job("FAUmail-Feedback prüfen", job)

    def fetch_timetable(self):
        self._run_job("Stundenplan holen", self.sc.fetch_timetable_markdown)

    def _open_timetable(self):
        path = os.path.join(self.sc.DOWNLOAD_FOLDER, "timetable.md")
        if os.path.exists(path):
            _open_path(path)
        else:
            messagebox.showinfo(APP_TITLE, "timetable.md gibt es noch nicht.\n"
                                "Zuerst »Stundenplan von campo holen«.", parent=self.root)

    def _fill_settings_menu(self):
        m, sc = self.settings_menu, self.sc
        m.delete(0, "end")
        m.add_command(label="Download-Ordner ändern …", command=self._change_download_path)
        m.add_separator()
        if sc._is_installed():
            m.add_command(label="Cron-Jobs, Alias und Starter entfernen", command=self._uninstall)
        else:
            m.add_command(label="Cron-Jobs, Alias und Starter installieren (Terminal)",
                          command=lambda: self._run_in_terminal("--install"))
        if sc._is_imap_installed():
            m.add_command(label="FAUmail-Checker entfernen",
                          command=lambda: self._run_job("FAUmail-Checker entfernen",
                                                        sc._run_uninstall_imap))
        else:
            m.add_command(label="FAUmail-Checker einrichten (Terminal)",
                          command=lambda: self._run_in_terminal("--install-imap"))

    def _change_download_path(self):
        path = filedialog.askdirectory(initialdir=self.sc.DOWNLOAD_FOLDER, parent=self.root,
                                       title="Download-Ordner wählen")
        if not path:
            return
        cfg = self.sc.load_config()
        cfg["downloads_path"] = path
        self.sc.save_config(cfg)
        self.sc.DOWNLOAD_FOLDER = path
        self._append_log(f"Download-Ordner gespeichert: {path}\n")
        self._update_folder_button()
        self.reload_courses()

    def _uninstall(self):
        if messagebox.askyesno(APP_TITLE, "Cron-Jobs, den Shell-Alias und den Starter "
                               "im Anwendungsmenü entfernen?", parent=self.root):
            self._run_job("Cron-Jobs, Alias und Starter entfernen", self.sc._run_uninstall)

    def _run_in_terminal(self, *flags):
        """Run an interactive (stdin-reading) mode in a terminal window."""
        script = os.path.abspath(self.sc.__file__)
        cmd = " ".join(shlex.quote(a) for a in (sys.executable, script, *flags))
        shell = f'{cmd}; echo; read -r -p "Enter drücken zum Schließen … "'
        for term in (["konsole", "-e"], ["x-terminal-emulator", "-e"], ["xterm", "-e"]):
            if shutil.which(term[0]):
                subprocess.Popen(term + ["bash", "-c", shell], cwd=os.path.dirname(script),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                self._append_log(f"Im Terminal gestartet: {' '.join(flags)}\n")
                return
        messagebox.showerror(APP_TITLE, "Kein Terminal gefunden (konsole, x-terminal-emulator, "
                             f"xterm).\nBitte selbst ausführen:\n\n{cmd}", parent=self.root)

    def _on_close(self):
        if self.busy and not messagebox.askokcancel(
                APP_TITLE, "Ein Auftrag läuft noch und wird abgebrochen.\nTrotzdem beenden?",
                parent=self.root):
            return
        if self.holding_lock:
            self.sc._release_sync_lock()
        self.root.destroy()


def run(sc, preselect=None) -> None:
    """Open the dashboard. sc is the studon_client module object.

    preselect is the (title, source_url, folder) tuple from
    _detect_current_course; that course starts selected."""
    root = tk.Tk(className="studon_client")
    Dashboard(root, sc, preselect_folder=preselect[2] if preselect else None)
    root.mainloop()
