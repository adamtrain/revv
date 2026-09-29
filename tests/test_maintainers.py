from datetime import UTC, datetime, timedelta

from revv import filters
from revv.config import save_config
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
from revv.models import Comment

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
    assert maintainers.teams_for(
        "packages/checkout/src/cart/cart-sync.flags.ts"
    ) == ["Acme/checkout-experience"]
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


def test_find_uses_the_newest_bot_comment() -> None:
    settings = MaintainerSettings(DEFAULT_AUTHOR, DEFAULT_MARKER)
    old = comment(DEFAULT_AUTHOR, DEFAULT_MARKER + "\n`@Org/old` maintains:\n- [a.py](u)", age=5)
    new = comment(DEFAULT_AUTHOR, DEFAULT_MARKER + "\n`@Org/new` maintains:\n- [a.py](u)", age=1)
    impostor = comment("someone", DEFAULT_MARKER + "\n`@Org/fake` maintains:\n- [a.py](u)")
    unmarked = comment(DEFAULT_AUTHOR, "`@Org/nope` maintains:\n- [a.py](u)")
    found = find([old, impostor, new, unmarked], settings)
    assert found is not None and found.teams_for("a.py") == ["Org/new"]
    assert find([impostor], settings) is None


def test_ownership() -> None:
    ownership = Ownership(parse(SAMPLE), {team_key("Acme/payments-platform")})
    assert ownership.mine("packages/payments/src/public/index.ts")
    assert not ownership.mine(
        "packages/checkout/src/cart/cart-sync.flags.ts"
    )
    assert ownership.group_order(
        [
            "packages/payments/src/public/index.ts",
            "packages/billing/src/invoice-lifecycle/auto-finalize.ts",
            "unlisted.py",
        ]
    ) == ["Acme/payments-platform", "Acme/billing-lifecycle", None]
    assert short_team("@Acme/payments-platform") == "payments-platform"


def test_off_by_default_and_hides_the_comment_when_on() -> None:
    assert maintainer_settings() is None
    bot = comment(DEFAULT_AUTHOR, SAMPLE)
    assert not filters.ignores().comment_ignored(bot)
    save_config(maintainers={"enabled": True})
    filters.reload()
    assert maintainer_settings() == MaintainerSettings(DEFAULT_AUTHOR, DEFAULT_MARKER)
    assert filters.ignores().comment_ignored(bot)  # represented structurally instead
    assert not filters.ignores().comment_ignored(comment(DEFAULT_AUTHOR, "a different comment"))


async def test_maintainer_teams_in_the_review(tmp_path) -> None:
    from revv.demo import DEMO_REF, DemoBackend
    from revv.ui.app import RevvApp
    from revv.ui.conversation import Card
    from revv.ui.review import ReviewScreen

    save_config(maintainers={"enabled": True})
    filters.reload()
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


async def test_m_and_M_do_nothing_when_the_extra_is_off() -> None:
    from revv.demo import DEMO_REF, DemoBackend
    from revv.ui.app import RevvApp
    from revv.ui.review import ReviewScreen

    app = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.4)
        screen = app.screen
        assert isinstance(screen, ReviewScreen) and screen.ownership is None
        await pilot.press("1", "M", "m")
        await pilot.pause(0.2)
        assert not screen.diff.only_mine


def test_team_cache_can_be_forgotten_on_its_own(tmp_path) -> None:
    from revv.cache import DiskCache
    from revv.models import RepoRef

    cache = DiskCache(tmp_path)
    cache.save_teams("github.com", "acme", "you", ["acme/python-reviewers"])
    cache.save_blob(RepoRef("acme", "netkit"), "abc", "a.py", "x")
    assert cache.load_teams("github.com", "acme", "you") == ["acme/python-reviewers"]
    assert cache.cached_teams() == ["acme/python-reviewers"]
    cache.clear_teams()
    assert cache.load_teams("github.com", "acme", "you") is None
    assert cache.load_blob(RepoRef("acme", "netkit"), "abc", "a.py") == (True, "x")  # untouched
