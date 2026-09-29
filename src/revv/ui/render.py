"""Rendering helpers: markdown comments and comment threads as lists of `Strip`s."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Console, RenderableType
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.strip import Strip

from revv.config import display_name
from revv.highlight import display_text
from revv.models import Comment, ReviewThread
from revv.ui.palette import Palette

SUGGESTION_RE = re.compile(r"```suggestion[^\n]*\n(.*?)```", re.DOTALL)


def segments(text: Text, console: Console) -> list[Segment]:
    """Render a Text to segments, keeping its base style.

    (`Text.render` drops the base style when the text has no spans.)
    """
    rendered = text.render(console)
    if text.style:
        style = console.get_style(text.style) if isinstance(text.style, str) else text.style
        rendered = Segment.apply_style(rendered, style)
    return list(rendered)


def relative_time(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return ""
    now = now or datetime.now(UTC)
    seconds = (now - when).total_seconds()
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60) or 1}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)}d ago"
    if when.year == now.year:
        return when.strftime("%b %-d")
    return when.strftime("%b %-d %Y")


class MarkdownRenderer:
    """Renders markdown to strips at a given width, with colours from the palette."""

    def __init__(self, palette: Palette) -> None:
        self.palette = palette
        p = palette
        code_bg = p.bg.blend(p.fg, 0.10)
        self.console = Console(
            width=120,
            color_system="truecolor",
            force_terminal=True,
            legacy_windows=False,
            no_color=False,
            theme=RichTheme(
                {
                    "markdown.code": p.style(p.accent_fg, code_bg),
                    "markdown.code_block": p.style(p.text, code_bg),
                    "markdown.block_quote": p.style(p.muted, italic=True),
                    "markdown.link": p.style(p.primary_fg, underline=True),
                    "markdown.link_url": p.style(p.primary_fg, underline=True),
                    "markdown.h1": p.style(p.text, bold=True),
                    "markdown.h2": p.style(p.text, bold=True),
                    "markdown.h3": p.style(p.text, bold=True),
                    "markdown.h4": p.style(p.muted, bold=True),
                    "markdown.hr": p.style(p.faint),
                    "markdown.item.bullet": p.style(p.primary_fg, bold=True),
                    "markdown.item.number": p.style(p.primary_fg, bold=True),
                    "markdown.table.border": p.style(p.faint),
                    "markdown.table.header": p.style(p.text, bold=True),
                }
            ),
        )

    def lines(self, renderable: RenderableType, width: int, bg: Style | None = None) -> list[Strip]:
        options = self.console.options.update(width=max(1, width), height=None)
        lines = self.console.render_lines(renderable, options, style=bg, pad=True)
        return [Strip(line, width) for line in lines]

    def markdown(self, body: str, width: int, bg: Style | None = None) -> list[Strip]:
        body = body.strip() or "_(no description)_"
        markdown = Markdown(
            body,
            code_theme=self.palette.syntax_theme,
            inline_code_theme=None,
            hyperlinks=True,
        )
        return self.lines(markdown, width, bg)


@dataclass(slots=True)
class RenderedThread:
    strips: list[Strip]
    comments: list[Comment | None]  # which comment each strip belongs to


def _pad(segments: list[Segment], width: int, style: Style) -> list[Segment]:
    strip = Strip(segments).adjust_cell_length(width, style)
    return list(strip)


class ThreadRenderer:
    """Draws a review thread as a rounded box, one strip per terminal row."""

    def __init__(self, palette: Palette, markdown: MarkdownRenderer) -> None:
        self.p = palette
        self.md = markdown

    def _header(
        self,
        comment: Comment,
        pr_author: str,
        left: str,
        fill_style: Style,
        width: int,
        right: Text | None = None,
    ) -> Strip:
        p = self.p
        bg = p.thread_bg
        text = Text(left, style=fill_style)
        text.append(" ")
        text.append(
            display_name(comment.author), p.style(p.author_color(comment.author), bg, bold=True)
        )
        if comment.author == pr_author:
            text.append(" author", p.style(p.faint, bg))
        text.append(f" · {relative_time(comment.created_at)}", p.style(p.muted, bg))
        if comment.edited:
            text.append(" · edited", p.style(p.faint, bg))
        if comment.is_pending:
            text.append(" ")
            text.append(" pending ", p.style(p.bg, p.warning, bold=True))
        text.append(" ", fill_style)
        return self._rule(text, right, fill_style, width)

    def _rule(self, left: Text, right: Text | None, fill_style: Style, width: int) -> Strip:
        """A horizontal box edge: `left` text, a run of ─, optional `right` text, then corner."""
        corner = left.plain[:1]
        end = {"╭": "╮", "├": "┤", "╰": "╯"}.get(corner, "─")
        used = left.cell_len + (right.cell_len if right else 0) + 1
        fill = max(0, width - used)
        line = left.copy()
        line.append("─" * fill, fill_style)
        if right is not None:
            line.append_text(right)
        line.append(end, fill_style)
        line.truncate(width)
        return Strip(segments(line, self.md.console), width).adjust_cell_length(width, fill_style)

    def _suggestion(self, code: str, original: list[str] | None, width: int) -> list[Strip]:
        p = self.p
        strips: list[Strip] = []
        title = Text(" Suggested change", p.style(p.muted, p.thread_bg, italic=True))
        strips.append(
            Strip(segments(title, self.md.console)).adjust_cell_length(
                width, p.style(bg=p.thread_bg)
            )
        )
        rows: list[tuple[str, str, Style, Style]] = []
        for line in original or []:
            rows.append(("-", line, p.style(p.del_fg, p.del_bg), p.style(p.text, p.del_bg)))
        for line in code.rstrip("\n").split("\n") if code.strip() else []:
            rows.append(("+", line, p.style(p.add_fg, p.add_bg), p.style(p.text, p.add_bg)))
        for sign, line, sign_style, text_style in rows:
            text = Text(f" {sign} ", sign_style)
            text.append(display_text(line), text_style)
            for piece in text.wrap(self.md.console, width, overflow="fold", no_wrap=False) or [
                text
            ]:
                piece.pad_right(width - piece.cell_len)
                piece.stylize(Style(bgcolor=text_style.bgcolor), 0, len(piece))
                strips.append(
                    Strip(segments(piece, self.md.console), width).adjust_cell_length(
                        width, text_style
                    )
                )
        return strips

    def _body(
        self,
        comment: Comment,
        width: int,
        original: Callable[[], list[str] | None] | None,
    ) -> list[Strip]:
        bg = self.p.style(self.p.text, self.p.thread_bg)
        body = comment.body
        strips: list[Strip] = []
        position = 0
        for match in SUGGESTION_RE.finditer(body):
            before = body[position : match.start()].strip()
            if before:
                strips += self.md.markdown(before, width, bg)
            strips += self._suggestion(match.group(1), original() if original else None, width)
            position = match.end()
        rest = body[position:].strip()
        if rest or not strips:
            strips += self.md.markdown(rest, width, bg)
        if comment.reactions:
            p = self.p
            reactions = Text(" ", bg)
            for reaction in comment.reactions:
                style = (
                    p.style(p.primary_fg, p.mix(p.primary, 0.25, p.thread_bg))
                    if reaction.viewer_has_reacted
                    else p.style(p.muted, p.thread_bg.blend(p.fg, 0.08))
                )
                reactions.append(f" {reaction.emoji} {reaction.count} ", style)
                reactions.append(" ", bg)
            strips.append(Strip(segments(reactions, self.md.console)).adjust_cell_length(width, bg))
        return strips

    def render(
        self,
        thread: ReviewThread,
        width: int,
        *,
        pr_author: str,
        focused: bool,
        collapsed: bool,
        original: Callable[[], list[str] | None] | None = None,
        hints: str = "",
    ) -> RenderedThread:
        p = self.p
        bg = p.thread_bg
        if thread.has_pending:
            edge = p.warning
        elif thread.is_resolved or thread.is_outdated:
            edge = p.fg_mix(0.3)
        else:
            edge = p.fg_mix(0.45)
        if focused:
            edge = p.accent
        edge_style = p.style(edge, bg)
        width = max(20, width)

        if collapsed:
            return RenderedThread([self._collapsed(thread, width, edge_style)], [thread.root])

        strips: list[Strip] = []
        owners: list[Comment | None] = []
        inner = width - 4
        status = self._status(thread)
        for index, comment in enumerate(thread.comments):
            corner = "╭─" if index == 0 else "├─"
            header = self._header(
                comment, pr_author, corner, edge_style, width, right=status if index == 0 else None
            )
            strips.append(header)
            owners.append(comment)
            if index == 0 and thread.is_outdated and comment.diff_hunk:
                for strip in self._diff_hunk(comment.diff_hunk, inner):
                    strips.append(self._boxed(strip, edge_style, width))
                    owners.append(comment)
            for strip in self._body(comment, inner, original):
                strips.append(self._boxed(strip, edge_style, width))
                owners.append(comment)
        footer_hint = Text(f" {hints} ", p.style(p.muted, bg)) if hints and focused else None
        strips.append(self._rule(Text("╰─", edge_style), footer_hint, edge_style, width))
        owners.append(thread.comments[-1] if thread.comments else None)
        return RenderedThread(strips, owners)

    def _status(self, thread: ReviewThread) -> Text | None:
        p = self.p
        bg = p.thread_bg
        if thread.is_resolved:
            who = f" by {display_name(thread.resolved_by)}" if thread.resolved_by else ""
            return Text(f" ✓ resolved{who} ", p.style(p.add_fg, bg))
        if thread.is_outdated:
            return Text(" outdated ", p.style(p.warning_fg, bg))
        return None

    def _boxed(self, strip: Strip, edge_style: Style, width: int) -> Strip:
        segments = [Segment("│ ", edge_style), *strip, Segment(" │", edge_style)]
        return Strip(segments).adjust_cell_length(width, edge_style)

    def _diff_hunk(self, hunk: str, width: int) -> list[Strip]:
        p = self.p
        lines = [ln for ln in hunk.split("\n") if not ln.startswith("@@")][-4:]
        strips = []
        for line in lines:
            tag, text = line[:1], display_text(line[1:])
            if tag == "+":
                style = p.style(p.text, p.add_bg)
            elif tag == "-":
                style = p.style(p.text, p.del_bg)
            else:
                style = p.style(p.muted, p.bg)
            piece = Text(f"{tag or ' '} {text}", style)
            piece.truncate(width, overflow="ellipsis")
            strips.append(Strip(segments(piece, self.md.console)).adjust_cell_length(width, style))
        return strips

    def _collapsed(self, thread: ReviewThread, width: int, edge_style: Style) -> Strip:
        p = self.p
        bg = p.thread_bg
        root = thread.root
        text = Text("╶ ▸ ", edge_style)
        if thread.is_resolved:
            text.append("✓ resolved", p.style(p.add_fg, bg))
        elif thread.is_outdated:
            text.append("outdated", p.style(p.warning_fg, bg))
        elif thread.has_pending:
            text.append("pending", p.style(p.warning_fg, bg))
        else:
            text.append("thread", p.style(p.muted, bg))
        if root is not None:
            text.append(" · ", p.style(p.faint, bg))
            text.append(
                display_name(root.author), p.style(p.author_color(root.author), bg, bold=True)
            )
            text.append(": ", p.style(p.faint, bg))
            first = " ".join(SUGGESTION_RE.sub("[suggestion] ", root.body).split())
            text.append(first, p.style(p.muted, bg, italic=True))
        count = len(thread.comments)
        suffix = Text(f"  {count} comment{'s' if count != 1 else ''} ", p.style(p.faint, bg))
        text.truncate(max(1, width - suffix.cell_len), overflow="ellipsis")
        text.append_text(suffix)
        return Strip(segments(text, self.md.console)).adjust_cell_length(width, p.style(bg=bg))
