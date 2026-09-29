"""End-to-end tests: drive the app with keys against the in-memory demo backend."""

from __future__ import annotations

from collections.abc import Callable

from textual.pilot import Pilot
from textual.widgets import TextArea

from revv.demo import DEMO_REF, DemoBackend
from revv.diff import LineKind
from revv.models import ReviewEvent, Side
from revv.ui.app import RevvApp
from revv.ui.conversation import Card, ConversationView
from revv.ui.diffmodel import Row, RowKind
from revv.ui.diffview import DiffView
from revv.ui.editor import CommentEditor
from revv.ui.inbox import InboxScreen
from revv.ui.review import ReviewScreen

SIZE = (140, 45)


async def loaded(pilot: Pilot, tab: str = "files") -> ReviewScreen:
    """Wait for the review screen, then show the files tab (most tests are about the diff)."""
    for _ in range(100):
        screen = pilot.app.screen
        if isinstance(screen, ReviewScreen) and screen.session.loaded:
            await pilot.pause()
            if tab == "files" and screen.tab != "files":
                await pilot.press("1")
                await pilot.pause()
            if screen.diff.rows or tab != "files":
                return screen
        await pilot.pause(0.02)
    raise AssertionError("review screen never loaded")


async def type_text(pilot: Pilot, text: str) -> None:
    keys = {" ": "space", "\n": "enter", "`": "grave_accent", ".": "full_stop", ",": "comma"}
    for char in text:
        await pilot.press(keys.get(char, char))


def goto(diff: DiffView, predicate: Callable[[Row], bool]) -> int:
    for index, row in enumerate(diff.rows):
        if predicate(row):
            diff.set_cursor(index)
            return index
    raise AssertionError("no matching row")


def code_line(path: str, text: str) -> Callable[[Row], bool]:
    def match(row: Row) -> bool:
        return (
            row.kind is RowKind.LINE
            and row.wrap == 0
            and row.section.path == path
            and row.line is not None
            and text in row.line.text
        )

    return match


async def test_loads_pull_request(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        assert screen.pr.title.startswith("Retry failed requests")
        assert len(screen.diff.sections) == 11
        paths = [s.path for s in screen.diff.sections]
        assert paths[0] == "docs/architecture.png"  # folders first, like the tree
        assert screen.focused is screen.diff


async def test_navigation_keys(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        diff = screen.diff
        await pilot.press("right_square_bracket")
        assert diff.current_section is not None
        assert diff.current_section.path == "src/netkit/client.py"
        assert diff.current_row is not None and diff.current_row.kind is RowKind.FILE
        await pilot.press("right_curly_bracket")
        row = diff.current_row
        assert row is not None and row.is_code
        assert row.line is not None and row.line.kind is not LineKind.CONTEXT
        await pilot.press("n")
        assert diff.current_row is not None and diff.current_row.kind is RowKind.THREAD
        await pilot.press("G")
        assert diff.cursor == len(diff.rows) - 1 or not diff.rows[-1].selectable
        await pilot.press("g")
        assert diff.cursor == 0


async def test_comment_on_line_adds_to_pending_review(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        goto(screen.diff, code_line("src/netkit/client.py", "def delete(self, path: str)"))
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CommentEditor)
        await type_text(pilot, "Needs a test")
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ReviewScreen)
        thread = next(t for t in screen.pr.threads if t.root and t.root.body == "Needs a test")
        assert thread.is_pending
        assert thread.side is Side.RIGHT and thread.path == "src/netkit/client.py"
        assert any(r.thread is thread for r in screen.diff.rows)
        stored = next(t for t in backend._pr.threads if t.id == thread.id)
        assert stored.comments[0].is_pending


async def test_range_comment_and_suggestion(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        goto(screen.diff, code_line("src/netkit/retry.py", "attempts: int = 4"))
        await pilot.press("V", "j", "j")
        selection = screen.diff.selection()
        assert selection is not None and len(selection.lines) == 3
        await pilot.press("s")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, CommentEditor)
        text = editor.query_one(TextArea).text
        assert text.startswith("```suggestion\n    attempts: int = 4\n")
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        thread = screen.pr.threads[-1]
        assert thread.start_line is not None and thread.line == thread.start_line + 2
        assert "```suggestion" in thread.comments[0].body
        assert screen.diff.sel_anchor is None


async def test_expanded_context_lines_cannot_be_commented(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        diff = screen.diff
        index = goto(
            diff, lambda r: r.kind is RowKind.GAP and r.section.path == "src/netkit/client.py"
        )
        before = len(diff.rows)
        await pilot.press("enter")
        await pilot.pause()
        assert len(diff.rows) > before
        expanded = diff.rows[index]
        assert expanded.line is not None and expanded.line.expanded
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)  # no editor for expanded lines


async def test_reply_and_resolve_thread(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        goto(screen.diff, code_line("src/netkit/retry.py", "RETRYABLE = "))
        thread = screen.diff.active_thread
        assert thread is not None and not thread.is_resolved
        await pilot.press("r")
        await pilot.pause()
        await type_text(pilot, "Good point")
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert thread.comments[-1].body == "Good point"
        assert thread.comments[-1].is_pending
        await pilot.press("x")
        await pilot.pause(0.1)
        assert thread.is_resolved
        assert next(t for t in backend._pr.threads if t.id == thread.id).is_resolved
        await pilot.press("x")
        await pilot.pause(0.1)
        assert not thread.is_resolved


async def test_edit_and_delete_own_comment(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        pending = next(t for t in screen.pr.threads if t.is_pending)
        screen.diff.jump_to_thread(pending)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, CommentEditor)
        editor.query_one(TextArea).load_text("Edited!")
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert pending.comments[0].body == "Edited!"
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause(0.1)
        assert pending not in screen.pr.threads


async def test_mark_viewed_moves_on(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        diff = screen.diff
        first = diff.current_section
        assert first is not None and not first.file.is_viewed
        await pilot.press("v")
        await pilot.pause(0.1)
        assert first.file.is_viewed and first.collapsed
        assert backend._pr.file(first.path).is_viewed  # type: ignore[union-attr]
        assert diff.current_section is not first
        assert diff.current_section is not None and not diff.current_section.file.is_viewed


async def test_submit_review_publishes_pending_comments(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        assert screen.pr.pending_comment_count == 1
        await pilot.press("A")
        await pilot.pause()
        await type_text(pilot, "Ship it")
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)
        assert screen.pr.pending_review is None
        review = next(r for r in backend._pr.reviews if r.author == "you" and r.body == "Ship it")
        assert review.state == "APPROVED"
        assert all(not c.is_pending for t in backend._pr.threads for c in t.comments)


async def test_discard_review_then_comment_immediately(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("S")
        await pilot.pause()
        await pilot.click("#discard")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause(0.1)
        assert screen.pr.pending_review is None
        assert not any(t.is_pending for t in screen.pr.threads)
        goto(screen.diff, code_line("src/netkit/client.py", "def delete(self, path: str)"))
        await pilot.press("c")
        await pilot.pause()
        await type_text(pilot, "Right now")
        await pilot.press("ctrl+g")
        await pilot.pause(0.2)
        thread = screen.pr.threads[-1]
        assert thread.root is not None and thread.root.body == "Right now"
        assert not thread.is_pending
        assert screen.pr.pending_review is None
        assert any(r.state == "COMMENTED" and r.author == "you" for r in backend._pr.reviews)


async def test_general_comment_and_resolving_it(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("C")
        await pilot.pause()
        await type_text(pilot, "LGTM overall")
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert screen.pr.comments[-1].body == "LGTM overall"
        await pilot.press("2")
        await pilot.pause()
        conversation = screen.query_one(ConversationView)
        target = next(
            card
            for card in conversation.query(Card)
            if card.item.kind == "comment" and getattr(card.item.obj, "author", "") == "mona"
        )
        target.focus()
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause(0.1)
        comment = target.item.obj
        assert comment.is_resolved  # type: ignore[union-attr]
        stored = next(c for c in backend._pr.comments if c.id == comment.id)  # type: ignore[union-attr]
        assert stored.is_minimized and stored.minimized_reason == "RESOLVED"


async def test_conversation_jumps_to_thread(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("2")
        await pilot.pause()
        cards = [c for c in screen.query(Card) if c.item.kind == "thread"]
        cards[0].focus()
        await pilot.press("enter")
        await pilot.pause()
        assert screen.tab == "files"
        row = screen.diff.current_row
        assert row is not None and row.thread is cards[0].item.obj


async def test_split_view_toggle(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("vertical_line")
        await pilot.pause()
        assert screen.diff.split
        assert any(r.kind is RowKind.SPLIT for r in screen.diff.rows)
        goto(
            screen.diff,
            lambda r: (
                r.kind is RowKind.SPLIT and r.line is not None and r.line.kind is LineKind.DEL
            ),
        )
        await pilot.press("h")
        selection = screen.diff.selection()
        assert selection is not None and selection.end[0] is Side.LEFT


async def test_file_finder(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("f")
        await pilot.pause()
        await type_text(pilot, "badge.tsx")
        await pilot.pause(0.3)
        await pilot.press("enter")
        await pilot.pause()
        assert screen.diff.current_section is not None
        assert screen.diff.current_section.path.endswith("StatusBadge.tsx")


async def test_inbox_lists_requests_and_opens_by_number(backend: DemoBackend) -> None:
    app = RevvApp(backend, repo=DEMO_REF.repo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        inbox = app.screen
        assert isinstance(inbox, InboxScreen)
        requested = next(s for s in inbox.sections if s.key == "requested")
        assert sorted(i.ref.number for i in requested.items) == [17, 40, 42, 44, 46]
        team = next(i for i in requested.items if i.ref.number == 17)
        assert team.requested_teams == ["acme/python-reviewers"] and not team.requested_directly
        await pilot.press("4", "2", "enter")
        screen = await loaded(pilot)
        assert screen.session.ref.number == 42
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, InboxScreen)


async def test_help_screen(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        await loaded(pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        assert type(app.screen).__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)


async def test_submit_request_changes_requires_feedback(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("S")
        await pilot.pause()
        dialog = app.screen
        dialog.query_one("#event").query("RadioButton")[2].value = True  # Request changes
        await pilot.pause()
        assert dialog.event is ReviewEvent.REQUEST_CHANGES  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)
        # there is one pending comment, so an empty body is fine
        assert screen.pr.pending_review is None


async def test_test_files_start_hidden_and_t_toggles(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        diff = screen.diff
        tests = {s.path for s in diff.sections if "test" in s.kinds}
        assert tests == {"tests/test_retry.py", "web/src/components/StatusBadge.test.tsx"}
        # hidden by default, without touching GitHub
        assert not tests & {s.path for s in diff.visible_sections}
        assert not {r.section.path for r in diff.rows} & tests
        assert tests.isdisjoint(screen.file_tree._path_nodes)
        assert screen.query_one("#hidden-note").display
        assert not any(backend._pr.file(path).is_viewed for path in tests)  # type: ignore[union-attr]
        await pilot.press("T")  # show them
        await pilot.pause()
        assert tests <= {s.path for s in diff.visible_sections}
        await pilot.press("T")  # hide them again, marking them as viewed
        await pilot.pause(0.2)
        assert not tests & {s.path for s in diff.visible_sections}
        assert all(backend._pr.file(path).is_viewed for path in tests)  # type: ignore[union-attr]


async def test_generated_files_start_hidden(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        generated = {s.path for s in screen.diff.sections if "generated" in s.kinds}
        assert generated == {"uv.lock"}
        assert "uv.lock" not in {s.path for s in screen.diff.visible_sections}
        await pilot.press("X")
        await pilot.pause()
        assert "uv.lock" in {s.path for s in screen.diff.visible_sections}
        await pilot.press("X")
        await pilot.pause(0.2)
        assert backend._pr.file("uv.lock").is_viewed  # type: ignore[union-attr]
        # jumping to a hidden file (e.g. from the file finder) shows it again
        section = next(s for s in screen.diff.sections if s.path == "uv.lock")
        screen.go_to_section(section)
        await pilot.pause()
        assert screen.diff.current_section is section
        assert "uv.lock" in screen.file_tree._path_nodes


async def test_viewed_puts_next_file_header_at_top(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        diff = screen.diff
        await pilot.press("v")
        await pilot.pause(0.1)
        row = diff.current_row
        assert row is not None and row.kind is RowKind.FILE
        assert round(diff.scroll_offset.y) == diff.cursor  # the header is the top line


async def test_progress_is_weighted_by_changed_lines(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        header = screen.header
        text = header._progress(screen.pr, header_palette(app)).plain
        assert "%" in text and "files" in text
        # hidden (test/generated) files don't count
        visible = [f for f in screen.pr.files if f.path not in header.hidden]
        assert f"/{len(visible)} files" in text


def header_palette(app: RevvApp):
    from revv.ui.palette import Palette

    return Palette.from_app(app)


async def test_notices_changes_on_github_and_refreshes(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        banner = screen.query_one("#banner")
        assert not banner.display
        screen.check_for_changes()
        await pilot.pause(0.1)
        assert not banner.display  # nothing changed yet
        # someone replies on GitHub
        thread = backend._pr.threads[0]
        thread.comments.append(backend._comment("mona", "Any update?", 0))
        screen.check_for_changes()
        await pilot.pause(0.1)
        assert banner.display and "1 new comment" in str(banner.render())
        await pilot.press("R")
        await pilot.pause(0.2)
        assert not banner.display
        local = next(t for t in screen.pr.threads if t.id == thread.id)
        assert local.comments[-1].body == "Any update?"


async def test_nicknames(app: RevvApp) -> None:
    from textual.widgets import Input

    from revv import config

    config.set_nicknames({"hubot": "Robot"})
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        thread = next(
            t
            for t in screen.pr.threads
            if t.root and t.root.author == "mona" and not t.is_resolved and not t.is_outdated
        )
        screen.diff.jump_to_thread(thread)
        await pilot.pause()
        await pilot.press("at")  # the people in the thread under the cursor
        await pilot.pause()
        dialog = app.screen
        names = sorted(str(f.name) for f in dialog.query(Input))
        assert names == sorted({c.author for c in thread.comments})
        field = next(f for f in dialog.query(Input) if f.name == "mona")
        field.value = "Mona Lisa"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert config.display_name("mona") == "Mona Lisa"
        # other nicknames are kept
        assert config.load_config()["nicknames"] == {"hubot": "Robot", "mona": "Mona Lisa"}
        rendered = screen.diff._render_thread(
            screen.diff.current_section,  # type: ignore[arg-type]
            thread,
            None,
            False,
        )
        assert "Mona Lisa" in rendered.strips[0].text
        # on the description card: the author and every reviewer
        await pilot.press("2")
        await pilot.pause()
        description = next(c for c in screen.query(Card) if c.item.kind == "description")
        description.focus()
        await pilot.press("at")
        await pilot.pause()
        names = [str(f.name) for f in app.screen.query(Input)]
        assert names[0] == screen.pr.author
        assert {"mona", "hubot", "you"} <= set(names)
        await pilot.press("escape")


async def test_inbox_nickname_is_for_the_highlighted_author(backend: DemoBackend) -> None:
    from textual.widgets import Input, OptionList

    app = RevvApp(backend, repo=DEMO_REF.repo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        options = app.screen.query_one(OptionList)
        options.highlighted = options.get_option_index("github.com/acme/netkit#42")
        await pilot.press("at")
        await pilot.pause()
        assert [str(f.name) for f in app.screen.query(Input)] == ["octocat"]
        await pilot.press("escape")


async def test_inbox_sorting_stacks_and_ignoring(backend: DemoBackend) -> None:
    from textual.widgets import OptionList

    from revv import config
    from revv.ui.inbox import StackHeader

    app = RevvApp(backend, repo=DEMO_REF.repo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        inbox = app.screen
        assert isinstance(inbox, InboxScreen)
        options = inbox.query_one(OptionList)

        def order() -> list[str]:
            items, _ = inbox._items()
            return [
                f"stack{r.item.stack_number}"
                if isinstance(r, StackHeader)
                else f"#{r.item.ref.number}"
                for r in inbox._arrange(items)
            ]

        # oldest (lowest number) first; a stack is folded into one row
        assert order() == ["#17", "#40", "#42", "stack3"]
        await pilot.press("s")
        assert order() == ["stack3", "#42", "#40", "#17"]
        assert config.load_config()["inbox_sort"] == "desc"
        await pilot.press("s")

        # enter on a stack unfolds it (it doesn't open a pull request) ...
        options.highlighted = options.get_option_index("stack:STACK_3")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, InboxScreen)
        assert order() == ["#17", "#40", "#42", "stack3", "#44", "#46"]
        # ... with the lowest pull request you haven't reviewed highlighted
        assert options.highlighted_option is not None
        assert options.highlighted_option.id == "github.com/acme/netkit#44"
        await pilot.press("left")  # fold it again
        await pilot.pause()
        assert order() == ["#17", "#40", "#42", "stack3"]
        assert options.highlighted_option.id == "stack:STACK_3"
        await pilot.press("right")  # arrows unfold too
        await pilot.pause()
        assert "#46" in order()
        await pilot.press("down", "enter")  # a member opens as usual
        await pilot.pause(0.2)
        assert isinstance(app.screen, ReviewScreen)
        assert app.screen.session.ref.number == 46
        await pilot.press("q")
        await pilot.pause(0.2)

        # ignore the highlighted pull request
        options.highlighted = options.get_option_index("github.com/acme/netkit#17")
        await pilot.press("i")
        await pilot.pause()
        assert "#17" not in order()
        assert "github.com/acme/netkit#17" in config.ignored_prs()
        await pilot.press("tab", "tab", "tab")  # to the Ignored tab
        await pilot.pause()
        assert inbox.current == "ignored" and order() == ["#17"]
        await pilot.press("i")
        await pilot.pause()
        assert "github.com/acme/netkit#17" not in config.ignored_prs()
        assert inbox.current == "requested"


async def test_search_changed_lines(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("slash")
        await pilot.pause()
        await type_text(pilot, "AssertionError")
        await pilot.pause(0.4)
        await pilot.press("enter")
        await pilot.pause()
        row = screen.diff.current_row
        assert row is not None and row.line is not None
        assert "AssertionError" in row.line.text
        assert row.section.path == "src/netkit/client.py"


async def test_search_reveals_hidden_files(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("slash")
        await pilot.pause()
        await type_text(pilot, "test_other_errors_are_not_retried")
        await pilot.pause(0.4)
        await pilot.press("enter")
        await pilot.pause()
        row = screen.diff.current_row
        assert row is not None and row.section.path == "tests/test_retry.py"
        assert "tests/test_retry.py" in screen.file_tree._path_nodes


async def test_changes_since_last_review(app: RevvApp, backend: DemoBackend) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("L")
        await pilot.pause(0.2)
        assert screen.since is not None
        paths = [s.path for s in screen.diff.sections]
        assert paths == ["src/netkit/client.py", "web/src/components/StatusBadge.tsx"]
        client = screen.diff.sections[0]
        added = [ln.text for h in client.hunks for ln in h.lines if ln.kind is LineKind.ADD]
        assert any("def delete" in text for text in added)
        banner = screen.query_one("#banner")
        assert banner.display and "since your last review" in str(banner.render())
        # comment on a new line: allowed, it's part of the PR's diff too
        goto(screen.diff, code_line("src/netkit/client.py", "def delete(self, path: str)"))
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CommentEditor)
        await type_text(pilot, "New since last time")
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)
        assert any(t.root and t.root.body == "New since last time" for t in screen.pr.threads)
        # marking viewed updates the pull request's own file
        screen.diff.jump_to_section(client)
        await pilot.press("v")
        await pilot.pause(0.2)
        assert screen.pr.file("src/netkit/client.py").is_viewed  # type: ignore[union-attr]
        assert backend._pr.file("src/netkit/client.py").is_viewed  # type: ignore[union-attr]
        await pilot.press("L")
        await pilot.pause(0.2)
        assert screen.since is None
        assert len(screen.diff.sections) == 11
        assert not banner.display


async def test_reactions(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        goto(screen.diff, code_line("src/netkit/retry.py", "RETRYABLE = "))
        await pilot.press("n")  # into the thread below the line
        comment = screen.diff.active_comment
        assert comment is not None
        await pilot.press("plus")
        await pilot.pause()
        await pilot.press("1")  # 👍
        await pilot.pause(0.1)
        thumbs = next(r for r in comment.reactions if r.content == "THUMBS_UP")
        assert thumbs.viewer_has_reacted and thumbs.count == 1
        await pilot.press("plus")
        await pilot.pause()
        await pilot.press("1")  # again: take it back
        await pilot.pause(0.1)
        assert not any(r.content == "THUMBS_UP" for r in comment.reactions)
        # the description can be reacted to from the conversation tab
        await pilot.press("2")
        await pilot.pause()
        description = next(c for c in screen.query(Card) if c.item.kind == "description")
        description.focus()
        await pilot.press("plus")
        await pilot.pause()
        await pilot.press("7")  # 🚀
        await pilot.pause(0.1)
        assert any(r.content == "ROCKET" and r.viewer_has_reacted for r in screen.pr.reactions)


async def test_sidebar_width_is_adjustable_and_remembered(app: RevvApp) -> None:
    from revv import config

    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        sidebar = screen.query_one("#sidebar")
        start = sidebar.outer_size.width
        await pilot.press("greater_than_sign", "greater_than_sign")
        await pilot.pause()
        assert sidebar.outer_size.width == start + 8
        assert config.load_config()["sidebar_width"] == start + 8
        await pilot.press("less_than_sign")
        await pilot.pause()
        assert config.load_config()["sidebar_width"] == start + 4
    again = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo)
    async with again.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        assert screen.query_one("#sidebar").outer_size.width == start + 4


async def test_opens_on_the_conversation_tab(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot, tab="conversation")
        await pilot.pause(0.1)
        assert screen.tab == "conversation"
        assert isinstance(app.focused, Card)
        await pilot.press("1")
        await pilot.pause()
        assert screen.tab == "files" and screen.diff.rows and app.focused is screen.diff
        # the diff opened at the first unviewed, visible file
        section = screen.diff.current_section
        assert section is not None and not section.file.is_viewed


async def test_thread_navigation_all_and_unresolved(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        diff = screen.diff
        diff.set_cursor(0)
        seen = []
        for _ in range(len(screen.pr.threads)):
            await pilot.press("n")
            await pilot.pause()
            row = diff.current_row
            assert row is not None and row.kind is RowKind.THREAD and row.thread is not None
            seen.append(row.thread.id)
        assert set(seen) == {t.id for t in screen.pr.threads}  # every thread, hidden files too
        assert "tests/test_retry.py" in {s.path for s in diff.visible_sections}
        diff.set_cursor(0)
        unresolved = []
        for _ in range(8):
            await pilot.press("u")
            await pilot.pause()
            row = diff.current_row
            assert row is not None and row.thread is not None
            assert not row.thread.is_resolved
            unresolved.append(row.thread.id)
        expected = {t.id for t in screen.pr.threads if not t.is_resolved}
        assert set(unresolved) == expected  # and it wraps around
        await pilot.press("U")
        await pilot.pause()
        assert diff.current_row is not None and not diff.current_row.thread.is_resolved  # type: ignore[union-attr]


async def test_inbox_scope_toggle(backend: DemoBackend) -> None:
    app = RevvApp(backend, repo=DEMO_REF.repo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        inbox = app.screen
        assert isinstance(inbox, InboxScreen)
        await pilot.press("a")  # all repositories
        await pilot.pause(0.3)
        assert inbox.all_repos and inbox.scope is None
        await pilot.press("a")  # and back
        await pilot.pause(0.3)
        assert not inbox.all_repos
        assert [s.key for s in inbox.sections] == ["requested", "reviewed", "mine"]


async def test_settings_screen(app: RevvApp) -> None:
    from textual.widgets import Select, Switch

    from revv import config
    from revv.ui.settings import SettingsScreen

    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        await pilot.press("comma")
        await pilot.pause(0.2)
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        settings.query_one("#panc", Switch).value = True
        settings.query_one("#hide-tests", Switch).value = False
        settings.query_one("#open-tab", Select).value = "files"
        await pilot.pause()
        saved = config.load_config()
        assert saved["panc"] is True
        assert saved["hide_by_default"] == ["generated"]
        assert saved["open_tab"] == "files"
        sidebar = settings.query_one("#sidebar")
        sidebar.value = "44"  # type: ignore[attr-defined]
        await pilot.pause()
        assert config.load_config()["sidebar_width"] == 44
        assert screen.query_one("#sidebar").outer_size.width == 44
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)


async def test_typing_a_comma_in_the_editor_stays_in_the_editor(app: RevvApp) -> None:
    async with app.run_test(size=SIZE) as pilot:
        screen = await loaded(pilot)
        goto(screen.diff, code_line("src/netkit/client.py", "def delete(self, path: str)"))
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("a", "comma", "b", "comma", "space", "c")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, CommentEditor)
        assert editor.query_one(TextArea).text == "a,b, c"
        await pilot.press("escape")
