from revv.diff import (
    LineKind,
    change_blocks,
    compute_gaps,
    make_patch,
    parse_patch,
    split_lines,
    word_diff,
)
from revv.models import Side
from revv.ui.diffmodel import split_pairs

PATCH = """\
@@ -1,4 +1,5 @@ header
 one
-two
+TWO
+two and a half
 three
 four
@@ -10,3 +11,2 @@ def later():
 ten
-eleven
 twelve
\\ No newline at end of file"""


def test_parse_patch_numbers_lines():
    hunks = parse_patch(PATCH)
    assert len(hunks) == 2
    first, second = hunks
    assert first.section == "header"
    assert [(ln.kind, ln.old_no, ln.new_no, ln.text) for ln in first.lines] == [
        (LineKind.CONTEXT, 1, 1, "one"),
        (LineKind.DEL, 2, None, "two"),
        (LineKind.ADD, None, 2, "TWO"),
        (LineKind.ADD, None, 3, "two and a half"),
        (LineKind.CONTEXT, 3, 4, "three"),
        (LineKind.CONTEXT, 4, 5, "four"),
    ]
    assert second.lines[-1].no_newline
    assert second.lines[1].anchor == (Side.LEFT, 11)
    assert first.lines[2].anchor == (Side.RIGHT, 2)


def test_parse_patch_ignores_trailing_newline_and_crlf():
    hunks = parse_patch("@@ -1 +1 @@\n-a\r\n+b\r\n")
    assert [(ln.kind, ln.text) for ln in hunks[0].lines] == [
        (LineKind.DEL, "a"),
        (LineKind.ADD, "b"),
    ]


def test_parse_patch_keeps_blank_context_lines():
    hunks = parse_patch("@@ -1,3 +1,3 @@\n a\n\n-b\n+c")
    kinds = [ln.kind for ln in hunks[0].lines]
    assert kinds == [LineKind.CONTEXT, LineKind.CONTEXT, LineKind.DEL, LineKind.ADD]
    assert hunks[0].lines[1].new_no == 2


def test_gaps_between_hunks():
    hunks = parse_patch(PATCH)
    gaps = compute_gaps(hunks, new_total=20)
    assert gaps[0] is None  # first hunk starts at line 1
    middle = gaps[1]
    assert middle is not None
    assert (middle.old_start, middle.new_start, middle.count) == (5, 6, 5)
    trailing = gaps[2]
    assert trailing is not None and (trailing.new_start, trailing.count) == (13, 8)


def test_gap_lines_are_expanded_context():
    hunks = parse_patch(PATCH)
    gap = compute_gaps(hunks, new_total=12)[1]
    assert gap is not None
    new_lines = [f"line {n}" for n in range(1, 13)]
    lines = gap.lines(new_lines)
    assert [(ln.old_no, ln.new_no, ln.text) for ln in lines][:2] == [
        (5, 6, "line 6"),
        (6, 7, "line 7"),
    ]
    assert all(ln.expanded and ln.kind is LineKind.CONTEXT for ln in lines)


def test_unknown_trailing_gap():
    hunks = parse_patch(PATCH)
    trailing = compute_gaps(hunks)[-1]
    assert trailing is not None and trailing.count is None


def test_new_file_patch_has_no_leading_gap():
    patch = make_patch("", "a\nb\n")
    hunks = parse_patch(patch)
    assert hunks[0].new_first == 1
    assert compute_gaps(hunks, 2) == [None, None]


def test_change_blocks_and_split_pairs():
    hunks = parse_patch(PATCH)
    blocks = change_blocks(hunks[0].lines)
    assert len(blocks) == 1
    dels, adds = blocks[0]
    assert [d.text for d in dels] == ["two"]
    assert [a.text for a in adds] == ["TWO", "two and a half"]
    pairs = split_pairs(hunks[0].lines)
    assert [
        (left.text if left else None, right.text if right else None) for left, right in pairs
    ] == [
        ("one", "one"),
        ("two", "TWO"),
        (None, "two and a half"),
        ("three", "three"),
        ("four", "four"),
    ]


def test_word_diff_marks_changed_words():
    result = word_diff('    version = "0.4.2"', '    version = "0.5.0"')
    assert result is not None
    old, new = result
    assert old and new
    assert all(end > start for start, end in old + new)


def test_word_diff_skips_unrelated_lines():
    assert word_diff("        while True:", "        with attempt:") is None
    assert word_diff("same", "same") is None


def test_make_patch_round_trip():
    old = "a\nb\nc\nd\ne\nf\ng\nh\n"
    new = "a\nb\nC\nd\ne\nf\ng\nh\ni\n"
    hunks = parse_patch(make_patch(old, new))
    added = [ln.text for h in hunks for ln in h.lines if ln.kind is LineKind.ADD]
    removed = [ln.text for h in hunks for ln in h.lines if ln.kind is LineKind.DEL]
    assert added == ["C", "i"]
    assert removed == ["c"]


def test_split_lines():
    assert split_lines("a\r\nb\n") == ["a", "b"]
    assert split_lines("") == []
