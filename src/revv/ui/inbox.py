"""The review inbox: pull requests waiting for you, and a quick way to open any other."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import ClassVar

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.color import Color
from textual.events import Key
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from revv.backend import Backend
from revv.cache import DiskCache
from revv.config import (
    display_name,
    ignored_prs,
    save_config,
    set_ignored,
    setting,
    update_nicknames,
)
from revv.inbox import (
    InboxSection,
    carry_over_details,
    inbox_sections,
    load_details,
    load_section,
)
from revv.models import PRRef, PRSummary, RepoRef
from revv.targets import parse_pr_ref
from revv.ui.dialogs import NicknameDialog
from revv.ui.palette import Palette
from revv.ui.render import relative_time
from revv.ui.widgets import CHECK_MARKS, DECISIONS, StatusBar, tone

IGNORED = "ignored"


@dataclass(slots=True)
class Entry:
    """One row of the list: a pull request, possibly part of a stack."""

    item: PRSummary
    stack: str | None = None  # None, or "middle" / "last" member of a stack group


@dataclass(slots=True)
class StackHeader:
    """A stack of pull requests: folded into one row, or a header above its members."""

    item: PRSummary  # the stack's next pull request to review (the lowest unreviewed one)
    members: list[PRSummary]  # in stack order
    expanded: bool = False

    @property
    def stack_id(self) -> str:
        return self.item.stack_id or ""


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
    InboxScreen OptionList:focus { border: none; background-tint: transparent; }
    InboxScreen OptionList > .option-list--option { padding: 0 1; }
    InboxScreen OptionList > .option-list--option-highlighted {
        background: $primary 22%;
        text-style: none;
    }
    InboxScreen OptionList:focus > .option-list--option-highlighted {
        background: $primary 30%;
    }
    InboxScreen OptionList > .option-list--separator { color: $panel; }
    InboxScreen #empty { height: 1fr; content-align: center middle; color: $text-muted; display: none; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "app.quit", "Quit", show=False),
        Binding("slash", "focus_filter", "Filter", show=False),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("tab", "section(1)", "Next section", show=False),
        Binding("shift+tab", "section(-1)", "Previous section", show=False),
        Binding("right,l", "expand_stack", "Expand stack", show=False),
        Binding("left,h", "collapse_stack", "Collapse stack", show=False),
        Binding("j", "cursor(1)", "Down", show=False),
        Binding("k", "cursor(-1)", "Up", show=False),
        Binding("r,R", "reload", "Refresh", show=False),
        Binding("o", "open_browser", "Open in browser", show=False),
        Binding("a", "toggle_scope", "All repos", show=False),
        Binding("s", "toggle_sort", "Sort", show=False),
        Binding("i", "toggle_ignored", "Ignore", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("at", "nicknames", "Nicknames", show=False),
    ]

    class OpenPullRequest(Message):
        def __init__(self, ref: PRRef) -> None:
            super().__init__()
            self.ref = ref

    def __init__(
        self,
        backend: Backend,
        repo: RepoRef | None,
        *,
        all_repos: bool = False,
        cache: DiskCache | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.repo = repo
        self.all_repos = all_repos or repo is None
        self.cache = cache
        self.sections: list[InboxSection] = []
        self.current = "requested"
        self.sort = "desc" if setting("inbox_sort") == "desc" else "asc"
        self.ignored = set(ignored_prs())
        self._loaded_once = False
        self._status = ""
        self._filtering = False  # the user asked for the filter box (/ or typing a number)
        self.expanded_stacks: set[str] = set()
        self._stack_focus: dict[str, PRSummary] = {}
        self._pending_highlight: str | None = None

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
        self._show_hints()
        self.reload()
        self.set_interval(60, self._auto_reload)

    def _show_hints(self) -> None:
        hints = [
            ("↵", "review"),
            ("/", "filter or #number"),
            ("tab", "section"),
            ("→ ←", "stack"),
            ("s", "newest first" if self.sort == "asc" else "oldest first"),
            ("i", "unignore" if self.current == IGNORED else "ignore"),
            ("o", "browser"),
            ("r", "refresh"),
        ]
        if self.repo:
            hints.append(("a", "this repo" if self.all_repos else "all repos"))
        hints += [("@", "nicknames"), ("q", "quit")]
        self.query_one(StatusBar).show(hints)

    def _auto_reload(self) -> None:
        if self.is_current and self._loaded_once:
            self.reload(quiet=True)

    def on_screen_resume(self) -> None:
        if self._loaded_once:
            self.reload(quiet=True)

    # -- sections and caching --------------------------------------------------------

    @property
    def _cache_key(self) -> tuple[str, str]:
        host = self.repo.host if self.repo else self.backend.host
        return host, self.scope.full_name if self.scope else ""

    def _setup_sections(self) -> None:
        self.sections = inbox_sections(self.scope)
        cached = self.cache.load_inbox(*self._cache_key) if self.cache else None
        if isinstance(cached, dict):  # show the last known inbox right away
            for section in self.sections:
                items = cached.get(section.key)
                if isinstance(items, list):
                    section.items = items
                    section.loaded = True
        tabs = self.query_one(Tabs)
        if not tabs.tab_count:  # the tabs are the same for every scope: create them once
            for section in self.sections:
                tabs.add_tab(Tab(section.title, id=section.key))
            tabs.add_tab(Tab("Ignored", id=IGNORED))
        if self.current not in [s.key for s in self.sections] + [IGNORED]:
            self.current = self.sections[0].key
        tabs.active = self.current
        self._update_tabs()
        self._update_title()
        self._render_list()

    def _update_tabs(self) -> None:
        tabs = self.query_one(Tabs)
        for section in self.sections:
            if section.error:
                label = f"{section.title} !"
            elif not section.loaded:
                label = section.title
            else:
                count = sum(1 for i in section.items if i.key not in self.ignored)
                label = f"{section.title} {count}"
            tabs.query_one(f"#{section.key}", Tab).label = label
        ignored = len(self._ignored_items())
        tabs.query_one(f"#{IGNORED}", Tab).label = f"Ignored {ignored}"
        if ignored or self.current == IGNORED:
            tabs.show(IGNORED)
        else:
            tabs.hide(IGNORED)

    def _ignored_items(self) -> list[PRSummary]:
        seen: dict[str, PRSummary] = {}
        for section in self.sections:
            for item in section.items:
                if item.key in self.ignored:
                    seen.setdefault(item.key, item)
        return list(seen.values())

    def _save_cache(self) -> None:
        if self.cache is None:
            return
        cache, key = self.cache, self._cache_key
        value = {section.key: section.items for section in self.sections if section.loaded}
        self.run_worker(
            lambda: cache.save_inbox(*key, value),
            thread=True,
            group="inbox-cache",
            exit_on_error=False,
        )

    def _update_title(self) -> None:
        p = Palette.from_app(self.app)
        text = Text()
        text.append("revv ", p.style(p.primary_fg, bold=True))
        text.append("review inbox", p.style(p.text, bold=True))
        text.append("  ·  ", p.style(p.faint))
        scope = self.scope.full_name if self.scope else "all repositories"
        text.append(scope, p.style(p.muted))
        right = Text()
        right.append("oldest first" if self.sort == "asc" else "newest first", p.style(p.faint))
        if self._status:
            right.append(f"  {self._status}", p.style(p.faint))
        width = self.query_one("#title", Static).content_region.width or 80
        text.pad_right(max(1, width - text.cell_len - right.cell_len))
        text.append_text(right)
        self.query_one("#title", Static).update(text)

    def _set_status(self, status: str) -> None:
        self._status = status
        self._update_title()

    @work(exclusive=True, group="inbox")
    async def reload(self, quiet: bool = False) -> None:
        """Refresh every section; each one is shown as soon as its search returns, and the
        slower details (size, checks, reviewers) are filled in afterwards."""
        sections = self.sections
        options = self.query_one(OptionList)
        if not quiet and not any(s.loaded for s in sections):
            options.loading = True
        self._set_status("↻ refreshing…")

        async def refresh(section: InboxSection) -> None:
            previous = {item.ref: item for item in section.items}
            await load_section(self.backend, section)
            carry_over_details(previous, section.items)
            if sections is self.sections:
                self._update_tabs()
                if section.key == self.current or self.current == IGNORED:
                    options.loading = False
                    self._render_list()

        await asyncio.gather(*(refresh(section) for section in sections))
        options.loading = False
        self._loaded_once = True
        if sections is not self.sections:
            return  # the scope changed meanwhile
        with contextlib.suppress(Exception):  # rows are still useful without the details
            await load_details(self.backend, sections)
        if sections is not self.sections:
            return
        self._render_list()
        self._save_cache()
        self._set_status("")

    # -- the list --------------------------------------------------------------------

    @property
    def section(self) -> InboxSection | None:
        return next((s for s in self.sections if s.key == self.current), None)

    def _items(self) -> tuple[list[PRSummary], bool]:
        """Rows for the current tab, and whether that tab has loaded."""
        if self.current == IGNORED:
            return self._ignored_items(), all(s.loaded for s in self.sections)
        section = self.section
        if section is None:
            return [], False
        return [i for i in section.items if i.key not in self.ignored], section.loaded

    def _matches(self, item: PRSummary, query: str) -> bool:
        if not query:
            return True
        haystack = " ".join(
            [
                f"#{item.ref.number}",
                item.title,
                item.author,
                display_name(item.author),
                item.head_ref,
                item.ref.repo.full_name,
                *item.requested_teams,
                *(label.name for label in item.labels),
            ]
        ).lower()
        return all(word in haystack for word in query.lower().split())

    def _arrange(self, items: list[PRSummary]) -> list[Entry | StackHeader]:
        """Sort by number; a stack becomes one row (or a header and its members, in stack
        order, once expanded)."""
        ordered = sorted(items, key=lambda i: i.ref.number, reverse=self.sort == "desc")
        stacks: dict[str, list[PRSummary]] = {}
        for item in ordered:
            if item.stack_id and item.stack_size > 1:
                stacks.setdefault(item.stack_id, []).append(item)
        rows: list[Entry | StackHeader] = []
        placed: set[str] = set()
        self._stack_focus = {}
        for item in ordered:
            stack = item.stack_id if item.stack_id in stacks else None
            if stack is None:
                rows.append(Entry(item))
                continue
            if stack in placed:
                continue
            placed.add(stack)
            members = sorted(stacks[stack], key=lambda i: i.stack_position)
            focus = next((m for m in members if not m.my_review_state), members[0])
            self._stack_focus[stack] = focus
            expanded = stack in self.expanded_stacks
            rows.append(StackHeader(focus, members, expanded))
            if expanded:
                for index, member in enumerate(members):
                    kind = "last" if index == len(members) - 1 else "middle"
                    rows.append(Entry(member, kind))
        return rows

    def _render_list(self) -> None:
        options = self.query_one(OptionList)
        empty = self.query_one("#empty", Static)
        highlighted = options.highlighted_option.id if options.highlighted_option else None
        options.clear_options()
        section = self.section
        query = self.query_one(Input).value.strip()
        ref = parse_pr_ref(query, self.repo) if query else None
        all_items, loaded = self._items()
        items = [i for i in all_items if self._matches(i, query)] if ref is None else []
        if section is not None and section.error:
            empty.update(Text(f"Couldn't load: {section.error}", style="bold red"))
        elif ref is not None:
            empty.update(Text.assemble("Press ", ("enter", "bold"), f" to open {ref}"))
        elif not all_items and loaded:
            messages = {
                "requested": "Nothing is waiting for your review 🎉",
                "reviewed": "No open pull requests that you have reviewed",
                "mine": "You have no open pull requests",
                IGNORED: "No ignored pull requests",
            }
            empty.update(Text(messages.get(self.current, "Nothing here"), style="italic"))
        elif not items and loaded:
            empty.update(Text("No matches", style="italic"))
        else:
            empty.update("")
        empty.display = not items and (loaded or ref is not None)
        options.display = bool(items)
        if items and not self._filtering and not options.has_focus:
            options.focus()  # the list couldn't take focus while it was empty
        rows = self._arrange(items)
        entries: list[Option | None] = []
        ids: list[str] = []
        for row in rows:
            if isinstance(row, StackHeader):
                if entries:
                    entries.append(None)
                option_id = f"stack:{row.stack_id}"
                entries.append(Option(self._stack_prompt(row), id=option_id))
                ids.append(option_id)
                continue
            if row.stack is None and entries:
                entries.append(None)
            entries.append(Option(self._prompt(row), id=row.item.key))
            ids.append(row.item.key)
        options.add_options(entries)
        if ids:
            target = self._pending_highlight or highlighted
            self._pending_highlight = None
            if target not in ids:
                # a pull request folded into its stack: highlight the stack
                item = self._find(target) if target else None
                stack = f"stack:{item.stack_id}" if item and item.stack_id else None
                target = stack if stack in ids else ids[0]
            with contextlib.suppress(Exception):
                options.highlighted = options.get_option_index(target)

    def _stack_prompt(self, header: StackHeader) -> RenderableType:
        p = Palette.from_app(self.app)
        item = header.item
        line = Table.grid(expand=True, padding=(0, 1))
        line.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        line.add_column(justify="right", no_wrap=True)
        text = Text()
        text.append("▾ " if header.expanded else "▸ ", p.style(p.accent_fg, bold=True))
        text.append("▤ ", p.style(p.accent_fg, bold=True))
        name = f"Stack #{item.stack_number}" if item.stack_number else "Stack"
        text.append(name, p.style(p.accent_fg, bold=True))
        text.append(f" · {item.stack_size} pull requests", p.style(p.muted))
        if len(header.members) < item.stack_size:
            text.append(f" ({len(header.members)} here)", p.style(p.faint))
        hint = Text("← collapse" if header.expanded else "→ expand", p.style(p.faint, italic=True))
        line.add_row(text, hint)
        if header.expanded:
            return line
        return Group(line, self._prompt(Entry(item, "folded")))

    def _prompt(self, entry: Entry) -> Table:
        p = Palette.from_app(self.app)
        item = entry.item
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=2, no_wrap=True)
        table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        table.add_column(justify="right", no_wrap=True)

        # stack connectors in the gutter
        connector = p.style(p.accent_fg)
        if entry.stack is None or entry.stack == "folded":
            gutters = [Text(""), Text(""), Text("")]
        else:
            more = entry.stack != "last"
            gutters = [
                Text("├─" if more else "└─", connector),
                Text("│ " if more else "  ", connector),
                Text("│ " if more else "  ", connector),
            ]

        title = Text()
        title.append(f"#{item.ref.number}  ", p.style(p.primary_fg, bold=True))
        title.append(item.title, p.style(p.muted if item.is_draft else p.text, bold=True))
        if item.is_draft:
            title.append("  draft", p.style(p.faint, italic=True))

        reason = Text()
        if item.stack_position and item.stack_size > 1:
            position = f" {item.stack_position}/{item.stack_size} "
            reason.append(position, p.style(p.accent_fg, p.mix(p.accent, 0.18)))
            reason.append(" ")
        if self.current == "requested":
            if not item.details_loaded:
                reason.append(" requested ", p.style(p.bg, p.fg_mix(0.5), bold=True))
            elif item.requested_directly and item.my_review_state:
                reason.append(" re-review ", p.style(p.bg, p.accent, bold=True))
            elif item.requested_directly:
                reason.append(" requested: you ", p.style(p.bg, p.warning, bold=True))
            elif item.requested_teams:
                teams = ", ".join(item.requested_teams[:2])
                if len(item.requested_teams) > 2:
                    teams += f" +{len(item.requested_teams) - 2}"
                reason.append(f" team: {teams} ", p.style(p.bg, p.primary, bold=True))
            elif item.assigned:
                reason.append(" assigned ", p.style(p.bg, p.primary, bold=True))

        who = Text()
        if self.scope is None:
            who.append(item.ref.repo.full_name, p.style(p.muted))
            who.append(" · ", p.style(p.faint))
        who.append(display_name(item.author), p.style(p.author_color(item.author), bold=True))
        if item.created_at is not None:
            who.append(f" opened {relative_time(item.created_at)}", p.style(p.muted))
        who.append(f" · updated {relative_time(item.updated_at)}", p.style(p.faint))

        checks = Text()
        if item.checks_state in CHECK_MARKS:
            label, color = CHECK_MARKS[item.checks_state]
            checks.append(label, p.style(tone(p, color)))

        detail = Text()
        if item.head_ref:
            detail.append(item.head_ref, p.style(p.primary_fg))
            if item.base_ref:
                detail.append(" → ", p.style(p.faint))
                detail.append(item.base_ref, p.style(p.muted))
        if item.details_loaded:
            detail.append(f"   +{item.additions}", p.style(p.add_fg))
            detail.append(f" −{item.deletions}", p.style(p.del_fg))
            if item.changed_files:
                files = item.changed_files
                detail.append(f" · {files} file{'s' if files != 1 else ''}", p.style(p.muted))
            if item.comments:
                detail.append(
                    f" · {item.comments} comment{'s' if item.comments != 1 else ''}",
                    p.style(p.muted),
                )
        for label in item.labels[:4]:
            detail.append("  ")
            detail.append_text(self._label(label.name, label.color, p))

        status = Text()
        if item.my_review_state:
            mine = {
                "APPROVED": ("✓ you approved", "success"),
                "CHANGES_REQUESTED": ("✗ you requested changes", "error"),
                "COMMENTED": ("you commented", "muted"),
                "DISMISSED": ("your review was dismissed", "muted"),
            }.get(item.my_review_state)
            if mine:
                style = p.style(tone(p, mine[1])) if mine[1] != "muted" else p.style(p.muted)
                status.append(mine[0], style)
        elif item.review_decision in DECISIONS:
            label, color = DECISIONS[item.review_decision]
            status.append(label, p.style(tone(p, color)))

        table.add_row(gutters[0], title, reason)
        table.add_row(gutters[1], who, checks)
        table.add_row(gutters[2], detail, status)
        return table

    @staticmethod
    def _label(name: str, color: str, p: Palette) -> Text:
        try:
            base = Color.parse("#" + color)
        except Exception:
            base = p.primary
        background = p.bg.blend(base, 0.35)
        return Text(f" {name} ", p.style(p.bg.blend(base, 0.95).blend(p.fg, 0.35), background))

    # -- events ----------------------------------------------------------------------

    @on(Tabs.TabActivated)
    def tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab.id and event.tab.id != self.current:
            self.current = event.tab.id
            self._show_hints()
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
                self._activate(options.highlighted_option)

    @on(OptionList.OptionSelected)
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        self._activate(event.option)

    def _activate(self, option: Option) -> None:
        option_id = option.id or ""
        if option_id.startswith("stack:"):  # enter on a stack folds or unfolds it
            stack = option_id.removeprefix("stack:")
            if stack in self.expanded_stacks:
                self._collapse(stack)
            else:
                self._expand(stack)
            return
        self._open_option(option)

    def _expand(self, stack: str) -> None:
        self.expanded_stacks.add(stack)
        focus = self._stack_focus.get(stack)
        self._pending_highlight = focus.key if focus else None
        self._render_list()

    def _collapse(self, stack: str) -> None:
        self.expanded_stacks.discard(stack)
        self._pending_highlight = f"stack:{stack}"
        self._render_list()

    def _highlighted_stack(self) -> str | None:
        """The stack of the highlighted row (its header, or one of its members)."""
        option = self.query_one(OptionList).highlighted_option
        if option is None or option.id is None:
            return None
        if option.id.startswith("stack:"):
            return option.id.removeprefix("stack:")
        item = self._find(option.id)
        if item is not None and item.stack_id in self.expanded_stacks:
            return item.stack_id
        return None

    def action_expand_stack(self) -> None:
        stack = self._highlighted_stack()
        if stack is not None and stack not in self.expanded_stacks:
            self._expand(stack)

    def action_collapse_stack(self) -> None:
        stack = self._highlighted_stack()
        if stack is not None and stack in self.expanded_stacks:
            self._collapse(stack)

    def _find(self, option_id: str | None) -> PRSummary | None:
        if option_id is None:
            return None
        if option_id.startswith("stack:"):  # a folded stack stands for its next PR
            return self._stack_focus.get(option_id.removeprefix("stack:"))
        items, _ = self._items()
        return next((i for i in items if i.key == option_id), None)

    def _highlighted(self) -> PRSummary | None:
        option = self.query_one(OptionList).highlighted_option
        return self._find(option.id if option else None)

    def _open_option(self, option: Option) -> None:
        item = self._find(option.id)
        if item is not None:
            self.open(item.ref)

    def open(self, ref: PRRef) -> None:
        self.post_message(self.OpenPullRequest(ref))

    def on_key(self, event: Key) -> None:
        # Typing a digit anywhere starts "go to PR number"
        filter_input = self.query_one(Input)
        if event.character and event.character.isdigit() and not filter_input.has_focus:
            self._filtering = True
            filter_input.value += event.character
            filter_input.cursor_position = len(filter_input.value)
            filter_input.focus()
            event.stop()

    # -- actions ---------------------------------------------------------------------

    def action_focus_filter(self) -> None:
        self._filtering = True
        self.query_one(Input).focus()

    def action_clear_filter(self) -> None:
        self._filtering = False
        filter_input = self.query_one(Input)
        if filter_input.value:
            filter_input.value = ""
        self.query_one(OptionList).focus()

    def action_section(self, delta: int) -> None:
        if self.query_one(Input).has_focus:
            self._filtering = False
            self.query_one(OptionList).focus()
        keys = [s.key for s in self.sections]
        if self._ignored_items() or self.current == IGNORED:
            keys.append(IGNORED)
        index = (keys.index(self.current) + delta) % len(keys) if self.current in keys else 0
        self.query_one(Tabs).active = keys[index]

    def action_cursor(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if delta > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_reload(self) -> None:
        self.reload()

    def action_toggle_sort(self) -> None:
        self.sort = "desc" if self.sort == "asc" else "asc"
        save_config(inbox_sort=self.sort)
        self._update_title()
        self._show_hints()
        self._render_list()

    def action_toggle_ignored(self) -> None:
        item = self._highlighted()
        if item is None:
            return
        ignore = item.key not in self.ignored
        set_ignored(item.key, ignore)
        if ignore:
            self.ignored.add(item.key)
            self.notify(
                f"Ignoring #{item.ref.number} (it's in the Ignored tab; i there brings it back)",
                timeout=3,
            )
        else:
            self.ignored.discard(item.key)
            self.notify(f"#{item.ref.number} is back in the list", timeout=2)
        self._update_tabs()
        if self.current == IGNORED and not self._ignored_items():
            self.query_one(Tabs).active = self.sections[0].key
        self._render_list()

    def action_toggle_scope(self) -> None:
        if self.repo is None:
            self.notify("Not in a GitHub repository, so the inbox covers all repositories")
            return
        self.all_repos = not self.all_repos
        self._set_status("")
        self._setup_sections()
        self._show_hints()
        self.reload()

    def action_open_browser(self) -> None:
        item = self._highlighted()
        if item is not None:
            self.app.open_url(item.ref.web_url)

    def action_nicknames(self) -> None:
        self.edit_nicknames()

    @work(group="nicknames")
    async def edit_nicknames(self) -> None:
        item = self._highlighted()
        if item is None:
            self.notify("Highlight a pull request to nickname its author", timeout=2)
            return
        result = await self.app.push_screen_wait(
            NicknameDialog([item.author], setting("nicknames"))
        )
        if result is not None:
            update_nicknames(result)
            self._render_list()
            self.notify("Nickname saved", timeout=1.5)

    def action_help(self) -> None:
        self.notify(
            "enter: review · type a number or URL to open any PR · tab: switch section · "
            "s: sort · i: ignore · a: all repos · o: browser · r: refresh · @: nicknames · q: quit",
            title="Inbox",
            timeout=6,
        )
