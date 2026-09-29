"""Unified diff parsing plus the bits of diff intelligence the viewer needs.

GitHub gives us a per-file unified diff ("patch") made of hunks. From that we build
`DiffLine`s carrying both old and new line numbers, work out which unchanged regions
("gaps") sit between hunks so they can be expanded later, and compute word-level
emphasis for lines that were modified rather than wholesale replaced.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum

from revv.models import Side


class LineKind(Enum):
    CONTEXT = " "
    ADD = "+"
    DEL = "-"


@dataclass(slots=True)
class DiffLine:
    kind: LineKind
    text: str
    old_no: int | None
    new_no: int | None
    no_newline: bool = False
    expanded: bool = False  # revealed by expanding a gap: visible, but not commentable
    emph: tuple[tuple[int, int], ...] = ()  # character ranges that changed (word diff)

    @property
    def side(self) -> Side:
        return Side.LEFT if self.kind is LineKind.DEL else Side.RIGHT

    @property
    def anchor(self) -> tuple[Side, int]:
        """Where a comment on this line is anchored (as the GitHub API expects it)."""
        if self.kind is LineKind.DEL:
            assert self.old_no is not None
            return Side.LEFT, self.old_no
        assert self.new_no is not None
        return Side.RIGHT, self.new_no

    def number_on(self, side: Side) -> int | None:
        return self.old_no if side is Side.LEFT else self.new_no


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list[DiffLine] = field(default_factory=list)

    # With a zero count, a hunk's start is the line *after which* it applies.
    @property
    def old_first(self) -> int:
        return self.old_start if self.old_count else self.old_start + 1

    @property
    def new_first(self) -> int:
        return self.new_start if self.new_count else self.new_start + 1

    @property
    def old_next(self) -> int:
        return self.old_first + self.old_count

    @property
    def new_next(self) -> int:
        return self.new_first + self.new_count

    @property
    def header(self) -> str:
        section = f" {self.section}" if self.section else ""
        return (
            f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@{section}"
        )


@dataclass(slots=True)
class Gap:
    """A run of unchanged lines hidden between (or around) hunks."""

    old_start: int
    new_start: int
    count: int | None  # None: runs to the end of the file, length not yet known

    def lines(self, new_lines: list[str]) -> list[DiffLine]:
        """Materialize the gap from the full text of the new file."""
        end = len(new_lines) + 1 if self.count is None else self.new_start + self.count
        end = min(end, len(new_lines) + 1)
        return [
            DiffLine(
                LineKind.CONTEXT,
                new_lines[new_no - 1],
                self.old_start + (new_no - self.new_start),
                new_no,
                expanded=True,
            )
            for new_no in range(self.new_start, end)
        ]


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def parse_patch(patch: str) -> list[Hunk]:
    """Parse the hunks of a single-file unified diff (headers before the first @@ are ignored)."""
    hunks: list[Hunk] = []
    hunk: Hunk | None = None
    old_no = new_no = 0
    old_left = new_left = 0
    for raw in patch.split("\n"):
        if raw.startswith("@@"):
            match = HUNK_RE.match(raw)
            if match is None:
                continue
            o_start, o_count, n_start, n_count, section = match.groups()
            hunk = Hunk(
                int(o_start),
                1 if o_count is None else int(o_count),
                int(n_start),
                1 if n_count is None else int(n_count),
                section.strip(),
            )
            hunks.append(hunk)
            old_no, new_no = hunk.old_first, hunk.new_first
            old_left, new_left = hunk.old_count, hunk.new_count
            continue
        if hunk is None:
            continue
        if raw.startswith("\\"):
            if hunk.lines:
                hunk.lines[-1].no_newline = True
            continue
        if old_left <= 0 and new_left <= 0:
            continue  # trailing junk (e.g. the empty string after a final newline)
        tag, text = raw[:1], raw[1:].removesuffix("\r")
        if tag == "+":
            hunk.lines.append(DiffLine(LineKind.ADD, text, None, new_no))
            new_no += 1
            new_left -= 1
        elif tag == "-":
            hunk.lines.append(DiffLine(LineKind.DEL, text, old_no, None))
            old_no += 1
            old_left -= 1
        else:  # " " or a bare empty line (some tools strip the trailing space)
            hunk.lines.append(DiffLine(LineKind.CONTEXT, text, old_no, new_no))
            old_no += 1
            new_no += 1
            old_left -= 1
            new_left -= 1
    return hunks


def compute_gaps(hunks: list[Hunk], new_total: int | None = None) -> list[Gap | None]:
    """The gap before each hunk, plus one trailing gap (so len(result) == len(hunks) + 1).

    Entries are None where nothing is hidden. The trailing gap has an unknown length until
    the total line count of the new file is known.
    """
    gaps: list[Gap | None] = []
    old_next = new_next = 1
    for hunk in hunks:
        count = hunk.new_first - new_next
        gaps.append(Gap(old_next, new_next, count) if count > 0 else None)
        old_next, new_next = hunk.old_next, hunk.new_next
    if not hunks:
        gaps.append(None)
    elif new_total is None:
        gaps.append(Gap(old_next, new_next, None))
    else:
        count = new_total - new_next + 1
        gaps.append(Gap(old_next, new_next, count) if count > 0 else None)
    return gaps


def change_blocks(lines: list[DiffLine]) -> list[tuple[list[DiffLine], list[DiffLine]]]:
    """Group consecutive changes into (deleted, added) blocks, in order."""
    blocks: list[tuple[list[DiffLine], list[DiffLine]]] = []
    dels: list[DiffLine] = []
    adds: list[DiffLine] = []
    for line in lines:
        if line.kind is LineKind.CONTEXT:
            if dels or adds:
                blocks.append((dels, adds))
                dels, adds = [], []
        elif line.kind is LineKind.DEL:
            if adds:  # a deletion after additions starts a new block
                blocks.append((dels, adds))
                dels, adds = [], []
            dels.append(line)
        else:
            adds.append(line)
    if dels or adds:
        blocks.append((dels, adds))
    return blocks


_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")
_MIN_SIMILARITY = 0.35
_MAX_EMPH_LINE = 1000


def _token_offsets(tokens: list[str]) -> list[int]:
    offsets = [0]
    for token in tokens:
        offsets.append(offsets[-1] + len(token))
    return offsets


def _merge(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def word_diff(
    old: str, new: str
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]] | None:
    """Character ranges that differ between two lines, or None if they are too dissimilar.

    Similarity only counts non-whitespace characters, so two unrelated lines that merely
    share indentation and punctuation are not treated as edits of each other.
    """
    if old == new or len(old) > _MAX_EMPH_LINE or len(new) > _MAX_EMPH_LINE:
        return None
    a, b = _TOKEN_RE.findall(old), _TOKEN_RE.findall(new)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    opcodes = matcher.get_opcodes()
    same = sum(
        len(token.strip()) for op, i1, i2, _, _ in opcodes if op == "equal" for token in a[i1:i2]
    )
    total = max(len("".join(old.split())), len("".join(new.split())), 1)
    if same / total < _MIN_SIMILARITY:
        return None
    a_off, b_off = _token_offsets(a), _token_offsets(b)
    a_ranges: list[tuple[int, int]] = []
    b_ranges: list[tuple[int, int]] = []
    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            continue
        if i2 > i1:
            a_ranges.append((a_off[i1], a_off[i2]))
        if j2 > j1:
            b_ranges.append((b_off[j1], b_off[j2]))
    return _merge(a_ranges), _merge(b_ranges)


def annotate_word_diff(hunks: list[Hunk]) -> None:
    """Fill in `DiffLine.emph` for deleted/added line pairs within each change block."""
    for hunk in hunks:
        for dels, adds in change_blocks(hunk.lines):
            for old, new in zip(dels, adds, strict=False):
                result = word_diff(old.text, new.text)
                if result is not None:
                    old.emph, new.emph = result


def make_patch(old_text: str, new_text: str, context: int = 3) -> str:
    """Produce a unified diff (hunks only) locally, for files GitHub didn't send a patch for."""
    diff = difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(), lineterm="", n=context
    )
    return "\n".join(line for line in diff if not line.startswith(("---", "+++")))


def split_lines(text: str) -> list[str]:
    """Split file content into lines the way diff line numbers count them."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]
