"""load_config: confidential.json + config.json5 merge into one config, and the
safety guards — a placeholder device key must never boot (GitHub issue #2
regression), an old single-file config.json asks for a migration, secrets in
config.json5 are refused, and an account without a token is skipped.

  python3 tests/test_config_guard.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R


def _load_with(confidential: dict | None, settings: str | None = None, legacy: dict | None = None):
    tmp = Path(tempfile.mkdtemp())
    if confidential is not None:
        (tmp / "confidential.json").write_text(json.dumps(confidential))
    if settings is not None:
        (tmp / "config.json5").write_text(settings)
    if legacy is not None:
        (tmp / "config.json").write_text(json.dumps(legacy))
    saved = R.CONFIDENTIAL_PATH, R.SETTINGS_PATH, R.LEGACY_CONFIG_PATH, R.ROOT
    R.CONFIDENTIAL_PATH, R.SETTINGS_PATH = tmp / "confidential.json", tmp / "config.json5"
    R.LEGACY_CONFIG_PATH, R.ROOT = tmp / "config.json", tmp
    try:
        return R.load_config()
    finally:
        R.CONFIDENTIAL_PATH, R.SETTINGS_PATH, R.LEGACY_CONFIG_PATH, R.ROOT = saved


def _refused(confidential, settings=None, legacy=None) -> str:
    try:
        _load_with(confidential, settings, legacy)
    except SystemExit as e:
        return str(e)
    raise AssertionError("must refuse startup")


ACCT = {"a": "sk-ant-oat01-test"}
DEV = {"laptop": "dev-laptop-a1b2c3"}


def test_placeholder_device_key_refuses_startup():
    msg = _refused({"accounts": ACCT, "devices": {"my-laptop": "dev-my-laptop-REPLACE-WITH-RANDOM-HEX"}})
    assert "placeholder" in msg, msg


def test_real_keys_boot():
    cfg = _load_with({"accounts": ACCT, "devices": DEV})
    assert cfg["accounts"] == [{"name": "a", "token": "sk-ant-oat01-test"}], cfg["accounts"]
    assert cfg["devices"] == DEV
    assert cfg["codex_accounts"] == []


def test_account_without_token_is_skipped_not_fatal():
    cfg = _load_with({"accounts": {**ACCT, "b": ""}, "devices": DEV})
    assert [a["name"] for a in cfg["accounts"]] == ["a"]


def test_settings_are_json5_with_comments_and_account_options():
    cfg = _load_with({"accounts": {**ACCT, "b": "sk-ant-oat01-b"}, "devices": DEV}, """
        // a comment line
        {
          "switch_threshold": 0.9,  // same-line comment
          strategy: 'least-used',   /* unquoted key, single quotes */
          "account_options": {"b": {"priority": 2, "disabled": true}},
        }""")
    assert cfg["switch_threshold"] == 0.9 and cfg["strategy"] == "least-used"
    by_name = {a["name"]: a for a in cfg["accounts"]}
    assert by_name["b"] == {"name": "b", "token": "sk-ant-oat01-b", "priority": 2, "disabled": True}
    assert by_name["a"] == {"name": "a", "token": "sk-ant-oat01-test"}


def test_missing_settings_file_means_defaults():
    cfg = _load_with({"accounts": ACCT, "devices": DEV})
    assert "switch_threshold" not in cfg  # rotator's CFG.get defaults apply


def test_secrets_in_settings_are_refused():
    msg = _refused({"accounts": ACCT, "devices": DEV}, '{"devices": {"x": "dev-x-1"}}')
    assert "confidential.json" in msg, msg


def test_old_single_file_config_asks_for_migration():
    msg = _refused(None, legacy={"accounts": [], "devices": {}})
    assert "setup.py migrate" in msg, msg


def test_nothing_configured_asks_for_setup():
    msg = _refused(None)
    assert "./setup.py" in msg, msg


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f()
        print(f"ok   {n}")
    print(f"{len(fns)} passed")
