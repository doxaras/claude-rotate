"""confidential.json: claude-rotate's secrets, in one place for both of its users.

setup.py is the only writer (chmod 600); rotator.py's load_config reads it.
Standard library only and import-safe, like codex_image.py: setup.py imports
it without loading rotator's config.
"""
import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass(slots=True)
class Confidential:
    """The three sections of confidential.json. A section left out, or null, is
    empty."""
    accounts: dict[str, str] = field(default_factory=dict)  # Claude account name -> setup-token
    codex_accounts: dict[str, str] = field(default_factory=dict)  # Codex account name -> CODEX_HOME
    devices: dict[str, str] = field(default_factory=dict)  # device name -> device key
    # Top-level keys that are no section (a newer version's, say): kept as they
    # are, so that setup.py's rewrite of the file never drops them.
    unknown_keys: dict[str, object] = field(default_factory=dict)


CONFIDENTIAL_SECTIONS = tuple(f.name for f in fields(Confidential) if f.name != "unknown_keys")


def read_confidential(path: Path) -> Confidential:
    """The file at `path`. An unknown top-level key is reported by its name
    only - never its value - and kept in unknown_keys."""
    data = json.loads(path.read_text())
    unknown = {k: v for k, v in data.items() if k not in CONFIDENTIAL_SECTIONS}
    if unknown:
        print(f"warning: unknown key(s) in {path.name}, not used and left as they are: "
              f"{', '.join(sorted(unknown))}", file=sys.stderr)
    sections = {k: v for k, v in data.items() if k in CONFIDENTIAL_SECTIONS and v is not None}
    return Confidential(**sections, unknown_keys=unknown)


def write_private(path: Path, text: str) -> None:
    """Write through a temporary file created 0600 and a rename: no reader ever
    sees half a file, and the secrets inside are never world-readable."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)  # O_CREAT's mode only applies to a new file
    tmp.replace(path)


def write_confidential(path: Path, confidential: Confidential) -> None:
    """The sections, then any unknown keys read with them."""
    data = asdict(confidential)
    unknown = data.pop("unknown_keys")
    write_private(path, json.dumps({**data, **unknown}, indent=2) + "\n")
