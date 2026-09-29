"""The review inbox: which pull requests are waiting for you.

Only pull requests that involve you are listed (requested from you or one of your teams,
assigned to you, reviewed by you, or opened by you); anything else is one PR number away.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from revv.backend import Backend
from revv.cache import DiskCache
from revv.maintainers import MaintainerSettings, applies_to, find, lookup_my_teams
from revv.models import PRRef, PRSummary, RepoRef


@dataclass
class InboxSection:
    key: str
    title: str
    queries: list[str]
    items: list[PRSummary] = field(default_factory=list)
    error: str | None = None
    loaded: bool = False


def inbox_sections(repo: RepoRef | None) -> list[InboxSection]:
    scope = f" repo:{repo.full_name}" if repo else " archived:false"
    base = "is:pr is:open sort:updated-desc" + scope
    return [
        # review-requested also matches requests to any team you're a member of
        InboxSection(
            "requested",
            "To review",
            [base + " review-requested:@me", base + " assignee:@me -author:@me"],
        ),
        InboxSection("reviewed", "Reviewed", [base + " reviewed-by:@me -author:@me"]),
        InboxSection("mine", "Mine", [base + " author:@me"]),
    ]


async def load_section(backend: Backend, section: InboxSection) -> None:
    """Run a section's searches (the quick part) and merge their results."""
    try:
        results = await asyncio.gather(*(backend.search_pull_requests(q) for q in section.queries))
    except Exception as error:  # shown in the tab instead of failing the whole inbox
        section.error = str(error)
        section.loaded = True
        return
    merged: dict[PRRef, PRSummary] = {}
    for index, items in enumerate(results):
        for item in items:
            if item.ref not in merged:
                merged[item.ref] = item
            if section.key == "requested" and index == 1:
                merged[item.ref].assigned = True
    section.items = sorted(merged.values(), key=lambda item: item.updated_at, reverse=True)
    section.error = None
    section.loaded = True


DETAIL_FIELDS = (
    "review_decision",
    "additions",
    "deletions",
    "changed_files",
    "comments",
    "requested_directly",
    "requested_teams",
    "checks_state",
    "details_loaded",
    "maintainers",
)


def carry_over_details(previous: dict[PRRef, PRSummary], items: list[PRSummary]) -> None:
    """Keep showing the last known details of each row until fresh ones arrive."""
    for item in items:
        old = previous.get(item.ref)
        if old is not None and old.details_loaded and not item.details_loaded:
            for name in DETAIL_FIELDS:
                setattr(item, name, getattr(old, name))
            item.assigned = item.assigned or old.assigned


async def load_details(backend: Backend, sections: list[InboxSection]) -> None:
    """Fill in the slower per-PR fields (size, checks, reviewers) for every row."""
    items = [item for section in sections for item in section.items]
    if items:
        await backend.pull_request_details(items)


async def load_maintainers(
    backend: Backend, cache: DiskCache | None, sections: list[InboxSection]
) -> set[str] | None:
    """Read the maintainers comment of each pull request in the one repository the
    maintainer-teams extra is for into its row, and return the teams you're on (None when
    no row is from there)."""
    rows: dict[str, list[PRSummary]] = {}  # a pull request can be in several sections
    for section in sections:
        for item in section.items:
            if item.node_id and applies_to(item.ref.repo):
                rows.setdefault(item.node_id, []).append(item)
    if not rows:
        return None
    settings = MaintainerSettings.load()
    comments = await backend.comments_by(list(rows), settings.author)
    orgs: set[str] = set()
    for node_id, items in rows.items():
        found = find(comments.get(node_id, []), settings)
        if found is not None:
            orgs |= found.orgs
        for item in items:
            item.maintainers = dict(found.teams_by_path) if found else {}
    login = await backend.viewer_login()
    return await lookup_my_teams(backend, cache, backend.host, login, orgs, settings.my_teams)


async def load_inbox(backend: Backend, sections: list[InboxSection]) -> None:
    await asyncio.gather(*(load_section(backend, section) for section in sections))
    await load_details(backend, sections)
