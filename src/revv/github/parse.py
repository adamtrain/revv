"""Turn GitHub API JSON into `revv.models` objects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
    ReviewThread,
    Side,
    ViewedState,
)

Json = dict[str, Any]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _login(actor: Json | None) -> str:
    if not actor:
        return "ghost"
    return actor.get("login") or actor.get("slug") or actor.get("name") or "ghost"


def _body(node: Json) -> str:
    return (node.get("body") or "").replace("\r\n", "\n")


def _nodes(connection: Json | None) -> list[Json]:
    if not connection:
        return []
    return [node for node in connection.get("nodes") or [] if node]


def parse_reactions(groups: list[Json] | None) -> list[Reaction]:
    reactions = []
    for group in groups or []:
        count = (group.get("reactors") or {}).get("totalCount", 0)
        if count:
            reactions.append(Reaction(group["content"], count, bool(group.get("viewerHasReacted"))))
    return reactions


def parse_review_comment(node: Json) -> Comment:
    created = parse_time(node.get("createdAt")) or _EPOCH
    return Comment(
        id=node["id"],
        author=_login(node.get("author")),
        body=_body(node),
        created_at=created,
        url=node.get("url") or "",
        updated_at=parse_time(node.get("updatedAt")),
        edited=bool(node.get("lastEditedAt")),
        is_pending=node.get("state") == "PENDING",
        viewer_can_update=bool(node.get("viewerCanUpdate")),
        viewer_can_delete=bool(node.get("viewerCanDelete")),
        viewer_can_minimize=bool(node.get("viewerCanMinimize")),
        viewer_can_unminimize=bool(node.get("viewerCanUnminimize")),
        viewer_can_react=node.get("viewerCanReact", True) is not False,
        viewer_did_author=bool(node.get("viewerDidAuthor")),
        is_minimized=bool(node.get("isMinimized")),
        minimized_reason=node.get("minimizedReason"),
        author_association=node.get("authorAssociation") or "",
        reactions=parse_reactions(node.get("reactionGroups")),
        is_review_comment=True,
        review_id=(node.get("pullRequestReview") or {}).get("id"),
        diff_hunk=node.get("diffHunk") or "",
        reply_to_id=(node.get("replyTo") or {}).get("id"),
    )


def parse_issue_comment(node: Json) -> Comment:
    return Comment(
        id=node["id"],
        author=_login(node.get("author")),
        body=_body(node),
        created_at=parse_time(node.get("createdAt")) or _EPOCH,
        url=node.get("url") or "",
        updated_at=parse_time(node.get("updatedAt")),
        edited=bool(node.get("lastEditedAt")),
        viewer_can_update=bool(node.get("viewerCanUpdate")),
        viewer_can_delete=bool(node.get("viewerCanDelete")),
        viewer_can_minimize=bool(node.get("viewerCanMinimize")),
        viewer_can_unminimize=bool(node.get("viewerCanUnminimize")),
        viewer_can_react=node.get("viewerCanReact", True) is not False,
        viewer_did_author=bool(node.get("viewerDidAuthor")),
        is_minimized=bool(node.get("isMinimized")),
        minimized_reason=node.get("minimizedReason"),
        author_association=node.get("authorAssociation") or "",
        reactions=parse_reactions(node.get("reactionGroups")),
    )


def parse_thread(node: Json) -> ReviewThread:
    start_side = node.get("startDiffSide")
    return ReviewThread(
        id=node["id"],
        path=node["path"],
        side=Side(node.get("diffSide") or "RIGHT"),
        line=node.get("line"),
        start_line=node.get("startLine"),
        start_side=Side(start_side) if start_side else None,
        original_line=node.get("originalLine"),
        original_start_line=node.get("originalStartLine"),
        is_resolved=bool(node.get("isResolved")),
        is_outdated=bool(node.get("isOutdated")),
        is_file_level=node.get("subjectType") == "FILE",
        viewer_can_resolve=bool(node.get("viewerCanResolve")),
        viewer_can_unresolve=bool(node.get("viewerCanUnresolve")),
        viewer_can_reply=node.get("viewerCanReply", True) is not False,
        resolved_by=_login(node["resolvedBy"]) if node.get("resolvedBy") else None,
        comments=[parse_review_comment(c) for c in _nodes(node.get("comments"))],
    )


def parse_review(node: Json) -> Review:
    return Review(
        id=node["id"],
        author=_login(node.get("author")),
        state=node.get("state") or "COMMENTED",
        body=_body(node),
        created_at=parse_time(node.get("createdAt")) or _EPOCH,
        submitted_at=parse_time(node.get("submittedAt")),
        url=node.get("url") or "",
        viewer_can_update=bool(node.get("viewerCanUpdate")),
        viewer_can_delete=bool(node.get("viewerCanDelete")),
        viewer_can_minimize=bool(node.get("viewerCanMinimize")),
        viewer_can_unminimize=bool(node.get("viewerCanUnminimize")),
        viewer_did_author=bool(node.get("viewerDidAuthor")),
        is_minimized=bool(node.get("isMinimized")),
        minimized_reason=node.get("minimizedReason"),
        comment_count=(node.get("comments") or {}).get("totalCount", 0),
        reactions=parse_reactions(node.get("reactionGroups")),
    )


_CHANGE_TYPES = {
    "ADDED": FileStatus.ADDED,
    "DELETED": FileStatus.REMOVED,
    "RENAMED": FileStatus.RENAMED,
    "COPIED": FileStatus.COPIED,
    "MODIFIED": FileStatus.MODIFIED,
    "CHANGED": FileStatus.CHANGED,
}


def parse_graphql_file(node: Json) -> ChangedFile:
    return ChangedFile(
        path=node["path"],
        status=_CHANGE_TYPES.get(node.get("changeType") or "", FileStatus.MODIFIED),
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
        viewed=ViewedState(node.get("viewerViewedState") or "UNVIEWED"),
    )


def merge_rest_file(file: ChangedFile | None, item: Json) -> ChangedFile:
    """Combine a REST `pulls/N/files` entry (which has the patch) with GraphQL data."""
    try:
        status = FileStatus(item.get("status") or "modified")
    except ValueError:
        status = FileStatus.MODIFIED
    if file is None:
        file = ChangedFile(path=item["filename"], status=status)
    file.status = status
    file.additions = item.get("additions", file.additions)
    file.deletions = item.get("deletions", file.deletions)
    file.previous_path = item.get("previous_filename")
    file.patch = item.get("patch")
    file.sha = item.get("sha")
    return file


def parse_pull_request(data: Json, ref: PRRef) -> tuple[PullRequest, dict[str, str | None]]:
    """Parse the main PR query. Also returns end cursors of connections with more pages."""
    viewer = _login(data.get("viewer"))
    node = (data.get("repository") or {}).get("pullRequest")
    if node is None:
        raise LookupError(f"Pull request {ref} not found")
    head = _nodes(node.get("headCommit"))
    checks = None
    if head:
        rollup = (head[0].get("commit") or {}).get("statusCheckRollup")
        checks = rollup.get("state") if rollup else None
    commits = []
    for commit_node in _nodes(node.get("commits")):
        commit = commit_node.get("commit") or {}
        author = commit.get("author") or {}
        commits.append(
            Commit(
                oid=commit.get("oid", ""),
                headline=commit.get("messageHeadline", ""),
                author=_login(author.get("user")) if author.get("user") else author.get("name", ""),
                authored_at=parse_time(commit.get("authoredDate")),
            )
        )
    requests = []
    for request in _nodes(node.get("reviewRequests")):
        reviewer = request.get("requestedReviewer")
        if reviewer:
            requests.append(_login(reviewer))
    latest = {}
    for review in _nodes(node.get("latestReviews")):
        latest[_login(review.get("author"))] = review.get("state") or ""
    pr = PullRequest(
        id=node["id"],
        ref=ref,
        title=node.get("title") or "",
        body=_body(node),
        url=node.get("url") or ref.web_url,
        state=node.get("state") or "OPEN",
        is_draft=bool(node.get("isDraft")),
        author=_login(node.get("author")),
        created_at=parse_time(node.get("createdAt")) or _EPOCH,
        updated_at=parse_time(node.get("updatedAt")) or _EPOCH,
        base_ref=node.get("baseRefName") or "",
        head_ref=node.get("headRefName") or "",
        base_oid=node.get("baseRefOid") or "",
        head_oid=node.get("headRefOid") or "",
        viewer_login=viewer,
        viewer_did_author=bool(node.get("viewerDidAuthor")),
        head_repo=(node.get("headRepository") or {}).get("nameWithOwner", ""),
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
        changed_files=node.get("changedFiles") or 0,
        review_decision=node.get("reviewDecision"),
        checks_state=checks,
        mergeable=node.get("mergeable") or "UNKNOWN",
        labels=[Label(n["name"], n.get("color", "888888")) for n in _nodes(node.get("labels"))],
        review_requests=requests,
        latest_reviews=latest,
        files=[parse_graphql_file(f) for f in _nodes(node.get("files"))],
        threads=[parse_thread(t) for t in _nodes(node.get("reviewThreads"))],
        comments=[parse_issue_comment(c) for c in _nodes(node.get("comments"))],
        reviews=[parse_review(r) for r in _nodes(node.get("reviews"))],
        commits=commits,
        total_commits=(node.get("headCommit") or {}).get("totalCount", len(commits)),
    )
    cursors: dict[str, str | None] = {}
    for key in ("files", "reviewThreads", "comments", "reviews"):
        info = (node.get(key) or {}).get("pageInfo") or {}
        cursors[key] = info.get("endCursor") if info.get("hasNextPage") else None
    return pr, cursors


def parse_search_results(data: Json, host: str) -> list[PRSummary]:
    viewer = _login(data.get("viewer"))
    results = []
    for node in _nodes(data.get("search")):
        if "number" not in node:
            continue
        repo = node.get("repository") or {}
        ref = PRRef(RepoRef(_login(repo.get("owner")), repo.get("name", ""), host), node["number"])
        users: list[str] = []
        teams: list[str] = []
        for request in _nodes(node.get("reviewRequests")):
            reviewer = request.get("requestedReviewer") or {}
            if reviewer.get("__typename") == "Team":
                teams.append(reviewer.get("combinedSlug") or "team")
            elif reviewer:
                users.append(_login(reviewer))
        my_state = None
        for review in _nodes(node.get("latestReviews")):
            if _login(review.get("author")) == viewer:
                my_state = review.get("state")
        head = _nodes(node.get("commits"))
        rollup = (head[0].get("commit") or {}).get("statusCheckRollup") if head else None
        results.append(
            PRSummary(
                ref=ref,
                title=node.get("title") or "",
                author=_login(node.get("author")),
                updated_at=parse_time(node.get("updatedAt")) or _EPOCH,
                created_at=parse_time(node.get("createdAt")),
                is_draft=bool(node.get("isDraft")),
                review_decision=node.get("reviewDecision"),
                requested_directly=viewer in users,
                requested_teams=teams,
                my_review_state=my_state,
                additions=node.get("additions") or 0,
                deletions=node.get("deletions") or 0,
                comments=(node.get("comments") or {}).get("totalCount", 0),
                head_ref=node.get("headRefName") or "",
                checks_state=rollup.get("state") if rollup else None,
                labels=[
                    Label(n["name"], n.get("color", "888888")) for n in _nodes(node.get("labels"))
                ],
            )
        )
    return results
