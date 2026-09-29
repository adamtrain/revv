"""The review inbox: which pull requests are waiting for you."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from revv.backend import Backend
from revv.models import PRSummary, RepoRef


@dataclass
class InboxSection:
    key: str
    title: str
    query: str
    items: list[PRSummary] = field(default_factory=list)
    error: str | None = None
    loaded: bool = False


def inbox_sections(repo: RepoRef | None) -> list[InboxSection]:
    scope = f" repo:{repo.full_name}" if repo else " archived:false"
    base = "is:pr is:open sort:updated-desc" + scope
    sections = [
        # review-requested includes requests to any team you are a member of
        InboxSection("requested", "To review", base + " review-requested:@me"),
        InboxSection("reviewed", "Reviewed", base + " reviewed-by:@me -author:@me"),
        InboxSection("mine", "Mine", base + " author:@me"),
    ]
    if repo is not None:
        sections.append(InboxSection("all", "All open", base))
    return sections


async def load_inbox(backend: Backend, sections: list[InboxSection]) -> None:
    async def load(section: InboxSection) -> None:
        try:
            section.items = await backend.search_pull_requests(section.query)
            section.error = None
        except Exception as error:  # show the error in the tab rather than failing
            section.error = str(error)
        section.loaded = True

    await asyncio.gather(*(load(section) for section in sections))
    requested = next((s for s in sections if s.key == "requested"), None)
    if requested is None:
        return
    # Only the "To review" search tells us reliably that a team request involves you.
    wanted = {item.ref for item in requested.items}
    for section in sections:
        if section is requested:
            continue
        for item in section.items:
            if item.ref not in wanted:
                item.requested_teams = []
