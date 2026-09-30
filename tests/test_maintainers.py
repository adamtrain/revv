import io
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from revv import filters, maintainers
from revv.cache import DiskCache
from revv.config import save_config
from revv.demo import DEMO_REF, DemoBackend
from revv.maintainers import (
    DEFAULT_AUTHOR,
    DEFAULT_MARKER,
    MaintainerSettings,
    Ownership,
    find,
    maintainer_settings,
    parse,
    short_team,
    team_key,
)
from revv.models import Comment, RepoRef
from revv.ui.app import RevvApp

OBSIDIAN = RepoRef("VantaInc", "obsidian")


@pytest.fixture
def demo_is_obsidian(monkeypatch):
    """Pretend the demo repository is the one repository the extra is for."""
    monkeypatch.setattr(maintainers, "REPOSITORIES", frozenset({"github.com/acme/netkit"}))
    filters.reload()


SAMPLE = """<!-- maintainers-comment -->
<details>
<summary>Maintainer teams for changed files</summary>

`@Acme/checkout-experience` maintains:
- [packages/checkout/src/cart/cart-sync.flags.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-2c32f635b5e0543210edfa90d26324d62eaaea59ef26a3f829ea01b13a594b54)

`@Acme/payments-platform` maintains:
- [packages/common/src/payments/PaymentUpdate.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-22857b3438d128fc7fd1644cd10fb8d1332128ac3733be8226e71af8e5c6b24f)
- [packages/payments/src/public/index.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-1d0612f7afb5ffef0af13f063c9418e1288ca5fab74bf79b1e57766900070b48)
- [packages/payments/src/processors/card-client.test.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-747a3d18216890b80e7b822bcfaa7f7b1054066ca41caab2b9660673f86b914c)
- [packages/payments/src/processors/card-client.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-f6751948883806cdbce35197192b539b3e2d8891ae56d473538a5e626aec69ff)
- [packages/payments/src/processors/imported-card.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-a1e704afe1fff805dc66efe81e30cb00722e523d451f25e247d5894f6c58318b)

`@Acme/billing-lifecycle` maintains:
- [packages/billing/src/invoice-lifecycle/auto-finalize.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-1efd5a7bc21716483c2354f04f4049958c8204da44086a829fe3f51333737c45)
- [packages/billing/src/invoice-lifecycle/custom-fields.ts](https://github.com/Acme/monorepo/pull/1234/files#diff-1eca4d3bc7857d8aa8d5e148f4ee29bf41525edbd7ffb38283043916cb750e16)

</details>
"""

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def comment(author: str, body: str, age: int = 0) -> Comment:
    return Comment(
        id=f"{author}{age}", author=author, body=body, created_at=NOW - timedelta(hours=age)
    )


def test_parse_sample() -> None:
    maintainers = parse(SAMPLE)
    assert len(maintainers.teams_by_path) == 8
    assert maintainers.teams_for("packages/payments/src/public/index.ts") == [
        "Acme/payments-platform"
    ]
    assert maintainers.teams_for("packages/checkout/src/cart/cart-sync.flags.ts") == [
        "Acme/checkout-experience"
    ]
    assert set(maintainers.files_by_team) == {
        "Acme/checkout-experience",
        "Acme/payments-platform",
        "Acme/billing-lifecycle",
    }
    assert len(maintainers.files_by_team["Acme/payments-platform"]) == 5
    assert maintainers.orgs == {"Acme"}


def test_a_file_can_have_several_teams() -> None:
    body = "`@Org/a` maintains:\n- [x.py](u)\n\n`@Org/b` maintains:\n- [x.py](u)\n- `y.py`\n"
    maintainers = parse(body)
    assert maintainers.teams_for("x.py") == ["Org/a", "Org/b"]
    assert maintainers.teams_for("y.py") == ["Org/b"]


def test_find_uses_the_newest_bot_comment_wherever_it_is() -> None:
    settings = MaintainerSettings()
    old = comment(DEFAULT_AUTHOR, DEFAULT_MARKER + "\n`@Org/old` maintains:\n- [a.py](u)", age=5)
    new = comment(DEFAULT_AUTHOR, DEFAULT_MARKER + "\n`@Org/new` maintains:\n- [a.py](u)", age=1)
    impostor = comment("someone", DEFAULT_MARKER + "\n`@Org/fake` maintains:\n- [a.py](u)")
    unmarked = comment(DEFAULT_AUTHOR, "`@Org/nope` maintains:\n- [a.py](u)")
    chatter = [comment("mona", f"comment {i}", age=10 + i) for i in range(150)]
    found = find([*chatter, old, impostor, new, unmarked, *chatter], settings)
    assert found is not None and found.teams_for("a.py") == ["Org/new"]
    assert find([impostor, *chatter], settings) is None
    # REST-style bot logins match too
    bot = comment(DEFAULT_AUTHOR + "[bot]", DEFAULT_MARKER + "\n`@Org/x` maintains:\n- [b.py](u)")
    found = find([bot], settings)
    assert found is not None and found.teams_for("b.py") == ["Org/x"]


def test_ownership() -> None:
    ownership = Ownership(parse(SAMPLE), {team_key("Acme/payments-platform")})
    assert ownership.mine("packages/payments/src/public/index.ts")
    assert not ownership.mine("packages/checkout/src/cart/cart-sync.flags.ts")
    assert ownership.group_order(
        [
            "packages/payments/src/public/index.ts",
            "packages/billing/src/invoice-lifecycle/auto-finalize.ts",
            "unlisted.py",
        ]
    ) == ["Acme/payments-platform", "Acme/billing-lifecycle", None]
    assert short_team("@Acme/payments-platform") == "payments-platform"


def test_only_on_in_its_one_repository() -> None:
    assert maintainer_settings(OBSIDIAN) == MaintainerSettings()
    assert maintainer_settings(RepoRef("vantainc", "Obsidian")) is not None
    assert maintainer_settings(DEMO_REF.repo) is None
    assert maintainer_settings(RepoRef("VantaInc", "obsidian", host="ghe.example.com")) is None
    assert maintainer_settings(None) is None
    save_config(maintainers={"my_teams": ["Acme/billing-lifecycle"]})
    settings = maintainer_settings(OBSIDIAN)
    assert settings is not None and settings.my_teams == ("Acme/billing-lifecycle",)


def test_the_comment_is_hidden_only_where_the_extra_is_on() -> None:
    bot = comment(DEFAULT_AUTHOR, SAMPLE)
    assert filters.ignores(OBSIDIAN).comment_ignored(bot)  # shown in revv's own way instead
    other = comment(DEFAULT_AUTHOR, "a different comment")
    assert not filters.ignores(OBSIDIAN).comment_ignored(other)
    assert not filters.ignores(DEMO_REF.repo).comment_ignored(bot)
    assert not filters.ignores().comment_ignored(bot)


async def test_maintainer_teams_in_the_review(demo_is_obsidian) -> None:
    from revv.ui.conversation import Card
    from revv.ui.dialogs import HelpScreen
    from revv.ui.review import ReviewScreen

    app = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.4)
        screen = app.screen
        assert isinstance(screen, ReviewScreen)
        ownership = screen.ownership
        assert ownership is not None
        assert ownership.my_teams == {"acme/python-reviewers"}
        # the bot comment is hidden; a summary card stands in for it
        kinds = [c.item.kind for c in screen.query(Card)]
        assert "maintainers" in kinds
        authors = [
            getattr(c.item.obj, "author", "")
            for c in screen.query(Card)
            if c.item.kind == "comment"
        ]
        assert DEFAULT_AUTHOR not in authors
        # your team's files come first, grouped under your team in the tree
        await pilot.press("1")
        await pilot.pause(0.2)
        paths = [s.path for s in screen.diff.sections]
        mine = [p for p in paths if ownership.mine(p)]
        assert paths[: len(mine)] == mine
        first_group = screen.file_tree.root.children[0]
        assert "python-reviewers" in str(first_group.label)
        assert "★ yours" in str(screen.header.render())
        # M: only your team's files
        await pilot.press("M")
        await pilot.pause(0.2)
        visible = {s.path for s in screen.diff.visible_sections}
        assert visible and all(ownership.mine(p) for p in visible)
        await pilot.press("M")
        await pilot.pause(0.2)
        # m: back to folders
        await pilot.press("m")
        await pilot.pause(0.2)
        assert not screen.group_by_team
        assert "python-reviewers" not in str(screen.file_tree.root.children[0].label)
        # the help lists m and M here
        await pilot.press("question_mark")
        await pilot.pause(0.1)
        assert isinstance(app.screen, HelpScreen) and app.screen.maintainers


async def test_nothing_changes_in_other_repositories() -> None:
    from revv.ui.conversation import Card
    from revv.ui.dialogs import HelpScreen
    from revv.ui.review import ReviewScreen

    app = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.4)
        screen = app.screen
        assert isinstance(screen, ReviewScreen) and screen.ownership is None
        # the bot's comment is an ordinary comment here
        authors = [getattr(c.item.obj, "author", "") for c in screen.query(Card)]
        assert DEFAULT_AUTHOR in authors
        await pilot.press("1", "M", "m")
        await pilot.pause(0.2)
        assert not screen.diff.only_mine
        await pilot.press("question_mark")
        await pilot.pause(0.1)
        assert isinstance(app.screen, HelpScreen) and not app.screen.maintainers


def test_team_cache_can_be_forgotten_on_its_own(tmp_path) -> None:
    cache = DiskCache(tmp_path)
    cache.save_teams("github.com", "acme", "you", ["acme/python-reviewers"])
    cache.save_blob(RepoRef("acme", "netkit"), "abc", "a.py", "x")
    assert cache.load_teams("github.com", "acme", "you") == ["acme/python-reviewers"]
    assert cache.cached_teams() == ["acme/python-reviewers"]
    cache.clear_teams()
    assert cache.load_teams("github.com", "acme", "you") is None
    assert cache.load_blob(RepoRef("acme", "netkit"), "abc", "a.py") == (True, "x")  # untouched


class PagedGitHub:
    """A GitHub where one pull request has more comments than fit on a page, and the
    maintainers comment is on the second page."""

    host = "github.com"

    def __init__(self) -> None:
        self.documents: list[str] = []

    async def graphql(self, document: str, /, **variables):
        from revv.github import queries as q

        self.documents.append(document)
        if document == q.COMMENT_AUTHORS:
            nodes = []
            for pr in variables["ids"]:
                if pr == "PR_busy":
                    people = [{"id": f"IC_{i}", "author": {"login": "mona"}} for i in range(100)]
                    page = {"hasNextPage": True, "endCursor": "page-2"}
                else:
                    people = [
                        {"id": "IC_hello", "author": {"login": "mona"}},
                        {"id": "IC_quiet", "author": {"login": DEFAULT_AUTHOR}},
                    ]
                    page = {"hasNextPage": False, "endCursor": None}
                nodes.append({"id": pr, "comments": {"pageInfo": page, "nodes": people}})
            return {"nodes": nodes}
        if document == q.MORE_COMMENT_AUTHORS:
            assert variables == {"id": "PR_busy", "after": "page-2"}
            people = [
                {"id": "IC_late", "author": {"login": DEFAULT_AUTHOR}},
                {"id": "IC_ghost", "author": None},  # a deleted account
            ]
            page = {"hasNextPage": False, "endCursor": None}
            return {"node": {"comments": {"pageInfo": page, "nodes": people}}}
        if document == q.COMMENTS_BY_ID:
            return {
                "nodes": [
                    {
                        "id": comment_id,
                        "body": f"{DEFAULT_MARKER} {comment_id}",
                        "author": {"login": DEFAULT_AUTHOR},
                        "createdAt": "2026-09-01T00:00:00Z",
                    }
                    for comment_id in variables["ids"]
                ]
            }
        raise AssertionError(f"unexpected query {document[:40]}")


async def test_every_comment_is_scanned_for_the_bots() -> None:
    from revv.github.backend import GitHubBackend
    from revv.github.client import GitHubClient

    backend = GitHubBackend(cast(GitHubClient, PagedGitHub()))
    found = await backend.comments_by(["PR_busy", "PR_quiet"], DEFAULT_AUTHOR)
    assert {pr: [c.id for c in comments] for pr, comments in found.items()} == {
        "PR_busy": ["IC_late"],
        "PR_quiet": ["IC_quiet"],
    }
    assert found["PR_busy"][0].body == f"{DEFAULT_MARKER} IC_late"


def _row_text(inbox, key: str) -> str:
    from rich.console import Console
    from textual.widgets import OptionList

    console = Console(width=150, file=io.StringIO(), record=True)
    console.print(inbox.query_one(OptionList).get_option(key).prompt)
    return console.export_text()


async def test_the_inbox_counts_your_teams_files(demo_is_obsidian) -> None:
    from rich.text import Text
    from textual.widgets import Input, OptionList

    from revv.ui.inbox import InboxScreen
    from revv.ui.palette import Palette

    app = RevvApp(DemoBackend(latency=0), repo=DEMO_REF.repo)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.5)
        inbox = app.screen
        assert isinstance(inbox, InboxScreen)
        assert inbox.my_teams == {"acme/python-reviewers"}
        section = next(s for s in inbox.sections if s.key == "requested")
        rows = {item.ref.number: item for item in section.items}
        assert rows[17].maintainers is not None
        assert set(rows[17].maintainers) == {
            "pyproject.toml",
            "src/netkit/__init__.py",
            "README.md",
        }
        p = Palette.from_app(app)

        def note(number: int) -> str | None:
            text: Text | None = inbox._ownership_note(rows[number], p)
            return text.plain if text is not None else None

        assert note(17) == "★ 2 files are your team's"
        assert note(40) == "no files are your team's"
        assert note(44) == "★ 1 is your team's"  # after "3 files, "
        assert note(46) is None  # no maintainers comment
        assert "★ 2 files are your team's" in _row_text(inbox, "github.com/acme/netkit#17")
        assert "3 files, ★ 1 is your team's" in _row_text(inbox, "stack:STACK_3")  # #44
        # a team's name finds the pull requests with files it maintains
        inbox.query_one("#filter", Input).value = "web-platform"
        await pilot.pause(0.1)
        options = inbox.query_one(OptionList)
        ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
        assert [i for i in ids if i] == ["github.com/acme/netkit#40", "github.com/acme/netkit#42"]


async def test_the_inbox_leaves_other_repositories_alone(monkeypatch) -> None:
    from revv.ui.inbox import InboxScreen

    backend = DemoBackend(latency=0)
    calls = []
    original = backend.comments_by

    async def spy(node_ids: list[str], author: str) -> dict[str, list[Comment]]:
        calls.append(node_ids)
        return await original(node_ids, author)

    monkeypatch.setattr(backend, "comments_by", spy)
    app = RevvApp(backend, repo=DEMO_REF.repo)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.5)
        inbox = app.screen
        assert isinstance(inbox, InboxScreen)
        assert not calls
        assert all(i.maintainers is None for s in inbox.sections for i in s.items)
        assert "your team" not in _row_text(inbox, "github.com/acme/netkit#17")


async def test_settings_offer_the_team_reset_only_there(tmp_path, monkeypatch) -> None:
    from revv.ui.settings import SettingsScreen

    cache = DiskCache(tmp_path / "cache")
    app = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause(0.4)
        await pilot.press("comma")
        await pilot.pause(0.1)
        assert isinstance(app.screen, SettingsScreen)
        assert not app.screen.query("#forget-teams")  # nobody else ever sees it
        await pilot.press("escape")

    monkeypatch.setattr(maintainers, "REPOSITORIES", frozenset({"github.com/acme/netkit"}))
    filters.reload()
    app = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause(0.4)
        assert cache.cached_teams() == ["acme/python-reviewers"]
        await pilot.press("comma")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        notes = " ".join(str(s.render()) for s in screen.query(".note"))
        assert "change without notice" in notes and "no use to anybody else" in notes
        screen.query_one("#forget-teams").press()
        await pilot.pause(0.3)
        assert cache.cached_teams() == ["acme/python-reviewers"]  # checked again with GitHub


async def test_the_maintainers_card_leads_to_the_first_file_to_look_at(demo_is_obsidian) -> None:
    """Your teams arrive after the pull request is shown, regrouping the files; the files
    tab must then open on the first file to look at in the new order."""
    from revv.ui.conversation import Card
    from revv.ui.review import ReviewScreen

    app = RevvApp(DemoBackend(latency=0.02), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.6)
        screen = app.screen
        assert isinstance(screen, ReviewScreen) and screen.tab == "conversation"
        assert screen.ownership is not None and screen.ownership.my_teams
        diff = screen.diff
        # move around the conversation, over the maintainers card and back
        await pilot.press("j", "j", "j", "k", "k", "k")
        card = next(c for c in screen.query(Card) if c.item.kind == "maintainers")
        card.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert screen.tab == "files"
        first = next(s for s in diff.sections if not s.file.is_viewed and not diff.is_hidden(s))
        assert diff.current_section is first
        assert screen.ownership.mine(first.path)  # your team's files come first
        # regrouping keeps the cursor on the same file
        await pilot.press("right_square_bracket", "right_square_bracket")
        await pilot.pause()
        here = diff.current_section
        assert here is not None
        await pilot.press("m")
        await pilot.pause(0.2)
        assert diff.current_section is not None and diff.current_section.path == here.path


async def test_maintainer_teams_can_be_nicknamed(demo_is_obsidian) -> None:
    from textual.widgets import Input

    from revv import config
    from revv.ui.conversation import Card
    from revv.ui.review import ReviewScreen

    config.set_nicknames({"acme/python-reviewers": "Pythonistas"})
    app = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.4)
        screen = app.screen
        assert isinstance(screen, ReviewScreen)
        assert "Pythonistas" in str(screen.file_tree.root.children[0].label)
        card = next(c for c in screen.query(Card) if c.item.kind == "maintainers")
        card.focus()
        await pilot.pause()
        await pilot.press("at")  # the teams on the card
        await pilot.pause()
        names = sorted(str(f.name) for f in app.screen.query(Input))
        assert names == ["acme/docs", "acme/python-reviewers", "acme/web-platform"]
        await pilot.press("escape")
