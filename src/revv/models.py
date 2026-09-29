"""Plain data models for a pull request under review.

These are deliberately independent of both the GitHub API shape and the UI, so the
same objects are produced by the real GitHub backend and by the offline demo backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Side(StrEnum):
    """Which side of the diff a comment is anchored to."""

    LEFT = "LEFT"  # the base ("old") version
    RIGHT = "RIGHT"  # the head ("new") version


class FileStatus(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"

    @property
    def letter(self) -> str:
        return {
            FileStatus.ADDED: "A",
            FileStatus.REMOVED: "D",
            FileStatus.MODIFIED: "M",
            FileStatus.RENAMED: "R",
            FileStatus.COPIED: "C",
            FileStatus.CHANGED: "T",
            FileStatus.UNCHANGED: "·",
        }[self]


class ViewedState(StrEnum):
    VIEWED = "VIEWED"
    UNVIEWED = "UNVIEWED"
    DISMISSED = "DISMISSED"  # was viewed, but the file changed since


class ReviewEvent(StrEnum):
    COMMENT = "COMMENT"
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


REACTION_EMOJI: dict[str, str] = {
    "THUMBS_UP": "👍",
    "THUMBS_DOWN": "👎",
    "LAUGH": "😄",
    "HOORAY": "🎉",
    "CONFUSED": "😕",
    "HEART": "❤️",
    "ROCKET": "🚀",
    "EYES": "👀",
}


@dataclass(frozen=True, slots=True)
class RepoRef:
    owner: str
    name: str
    host: str = "github.com"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class PRRef:
    repo: RepoRef
    number: int

    def __str__(self) -> str:
        return f"{self.repo.full_name}#{self.number}"

    @property
    def web_url(self) -> str:
        return f"{self.repo.web_url}/pull/{self.number}"


@dataclass(slots=True)
class Reaction:
    content: str
    count: int
    viewer_has_reacted: bool = False

    @property
    def emoji(self) -> str:
        return REACTION_EMOJI.get(self.content, self.content.lower())


@dataclass(eq=False)
class Comment:
    """A review comment (inside a thread) or a general conversation comment."""

    id: str
    author: str
    body: str
    created_at: datetime
    url: str = ""
    updated_at: datetime | None = None
    edited: bool = False
    is_pending: bool = False
    viewer_can_update: bool = False
    viewer_can_delete: bool = False
    viewer_can_minimize: bool = False
    viewer_can_unminimize: bool = False
    viewer_can_react: bool = True
    viewer_did_author: bool = False
    is_minimized: bool = False
    minimized_reason: str | None = None
    author_association: str = ""
    reactions: list[Reaction] = field(default_factory=list)
    # Review comments only:
    is_review_comment: bool = False
    review_id: str | None = None
    diff_hunk: str = ""
    reply_to_id: str | None = None

    @property
    def is_resolved(self) -> bool:
        """General comments are "resolved" by minimizing them with the RESOLVED reason."""
        return self.is_minimized and (self.minimized_reason or "").upper() == "RESOLVED"


@dataclass(eq=False)
class ReviewThread:
    id: str
    path: str
    side: Side
    line: int | None
    start_line: int | None = None
    start_side: Side | None = None
    original_line: int | None = None
    original_start_line: int | None = None
    is_resolved: bool = False
    is_outdated: bool = False
    is_file_level: bool = False
    viewer_can_resolve: bool = False
    viewer_can_unresolve: bool = False
    viewer_can_reply: bool = True
    resolved_by: str | None = None
    comments: list[Comment] = field(default_factory=list)
    rev: int = 0  # bumped on every local change, used for render caching

    def touch(self) -> None:
        self.rev += 1

    @property
    def is_pending(self) -> bool:
        return bool(self.comments) and all(c.is_pending for c in self.comments)

    @property
    def has_pending(self) -> bool:
        return any(c.is_pending for c in self.comments)

    @property
    def root(self) -> Comment | None:
        return self.comments[0] if self.comments else None

    @property
    def is_range(self) -> bool:
        return self.start_line is not None and self.start_line != self.line

    @property
    def line_label(self) -> str:
        if self.is_file_level:
            return "file"
        line = self.line if self.line is not None else self.original_line
        start = self.start_line if self.line is not None else self.original_start_line
        if start is not None and start != line:
            return f"L{start}-{line}"
        return f"L{line}" if line is not None else "?"


@dataclass(eq=False)
class Review:
    id: str
    author: str
    state: str  # PENDING / COMMENTED / APPROVED / CHANGES_REQUESTED / DISMISSED
    body: str
    created_at: datetime
    submitted_at: datetime | None = None
    url: str = ""
    viewer_can_update: bool = False
    viewer_can_delete: bool = False
    viewer_can_minimize: bool = False
    viewer_can_unminimize: bool = False
    viewer_did_author: bool = False
    is_minimized: bool = False
    minimized_reason: str | None = None
    comment_count: int = 0
    reactions: list[Reaction] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.is_minimized and (self.minimized_reason or "").upper() == "RESOLVED"


@dataclass(eq=False)
class ChangedFile:
    path: str
    status: FileStatus
    additions: int = 0
    deletions: int = 0
    previous_path: str | None = None
    patch: str | None = None
    viewed: ViewedState = ViewedState.UNVIEWED
    sha: str | None = None

    @property
    def is_viewed(self) -> bool:
        return self.viewed == ViewedState.VIEWED

    @property
    def is_binary_or_large(self) -> bool:
        """GitHub omits the patch for binary files and very large diffs."""
        return self.patch is None and self.status != FileStatus.RENAMED


@dataclass(slots=True)
class Commit:
    oid: str
    headline: str
    author: str
    authored_at: datetime | None = None

    @property
    def short(self) -> str:
        return self.oid[:7]


@dataclass(slots=True)
class Label:
    name: str
    color: str  # hex without '#'


@dataclass(eq=False)
class PullRequest:
    id: str
    ref: PRRef
    title: str
    body: str
    url: str
    state: str  # OPEN / CLOSED / MERGED
    is_draft: bool
    author: str
    created_at: datetime
    updated_at: datetime
    base_ref: str
    head_ref: str
    base_oid: str
    head_oid: str
    viewer_login: str
    viewer_did_author: bool = False
    head_repo: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    review_decision: str | None = None
    checks_state: str | None = None
    mergeable: str = "UNKNOWN"
    labels: list[Label] = field(default_factory=list)
    review_requests: list[str] = field(default_factory=list)
    latest_reviews: dict[str, str] = field(default_factory=dict)  # login -> state
    files: list[ChangedFile] = field(default_factory=list)
    threads: list[ReviewThread] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    total_commits: int = 0

    @property
    def pending_review(self) -> Review | None:
        for review in self.reviews:
            if review.state == "PENDING":
                return review
        return None

    def file(self, path: str) -> ChangedFile | None:
        for f in self.files:
            if f.path == path:
                return f
        return None

    def threads_for(self, path: str) -> list[ReviewThread]:
        return [t for t in self.threads if t.path == path]

    @property
    def pending_comment_count(self) -> int:
        return sum(1 for t in self.threads for c in t.comments if c.is_pending)

    @property
    def unresolved_count(self) -> int:
        return sum(1 for t in self.threads if not t.is_resolved and not t.is_pending)


@dataclass(slots=True)
class PRSummary:
    """A row in the review inbox."""

    ref: PRRef
    title: str
    author: str
    updated_at: datetime
    created_at: datetime | None = None
    is_draft: bool = False
    review_decision: str | None = None
    requested_directly: bool = False
    requested_teams: list[str] = field(default_factory=list)
    my_review_state: str | None = None  # the viewer's latest review, if any
    additions: int = 0
    deletions: int = 0
    comments: int = 0
    head_ref: str = ""
    checks_state: str | None = None
    labels: list[Label] = field(default_factory=list)

    @property
    def review_requested(self) -> bool:
        return self.requested_directly or bool(self.requested_teams)
