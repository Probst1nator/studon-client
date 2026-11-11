"""The @reboot cron lines carry the desktop session's DISPLAY and D-Bus address.

Without them the daily sync cannot show its login tray icon and stops waiting
for the login. Run with: pytest -q
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studon_client as s  # noqa: E402


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux cron only")
def test_reboot_lines_carry_the_session_env(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":1")
    daily, lecture, weekly = s._cron_lines()
    bus = f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus"
    for line in (daily, lecture):
        assert line.startswith("@reboot ")
        assert f"&& DISPLAY=:1 {bus} {sys.executable} " in line
    assert "DISPLAY=" not in weekly
    assert daily.endswith("--daily-sync")
    assert lecture.endswith("--lecture-sync")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux cron only")
def test_headless_install_sets_no_display(monkeypatch):
    """No desktop session at --install: no DISPLAY, so the daily sync keeps its
    bounded login wait (_has_display() stays false)."""
    monkeypatch.delenv("DISPLAY", raising=False)
    for line in s._cron_lines():
        assert "DISPLAY=" not in line
        assert "DBUS_SESSION_BUS_ADDRESS=" not in line
    monkeypatch.setenv("DISPLAY", "localhost:10.0")  # ssh -X
    for line in s._cron_lines():
        assert "DISPLAY=" not in line
