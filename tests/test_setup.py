"""setup.py: first-time init, add-account / add-device into confidential.json,
and the migration of an old single-file config.json — checked end to end by
loading the result with rotator.load_config.

  python3 tests/test_setup.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import json5
import rotator as R
import setup as S

PATHS = ("CONFIDENTIAL", "SETTINGS", "LEGACY", "LEGACY_BACKUP", "LEGACY_TOKENS", "LOGS", "CODEX")
# onboard must never restart a real claude-rotate service on the machine running the tests
S.restart_service = lambda: "(restart skipped in tests)"


@contextlib.contextmanager
def scratch():
    """Point setup.py (all but EXAMPLE) and rotator's loader at a fresh empty directory."""
    tmp = Path(tempfile.mkdtemp())
    saved = {name: getattr(S, name) for name in PATHS + ("ROOT",)}
    saved_r = R.CONFIDENTIAL_PATH, R.SETTINGS_PATH, R.LEGACY_CONFIG_PATH, R.ROOT
    cwd = os.getcwd()
    for name in PATHS:
        setattr(S, name, tmp / getattr(S, name).name)
    S.ROOT = tmp
    R.CONFIDENTIAL_PATH, R.SETTINGS_PATH = S.CONFIDENTIAL, S.SETTINGS
    R.LEGACY_CONFIG_PATH, R.ROOT = S.LEGACY, tmp
    try:
        yield tmp
    finally:
        for name, value in saved.items():
            setattr(S, name, value)
        R.CONFIDENTIAL_PATH, R.SETTINGS_PATH, R.LEGACY_CONFIG_PATH, R.ROOT = saved_r
        os.chdir(cwd)


def run(*argv: str, stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)  # not a tty: add-account reads a line
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = S.main(list(argv))
    finally:
        sys.stdin = old_stdin
    return status, out.getvalue()


def confidential(tmp: Path) -> dict:
    return json.loads((tmp / "confidential.json").read_text())


def test_init_creates_both_files():
    with scratch() as tmp:
        status, out = run()
        assert status == 0, out
        assert confidential(tmp) == {"accounts": {}, "codex_accounts": {}, "devices": {}}
        assert (tmp / "config.json5").read_text() == S.EXAMPLE.read_text()
        json5.loads(S.EXAMPLE.read_text())  # the example itself parses
        if os.name == "posix":
            assert (tmp / "confidential.json").stat().st_mode & 0o777 == 0o600


def test_add_account_and_device():
    with scratch() as tmp:
        assert run("add-account", "acct-1", stdin="sk-ant-oat01-one\n")[0] == 0
        status, out = run("add-account", "acct-2", stdin="not-a-token\n")
        assert status == 1 and "sk-ant-oat01-" in out, out
        status, out = run("add-device", "laptop")
        assert status == 0, out
        data = confidential(tmp)
        assert data["accounts"] == {"acct-1": "sk-ant-oat01-one"}
        assert data["devices"]["laptop"].startswith("dev-laptop-") and data["devices"]["laptop"] in out
        cfg = R.load_config()
        assert [a.name for a in cfg.accounts] == ["acct-1"]


def test_add_device_drops_a_placeholder():
    with scratch() as tmp:
        run()
        (tmp / "confidential.json").write_text(json.dumps(
            {"accounts": {}, "codex_accounts": {}, "devices": {"x": "dev-x-REPLACE-WITH-HEX"}}))
        run("add-device", "real")
        assert list(confidential(tmp)["devices"]) == ["real"]


def test_a_rewrite_keeps_unknown_keys_and_fills_null_sections():
    with scratch() as tmp:
        run()
        (tmp / "confidential.json").write_text(json.dumps(
            {"accounts": {}, "devices": None, "newer": {"x": 1}}))
        run("add-device", "real")
        assert confidential(tmp) == {"accounts": {}, "codex_accounts": {},
                                     "devices": {"real": confidential(tmp)["devices"]["real"]},
                                     "newer": {"x": 1}}


def test_onboard_refuses_an_existing_name():
    with scratch():
        assert run("onboard", "maria", "laptop")[0] == 0
        status, out = run("onboard", "maria", "laptop")
        assert status == 1 and "already exists" in out, out


def test_migrate_splits_an_old_config():
    with scratch() as tmp:
        (tmp / "tokens").mkdir()
        (tmp / "tokens" / "a.token").write_text("sk-ant-oat01-a\n")
        (tmp / "codex" / "plus-1").mkdir(parents=True)
        (tmp / "codex" / "plus-1" / "auth.json").write_text("{}")
        (tmp / "config.json").write_text(json.dumps({
            "bind": "10.0.0.5", "switch_threshold": 0.9,
            "accounts": [{"name": "a", "token_file": "tokens/a.token", "priority": 1},
                         {"name": "gone", "token_file": "tokens/gone.token"}],
            "codex_accounts": [{"name": "plus-1", "codex_home": "codex/plus-1", "disabled": True}],
            "devices": {"laptop": "dev-laptop-1"},
            "alerts": {"util_warn": 0.7},
        }))
        status, out = run("migrate")
        assert status == 0, out
        assert "gone" in out  # its token file is missing: reported, not migrated
        assert not (tmp / "config.json").exists() and (tmp / "config.json.migrated").exists()
        assert confidential(tmp) == {"accounts": {"a": "sk-ant-oat01-a"},
                                     "codex_accounts": {"plus-1": "codex/plus-1"},
                                     "devices": {"laptop": "dev-laptop-1"}}
        settings = json5.loads((tmp / "config.json5").read_text())
        assert settings == {"bind": "10.0.0.5", "switch_threshold": 0.9, "alerts": {"util_warn": 0.7},
                            "account_options": {"a": {"priority": 1}, "plus-1": {"disabled": True}}}
        cfg = R.load_config()
        assert cfg.accounts == [R.Account_config(name="a", token="sk-ant-oat01-a", priority=1)]
        assert cfg.codex_accounts[0].disabled is True
        status, out = run("migrate")
        assert status == 0 and "nothing to migrate" in out, out


def test_migrate_leaves_an_existing_config_json5_alone():
    with scratch() as tmp:
        (tmp / "config.json5").write_text("// mine\n{}\n")
        (tmp / "config.json").write_text(json.dumps({"accounts": [], "devices": {}}))
        status, out = run("migrate")
        assert status == 0 and "left as it is" in out, out
        assert (tmp / "config.json5").read_text() == "// mine\n{}\n"


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f()
        print(f"ok   {n}")
    print(f"{len(fns)} passed")
