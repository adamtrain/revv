"""The GitHub implementation of `revv.backend.Backend`."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from revv.github import queries as q
from revv.github.client import GitHubClient, GitHubError
from revv.github.parse import (
    apply_details,
    merge_rest_file,
    parse_graphql_file,
    parse_issue_comment,
    parse_pull_request,
    parse_review,
    parse_review_comment,
    parse_search_results,
    parse_thread,
)
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

_MORE = {
    "files": (q.MORE_FILES, parse_graphql_file, "files"),
    "reviewThreads": (q.MORE_THREADS, parse_thread, "threads"),
    "comments": (q.MORE_COMMENTS, parse_issue_comment, "comments"),
    "reviews": (q.MORE_REVIEWS, parse_review, "reviews"),
}

BLOB_BATCH = 20
VIEWED_BATCH = 25
DETAILS_BATCH = 25
MAX_FILE_PAGES = 30  # GitHub lists at most 3000 files for a pull request
_LAST_PAGE = re.compile(r'[?&]page=(\d+)[^>]*>;\s*rel="last"')


def _clean(input: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in input.items() if value is not None}


class GitHubBackend:
    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self.host = client.host
        self._blob_limit = asyncio.Semaphore(3)

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- loading -----------------------------------------------------------------

    async def _rest_files_page(self, ref: PRRef, page: int) -> httpx.Response:
        return await self.client.rest(
            "GET",
            f"/repos/{ref.repo.owner}/{ref.repo.name}/pulls/{ref.number}/files",
            params={"per_page": 100, "page": page},
        )

    async def _rest_files(self, ref: PRRef) -> list[dict[str, Any]]:
        """All changed files with patches; later pages are fetched in parallel."""
        first = await self._rest_files_page(ref, 1)
        items: list[dict[str, Any]] = first.json()
        match = _LAST_PAGE.search(first.headers.get("link", ""))
        last = min(MAX_FILE_PAGES, int(match.group(1))) if match else 1
        if last > 1:
            pages = await asyncio.gather(
                *(self._rest_files_page(ref, page) for page in range(2, last + 1))
            )
            for response in pages:
                items.extend(response.json())
        return items

    async def _more(self, pr: PullRequest, key: str, cursor: str | None) -> None:
        query, parse, attr = _MORE[key]
        items = getattr(pr, attr)
        while cursor:
            data = await self.client.graphql(query, id=pr.id, after=cursor)
            connection = (data.get("node") or {}).get(key) or {}
            items.extend(parse(node) for node in connection.get("nodes") or [] if node)
            info = connection.get("pageInfo") or {}
            cursor = info.get("endCursor") if info.get("hasNextPage") else None

    async def load_pull_request(self, ref: PRRef, reuse: PullRequest | None = None) -> PullRequest:
        files_task: asyncio.Task[list[dict[str, Any]]] | None = None
        if reuse is None:  # fetch the patches while the GraphQL query is in flight
            files_task = asyncio.create_task(self._rest_files(ref))
        try:
            data = await self.client.graphql(
                q.PULL_REQUEST, owner=ref.repo.owner, name=ref.repo.name, number=ref.number
            )
            try:
                pr, cursors = parse_pull_request(data, ref)
            except LookupError as error:
                raise GitHubError(str(error)) from None
            await asyncio.gather(
                *(self._more(pr, key, cursor) for key, cursor in cursors.items() if cursor)
            )
        except BaseException:
            if files_task is not None:
                files_task.cancel()
            raise

        if reuse is not None and reuse.head_oid == pr.head_oid and reuse.base_oid == pr.base_oid:
            patches = {f.path: f for f in reuse.files}
            for file in pr.files:
                if (old := patches.get(file.path)) is not None:
                    file.patch, file.sha, file.previous_path = old.patch, old.sha, old.previous_path
                    file.status = old.status
            return pr

        items = await files_task if files_task is not None else await self._rest_files(ref)
        by_path: dict[str, ChangedFile] = {f.path: f for f in pr.files}
        ordered: list[ChangedFile] = []
        seen: set[str] = set()
        for item in items:
            file = merge_rest_file(by_path.get(item["filename"]), item)
            if file.path not in seen:
                seen.add(file.path)
                ordered.append(file)
        ordered.extend(f for f in pr.files if f.path not in seen)
        pr.files = ordered
        return pr

    async def _blob_batch(
        self, pr: PullRequest, batch: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str | None]:
        variables = {f"e{i}": f"{oid}:{path}" for i, (oid, path) in enumerate(batch)}
        async with self._blob_limit:
            data = await self.client.graphql(
                q.blobs_query(len(batch)),
                owner=pr.ref.repo.owner,
                name=pr.ref.repo.name,
                **variables,
            )
        repo = data.get("repository") or {}
        result: dict[tuple[str, str], str | None] = {}
        for i, key in enumerate(batch):
            blob = repo.get(f"f{i}") or {}
            text = blob.get("text")
            if blob.get("isBinary") or blob.get("isTruncated"):
                text = None
            result[key] = text
        return result

    async def file_contents(
        self, pr: PullRequest, requests: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str | None]:
        batches = [requests[i : i + BLOB_BATCH] for i in range(0, len(requests), BLOB_BATCH)]
        results: dict[tuple[str, str], str | None] = {}
        for part in await asyncio.gather(*(self._blob_batch(pr, b) for b in batches)):
            results.update(part)
        return results

    async def compare(self, pr: PullRequest, base: str, head: str) -> list[ChangedFile]:
        repo = pr.ref.repo
        response = await self.client.rest(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/compare/{base}...{head}",
            params={"per_page": 1},
        )
        return [merge_rest_file(None, item) for item in response.json().get("files") or []]

    async def viewer_teams(self, org: str, login: str) -> list[str]:
        data = await self.client.graphql(q.VIEWER_TEAMS, org=org, login=login)
        teams = ((data.get("organization") or {}).get("teams") or {}).get("nodes") or []
        return [t["combinedSlug"] for t in teams if t and t.get("combinedSlug")]

    async def fingerprint(self, ref: PRRef) -> Fingerprint:
        data = await self.client.graphql(
            q.FINGERPRINT, owner=ref.repo.owner, name=ref.repo.name, number=ref.number
        )
        node = (data.get("repository") or {}).get("pullRequest") or {}
        state = node.get("state") or "OPEN"
        return Fingerprint(
            head=node.get("headRefOid") or "",
            state="DRAFT" if node.get("isDraft") and state == "OPEN" else state,
            comments=(node.get("comments") or {}).get("totalCount", 0),
            reviews=tuple(
                sorted(
                    (r["id"], r.get("state") or "")
                    for r in (node.get("reviews") or {}).get("nodes") or []
                    if r
                )
            ),
            threads=tuple(
                sorted(
                    (
                        t["id"],
                        bool(t.get("isResolved")),
                        (t.get("comments") or {}).get("totalCount", 0),
                    )
                    for t in (node.get("reviewThreads") or {}).get("nodes") or []
                    if t
                )
            ),
        )

    async def search_pull_requests(self, query: str) -> list[PRSummary]:
        data = await self.client.graphql(q.SEARCH_PULL_REQUESTS, query=query)
        return parse_search_results(data, self.host)

    async def pull_request_details(self, items: list[PRSummary]) -> None:
        ids = list(dict.fromkeys(item.node_id for item in items if item.node_id))
        chunks = [ids[i : i + DETAILS_BATCH] for i in range(0, len(ids), DETAILS_BATCH)]
        for data in await asyncio.gather(
            *(self.client.graphql(q.PR_DETAILS, ids=chunk) for chunk in chunks)
        ):
            apply_details(data, items)

    # -- reviews -----------------------------------------------------------------

    async def start_review(self, pr: PullRequest) -> Review:
        data = await self.client.graphql(
            q.ADD_REVIEW, input={"pullRequestId": pr.id, "commitOID": pr.head_oid}
        )
        return parse_review(data["addPullRequestReview"]["pullRequestReview"])

    async def submit_review(
        self, pr: PullRequest, review_id: str | None, event: ReviewEvent, body: str
    ) -> Review:
        if review_id:
            data = await self.client.graphql(
                q.SUBMIT_REVIEW,
                input=_clean(
                    {"pullRequestReviewId": review_id, "event": event.value, "body": body or None}
                ),
            )
            return parse_review(data["submitPullRequestReview"]["pullRequestReview"])
        data = await self.client.graphql(
            q.ADD_REVIEW,
            input=_clean(
                {
                    "pullRequestId": pr.id,
                    "commitOID": pr.head_oid,
                    "event": event.value,
                    "body": body or None,
                }
            ),
        )
        return parse_review(data["addPullRequestReview"]["pullRequestReview"])

    async def delete_review(self, review_id: str) -> None:
        await self.client.graphql(q.DELETE_REVIEW, input={"pullRequestReviewId": review_id})

    async def update_review_body(self, review_id: str, body: str) -> Review:
        data = await self.client.graphql(
            q.UPDATE_REVIEW, input={"pullRequestReviewId": review_id, "body": body}
        )
        return parse_review(data["updatePullRequestReview"]["pullRequestReview"])

    # -- review threads ----------------------------------------------------------

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
        input = {
            "pullRequestReviewId": review_id,
            "path": path,
            "body": body,
            "subjectType": "FILE" if file_level else "LINE",
        }
        if not file_level:
            input |= _clean(
                {
                    "line": line,
                    "side": side.value if side else None,
                    "startLine": start_line if start_line != line or start_side != side else None,
                    "startSide": (
                        start_side.value
                        if start_side and (start_line != line or start_side != side)
                        else None
                    ),
                }
            )
        data = await self.client.graphql(q.ADD_THREAD, input=input)
        thread = (data.get("addPullRequestReviewThread") or {}).get("thread")
        if thread is None:
            raise GitHubError("GitHub did not create the comment (is the line part of the diff?)")
        return parse_thread(thread)

    async def add_reply(self, review_id: str, thread_id: str, body: str) -> Comment:
        data = await self.client.graphql(
            q.ADD_REPLY,
            input={
                "pullRequestReviewId": review_id,
                "pullRequestReviewThreadId": thread_id,
                "body": body,
            },
        )
        return parse_review_comment(data["addPullRequestReviewThreadReply"]["comment"])

    async def update_review_comment(self, comment_id: str, body: str) -> Comment:
        data = await self.client.graphql(
            q.UPDATE_REVIEW_COMMENT,
            input={"pullRequestReviewCommentId": comment_id, "body": body},
        )
        return parse_review_comment(
            data["updatePullRequestReviewComment"]["pullRequestReviewComment"]
        )

    async def delete_review_comment(self, comment_id: str) -> None:
        await self.client.graphql(q.DELETE_REVIEW_COMMENT, input={"id": comment_id})

    async def set_thread_resolved(
        self, thread_id: str, resolved: bool
    ) -> tuple[bool, str | None, bool, bool]:
        query, key = (
            (q.RESOLVE_THREAD, "resolveReviewThread")
            if resolved
            else (q.UNRESOLVE_THREAD, "unresolveReviewThread")
        )
        data = await self.client.graphql(query, input={"threadId": thread_id})
        thread = data[key]["thread"]
        return (
            bool(thread.get("isResolved")),
            (thread.get("resolvedBy") or {}).get("login"),
            bool(thread.get("viewerCanResolve")),
            bool(thread.get("viewerCanUnresolve")),
        )

    # -- conversation ------------------------------------------------------------

    async def add_issue_comment(self, pr: PullRequest, body: str) -> Comment:
        data = await self.client.graphql(q.ADD_COMMENT, input={"subjectId": pr.id, "body": body})
        return parse_issue_comment(data["addComment"]["commentEdge"]["node"])

    async def update_issue_comment(self, comment_id: str, body: str) -> Comment:
        data = await self.client.graphql(
            q.UPDATE_ISSUE_COMMENT, input={"id": comment_id, "body": body}
        )
        return parse_issue_comment(data["updateIssueComment"]["issueComment"])

    async def delete_issue_comment(self, comment_id: str) -> None:
        await self.client.graphql(q.DELETE_ISSUE_COMMENT, input={"id": comment_id})

    async def set_minimized(
        self, subject_id: str, minimized: bool, reason: str = "RESOLVED"
    ) -> tuple[bool, str | None]:
        if minimized:
            data = await self.client.graphql(
                q.MINIMIZE, input={"subjectId": subject_id, "classifier": reason}
            )
            result = data["minimizeComment"]["minimizedComment"] or {}
        else:
            data = await self.client.graphql(q.UNMINIMIZE, input={"subjectId": subject_id})
            result = data["unminimizeComment"]["unminimizedComment"] or {}
        return bool(result.get("isMinimized")), result.get("minimizedReason")

    async def set_viewed(self, pr: PullRequest, path: str, viewed: bool) -> None:
        query = q.MARK_VIEWED if viewed else q.UNMARK_VIEWED
        await self.client.graphql(query, input={"pullRequestId": pr.id, "path": path})

    async def set_viewed_many(self, pr: PullRequest, paths: list[str], viewed: bool) -> None:
        for start in range(0, len(paths), VIEWED_BATCH):
            chunk = paths[start : start + VIEWED_BATCH]
            variables = {f"p{i}": path for i, path in enumerate(chunk)}
            await self.client.graphql(q.viewed_mutation(len(chunk), viewed), pr=pr.id, **variables)

    async def set_reaction(self, subject_id: str, content: str, add: bool) -> None:
        query = q.ADD_REACTION if add else q.REMOVE_REACTION
        await self.client.graphql(query, input={"subjectId": subject_id, "content": content})
