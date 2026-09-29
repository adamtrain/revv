"""The review screen: header, file tree, diff, conversation, and every reviewing action."""

from __future__ import annotations

import asyncio
import hashlib
from functools import partial
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.command import CommandPalette, DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import ContentSwitcher, Static, Tree

from revv.classify import GitAttributes
from revv.diff import LineKind
from revv.models import Comment, PullRequest, Review, ReviewEvent, ReviewThread, Side
from revv.session import ReviewSession
from revv.ui.conversation import ConversationView, Item
from revv.ui.dialogs import ConfirmDialog, HelpScreen, SubmitResult, SubmitReviewDialog
from revv.ui.diffmodel import FileSection, RowKind
from revv.ui.diffview import DiffView, Selection
from revv.ui.editor import DRAFTS, CommentEditor, EditorResult
from revv.ui.filetree import FileTree, tree_order
from revv.ui.palette import Palette
from revv.ui.widgets import PRHeader, StatusBar

PREFETCH_LIMIT = 150  # fetch full file text up front for PRs with at most this many files


class FileFinder(Provider):
    """Fuzzy "go to file" for the command palette."""

    def _review_screen(self) -> ReviewScreen | None:
        screen = self.screen
        return screen if isinstance(screen, ReviewScreen) else None

    async def discover(self) -> Hits:
        screen = self._review_screen()
        if screen is None:
            return
        for section in screen.diff.sections:
            yield DiscoveryHit(
                section.path,
                partial(screen.go_to_section, section),
                help=self._help(section),
            )

    async def search(self, query: str) -> Hits:
        screen = self._review_screen()
        if screen is None:
            return
        matcher = self.matcher(query)
        for section in screen.diff.sections:
            score = matcher.match(section.path)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(section.path),
                    partial(screen.go_to_section, section),
                    help=self._help(section),
                )

    @staticmethod
    def _help(section: FileSection) -> str:
        file = section.file
        parts = [f"{file.status.value}", f"+{file.additions} −{file.deletions}"]
        if section.unresolved_count:
            parts.append(f"{section.unresolved_count} unresolved")
        if file.is_viewed:
            parts.append("viewed")
        return " · ".join(parts)


def diff_anchor(path: str) -> str:
    return "diff-" + hashlib.sha256(path.encode()).hexdigest()


class ReviewScreen(Screen):
    DEFAULT_CSS = """
    ReviewScreen { layout: vertical; }
    ReviewScreen #tabs { height: 1fr; }
    ReviewScreen #files { height: 1fr; }
    ReviewScreen #sidebar { width: 34; max-width: 40%; border-right: vkey $panel; }
    ReviewScreen #sidebar:focus-within { border-right: vkey $accent; }
    ReviewScreen FileTree { height: 1fr; }
    ReviewScreen #hidden-note {
        height: auto; background: $surface; color: $text-muted; padding: 0 1;
        border-top: solid $panel; display: none;
    }
    ReviewScreen DiffView { width: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "back", "Back", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("1", "switch_tab('files')", "Files", show=False),
        Binding("2", "switch_tab('conversation')", "Conversation", show=False),
        Binding("t", "toggle_tree", "Tree", show=False),
        Binding("slash,ctrl+k", "find_file", "Go to file", show=False),
        Binding("c", "comment", "Comment", show=False),
        Binding("s", "suggest", "Suggest", show=False),
        Binding("r", "reply", "Reply", show=False),
        Binding("x", "resolve", "Resolve", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("v", "viewed", "Viewed", show=False),
        Binding("S", "submit", "Submit review", show=False),
        Binding("A", "submit('APPROVE')", "Approve", show=False),
        Binding("C", "general_comment", "Comment on PR", show=False),
        Binding("R", "refresh", "Refresh", show=False),
        Binding("o", "open_browser", "Open in browser", show=False),
        Binding("y", "copy_location", "Copy location", show=False),
        Binding("T", "hide_kind('test')", "Hide tests", show=False),
        Binding("X", "hide_kind('generated')", "Hide generated", show=False),
    ]

    def __init__(self, session: ReviewSession, *, from_inbox: bool = False) -> None:
        super().__init__()
        self.session = session
        self.from_inbox = from_inbox
        self._busy = 0

    # -- composition ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield PRHeader(id="header")
        with ContentSwitcher(initial="files", id="tabs"):
            with Horizontal(id="files"):
                with Vertical(id="sidebar"):
                    yield FileTree(id="tree")
                    yield Static(id="hidden-note")
                yield DiffView(id="diff")
            yield ConversationView(id="conversation")
        yield StatusBar(id="status")

    @property
    def diff(self) -> DiffView:
        return self.query_one(DiffView)

    @property
    def file_tree(self) -> FileTree:
        return self.query_one(FileTree)

    @property
    def conversation(self) -> ConversationView:
        return self.query_one(ConversationView)

    @property
    def header(self) -> PRHeader:
        return self.query_one(PRHeader)

    @property
    def pr(self) -> PullRequest:
        return self.session.pr

    @property
    def tab(self) -> str:
        return self.query_one(ContentSwitcher).current or "files"

    def on_mount(self) -> None:
        self.header.show(None, f"Loading {self.session.ref}…")
        self.query_one("#files").loading = True
        self.diff.focus()
        self.load()

    @work(exclusive=True, group="load")
    async def load(self) -> None:
        try:
            pr = await self.session.load()
        except Exception as error:
            self.query_one("#files").loading = False
            self.header.show(None, f"Couldn't load {self.session.ref}: {error}")
            self.notify(
                str(error), title="Couldn't load pull request", severity="error", timeout=10
            )
            return
        self._present(pr, first=True)
        self.prefetch()
        self.load_attributes()

    def _present(self, pr: PullRequest, *, first: bool = False) -> None:
        order = {path: i for i, path in enumerate(tree_order([f.path for f in pr.files]))}
        pr.files.sort(key=lambda f: order.get(f.path, 0))
        diff = self.diff
        self.query_one("#files").loading = False
        diff.load(pr, keep_state=not first)
        if first:
            # start at the first file that still needs looking at
            target = next((s for s in diff.sections if not s.file.is_viewed), None)
            if diff.rows:
                if target is not None and target.index > 0:
                    diff.jump_to_section(target)
                else:
                    diff.set_cursor(0)
            else:
                diff.pending_jump = target
        self.rebuild_tree()
        self.conversation.show(pr)
        self.header.show(pr)
        self.update_status()
        if not first:
            self._mark_hidden_viewed()

    def rebuild_tree(self) -> None:
        diff = self.diff
        self.file_tree.build(diff.visible_sections)
        note = self.query_one("#hidden-note", Static)
        counts = diff.hidden_counts()
        if not counts:
            note.display = False
            return
        p = Palette.from_app(self.app)
        text = Text()
        for kind, key, label in (("test", "T", "test"), ("generated", "X", "generated")):
            count = counts.get(kind)
            if count:
                if text:
                    text.append("\n")
                text.append(f"⊘ {count} {label} hidden", p.style(p.muted))
                text.append(f" · {key} to show", p.style(p.faint))
        note.update(text)
        note.display = True

    @work(group="attributes")
    async def load_attributes(self) -> None:
        """Honour the repository's linguist-generated rules from .gitattributes."""
        try:
            text = await self.session.text_at_head(".gitattributes")
        except Exception:
            return
        if not text:
            return
        attributes = GitAttributes.parse(text)
        if not attributes.rules:
            return
        self.diff.attributes = attributes
        self.diff.reclassify()
        self.diff.relayout()
        self.rebuild_tree()

    @work(group="prefetch")
    async def prefetch(self) -> None:
        sections = [
            s
            for s in self.diff.sections
            if s.file.patch and not s.is_whole_file and s.new_lines is None
        ]
        if not sections or len(self.diff.sections) > PREFETCH_LIMIT:
            return
        for section in sections:
            section.content_state = "loading"
        try:
            texts = await self.session.new_texts(s.file for s in sections)
        except Exception:
            for section in sections:
                section.content_state = "none"
            return
        for section in sections:
            section.set_new_text(texts.get(section.path))
        self.diff.reclassify()  # file headers may reveal generated files
        self.diff._code_cache.clear()
        self.diff.relayout()
        self.rebuild_tree()

    @on(DiffView.ContentWanted)
    def content_wanted(self, message: DiffView.ContentWanted) -> None:
        self.fetch_content(message.sections)

    @work(group="content")
    async def fetch_content(self, sections: list[FileSection]) -> None:
        for section in sections:
            section.content_state = "loading"
        self.diff.refresh()
        try:
            texts = await self.session.new_texts(s.file for s in sections)
        except Exception as error:
            for section in sections:
                section.content_state = "none"
                section.expanded_gaps.clear()
            self.diff.relayout()
            self.notify(f"Couldn't fetch the file: {error}", severity="error")
            return
        for section in sections:
            self.diff.content_loaded(section, texts.get(section.path))

    # -- keeping widgets in sync -------------------------------------------------------

    def after_change(self, *, threads: bool = True, conversation: bool = True) -> None:
        if not self.session.loaded:
            return
        if threads:
            self.diff.sync_threads()
        self.file_tree.refresh_labels()
        if conversation:
            self.conversation.show(self.pr)
        self.header.show(self.pr)
        self.update_status()

    @on(DiffView.CursorFileChanged)
    def cursor_file_changed(self, message: DiffView.CursorFileChanged) -> None:
        if not self.file_tree.has_focus:
            self.file_tree.reveal(message.section)

    @on(DiffView.CursorMoved)
    def cursor_moved(self) -> None:
        self.update_status()

    @on(DiffView.CommentWanted)
    def comment_wanted(self) -> None:
        self.action_comment()

    @on(Tree.NodeHighlighted)
    def tree_highlighted(self, event: Tree.NodeHighlighted) -> None:
        section = event.node.data
        if self.file_tree.has_focus and isinstance(section, FileSection):
            self.diff.jump_to_section(section)

    @on(Tree.NodeSelected)
    def tree_selected(self, event: Tree.NodeSelected) -> None:
        section = event.node.data
        if isinstance(section, FileSection):
            if section.collapsed:
                self.diff.toggle_section(section, collapsed=False)
            self.diff.jump_to_section(section)
            self.diff.focus()

    @on(ConversationView.JumpToThread)
    def jump_to_thread(self, message: ConversationView.JumpToThread) -> None:
        self.action_switch_tab("files")
        self.diff.fold[message.thread.id] = False
        self.diff.relayout()
        self.diff.jump_to_thread(message.thread)
        self.diff.focus()

    @on(ConversationView.Focused)
    def conversation_focused(self) -> None:
        self.update_status()

    def on_descendant_focus(self) -> None:
        self.update_status()

    def update_status(self) -> None:
        status = self.query_one(StatusBar)
        if not self.session.loaded:
            status.show([("q", "quit")])
            return
        if self.tab == "conversation":
            item = self.conversation.focused_item
            hints: list[tuple[str, str]] = [("j/k", "move")]
            if item is not None and item.kind == "thread":
                hints += [("↵", "jump to code"), ("r", "reply"), ("x", "resolve")]
            elif item is not None and item.kind in ("comment", "review"):
                hints += [("↵", "fold"), ("r", "quote reply"), ("x", "resolve")]
                obj = item.obj
                if getattr(obj, "viewer_can_update", False):
                    hints.append(("e", "edit"))
            hints += [("C", "comment"), ("S", "submit review"), ("1", "files")]
            status.show(hints)
            return
        if self.file_tree.has_focus:
            status.show(
                [
                    ("j/k", "browse"),
                    ("↵", "open"),
                    ("tab", "to diff"),
                    ("v", "viewed"),
                    ("t", "hide tree"),
                ]
            )
            return
        diff = self.diff
        row = diff.current_row
        hints = []
        location = ""
        if row is not None:
            section = row.section
            location = section.path.rsplit("/", 1)[-1]
            if diff.sel_anchor is not None:
                selection = diff.selection()
                count = len(selection.lines) if selection else 0
                hints = [
                    ("c", f"comment on {count} lines"),
                    ("s", "suggest"),
                    ("esc", "cancel selection"),
                ]
            elif row.kind is RowKind.FILE:
                hints = [
                    ("↵", "fold"),
                    ("c", "comment on file"),
                    ("v", "viewed"),
                    ("]", "next file"),
                ]
            elif row.kind is RowKind.GAP:
                hints = [("↵", "expand"), ("E", "expand file"), ("}", "next change")]
            elif row.kind is RowKind.THREAD and row.thread is not None:
                thread = row.thread
                hints = [("r", "reply")]
                if not thread.is_pending:
                    hints.append(("x", "unresolve" if thread.is_resolved else "resolve"))
                comment = diff.active_comment
                if comment is not None and comment.viewer_can_update:
                    hints.append(("e", "edit"))
                if comment is not None and comment.viewer_can_delete:
                    hints.append(("d", "delete"))
                hints += [("z", "fold"), ("n", "next thread")]
            elif row.is_code:
                line = row.line_on(diff.cursor_side) if row.kind is RowKind.SPLIT else row.line
                if line is not None and line.expanded:
                    hints = [("}", "next change"), ("]", "next file")]
                else:
                    hints = [("c", "comment"), ("s", "suggest"), ("V", "select lines")]
                    if diff.active_thread is not None:
                        hints.append(("r", "reply"))
                hints += [("v", "viewed"), ("}", "next change"), ("]", "next file")]
                if line is not None:
                    number = (
                        line.number_on(diff.cursor_side)
                        if row.kind is RowKind.SPLIT
                        else line.anchor[1]
                    )
                    if number is not None:
                        location += f":{number}"
        viewed = sum(1 for f in self.pr.files if f.is_viewed)
        location += f" · {viewed}/{len(self.pr.files)} viewed"
        status.show(hints, location)

    # -- navigation ------------------------------------------------------------------

    def action_switch_tab(self, tab: str) -> None:
        self.query_one(ContentSwitcher).current = tab
        self.header.tab = tab
        self.header.refresh()
        if tab == "files":
            self.diff.focus()
        else:
            self.conversation.focus_first()
        self.update_status()

    def action_toggle_tree(self) -> None:
        tree = self.file_tree
        tree.display = not tree.display
        if not tree.display and tree.has_focus:
            self.diff.focus()

    def action_find_file(self) -> None:
        if not self.session.loaded:
            return
        self.app.push_screen(CommandPalette(providers=[FileFinder], placeholder="Go to file…"))

    def go_to_section(self, section: FileSection) -> None:
        self.action_switch_tab("files")
        if section.collapsed:
            self.diff.toggle_section(section, collapsed=False)
        self.diff.jump_to_section(section)
        self.diff.focus()

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_back(self) -> None:
        if self.from_inbox:
            self.app.pop_screen()
        else:
            self.app.exit(self._exit_note())

    def _exit_note(self) -> str | None:
        if not self.session.loaded:
            return None
        pending = self.pr.pending_comment_count
        if self.pr.pending_review is not None and pending:
            return (
                f"Your review of {self.pr.ref} has {pending} pending comment"
                f"{'s' if pending != 1 else ''}. They are saved on GitHub; "
                "run revv again (or use the web UI) to submit."
            )
        return None

    # -- helpers ---------------------------------------------------------------------

    def _ready(self) -> bool:
        if not self.session.loaded:
            self.notify("Still loading…", timeout=1.5)
            return False
        return True

    def _selection_preview(self, selection: Selection) -> Text:
        """A preview of the lines being commented on, for the editor."""
        p = Palette.from_app(self.app)
        text = Text()
        lines = selection.lines
        shown = lines if len(lines) <= 8 else [*lines[:3], None, *lines[-4:]]
        for line in shown:
            if line is None:
                text.append(f"      ⋯ {len(lines) - 7} more lines\n", p.style(p.faint))
                continue
            _, number = selection.anchor(line)
            sign = {LineKind.ADD: "+", LineKind.DEL: "-"}.get(line.kind, " ")
            bg = {LineKind.ADD: p.add_bg, LineKind.DEL: p.del_bg}.get(line.kind, p.surface)
            text.append(f"{number:>5} {sign} ", p.style(p.muted, bg))
            text.append(line.text.expandtabs(4), p.style(p.text, bg))
            text.append("\n")
        text.rstrip()
        return text

    @staticmethod
    def _suggestion_text(selection: Selection) -> str | None:
        """Raw text of the selected new-version lines, if they can take a suggestion."""
        lines = []
        for line in selection.lines:
            side, _ = selection.anchor(line)
            if side is Side.LEFT:
                return None
            lines.append(line.text)
        return "\n".join(lines)

    def _current_thread(self) -> ReviewThread | None:
        if self.tab == "conversation":
            item = self.conversation.focused_item
            if item is not None and isinstance(item.obj, ReviewThread):
                return item.obj
            return None
        return self.diff.active_thread

    def _current_comment(self) -> Comment | Review | None:
        if self.tab == "conversation":
            item = self.conversation.focused_item
            if item is None:
                return None
            if item.kind == "thread":
                thread = item.obj
                assert isinstance(thread, ReviewThread)
                mine = [c for c in thread.comments if c.viewer_can_update]
                return mine[-1] if mine else (thread.comments[-1] if thread.comments else None)
            if isinstance(item.obj, (Comment, Review)):
                return item.obj
            return None
        return self.diff.active_comment

    async def _optimistic(self, coroutine) -> asyncio.Future:
        """Start an action whose local (optimistic) effect happens before its first await."""
        task = asyncio.ensure_future(coroutine)
        await asyncio.sleep(0)
        return task

    async def _run(self, label: str, coroutine) -> bool:
        """Await an API call, reporting failure as a notification."""
        self._busy += 1
        try:
            await coroutine
            return True
        except Exception as error:
            self.notify(str(error), title=f"Couldn't {label}", severity="error", timeout=8)
            return False
        finally:
            self._busy -= 1

    # -- commenting ------------------------------------------------------------------

    def action_comment(self) -> None:
        if not self._ready():
            return
        if self.tab == "conversation":
            self.action_general_comment()
            return
        row = self.diff.current_row
        if row is None:
            return
        if row.kind is RowKind.FILE:
            self.comment_on_file(row.section)
            return
        if row.kind is RowKind.THREAD and row.thread is not None:
            self.reply_to(row.thread)
            return
        selection = self.diff.selection()
        if selection is None:
            self.notify("Move the cursor to a line of code to comment on it", timeout=2)
            return
        self.comment_on_lines(selection, suggest=False)

    def action_suggest(self) -> None:
        if not self._ready() or self.tab != "files":
            return
        selection = self.diff.selection()
        if selection is None:
            self.notify("Select lines of the new version to suggest a change", timeout=2)
            return
        if self._suggestion_text(selection) is None:
            self.notify("Suggestions can only replace lines of the new version", severity="warning")
            return
        self.comment_on_lines(selection, suggest=True)

    @work(group="edit")
    async def comment_on_lines(self, selection: Selection, suggest: bool) -> None:
        if not selection.commentable:
            self.notify(
                "GitHub only allows comments on lines inside the diff (not expanded context)",
                severity="warning",
                timeout=4,
            )
            return
        path = selection.section.path
        start_side, start = selection.start
        end_side, end = selection.end
        suggestion = self._suggestion_text(selection)
        initial = (
            f"```suggestion\n{suggestion}\n```\n" if suggest and suggestion is not None else ""
        )
        pending = self.pr.pending_review is not None
        result: EditorResult | None = await self.app.push_screen_wait(
            CommentEditor(
                f"Comment on {path} · {selection.label}",
                mode="thread",
                context=self._selection_preview(selection),
                initial=initial,
                draft_key=f"line:{path}:{start_side}:{start}:{end_side}:{end}",
                pending_review=pending,
                pending_count=self.pr.pending_comment_count,
                suggestion=suggestion,
            )
        )
        if result is None:
            return
        is_range = (start_side, start) != (end_side, end)
        ok = await self._run(
            "add the comment",
            self.session.add_thread(
                path=path,
                body=result.body,
                line=end,
                side=end_side,
                start_line=start if is_range else None,
                start_side=start_side if is_range else None,
                publish_now=result.publish_now,
            ),
        )
        if not ok:
            DRAFTS[f"line:{path}:{start_side}:{start}:{end_side}:{end}"] = result.body
            return
        self.diff.sel_anchor = None
        self.after_change()
        self.notify("Comment posted" if result.publish_now else "Added to your review", timeout=2)

    @work(group="edit")
    async def comment_on_file(self, section: FileSection) -> None:
        pending = self.pr.pending_review is not None
        result: EditorResult | None = await self.app.push_screen_wait(
            CommentEditor(
                f"Comment on the file {section.path}",
                mode="thread",
                draft_key=f"file:{section.path}",
                pending_review=pending,
                pending_count=self.pr.pending_comment_count,
            )
        )
        if result is None:
            return
        ok = await self._run(
            "add the comment",
            self.session.add_thread(
                path=section.path, body=result.body, file_level=True, publish_now=result.publish_now
            ),
        )
        if ok:
            if section.collapsed:
                section.collapsed = False
            self.after_change()
            self.notify(
                "Comment posted" if result.publish_now else "Added to your review", timeout=2
            )

    def action_reply(self) -> None:
        if not self._ready():
            return
        thread = self._current_thread()
        if thread is not None:
            self.reply_to(thread)
            return
        if self.tab == "conversation":
            item = self.conversation.focused_item
            if item is not None and isinstance(item.obj, (Comment, Review)):
                self.quote_reply(item.obj)
                return
        self.notify("Move to a comment thread to reply", timeout=2)

    @work(group="edit")
    async def reply_to(self, thread: ReviewThread) -> None:
        root = thread.root
        context = None
        if thread.comments:
            p = Palette.from_app(self.app)
            last = thread.comments[-1]
            context = Text()
            context.append(f"{thread.path} {thread.line_label}\n", p.style(p.faint))
            context.append(last.author, p.style(p.author_color(last.author), bold=True))
            context.append(": ")
            excerpt = " ".join(last.body.split())
            context.append(
                excerpt[:300] + ("…" if len(excerpt) > 300 else ""), p.style(p.muted, italic=True)
            )
        pending = self.pr.pending_review is not None
        result: EditorResult | None = await self.app.push_screen_wait(
            CommentEditor(
                f"Reply to {root.author if root else 'thread'}",
                mode="reply",
                context=context,
                draft_key=f"reply:{thread.id}",
                pending_review=pending,
                pending_count=self.pr.pending_comment_count,
            )
        )
        if result is None:
            return
        ok = await self._run("reply", self.session.reply(thread, result.body, result.publish_now))
        if ok:
            self.diff.fold[thread.id] = False
            self.after_change()
            self.notify(
                "Reply posted" if result.publish_now else "Reply added to your review", timeout=2
            )

    @work(group="edit")
    async def quote_reply(self, obj: Comment | Review) -> None:
        quoted = "\n".join(f"> {line}" for line in obj.body.strip().splitlines())
        initial = f"{quoted}\n\n" if quoted else f"@{obj.author} "
        await self._issue_comment(initial)

    def action_general_comment(self) -> None:
        if self._ready():
            self.general_comment()

    @work(group="edit")
    async def general_comment(self) -> None:
        await self._issue_comment("")

    async def _issue_comment(self, initial: str) -> None:
        result: EditorResult | None = await self.app.push_screen_wait(
            CommentEditor(
                f"Comment on #{self.pr.ref.number}",
                mode="issue",
                initial=initial,
                draft_key="issue" if not initial else None,
            )
        )
        if result is None:
            return
        if await self._run("post the comment", self.session.add_issue_comment(result.body)):
            self.after_change(threads=False)
            self.notify("Comment posted", timeout=2)

    # -- resolving, editing, deleting ------------------------------------------------------

    def action_resolve(self) -> None:
        if not self._ready():
            return
        thread = self._current_thread()
        if thread is not None:
            self.toggle_thread(thread)
            return
        if self.tab == "conversation":
            item = self.conversation.focused_item
            if item is not None and isinstance(item.obj, (Comment, Review)):
                self.toggle_minimized(item.obj)
                return
        self.notify("Move to a comment thread to resolve it", timeout=2)

    @work(group="edit")
    async def toggle_thread(self, thread: ReviewThread) -> None:
        if thread.is_pending:
            self.notify("This thread is still part of your pending review", severity="warning")
            return
        resolve = not thread.is_resolved
        if resolve and not thread.viewer_can_resolve:
            self.notify("You don't have permission to resolve this thread", severity="warning")
            return
        if not resolve and not thread.viewer_can_unresolve:
            self.notify("You don't have permission to unresolve this thread", severity="warning")
            return
        self.diff.fold.pop(thread.id, None)
        task = await self._optimistic(self.session.set_resolved(thread, resolve))
        self.after_change(conversation=False)  # the optimistic update shows immediately
        if await self._run("resolve the thread" if resolve else "unresolve the thread", task):
            self.notify("Resolved ✓" if resolve else "Unresolved", timeout=1.5)
        self.after_change()

    @work(group="edit")
    async def toggle_minimized(self, obj: Comment | Review) -> None:
        resolve = not obj.is_minimized
        allowed = obj.viewer_can_minimize if resolve else obj.viewer_can_unminimize
        if not allowed:
            self.notify("You don't have permission to resolve this comment", severity="warning")
            return
        task = await self._optimistic(self.session.set_comment_resolved(obj, resolve))
        self.conversation.refresh_cards()
        if await self._run("update the comment", task):
            self.notify("Marked as resolved ✓" if resolve else "Unresolved", timeout=1.5)
        self.after_change(threads=False)

    def action_edit(self) -> None:
        if not self._ready():
            return
        comment = self._current_comment()
        if comment is None or not comment.viewer_can_update:
            self.notify("Move to one of your own comments to edit it", timeout=2)
            return
        self.edit_comment(comment)

    @work(group="edit")
    async def edit_comment(self, comment: Comment | Review) -> None:
        result: EditorResult | None = await self.app.push_screen_wait(
            CommentEditor("Edit comment", mode="edit", initial=comment.body)
        )
        if result is None or result.body == comment.body:
            return
        if isinstance(comment, Review):
            ok = await self._run("save", self._update_review(comment, result.body))
        else:
            ok = await self._run("save", self.session.edit_comment(comment, result.body))
        if ok:
            self.after_change()
            self.notify("Saved", timeout=1.5)

    async def _update_review(self, review: Review, body: str) -> None:
        updated = await self.session.backend.update_review_body(review.id, body)
        review.body = updated.body

    def action_delete(self) -> None:
        if not self._ready():
            return
        comment = self._current_comment()
        if not isinstance(comment, Comment) or not comment.viewer_can_delete:
            self.notify("Move to one of your own comments to delete it", timeout=2)
            return
        self.delete_comment(comment)

    @work(group="edit")
    async def delete_comment(self, comment: Comment) -> None:
        excerpt = " ".join(comment.body.split())[:120]
        confirmed = await self.app.push_screen_wait(
            ConfirmDialog(f"Delete this comment?\n\n“{excerpt}”", "Delete")
        )
        if confirmed and await self._run(
            "delete the comment", self.session.delete_comment(comment)
        ):
            self.after_change()
            self.notify("Deleted", timeout=1.5)

    # -- viewed, submit, refresh -------------------------------------------------------

    def action_viewed(self) -> None:
        if not self._ready() or self.tab != "files":
            return
        node = self.file_tree.cursor_node if self.file_tree.has_focus else None
        if node is not None and isinstance(node.data, FileSection):
            section: FileSection | None = node.data
        else:
            section = self.diff.current_section
        if section is None:
            return
        self.toggle_viewed(section)

    @work(group="viewed")
    async def toggle_viewed(self, section: FileSection) -> None:
        viewed = not section.file.is_viewed
        task = await self._optimistic(self.session.set_viewed(section.file, viewed))
        diff = self.diff
        if viewed:
            section.collapsed = True
            diff.relayout()
            following = diff.sections[section.index + 1 :] + diff.sections[: section.index]
            nxt = next((s for s in following if not s.file.is_viewed), None)
            if nxt is not None:
                if nxt.collapsed:
                    nxt.collapsed = False
                    diff.relayout()
                diff.jump_to_section(nxt)
            else:
                diff.set_cursor(diff._starts[section.index])
                self.notify("All files viewed 🎉  Press S to submit your review", timeout=4)
        else:
            section.collapsed = False
            diff.relayout()
        self.file_tree.refresh_labels()
        self.header.show(self.pr)
        if not await self._run("update the viewed state", task):
            section.collapsed = not section.file.is_viewed
            diff.relayout()
            self.file_tree.refresh_labels()
            self.header.show(self.pr)
        self.update_status()

    def action_hide_kind(self, kind: str) -> None:
        """Mark every test (or generated) file as viewed and hide it; again to show them."""
        if not self._ready():
            return
        diff = self.diff
        label, key = ("test", "T") if kind == "test" else ("generated", "X")
        if kind in diff.hidden_kinds:
            diff.hidden_kinds.discard(kind)
            for section in diff.sections:
                if kind in section.kinds:
                    section.force_visible = False
            diff.relayout()
            self.rebuild_tree()
            self.update_status()
            self.notify(f"Showing {label} files again", timeout=2)
            return
        matching = [s for s in diff.sections if kind in s.kinds]
        if not matching:
            self.notify(f"No {label} files in this pull request", timeout=2)
            return
        diff.hidden_kinds.add(kind)
        for section in matching:
            section.force_visible = False
            section.collapsed = True
        diff.relayout()
        self.rebuild_tree()
        self.mark_sections_viewed(matching, label, key)

    @work(group="viewed")
    async def mark_sections_viewed(self, sections: list[FileSection], label: str, key: str) -> None:
        files = [s.file for s in sections if not s.file.is_viewed]
        ok = True
        if files:
            ok = await self._run("mark files as viewed", self.session.set_viewed_many(files, True))
        self.file_tree.refresh_labels()
        self.header.show(self.pr)
        self.update_status()
        if not ok:
            return
        hidden = len(sections)
        plural = "s" if hidden != 1 else ""
        if files:
            self.notify(
                f"Marked {len(files)} {label} file{'s' if len(files) != 1 else ''} as viewed "
                f"and hid {hidden} ({key} shows them again)",
                timeout=3,
            )
        else:
            self.notify(f"Hid {hidden} {label} file{plural} ({key} shows them again)", timeout=3)

    def _mark_hidden_viewed(self) -> None:
        """After a refresh, newly pushed files of a hidden kind get marked as viewed too."""
        diff = self.diff
        for kind in diff.hidden_kinds:
            stale = [s for s in diff.sections if kind in s.kinds and not s.file.is_viewed]
            if stale:
                label, key = ("test", "T") if kind == "test" else ("generated", "X")
                self.mark_sections_viewed(stale, label, key)

    def action_submit(self, preset: str | None = None) -> None:
        if self._ready():
            self.submit_review(ReviewEvent(preset) if preset else None)

    @work(group="edit")
    async def submit_review(self, preset: ReviewEvent | None) -> None:
        if preset is ReviewEvent.APPROVE and self.pr.viewer_did_author:
            self.notify("You can't approve your own pull request", severity="warning")
            preset = None
        result: SubmitResult | None = await self.app.push_screen_wait(
            SubmitReviewDialog(self.pr, preset, DRAFTS.get("review-body", ""))
        )
        if result is None:
            return
        if result.action == "discard":
            count = self.pr.pending_comment_count
            confirmed = await self.app.push_screen_wait(
                ConfirmDialog(
                    f"Discard your pending review and its {count} comment{'s' if count != 1 else ''}?",
                    "Discard",
                )
            )
            if confirmed and await self._run(
                "discard the review", self.session.discard_pending_review()
            ):
                self.after_change()
                self.notify("Pending review discarded", timeout=2)
            return
        DRAFTS["review-body"] = result.body
        label = {
            ReviewEvent.APPROVE: "Approved ✓",
            ReviewEvent.REQUEST_CHANGES: "Changes requested",
            ReviewEvent.COMMENT: "Review submitted",
        }[result.event]
        if await self._run(
            "submit the review", self.session.submit_review(result.event, result.body)
        ):
            DRAFTS.pop("review-body", None)
            self.after_change()
            self.notify(label, title=str(self.pr.ref), timeout=3)
            # GitHub settles a submitted review asynchronously; refresh a moment later.
            self.set_timer(2.0, lambda: self.action_refresh(quiet=True))

    def action_refresh(self, quiet: bool = False) -> None:
        if self._ready():
            self.refresh_pr(quiet)

    @work(exclusive=True, group="refresh")
    async def refresh_pr(self, quiet: bool = False) -> None:
        if not quiet:
            self.notify("Refreshing…", timeout=1)
        try:
            pr = await self.session.refresh()
        except Exception as error:
            self.notify(str(error), title="Couldn't refresh", severity="error")
            return
        self._present(pr)
        self.prefetch()
        if not quiet:
            self.notify("Up to date", timeout=1.5)

    # -- misc ------------------------------------------------------------------------

    def _location_url(self) -> str:
        pr = self.pr
        if self.tab == "conversation":
            item: Item | None = self.conversation.focused_item
            if item is not None:
                obj = item.obj
                if isinstance(obj, ReviewThread):
                    return obj.root.url if obj.root and obj.root.url else pr.url
                url = getattr(obj, "url", "")
                if url:
                    return url
            return pr.url
        row = self.diff.current_row
        if row is None:
            return pr.url + "/files"
        thread = row.thread if row.kind is RowKind.THREAD else None
        if thread is not None and thread.root is not None and thread.root.url:
            return thread.root.url
        anchor = diff_anchor(row.section.path)
        if row.is_code:
            line = row.line_on(self.diff.cursor_side) if row.kind is RowKind.SPLIT else row.line
            if line is not None:
                side, number = line.anchor
                return f"{pr.url}/files#{anchor}{'L' if side is Side.LEFT else 'R'}{number}"
        return f"{pr.url}/files#{anchor}"

    def action_open_browser(self) -> None:
        if self._ready():
            url = self._location_url()
            self.app.open_url(url)
            self.notify(f"Opened {url}", timeout=2)

    def action_copy_location(self) -> None:
        if not self._ready():
            return
        row = self.diff.current_row
        if row is None:
            return
        text = row.section.path
        if row.is_code:
            line = row.line_on(self.diff.cursor_side) if row.kind is RowKind.SPLIT else row.line
            if line is not None:
                _, number = line.anchor
                text += f":{number}"
        self.app.copy_to_clipboard(text)
        self.notify(f"Copied {text}", timeout=1.5)
