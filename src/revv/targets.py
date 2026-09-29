"""Figuring out which repository and pull request the user means."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from revv.models import PRRef, RepoRef

_URL = re.compile(
    r"^(?:https?://)?(?P<host>[^/\s]+)/(?P<owner>[^/\s]+)/(?P<name>[^/\s]+)/pull/(?P<number>\d+)"
)
_FULL_REF = re.compile(r"^(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)#(?P<number>\d+)$")
_NUMBER = re.compile(r"^(?:#|pr\s*|pull\s*)?(?P<number>\d+)$", re.IGNORECASE)
_REPO = re.compile(r"^(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)$")

_REMOTE_PATTERNS = [
    re.compile(
        r"^[\w.-]+@(?P<host>[^:/\s]+):(?:\d+/)?(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?$"
    ),
    re.compile(
        r"^(?:ssh|git|https?|git\+ssh)://(?:[^@/\s]+@)?(?P<host>[^:/\s]+)(?::\d+)?/"
        r"(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?$"
    ),
]


def normalize_host(host: str) -> str:
    host = host.lower()
    if host in ("www.github.com", "ssh.github.com", "api.github.com"):
        return "github.com"
    return host


def parse_pr_ref(text: str, default_repo: RepoRef | None) -> PRRef | None:
    """Accepts `123`, `#123`, `owner/repo#123` and pull request URLs."""
    text = text.strip()
    if match := _URL.match(text):
        repo = RepoRef(match["owner"], match["name"], normalize_host(match["host"]))
        return PRRef(repo, int(match["number"]))
    if match := _FULL_REF.match(text):
        host = default_repo.host if default_repo else "github.com"
        return PRRef(RepoRef(match["owner"], match["name"], host), int(match["number"]))
    if (match := _NUMBER.match(text)) and default_repo is not None:
        return PRRef(default_repo, int(match["number"]))
    return None


def parse_repo(text: str, host: str = "github.com") -> RepoRef | None:
    text = text.strip().removesuffix(".git")
    if match := _REPO.match(text):
        return RepoRef(match["owner"], match["name"], host)
    for pattern in _REMOTE_PATTERNS:
        if match := pattern.match(text):
            return RepoRef(match["owner"], match["name"], normalize_host(match["host"]))
    if text.startswith(("http://", "https://")):
        parts = text.split("/")
        if len(parts) >= 5:
            return RepoRef(parts[3], parts[4], normalize_host(parts[2]))
    return None


def parse_remote_url(url: str) -> RepoRef | None:
    url = url.strip()
    for pattern in _REMOTE_PATTERNS:
        if match := pattern.match(url):
            return RepoRef(match["owner"], match["name"], normalize_host(match["host"]))
    return None


def detect_repo(cwd: Path | None = None) -> RepoRef | None:
    """The GitHub repository of the git checkout we are in (preferring `upstream`)."""
    try:
        out = subprocess.run(
            ["git", "remote", "-v"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    remotes: dict[str, RepoRef] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            repo = parse_remote_url(parts[1])
            if repo is not None:
                remotes[parts[0]] = repo
    for name in ("upstream", "origin"):
        if name in remotes:
            return remotes[name]
    return next(iter(remotes.values()), None)
