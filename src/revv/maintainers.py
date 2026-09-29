"""Maintainer teams, from a bot comment listing which team maintains which changed file.

An opinionated, off-by-default extra for repositories where a bot posts a comment like:

    <!-- maintainers-comment -->
    <details>
    <summary>Maintainer teams for changed files</summary>

    `@Org/team-a` maintains:
    - [path/to/file.ts](https://github.com/Org/repo/pull/1/files#diff-…)

    `@Org/team-b` maintains:
    - [other/file.py](…)
    </details>

Turn it on in ~/.config/revv/config.json (it isn't in the settings screen):

    "maintainers": {"enabled": true}

and optionally override who posts the comment and how it's marked:

    "maintainers": {"enabled": true, "author": "developer-experience-ci-cd-app",
                    "marker": "<!-- maintainers-comment -->"}

Your teams come from GitHub (that needs a token that can read the organization); if GitHub
can't tell, list them yourself: "my_teams": ["Org/team"]. The comment itself is hidden, as
if ignored, since revv shows what it says in its own way.

With it on, the file tree groups files by maintainer team (yours first, `m` switches back
to folders), `M` shows only your teams' files, file headers name their teams, and the
inbox says how many files of each pull request your teams maintain.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from revv.config import setting

DEFAULT_AUTHOR = "developer-experience-ci-cd-app"
DEFAULT_MARKER = "<!-- maintainers-comment -->"

_TEAM = re.compile(r"^\s*`?@(?P<team>[\w.-]+/[\w.-]+)`?\s+maintains?\b", re.IGNORECASE)
_LINKED_FILE = re.compile(r"^\s*[-*+]\s+\[(?P<path>[^\]]+)\]\(")
_PLAIN_FILE = re.compile(r"^\s*[-*+]\s+`?(?P<path>[^`\s]+)`?\s*$")


@dataclass(frozen=True, slots=True)
class MaintainerSettings:
    author: str
    marker: str
    my_teams: tuple[str, ...] = ()  # teams you're on, if GitHub can't tell (e.g. SSO)
    group_by_team: bool = True


def maintainer_settings() -> MaintainerSettings | None:
    """The feature's settings, or None when it's off (the default)."""
    raw = setting("maintainers")
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    teams = raw.get("my_teams") or []
    return MaintainerSettings(
        author=str(raw.get("author") or DEFAULT_AUTHOR).lstrip("@"),
        marker=str(raw.get("marker") or DEFAULT_MARKER),
        my_teams=tuple(str(t) for t in teams if isinstance(t, str)),
        group_by_team=raw.get("group_by_team", True) is not False,
    )


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
    """The newest maintainers comment among a pull request's comments, parsed."""
    newest = None
    for comment in comments:
        if comment.author.lower() != settings.author.lower():
            continue
        if settings.marker not in comment.body:
            continue
        stamp = comment.updated_at or comment.created_at
        if newest is None or stamp >= (newest.updated_at or newest.created_at):
            newest = comment
    if newest is None:
        return None
    return parse(newest.body)


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
