"""load_config safety guards: a placeholder device key must never boot
(GitHub issue #2 regression), and missing token files are skipped.

  python3 tests/test_config_guard.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R


def _load_with(cfg: dict):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "tokens").mkdir()
    (tmp / "tokens" / "a.token").write_text("sk-ant-oat01-test")
    (tmp / "config.json").write_text(json.dumps(cfg))
    old_cfg, old_root = R.CONFIG_PATH, R.ROOT
    R.CONFIG_PATH, R.ROOT = tmp / "config.json", tmp
    try:
        return R.load_config()
    finally:
        R.CONFIG_PATH, R.ROOT = old_cfg, old_root


ACCT = [{"name": "a", "token_file": "tokens/a.token"}]


def test_placeholder_device_key_refuses_startup():
    try:
        _load_with({"accounts": ACCT,
                    "devices": {"my-laptop": "dev-my-laptop-REPLACE-WITH-RANDOM-HEX"}})
        raise AssertionError("placeholder key must refuse startup")
    except SystemExit as e:
        assert "placeholder" in str(e), str(e)


def test_real_keys_boot():
    cfg = _load_with({"accounts": ACCT, "devices": {"laptop": "dev-laptop-a1b2c3"}})
    assert cfg["accounts"][0]["token"] == "sk-ant-oat01-test"


def test_missing_token_file_is_skipped_not_fatal():
    cfg = _load_with({"accounts": ACCT + [{"name": "b", "token_file": "tokens/nope.token"}],
                      "devices": {"laptop": "dev-laptop-a1b2c3"}})
    assert [a["name"] for a in cfg["accounts"]] == ["a"]


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f()
        print(f"ok   {n}")
    print(f"{len(fns)} passed")
