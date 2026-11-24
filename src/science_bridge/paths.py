"""Locate Claude Science's files and this tool's own state directory."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

HOME = Path.home()
CS_HOME = Path(os.environ.get("SCB_CS_HOME", HOME / ".claude-science"))
CS_BIN = Path(os.environ.get("SCB_CS_BIN", CS_HOME / "bin" / "claude-science"))
STATE_DIR = Path(os.environ.get("SCB_STATE_DIR", HOME / ".science-bridge"))
BACKUP_LOG = Path(
    os.environ.get("SCB_BACKUP_LOG", HOME / "Library" / "Logs" / "claude-science-backup.log")
)


def data_dir() -> Path:
    """Data folder: `data_dir` in config.toml if set, else ~/.claude-science."""
    cfg = CS_HOME / "config.toml"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("["):
                break
            m = re.match(r"\s*data_dir\s*=\s*['\"]?([^'\"#]+?)['\"]?\s*(#.*)?$", line)
            if m:
                return Path(os.path.expanduser(m.group(1).strip()))
    return CS_HOME


def db_path() -> Path:
    """The active org's operon-cli.db (override with SCB_DB)."""
    if env := os.environ.get("SCB_DB"):
        return Path(env)
    org = json.loads((CS_HOME / "active-org.json").read_text())["org_uuid"]
    return data_dir() / "orgs" / org / "operon-cli.db"


def state_dir() -> Path:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STATE_DIR
