"""Cross-platform regression tests — the failures a Linux/macOS dev never sees.

Every case here was a real Windows bug found on a fresh install, and every one of them was SILENT:
setup printed green checkmarks while wiring hooks that could not run, and the piped commands that
crashed looked perfect in a terminal. They're pinned here because the whole class is invisible on
the platform this is developed on.
"""
import io
import json
import re
from pathlib import Path

import pytest

import engrim.cli as cli
from engrim.cli import main

WIN_BIN = r"C:\Users\timgo\AppData\Local\Programs\Python\Python313\Scripts\engrim.EXE"
_REAL_VERIFY = cli._verify_hook_bin        # kept before the autouse stub below can replace it


@pytest.fixture(autouse=True)
def _assume_bin_runs(monkeypatch):
    """The wiring tests below fake a binary PATH that doesn't exist on this machine, so the live
    check in `setup` would fail every one of them for the wrong reason. They're about the command
    STRING setup writes; the check itself is pinned separately in section 4."""
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)


def _run_setup(tmp_path, monkeypatch, which=WIN_BIN):
    """Run `setup` against a throwaway settings.json with the resolved binary path faked."""
    monkeypatch.setattr(cli.shutil, "which", lambda _n: which)
    settings = tmp_path / "settings.json"
    main(["--db", str(tmp_path / "m.db"), "setup", "--settings", str(settings), "--no-claude-md"])
    return settings, json.loads(settings.read_text(encoding="utf-8"))


def _commands(cfg):
    return [h["command"] for groups in cfg["hooks"].values() for g in groups for h in g["hooks"]]


# --------------------------------------------------------------------- 1. the unquoted Windows path

def test_setup_writes_hook_paths_the_shell_can_actually_run(tmp_path, monkeypatch):
    """Raw, `C:\\Users\\...` reaches bash as `C:UserstimgoAppData...` — backslashes eaten as escape
    sequences, command not found, and `2>/dev/null || true` swallows the error. Silent no-op."""
    _, cfg = _run_setup(tmp_path, monkeypatch)
    cmds = _commands(cfg) + [cfg["statusLine"]["command"]]
    assert cmds, "setup wired nothing"
    for cmd in cmds:
        assert "\\" not in cmd, f"backslash survives into a shell command: {cmd}"
        assert cmd.startswith('"') and '" ' in cmd, f"binary path is not quoted: {cmd}"
        assert "engrim.EXE" in cmd                      # the real binary, not a bare guess


def test_setup_quotes_posix_paths_with_spaces(tmp_path, monkeypatch):
    """Same quoting bug, rarer trigger: a space in the install path split the command in two."""
    _, cfg = _run_setup(tmp_path, monkeypatch, which="/opt/my tools/bin/engrim")
    assert all('"/opt/my tools/bin/engrim"' in c for c in _commands(cfg))


# ------------------------------------------------------------------------- 2. idempotency of setup

def test_setup_does_not_duplicate_hooks_on_windows(tmp_path, monkeypatch):
    """setup is documented idempotent. The marker `engrim hook` never matched `engrim.EXE hook`,
    so on Windows every run appended another hook group — four more on every invocation."""
    _, first = _run_setup(tmp_path, monkeypatch)
    _, second = _run_setup(tmp_path, monkeypatch)
    for event in first["hooks"]:
        assert len(second["hooks"][event]) == 1, f"{event} duplicated on the second run"
    assert second["hooks"] == first["hooks"]
    assert second["statusLine"] == first["statusLine"]


def test_setup_recognises_hooks_written_before_the_quoting_fix(tmp_path, monkeypatch):
    """The upgrade path: an existing install has unquoted POSIX commands. Re-running setup must
    still see its own hooks and leave them alone rather than wiring a second copy."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
                                     "command": "/usr/local/bin/engrim hook 2>/dev/null || true"}]}],
    }}), encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/local/bin/engrim")
    main(["--db", str(tmp_path / "m.db"), "setup", "--settings", str(settings), "--no-claude-md"])
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    assert len(cfg["hooks"]["SessionStart"]) == 1


def test_setup_leaves_an_existing_engrim_status_line_alone(tmp_path, monkeypatch):
    """Same normalisation, applied to the status line — a Windows-spelled one must be recognised."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f'"{WIN_BIN}" statusline'}}), encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: WIN_BIN)
    main(["--db", str(tmp_path / "m.db"), "setup", "--settings", str(settings), "--no-claude-md"])
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    assert cfg["statusLine"]["command"] == f'"{WIN_BIN}" statusline'   # untouched, not re-wired


# ------------------------------------------------------- 3. the std streams are not always UTF-8

def _swap_stdout_to(monkeypatch, encoding):
    """Stand in for a Windows pipe: a text stream over bytes with a non-UTF-8 codec."""
    raw = io.BytesIO()
    monkeypatch.setattr(cli.sys, "stdout", io.TextIOWrapper(raw, encoding=encoding, newline=""))
    return raw


def test_statusline_survives_a_cp1252_stdout(tmp_path, monkeypatch):
    """Windows gives a piped stdout cp1252, and Claude Code reads the status line through a pipe.
    The bar leads with 🧠, so it died with UnicodeEncodeError on every single refresh."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "a curated record"])
    raw = _swap_stdout_to(monkeypatch, "cp1252")
    main(["--db", str(db), "statusline", "-p", "/p"])          # used to raise UnicodeEncodeError
    cli.sys.stdout.flush()
    assert "🧠 engrim" in raw.getvalue().decode("utf-8")


@pytest.mark.parametrize("cmd", ["context", "stats"])
def test_other_emoji_commands_survive_a_cp1252_stdout(tmp_path, monkeypatch, cmd):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "a curated record"])
    raw = _swap_stdout_to(monkeypatch, "cp1252")
    main(["--db", str(db), cmd, "-p", "/p"])
    cli.sys.stdout.flush()
    assert raw.getvalue()                                       # produced output instead of dying


def test_hook_payload_decodes_when_stdin_is_not_utf8(tmp_path, monkeypatch):
    """The mirror image: stdin is cp1252 too, while Claude Code sends UTF-8 JSON. `assist` catches
    the decode failure and emits an empty block, so the minder just silently injected nothing.

    The Cyrillic capital is deliberate: it encodes to D0 90, and 0x90 is one of the few bytes cp1252
    genuinely refuses. Most UTF-8 sneaks through that codec as mojibake instead — quieter, and it
    corrupts the prompt the ranking runs on, which is why the encoding is asserted directly too."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact",
          "-s", "the billing pipeline reconciles nightly"])
    payload = json.dumps({"prompt": "how does the billing pipeline treat Алиса's café résumé 🧠",
                          "workspace": {"project_dir": "/p"}}).encode("utf-8")
    monkeypatch.setattr(cli.sys, "stdin",
                        io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252"))
    raw = _swap_stdout_to(monkeypatch, "cp1252")
    main(["--db", str(db), "assist", "-p", "/p"])
    cli.sys.stdout.flush()
    out = json.loads(raw.getvalue().decode("utf-8"))
    assert "billing pipeline" in out["hookSpecificOutput"]["additionalContext"]
    assert (cli.sys.stdin.encoding or "").lower().replace("-", "") == "utf8"


def test_no_text_file_is_opened_with_the_locale_codec():
    """Guard the whole class: `open()` defaults to cp1252 on Windows, so every text read/write has
    to name its encoding. Reading a settings.json or CLAUDE.md containing an emoji crashed setup."""
    src = Path(cli.__file__).parent
    offenders = []
    for path in sorted(src.glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])open\(", line) and "encoding=" not in line \
                    and not re.search(r"""["'][rwax]b["']""", line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "text-mode open() without encoding=:\n" + "\n".join(offenders)


# ------------------------------------------- 4. the checkmark has to be earned, not just printed

def test_setup_fails_loudly_when_the_wired_binary_cannot_run(tmp_path, monkeypatch, capsys):
    """The bug this whole file documents: every hook ends in `|| true`, so a completely broken
    install still printed a full column of green checkmarks and the user walked away happy.
    Setup must verify the binary it just wired and exit non-zero when it can't run."""
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: "command not found")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: WIN_BIN)
    settings = tmp_path / "settings.json"
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "m.db"), "setup",
              "--settings", str(settings), "--no-claude-md"])
    assert exc.value.code != 0, "a non-functional install must not exit clean"
    out = capsys.readouterr()
    assert "NOT done" in str(exc.value)
    assert "Done." not in out.out, "the success line must not print over a failed check"
    # The hooks are still written — a fixed PATH should not also require re-wiring by hand.
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"]


def test_setup_flushes_stdout_before_the_failure_gets_the_last_word(tmp_path, monkeypatch):
    """The failure message is meant to be the LAST thing on screen, under the checkmarks. It goes to
    stderr (unbuffered) while the checkmarks go to stdout, which is block-buffered whenever it isn't
    a terminal — so without an explicit flush the verdict lands FIRST under a pipe. Correct-looking
    in a bare terminal, inverted everywhere else: the same shape as the cp1252 bug above."""
    flushed = []

    class _TrackingOut(io.StringIO):
        def flush(self):
            flushed.append(self.getvalue())
            super().flush()

    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: "command not found")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: WIN_BIN)
    monkeypatch.setattr(cli.sys, "stdout", _TrackingOut())
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "m.db"), "setup",
              "--settings", str(tmp_path / "settings.json"), "--no-claude-md"])
    assert flushed, "stdout was never flushed, so the verdict can print above the checkmarks"
    assert "wired SessionStart hook" in flushed[-1], "flushed before the checkmarks were written"


def test_verify_hook_bin_accepts_a_binary_that_runs():
    """The happy path goes through a shell, because it is the quoting that broke, not the binary.
    Any real interpreter answers `--help` with exit 0, so it stands in for a working install."""
    assert _REAL_VERIFY(cli._hook_bin(cli.sys.executable)) is None


def test_verify_hook_bin_reports_why_a_broken_binary_failed(tmp_path):
    """A missing binary must come back as a reason string, not an exception or a bare False —
    that string is what setup shows the user, so an empty one is a silent failure again."""
    reason = _REAL_VERIFY(cli._hook_bin(str(tmp_path / "nope" / "engrim")))
    assert isinstance(reason, str) and reason.strip()


# ------------------------------------------------------------- project tags must not split in two

def test_windows_project_tags_converge_on_one_spelling(monkeypatch):
    """The tag is a dict key into your memory. The same directory arrives as `C:\\p` from getcwd()
    and `c:/p` from a hook payload; ungrouped, that is two memories for one project."""
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._norm_project(r"c:/Users/Tim/proj") == cli._norm_project(r"C:\Users\Tim\proj")
    assert cli._norm_project(r"c:\Users\Tim\proj\.") == cli._norm_project(r"C:\Users\Tim\proj")
    assert cli._norm_project(r"c:\Users\Tim\proj").startswith("C:")   # drive normalised, case kept
    assert "Users" in cli._norm_project(r"c:\Users\Tim\proj")


def test_posix_project_tags_are_untouched(monkeypatch):
    """POSIX paths are already the one true spelling — normalising there would silently re-tag
    every existing record in every store in the wild."""
    monkeypatch.setattr(cli.os, "name", "posix")
    for p in ("/home/tim/engrim", "/home/tim/engrim/.", "/Home/Tim/Engrim"):
        assert cli._norm_project(p) == p


def test_explicit_project_tags_are_never_rewritten(tmp_path, monkeypatch):
    """`-p my-tag` is a user-chosen label, not a path — normalisation must not touch it."""
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._resolve_project("My-Tag") == "My-Tag"
    monkeypatch.setenv("ENGRIM_PROJECT", "Team/Shared")
    assert cli._resolve_project("auto") == "Team/Shared"
