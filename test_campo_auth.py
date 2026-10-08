"""campo auth-failure handling: browser open, guards, rate limit, wait + retry.

Everything is mocked: no browser is opened and campo is never contacted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campo_auth  # noqa: E402
import studon_client as s  # noqa: E402


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(campo_auth, "STAMP_PATH", str(tmp_path / "stamp"))
    monkeypatch.setattr(campo_auth, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(campo_auth, "_unattended", False)
    monkeypatch.setattr(campo_auth, "_retry_depth", 0)
    monkeypatch.delenv("STUDON_NO_BROWSER", raising=False)


def test_failure_opens_browser_once_and_says_so(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    opened = []
    assert campo_auth.auth_failure("test", open_url=opened.append) is True
    assert len(opened) == 1
    assert "Browser mit dem campo-Login geöffnet" in capsys.readouterr().out
    # second failure within the cooldown: no second tab
    assert campo_auth.auth_failure("test", open_url=opened.append) is False
    assert len(opened) == 1
    assert "schon geöffnet" in capsys.readouterr().out


def test_unattended_and_env_never_open_a_browser(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    opened = []
    campo_auth.set_unattended()
    assert campo_auth.auth_failure("daemon", open_url=opened.append) is False
    monkeypatch.setattr(campo_auth, "_unattended", False)
    monkeypatch.setenv("STUDON_NO_BROWSER", "1")
    assert campo_auth.auth_failure("cron", open_url=opened.append) is False
    assert opened == []
    assert "Browser nicht geöffnet" in capsys.readouterr().out


def test_wrapper_waits_for_login_then_retries_once(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(campo_auth.time, "sleep", lambda _s: None)
    calls = []

    @campo_auth.retry_after_login(access_check=lambda: True)
    def call():
        calls.append(1)
        if len(calls) == 1:
            campo_auth.auth_failure("test", open_url=lambda _u: None)
            return None
        return "ok"

    assert call() == "ok"
    assert len(calls) == 2


def test_wrapper_does_not_wait_in_unattended_mode(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    campo_auth.set_unattended()
    calls = []

    @campo_auth.retry_after_login(access_check=lambda: True)
    def call():
        calls.append(1)
        campo_auth.auth_failure("test", open_url=lambda _u: None)

    call()
    assert len(calls) == 1


def test_studon_client_routes_through_the_shared_helper(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    opened = []
    monkeypatch.setattr(s, "_open_url_in_browser", lambda url: opened.append(url) or True)
    assert s._handle_campo_auth_failure("test") is True
    assert opened == [s.CAMPO_STUDY_PLANNER_URL]


def test_unauthenticated_detection():
    class R:
        def __init__(self, code, url="https://www.campo.fau.de/x", text=""):
            self.status_code, self.url, self.text = code, url, text
    assert campo_auth.is_unauthenticated(R(403))
    assert campo_auth.is_unauthenticated(R(200, url="https://idp.fau.de/idp/profile/SAML2"))
    assert not campo_auth.is_unauthenticated(R(200))
