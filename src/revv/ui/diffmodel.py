"""Per-file view state and the flattening of a whole pull request into display rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from rich.cells import cell_len
from rich.text import Text

from revv.classify import HEADER_LINES, GitAttributes, is_generated, is_test_file
from revv.diff import (
    DiffLine,
    Gap,
    Hunk,
    LineKind,
    change_blocks,
    compute_gaps,
    parse_patch,
    word_diff,
)
from revv.highlight import Highlighter, display_text
from revv.models import ChangedFile, FileStatus, ReviewThread, Side


class RowKind(IntEnum):
    FILE = 0  # file header
    GAP = 1  # hidden unchanged lines (expandable)
    LINE = 2  # a unified diff line (or a wrapped continuation of one)
    SPLIT = 3  # a side-by-side pair of lines
    THREAD = 4  # one terminal row of a rendered comment thread
    NOTE = 5  # informational text (binary file, etc.)
    SPACER = 6  # blank separator between files


@dataclass(slots=True, eq=False)
class Row:
    kind: RowKind
    section: FileSection
    line: DiffLine | None = None  # LINE, or the left half of SPLIT
    right: DiffLine | None = None  # right half of SPLIT
    wrap: int = 0  # >0 for continuation rows of a wrapped line
    gap: int = -1  # GAP: index into section.gaps
    hunk: Hunk | None = None  # GAP: the hunk after the gap
    thread: ReviewThread | None = None
    tline: int = 0  # THREAD: which rendered row of the thread
    side: Side | None = None  # THREAD in split mode: which half it hangs under
    text: str = ""  # NOTE

    @property
    def is_code(self) -> bool:
        return self.kind is RowKind.LINE or self.kind is RowKind.SPLIT

    @property
    def selectable(self) -> bool:
        return self.kind is not RowKind.SPACER and self.wrap == 0

    def line_on(self, side: Side) -> DiffLine | None:
        if self.kind is RowKind.LINE:
            return self.line
        if self.kind is RowKind.SPLIT:
            return self.line if side is Side.LEFT else self.right
        return None


def split_pairs(lines: list[DiffLine]) -> list[tuple[DiffLine | None, DiffLine | None]]:
    """Pair up deletions and additions for side-by-side display."""
    pairs: list[tuple[DiffLine | None, DiffLine | None]] = []
    dels: list[DiffLine] = []
    adds: list[DiffLine] = []

    def flush() -> None:
        for i in range(max(len(dels), len(adds))):
            pairs.append((dels[i] if i < len(dels) else None, adds[i] if i < len(adds) else None))
        dels.clear()
        adds.clear()

    for line in lines:
        if line.kind is LineKind.CONTEXT:
            flush()
            pairs.append((line, line))
        elif line.kind is LineKind.DEL:
            if adds:
                flush()
            dels.append(line)
        else:
            adds.append(line)
    flush()
    return pairs


class FileSection:
    """Everything the diff view knows about one changed file."""

    def __init__(self, file: ChangedFile, index: int) -> None:
        self.file = file
        self.index = index
        self.collapsed = file.is_viewed
        self.new_lines: list[str] | None = None  # full text of the new version, once fetched
        self.content_state = "none"  # none / loading / loaded / unavailable
        self.expanded_gaps: set[int] = set()
        self.threads: list[ReviewThread] = []
        self.file_threads: list[ReviewThread] = []
        self.line_threads: dict[tuple[Side, int], list[ReviewThread]] = {}
        self.loose_threads: list[ReviewThread] = []  # outdated or not locatable in the diff
        self._hunks: list[Hunk] | None = None
        self._gap_cache: dict[int, list[DiffLine]] = {}
        self._hl_key: tuple[str, bool] | None = None
        self.hl_old: dict[int, Text] = {}
        self.hl_new: dict[int, Text] = {}
        self.max_line = 0
        self.kinds: set[str] = set()  # "test" and/or "generated"
        self.force_visible = False  # shown even though its kind is hidden
        self.classify()

    @property
    def path(self) -> str:
        return self.file.path

    @property
    def is_orphan(self) -> bool:
        """Not part of the diff anymore; only here for comments left on an earlier version."""
        return self.file.status == FileStatus.UNCHANGED

    def head_lines(self) -> list[str] | None:
        """The first lines of the file, if we have them (to spot generated-file headers)."""
        if self.new_lines is not None:
            return self.new_lines[:HEADER_LINES]
        hunks = self.hunks
        if not hunks:
            return None
        first = hunks[0]
        if self.file.status == FileStatus.REMOVED:
            if first.old_first == 1:
                return [ln.text for ln in first.lines if ln.kind is not LineKind.ADD][:HEADER_LINES]
            return None
        if first.new_first == 1:
            return [ln.text for ln in first.lines if ln.kind is not LineKind.DEL][:HEADER_LINES]
        return None

    def classify(self, attributes: GitAttributes | None = None) -> None:
        kinds = set()
        if is_test_file(self.path):
            kinds.add("test")
        if is_generated(self.path, self.head_lines(), attributes):
            kinds.add("generated")
        self.kinds = kinds

    @property
    def hunks(self) -> list[Hunk]:
        if self._hunks is None:
            self._hunks = parse_patch(self.file.patch) if self.file.patch else []
            for hunk in self._hunks:
                for dels, adds in change_blocks(hunk.lines):
                    for old, new in zip(dels, adds, strict=False):
                        result = word_diff(display_text(old.text), display_text(new.text))
                        if result is not None:
                            old.emph, new.emph = result
            numbers = [
                max(h.old_start + h.old_count, h.new_start + h.new_count) for h in self._hunks
            ]
            self.max_line = max(numbers, default=0)
        return self._hunks

    @property
    def is_whole_file(self) -> bool:
        """Added and removed files are entirely inside the patch: nothing to expand."""
        return self.file.status in (FileStatus.ADDED, FileStatus.REMOVED)

    def gaps(self) -> list[Gap | None]:
        if self.is_whole_file:
            return [None] * (len(self.hunks) + 1)
        total = len(self.new_lines) if self.new_lines is not None else None
        return compute_gaps(self.hunks, total)

    def set_new_text(self, text: str | None) -> None:
        if text is None:
            self.content_state = "unavailable"
            return
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        self.new_lines = [line.removesuffix("\r") for line in lines]
        self.content_state = "loaded"
        self._gap_cache.clear()
        self.max_line = max(self.max_line, len(self.new_lines))

    def gap_lines(self, index: int) -> list[DiffLine]:
        cached = self._gap_cache.get(index)
        if cached is None:
            gap = self.gaps()[index]
            cached = gap.lines(self.new_lines) if gap is not None and self.new_lines else []
            self._gap_cache[index] = cached
        return cached

    def set_threads(self, threads: list[ReviewThread]) -> None:
        self.threads = threads
        self.file_threads = [t for t in threads if t.is_file_level]
        anchors: set[tuple[Side, int]] = set()
        for hunk in self.hunks:
            for line in hunk.lines:
                if line.kind is not LineKind.ADD and line.old_no is not None:
                    anchors.add((Side.LEFT, line.old_no))
                if line.kind is not LineKind.DEL and line.new_no is not None:
                    anchors.add((Side.RIGHT, line.new_no))
        self.line_threads = {}
        self.loose_threads = []
        for thread in threads:
            if thread.is_file_level:
                continue
            key = (thread.side, thread.line) if thread.line is not None else None
            if key is not None and key in anchors:
                self.line_threads.setdefault(key, []).append(thread)  # type: ignore[arg-type]
            else:
                self.loose_threads.append(thread)

    def threads_at(self, line: DiffLine) -> list[ReviewThread]:
        found: list[ReviewThread] = []
        if line.expanded:
            return found
        if line.kind is not LineKind.ADD and line.old_no is not None:
            found += self.line_threads.get((Side.LEFT, line.old_no), [])
        if line.kind is not LineKind.DEL and line.new_no is not None:
            found += self.line_threads.get((Side.RIGHT, line.new_no), [])
        return found

    @property
    def unresolved_count(self) -> int:
        return sum(1 for t in self.threads if not t.is_resolved and not t.is_pending)

    @property
    def pending_count(self) -> int:
        return sum(1 for t in self.threads for c in t.comments if c.is_pending)

    # -- highlighting --------------------------------------------------------------

    def ensure_highlight(self, highlighter: Highlighter) -> None:
        key = (highlighter.style_name, self.new_lines is not None)
        if self._hl_key == key:
            return
        new_numbers: list[int] = []
        new_texts: list[str] = []
        old_numbers: list[int] = []
        old_texts: list[str] = []
        if self.new_lines is not None:
            new_numbers = list(range(1, len(self.new_lines) + 1))
            new_texts = [display_text(t) for t in self.new_lines]
        for hunk in self.hunks:
            for line in hunk.lines:
                if self.new_lines is None and line.kind is not LineKind.DEL:
                    assert line.new_no is not None
                    new_numbers.append(line.new_no)
                    new_texts.append(display_text(line.text))
                if line.kind is not LineKind.ADD:
                    assert line.old_no is not None
                    old_numbers.append(line.old_no)
                    old_texts.append(display_text(line.text))
        self.hl_new = dict(
            zip(new_numbers, highlighter.highlight(new_texts, self.path), strict=False)
        )
        self.hl_old = dict(
            zip(old_numbers, highlighter.highlight(old_texts, self.path), strict=False)
        )
        self._hl_key = key

    def highlighted(self, line: DiffLine) -> Text | None:
        if line.kind is LineKind.DEL:
            return self.hl_old.get(line.old_no or 0)
        return self.hl_new.get(line.new_no or 0) or self.hl_old.get(line.old_no or 0)

    def original_lines(self, thread: ReviewThread) -> list[str] | None:
        """The lines a thread is attached to (for rendering suggested changes)."""
        if thread.line is None:
            return None
        start = thread.start_line or thread.line
        found: dict[int, str] = {}
        for hunk in self.hunks:
            for line in hunk.lines:
                if thread.side is Side.LEFT and line.kind is not LineKind.ADD:
                    number = line.old_no
                elif thread.side is Side.RIGHT and line.kind is not LineKind.DEL:
                    number = line.new_no
                else:
                    continue
                if number is not None and start <= number <= thread.line:
                    found[number] = line.text
        return [found[n] for n in sorted(found)] or None


@dataclass(slots=True)
class Geometry:
    """Column layout of the diff view for a given width."""

    width: int
    split: bool
    nw: int  # width of a line-number column

    @property
    def unified_gutter(self) -> int:
        return 1 + self.nw + 1 + self.nw + 1 + 2  # marker old␣new␣sign␣

    @property
    def unified_code(self) -> int:
        return max(10, self.width - self.unified_gutter)

    @property
    def side_gutter(self) -> int:
        return self.nw + 3  # number␣sign␣

    @property
    def left_code(self) -> int:
        return max(8, (self.width - 2 - 2 * self.side_gutter) // 2)

    @property
    def right_code(self) -> int:
        return max(8, self.width - 2 - 2 * self.side_gutter - self.left_code)

    @property
    def right_start(self) -> int:
        """Column where the right half (after the divider) begins."""
        return 1 + self.side_gutter + self.left_code + 1

    def thread_box(self, side: Side | None) -> tuple[int, int]:
        """(indent, width) of a thread box."""
        if not self.split:
            indent = min(self.unified_gutter, max(0, self.width - 40))
            return indent, self.width - indent
        if side is Side.LEFT:
            indent = 1 + self.side_gutter
            width = self.left_code
        else:
            indent = self.right_start + self.side_gutter
            width = self.right_code
        if width < 36:  # too cramped: use the full width
            return 1, self.width - 1
        return indent, width


MAX_WRAP_ROWS = 12  # very long lines (minified code) are cut off after this many rows


def wraps(line: DiffLine, width: int) -> int:
    if len(line.text) > width * MAX_WRAP_ROWS * 2:
        return MAX_WRAP_ROWS
    length = cell_len(display_text(line.text))
    if length <= width:
        return 1
    return min(MAX_WRAP_ROWS, -(-length // width))


ThreadHeight = Callable[[FileSection, ReviewThread, Side | None], int]


def build_rows(section: FileSection, geo: Geometry, thread_height: ThreadHeight) -> list[Row]:
    """Flatten one file into display rows."""
    rows: list[Row] = [Row(RowKind.FILE, section)]
    if section.collapsed:
        rows.append(Row(RowKind.SPACER, section))
        return rows

    def add_threads(threads: list[ReviewThread], side: Side | None = None) -> None:
        for thread in threads:
            for index in range(thread_height(section, thread, side)):
                rows.append(Row(RowKind.THREAD, section, thread=thread, tline=index, side=side))

    add_threads(section.file_threads)
    add_threads(section.loose_threads)

    file = section.file
    if not section.hunks:
        if file.status == FileStatus.UNCHANGED:
            text = (
                "No longer changed by this pull request: these comments are on an earlier version"
            )
        elif file.patch is None and file.status != FileStatus.RENAMED:
            text = (
                "Binary file not shown"
                if file.additions + file.deletions == 0
                else ("Diff too large to show here")
            )
        elif file.status == FileStatus.RENAMED:
            text = "File renamed without changes"
        else:
            text = "No changes to show"
        rows.append(Row(RowKind.NOTE, section, text=text))
        rows.append(Row(RowKind.SPACER, section))
        return rows

    def add_lines(lines: list[DiffLine]) -> None:
        if geo.split:
            for left, right in split_pairs(lines):
                count = max(
                    wraps(left, geo.left_code) if left else 1,
                    wraps(right, geo.right_code) if right else 1,
                )
                for w in range(count):
                    rows.append(Row(RowKind.SPLIT, section, line=left, right=right, wrap=w))
                if (
                    left is not None
                    and not left.expanded
                    and left.old_no is not None
                    and left.kind is not LineKind.ADD
                ):
                    add_threads(section.line_threads.get((Side.LEFT, left.old_no), []), Side.LEFT)
                if (
                    right is not None
                    and not right.expanded
                    and right.new_no is not None
                    and right.kind is not LineKind.DEL
                ):
                    add_threads(
                        section.line_threads.get((Side.RIGHT, right.new_no), []), Side.RIGHT
                    )
        else:
            for line in lines:
                for w in range(wraps(line, geo.unified_code)):
                    rows.append(Row(RowKind.LINE, section, line=line, wrap=w))
                threads = section.threads_at(line)
                if threads:
                    add_threads(threads)

    gaps = section.gaps()
    for index, hunk in enumerate(section.hunks):
        gap = gaps[index]
        if gap is not None:
            if index in section.expanded_gaps and section.new_lines is not None:
                add_lines(section.gap_lines(index))
            else:
                rows.append(Row(RowKind.GAP, section, gap=index, hunk=hunk))
        add_lines(hunk.lines)
    trailing = len(section.hunks)
    gap = gaps[trailing]
    if gap is not None and gap.count:
        if trailing in section.expanded_gaps:
            add_lines(section.gap_lines(trailing))
        else:
            rows.append(Row(RowKind.GAP, section, gap=trailing))
    rows.append(Row(RowKind.SPACER, section))
    return rows
