"""An offline, in-memory backend with a realistic pull request, for `revv --demo` and tests."""

from __future__ import annotations

import asyncio
import copy
import itertools
import random
from datetime import UTC, datetime, timedelta

from revv.diff import make_patch
from revv.models import (
    ChangedFile,
    Comment,
    Commit,
    FileStatus,
    Label,
    PRRef,
    PRSummary,
    PullRequest,
    Reaction,
    RepoRef,
    Review,
    ReviewEvent,
    ReviewThread,
    Side,
    ViewedState,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
VIEWER = "you"
AUTHOR = "octocat"
DEMO_REF = PRRef(RepoRef("acme", "netkit"), 42)

CLIENT_OLD = '''\
"""A tiny HTTP client used across netkit."""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from netkit.legacy_backoff import sleep_backoff

DEFAULT_TIMEOUT = 10.0


@dataclass
class Response:
    """What came back from the server."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding)

    def header(self, name: str, default: str | None = None) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default


@dataclass
class Client:
    """Talks to one HTTP API, adding default headers to every request."""

    base_url: str
    timeout: float = DEFAULT_TIMEOUT
    headers: dict[str, str] = field(default_factory=dict)

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def request(self, method: str, path: str, body: bytes | None = None) -> Response:
        req = urllib.request.Request(self._url(path), data=body, method=method)
        for key, value in self.headers.items():
            req.add_header(key, value)
        attempts = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return Response(resp.status, dict(resp.headers), resp.read())
            except OSError:
                attempts += 1
                if attempts > 3:
                    raise
                sleep_backoff(attempts)

    def get(self, path: str) -> Response:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Response:
        body = json.dumps(payload).encode("utf-8")
        return self.request("POST", path, body)
'''

CLIENT_NEW = '''\
"""A tiny HTTP client used across netkit."""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from netkit.retry import RetryPolicy, retrying

DEFAULT_TIMEOUT = 10.0
log = logging.getLogger(__name__)


@dataclass
class Response:
    """What came back from the server."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding)

    def header(self, name: str, default: str | None = None) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class Client:
    """Talks to one HTTP API, adding default headers to every request."""

    base_url: str
    timeout: float = DEFAULT_TIMEOUT
    headers: dict[str, str] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def request(self, method: str, path: str, body: bytes | None = None) -> Response:
        req = urllib.request.Request(self._url(path), data=body, method=method)
        for key, value in self.headers.items():
            req.add_header(key, value)
        for attempt in retrying(self.retry):
            with attempt:
                log.debug("%s %s (attempt %d)", method, path, attempt.number)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return Response(resp.status, dict(resp.headers), resp.read())
        raise AssertionError("unreachable: retrying() re-raises the last error")

    def get(self, path: str) -> Response:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Response:
        body = json.dumps(payload).encode("utf-8")
        return self.request("POST", path, body)

    def delete(self, path: str) -> Response:
        return self.request("DELETE", path)
'''

RETRY_NEW = '''\
"""Retry policies with exponential backoff and jitter."""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from dataclasses import dataclass

RETRYABLE = (ConnectionError, TimeoutError)


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to try, and how long to wait in between."""

    attempts: int = 4
    base_delay: float = 0.25
    max_delay: float = 8.0
    jitter: float = 0.1

    def delay(self, attempt: int) -> float:
        """Seconds to sleep after `attempt` (1-based) failed, with jitter."""
        delay = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return delay * (1 + random.uniform(-self.jitter, self.jitter))


class Attempt:
    """One try. Use it as a context manager around the code being retried."""

    def __init__(self, number: int, policy: RetryPolicy) -> None:
        self.number = number
        self.policy = policy
        self.failed = False

    def __enter__(self) -> Attempt:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None or not isinstance(exc, RETRYABLE):
            return False
        if self.number >= self.policy.attempts:
            return False  # out of attempts: let the error propagate
        self.failed = True
        time.sleep(self.policy.delay(self.number))
        return True


def retrying(policy: RetryPolicy) -> Iterator[Attempt]:
    """Yield attempts until one succeeds or the policy gives up."""
    for number in range(1, policy.attempts + 1):
        attempt = Attempt(number, policy)
        yield attempt
        if not attempt.failed:
            return
'''

TEST_RETRY_NEW = """\
import pytest

from netkit.retry import RetryPolicy, retrying


def test_delay_grows_exponentially():
    policy = RetryPolicy(base_delay=1, max_delay=100, jitter=0)
    assert [policy.delay(n) for n in range(1, 5)] == [1, 2, 4, 8]


def test_delay_is_capped():
    policy = RetryPolicy(base_delay=1, max_delay=5, jitter=0)
    assert policy.delay(10) == 5


def test_gives_up_after_all_attempts(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    calls = 0
    with pytest.raises(ConnectionError):
        for attempt in retrying(RetryPolicy(attempts=3)):
            with attempt:
                calls += 1
                raise ConnectionError("boom")
    assert calls == 3


def test_other_errors_are_not_retried():
    calls = 0
    with pytest.raises(ValueError):
        for attempt in retrying(RetryPolicy(attempts=5)):
            with attempt:
                calls += 1
                raise ValueError("not a network problem")
    assert calls == 1
"""

LEGACY_OLD = '''\
"""Deprecated: use netkit.retry instead."""

import time


def sleep_backoff(attempt: int) -> None:
    """Sleep a little longer after every failed attempt."""
    time.sleep(0.5 * attempt)
'''

README_OLD = """\
# netkit

Small, dependency-free networking helpers.

## Usage

```python
from netkit.client import Client

api = Client("https://api.example.com", headers={"Authorization": "Bearer ..."})
print(api.get("/status").json())
```

## Development

Run the tests with `pytest`.
"""

README_NEW = """\
# netkit

Small, dependency-free networking helpers.

## Usage

```python
from netkit.client import Client

api = Client("https://api.example.com", headers={"Authorization": "Bearer ..."})
print(api.get("/status").json())
```

## Retries

Requests that fail with a connection error or a timeout are retried with
exponential backoff. Tune it per client:

```python
from netkit.retry import RetryPolicy

api = Client("https://api.example.com", retry=RetryPolicy(attempts=6, max_delay=30))
```

## Development

Run the tests with `pytest`.
"""

BADGE_OLD = """\
import { type FC } from "react";

type Status = "ok" | "degraded" | "down";

interface Props {
  status: Status;
  label?: string;
}

const COLORS: Record<Status, string> = {
  ok: "green",
  degraded: "orange",
  down: "red",
};

export const StatusBadge: FC<Props> = ({ status, label }) => {
  const color = COLORS[status];
  return (
    <span className={`badge badge-${color}`}>
      {label ?? status}
    </span>
  );
};
"""

BADGE_NEW = """\
import { type FC } from "react";

type Status = "ok" | "degraded" | "down" | "retrying";

interface Props {
  status: Status;
  label?: string;
  /** Number of the current retry attempt, shown while retrying. */
  attempt?: number;
}

const COLORS: Record<Status, string> = {
  ok: "green",
  degraded: "orange",
  down: "red",
  retrying: "yellow",
};

export const StatusBadge: FC<Props> = ({ status, label, attempt }) => {
  const color = COLORS[status];
  const text = status === "retrying" && attempt ? `retrying (#${attempt})` : status;
  return (
    <span className={`badge badge-${color}`} aria-live="polite">
      {label ?? text}
    </span>
  );
};
"""

URLS_OLD = """\
from urllib.parse import urljoin, urlparse


def join(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def host_of(url: str) -> str:
    return urlparse(url).netloc
"""

URLS_NEW = '''\
from urllib.parse import urljoin, urlparse


def join(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def host_of(url: str) -> str:
    """The host (and port, if any) of a URL."""
    return urlparse(url).netloc
'''

PYPROJECT_OLD = """\
[project]
name = "netkit"
version = "0.4.2"
description = "Small, dependency-free networking helpers."
requires-python = ">=3.11"
dependencies = []

[tool.pytest.ini_options]
testpaths = ["tests"]
"""

PYPROJECT_NEW = """\
[project]
name = "netkit"
version = "0.5.0"
description = "Small, dependency-free networking helpers."
requires-python = ">=3.11"
dependencies = []

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
"""

BADGE_TEST_NEW = """\
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "./StatusBadge";

test("shows the retry attempt while retrying", () => {
  render(<StatusBadge status="retrying" attempt={2} />);
  expect(screen.getByText("retrying (#2)")).toBeInTheDocument();
});

test("prefers an explicit label", () => {
  render(<StatusBadge status="ok" label="All good" />);
  expect(screen.getByText("All good")).toBeInTheDocument();
});
"""

UV_LOCK_OLD = """\
# This file is automatically @generated by uv.
version = 1
requires-python = ">=3.11"

[[package]]
name = "netkit"
version = "0.4.2"
source = { editable = "." }
"""

UV_LOCK_NEW = """\
# This file is automatically @generated by uv.
version = 1
requires-python = ">=3.11"

[[package]]
name = "netkit"
version = "0.5.0"
source = { editable = "." }
"""

BODY = """\
## Summary

Replaces the ad-hoc retry loop in `Client.request` with a reusable **`RetryPolicy`**:

- exponential backoff with jitter, capped at `max_delay`
- only connection errors and timeouts are retried
- `legacy_backoff` is gone 🎉

The status badge in the dashboard learns a `retrying` state so people can see what is going on.

## Testing

- [x] unit tests for the policy
- [ ] try it against the staging API

Closes #37.
"""


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    raise ValueError(f"{needle!r} not found")


class DemoBackend:
    """Implements the Backend protocol entirely in memory."""

    host = "github.com"

    def __init__(self, latency: float = 0.12) -> None:
        self.latency = latency
        self._ids = itertools.count(1000)
        self._texts: dict[str, str] = {}
        self._pr = self._build()

    async def _wait(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency * random.uniform(0.5, 1.5))

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    def _comment(
        self, author: str, body: str, hours_ago: float, *, review: bool = True, **extra
    ) -> Comment:
        mine = author == VIEWER
        comment = Comment(
            id=self._id("RC" if review else "IC"),
            author=author,
            body=body,
            created_at=NOW - timedelta(hours=hours_ago),
            url=f"{DEMO_REF.web_url}#discussion",
            viewer_can_update=mine,
            viewer_can_delete=mine,
            viewer_can_minimize=True,
            viewer_can_unminimize=True,
            viewer_did_author=mine,
            is_review_comment=review,
            author_association="MEMBER",
        )
        for key, value in extra.items():
            setattr(comment, key, value)
        return comment

    def _build(self) -> PullRequest:
        files = [
            ("src/netkit/client.py", None, CLIENT_OLD, CLIENT_NEW, FileStatus.MODIFIED),
            ("src/netkit/retry.py", None, "", RETRY_NEW, FileStatus.ADDED),
            ("tests/test_retry.py", None, "", TEST_RETRY_NEW, FileStatus.ADDED),
            ("src/netkit/legacy_backoff.py", None, LEGACY_OLD, "", FileStatus.REMOVED),
            ("README.md", None, README_OLD, README_NEW, FileStatus.MODIFIED),
            ("web/src/components/StatusBadge.tsx", None, BADGE_OLD, BADGE_NEW, FileStatus.MODIFIED),
            ("src/netkit/urls.py", "src/netkit/utils.py", URLS_OLD, URLS_NEW, FileStatus.RENAMED),
            ("pyproject.toml", None, PYPROJECT_OLD, PYPROJECT_NEW, FileStatus.MODIFIED),
            ("uv.lock", None, UV_LOCK_OLD, UV_LOCK_NEW, FileStatus.MODIFIED),
            (
                "web/src/components/StatusBadge.test.tsx",
                None,
                "",
                BADGE_TEST_NEW,
                FileStatus.ADDED,
            ),
        ]
        changed: list[ChangedFile] = []
        for path, previous, old, new, status in files:
            patch = make_patch(old, new)
            additions = sum(1 for ln in patch.splitlines() if ln.startswith("+"))
            deletions = sum(1 for ln in patch.splitlines() if ln.startswith("-"))
            changed.append(
                ChangedFile(
                    path=path,
                    status=status,
                    additions=additions,
                    deletions=deletions,
                    previous_path=previous,
                    patch=patch,
                    viewed=ViewedState.VIEWED if path == "pyproject.toml" else ViewedState.UNVIEWED,
                )
            )
            if status != FileStatus.REMOVED:
                self._texts[path] = new
        changed.append(ChangedFile("docs/architecture.png", FileStatus.ADDED, 0, 0))
        changed.sort(key=lambda f: f.path)

        review_mona = Review(
            id=self._id("PRR"),
            author="mona",
            state="CHANGES_REQUESTED",
            body="A couple of questions inline, otherwise this is looking great.",
            created_at=NOW - timedelta(hours=20),
            submitted_at=NOW - timedelta(hours=20),
            comment_count=3,
            viewer_can_minimize=True,
            viewer_can_unminimize=True,
        )
        review_hubot = Review(
            id=self._id("PRR"),
            author="hubot",
            state="COMMENTED",
            body="",
            created_at=NOW - timedelta(hours=18),
            submitted_at=NOW - timedelta(hours=18),
            comment_count=2,
        )
        pending = Review(
            id=self._id("PRR"),
            author=VIEWER,
            state="PENDING",
            body="",
            created_at=NOW - timedelta(minutes=30),
            viewer_can_update=True,
            viewer_can_delete=True,
            viewer_did_author=True,
        )

        threads = [
            ReviewThread(
                id=self._id("PRRT"),
                path="src/netkit/client.py",
                side=Side.RIGHT,
                line=_line_of(CLIENT_NEW, "for attempt in retrying"),
                viewer_can_resolve=True,
                comments=[
                    self._comment(
                        "mona",
                        "Much clearer than the hand-rolled loop 👌\n\n"
                        "Should we log when we finally give up, so it shows up in production logs?",
                        20,
                        review_id=review_mona.id,
                        reactions=[Reaction("THUMBS_UP", 2)],
                    ),
                    self._comment(
                        AUTHOR,
                        "Good call. I'll add a `log.warning` in `Attempt.__exit__` "
                        "when we run out of attempts.",
                        19,
                    ),
                ],
            ),
            ReviewThread(
                id=self._id("PRRT"),
                path="src/netkit/retry.py",
                side=Side.RIGHT,
                line=_line_of(RETRY_NEW, "RETRYABLE = "),
                viewer_can_resolve=True,
                comments=[
                    self._comment(
                        "mona",
                        "Should `urllib.error.URLError` be retryable as well? It wraps "
                        "connection failures, so as written we'd never retry a refused "
                        "connection.",
                        20,
                        review_id=review_mona.id,
                    )
                ],
            ),
            ReviewThread(
                id=self._id("PRRT"),
                path="src/netkit/retry.py",
                side=Side.RIGHT,
                line=_line_of(RETRY_NEW, "delay = min("),
                is_resolved=True,
                resolved_by=AUTHOR,
                viewer_can_unresolve=True,
                comments=[
                    self._comment(
                        "hubot",
                        "```suggestion\n"
                        "        delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))\n"
                        "```\nParentheses make the precedence obvious.",
                        18,
                        review_id=review_hubot.id,
                    ),
                    self._comment(AUTHOR, "`**` binds tighter anyway, but fair enough.", 17),
                ],
            ),
            ReviewThread(
                id=self._id("PRRT"),
                path="tests/test_retry.py",
                side=Side.RIGHT,
                line=_line_of(TEST_RETRY_NEW, "assert calls == 3"),
                start_line=_line_of(TEST_RETRY_NEW, "def test_gives_up_after_all_attempts"),
                start_side=Side.RIGHT,
                viewer_can_resolve=True,
                comments=[
                    self._comment(
                        VIEWER,
                        "Could we also assert the sleep durations here? Capturing the "
                        "arguments passed to `time.sleep` would cover the backoff too.",
                        0.5,
                        is_pending=True,
                        review_id=pending.id,
                    )
                ],
            ),
            ReviewThread(
                id=self._id("PRRT"),
                path="src/netkit/legacy_backoff.py",
                side=Side.LEFT,
                line=_line_of(LEGACY_OLD, "def sleep_backoff"),
                is_resolved=True,
                resolved_by=AUTHOR,
                viewer_can_unresolve=True,
                comments=[self._comment("mona", "Happy to see this one go 🎉", 20)],
            ),
            ReviewThread(
                id=self._id("PRRT"),
                path="src/netkit/client.py",
                side=Side.RIGHT,
                line=None,
                original_line=52,
                is_outdated=True,
                viewer_can_resolve=True,
                comments=[
                    self._comment(
                        "mona",
                        "This `while True` loop is hard to follow — could it be a `for` loop?",
                        40,
                        diff_hunk=(
                            "@@ -48,6 +48,15 @@ class Client:\n"
                            "         for key, value in self.headers.items():\n"
                            "             req.add_header(key, value)\n"
                            "+        attempts = 0\n"
                            "+        while True:"
                        ),
                    )
                ],
            ),
            ReviewThread(
                id=self._id("PRRT"),
                path="README.md",
                side=Side.RIGHT,
                line=None,
                is_file_level=True,
                viewer_can_resolve=True,
                comments=[
                    self._comment(
                        "hubot",
                        "Docs look good. Maybe mention which exceptions are retried?",
                        18,
                        review_id=review_hubot.id,
                    )
                ],
            ),
        ]
        comments = [
            self._comment(
                "ci-bot",
                "⚠️ Coverage decreased by **0.4%** (`netkit/client.py`).",
                30,
                review=False,
                is_minimized=True,
                minimized_reason="RESOLVED",
            ),
            self._comment(
                "mona",
                "Overall LGTM once the retryable-exceptions question is settled. "
                "Could we get a CHANGELOG entry too?",
                20,
                review=False,
                reactions=[Reaction("THUMBS_UP", 1), Reaction("EYES", 1)],
            ),
            self._comment(
                AUTHOR,
                "@mona CHANGELOG entry is coming in the next push, thanks for the review!",
                19,
                review=False,
            ),
        ]
        commits = [
            Commit(
                "3f9c2a17b0d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8",
                "Add RetryPolicy with exponential backoff",
                AUTHOR,
                NOW - timedelta(hours=26),
            ),
            Commit(
                "8a1b2c3d4e5f60718293a4b5c6d7e8f901234567",
                "Use RetryPolicy in Client.request",
                AUTHOR,
                NOW - timedelta(hours=25),
            ),
            Commit(
                "c0ffee00112233445566778899aabbccddeeff00",
                "Show retrying state in StatusBadge",
                AUTHOR,
                NOW - timedelta(hours=22),
            ),
            Commit(
                "deadbeef00112233445566778899aabbccddeeff",
                "Remove legacy_backoff",
                AUTHOR,
                NOW - timedelta(hours=21),
            ),
        ]
        return PullRequest(
            id="PR_demo42",
            ref=DEMO_REF,
            title="Retry failed requests with exponential backoff",
            body=BODY,
            url=DEMO_REF.web_url,
            state="OPEN",
            is_draft=False,
            author=AUTHOR,
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(hours=2),
            base_ref="main",
            head_ref="retry-backoff",
            base_oid="b" * 40,
            head_oid="deadbeef00112233445566778899aabbccddeeff",
            viewer_login=VIEWER,
            head_repo="acme/netkit",
            additions=sum(f.additions for f in changed),
            deletions=sum(f.deletions for f in changed),
            changed_files=len(changed),
            review_decision="CHANGES_REQUESTED",
            checks_state="SUCCESS",
            mergeable="MERGEABLE",
            labels=[Label("enhancement", "a2eeef"), Label("networking", "5319e7")],
            review_requests=[VIEWER],
            latest_reviews={"mona": "CHANGES_REQUESTED", "hubot": "COMMENTED"},
            files=changed,
            threads=threads,
            comments=comments,
            reviews=[review_mona, review_hubot, pending],
            commits=commits,
            total_commits=len(commits),
        )

    # -- Backend protocol --------------------------------------------------------

    async def aclose(self) -> None:
        pass

    async def load_pull_request(self, ref: PRRef, reuse: PullRequest | None = None) -> PullRequest:
        await self._wait()
        return copy.deepcopy(self._pr)

    async def file_contents(
        self, pr: PullRequest, requests: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str | None]:
        await self._wait()
        return {(oid, path): self._texts.get(path) for oid, path in requests}

    async def search_pull_requests(self, query: str) -> list[PRSummary]:
        await self._wait()
        pr = self._pr
        main = PRSummary(
            ref=pr.ref,
            title=pr.title,
            author=pr.author,
            updated_at=pr.updated_at,
            created_at=pr.created_at,
            review_decision=pr.review_decision,
            requested_directly=True,
            additions=pr.additions,
            deletions=pr.deletions,
            comments=len(pr.comments),
            head_ref=pr.head_ref,
            checks_state=pr.checks_state,
            labels=pr.labels,
        )

        def summary(number: int, title: str, author: str, hours: float, **extra) -> PRSummary:
            item = PRSummary(
                ref=PRRef(DEMO_REF.repo, number),
                title=title,
                author=author,
                updated_at=NOW - timedelta(hours=hours),
                created_at=NOW - timedelta(hours=hours * 3),
                head_ref=title.lower().split()[0],
                additions=extra.pop("additions", 40),
                deletions=extra.pop("deletions", 12),
            )
            for key, value in extra.items():
                setattr(item, key, value)
            return item

        team = summary(
            17,
            "Bump the minimum Python version to 3.12",
            "mona",
            7,
            requested_teams=["acme/python-reviewers"],
            review_decision="REVIEW_REQUIRED",
            checks_state="SUCCESS",
            additions=17,
            deletions=9,
            comments=2,
        )
        rereview = summary(
            40,
            "Stream large response bodies instead of buffering",
            "hubot",
            3,
            requested_directly=True,
            my_review_state="CHANGES_REQUESTED",
            review_decision="CHANGES_REQUESTED",
            checks_state="PENDING",
            additions=212,
            deletions=64,
            comments=9,
        )
        reviewed = summary(
            55,
            "Docs: explain proxy configuration",
            "mona",
            30,
            my_review_state="APPROVED",
            review_decision="APPROVED",
            checks_state="SUCCESS",
            additions=58,
            deletions=3,
        )
        mine = summary(
            61,
            "Add connection pooling",
            VIEWER,
            50,
            is_draft=True,
            checks_state="FAILURE",
            additions=388,
            deletions=97,
            comments=1,
        )
        for item in (main, rereview, team, reviewed, mine):
            item.node_id = f"PR_{item.ref.number}"
        if "review-requested:@me" in query:
            return [main, rereview, team]
        if "assignee:@me" in query:
            return []
        if "reviewed-by:@me" in query:
            return [rereview, reviewed]
        if "author:@me" in query:
            return [mine]
        return [main, rereview, team, reviewed, mine]

    async def pull_request_details(self, items: list[PRSummary]) -> None:
        await self._wait()
        for item in items:
            item.details_loaded = True

    def _find_thread(self, thread_id: str) -> ReviewThread:
        return next(t for t in self._pr.threads if t.id == thread_id)

    def _find_comment(self, comment_id: str) -> Comment:
        for thread in self._pr.threads:
            for comment in thread.comments:
                if comment.id == comment_id:
                    return comment
        return next(c for c in self._pr.comments if c.id == comment_id)

    async def start_review(self, pr: PullRequest) -> Review:
        await self._wait()
        existing = self._pr.pending_review
        if existing is not None:
            return copy.deepcopy(existing)
        review = Review(
            id=self._id("PRR"),
            author=VIEWER,
            state="PENDING",
            body="",
            created_at=datetime.now(UTC),
            viewer_can_update=True,
            viewer_can_delete=True,
            viewer_did_author=True,
        )
        self._pr.reviews.append(review)
        return copy.deepcopy(review)

    async def submit_review(
        self, pr: PullRequest, review_id: str | None, event: ReviewEvent, body: str
    ) -> Review:
        await self._wait()
        state = {
            ReviewEvent.COMMENT: "COMMENTED",
            ReviewEvent.APPROVE: "APPROVED",
            ReviewEvent.REQUEST_CHANGES: "CHANGES_REQUESTED",
        }[event]
        review = next((r for r in self._pr.reviews if r.id == review_id), None)
        if review is None:
            review = Review(
                id=self._id("PRR"),
                author=VIEWER,
                state=state,
                body=body,
                created_at=datetime.now(UTC),
                viewer_did_author=True,
            )
            self._pr.reviews.append(review)
        review.state, review.body, review.submitted_at = state, body, datetime.now(UTC)
        for thread in self._pr.threads:
            for comment in thread.comments:
                if comment.review_id == review.id:
                    comment.is_pending = False
        if event is not ReviewEvent.COMMENT:
            self._pr.latest_reviews[VIEWER] = state
        return copy.deepcopy(review)

    async def delete_review(self, review_id: str) -> None:
        await self._wait()
        self._pr.reviews = [r for r in self._pr.reviews if r.id != review_id]
        for thread in self._pr.threads:
            thread.comments = [c for c in thread.comments if c.review_id != review_id]
        self._pr.threads = [t for t in self._pr.threads if t.comments]

    async def update_review_body(self, review_id: str, body: str) -> Review:
        await self._wait()
        review = next(r for r in self._pr.reviews if r.id == review_id)
        review.body = body
        return copy.deepcopy(review)

    async def add_thread(
        self,
        pr: PullRequest,
        review_id: str,
        *,
        path: str,
        body: str,
        line: int | None,
        side: Side | None,
        start_line: int | None = None,
        start_side: Side | None = None,
        file_level: bool = False,
    ) -> ReviewThread:
        await self._wait()
        thread = ReviewThread(
            id=self._id("PRRT"),
            path=path,
            side=side or Side.RIGHT,
            line=None if file_level else line,
            start_line=None if file_level else start_line,
            start_side=None if file_level else start_side,
            is_file_level=file_level,
            viewer_can_resolve=True,
            comments=[self._comment(VIEWER, body, 0, is_pending=True, review_id=review_id)],
        )
        self._pr.threads.append(thread)
        return copy.deepcopy(thread)

    async def add_reply(self, review_id: str, thread_id: str, body: str) -> Comment:
        await self._wait()
        comment = self._comment(VIEWER, body, 0, is_pending=True, review_id=review_id)
        self._find_thread(thread_id).comments.append(comment)
        return copy.deepcopy(comment)

    async def update_review_comment(self, comment_id: str, body: str) -> Comment:
        await self._wait()
        comment = self._find_comment(comment_id)
        comment.body, comment.edited = body, True
        return copy.deepcopy(comment)

    async def delete_review_comment(self, comment_id: str) -> None:
        await self._wait()
        for thread in self._pr.threads:
            thread.comments = [c for c in thread.comments if c.id != comment_id]
        self._pr.threads = [t for t in self._pr.threads if t.comments]

    async def set_thread_resolved(
        self, thread_id: str, resolved: bool
    ) -> tuple[bool, str | None, bool, bool]:
        await self._wait()
        thread = self._find_thread(thread_id)
        thread.is_resolved = resolved
        thread.resolved_by = VIEWER if resolved else None
        return resolved, thread.resolved_by, not resolved, resolved

    async def add_issue_comment(self, pr: PullRequest, body: str) -> Comment:
        await self._wait()
        comment = self._comment(VIEWER, body, 0, review=False)
        comment.created_at = datetime.now(UTC)
        self._pr.comments.append(comment)
        return copy.deepcopy(comment)

    async def update_issue_comment(self, comment_id: str, body: str) -> Comment:
        await self._wait()
        comment = self._find_comment(comment_id)
        comment.body, comment.edited = body, True
        return copy.deepcopy(comment)

    async def delete_issue_comment(self, comment_id: str) -> None:
        await self._wait()
        self._pr.comments = [c for c in self._pr.comments if c.id != comment_id]

    async def set_minimized(
        self, subject_id: str, minimized: bool, reason: str = "RESOLVED"
    ) -> tuple[bool, str | None]:
        await self._wait()
        target: Comment | Review | None = next(
            (r for r in self._pr.reviews if r.id == subject_id), None
        )
        if target is None:
            target = self._find_comment(subject_id)
        target.is_minimized = minimized
        target.minimized_reason = reason if minimized else None
        return minimized, target.minimized_reason

    async def set_viewed(self, pr: PullRequest, path: str, viewed: bool) -> None:
        await self._wait()
        file = self._pr.file(path)
        if file is not None:
            file.viewed = ViewedState.VIEWED if viewed else ViewedState.UNVIEWED

    async def set_viewed_many(self, pr: PullRequest, paths: list[str], viewed: bool) -> None:
        await self._wait()
        for path in paths:
            file = self._pr.file(path)
            if file is not None:
                file.viewed = ViewedState.VIEWED if viewed else ViewedState.UNVIEWED

    async def set_reaction(self, subject_id: str, content: str, add: bool) -> None:
        await self._wait()
