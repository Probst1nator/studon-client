"""Lifecycle flags must win over --gui.

The tools installer runs `studon_client.py --uninstall-skill --gui` (the tool's
launch args are appended). When --gui was handled first, the dashboard opened,
the skill file stayed and the run exited 0. Run with: pytest -q
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _run(flag, home):
    env = dict(os.environ, HOME=str(home), CLI_TOOLS_KIT_SKIP_USER_PATH="1")
    return subprocess.run([sys.executable, os.path.join(HERE, "studon_client.py"), flag, "--gui"],
                          capture_output=True, text=True, env=env, timeout=60)


def test_install_and_uninstall_skill_beat_gui(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "studon-client" / "SKILL.md"
    r = _run("--install-skill", tmp_path)
    assert r.returncode == 0, r.stderr
    assert skill.is_file()
    r = _run("--uninstall-skill", tmp_path)
    assert r.returncode == 0, r.stderr
    assert not skill.exists()
