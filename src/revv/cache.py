"""On-disk cache so pull requests and the inbox open instantly.

What's stored (under $XDG_CACHE_HOME/revv, or ~/.cache/revv):

* the last loaded state of each pull request, shown immediately on open and then
  refreshed from GitHub in the background;
* file contents, keyed by commit and path (they never change);
* the last inbox of each scope.

Entries are pickled and compressed, written atomically, readable only by you, and
simply ignored if they can't be read (e.g. after an upgrade changed the format).
Old entries are pruned automatically.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pickle
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

from revv.models import AiCheck, PRRef, PullRequest, RepoRef


def _schema() -> str:
    """A fingerprint of the cached data classes: any change to their fields invalidates
    old cache entries automatically, instead of unpickling objects with missing fields."""
    import dataclasses

    from revv import models

    parts = []
    for name, value in sorted(vars(models).items()):
        if isinstance(value, type) and dataclasses.is_dataclass(value):
            parts.append(name + ":" + ",".join(f.name for f in dataclasses.fields(value)))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


FORMAT = f"4-{_schema()}"
PR_MAX_AGE = 30 * 86400
BLOB_MAX_AGE = 14 * 86400
BLOB_MAX_BYTES = 256 * 1024 * 1024

_MISSING = object()


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "revv"


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)


class DiskCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_cache_dir()

    # -- plumbing ----------------------------------------------------------------

    def _read(self, path: Path) -> Any:
        try:
            raw = path.read_bytes()
            version, value = pickle.loads(zlib.decompress(raw))
        except FileNotFoundError:
            return _MISSING
        except Exception:
            with contextlib.suppress(OSError):
                path.unlink()
            return _MISSING
        if version != FORMAT:
            return _MISSING
        return value

    def _write(self, path: Path, value: Any) -> None:
        data = zlib.compress(pickle.dumps((FORMAT, value), pickle.HIGHEST_PROTOCOL), 1)
        self.write_bytes(path, data)

    def encode(self, value: Any) -> bytes:
        """Serialize now (on the caller's thread); write later with `write_bytes`."""
        return pickle.dumps((FORMAT, value), pickle.HIGHEST_PROTOCOL)

    def write_bytes(self, path: Path, data: bytes, *, compress: bool = False) -> None:
        if compress:
            data = zlib.compress(data, 1)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(handle, "wb") as out:
                    out.write(data)
                os.replace(temporary, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError:
            pass  # a cache that can't be written is just a slower revv

    # -- pull requests -------------------------------------------------------------

    def pr_path(self, ref: PRRef) -> Path:
        repo = ref.repo
        return (
            self.root
            / "prs"
            / _slug(repo.host)
            / _slug(repo.owner)
            / _slug(repo.name)
            / f"{ref.number}.bin"
        )

    def load_pr(self, ref: PRRef) -> PullRequest | None:
        value = self._read(self.pr_path(ref))
        return value if isinstance(value, PullRequest) else None

    def pr_saved_at(self, ref: PRRef) -> float | None:
        try:
            return self.pr_path(ref).stat().st_mtime
        except OSError:
            return None

    def save_pr(self, pr: PullRequest) -> None:
        self._write(self.pr_path(pr.ref), pr)

    # -- file contents ---------------------------------------------------------------

    def blob_path(self, repo: RepoRef, oid: str, path: str) -> Path:
        digest = hashlib.sha256(f"{oid}:{path}".encode()).hexdigest()
        return self.root / "blobs" / _slug(repo.host) / digest[:2] / digest

    def load_blob(self, repo: RepoRef, oid: str, path: str) -> tuple[bool, str | None]:
        """(found, text). A cached None means "binary or missing"."""
        value = self._read(self.blob_path(repo, oid, path))
        if value is _MISSING:
            return False, None
        return True, value

    def save_blob(self, repo: RepoRef, oid: str, path: str, text: str | None) -> None:
        self._write(self.blob_path(repo, oid, path), text)

    # -- AI checks of descriptions (panc) ---------------------------------------------

    def ai_check_path(self, ref: PRRef) -> Path:
        repo = ref.repo
        return (
            self.root
            / "panc"
            / _slug(repo.host)
            / _slug(repo.owner)
            / _slug(repo.name)
            / f"{ref.number}.bin"
        )

    def load_ai_check(self, ref: PRRef) -> AiCheck | None:
        value = self._read(self.ai_check_path(ref))
        return value if isinstance(value, AiCheck) else None

    def save_ai_check(self, ref: PRRef, check: AiCheck) -> None:
        self._write(self.ai_check_path(ref), check)

    # -- inbox -----------------------------------------------------------------------

    def inbox_path(self, host: str, scope: str) -> Path:
        return self.root / "inbox" / _slug(host) / f"{_slug(scope) or 'all'}.bin"

    def load_inbox(self, host: str, scope: str) -> Any:
        value = self._read(self.inbox_path(host, scope))
        return None if value is _MISSING else value

    def save_inbox(self, host: str, scope: str, value: Any) -> None:
        self._write(self.inbox_path(host, scope), value)

    # -- housekeeping ----------------------------------------------------------------

    def prune(self) -> None:
        """Drop stale entries and keep the file cache under its size budget."""
        now = time.time()
        self._prune_tree(self.root / "prs", now - PR_MAX_AGE)
        blobs = self._prune_tree(self.root / "blobs", now - BLOB_MAX_AGE)
        total = sum(size for _, size, _ in blobs)
        for path, size, _ in sorted(blobs, key=lambda entry: entry[2]):
            if total <= BLOB_MAX_BYTES:
                break
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size

    @staticmethod
    def _prune_tree(root: Path, cutoff: float) -> list[tuple[Path, int, float]]:
        kept: list[tuple[Path, int, float]] = []
        if not root.exists():
            return kept
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    path.unlink()
                else:
                    kept.append((path, stat.st_size, stat.st_mtime))
            except OSError:
                continue
        return kept
