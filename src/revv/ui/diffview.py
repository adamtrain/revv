"""The diff view: every changed file of a pull request in one fast, scrollable document.

Built on Textual's Line API: only visible rows are rendered, and rendered code lines are
cached, so even very large pull requests scroll smoothly. Comment threads are drawn inline
under the lines they belong to.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.cache import LRUCache
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from revv import filters
from revv.classify import GitAttributes
from revv.diff import DiffLine, LineKind
from revv.filters import ignores
from revv.highlight import display_text
from revv.models import ChangedFile, Comment, FileStatus, PullRequest, ReviewThread, Side
from revv.ui.diffmodel import MAX_WRAP_ROWS, FileSection, Geometry, Row, RowKind, build_rows
from revv.ui.palette import Palette
from revv.ui.render import MarkdownRenderer, RenderedThread, ThreadRenderer, segments

NORMAL, CURSOR, SELECTED = 0, 1, 2
SCROLL_OFF = 3


@dataclass(slots=True)
class Selection:
    """Lines picked for a new comment: a single line or a range, on one side."""

    section: FileSection
    lines: list[DiffLine]
    side: Side | None  # set in split view; None means "each line's natural side"

    @property
    def first(self) -> DiffLine:
        return self.lines[0]

    @property
    def last(self) -> DiffLine:
        return self.lines[-1]

    def anchor(self, line: DiffLine) -> tuple[Side, int]:
        if self.side is None or line.kind is not LineKind.CONTEXT:
            return line.anchor
        number = line.old_no if self.side is Side.LEFT else line.new_no
        assert number is not None
        return self.side, number

    @property
    def end(self) -> tuple[Side, int]:
        return self.anchor(self.last)

    @property
    def start(self) -> tuple[Side, int]:
        return self.anchor(self.first)

    @property
    def label(self) -> str:
        start_side, start = self.start
        end_side, end = self.end
        if (start_side, start) == (end_side, end):
            return f"line {end}" + (" (old)" if end_side is Side.LEFT else "")
        return f"lines {start}–{end}" + (" (old)" if end_side is Side.LEFT else "")

    @property
    def commentable(self) -> bool:
        return all(not line.expanded for line in self.lines)


class DiffView(ScrollView, can_focus=True):
    """Scrollable, cursor-driven view over all file diffs of a pull request."""

    DEFAULT_CSS = """
    DiffView {
        background: $background;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        overflow-x: hidden;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j,down", "move(1)", "Down", show=False),
        Binding("k,up", "move(-1)", "Up", show=False),
        Binding("ctrl+d", "half_page(1)", "Half page down", show=False),
        Binding("ctrl+u", "half_page(-1)", "Half page up", show=False),
        Binding("pagedown,ctrl+f,space", "page(1)", "Page down", show=False),
        Binding("pageup,ctrl+b", "page(-1)", "Page up", show=False),
        Binding("g,home", "top", "Top", show=False),
        Binding("G,end", "bottom", "Bottom", show=False),
        Binding("right_curly_bracket", "next_change(1)", "Next change", show=False),
        Binding("left_curly_bracket", "next_change(-1)", "Prev change", show=False),
        Binding("right_square_bracket", "next_file(1)", "Next file", show=False),
        Binding("left_square_bracket", "next_file(-1)", "Prev file", show=False),
        Binding("n", "next_thread(1)", "Next thread", show=False),
        Binding("N", "next_thread(-1)", "Prev thread", show=False),
        Binding("u", "next_thread(1, True)", "Next unresolved thread", show=False),
        Binding("U", "next_thread(-1, True)", "Prev unresolved thread", show=False),
        Binding("enter", "activate", "Open/expand", show=False),
        Binding("z", "toggle_fold", "Fold", show=False),
        Binding("V", "select_mode", "Select lines", show=False),
        Binding("escape", "clear_selection", "Clear selection", show=False),
        Binding("h,left", "side(-1)", "Left side", show=False),
        Binding("l,right", "side(1)", "Right side", show=False),
        Binding("E", "expand_file", "Expand file", show=False),
        Binding("vertical_line", "toggle_split", "Split/unified", show=False),
    ]

    class CursorFileChanged(Message):
        def __init__(self, section: FileSection) -> None:
            super().__init__()
            self.section = section

    class ContentWanted(Message):
        """Full file text is needed (to expand context)."""

        def __init__(self, sections: list[FileSection]) -> None:
            super().__init__()
            self.sections = sections

    class CommentWanted(Message):
        """Enter was pressed on a code line: the screen should open the comment editor."""

    class CursorMoved(Message):
        pass

    class SectionsChanged(Message):
        """A hidden file was revealed (e.g. by jumping to it)."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pr: PullRequest | None = None
        self.sections: list[FileSection] = []
        self.rows: list[Row] = []
        self._starts: list[int] = []
        self.cursor = 0
        self.sel_anchor: int | None = None  # visual-selection anchor row
        self.cursor_side: Side = Side.RIGHT  # split view: which half the cursor is on
        self.split = False
        self.fold: dict[str, bool] = {}  # thread id -> collapsed, when the user chose
        self._palette: Palette | None = None
        self._md: MarkdownRenderer | None = None
        self._thread_renderer: ThreadRenderer | None = None
        self._code_cache: LRUCache[tuple, list[Strip]] = LRUCache(4096)
        self._thread_cache: LRUCache[tuple, RenderedThread] = LRUCache(512)
        self._geo: Geometry | None = None
        self._last_section: FileSection | None = None
        self._nw = 4
        self.pending_jump: FileSection | None = None  # applied after the first real layout
        self.hidden_kinds: set[str] = set()  # e.g. {"test", "generated"}
        self.since_mode = False  # showing only the changes since your last review
        self.attributes: GitAttributes | None = None  # linguist-generated rules
        self.auto_split = True  # choose side-by-side on the first layout if there's room

    # -- setup -------------------------------------------------------------------

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, self._theme_changed)

    def _theme_changed(self, _theme: object) -> None:
        self._palette = None
        self._md = None
        self._thread_renderer = None
        self._code_cache.clear()
        self._thread_cache.clear()
        for section in self.sections:
            section._hl_key = None
        self.refresh()

    @property
    def palette(self) -> Palette:
        if self._palette is None:
            self._palette = Palette.from_app(self.app)
        return self._palette

    @property
    def markdown(self) -> MarkdownRenderer:
        if self._md is None:
            self._md = MarkdownRenderer(self.palette)
        return self._md

    @property
    def thread_renderer(self) -> ThreadRenderer:
        if self._thread_renderer is None:
            self._thread_renderer = ThreadRenderer(self.palette, self.markdown)
        return self._thread_renderer

    def is_hidden(self, section: FileSection) -> bool:
        return not section.force_visible and bool(section.kinds & self.hidden_kinds)

    @property
    def visible_sections(self) -> list[FileSection]:
        return [s for s in self.sections if not self.is_hidden(s)]

    def hidden_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for section in self.sections:
            if self.is_hidden(section):
                for kind in section.kinds & self.hidden_kinds:
                    counts[kind] = counts.get(kind, 0) + 1
        return counts

    def reclassify(self) -> None:
        for section in self.sections:
            section.classify(self.attributes)

    def load(self, pr: PullRequest, *, keep_state: bool = False) -> None:
        """Show a (possibly refreshed) pull request."""
        old = {s.path: s for s in self.sections} if keep_state else {}
        self.pr = pr
        sections = []
        # Threads can sit on files the pull request no longer changes (e.g. the change
        # was reverted); give those files a place too, so every thread can be reached.
        changed = {f.path for f in pr.files}
        orphans = (
            []
            if self.since_mode  # that view deliberately shows only part of the files
            else sorted(
                {t.path for t in ignores().threads(pr.threads) if t.path not in changed},
                key=str.lower,
            )
        )
        files = [*pr.files, *(ChangedFile(path, FileStatus.UNCHANGED) for path in orphans)]
        for index, file in enumerate(files):
            section = FileSection(file, index)
            previous = old.get(file.path)
            if previous is not None and previous.file.patch == file.patch:
                section.collapsed = previous.collapsed
                section.expanded_gaps = previous.expanded_gaps
                section.new_lines = previous.new_lines
                section.content_state = previous.content_state
                section.force_visible = previous.force_visible
            section.classify(self.attributes)
            sections.append(section)
        self.sections = sections
        self._code_cache.clear()
        self._thread_cache.clear()
        self.sync_threads()

    def sync_threads(self) -> None:
        """Re-attach threads to files after comments changed, and re-layout."""
        if self.pr is None:
            return
        by_path: dict[str, list[ReviewThread]] = {}
        for thread in ignores().threads(self.pr.threads):  # ignored comments don't exist
            by_path.setdefault(thread.path, []).append(thread)
        for section in self.sections:
            threads = by_path.get(section.path, [])
            if self.since_mode:
                # Old-side lines refer to a different base here; only new-side threads fit.
                threads = [t for t in threads if t.is_file_level or t.side is Side.RIGHT]
            section.set_threads(threads)
            if self.since_mode:
                section.loose_threads = []
        self.relayout()

    # -- layout ------------------------------------------------------------------

    @property
    def content_width(self) -> int:
        return max(20, self.scrollable_content_region.width or self.size.width)

    def _geometry(self) -> Geometry:
        nw = max([4] + [len(str(s.max_line)) for s in self.sections if s._hunks is not None])
        self._nw = nw
        return Geometry(self.content_width, self.split, nw)

    def _thread_collapsed(self, thread: ReviewThread) -> bool:
        choice = self.fold.get(thread.id)
        if choice is not None:
            return choice
        return (thread.is_resolved or thread.is_outdated) and not thread.has_pending

    def _render_thread(
        self, section: FileSection, thread: ReviewThread, side: Side | None, focused: bool
    ) -> RenderedThread:
        geo = self._geo or self._geometry()
        _, width = geo.thread_box(side)
        collapsed = self._thread_collapsed(thread)
        key = (
            thread.id,
            thread.rev,
            width,
            focused,
            collapsed,
            len(thread.comments),
            filters.version,
        )
        rendered = self._thread_cache.get(key)
        if rendered is None:
            assert self.pr is not None
            hints = self._thread_hints(thread)
            rendered = self.thread_renderer.render(
                thread,
                width,
                pr_author=self.pr.author,
                focused=focused,
                collapsed=collapsed,
                original=lambda: section.original_lines(thread),
                hints=hints,
            )
            self._thread_cache[key] = rendered
        return rendered

    def _thread_hints(self, thread: ReviewThread) -> str:
        hints = ["r reply"]
        if thread.is_resolved:
            hints.append("x unresolve")
        elif not thread.is_pending:
            hints.append("x resolve")
        hints.append("e edit" if any(c.viewer_can_update for c in thread.comments) else "")
        hints.append("z fold")
        return " · ".join(h for h in hints if h)

    def relayout(self) -> None:
        """Rebuild all rows (after width, mode, fold or data changes), keeping the cursor."""
        if self.size.width <= 0:
            return  # not laid out yet; on_resize will call us again
        if self.auto_split:
            self.auto_split = False
            self.split = self.content_width >= 170
        key = self._row_key(self.rows[self.cursor]) if self.rows else None
        screen_offset = self.cursor - self.scroll_offset.y
        anchor_key = (
            self._row_key(self.rows[self.sel_anchor])
            if self.sel_anchor is not None and self.sel_anchor < len(self.rows)
            else None
        )
        geo = self._geo = self._geometry()

        def height(section: FileSection, thread: ReviewThread, side: Side | None) -> int:
            return len(self._render_thread(section, thread, side, False).strips)

        rows: list[Row] = []
        starts: list[int] = []
        for section in self.sections:
            starts.append(len(rows))
            if not self.is_hidden(section):
                rows.extend(build_rows(section, geo, height))
        self.rows = rows
        self._starts = starts
        self.virtual_size = Size(geo.width, len(rows))
        if key is not None:
            self.cursor = self._find_row(key)
            if anchor_key is not None:
                self.sel_anchor = self._find_row(anchor_key)
            self.scroll_to(y=max(0, self.cursor - screen_offset), animate=False, immediate=True)
        else:
            self.cursor = 0
        self.cursor = min(self.cursor, max(0, len(rows) - 1))
        self.refresh()
        if self.pending_jump is not None and rows:
            section, self.pending_jump = self.pending_jump, None
            if section.index > 0:
                self.jump_to_section(section)
            else:
                self.set_cursor(0)

    @staticmethod
    def _row_key(row: Row) -> tuple:
        section = row.section.index
        if row.kind is RowKind.THREAD and row.thread is not None:
            return (section, "thread", row.thread.id, row.tline)
        if row.kind is RowKind.GAP:
            gap = row.section.gaps()[row.gap]
            return (section, "gap", row.gap, gap.new_start if gap is not None else -1)
        if row.is_code:
            line = row.line or row.right
            assert line is not None
            return (section, "line", line.old_no, line.new_no, line.kind)
        return (section, row.kind.name)

    def _find_row(self, key: tuple) -> int:
        section_index = key[0]
        if section_index >= len(self._starts):
            return 0
        start = self._starts[section_index]
        end = (
            self._starts[section_index + 1]
            if section_index + 1 < len(self._starts)
            else len(self.rows)
        )
        kind = key[1]
        best = start
        for index in range(start, end):
            row = self.rows[index]
            if kind == "thread" and row.kind is RowKind.THREAD and row.thread is not None:
                if row.thread.id == key[2]:
                    # the thread may have shrunk (folded): clamp to its last row
                    best = index
                    if row.tline >= key[3]:
                        return index
            elif kind == "gap" and row.kind is RowKind.GAP and row.gap == key[2]:
                return index
            elif kind == "gap" and row.is_code and row.wrap == 0:
                # the gap was expanded: land on its first line
                line = row.line or row.right
                if line is not None and line.expanded and line.new_no == key[3]:
                    return index
            elif kind == "line" and row.is_code and row.wrap == 0:
                for line in (row.line, row.right):
                    if line is not None and (line.old_no, line.new_no, line.kind) == key[2:]:
                        return index
                # the gap containing the line may have been expanded/collapsed: stay close
                line = row.line or row.right
                if (
                    line is not None
                    and key[3] is not None
                    and line.new_no is not None
                    and line.new_no <= key[3]
                ):
                    best = index
        return best

    def on_resize(self, event: events.Resize) -> None:
        if self.sections:
            self._thread_cache.clear()
            self.relayout()

    # -- cursor ------------------------------------------------------------------

    @property
    def current_row(self) -> Row | None:
        if not self.rows:
            return None
        return self.rows[min(self.cursor, len(self.rows) - 1)]

    @property
    def current_section(self) -> FileSection | None:
        row = self.current_row
        return row.section if row else None

    @property
    def active_thread(self) -> ReviewThread | None:
        """The thread under the cursor, or the first one attached to the cursor line."""
        row = self.current_row
        if row is None:
            return None
        if row.kind is RowKind.THREAD:
            return row.thread
        if row.is_code:
            threads = self._threads_for_row(row)
            if threads:
                return threads[0]
        if row.kind is RowKind.FILE and row.section.file_threads:
            return row.section.file_threads[0]
        return None

    def _threads_for_row(self, row: Row) -> list[ReviewThread]:
        section = row.section
        if row.kind is RowKind.LINE and row.line is not None:
            return section.threads_at(row.line)
        if row.kind is RowKind.SPLIT:
            line = row.line_on(self.cursor_side)
            if line is not None and not line.expanded:
                key = (
                    line.anchor
                    if line.kind is not LineKind.CONTEXT
                    else (
                        self.cursor_side,
                        line.old_no if self.cursor_side is Side.LEFT else line.new_no,
                    )
                )
                return section.line_threads.get(key, [])  # type: ignore[arg-type]
        return []

    @property
    def active_comment(self) -> Comment | None:
        """The comment under the cursor inside an expanded thread."""
        row = self.current_row
        if row is None or row.kind is not RowKind.THREAD or row.thread is None:
            thread = self.active_thread
            return thread.comments[-1] if thread and thread.comments else None
        rendered = self._render_thread(row.section, row.thread, row.side, True)
        if row.tline < len(rendered.comments):
            return rendered.comments[row.tline]
        return None

    def selection(self) -> Selection | None:
        """The line(s) a new comment would attach to."""
        row = self.current_row
        if row is None or not row.is_code:
            return None
        rows = [row]
        if self.sel_anchor is not None:
            lo, hi = sorted((self.sel_anchor, self.cursor))
            rows = [
                r
                for r in self.rows[lo : hi + 1]
                if r.is_code and r.wrap == 0 and r.section is row.section
            ]
        lines: list[DiffLine] = []
        side: Side | None = None
        for r in rows:
            if r.kind is RowKind.SPLIT:
                side = self.cursor_side
                line = r.line_on(side)
            else:
                line = r.line
            if line is not None and line not in lines:
                lines.append(line)
        if not lines:
            return None
        return Selection(row.section, lines, side)

    def _is_selected(self, index: int) -> bool:
        if self.sel_anchor is None:
            return False
        lo, hi = sorted((self.sel_anchor, self.cursor))
        return lo <= index <= hi

    def set_cursor(self, index: int, *, center: bool = False, top: bool = False) -> None:
        if not self.rows:
            return
        index = max(0, min(index, len(self.rows) - 1))
        while index > 0 and not self.rows[index].selectable:
            index -= 1
        previous = self.cursor
        self.cursor = index
        self._ensure_visible(center=center, top=top)  # a scroll repaints everything anyway
        if self.sel_anchor is not None:
            self.refresh()
        else:
            self._refresh_span(previous)
            self._refresh_span(index)
        section = self.rows[index].section
        if section is not self._last_section:
            self._last_section = section
            self.post_message(self.CursorFileChanged(section))
        self.post_message(self.CursorMoved())

    def _item_span(self, index: int) -> tuple[int, int] | None:
        """All rows of the item at `index`: a wrapped line, or a whole thread."""
        if not 0 <= index < len(self.rows):
            return None
        row = self.rows[index]
        if row.kind is RowKind.THREAD:
            start = end = index - row.tline
            while (
                end + 1 < len(self.rows)
                and self.rows[end + 1].thread is row.thread
                and self.rows[end + 1].tline == self.rows[end].tline + 1
            ):
                end += 1
            return start, end
        return self._extent(self._extent_start(index))

    def _refresh_span(self, index: int) -> None:
        span = self._item_span(index)
        if span is not None:
            self.refresh_lines(span[0], span[1] - span[0] + 1)

    def _extent(self, index: int) -> tuple[int, int]:
        """First and last row of the item at `index` (wrapped line, or a whole thread)."""
        row = self.rows[index]
        end = index
        if row.kind is RowKind.THREAD:
            while (
                end + 1 < len(self.rows)
                and self.rows[end + 1].thread is row.thread
                and self.rows[end + 1].tline > self.rows[end].tline
            ):
                end += 1
            return index, end
        while end + 1 < len(self.rows) and self.rows[end + 1].wrap > 0:
            end += 1
        return index, end

    def _ensure_visible(self, *, center: bool = False, top: bool = False) -> None:
        height = self.scrollable_content_region.height or self.size.height
        if height <= 0:
            return
        first, last = self._extent(self.cursor)
        scroll_y = round(self.scroll_offset.y)
        margin = min(SCROLL_OFF, max(0, (height - 2) // 3))
        if top:
            target = first  # e.g. a file header: put it right at the top
        elif center:
            target = max(0, first - height // 2)
        elif first < scroll_y + margin + 1:
            target = max(0, first - margin - 1)
        elif last >= scroll_y + height - margin:
            target = min(first - 1, last - height + margin + 1)
        else:
            return
        self.scroll_to(y=max(0, target), animate=False, immediate=True)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        # Mouse-wheel scrolling drags the cursor along so it stays on screen.
        if not self.rows:
            return
        height = self.scrollable_content_region.height
        if height <= 0:
            return
        top = round(new_value)
        first_visible = top + (1 if self._sticky_section(top) is not None else 0)
        last_visible = top + height - 1
        if first_visible <= self.cursor <= last_visible:
            return
        target = first_visible if self.cursor < first_visible else last_visible
        target = max(0, min(target, len(self.rows) - 1))
        step = 1 if self.cursor < first_visible else -1
        while 0 < target < len(self.rows) - 1 and not self.rows[target].selectable:
            target += step
        self.cursor = target
        self.post_message(self.CursorMoved())

    def _step(self, index: int, delta: int) -> int:
        target = index + delta
        while 0 <= target < len(self.rows) and not self.rows[target].selectable:
            target += 1 if delta > 0 else -1
        if not 0 <= target < len(self.rows):
            return index
        return target

    def action_move(self, delta: int) -> None:
        index = self.cursor
        for _ in range(abs(delta)):
            index = self._step(index, 1 if delta > 0 else -1)
        self.set_cursor(index)

    def action_half_page(self, direction: int) -> None:
        height = self.scrollable_content_region.height
        self._jump_by(direction * max(1, height // 2))

    def action_page(self, direction: int) -> None:
        height = self.scrollable_content_region.height
        self._jump_by(direction * max(1, height - 2))

    def _jump_by(self, amount: int) -> None:
        target = max(0, min(len(self.rows) - 1, self.cursor + amount))
        self.scroll_to(y=max(0, self.scroll_offset.y + amount), animate=False, immediate=True)
        step = 1 if amount > 0 else -1
        while 0 < target < len(self.rows) - 1 and not self.rows[target].selectable:
            target += step
        self.set_cursor(target)

    def action_top(self) -> None:
        self.set_cursor(0)

    def action_bottom(self) -> None:
        self.set_cursor(len(self.rows) - 1)

    def action_next_file(self, direction: int) -> None:
        visible = self.visible_sections
        if not visible:
            return
        section = self.current_section
        if section is None or section not in visible:
            self.set_cursor(self._starts[visible[0].index], top=True)
            return
        position = visible.index(section)
        if direction < 0 and self.cursor > self._starts[section.index]:
            target = section  # first go back to the top of the current file
        else:
            target = visible[max(0, min(len(visible) - 1, position + direction))]
        self.set_cursor(self._starts[target.index], top=True)

    def _reveal(self, section: FileSection) -> None:
        if self.is_hidden(section):
            section.force_visible = True
            self.post_message(self.SectionsChanged())

    def changed_lines(self) -> Iterator[tuple[FileSection, DiffLine]]:
        """Every added or deleted line of the pull request, in display order."""
        for section in self.sections:
            for hunk in section.hunks:
                for line in hunk.lines:
                    if line.kind is not LineKind.CONTEXT:
                        yield section, line

    def jump_to_line(self, section: FileSection, line: DiffLine) -> None:
        """Show a diff line (unfolding or revealing its file if needed) and put the cursor on it."""
        changed = False
        if section.collapsed:
            section.collapsed = False
            changed = True
        if self.is_hidden(section):
            self._reveal(section)
            changed = True
        if changed:
            self.relayout()
        start = self._starts[section.index]
        for index in range(start, len(self.rows)):
            row = self.rows[index]
            if row.section is not section:
                break
            if row.wrap == 0 and (row.line is line or row.right is line):
                if self.split:
                    self.cursor_side = Side.LEFT if row.right is not line else Side.RIGHT
                self.set_cursor(index, center=True)
                return
        self.jump_to_section(section)

    def jump_to_section(self, section: FileSection) -> None:
        if self.is_hidden(section):
            self._reveal(section)
            self.relayout()
        self.set_cursor(self._starts[section.index], top=True)

    def _is_change_row(self, index: int) -> bool:
        row = self.rows[index]
        return (
            row.is_code
            and row.wrap == 0
            and any(
                line is not None and line.kind is not LineKind.CONTEXT
                for line in (row.line, row.right)
            )
        )

    def _is_change_start(self, index: int) -> bool:
        if not self._is_change_row(index):
            return False
        previous = index - 1
        while previous >= 0 and (
            self.rows[previous].kind is RowKind.THREAD or self.rows[previous].wrap > 0
        ):
            previous -= 1
        if previous < 0 or self.rows[previous].section is not self.rows[index].section:
            return True
        return not self._is_change_row(previous)

    def action_next_change(self, direction: int) -> None:
        """Jump to the start of the next (or previous) block of changed lines."""
        index = self.cursor + direction
        while 0 <= index < len(self.rows):
            if self._is_change_start(index):
                self.set_cursor(index, center=True)
                return
            index += direction
        self.app.bell()

    def _thread_order(self) -> list[tuple[tuple[int, float], ReviewThread]]:
        """Every thread in reading order, including those in folded or hidden files."""
        first_rows: dict[str, int] = {}
        for index, row in enumerate(self.rows):
            if row.kind is RowKind.THREAD and row.tline == 0 and row.thread is not None:
                first_rows.setdefault(row.thread.id, index)
        order: list[tuple[tuple[int, float], ReviewThread]] = []
        for section in self.sections:
            anchored = sorted(
                (t for threads in section.line_threads.values() for t in threads),
                key=lambda t: t.line or 0,
            )
            threads = [*section.file_threads, *section.loose_threads, *anchored]
            base = self._starts[section.index] if section.index < len(self._starts) else 0
            for ordinal, thread in enumerate(threads):
                row = first_rows.get(thread.id)
                position = float(row) if row is not None else base + 0.5 + ordinal / 1000
                order.append(((section.index, position), thread))
        order.sort(key=lambda entry: entry[0])
        return order

    def action_next_thread(self, direction: int, unresolved: bool = False) -> None:
        """Jump to the next/previous comment thread (optionally only unresolved ones),
        unfolding or revealing its file if needed, and wrapping around at the ends."""
        current = self.current_row
        section = self.current_section
        current_thread = current.thread if current and current.kind is RowKind.THREAD else None
        here = (section.index if section else 0, float(self.cursor))
        candidates = [
            (key, thread)
            for key, thread in self._thread_order()
            if thread is not current_thread and not (unresolved and thread.is_resolved)
        ]
        if not candidates:
            kind = "unresolved threads" if unresolved else "comment threads"
            qualifies = current_thread is not None and not (
                unresolved and current_thread.is_resolved
            )
            if qualifies:
                self.notify(f"This is the only one of the {kind}", timeout=2)
            else:
                self.notify(f"No {kind} in this pull request", timeout=2)
            return
        if direction > 0:
            ahead = [entry for entry in candidates if entry[0] > here]
            target = ahead[0] if ahead else candidates[0]
        else:
            behind = [entry for entry in candidates if entry[0] < here]
            target = behind[-1] if behind else candidates[-1]
        self.jump_to_thread(target[1])

    def thread_row(self, thread: ReviewThread) -> int | None:
        for index, row in enumerate(self.rows):
            if row.kind is RowKind.THREAD and row.thread is thread and row.tline == 0:
                return index
        return None

    def jump_to_thread(self, thread: ReviewThread) -> None:
        for section in self.sections:
            if section.path != thread.path:
                continue
            if section.collapsed or self.is_hidden(section):
                section.collapsed = False
                self._reveal(section)
                self.relayout()
        index = self.thread_row(thread)
        if index is None:
            for section in self.sections:
                if section.path == thread.path:
                    self.jump_to_section(section)
            return
        self.set_cursor(index, center=True)

    def action_side(self, direction: int) -> None:
        if not self.split:
            return
        self.cursor_side = Side.LEFT if direction < 0 else Side.RIGHT
        self.refresh()
        self.post_message(self.CursorMoved())

    def action_select_mode(self) -> None:
        row = self.current_row
        if row is None or not row.is_code:
            self.app.bell()
            return
        self.sel_anchor = None if self.sel_anchor is not None else self.cursor
        self.refresh()
        self.post_message(self.CursorMoved())

    def action_clear_selection(self) -> None:
        if self.sel_anchor is not None:
            self.sel_anchor = None
            self.refresh()
            self.post_message(self.CursorMoved())

    # -- folding & expanding -----------------------------------------------------------

    def action_activate(self) -> None:
        row = self.current_row
        if row is None:
            return
        if row.kind is RowKind.FILE:
            self.toggle_section(row.section)
        elif row.kind is RowKind.GAP:
            self.expand_gap(row.section, row.gap)
        elif row.kind is RowKind.THREAD:
            self.action_toggle_fold()
        elif row.is_code:
            self.post_message(self.CommentWanted())

    def action_toggle_fold(self) -> None:
        row = self.current_row
        if row is None:
            return
        thread = self.active_thread
        if thread is None:
            if row.kind is RowKind.FILE or row.is_code:
                self.toggle_section(row.section)
            return
        self.fold[thread.id] = not self._thread_collapsed(thread)
        self.relayout()
        index = self.thread_row(thread)
        if index is not None and row.kind is RowKind.THREAD:
            self.set_cursor(index)

    def toggle_section(self, section: FileSection, collapsed: bool | None = None) -> None:
        section.collapsed = (not section.collapsed) if collapsed is None else collapsed
        self.relayout()
        if section.collapsed:
            self.set_cursor(self._starts[section.index])

    def expand_gap(self, section: FileSection, gap: int) -> None:
        section.expanded_gaps.add(gap)
        if section.new_lines is None:
            if section.content_state == "unavailable":
                self.notify("Full file content isn't available for this file", severity="warning")
                section.expanded_gaps.discard(gap)
                return
            self.post_message(self.ContentWanted([section]))
            return
        self.relayout()

    def action_expand_file(self) -> None:
        section = self.current_section
        if section is None:
            return
        gaps = section.gaps()
        section.expanded_gaps.update(i for i, gap in enumerate(gaps) if gap is not None)
        if section.new_lines is None:
            section.expanded_gaps.update(range(len(section.hunks) + 1))
            self.post_message(self.ContentWanted([section]))
            return
        self.relayout()

    def content_loaded(self, section: FileSection, text: str | None) -> None:
        section.set_new_text(text)
        section.classify(self.attributes)
        if text is None:
            section.expanded_gaps.clear()
        self._code_cache.clear()
        self.relayout()

    def action_toggle_split(self) -> None:
        self.split = not self.split
        self.sel_anchor = None
        self._code_cache.clear()
        self._thread_cache.clear()
        self.relayout()
        self.notify("Side-by-side view" if self.split else "Unified view", timeout=1.5)

    # -- mouse ---------------------------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        index = round(self.scroll_offset.y) + offset.y
        if (
            offset.y == 0
            and index < len(self.rows)
            and self._sticky_section(round(self.scroll_offset.y))
        ):
            section = self._sticky_section(round(self.scroll_offset.y))
            if section is not None:
                self.jump_to_section(section)
                return
        if not 0 <= index < len(self.rows):
            return
        if self.split and self._geo is not None:
            self.cursor_side = Side.LEFT if offset.x < self._geo.right_start else Side.RIGHT
        while index > 0 and not self.rows[index].selectable:
            index -= 1
        self.set_cursor(index)
        if event.chain >= 2 or self.rows[index].kind in (RowKind.GAP, RowKind.FILE):
            self.action_activate()

    # -- rendering -------------------------------------------------------------------

    def _sticky_section(self, top: int) -> FileSection | None:
        if not self.rows or top <= 0 or top >= len(self.rows):
            return None
        row = self.rows[top]
        if row.kind in (RowKind.FILE, RowKind.SPACER):
            return None
        return row.section

    def render_line(self, y: int) -> Strip:
        width = self.content_width
        p = self.palette
        top = round(self.scroll_offset.y)
        index = top + y
        if y == 0:
            sticky = self._sticky_section(top)
            if sticky is not None:
                return self._render_file_header(sticky, width, sticky=True)
        if index >= len(self.rows):
            return Strip.blank(width, p.style(bg=p.bg))
        row = self.rows[index]
        try:
            return self._render_row(row, index, width)
        except Exception as error:  # never let one odd line take the whole view down
            self.log.error(f"render error on row {index}: {error!r}")
            text = Text(f"  ⚠ could not render this line ({error})", p.style(p.del_fg, p.bg))
            return Strip(segments(text, self.markdown.console)).adjust_cell_length(
                width, p.style(bg=p.bg)
            )

    def _state(self, index: int) -> int:
        if index == self.cursor or (
            self.rows[index].wrap > 0 and self._extent_start(index) == self.cursor
        ):
            return CURSOR
        if self._is_selected(index) or (
            self.rows[index].wrap > 0 and self._is_selected(self._extent_start(index))
        ):
            return SELECTED
        return NORMAL

    def _extent_start(self, index: int) -> int:
        while index > 0 and self.rows[index].wrap > 0:
            index -= 1
        return index

    def _render_row(self, row: Row, index: int, width: int) -> Strip:
        p = self.palette
        kind = row.kind
        if kind is RowKind.FILE:
            return self._render_file_header(row.section, width, cursor=index == self.cursor)
        if kind is RowKind.LINE:
            return self._render_unified(row, index, width)
        if kind is RowKind.SPLIT:
            return self._render_split(row, index, width)
        if kind is RowKind.THREAD:
            return self._render_thread_row(row, index, width)
        if kind is RowKind.GAP:
            return self._render_gap(row, index, width)
        if kind is RowKind.NOTE:
            style = p.style(p.muted, p.bg, italic=True)
            text = Text(f"    {row.text}", style)
            if index == self.cursor:
                text.stylize(p.style(bg=p.cursor_bg(p.bg)))
            return Strip(segments(text, self.markdown.console)).adjust_cell_length(
                width, style if index != self.cursor else p.style(bg=p.cursor_bg(p.bg))
            )
        return Strip.blank(width, p.style(bg=p.bg))

    # file header ------------------------------------------------------------------

    def _render_file_header(
        self, section: FileSection, width: int, *, cursor: bool = False, sticky: bool = False
    ) -> Strip:
        p = self.palette
        file = section.file
        bg = p.header_bg if not cursor else p.header_bg.blend(p.primary, 0.35)
        base = p.style(p.text, bg)
        status_color = {
            FileStatus.ADDED: p.add_fg,
            FileStatus.REMOVED: p.del_fg,
            FileStatus.RENAMED: p.primary_fg,
            FileStatus.COPIED: p.primary_fg,
        }.get(file.status, p.warning_fg)
        text = Text(" ", base)
        text.append("▸ " if section.collapsed else "▾ ", p.style(p.muted, bg))
        text.append(file.status.letter, p.style(status_color, bg, bold=True))
        text.append(" ")
        if file.previous_path and file.previous_path != file.path:
            text.append(file.previous_path, p.style(p.muted, bg))
            text.append(" → ", p.style(p.faint, bg))
        directory, _, name = file.path.rpartition("/")
        if directory:
            text.append(directory + "/", p.style(p.muted, bg))
        text.append(name, p.style(p.text, bg, bold=True))
        right = Text("", base)
        if section.pending_count:
            right.append(f" ✎ {section.pending_count} pending ", p.style(p.warning_fg, bg))
        if section.unresolved_count:
            right.append(f" ● {section.unresolved_count} ", p.style(p.accent_fg, bg))
        resolved = sum(1 for t in section.threads if t.is_resolved)
        if resolved:
            right.append(f" ✓ {resolved} ", p.style(p.faint, bg))
        if file.additions:
            right.append(f" +{file.additions}", p.style(p.add_fg, bg))
        if file.deletions:
            right.append(f" −{file.deletions}", p.style(p.del_fg, bg))
        if file.is_viewed:
            right.append("  ✓ viewed ", p.style(p.add_fg, bg, bold=True))
        elif file.viewed.value == "DISMISSED":
            right.append("  ↻ changed since viewed ", p.style(p.warning_fg, bg))
        else:
            right.append("  ☐ viewed ", p.style(p.faint, bg))
        if sticky:
            right.append("↑", p.style(p.faint, bg))
        available = width - right.cell_len - 1
        if text.cell_len > available:
            text.truncate(max(8, available), overflow="ellipsis")
        text.pad_right(max(0, width - text.cell_len - right.cell_len))
        text.append_text(right)
        strip = Strip(segments(text, self.markdown.console))
        if sticky:
            strip = strip.apply_style(Style(underline=True))
        return strip.adjust_cell_length(width, base)

    # code rows --------------------------------------------------------------------

    def _line_colors(self, line: DiffLine | None, state: int):
        p = self.palette
        if line is None:
            bg = p.bg.blend(p.fg, 0.02)
            gutter = bg
            emph = bg
        elif line.kind is LineKind.ADD:
            bg, gutter, emph = p.add_bg, p.add_gutter_bg, p.add_emph_bg
        elif line.kind is LineKind.DEL:
            bg, gutter, emph = p.del_bg, p.del_gutter_bg, p.del_emph_bg
        elif line.expanded:
            bg, gutter, emph = p.expanded_bg, p.expanded_bg, p.expanded_bg
        else:
            bg, gutter, emph = p.bg, p.context_gutter_bg, p.bg
        if state == CURSOR:
            bg, emph = p.cursor_bg(bg), p.cursor_bg(emph)
            gutter = p.cursor_gutter_bg
        elif state == SELECTED:
            bg, emph = p.select_bg(bg), p.select_bg(emph)
            gutter = p.select_bg(gutter)
        return bg, gutter, emph

    def _code_strips(
        self, section: FileSection, line: DiffLine | None, width: int, state: int
    ) -> list[Strip]:
        key = (id(line), width, state)
        cached = self._code_cache.get(key)
        if cached is not None:
            return cached
        p = self.palette
        bg, _gutter, emph = self._line_colors(line, state)
        base = p.style(p.text, bg)
        if line is None:
            strips = [Strip.blank(width, base)]
            self._code_cache[key] = strips
            return strips
        section.ensure_highlight(p.highlighter)
        highlighted = section.highlighted(line)
        text = highlighted.copy() if highlighted is not None else Text(display_text(line.text))
        text.style = base
        if line.expanded:
            text.stylize(Style(dim=True))
        if line.emph:
            emph_style = Style(bgcolor=emph.rich_color)
            for start, end in line.emph:
                text.stylize(emph_style, start, end)
        if line.no_newline:
            text.append(" ⏎̸", p.style(p.faint, bg))
        strip = Strip(segments(text, self.markdown.console))
        total = max(1, strip.cell_length)
        count = -(-total // width)
        strips = [
            strip.crop(i * width, (i + 1) * width).adjust_cell_length(width, base)
            for i in range(min(count, MAX_WRAP_ROWS))
        ]
        if count > MAX_WRAP_ROWS:
            start = (MAX_WRAP_ROWS - 1) * width
            more = Text(
                f" … {total - start - width + 16:,} more columns ",
                p.style(p.muted, bg, italic=True),
            )
            keep = max(0, width - more.cell_len)
            last = Strip.join(
                [strip.crop(start, start + keep), Strip(segments(more, self.markdown.console))]
            )
            strips[-1] = last.adjust_cell_length(width, base)
        self._code_cache[key] = strips
        return strips

    def _thread_marker(self, section: FileSection, line: DiffLine | None) -> Segment | None:
        if line is None:
            return None
        threads = section.threads_at(line)
        if not threads:
            return None
        p = self.palette
        if any(t.has_pending for t in threads):
            color = p.warning
        elif all(t.is_resolved for t in threads):
            color = p.faint
        else:
            color = p.accent
        return Segment("●", p.style(color))

    def _render_unified(self, row: Row, index: int, width: int) -> Strip:
        p = self.palette
        geo = self._geo or self._geometry()
        line = row.line
        assert line is not None
        state = self._state(index)
        bg, gutter_bg, _ = self._line_colors(line, state)
        nw = geo.nw
        if state == CURSOR:
            num_style = p.style(p.bg if p.dark else p.fg, gutter_bg, bold=True)
        else:
            num_style = p.style(p.faint if line.kind is LineKind.CONTEXT else p.muted, gutter_bg)
        if row.wrap == 0:
            old = (
                f"{line.old_no:>{nw}}"
                if line.old_no is not None and line.kind is not LineKind.ADD
                else " " * nw
            )
            new = (
                f"{line.new_no:>{nw}}"
                if line.new_no is not None and line.kind is not LineKind.DEL
                else " " * nw
            )
            sign = {LineKind.ADD: "+", LineKind.DEL: "-"}.get(line.kind, " ")
        else:
            old = new = " " * nw
            sign = "↪" if state else " "
        sign_style = {
            LineKind.ADD: p.style(p.add_fg, gutter_bg, bold=True),
            LineKind.DEL: p.style(p.del_fg, gutter_bg, bold=True),
        }.get(line.kind, num_style)
        if state == CURSOR:
            sign_style = num_style
        marker = None
        if state in (SELECTED, CURSOR):
            marker = Segment("▌", p.style(p.accent if state == SELECTED else p.primary, bg))
        elif row.wrap == 0:
            marker_segment = self._thread_marker(row.section, line)
            if marker_segment is not None:
                marker = Segment(
                    marker_segment.text, (marker_segment.style or Style()) + p.style(bg=gutter_bg)
                )
        segments = [
            marker or Segment(" ", p.style(bg=gutter_bg)),
            Segment(f"{old} {new} ", num_style),
            Segment(sign, sign_style),
            Segment(" ", p.style(bg=bg)),
        ]
        code = self._code_strips(row.section, line, geo.unified_code, state)
        piece = (
            code[min(row.wrap, len(code) - 1)]
            if row.wrap < len(code)
            else Strip.blank(geo.unified_code, p.style(bg=bg))
        )
        return Strip([*segments, *piece]).adjust_cell_length(width, p.style(bg=bg))

    def _render_half(
        self,
        row: Row,
        line: DiffLine | None,
        side: Side,
        width: int,
        state: int,
    ) -> list[Segment]:
        p = self.palette
        geo = self._geo or self._geometry()
        nw = geo.nw
        bg, gutter_bg, _ = self._line_colors(line, state)
        if line is None:
            filler = p.bg.blend(p.fg, 0.025)
            return [Segment(" " * (geo.side_gutter + width), p.style(p.faint, filler))]
        number = line.old_no if side is Side.LEFT else line.new_no
        if state == CURSOR:
            num_style = p.style(p.bg if p.dark else p.fg, gutter_bg, bold=True)
        else:
            num_style = p.style(p.faint if line.kind is LineKind.CONTEXT else p.muted, gutter_bg)
        if row.wrap == 0 and number is not None:
            num = f"{number:>{nw}}"
            sign = {LineKind.ADD: "+", LineKind.DEL: "-"}.get(line.kind, " ")
        else:
            num, sign = " " * nw, " "
        sign_style = {
            LineKind.ADD: p.style(p.add_fg, gutter_bg, bold=True),
            LineKind.DEL: p.style(p.del_fg, gutter_bg, bold=True),
        }.get(line.kind, num_style)
        code = self._code_strips(row.section, line, width, state)
        if row.wrap < len(code):
            piece = code[row.wrap]
        else:
            piece = Strip.blank(width, p.style(bg=bg))
        return [
            Segment(num, num_style),
            Segment(" ", p.style(bg=gutter_bg)),
            Segment(sign, sign_style),
            Segment(" ", p.style(bg=bg)),
            *piece,
        ]

    def _render_split(self, row: Row, index: int, width: int) -> Strip:
        p = self.palette
        geo = self._geo or self._geometry()
        state = self._state(index)
        left_state = state if self.cursor_side is Side.LEFT or state == SELECTED else NORMAL
        right_state = state if self.cursor_side is Side.RIGHT or state == SELECTED else NORMAL
        if state == SELECTED:
            left_state = SELECTED if self.cursor_side is Side.LEFT else NORMAL
            right_state = SELECTED if self.cursor_side is Side.RIGHT else NORMAL
        marker: Segment
        if state in (CURSOR, SELECTED):
            marker = Segment("▌", p.style(p.primary if state == CURSOR else p.accent, p.bg))
        else:
            dot = None
            if row.wrap == 0:
                dot = self._thread_marker(row.section, row.line) or self._thread_marker(
                    row.section, row.right
                )
            marker = (
                Segment(dot.text, (dot.style or Style()) + p.style(bg=p.bg))
                if dot
                else Segment(" ", p.style(bg=p.bg))
            )
        left = self._render_half(row, row.line, Side.LEFT, geo.left_code, left_state)
        right = self._render_half(row, row.right, Side.RIGHT, geo.right_code, right_state)
        divider = Segment("│", p.style(p.faint, p.bg))
        return Strip([marker, *left, divider, *right]).adjust_cell_length(width, p.style(bg=p.bg))

    def _render_gap(self, row: Row, index: int, width: int) -> Strip:
        p = self.palette
        section = row.section
        gap = section.gaps()[row.gap]
        cursor = index == self.cursor
        bg = p.hunk_bg if not cursor else p.cursor_bg(p.hunk_bg)
        style = p.style(p.primary_fg, bg)
        text = Text(" ", style)
        if gap is not None and gap.count is not None:
            count = f"{gap.count} unchanged line{'s' if gap.count != 1 else ''}"
        else:
            count = "more lines"
        loading = section.content_state == "loading" and row.gap in section.expanded_gaps
        text.append("  ⋯ ", p.style(p.primary_fg, bg, bold=True))
        text.append(f"{count}" if not loading else "loading…", style)
        if row.hunk is not None:
            header = row.hunk.header
            text.append("   ")
            text.append(
                header.split("@@")[1].strip() if "@@" in header else header, p.style(p.faint, bg)
            )
            if row.hunk.section:
                text.append("  " + row.hunk.section, p.style(p.muted, bg, italic=True))
        if cursor:
            hint = Text("↵ expand ", p.style(p.muted, bg))
            text.truncate(max(1, width - hint.cell_len), overflow="ellipsis")
            text.pad_right(max(0, width - text.cell_len - hint.cell_len))
            text.append_text(hint)
        return Strip(segments(text, self.markdown.console)).adjust_cell_length(
            width, p.style(bg=bg)
        )

    def _render_thread_row(self, row: Row, index: int, width: int) -> Strip:
        p = self.palette
        geo = self._geo or self._geometry()
        thread = row.thread
        assert thread is not None
        current = self.current_row
        focused = (
            current is not None and current.thread is thread and current.kind is RowKind.THREAD
        )
        rendered = self._render_thread(row.section, thread, row.side, focused)
        indent, box_width = geo.thread_box(row.side)
        bg_style = p.style(bg=p.bg)
        if row.tline >= len(rendered.strips):
            return Strip.blank(width, bg_style)
        strip = rendered.strips[row.tline]
        segments: list[Segment] = []
        if index == self.cursor:
            segments.append(Segment("▌", p.style(p.primary, p.bg)))
            segments.append(Segment(" " * max(0, indent - 1), bg_style))
        else:
            segments.append(Segment(" " * indent, bg_style))
        return Strip([*segments, *strip.adjust_cell_length(box_width)]).adjust_cell_length(
            width, bg_style
        )
