"""The review screen: header, file tree, diff, conversation, and every reviewing action."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import time
from datetime import UTC, datetime
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

from revv import filters, panc
from revv.classify import GitAttributes
from revv.config import display_name, save_config, setting, update_nicknames
from revv.diff import DiffLine, LineKind, parse_patch
from revv.maintainers import Ownership, find, maintainer_settings, short_team, team_key
from revv.models import (
    REACTION_EMOJI,
    AiCheck,
    ChangedFile,
    Comment,
    Fingerprint,
    PullRequest,
    Review,
    ReviewEvent,
    ReviewThread,
    Side,
)
from revv.session import ReviewSession
from revv.ui.conversation import ConversationView, Item
from revv.ui.dialogs import (
    ConfirmDialog,
    HelpScreen,
    IgnoreCommentDialog,
    NicknameDialog,
    ReactionPicker,
    SubmitResult,
    SubmitReviewDialog,
)
from revv.ui.diffmodel import FileSection, RowKind
from revv.ui.diffview import DiffView, Selection
from revv.ui.editor import DRAFTS, CommentEditor, EditorResult
from revv.ui.filetree import FileTree, tree_order
from revv.ui.palette import Palette
from revv.ui.render import relative_time
from revv.ui.widgets import PRHeader, StatusBar

PREFETCH_LIMIT = 150  # fetch full file text up front for PRs with at most this many files


class LineFinder(Provider):
    """Fuzzy search over every changed line of the pull request."""

    async def search(self, query: str) -> Hits:
        screen = self.screen
        if not isinstance(screen, ReviewScreen) or not query.strip():
            return
        matcher = self.matcher(query)
        for count, (section, line) in enumerate(screen.diff.changed_lines()):
            if count % 400 == 399:
                await asyncio.sleep(0)  # stay responsive on huge pull requests
            text = line.text.strip()
            if not text:
                continue
            score = matcher.match(text)
            if score > 0:
                side, number = line.anchor
                sign = "+" if line.kind is LineKind.ADD else "−"
                yield Hit(
                    score,
                    matcher.highlight(text),
                    partial(screen.go_to_line, section, line),
                    help=f"{sign} {section.path}:{number}"
                    + (" (old)" if side is Side.LEFT else ""),
                )


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
    ReviewScreen #sidebar { width: 34; max-width: 75%; border-right: vkey $panel; }
    ReviewScreen #sidebar:focus-within { border-right: vkey $accent; }
    ReviewScreen FileTree { height: 1fr; }
    ReviewScreen #banner {
        height: auto; padding: 0 1; display: none; text-style: bold;
    }
    ReviewScreen #banner.cached { background: $warning 30%; color: $text; }
    ReviewScreen #banner.offline { background: $error 35%; color: $text; }
    ReviewScreen #banner.changed { background: $accent 35%; color: $text; }
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
        Binding("slash", "search_lines", "Search changed lines", show=False),
        Binding("f,ctrl+k", "find_file", "Go to file", show=False),
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
        Binding("at", "nicknames", "Nicknames", show=False),
        Binding("L", "since_review", "Since last review", show=False),
        Binding("m", "group_by_team", "Group by team", show=False),
        Binding("M", "only_mine", "Only your teams' files", show=False),
        Binding("less_than_sign", "sidebar_width(-4)", "Narrower tree", show=False),
        Binding("greater_than_sign", "sidebar_width(4)", "Wider tree", show=False),
        Binding("plus", "react", "React", show=False),
        Binding("i", "ignore_comment", "Ignore comments like this", show=False),
        Binding("T", "hide_kind('test')", "Hide tests", show=False),
        Binding("X", "hide_kind('generated')", "Hide generated", show=False),
    ]

    def __init__(self, session: ReviewSession, *, from_inbox: bool = False) -> None:
        super().__init__()
        self.session = session
        self.from_inbox = from_inbox
        self.initial_tab = "files" if setting("open_tab") == "files" else "conversation"
        self._busy = 0
        self._syncing = False
        self._save_timer = None
        self._last_write = 0.0
        self._checking = False
        self._acknowledged: Fingerprint | None = (
            None  # remote state known to change nothing visible
        )
        self.ai_check: AiCheck | None = None  # panc's verdict on the description
        self.ownership: Ownership | None = None  # maintainer teams (an opt-in extra)
        settings = maintainer_settings()
        self.group_by_team = settings.group_by_team if settings else True
        self._teams_loaded = False
        self._marked_kinds: set[str] = set()  # kinds hidden with T / X, which marks them viewed
        self.since: Review | None = None  # showing only the changes since this review
        self.since_files: list[ChangedFile] | None = None

    # -- composition ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Keep references rather than querying: callbacks that finish after the screen
        # closed (e.g. a cancelled sync's cleanup) must not crash looking for widgets.
        self.header = PRHeader(id="header")
        self.banner = Static(id="banner")
        self.switcher = ContentSwitcher(initial=self.initial_tab, id="tabs")
        self.files_pane = Horizontal(id="files")
        self.sidebar = Vertical(id="sidebar")
        self.file_tree = FileTree(id="tree")
        self.hidden_note = Static(id="hidden-note")
        self.diff = DiffView(id="diff")
        self.conversation = ConversationView(id="conversation")
        self.status_bar = StatusBar(id="status")
        yield self.header
        yield self.banner
        with self.switcher:
            with self.files_pane:
                with self.sidebar:
                    yield self.file_tree
                    yield self.hidden_note
                yield self.diff
            yield self.conversation
        yield self.status_bar

    @property
    def pr(self) -> PullRequest:
        return self.session.pr

    @property
    def tab(self) -> str:
        return self.switcher.current or "files"

    def on_mount(self) -> None:
        self._apply_sidebar_width(setting("sidebar_width"))
        kinds = setting("hide_by_default")
        self.diff.hidden_kinds = {k for k in kinds if k in ("test", "generated")}
        self.header.tab = self.initial_tab
        self.header.show(None, f"Loading {self.session.ref}…")
        self.files_pane.loading = True
        if self.initial_tab == "files":
            self.diff.focus()
        self.load()

    @work(exclusive=True, group="load")
    async def load(self) -> None:
        try:
            pr = await self.session.load()
        except Exception as error:
            self.files_pane.loading = False
            self.header.show(None, f"Couldn't load {self.session.ref}: {error}")
            self.notify(
                str(error), title="Couldn't load pull request", severity="error", timeout=10
            )
            return
        self._present(pr, first=True)
        if not self.session.fresh:  # opened from the disk cache: sync right away
            self.show_banner("cached")
            self.refresh_pr(quiet=True)
        self.prefetch()
        self.load_attributes()
        interval = setting("refresh_interval")
        if isinstance(interval, int | float) and interval > 0:
            self.set_interval(max(10, interval), self.check_for_changes)

    def _display_order(self, paths: list[str]) -> dict[str, int]:
        """Where each file goes: by folder, or grouped by maintainer team (yours first)."""
        folders = tree_order(paths)
        ownership = self.ownership
        if ownership is None or not self.group_by_team:
            return {path: i for i, path in enumerate(folders)}
        groups = {group: i for i, group in enumerate(ownership.group_order(paths))}
        ranked = sorted(folders, key=lambda path: groups[ownership.group_of(path)])
        return {path: i for i, path in enumerate(ranked)}

    def _update_ownership(self, pr: PullRequest) -> None:
        """Read the maintainers comment (if the feature is on) and your teams (cached)."""
        settings = maintainer_settings()
        found = find(pr.comments, settings) if settings else None
        if found is None:
            self.ownership = None
        else:
            known = self.ownership.my_teams if self.ownership else set()
            known |= {team_key(t) for t in settings.my_teams} if settings else set()
            self.ownership = Ownership(found, known)
            if not self._teams_loaded:
                self.load_my_teams()
        self.diff.ownership = self.ownership
        self.conversation.ownership = self.ownership

    def recheck_teams(self) -> None:
        """Look your teams up again (after the settings screen forgot the cached ones)."""
        self._teams_loaded = False
        if self.ownership is not None:
            settings = maintainer_settings()
            self.ownership.my_teams = (
                {team_key(t) for t in settings.my_teams} if settings else set()
            )
            self.load_my_teams()

    @work(group="teams")
    async def load_my_teams(self) -> None:
        ownership = self.ownership
        if ownership is None:
            return
        self._teams_loaded = True
        settings = maintainer_settings()
        extra = settings.my_teams if settings else ()
        teams = await self.session.my_teams(ownership.maintainers.orgs, extra)
        if self.ownership is None or teams == self.ownership.my_teams:
            return
        self.ownership.my_teams = teams
        self._present(self.pr)  # regroup: your teams come first

    def _present(self, pr: PullRequest, *, first: bool = False) -> None:
        self._update_ownership(pr)
        order = self._display_order([f.path for f in pr.files])
        pr.files.sort(key=lambda f: order.get(f.path, 0))
        diff = self.diff
        self.files_pane.loading = False
        if self.since_files is not None:
            # the diff shows only what changed since your last review
            since = self.since_files
            order = self._display_order([f.path for f in since])
            since.sort(key=lambda f: order.get(f.path, 0))
            diff.load(dataclasses.replace(pr, files=since), keep_state=not first)
        else:
            diff.load(pr, keep_state=not first)
        if first:
            # start at the first file that still needs looking at
            target = next(
                (s for s in diff.sections if not s.file.is_viewed and not diff.is_hidden(s)), None
            )
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
        if first and self.tab == "conversation":
            self.call_after_refresh(self.conversation.focus_first)
        self.update_status()
        if not first:
            self._mark_hidden_viewed()
        if setting("panc") and panc.executable() is not None:
            self.check_description()

    def rebuild_tree(self) -> None:
        diff = self.diff
        self.file_tree.build(diff.visible_sections, self._team_groups())
        self.header.hidden = {s.path for s in diff.sections if diff.is_hidden(s)}
        ownership = self.ownership
        self.header.mine = (
            {f.path for f in self.pr.files if ownership.mine(f.path)} if ownership else None
        )
        self.header.refresh()
        note = self.hidden_note
        counts = diff.hidden_counts()
        if not counts:
            note.display = False
            return
        p = Palette.from_app(self.app)
        text = Text()
        for kind, key, label in (
            ("test", "T", "test"),
            ("generated", "X", "generated"),
            ("others", "M", "other teams'"),
        ):
            count = counts.get(kind)
            if count:
                if text:
                    text.append("\n")
                text.append(f"⊘ {count} {label} hidden", p.style(p.muted))
                text.append(f" · {key}", p.style(p.accent_fg, bold=True))
        note.update(text)
        note.display = True

    def _team_groups(self) -> list[tuple[Text, list[FileSection]]] | None:
        """The file tree's groups when grouping by maintainer team."""
        ownership = self.ownership
        if ownership is None or not self.group_by_team:
            return None
        sections = self.diff.visible_sections
        p = Palette.from_app(self.app)
        groups = []
        for group in ownership.group_order(s.path for s in sections):
            members = [s for s in sections if ownership.group_of(s.path) == group]
            label = Text()
            if group is None:
                label.append("no maintainer listed", p.style(p.faint, bold=True))
            elif ownership.is_mine(group):
                label.append(f"★ {short_team(group)}", p.style(p.accent_fg, bold=True))
                label.append(" your team", p.style(p.faint, italic=True))
            else:
                label.append(short_team(group), p.style(p.muted, bold=True))
            label.append(f" {len(members)}", p.style(p.faint))
            groups.append((label, members))
        return groups

    def action_group_by_team(self) -> None:
        """m: group the files by maintainer team, or by folder."""
        self.group_by_team = not self.group_by_team
        raw = setting("maintainers")
        if isinstance(raw, dict):
            save_config(maintainers={**raw, "group_by_team": self.group_by_team})
        self._present(self.pr)
        self.notify(
            "Grouped by maintainer team" if self.group_by_team else "Grouped by folder", timeout=1.5
        )

    def action_only_mine(self) -> None:
        """M: show only the files your teams maintain (again: everything)."""
        diff = self.diff
        ownership = self.ownership
        if ownership is None:
            return
        if not ownership.my_teams:
            self.notify(
                'revv doesn\'t know your teams: list them as "my_teams" in the maintainers config',
                severity="warning",
                timeout=5,
            )
            return
        diff.only_mine = not diff.only_mine
        for section in diff.sections:
            section.force_visible = False
        diff.relayout()
        self.rebuild_tree()
        self.update_status()
        if diff.only_mine:
            mine = [s for s in diff.sections if not diff.is_hidden(s)]
            if mine:
                diff.jump_to_section(mine[0])
            self.notify(f"Showing your teams' {len(mine)} files (M shows everything)", timeout=2)
        else:
            self.notify("Showing every file", timeout=1.5)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # m / M only exist while the maintainer-teams extra has something to show
        return not (action in ("group_by_team", "only_mine") and self.ownership is None)

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
        self._save_soon()

    def _save_soon(self) -> None:
        """Keep the disk cache current after local changes (debounced)."""
        if self._save_timer is not None:
            self._save_timer.stop()
        self._save_timer = self.set_timer(1.5, self.session.save)

    def on_unmount(self) -> None:
        if self.session.loaded:
            self.session.save()

    @on(DiffView.CursorFileChanged)
    def cursor_file_changed(self, message: DiffView.CursorFileChanged) -> None:
        if not self.file_tree.has_focus:
            self.file_tree.reveal(message.section)

    @on(DiffView.SectionsChanged)
    def sections_changed(self) -> None:
        self.rebuild_tree()

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

    @on(ConversationView.ShowFiles)
    def show_files(self) -> None:
        self.action_switch_tab("files")

    @on(ConversationView.Focused)
    def conversation_focused(self) -> None:
        self.update_status()

    def on_descendant_focus(self) -> None:
        self.update_status()

    def update_status(self) -> None:
        status = self.status_bar
        if not self.session.loaded:
            status.show([("q", "quit")])
            return
        if self.tab == "conversation":
            item = self.conversation.focused_item
            hints: list[tuple[str, str]] = [("j/k", "move")]
            if item is not None and item.kind == "thread":
                hints += [("↵", "jump to code"), ("r", "reply"), ("x", "resolve")]
            elif item is not None and item.kind in ("comment", "review"):
                hints += [("↵", "fold"), ("r", "quote reply"), ("x", "resolve"), ("+", "react")]
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
                hints += [("+", "react"), ("z", "fold"), ("n", "next"), ("u", "next unresolved")]
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
        status.show(hints, location)

    # -- navigation ------------------------------------------------------------------

    def action_switch_tab(self, tab: str) -> None:
        self.switcher.current = tab
        self.header.tab = tab
        self.header.refresh()
        if tab == "files":
            self.diff.focus()
        else:
            self.conversation.focus_first()
        self.update_status()

    SIDEBAR_MIN, SIDEBAR_MAX = 16, 120

    def _apply_sidebar_width(self, width: int) -> int:
        width = max(self.SIDEBAR_MIN, min(self.SIDEBAR_MAX, int(width)))
        self.sidebar.styles.width = width
        return width

    def action_sidebar_width(self, delta: int) -> None:
        sidebar = self.sidebar
        if not sidebar.display:
            sidebar.display = True
        current = sidebar.styles.width.value if sidebar.styles.width else 34
        width = self._apply_sidebar_width(int(current) + delta)
        save_config(sidebar_width=width)
        self.notify(f"File tree: {width} columns", timeout=1)

    def action_toggle_tree(self) -> None:
        sidebar = self.sidebar
        sidebar.display = not sidebar.display
        if not sidebar.display and self.file_tree.has_focus:
            self.diff.focus()

    def action_find_file(self) -> None:
        if not self.session.loaded:
            return
        self.app.push_screen(CommandPalette(providers=[FileFinder], placeholder="Go to file…"))

    def action_search_lines(self) -> None:
        if not self.session.loaded:
            return
        self.app.push_screen(
            CommandPalette(providers=[LineFinder], placeholder="Search the changed lines…")
        )

    def go_to_line(self, section: FileSection, line: DiffLine) -> None:
        self.action_switch_tab("files")
        self.diff.jump_to_line(section, line)
        self.diff.focus()

    def go_to_section(self, section: FileSection) -> None:
        self.action_switch_tab("files")
        if section.collapsed:
            self.diff.toggle_section(section, collapsed=False)
        self.diff.jump_to_section(section)
        self.diff.focus()

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def _people_here(self) -> list[str]:
        """Who `@` renames: the people in whatever is selected."""
        pr = self.pr

        def authors(thread: ReviewThread) -> list[str]:
            return list(dict.fromkeys(c.author for c in thread.comments))

        if self.tab == "conversation":
            item = self.conversation.focused_item
            if item is None or item.kind == "description":
                return [pr.author, *ConversationView.reviewer_logins(pr)]
            if isinstance(item.obj, ReviewThread):
                return authors(item.obj)
            return [getattr(item.obj, "author", pr.author)]
        thread = self.diff.active_thread
        if thread is not None:
            return authors(thread)
        return [pr.author]

    def action_nicknames(self) -> None:
        self.edit_nicknames()

    @work(group="nicknames")
    async def edit_nicknames(self) -> None:
        if not self.session.loaded:
            return
        people = self._people_here()
        result = await self.app.push_screen_wait(NicknameDialog(people, setting("nicknames")))
        if result is None:
            return
        update_nicknames(result)
        self.refresh_names()
        self.notify("Nickname saved" if len(result) == 1 else "Nicknames saved", timeout=1.5)

    def refresh_names(self) -> None:
        """Re-render everything that shows people's names."""
        diff = self.diff
        diff._thread_cache.clear()
        diff.refresh()
        self.conversation.refresh_cards()
        self.header.refresh()
        self.update_status()

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

    def _ready(self, write: bool = False) -> bool:
        if not self.session.loaded:
            self.notify("Still loading…", timeout=1.5)
            return False
        if write and not self.session.fresh:
            self.notify("Syncing with GitHub — one moment…", timeout=1.5)
            if not self._syncing:
                self.refresh_pr(quiet=True)
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
        self._last_write = time.monotonic()
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
        if not self._ready(write=True):
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
        if not self._ready(write=True) or self.tab != "files":
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
        if self.since is not None:
            anchors = [selection.anchor(line) for line in selection.lines]
            if any(side is Side.LEFT for side, _ in anchors):
                self.notify(
                    "Old lines here are from your last review, not the PR's base: press L "
                    "to comment on them in the full diff",
                    severity="warning",
                    timeout=4,
                )
                return
            if not self._in_pull_request_diff(path, anchors):
                self.notify(
                    "Those lines aren't part of the pull request's diff, so GitHub won't take "
                    "a comment there",
                    severity="warning",
                    timeout=4,
                )
                return
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
        if not self._ready(write=True):
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
            context.append(
                display_name(last.author), p.style(p.author_color(last.author), bold=True)
            )
            context.append(": ")
            excerpt = " ".join(last.body.split())
            context.append(
                excerpt[:300] + ("…" if len(excerpt) > 300 else ""), p.style(p.muted, italic=True)
            )
        pending = self.pr.pending_review is not None
        result: EditorResult | None = await self.app.push_screen_wait(
            CommentEditor(
                f"Reply to {display_name(root.author) if root else 'thread'}",
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
        if self._ready(write=True):
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
        if not self._ready(write=True):
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

    def action_ignore_comment(self) -> None:
        if not self._ready():
            return
        target = self._current_comment()
        if target is None:
            self.notify("Move to a comment to ignore comments like it", timeout=2)
            return
        self.ignore_like(target)

    @work(group="edit")
    async def ignore_like(self, target: Comment | Review) -> None:
        rule = await self.app.push_screen_wait(IgnoreCommentDialog(target.author, target.body))
        if rule is None:
            return
        filters.add_comment_rule(rule)
        self.refresh_filters()
        self.notify(f"Ignoring {rule.describe()} · change it in settings (,)", timeout=4)

    def refresh_filters(self) -> None:
        """Re-show everything after the ignore rules changed."""
        if not self.session.loaded:
            return
        self.diff._thread_cache.clear()
        self._present(self.pr)

    def action_react(self) -> None:
        if not self._ready(write=True):
            return
        target: Comment | Review | PullRequest | None = self._current_comment()
        if self.tab == "conversation":
            item = self.conversation.focused_item
            if item is not None and item.kind == "description":
                target = self.pr
        if target is None:
            self.notify("Move to a comment to react to it", timeout=2)
            return
        self.react_to(target)

    @work(group="edit")
    async def react_to(self, target: Comment | Review | PullRequest) -> None:
        mine = {r.content for r in target.reactions if r.viewer_has_reacted}
        content = await self.app.push_screen_wait(ReactionPicker(mine))
        if content is None:
            return
        task = await self._optimistic(self.session.toggle_reaction(target, content))
        self.after_change(threads=False)
        self.diff.refresh()
        if await self._run("react", task):
            emoji = REACTION_EMOJI.get(content, content)
            added = task.result()
            self.notify(f"{emoji} added" if added else f"{emoji} removed", timeout=1.2)
        self.after_change(threads=False)
        self.diff.refresh()

    def action_edit(self) -> None:
        if not self._ready(write=True):
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
        if not self._ready(write=True):
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
        if not self._ready(write=True) or self.tab != "files":
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
        if section.is_orphan:
            self.notify("That file isn't part of the pull request's diff anymore", timeout=2)
            return
        viewed = not section.file.is_viewed
        target = self._pr_file(section)
        task = await self._optimistic(self.session.set_viewed(target, viewed))
        section.file.viewed = target.viewed
        diff = self.diff
        if viewed:
            section.collapsed = True
            diff.relayout()
            following = [
                s
                for s in diff.sections[section.index + 1 :] + diff.sections[: section.index]
                if not diff.is_hidden(s)
            ]
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
        """Show test (or generated) files; or hide them again, marking them as viewed."""
        if not self._ready():
            return
        diff = self.diff
        label, key = ("test", "T") if kind == "test" else ("generated", "X")
        matching = [s for s in diff.sections if kind in s.kinds]
        if not matching:
            self.notify(f"No {label} files in this pull request", timeout=2)
            return
        if kind in diff.hidden_kinds:
            diff.hidden_kinds.discard(kind)
            self._marked_kinds.discard(kind)
            for section in matching:
                section.force_visible = False
            diff.relayout()
            self.rebuild_tree()
            self.update_status()
            count = len(matching)
            self.notify(
                f"Showing {count} {label} file{'s' if count != 1 else ''} "
                f"({key} hides them again and marks them as viewed)",
                timeout=3,
            )
            return
        if not self._ready(write=True):
            return
        diff.hidden_kinds.add(kind)
        self._marked_kinds.add(kind)
        for section in matching:
            section.force_visible = False
            section.collapsed = True
        diff.relayout()
        self.rebuild_tree()
        self.mark_sections_viewed(matching, label, key)

    @work(group="viewed")
    async def mark_sections_viewed(self, sections: list[FileSection], label: str, key: str) -> None:
        files = [self._pr_file(s) for s in sections if not s.file.is_viewed and not s.is_orphan]
        ok = True
        if files:
            ok = await self._run("mark files as viewed", self.session.set_viewed_many(files, True))
        for section in sections:
            section.file.viewed = self._pr_file(section).viewed
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

    def _pr_file(self, section: FileSection) -> ChangedFile:
        """The pull request's own record of a file (the diff may show a comparison copy)."""
        return self.pr.file(section.path) or section.file

    def _mark_hidden_viewed(self) -> None:
        """After a refresh, newly pushed files of a kind you hid with T / X (which marks
        them viewed) get marked as viewed too. Kinds that are merely hidden by default
        are never marked: opening or refreshing a pull request doesn't write to GitHub."""
        diff = self.diff
        for kind in diff.hidden_kinds & self._marked_kinds:
            stale = [s for s in diff.sections if kind in s.kinds and not s.file.is_viewed]
            if stale:
                label, key = ("test", "T") if kind == "test" else ("generated", "X")
                self.mark_sections_viewed(stale, label, key)

    def action_submit(self, preset: str | None = None) -> None:
        if self._ready(write=True):
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
        self._syncing = True
        self.header.syncing = True
        self.header.refresh()
        try:
            pr = await self.session.refresh()
        except Exception as error:
            if not self.session.fresh:
                self.show_banner("offline")
            self.notify(str(error), title="Couldn't refresh", severity="error")
            return
        finally:
            self._syncing = False
            self.header.syncing = False
            self.header.refresh()
        self._acknowledged = None
        if self.since is not None:
            self.show_since_review(quiet=True)  # recompute against the new head
        else:
            self._present(pr)
        self.show_banner(None)
        self.prefetch()
        if not quiet:
            self.notify("Up to date", timeout=1.5)

    # -- noticing changes on GitHub -------------------------------------------------

    def show_banner(self, kind: str | None, detail: str = "") -> None:
        banner = self.banner
        banner.set_classes(kind or "")
        if kind is None and self.since is not None:
            kind = "since"
        if kind is None:
            banner.display = False
            return
        age = ""
        if self.session.cached_at:
            age = f" from {relative_time(datetime.fromtimestamp(self.session.cached_at, UTC))}"
        text = {
            "cached": f"◷ Showing the cached copy{age} · syncing with GitHub…",
            "offline": f"⚠ Couldn't reach GitHub · showing the cached copy{age} · R to retry",
            "changed": f"↻ Updated on GitHub: {detail} · press R to refresh",
            "since": self._since_text(),
        }[kind]
        banner.update(text)
        banner.display = True

    def _since_text(self) -> str:
        review = self.since
        if review is None:
            return ""
        when = relative_time(review.submitted_at)
        commit = (review.commit_oid or "")[:7]
        return f"⟲ Only changes since your last review ({when}, {commit}) · L shows everything"

    # -- AI check of the description (panc) --------------------------------------------

    def _show_ai(self, check: AiCheck | None, running: bool) -> None:
        self.conversation.set_ai_check(check, running)
        self.header.ai = check
        self.header.refresh()

    @work(exclusive=True, group="panc")
    async def check_description(self) -> None:
        """Run panc on the description, unless a cached result still fits it (<10% changed)."""
        session = self.session
        body = self.pr.body.strip()
        if not body:
            return
        check = self.ai_check
        if check is None and session.cache is not None:
            check = await asyncio.to_thread(session.cache.load_ai_check, session.ref)
        if check is not None:
            self.ai_check = check
            self._show_ai(check, running=False)
            stale = panc.changed_enough(check.text, body)
            retry_error = check.error is not None and time.time() - check.checked_at > 3600
            if not stale and not retry_error:
                return
        if not session.fresh:
            return  # wait for the sync with GitHub: the description may still change
        self._show_ai(check, running=True)
        result = await panc.check(body)
        self.ai_check = result
        if session.cache is not None:
            await asyncio.to_thread(session.cache.save_ai_check, session.ref, result)
        self._show_ai(result, running=False)
        if result.error:
            self.notify(result.error, title="panc", severity="warning", timeout=5)
        else:
            self.notify(f"{result.verdict.upper()}: {result.headline}", title="panc", timeout=4)

    # -- changes since your last review ----------------------------------------------

    def action_since_review(self) -> None:
        if not self._ready():
            return
        if self.since is not None:
            self.since = None
            self.since_files = None
            self.diff.since_mode = False
            self._present(self.pr)
            self.show_banner(None)
            self.notify("Showing all changes", timeout=1.5)
            return
        self.show_since_review()

    @work(exclusive=True, group="since")
    async def show_since_review(self, quiet: bool = False) -> None:
        try:
            result = await self.session.changes_since_last_review()
        except Exception as error:
            self.notify(
                str(error), title="Couldn't compare with your last review", severity="error"
            )
            return
        if result is None:
            self.notify("You haven't submitted a review on this pull request yet", timeout=3)
            return
        review, files = result
        if not files:
            if not quiet:
                self.notify("Nothing changed since your last review 🎉", timeout=3)
            if self.since is not None:
                self.since, self.since_files = None, None
                self.diff.since_mode = False
                self._present(self.pr)
                self.show_banner(None)
            return
        self.since, self.since_files = review, files
        self.diff.since_mode = True
        self.action_switch_tab("files")
        self._present(self.pr)
        self.show_banner(None)  # shows the "since" banner
        first = next((s for s in self.diff.sections if not self.diff.is_hidden(s)), None)
        if first is not None and not quiet:
            self.diff.jump_to_section(first)

    def _in_pull_request_diff(self, path: str, anchors: list[tuple[Side, int]]) -> bool:
        """Whether lines can be commented on: they must be part of the PR's own diff."""
        file = self.pr.file(path)
        if file is None or file.patch is None:
            return False
        valid: set[tuple[Side, int]] = set()
        for hunk in parse_patch(file.patch):
            for line in hunk.lines:
                if line.kind is not LineKind.ADD and line.old_no is not None:
                    valid.add((Side.LEFT, line.old_no))
                if line.kind is not LineKind.DEL and line.new_no is not None:
                    valid.add((Side.RIGHT, line.new_no))
        return all(anchor in valid for anchor in anchors)

    def on_click(self, event) -> None:
        widget = getattr(event, "widget", None)
        if widget is not None and widget.id == "banner" and self.session.loaded:
            self.action_refresh()

    @work(exclusive=True, group="check")
    async def check_for_changes(self) -> None:
        """Every so often, cheaply check whether the PR changed on GitHub."""
        session = self.session
        if not session.loaded or not session.fresh or self._syncing or self._busy:
            return
        if time.monotonic() - self._last_write < 10:
            return  # our own changes may still be settling on GitHub
        local = Fingerprint.of(session.pr)
        try:
            remote = await session.fingerprint()
        except Exception:
            return  # offline for a moment; try again next time
        if self._syncing or remote == self._acknowledged:
            return
        changes = remote.changes_since(local)
        if changes and filters.ignores().active:
            # Only announce what you'd actually see: compare with ignored comments left out.
            try:
                fresh = await session.backend.load_pull_request(session.ref, reuse=session.pr)
            except Exception:
                return
            shown = filters.ignores()
            changes = Fingerprint.of(shown.visible_pr(fresh)).changes_since(
                Fingerprint.of(shown.visible_pr(session.pr))
            )
            if not changes:
                self._acknowledged = remote  # nothing visible changed: don't look again
                return
        if changes:
            self.show_banner("changed", ", ".join(changes))

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
