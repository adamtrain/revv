"""Persistent settings in ~/.config/revv/config.json ($XDG_CONFIG_HOME is respected).

Everything here can be changed from inside the app, but the file is plain JSON too:

    {
      "theme": "nord",
      "hide_by_default": ["test", "generated"],
      "nicknames": {"octocat": "Octo"},
      "inbox_sort": "asc",
      "refresh_interval": 30,
      "sidebar_width": 34
    }
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "theme": None,
    "hide_by_default": ["test", "generated"],  # file kinds hidden when a PR opens
    "nicknames": {},  # GitHub login -> the name you'd rather see
    "ignored_prs": {},  # "host/owner/repo#number" -> when it was ignored (unix time)
    "inbox_sort": "asc",  # "asc": oldest (lowest number) first; "desc": newest first
    "refresh_interval": 30,  # seconds between checks for changes on GitHub; 0 turns them off
    "sidebar_width": 34,  # columns of the file tree (< and > change it)
    "open_tab": "conversation",  # what a pull request opens on: "conversation" or "files"
    "panc": False,  # check PR descriptions for AI writing with panc (sends them to Pangram)
}

IGNORED_PR_TTL = 180 * 86400  # forget ignored pull requests after half a year


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "revv" / "config.json"


def load_config() -> dict[str, Any]:
    try:
        value = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_config(**values: Any) -> None:
    config = load_config() | values
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def setting(name: str) -> Any:
    """A setting, falling back to its default when missing or of the wrong type."""
    default = DEFAULTS[name]
    value = load_config().get(name, default)
    if default is not None and not isinstance(value, type(default)):
        return default
    return value


# -- nicknames ---------------------------------------------------------------------

_nicknames: dict[str, str] | None = None


def nicknames() -> dict[str, str]:
    global _nicknames
    if _nicknames is None:
        raw = setting("nicknames")
        _nicknames = {
            str(login).lower(): str(name) for login, name in raw.items() if str(name).strip()
        }
    return _nicknames


def display_name(login: str) -> str:
    """How a GitHub user is shown: their nickname if you gave them one."""
    return nicknames().get(login.lower(), login)


def set_nicknames(names: dict[str, str]) -> None:
    """Replace all nicknames."""
    global _nicknames
    cleaned = {login: name.strip() for login, name in names.items() if name.strip()}
    save_config(nicknames=cleaned)
    _nicknames = {login.lower(): name for login, name in cleaned.items()}


def update_nicknames(changes: dict[str, str]) -> None:
    """Set (or, with an empty name, remove) the nicknames of some people, keeping the rest."""
    current = {str(k): str(v) for k, v in setting("nicknames").items()}
    lowered = {login.lower() for login in changes}
    merged = {k: v for k, v in current.items() if k.lower() not in lowered}
    merged |= {login: name for login, name in changes.items() if name.strip()}
    set_nicknames(merged)


# -- ignored pull requests ---------------------------------------------------------


def ignored_prs() -> dict[str, float]:
    raw = setting("ignored_prs")
    now = time.time()
    return {
        key: float(when)
        for key, when in raw.items()
        if isinstance(when, int | float) and now - when < IGNORED_PR_TTL
    }


def set_ignored(key: str, ignored: bool) -> None:
    entries = ignored_prs()
    if ignored:
        entries[key] = time.time()
    else:
        entries.pop(key, None)
    save_config(ignored_prs=entries)
