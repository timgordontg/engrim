"""Packaging + plugin-install guards.

Two things that go stale silently and are only noticed by someone installing fresh: the version
pinned in four separate manifests, and the plugin's assumption about where a venv puts its binaries.
The plugin route was 100% broken on Windows because of the second one.
"""
import json
import re
from pathlib import Path

import pytest

import engrim

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugin" / "hooks" / "hooks.json"
BOOTSTRAP = ROOT / "plugin" / "scripts" / "engrim-bootstrap.sh"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = ROOT / "plugin" / ".claude-plugin" / "plugin.json"

pytestmark = pytest.mark.skipif(not HOOKS.exists(), reason="running outside the source tree")


def _text(p):
    return p.read_text(encoding="utf-8")


def test_every_manifest_pins_the_released_version():
    """These drifted to 1.1.0 while the package shipped 1.2.0 — so the plugin installed a version
    behind the one being announced, and the marketplace advertised the wrong number."""
    v = engrim.__version__
    assert json.loads(_text(PLUGIN_MANIFEST))["version"] == v
    mk = json.loads(_text(MARKETPLACE))
    assert mk["metadata"]["version"] == v
    assert [p["version"] for p in mk["plugins"]] == [v]
    assert re.search(r"ENGRIM_PLUGIN_SOURCE:-engrim==([0-9][^}\"]*)\}", _text(BOOTSTRAP)).group(1) == v


def test_plugin_hooks_resolve_the_binary_on_both_venv_layouts():
    """A venv writes entry points to bin/ on POSIX and Scripts/*.exe on Windows. Only bin/ was
    checked, so on Windows all four hooks silently no-opped forever."""
    cfg = json.loads(_text(HOOKS))
    invocations = [h["command"] for groups in cfg["hooks"].values()
                   for g in groups for h in g["hooks"] if "/venv/" in h["command"]]
    assert len(invocations) == 4, "expected one engrim invocation per lifecycle event"
    for cmd in invocations:
        assert "/venv/bin/engrim" in cmd
        assert "/venv/Scripts/engrim.exe" in cmd, f"no Windows fallback: {cmd}"
        assert cmd.endswith("|| true"), "a hook must never fail the session"


def test_bootstrap_checks_both_venv_layouts_and_never_fails_the_session():
    sh = _text(BOOTSTRAP)
    assert '"$VENV/Scripts/$1.exe"' in sh, "bootstrap only looks in bin/ — Windows installs no-op"
    assert "command -v python3 || command -v python" in sh, "Windows ships `python`, not `python3`"
    assert sh.rstrip().endswith("exit 0"), "bootstrap must always exit 0"


def test_hook_wiring_covers_every_lifecycle_event():
    """setup and the plugin are two routes to the same wiring — they must not drift apart."""
    from engrim.cli import cmd_setup  # noqa: F401  (import guard: the module must load)
    import engrim.cli as cli
    src = Path(cli.__file__).read_text(encoding="utf-8")
    wired = set(re.findall(r'"(SessionStart|SessionEnd|Stop|UserPromptSubmit)":', src))
    assert wired == set(json.loads(_text(HOOKS))["hooks"])
