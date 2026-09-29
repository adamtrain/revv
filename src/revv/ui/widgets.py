"""Header and status bar for the review screen."""

from __future__ import annotations

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from revv.config import display_name
from revv.models import AiCheck, PullRequest
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
    """Three lines: the title and tabs; state, branches and checks; your progress."""

    DEFAULT_CSS = """
    PRHeader {
        height: 3;
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
        self.hidden: set[str] = set()  # paths of hidden (test/generated) files
        self.ai: AiCheck | None = None  # panc's verdict on the description

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

        def line(left: Text, right: Text) -> Text:
            left.truncate(max(10, width - right.cell_len - 2), overflow="ellipsis")
            left.pad_right(max(0, width - left.cell_len - right.cell_len))
            left.append_text(right)
            return left

        # 1: what it is, and where you are
        title = Text()
        title.append(f"{pr.ref.repo.full_name} ", p.style(p.muted))
        title.append(f"#{pr.ref.number} ", p.style(p.primary_fg, bold=True))
        title.append(pr.title, p.style(p.text, bold=True))
        tabs = Text()
        if self.ai is not None and not self.ai.error and self.ai.verdict:
            verdict = self.ai.verdict.strip()
            color = {"ai": p.error, "human": p.success}.get(verdict.lower(), p.warning)
            label = (
                " HUMAN "
                if verdict.lower() == "human"
                else f" {verdict.upper()} {self.ai.fraction_ai:.0%} AI "
            )
            tabs.append(label, p.style(color.get_contrast_text(1.0), color, bold=True))
            tabs.append("  ")
        for key, name, label in (
            ("1", "files", f"Files {len(pr.files)}"),
            ("2", "conversation", f"Conversation {len(pr.comments) + len(pr.threads)}"),
        ):
            active = self.tab == name
            key_style = p.style(p.bg, p.primary) if active else p.style(p.faint, p.panel)
            label_style = (
                p.style(p.bg, p.primary, bold=True) if active else p.style(p.muted, p.panel)
            )
            tabs.append(f" {key} ", key_style)
            tabs.append(f"{label} ", label_style)
            tabs.append(" ")

        # 2: state, people and branches; CI and the review decision on the right
        who = Text()
        who.append_text(state_badge(pr, p))
        who.append("  ")
        who.append(display_name(pr.author), p.style(p.author_color(pr.author), bold=True))
        who.append("  ")
        who.append(pr.head_ref, p.style(p.primary_fg))
        who.append(" → ", p.style(p.faint))
        who.append(pr.base_ref, p.style(p.primary_fg))
        who.append(f"  · updated {relative_time(pr.updated_at)}", p.style(p.faint))
        if pr.total_commits:
            commits = pr.total_commits
            who.append(f" · {commits} commit{'s' if commits != 1 else ''}", p.style(p.faint))
        status = Text()
        if pr.checks_state in CHECK_MARKS:
            label, color = CHECK_MARKS[pr.checks_state]
            status.append(label, p.style(tone(p, color)))
        if pr.review_decision in DECISIONS:
            label, color = DECISIONS[pr.review_decision]
            if status:
                status.append("   ")
            status.append(label, p.style(tone(p, color)))

        # 3: your review: progress, open threads, size; the pending review on the right
        progress = self._progress(pr, p)
        unresolved = pr.unresolved_count
        if unresolved:
            progress.append(
                f"   ● {unresolved} open thread{'s' if unresolved != 1 else ''}",
                p.style(p.accent_fg),
            )
        progress.append(f"   +{pr.additions}", p.style(p.add_fg))
        progress.append(f" −{pr.deletions}", p.style(p.del_fg))
        review = Text()
        if self.syncing:
            review.append("⟳ syncing  ", p.style(p.primary_fg))
        if pr.pending_review is not None:
            pending = pr.pending_comment_count
            review.append(
                f" ✎ {pending} pending comment{'s' if pending != 1 else ''} · S to submit ",
                p.style(p.bg, p.warning, bold=True),
            )
        elif not pr.viewer_did_author:
            review.append("S review · A approve", p.style(p.faint))
        return Text("\n").join([line(title, tabs), line(who, status), line(progress, review)])

    def _progress(self, pr: PullRequest, p: Palette) -> Text:
        """How much of the change has been viewed, weighted by changed lines."""
        files = [f for f in pr.files if f.path not in self.hidden] or pr.files
        total = sum(max(1, f.additions + f.deletions) for f in files)
        done = sum(max(1, f.additions + f.deletions) for f in files if f.is_viewed)
        fraction = done / total if total else 1.0
        viewed = sum(1 for f in files if f.is_viewed)
        width = 10
        filled = round(fraction * width)
        color = p.add_fg if fraction >= 1 else p.primary_fg
        text = Text()
        text.append("━" * filled, p.style(color))
        text.append("━" * (width - filled), p.style(p.fg_mix(0.18)))
        text.append(f" {fraction:.0%}", p.style(color, bold=True))
        text.append(f" · {viewed}/{len(files)} files viewed", p.style(p.muted))
        return text

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
