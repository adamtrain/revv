"""Header and status bar for the review screen."""

from __future__ import annotations

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from revv.models import PullRequest
from revv.ui.palette import Palette
from revv.ui.render import relative_time

CHECK_MARKS = {
    "SUCCESS": ("✓ checks", "success"),
    "FAILURE": ("✗ checks", "error"),
    "ERROR": ("✗ checks", "error"),
    "PENDING": ("● checks", "warning"),
    "EXPECTED": ("● checks", "warning"),
}

DECISIONS = {
    "APPROVED": ("✓ approved", "success"),
    "CHANGES_REQUESTED": ("✗ changes requested", "error"),
    "REVIEW_REQUIRED": ("● review required", "warning"),
}


def tone(p: Palette, name: str):
    return {"success": p.add_fg, "error": p.del_fg}.get(name, p.warning_fg)


def state_badge(pr: PullRequest, p: Palette) -> Text:
    if pr.state == "MERGED":
        label, color = " ⇄ Merged ", p.mix(p.accent, 0.9)
    elif pr.state == "CLOSED":
        label, color = " ✕ Closed ", p.error
    elif pr.is_draft:
        label, color = " ◌ Draft ", p.fg_mix(0.45)
    else:
        label, color = " ● Open ", p.success
    return Text(label, p.style(color.get_contrast_text(1.0), color, bold=True))


class PRHeader(Widget):
    """Two lines: title, then state, branches, checks and review progress."""

    DEFAULT_CSS = """
    PRHeader {
        height: 2;
        background: $panel;
        padding: 0 1;
    }
    """

    tab = reactive("files")

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pr: PullRequest | None = None
        self.message = "Loading…"
        self.syncing = False

    def show(self, pr: PullRequest | None, message: str = "") -> None:
        self.pr = pr
        self.message = message
        self.refresh()

    def render(self) -> Text:
        p = Palette.from_app(self.app)
        width = self.content_region.width or 80
        pr = self.pr
        if pr is None:
            return Text(f"revv  {self.message}", p.style(p.muted))
        top = Text()
        top.append(f"{pr.ref.repo.full_name} ", p.style(p.muted))
        top.append(f"#{pr.ref.number} ", p.style(p.primary_fg, bold=True))
        top.append(pr.title, p.style(p.text, bold=True))
        tabs = Text()
        for key, name, label in (
            ("1", "files", f"Files {len(pr.files)}"),
            ("2", "conversation", f"Conversation {len(pr.comments) + len(pr.threads)}"),
        ):
            active = self.tab == name
            style = p.style(p.bg, p.primary, bold=True) if active else p.style(p.muted, p.panel)
            tabs.append(
                f" {key} ",
                p.style(p.faint if not active else p.bg, p.panel if not active else p.primary),
            )
            tabs.append(f"{label} ", style)
            tabs.append(" ")
        top.truncate(max(10, width - tabs.cell_len - 1), overflow="ellipsis")
        top.pad_right(max(0, width - top.cell_len - tabs.cell_len))
        top.append_text(tabs)

        bottom = Text()
        bottom.append_text(state_badge(pr, p))
        bottom.append(" ")
        bottom.append(pr.author, p.style(p.author_color(pr.author), bold=True))
        bottom.append("  ")
        bottom.append(pr.head_ref, p.style(p.primary_fg))
        bottom.append(" → ", p.style(p.faint))
        bottom.append(pr.base_ref, p.style(p.primary_fg))
        bottom.append(f"  {relative_time(pr.updated_at)}", p.style(p.faint))
        if pr.checks_state in CHECK_MARKS:
            label, color = CHECK_MARKS[pr.checks_state]
            bottom.append("  ")
            bottom.append(label, p.style(tone(p, color)))
        if pr.review_decision in DECISIONS:
            label, color = DECISIONS[pr.review_decision]
            bottom.append("  ")
            bottom.append(label, p.style(tone(p, color)))
        bottom.append(f"  +{pr.additions}", p.style(p.add_fg))
        bottom.append(f" −{pr.deletions}", p.style(p.del_fg))

        right = Text()
        if self.syncing:
            right.append("⟳ syncing  ", p.style(p.primary_fg))
        viewed = sum(1 for f in pr.files if f.is_viewed)
        done = viewed == len(pr.files)
        right.append(f"{viewed}/{len(pr.files)} viewed", p.style(p.add_fg if done else p.muted))
        unresolved = pr.unresolved_count
        if unresolved:
            right.append(f"  ● {unresolved} open", p.style(p.accent_fg))
        if pr.pending_review is not None:
            pending = pr.pending_comment_count
            right.append("  ")
            right.append(f" ✎ {pending} pending · S submit ", p.style(p.bg, p.warning, bold=True))
        bottom.truncate(max(10, width - right.cell_len - 2), overflow="ellipsis")
        bottom.pad_right(max(0, width - bottom.cell_len - right.cell_len))
        bottom.append_text(right)
        return Text("\n").join([top, bottom])

    async def on_click(self, event) -> None:
        if event.y == 0 and self.pr is not None:
            width = self.content_region.width
            if event.x > width - 34:
                await self.screen.run_action(
                    "switch_tab('conversation')" if event.x > width - 20 else "switch_tab('files')"
                )


class StatusBar(Widget):
    """One line of context-sensitive key hints."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $panel;
        padding: 0 1;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.hints: list[tuple[str, str]] = []
        self.location = ""

    def show(self, hints: list[tuple[str, str]], location: str = "") -> None:
        self.hints = hints
        self.location = location
        self.refresh()

    def render(self) -> Text:
        p = Palette.from_app(self.app)
        text = Text()
        for key, label in self.hints:
            text.append(f" {key} ", p.style(p.accent_fg, p.mix(p.accent, 0.12, p.panel), bold=True))
            text.append(f" {label}  ", p.style(p.muted))
        right = Text()
        if self.location:
            right.append(self.location + "  ", p.style(p.faint))
        right.append(" ? ", p.style(p.accent_fg, p.mix(p.accent, 0.12, p.panel), bold=True))
        right.append(" help", p.style(p.muted))
        width = self.content_region.width or 80
        text.truncate(max(1, width - right.cell_len - 1), overflow="ellipsis")
        text.pad_right(max(0, width - text.cell_len - right.cell_len))
        text.append_text(right)
        return text
