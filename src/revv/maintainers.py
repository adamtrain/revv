"""Maintainer teams, from a bot comment listing which team maintains which changed file.

A very particular extra, built for one repository: the monorepo where revv's author works
(VantaInc/obsidian). It switches itself on there and is off everywhere else, so there's
nothing to set up; it's no use to anyone else, and may change without notice.

In that repository a bot posts a comment like this on every pull request (it can sit
anywhere among the comments; the newest one counts):

    <!-- maintainers-comment -->
    <details>
    <summary>Maintainer teams for changed files</summary>

    `@Org/team-a` maintains:
    - [path/to/file.ts](https://github.com/Org/repo/pull/1/files#diff-…)

    `@Org/team-b` maintains:
    - [other/file.py](…)
    </details>

The comment itself is hidden, as if ignored, since revv shows what it says in its own way:
the file tree groups files by maintainer team (yours first, `m` switches back to folders),
`M` shows only your teams' files, file headers name their teams, and the inbox says how
many files of each pull request your teams maintain.

Your teams come from GitHub (that needs a token that can read the organization). If GitHub
can't tell, list them in ~/.config/revv/config.json:

    "maintainers": {"my_teams": ["VantaInc/some-team"]}
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from revv.config import save_config, setting
from revv.models import RepoRef

DEFAULT_AUTHOR = "developer-experience-ci-cd-app"
DEFAULT_MARKER = "<!-- maintainers-comment -->"

# The only repository this is for.
REPOSITORIES = frozenset({"github.com/vantainc/obsidian"})

_TEAM = re.compile(r"^\s*`?@(?P<team>[\w.-]+/[\w.-]+)`?\s+maintains?\b", re.IGNORECASE)
_LINKED_FILE = re.compile(r"^\s*[-*+]\s+\[(?P<path>[^\]]+)\]\(")
_PLAIN_FILE = re.compile(r"^\s*[-*+]\s+`?(?P<path>[^`\s]+)`?\s*$")


def applies_to(repo: RepoRef | None) -> bool:
    """Whether the extra is on in a repository: only in the one it's built for."""
    return repo is not None and f"{repo.host}/{repo.full_name}".lower() in REPOSITORIES


@dataclass(frozen=True, slots=True)
class MaintainerSettings:
    my_teams: tuple[str, ...] = ()  # teams you're on, if GitHub can't tell (e.g. SSO)
    group_by_team: bool = True
    author: str = DEFAULT_AUTHOR
    marker: str = DEFAULT_MARKER

    @classmethod
    def load(cls) -> MaintainerSettings:
        raw = setting("maintainers")
        raw = raw if isinstance(raw, dict) else {}
        teams = raw.get("my_teams") or []
        return cls(
            my_teams=tuple(str(t) for t in teams if isinstance(t, str)),
            group_by_team=raw.get("group_by_team", True) is not False,
        )


def maintainer_settings(repo: RepoRef | None) -> MaintainerSettings | None:
    """The extra's settings in a repository, or None where it's off (everywhere else)."""
    return MaintainerSettings.load() if applies_to(repo) else None


def save_maintainer_settings(**changes: object) -> None:
    """Change some of the extra's settings (they live together under "maintainers")."""
    raw = setting("maintainers")
    save_config(maintainers={**(raw if isinstance(raw, dict) else {}), **changes})


def same_login(a: str, b: str) -> bool:
    """Whether two logins are the same account ("app" and "app[bot]" are)."""
    return a.lower().removesuffix("[bot]") == b.lower().removesuffix("[bot]")


def team_key(team: str) -> str:
    """Normalize "@Org/Team" and "org/team" to one comparable form."""
    return team.strip().lstrip("@").lower()


def short_team(team: str) -> str:
    """ "@Acme/payments-platform" -> "payments-platform"."""
    return team.lstrip("@").split("/", 1)[-1]


@dataclass
class Maintainers:
    """Which team maintains which file, as the bot comment says."""

    teams_by_path: dict[str, list[str]] = field(default_factory=dict)  # "Org/team" names

    @property
    def files_by_team(self) -> dict[str, list[str]]:
        teams: dict[str, list[str]] = {}
        for path, names in self.teams_by_path.items():
            for team in names:
                teams.setdefault(team, []).append(path)
        return teams

    @property
    def orgs(self) -> set[str]:
        return {team.split("/", 1)[0] for names in self.teams_by_path.values() for team in names}

    def teams_for(self, path: str) -> list[str]:
        return self.teams_by_path.get(path, [])


def parse(body: str) -> Maintainers:
    """Read the team → files lists out of the comment's markdown."""
    teams_by_path: dict[str, list[str]] = {}
    team: str | None = None
    for line in body.splitlines():
        if match := _TEAM.match(line):
            team = match["team"]
            continue
        if team is None:
            continue
        match = _LINKED_FILE.match(line) or _PLAIN_FILE.match(line)
        if match:
            path = match["path"].strip().strip("`")
            names = teams_by_path.setdefault(path, [])
            if team not in names:
                names.append(team)
        elif line.strip().startswith("</details") or line.strip().startswith("<summary"):
            continue
    return Maintainers(teams_by_path)


def find(comments: Iterable, settings: MaintainerSettings) -> Maintainers | None:
    """The newest maintainers comment among a pull request's comments (wherever it is in
    the list), parsed."""
    newest = None
    for comment in comments:
        if not same_login(comment.author, settings.author):
            continue
        if settings.marker not in comment.body:
            continue
        stamp = comment.updated_at or comment.created_at
        if newest is None or stamp >= (newest.updated_at or newest.created_at):
            newest = comment
    if newest is None:
        return None
    return parse(newest.body)


async def lookup_my_teams(
    backend, cache, host: str, login: str, orgs: Iterable[str], extra: Iterable[str] = ()
) -> set[str]:
    """The teams you're on in these organizations (team_key() forms), cached for a day.
    `extra` adds teams from the config for when GitHub won't say."""
    import asyncio

    teams = {team_key(t) for t in extra}
    for org in sorted(set(orgs)):
        cached = cache.load_teams(host, org, login) if cache is not None else None
        if cached is None:
            try:
                cached = await backend.viewer_teams(org, login)
            except Exception:
                cached = []  # e.g. the token isn't authorized for the organization
            if cache is not None:
                await asyncio.to_thread(cache.save_teams, host, org, login, cached)
        teams |= {team_key(t) for t in cached}
    return teams


def count_mine(maintainers: Maintainers, my_teams: set[str]) -> tuple[int, int]:
    """(files your teams maintain, files listed) for a pull request."""
    paths = list(maintainers.teams_by_path)
    mine = sum(1 for p in paths if any(team_key(t) in my_teams for t in maintainers.teams_for(p)))
    return mine, len(paths)


@dataclass
class Ownership:
    """Maintainers plus the teams you're on: what's yours in this pull request."""

    maintainers: Maintainers
    my_teams: set[str] = field(default_factory=set)  # team_key() forms

    def teams_for(self, path: str) -> list[str]:
        return self.maintainers.teams_for(path)

    def is_mine(self, team: str) -> bool:
        return team_key(team) in self.my_teams

    def mine(self, path: str) -> bool:
        return any(self.is_mine(team) for team in self.teams_for(path))

    def group_of(self, path: str) -> str | None:
        """The team a file is filed under when grouping: one of yours if you have one."""
        teams = self.teams_for(path)
        for team in teams:
            if self.is_mine(team):
                return team
        return teams[0] if teams else None

    def group_order(self, paths: Iterable[str]) -> list[str | None]:
        """Groups in display order: your teams, other teams by name, then no team."""
        groups = {self.group_of(path) for path in paths}
        named = sorted((g for g in groups if g), key=lambda g: (not self.is_mine(g), g.lower()))
        return [*named, *([None] if None in groups else [])]
