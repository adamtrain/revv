from datetime import UTC, datetime

from revv import filters
from revv.filters import CommentRule, Ignores, fuzzy_contains
from revv.models import Comment, Label, ReviewThread, Side

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def comment(author: str, body: str, pending: bool = False) -> Comment:
    return Comment(
        id=f"{author}:{body}", author=author, body=body, created_at=NOW, is_pending=pending
    )


def test_fuzzy_contains() -> None:
    body = "⚠️ Coverage decreased by **0.4%** (`netkit/client.py`)."
    assert fuzzy_contains(body, "coverage decreased")
    assert fuzzy_contains(body, "COVERAGE, decreased!")
    assert fuzzy_contains(body, "coverage 0 4")  # in order, gaps allowed
    assert fuzzy_contains(body, "cover decr")  # words may be prefixes
    assert not fuzzy_contains(body, "decreased coverage")  # order matters
    assert not fuzzy_contains(body, "coverage increased")
    assert not fuzzy_contains(body, "")


def test_comment_rules() -> None:
    bot = CommentRule(author="ci-bot")
    text = CommentRule(text="friendly reminder")
    both = CommentRule(author="mona", text="friendly reminder")
    assert bot.matches("CI-Bot", "anything")
    assert not bot.matches("mona", "anything")
    assert text.matches("anyone", "Just a friendly reminder: please rebase")
    assert both.matches("mona", "friendly reminder!")
    assert not both.matches("hubot", "friendly reminder!")
    assert not both.matches("mona", "please rebase")
    assert not CommentRule().matches("x", "y")


def test_labels_with_wildcards() -> None:
    ignores = Ignores(["wip", "size/*", "*bot*"], [])
    assert ignores.label_ignored("WIP")
    assert ignores.label_ignored("size/XL")
    assert ignores.label_ignored("dependabot-auto")
    assert not ignores.label_ignored("wip-later")
    assert not ignores.label_ignored("enhancement")
    labels = [Label("wip", "fff"), Label("bug", "f00"), Label("size/S", "0f0")]
    assert [label.name for label in ignores.labels(labels)] == ["bug"]


def test_threads_with_only_ignored_comments_disappear() -> None:
    ignores = Ignores([], [CommentRule(author="ci-bot")])
    hidden = ReviewThread("t1", "a.py", Side.RIGHT, 3, comments=[comment("ci-bot", "lint")])
    partly = ReviewThread(
        "t2", "a.py", Side.RIGHT, 4, comments=[comment("ci-bot", "lint"), comment("mona", "hm")]
    )
    drafts = ReviewThread(
        "t3", "a.py", Side.RIGHT, 5, comments=[comment("ci-bot", "x", pending=True)]
    )
    assert ignores.threads([hidden, partly, drafts]) == [partly, drafts]
    assert [c.author for c in ignores.comments(partly.comments)] == ["mona"]


def test_rules_are_saved_and_reloaded() -> None:
    filters.add_comment_rule(CommentRule(author="ci-bot"))
    filters.add_comment_rule(CommentRule(text="coverage decreased"))
    filters.add_comment_rule(CommentRule(author="ci-bot"))  # no duplicates
    assert filters.ignores().rules == [
        CommentRule(author="ci-bot"),
        CommentRule(text="coverage decreased"),
    ]
    filters.remove_comment_rule(CommentRule(author="ci-bot"))
    assert filters.ignores().rules == [CommentRule(text="coverage decreased")]
    filters.set_ignored_labels(["wip", " ", "size/*"])
    assert filters.ignores().label_patterns == ["wip", "size/*"]
