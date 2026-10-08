"""Shared handling of an expired or missing campo login.

One mechanism for studon_client.py and the standalone campo_search.py. When a
campo call hits HTTP 401/403, a redirect to the login/SSO page, or no Firefox
cookies, the failing site calls `auth_failure(context)`. That

  * opens the campo login page in the default browser (unless suppressed),
  * prints one plain line saying what happened and what to do next.

Interactive entry points are wrapped in `@retry_after_login(...)`: after a
flagged failure the wrapper polls the access check until the login works
(bounded) and then runs the call once more.

"Interactive" is not decided by isatty(): agents run commands without a TTY and
must still get the browser. Unattended runs opt out explicitly:
  * `set_unattended()` - called by the --daily-sync / --lecture-sync entry points,
  * env `STUDON_NO_BROWSER=1` - set on the weekly --campo-bescheinigungen cron line.

Stdlib only at import time, so campo_search.py can use it standalone.
"""
from __future__ import annotations

import functools
import os
import time
import webbrowser
from typing import Callable, Optional

CAMPO_LOGIN_URL = ('https://www.campo.fau.de/qisserver/pages/startFlow.xhtml'
                   '?_flowId=studyPlanner-flow')
BROWSER_COOLDOWN_SECONDS = 120   # at most one browser open per this many seconds
LOGIN_WAIT_SECONDS = 600         # how long an interactive call waits for the login
POLL_SECONDS = 5
# Per host, never synced: the stamp must not travel to other machines.
STATE_DIR = os.path.expanduser('~/.local/state/studon-client')
STAMP_PATH = os.path.join(STATE_DIR, 'campo_login_opened')
LOGIN_COMMAND = 'python studon_client.py --login campo'

_unattended = False
_retry_depth = 0     # >0 while inside a retry_after_login wrapper
_auth_failed = False  # set by auth_failure(), read by the wrapper


def set_unattended(flag: bool = True) -> None:
    """Mark this process as an unattended daemon/cron run: never open a browser."""
    global _unattended
    _unattended = flag


def browser_allowed() -> bool:
    """False for daemon/cron runs and when STUDON_NO_BROWSER is set (and not 0)."""
    if _unattended:
        return False
    return os.environ.get('STUDON_NO_BROWSER', '').strip().lower() in ('', '0', 'false', 'no')


def is_unauthenticated(resp) -> bool:
    """True if a requests.Response says 'not logged in': 401/403, or a bounce to
    the IdP / login page."""
    try:
        if resp.status_code in (401, 403):
            return True
        url = (resp.url or '').lower()
        if 'idp.fau.de' in url or 'login' in url:
            return True
        text = resp.text or ''
        return 'j_security_check' in text or 'SAMLRequest' in text
    except Exception:
        return False


def _seconds_since_last_open() -> Optional[float]:
    try:
        return max(0.0, time.time() - os.path.getmtime(STAMP_PATH))
    except OSError:
        return None


def _stamp_open() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STAMP_PATH, 'w') as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def auth_failure(context: str, login_url: str = CAMPO_LOGIN_URL,
                 open_url: Optional[Callable[[str], object]] = None) -> bool:
    """Handle one campo auth failure: open the login page, print what to do.

    Returns True if a browser window was opened by this call. Marks the failure
    so an enclosing `retry_after_login` wrapper waits and retries.
    """
    global _auth_failed
    _auth_failed = True
    prefix = f"❌ campo-Login abgelaufen ({context})."
    if not browser_allowed():
        print(f"{prefix} Browser nicht geöffnet (unbeaufsichtigter Lauf / STUDON_NO_BROWSER). "
              f"Einloggen mit `{LOGIN_COMMAND}`, dann den Befehl erneut ausführen.")
        return False

    will_wait = _retry_depth > 0
    follow_up = (f"Dort einloggen, das Tool wartet bis zu {LOGIN_WAIT_SECONDS // 60} min und macht dann weiter. "
                 f"Bricht der Aufruf vorher ab: nach dem Login den Befehl erneut ausführen."
                 if will_wait else
                 "Dort einloggen, dann den Befehl erneut ausführen.")

    age = _seconds_since_last_open()
    if age is not None and age < BROWSER_COOLDOWN_SECONDS:
        print(f"{prefix} Browser mit dem campo-Login wurde vor {int(age)} s schon geöffnet "
              f"(nicht erneut). {follow_up}")
        return False

    opener = open_url or webbrowser.open
    try:
        opener(login_url)
    except Exception as e:
        print(f"{prefix} Browser ließ sich nicht öffnen ({e}). "
              f"Login-Seite von Hand öffnen: {login_url} (oder `{LOGIN_COMMAND}`), dann den Befehl erneut ausführen.")
        return False
    _stamp_open()
    print(f"{prefix} Browser mit dem campo-Login geöffnet ({login_url}). {follow_up}")
    return True


def default_access_check() -> bool:
    """Standalone login probe (Firefox cookies + one GET); studon_client passes
    its own can_access_campo instead."""
    try:
        import browser_cookie3
        import requests
        s = requests.Session()
        for dom in ('fau.de', 'campo.fau.de'):
            s.cookies.update(browser_cookie3.firefox(domain_name=dom))
        s.headers.update({'User-Agent': 'Mozilla/5.0'})
        r = s.get(CAMPO_LOGIN_URL, timeout=10, allow_redirects=True)
        return r.status_code == 200 and not is_unauthenticated(r)
    except Exception:
        return False


def _wait_for_login(access_check: Callable[[], bool], max_wait_seconds: float) -> bool:
    deadline = time.time() + max_wait_seconds
    while True:
        try:
            if access_check():
                return True
        except Exception:
            pass
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        time.sleep(min(POLL_SECONDS, remaining))


def retry_after_login(access_check: Optional[Callable[[], bool]] = None,
                      max_wait_seconds: Optional[float] = None):
    """Decorator for interactive campo entry points.

    Runs the function. If an auth failure was flagged during the call (it returned
    or raised after `auth_failure`), and a browser is allowed, polls `access_check`
    until the login works or the wait runs out, then runs the function once more.
    Nested wrapped calls pass straight through; only the outermost one waits.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            global _retry_depth, _auth_failed
            if _retry_depth > 0:
                return fn(*args, **kwargs)
            _auth_failed = False
            _retry_depth += 1
            try:
                exc: Optional[BaseException] = None
                result = None
                try:
                    result = fn(*args, **kwargs)
                except Exception as e:  # re-raised below unless the login is fixed
                    exc = e
                if not _auth_failed or not browser_allowed():
                    if exc is not None:
                        raise exc
                    return result
                wait = LOGIN_WAIT_SECONDS if max_wait_seconds is None else max_wait_seconds
                if not _wait_for_login(access_check or default_access_check, wait):
                    print(f"❌ Nach {int(wait // 60)} min kein campo-Login erkannt. "
                          f"Einloggen (`{LOGIN_COMMAND}`), dann den Befehl erneut ausführen.")
                    if exc is not None:
                        raise exc
                    return result
                print("✅ campo-Login erkannt, Aufruf wird einmal wiederholt.")
                _auth_failed = False
                return fn(*args, **kwargs)
            finally:
                _retry_depth -= 1
        return wrapper
    return deco
