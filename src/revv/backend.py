"""The interface between the review session and wherever pull requests live."""

from __future__ import annotations

from typing import Protocol

from revv.models import (
    ChangedFile,
    Comment,
    Fingerprint,
    PRRef,
    PRSummary,
    PullRequest,
    Review,
    ReviewEvent,
    ReviewThread,
    Side,
)


class Backend(Protocol):
    """Everything revv needs from a code host. Implemented for GitHub and for the demo."""

    host: str

    async def load_pull_request(self, ref: PRRef, reuse: PullRequest | None = None) -> PullRequest:
        """Load a PR. With `reuse`, patches are reused if the head commit is unchanged."""
        ...

    async def file_contents(
        self, pr: PullRequest, requests: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str | None]:
        """Fetch file text for (oid, path) pairs; None for binary/missing/huge files."""
        ...

    async def compare(self, pr: PullRequest, base: str, head: str) -> list[ChangedFile]:
        """Files (with patches) changed between two commits, e.g. since your last review."""
        ...

    async def fingerprint(self, ref: PRRef) -> Fingerprint:
        """A cheap summary of the pull request, polled to notice changes."""
        ...

    async def search_pull_requests(self, query: str) -> list[PRSummary]:
        """A quick search; the slower fields are filled in by `pull_request_details`."""
        ...

    async def pull_request_details(self, items: list[PRSummary]) -> None:
        """Fill in size, checks, review decision and reviewers of inbox rows, in place."""
        ...

    async def start_review(self, pr: PullRequest) -> Review: ...

    async def submit_review(
        self, pr: PullRequest, review_id: str | None, event: ReviewEvent, body: str
    ) -> Review: ...

    async def delete_review(self, review_id: str) -> None: ...

    async def update_review_body(self, review_id: str, body: str) -> Review: ...

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
    ) -> ReviewThread: ...

    async def add_reply(self, review_id: str, thread_id: str, body: str) -> Comment: ...

    async def update_review_comment(self, comment_id: str, body: str) -> Comment: ...

    async def delete_review_comment(self, comment_id: str) -> None: ...

    async def set_thread_resolved(
        self, thread_id: str, resolved: bool
    ) -> tuple[bool, str | None, bool, bool]:
        """Returns (is_resolved, resolved_by, viewer_can_resolve, viewer_can_unresolve)."""
        ...

    async def add_issue_comment(self, pr: PullRequest, body: str) -> Comment: ...

    async def update_issue_comment(self, comment_id: str, body: str) -> Comment: ...

    async def delete_issue_comment(self, comment_id: str) -> None: ...

    async def set_minimized(
        self, subject_id: str, minimized: bool, reason: str = "RESOLVED"
    ) -> tuple[bool, str | None]: ...

    async def set_viewed(self, pr: PullRequest, path: str, viewed: bool) -> None: ...

    async def set_viewed_many(self, pr: PullRequest, paths: list[str], viewed: bool) -> None:
        """Mark (or unmark) several files as viewed, in as few requests as possible."""
        ...

    async def set_reaction(self, subject_id: str, content: str, add: bool) -> None: ...

    async def aclose(self) -> None: ...
