"""Tiny persistent settings (theme, layout) in ~/.config/revv/config.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "revv" / "config.json"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def save_config(**values: Any) -> None:
    config = load_config() | values
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
    except OSError:
        pass
