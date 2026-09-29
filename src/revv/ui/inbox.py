"""The review inbox: pull requests waiting for you, and a quick way to open any other."""

from __future__ import annotations

from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.events import Key
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from revv.backend import Backend
from revv.inbox import InboxSection, inbox_sections, load_inbox
from revv.models import PRRef, PRSummary, RepoRef
from revv.targets import parse_pr_ref
from revv.ui.palette import Palette
from revv.ui.render import relative_time
from revv.ui.widgets import CHECK_MARKS, DECISIONS, StatusBar, tone


class InboxScreen(Screen):
    DEFAULT_CSS = """
    InboxScreen { layout: vertical; }
    InboxScreen #title { height: 1; background: $panel; padding: 0 1; }
    InboxScreen #filter { margin: 1 1 0 1; }
    InboxScreen Tabs { margin: 0 1; }
    InboxScreen OptionList {
        height: 1fr; margin: 0 1; border: none; padding: 0;
        background: $background;
    }
    InboxScreen OptionList > .option-list--option { padding: 0 1; }
    InboxScreen #empty { height: 1fr; content-align: center middle; color: $text-muted; display: none; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "app.quit", "Quit", show=False),
        Binding("slash", "focus_filter", "Filter", show=False),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("tab,l,right", "section(1)", "Next section", show=False, priority=False),
        Binding("shift+tab,h,left", "section(-1)", "Previous section", show=False),
        Binding("j", "cursor(1)", "Down", show=False),
        Binding("k", "cursor(-1)", "Up", show=False),
        Binding("r,R", "reload", "Refresh", show=False),
        Binding("o", "open_browser", "Open in browser", show=False),
        Binding("a", "toggle_scope", "All repos", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self, backend: Backend, repo: RepoRef | None, *, all_repos: bool = False) -> None:
        super().__init__()
        self.backend = backend
        self.repo = repo
        self.all_repos = all_repos or repo is None
        self.sections: list[InboxSection] = []
        self.current = "requested"
        self._loaded_once = False

    @property
    def scope(self) -> RepoRef | None:
        return None if self.all_repos else self.repo

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        yield Input(
            placeholder="Filter, or type a PR number / URL and press enter",
            id="filter",
            select_on_focus=False,
        )
        yield Tabs(id="sections")
        yield OptionList(id="prs")
        yield Static("", id="empty")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self._setup_sections()
        self.query_one(OptionList).focus()
        self.query_one(StatusBar).show(
            [
                ("↵", "review"),
                ("/", "filter or #number"),
                ("tab", "section"),
                ("o", "browser"),
                ("r", "refresh"),
            ]
            + ([("a", "this repo" if self.all_repos else "all repos")] if self.repo else [])
            + [("q", "quit")]
        )
        self.reload()

    def on_screen_resume(self) -> None:
        if self._loaded_once:
            self.reload(quiet=True)

    def _setup_sections(self) -> None:
        self.sections = inbox_sections(self.scope)
        tabs = self.query_one(Tabs)
        tabs.clear()
        for section in self.sections:
            tabs.add_tab(Tab(section.title, id=section.key))
        if not any(s.key == self.current for s in self.sections):
            self.current = self.sections[0].key
        tabs.active = self.current
        self._update_title()

    def _update_title(self) -> None:
        p = Palette.from_app(self.app)
        text = Text()
        text.append("revv ", p.style(p.primary_fg, bold=True))
        text.append("review inbox", p.style(p.text, bold=True))
        text.append("  ·  ", p.style(p.faint))
        scope = self.scope.full_name if self.scope else "all repositories"
        text.append(scope, p.style(p.muted))
        self.query_one("#title", Static).update(text)

    @work(exclusive=True, group="inbox")
    async def reload(self, quiet: bool = False) -> None:
        options = self.query_one(OptionList)
        if not quiet:
            options.loading = True
        sections = self.sections
        await load_inbox(self.backend, sections)
        options.loading = False
        self._loaded_once = True
        if sections is not self.sections:
            return  # scope changed meanwhile
        tabs = self.query_one(Tabs)
        for section in sections:
            label = (
                f"{section.title} {len(section.items)}"
                if not section.error
                else f"{section.title} !"
            )
            tab = tabs.query_one(f"#{section.key}", Tab)
            tab.label = label
        self._render_list()

    @property
    def section(self) -> InboxSection | None:
        return next((s for s in self.sections if s.key == self.current), None)

    def _matches(self, item: PRSummary, query: str) -> bool:
        if not query:
            return True
        haystack = " ".join(
            [
                f"#{item.ref.number}",
                item.title,
                item.author,
                item.head_ref,
                item.ref.repo.full_name,
                *item.requested_teams,
                *(label.name for label in item.labels),
            ]
        ).lower()
        return all(word in haystack for word in query.lower().split())

    def _render_list(self) -> None:
        section = self.section
        options = self.query_one(OptionList)
        empty = self.query_one("#empty", Static)
        highlighted = options.highlighted_option.id if options.highlighted_option else None
        options.clear_options()
        if section is None:
            return
        query = self.query_one(Input).value.strip()
        ref = parse_pr_ref(query, self.repo) if query else None
        items = [i for i in section.items if self._matches(i, query)] if ref is None else []
        if section.error:
            empty.update(Text(f"Couldn't load: {section.error}", style="bold red"))
        elif ref is not None:
            empty.update(Text.assemble("Press ", ("enter", "bold"), f" to open {ref}"))
        elif not section.items and section.loaded:
            messages = {
                "requested": "Nothing is waiting for your review 🎉",
                "reviewed": "No open pull requests that you have reviewed",
                "mine": "You have no open pull requests",
                "all": "No open pull requests",
            }
            empty.update(Text(messages.get(section.key, "Nothing here"), style="italic"))
        elif not items and section.loaded:
            empty.update(Text("No matches", style="italic"))
        else:
            empty.update("")
        empty.display = not items and (section.loaded or ref is not None)
        options.display = bool(items)
        options.add_options(
            [Option(self._prompt(item), id=self._option_id(item)) for item in items]
        )
        if items:
            ids = [self._option_id(i) for i in items]
            options.highlighted = ids.index(highlighted) if highlighted in ids else 0

    @staticmethod
    def _option_id(item: PRSummary) -> str:
        return f"{item.ref.repo.host}/{item.ref.repo.full_name}#{item.ref.number}"

    def _prompt(self, item: PRSummary) -> Table:
        p = Palette.from_app(self.app)
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        table.add_column(justify="right", no_wrap=True)

        title = Text()
        title.append(f"#{item.ref.number} ", p.style(p.primary_fg, bold=True))
        title.append(
            item.title, p.style(p.faint if item.is_draft else p.text, bold=not item.is_draft)
        )
        if item.is_draft:
            title.append(" draft", p.style(p.faint, italic=True))

        reason = Text()
        if item.requested_directly and item.my_review_state:
            reason.append(" re-review ", p.style(p.bg, p.accent, bold=True))
        elif item.requested_directly:
            reason.append(" requested: you ", p.style(p.bg, p.warning, bold=True))
        if item.requested_teams and not item.requested_directly:
            teams = ", ".join(item.requested_teams[:2])
            if len(item.requested_teams) > 2:
                teams += f" +{len(item.requested_teams) - 2}"
            reason.append(f" team: {teams} ", p.style(p.bg, p.primary, bold=True))

        meta = Text()
        meta.append(" " * (len(str(item.ref.number)) + 2))
        if self.scope is None:
            meta.append(item.ref.repo.full_name, p.style(p.muted))
            meta.append(" · ", p.style(p.faint))
        meta.append(item.author, p.style(p.author_color(item.author)))
        meta.append(f" · {relative_time(item.updated_at)}", p.style(p.muted))
        meta.append(f" · +{item.additions}", p.style(p.add_fg))
        meta.append(f" −{item.deletions}", p.style(p.del_fg))
        if item.comments:
            meta.append(
                f" · {item.comments} comment{'s' if item.comments != 1 else ''}", p.style(p.muted)
            )
        for label in item.labels[:3]:
            meta.append(f" · {label.name}", p.style(p.faint))

        status = Text()
        if item.checks_state in CHECK_MARKS:
            label, color = CHECK_MARKS[item.checks_state]
            status.append(label, p.style(tone(p, color)))
        if item.my_review_state:
            mine = {
                "APPROVED": ("you approved", "success"),
                "CHANGES_REQUESTED": ("you requested changes", "error"),
                "COMMENTED": ("you commented", "muted"),
                "DISMISSED": ("your review was dismissed", "muted"),
            }.get(item.my_review_state)
            if mine:
                status.append("  ")
                status.append(mine[0], p.style(tone(p, mine[1]) if mine[1] != "muted" else p.muted))
        elif item.review_decision in DECISIONS:
            label, color = DECISIONS[item.review_decision]
            status.append("  ")
            status.append(label, p.style(tone(p, color)))
        table.add_row(title, reason)
        table.add_row(meta, status)
        return table

    # -- events ----------------------------------------------------------------------

    @on(Tabs.TabActivated)
    def tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab.id and event.tab.id != self.current:
            self.current = event.tab.id
            self._render_list()

    @on(Input.Changed, "#filter")
    def filter_changed(self) -> None:
        self._render_list()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        query = self.query_one(Input).value.strip()
        ref = parse_pr_ref(query, self.repo) if query else None
        if ref is not None:
            self.open(ref)
            return
        options = self.query_one(OptionList)
        if options.option_count:
            options.focus()
            if options.highlighted_option is not None:
                self._open_option(options.highlighted_option)

    @on(OptionList.OptionSelected)
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        self._open_option(event.option)

    def _find(self, option_id: str | None) -> PRSummary | None:
        section = self.section
        if section is None or option_id is None:
            return None
        return next((i for i in section.items if self._option_id(i) == option_id), None)

    def _open_option(self, option: Option) -> None:
        item = self._find(option.id)
        if item is not None:
            self.open(item.ref)

    class OpenPullRequest(Message):
        def __init__(self, ref: PRRef) -> None:
            super().__init__()
            self.ref = ref

    def open(self, ref: PRRef) -> None:
        self.post_message(self.OpenPullRequest(ref))

    def on_key(self, event: Key) -> None:
        # Typing a digit anywhere starts "go to PR number"
        filter_input = self.query_one(Input)
        if event.character and event.character.isdigit() and not filter_input.has_focus:
            filter_input.value += event.character
            filter_input.cursor_position = len(filter_input.value)
            filter_input.focus()
            event.stop()

    # -- actions ---------------------------------------------------------------------

    def action_focus_filter(self) -> None:
        self.query_one(Input).focus()

    def action_clear_filter(self) -> None:
        filter_input = self.query_one(Input)
        if filter_input.value:
            filter_input.value = ""
        self.query_one(OptionList).focus()

    def action_section(self, delta: int) -> None:
        if self.query_one(Input).has_focus and delta in (1, -1):
            # keep tab behaviour usable inside the filter box
            self.query_one(OptionList).focus()
        keys = [s.key for s in self.sections]
        index = (keys.index(self.current) + delta) % len(keys)
        self.query_one(Tabs).active = keys[index]

    def action_cursor(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if delta > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_reload(self) -> None:
        self.reload()

    def action_toggle_scope(self) -> None:
        if self.repo is None:
            self.notify("Not in a GitHub repository, so the inbox covers all repositories")
            return
        self.all_repos = not self.all_repos
        self._setup_sections()
        self.query_one(StatusBar).show(
            [
                ("↵", "review"),
                ("/", "filter or #number"),
                ("tab", "section"),
                ("o", "browser"),
                ("r", "refresh"),
                ("a", "this repo" if self.all_repos else "all repos"),
                ("q", "quit"),
            ]
        )
        self.reload()

    def action_open_browser(self) -> None:
        options = self.query_one(OptionList)
        option = options.highlighted_option
        item = self._find(option.id if option else None)
        if item is not None:
            self.app.open_url(item.ref.web_url)

    def action_help(self) -> None:
        self.notify(
            "enter: review · type a number or URL to open any PR · tab: switch section · "
            "a: all repos · o: open in browser · r: refresh · q: quit",
            title="Inbox",
            timeout=6,
        )
