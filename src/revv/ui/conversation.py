"""The conversation tab: description, review threads at a glance, and the timeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from revv.config import display_name
from revv.models import Comment, PullRequest, Review, ReviewThread
from revv.ui.palette import Palette
from revv.ui.render import SUGGESTION_RE, MarkdownRenderer, relative_time, segments

ItemKind = Literal["description", "comment", "review", "thread"]

REVIEW_VERBS = {
    "APPROVED": ("approved", "success"),
    "CHANGES_REQUESTED": ("requested changes", "error"),
    "COMMENTED": ("reviewed", "muted"),
    "DISMISSED": ("review dismissed", "muted"),
    "PENDING": ("pending review", "warning"),
}


@dataclass(eq=False)
class Item:
    kind: ItemKind
    obj: PullRequest | Comment | Review | ReviewThread
    expanded: bool = True

    @property
    def when(self) -> datetime:
        obj = self.obj
        if isinstance(obj, Review):
            return obj.submitted_at or obj.created_at
        if isinstance(obj, Comment):
            return obj.created_at
        if isinstance(obj, PullRequest):
            return obj.created_at
        return obj.root.created_at if obj.root else datetime.min


class Card(Widget, can_focus=True):
    """One focusable entry in the conversation."""

    DEFAULT_CSS = """
    Card { height: auto; margin: 0 1 1 1; }
    Card.compact { margin: 0 1 0 1; }
    """

    def __init__(self, item: Item, view: ConversationView) -> None:
        super().__init__(classes="compact" if item.kind == "thread" else "")
        self.item = item
        self.view = view
        self._cache: tuple[tuple, list[Strip]] | None = None

    def invalidate(self) -> None:
        self._cache = None
        self.refresh(layout=True)

    def strips(self, width: int) -> list[Strip]:
        key = (width, self.has_focus, self.item.expanded, self.view.version)
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, self.view.render_item(self.item, width, self.has_focus))
        return self._cache[1]

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self.strips(width))

    def render_line(self, y: int) -> Strip:
        strips = self.strips(self.size.width)
        if y < len(strips):
            return strips[y]
        return Strip.blank(self.size.width)

    def on_focus(self) -> None:
        self.refresh()
        self.scroll_visible(animate=False)
        self.view.post_message(ConversationView.Focused(self.item))

    def on_blur(self) -> None:
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if event.chain >= 2:
            self.view.activate(self)


class ConversationView(VerticalScroll):
    DEFAULT_CSS = """
    ConversationView { background: $background; padding: 1 1 0 1; }
    ConversationView .section-title { margin: 1 1 1 1; color: $text-muted; text-style: bold; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j,down", "move(1)", "Next", show=False),
        Binding("k,up", "move(-1)", "Previous", show=False),
        Binding("g,home", "edge(0)", "First", show=False),
        Binding("G,end", "edge(-1)", "Last", show=False),
        Binding("enter,z", "activate", "Open", show=False),
    ]

    class JumpToThread(Message):
        def __init__(self, thread: ReviewThread) -> None:
            super().__init__()
            self.thread = thread

    class Focused(Message):
        def __init__(self, item: Item) -> None:
            super().__init__()
            self.item = item

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pr: PullRequest | None = None
        self.version = 0
        self._palette: Palette | None = None
        self._md: MarkdownRenderer | None = None
        self._expanded: dict[str, bool] = {}

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, self._theme_changed)

    def _theme_changed(self, _theme: object) -> None:
        self._palette = None
        self._md = None
        self.version += 1
        for card in self.query(Card):
            card.invalidate()

    @property
    def palette(self) -> Palette:
        if self._palette is None:
            self._palette = Palette.from_app(self.app)
        return self._palette

    @property
    def md(self) -> MarkdownRenderer:
        if self._md is None:
            self._md = MarkdownRenderer(self.palette)
        return self._md

    # -- content ---------------------------------------------------------------------

    def show(self, pr: PullRequest) -> None:
        focused = self.focused_item
        focused_id = getattr(focused.obj, "id", None) if focused else None
        self.pr = pr
        self.version += 1
        self.remove_children()
        widgets: list[Widget] = [Card(Item("description", pr), self)]
        threads = [t for t in pr.threads]
        if threads:
            open_threads = [t for t in threads if not t.is_resolved]
            done = len(threads) - len(open_threads)
            title = f"Review threads — {len(open_threads)} open"
            if done:
                title += f", {done} resolved"
            widgets.append(Static(title, classes="section-title"))
            order = sorted(threads, key=lambda t: (t.is_resolved, t.path, t.line or 0))
            widgets += [Card(Item("thread", t), self) for t in order]
        timeline: list[Item] = []
        for comment in pr.comments:
            expanded = self._expanded.get(comment.id, not comment.is_minimized)
            timeline.append(Item("comment", comment, expanded))
        for review in pr.reviews:
            if review.state == "PENDING":
                continue
            if not review.body.strip() and review.state == "COMMENTED":
                continue  # just inline comments; they are listed as threads above
            expanded = self._expanded.get(review.id, not review.is_minimized)
            timeline.append(Item("review", review, expanded))
        if timeline:
            widgets.append(Static("Conversation", classes="section-title"))
            widgets += [Card(item, self) for item in sorted(timeline, key=lambda i: i.when)]
        widgets.append(
            Static(
                Text("  C  add a comment to the conversation", style="dim"), classes="section-title"
            )
        )
        self.mount_all(widgets)
        if focused_id is not None:
            self.call_after_refresh(self._refocus, focused_id)

    def _refocus(self, object_id: str) -> None:
        for card in self.query(Card):
            if getattr(card.item.obj, "id", None) == object_id:
                if self.screen.focused is None or isinstance(self.screen.focused, Card):
                    card.focus()
                return

    def refresh_cards(self) -> None:
        self.version += 1
        for card in self.query(Card):
            card.invalidate()

    @property
    def focused_item(self) -> Item | None:
        focused = self.screen.focused if self.is_attached else None
        if isinstance(focused, Card) and focused.view is self:
            return focused.item
        return None

    def focus_first(self) -> None:
        cards = list(self.query(Card))
        if cards and not isinstance(self.screen.focused, Card):
            cards[0].focus()

    # -- actions ---------------------------------------------------------------------

    def action_move(self, delta: int) -> None:
        cards = list(self.query(Card))
        if not cards:
            return
        focused = self.screen.focused
        index = cards.index(focused) if isinstance(focused, Card) and focused in cards else -1
        target = max(0, min(len(cards) - 1, index + delta))
        cards[target].focus()

    def action_edge(self, which: int) -> None:
        cards = list(self.query(Card))
        if cards:
            cards[which].focus()

    def action_activate(self) -> None:
        focused = self.screen.focused
        if isinstance(focused, Card):
            self.activate(focused)

    def activate(self, card: Card) -> None:
        item = card.item
        if item.kind == "thread":
            assert isinstance(item.obj, ReviewThread)
            self.post_message(self.JumpToThread(item.obj))
        elif item.kind in ("comment", "review"):
            item.expanded = not item.expanded
            self._expanded[item.obj.id] = item.expanded
            card.invalidate()

    # -- rendering ---------------------------------------------------------------------

    def render_item(self, item: Item, width: int, focused: bool) -> list[Strip]:
        if item.kind == "thread":
            assert isinstance(item.obj, ReviewThread)
            return [self._thread_line(item.obj, width, focused)]
        if item.kind == "description":
            assert isinstance(item.obj, PullRequest)
            return self._description(item.obj, width, focused)
        if item.kind == "review":
            assert isinstance(item.obj, Review)
            return self._review(item.obj, width, focused, item.expanded)
        assert isinstance(item.obj, Comment)
        return self._comment(item.obj, width, focused, item.expanded)

    def _edge(self, focused: bool, tone: str = "normal") -> Style:
        p = self.palette
        color = {
            "normal": p.fg_mix(0.35),
            "success": p.success,
            "error": p.error,
            "warning": p.warning,
        }.get(tone, p.fg_mix(0.35))
        if focused:
            color = p.accent
        return p.style(color, p.bg)

    def _render_text(self, text: Text, width: int, style: Style) -> Strip:
        text.truncate(width, overflow="ellipsis")
        return Strip(segments(text, self.md.console)).adjust_cell_length(width, style)

    def _box(
        self,
        header: Text,
        body: list[Strip],
        width: int,
        edge: Style,
        right: Text | None = None,
    ) -> list[Strip]:
        p = self.palette
        bg = p.style(p.text, p.bg)
        strips = []
        top = Text("╭─ ", edge)
        top.append_text(header)
        top.append(" ", edge)
        fill = max(0, width - top.cell_len - (right.cell_len if right else 0) - 1)
        top.append("─" * fill, edge)
        if right is not None:
            top.append_text(right)
        top.append("╮", edge)
        strips.append(self._render_text(top, width, bg))
        for line in body:
            strips.append(
                Strip(
                    [
                        Segment("│ ", edge),
                        *line.adjust_cell_length(width - 4, bg),
                        Segment(" │", edge),
                    ]
                )
            )
        bottom = Text("╰" + "─" * (width - 2) + "╯", edge)
        strips.append(self._render_text(bottom, width, bg))
        return strips

    def _markdown(self, body: str, width: int) -> list[Strip]:
        p = self.palette
        body = SUGGESTION_RE.sub(lambda m: "```\n" + m.group(1) + "```\n_(suggested change)_", body)
        return self.md.markdown(body, width, p.style(p.text, p.bg))

    def _reactions(self, obj: Comment | Review | PullRequest, width: int) -> list[Strip]:
        if not obj.reactions:
            return []
        p = self.palette
        text = Text()
        for reaction in obj.reactions:
            style = (
                p.style(p.primary_fg, p.mix(p.primary, 0.25))
                if reaction.viewer_has_reacted
                else p.style(p.muted, p.bg.blend(p.fg, 0.08))
            )
            text.append(f" {reaction.emoji} {reaction.count} ", style)
            text.append(" ")
        return [self._render_text(text, width, p.style(bg=p.bg))]

    def _description(self, pr: PullRequest, width: int, focused: bool) -> list[Strip]:
        p = self.palette
        header = Text()
        header.append(display_name(pr.author), p.style(p.author_color(pr.author), p.bg, bold=True))
        header.append(f" opened this {relative_time(pr.created_at)}", p.style(p.muted, p.bg))
        right = Text()
        for label in pr.labels[:4]:
            try:
                from textual.color import Color

                color = Color.parse("#" + label.color)
            except Exception:
                color = p.primary
            right.append(f" {label.name} ", p.style(color.get_contrast_text(1.0), color))
            right.append(" ", p.style(bg=p.bg))
        inner = width - 4
        body = self._markdown(pr.body, inner)
        body += self._reactions(pr, inner)
        body.append(Strip.blank(inner, p.style(bg=p.bg)))
        reviewers = Text("Reviewers: ", p.style(p.faint, p.bg))
        seen = set()
        for login, state in pr.latest_reviews.items():
            verb, tone = REVIEW_VERBS.get(state, (state.lower(), "muted"))
            color = {"success": p.add_fg, "error": p.del_fg, "warning": p.warning_fg}.get(
                tone, p.muted
            )
            reviewers.append(display_name(login), p.style(p.author_color(login), p.bg, bold=True))
            reviewers.append(f" {verb}", p.style(color, p.bg))
            reviewers.append(" · ", p.style(p.faint, p.bg))
            seen.add(login)
        for login in pr.review_requests:
            if login in seen:
                continue
            reviewers.append(display_name(login), p.style(p.author_color(login), p.bg, bold=True))
            reviewers.append(" requested", p.style(p.warning_fg, p.bg))
            reviewers.append(" · ", p.style(p.faint, p.bg))
        if len(reviewers) > len("Reviewers: "):
            reviewers.right_crop(3)
            for line in reviewers.wrap(self.md.console, inner):
                body.append(self._render_text(line, inner, p.style(bg=p.bg)))
        return self._box(header, body, width, self._edge(focused), right if right else None)

    def _comment(self, comment: Comment, width: int, focused: bool, expanded: bool) -> list[Strip]:
        p = self.palette
        header = Text()
        header.append(
            display_name(comment.author), p.style(p.author_color(comment.author), p.bg, bold=True)
        )
        if self.pr is not None and comment.author == self.pr.author:
            header.append(" author", p.style(p.faint, p.bg))
        header.append(f" · {relative_time(comment.created_at)}", p.style(p.muted, p.bg))
        if comment.edited:
            header.append(" · edited", p.style(p.faint, p.bg))
        right = None
        if comment.is_minimized:
            label = (
                "✓ resolved"
                if comment.is_resolved
                else f"hidden: {(comment.minimized_reason or '').lower()}"
            )
            right = Text(
                f" {label} ", p.style(p.add_fg if comment.is_resolved else p.warning_fg, p.bg)
            )
        edge = self._edge(focused, "success" if comment.is_resolved else "normal")
        if not expanded:
            return [self._collapsed(header, comment.body, width, edge, right)]
        inner = width - 4
        body = self._markdown(comment.body, inner) + self._reactions(comment, inner)
        return self._box(header, body, width, edge, right)

    def _review(self, review: Review, width: int, focused: bool, expanded: bool) -> list[Strip]:
        p = self.palette
        verb, tone = REVIEW_VERBS.get(review.state, (review.state.lower(), "muted"))
        color = {"success": p.add_fg, "error": p.del_fg, "warning": p.warning_fg}.get(tone, p.muted)
        header = Text()
        header.append(
            display_name(review.author), p.style(p.author_color(review.author), p.bg, bold=True)
        )
        header.append(f" {verb}", p.style(color, p.bg, bold=True))
        header.append(
            f" · {relative_time(review.submitted_at or review.created_at)}", p.style(p.muted, p.bg)
        )
        right = None
        if review.comment_count:
            right = Text(
                f" {review.comment_count} inline comment{'s' if review.comment_count != 1 else ''} ",
                p.style(p.faint, p.bg),
            )
        if review.is_minimized:
            right = Text(
                " ✓ resolved " if review.is_resolved else " hidden ", p.style(p.add_fg, p.bg)
            )
        edge = self._edge(focused, tone if tone in ("success", "error") else "normal")
        if not expanded:
            return [self._collapsed(header, review.body, width, edge, right)]
        inner = width - 4
        body = self._markdown(review.body, inner) if review.body.strip() else []
        body += self._reactions(review, inner)
        if not body:
            body = [
                self._render_text(
                    Text("(no summary)", p.style(p.faint, p.bg, italic=True)),
                    inner,
                    p.style(bg=p.bg),
                )
            ]
        return self._box(header, body, width, edge, right)

    def _collapsed(
        self, header: Text, body: str, width: int, edge: Style, right: Text | None
    ) -> Strip:
        p = self.palette
        text = Text("╶ ▸ ", edge)
        text.append_text(header)
        excerpt = " ".join(body.split())
        if excerpt:
            text.append(" · ", p.style(p.faint, p.bg))
            text.append(excerpt, p.style(p.faint, p.bg, italic=True))
        suffix = right or Text()
        text.truncate(max(1, width - suffix.cell_len), overflow="ellipsis")
        text.pad_right(max(0, width - text.cell_len - suffix.cell_len))
        text.append_text(suffix)
        return self._render_text(text, width, p.style(bg=p.bg))

    def _thread_line(self, thread: ReviewThread, width: int, focused: bool) -> Strip:
        p = self.palette
        bg = p.mix(p.primary, 0.18) if focused else p.bg
        text = Text(" ", p.style(bg=bg))
        if thread.has_pending:
            text.append("✎ ", p.style(p.warning_fg, bg, bold=True))
        elif thread.is_resolved:
            text.append("✓ ", p.style(p.add_fg, bg, bold=True))
        elif thread.is_outdated:
            text.append("◌ ", p.style(p.warning_fg, bg, bold=True))
        else:
            text.append("● ", p.style(p.accent_fg, bg, bold=True))
        text.append(thread.path, p.style(p.text if not thread.is_resolved else p.muted, bg))
        text.append(f" {thread.line_label}", p.style(p.faint, bg))
        root = thread.root
        if root is not None:
            text.append("  ")
            text.append(
                display_name(root.author), p.style(p.author_color(root.author), bg, bold=True)
            )
            text.append(": ", p.style(p.faint, bg))
            excerpt = SUGGESTION_RE.sub("[suggestion] ", root.body)
            text.append(" ".join(excerpt.split()), p.style(p.muted, bg, italic=True))
        count = len(thread.comments)
        suffix = Text(f"  {count} " + ("↵ jump " if focused else ""), p.style(p.faint, bg))
        text.truncate(max(1, width - suffix.cell_len), overflow="ellipsis")
        text.pad_right(max(0, width - text.cell_len - suffix.cell_len))
        text.append_text(suffix)
        return self._render_text(text, width, p.style(bg=bg))
