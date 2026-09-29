"""Things you asked not to see: ignored labels and ignored comments.

Both are configured in the settings (and comments with `i` on one). Everything the UI
shows goes through here, so ignored labels and comments behave as if they don't exist.

    "ignored_labels": ["wip", "size/*"]            # `*` matches anything
    "ignored_comments": [
        {"author": "ci-bot"},                          # everything from ci-bot
        {"text": "coverage decreased"},                # comments containing those words
        {"author": "mona", "text": "friendly reminder"}  # both must match
    ]

Text matching is forgiving: case and punctuation don't matter, and the words must appear
in that order (other words may sit in between; a word may be the start of a longer one).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from dataclasses import dataclass

from revv.config import save_config, setting
from revv.models import Comment, Label, PullRequest, Review, ReviewThread

_WORD = re.compile(r"\w+")


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def fuzzy_contains(body: str, text: str) -> bool:
    """Whether the words of `text` appear, in order, in `body` (prefixes count)."""
    wanted = words(text)
    if not wanted:
        return False
    position = 0
    haystack = words(body)
    for word in wanted:
        while position < len(haystack) and not haystack[position].startswith(word):
            position += 1
        if position == len(haystack):
            return False
        position += 1
    return True


@dataclass(frozen=True, slots=True)
class CommentRule:
    author: str | None = None
    text: str | None = None

    def matches(self, author: str, body: str) -> bool:
        if not self.author and not self.text:
            return False
        if self.author and author.lower() != self.author.lower():
            return False
        return not (self.text and not fuzzy_contains(body, self.text))

    def describe(self) -> str:
        if self.author and self.text:
            return f"from @{self.author} containing “{self.text}”"
        if self.author:
            return f"everything from @{self.author}"
        return f"anything containing “{self.text}”"

    def to_json(self) -> dict[str, str]:
        return {k: v for k, v in (("author", self.author), ("text", self.text)) if v}


def _label_regex(pattern: str) -> re.Pattern[str]:
    parts = (re.escape(part) for part in pattern.strip().split("*"))
    return re.compile("^" + ".*".join(parts) + "$", re.IGNORECASE)


class Ignores:
    def __init__(self, labels: Iterable[str], rules: Iterable[CommentRule]) -> None:
        self.label_patterns = [p.strip() for p in labels if p and p.strip()]
        self._label_regexes = [_label_regex(p) for p in self.label_patterns]
        self.rules = [r for r in rules if r.author or r.text]

    # -- labels ----------------------------------------------------------------------

    def label_ignored(self, name: str) -> bool:
        return any(regex.match(name) for regex in self._label_regexes)

    def labels(self, labels: Iterable[Label]) -> list[Label]:
        return [label for label in labels if not self.label_ignored(label.name)]

    # -- comments --------------------------------------------------------------------

    def comment_ignored(self, comment: Comment | Review) -> bool:
        if isinstance(comment, Comment) and comment.is_pending:
            return False  # your own drafts are never hidden
        return any(rule.matches(comment.author, comment.body) for rule in self.rules)

    def comments(self, comments: Iterable[Comment]) -> list[Comment]:
        return [c for c in comments if not self.comment_ignored(c)]

    def thread_hidden(self, thread: ReviewThread) -> bool:
        return bool(thread.comments) and not self.comments(thread.comments)

    def threads(self, threads: Iterable[ReviewThread]) -> list[ReviewThread]:
        return [t for t in threads if not self.thread_hidden(t)]

    def review_hidden(self, review: Review) -> bool:
        return (
            review.state != "PENDING" and bool(review.body.strip()) and self.comment_ignored(review)
        )

    def visible_pr(self, pr: PullRequest) -> PullRequest:
        """A copy of the pull request with everything ignored left out."""
        if not self.rules and not self._label_regexes:
            return pr
        threads = [
            dataclasses.replace(t, comments=self.comments(t.comments))
            for t in self.threads(pr.threads)
        ]
        return dataclasses.replace(
            pr,
            labels=self.labels(pr.labels),
            comments=self.comments(pr.comments),
            reviews=[r for r in pr.reviews if not self.review_hidden(r)],
            threads=threads,
        )


_ignores: Ignores | None = None
version = 0  # bumped whenever the rules change, for render caches


def ignores() -> Ignores:
    global _ignores
    if _ignores is None:
        rules = []
        for raw in setting("ignored_comments"):
            if isinstance(raw, dict):
                author = str(raw.get("author") or "").strip().lstrip("@") or None
                text = str(raw.get("text") or "").strip() or None
                rules.append(CommentRule(author, text))
        labels = [str(p) for p in setting("ignored_labels")]
        _ignores = Ignores(labels, rules)
    return _ignores


def reload() -> None:
    global _ignores, version
    _ignores = None
    version += 1


def set_ignored_labels(patterns: list[str]) -> None:
    save_config(ignored_labels=[p.strip() for p in patterns if p.strip()])
    reload()


def add_comment_rule(rule: CommentRule) -> None:
    rules = [r.to_json() for r in ignores().rules]
    if rule.to_json() not in rules:
        rules.append(rule.to_json())
    save_config(ignored_comments=rules)
    reload()


def remove_comment_rule(rule: CommentRule) -> None:
    rules = [r.to_json() for r in ignores().rules if r != rule]
    save_config(ignored_comments=rules)
    reload()
