"""The settings screen: every option in one place, saved as soon as it changes."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from revv import filters, panc
from revv.cache import default_cache_dir
from revv.config import config_path, ignored_prs, save_config, set_nicknames, setting
from revv.maintainers import MaintainerSettings, applies_to
from revv.ui.dialogs import NicknameDialog


def _size(path: Path) -> int:
    total = 0
    if path.exists():
        for file in path.rglob("*"):
            try:
                if file.is_file():
                    total += file.stat().st_size
            except OSError:
                continue
    return total


def _tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home + "/") else text


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class SettingsScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: $background 60%; }
    SettingsScreen > Vertical {
        width: 84; max-width: 96%; height: auto; max-height: 94%;
        background: $surface; border: round $primary; padding: 0 1;
        border-title-style: bold;
    }
    SettingsScreen VerticalScroll { height: auto; max-height: 40; }
    SettingsScreen .heading { color: $accent; text-style: bold; margin: 1 0 0 0; }
    SettingsScreen .row { height: auto; min-height: 3; }
    SettingsScreen .row Label { width: 1fr; padding: 1 1 0 0; }
    SettingsScreen .row Switch { width: auto; }
    SettingsScreen .row Select { width: 30; }
    SettingsScreen .row Input { width: 14; }
    SettingsScreen .row Button { min-width: 14; }
    SettingsScreen .note { color: $text-muted; padding: 0 0 0 1; height: auto; }
    SettingsScreen #ignored-labels { width: 30; }
    SettingsScreen #rules { height: auto; }
    SettingsScreen #add-rule Input { width: 1fr; }
    SettingsScreen #add-rule #rule-author { width: 20; }
    SettingsScreen #footer-note { color: $text-muted; margin: 1 0; height: auto; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        hidden = setting("hide_by_default")
        with Vertical() as box:
            box.border_title = "Settings"
            box.border_subtitle = "changes are saved right away · esc closes"
            with VerticalScroll():
                yield Static("Reviewing", classes="heading")
                yield self._switch(
                    "Hide test files when a pull request opens", "hide-tests", "test" in hidden
                )
                yield self._switch(
                    "Hide generated files when a pull request opens",
                    "hide-generated",
                    "generated" in hidden,
                )
                with Horizontal(classes="row"):
                    yield Label("Open pull requests on")
                    yield Select(
                        [("Conversation", "conversation"), ("Files", "files")],
                        value="files" if setting("open_tab") == "files" else "conversation",
                        allow_blank=False,
                        id="open-tab",
                    )
                with Horizontal(classes="row"):
                    yield Label("Check GitHub for changes every … seconds (0: never)")
                    yield Input(str(setting("refresh_interval")), type="integer", id="refresh")
                with Horizontal(classes="row"):
                    yield Label("File tree width (also < and >)")
                    yield Input(str(setting("sidebar_width")), type="integer", id="sidebar")

                yield Static("Inbox", classes="heading")
                with Horizontal(classes="row"):
                    yield Label("Order")
                    yield Select(
                        [("Oldest first", "asc"), ("Newest first", "desc")],
                        value="desc" if setting("inbox_sort") == "desc" else "asc",
                        allow_blank=False,
                        id="inbox-sort",
                    )
                with Horizontal(classes="row"):
                    yield Label(f"Ignored pull requests: {len(ignored_prs())}", id="ignored-label")
                    yield Button("Unignore all", id="clear-ignored")

                yield Static("Ignoring", classes="heading")
                with Horizontal(classes="row"):
                    yield Label("Labels to ignore (comma-separated, * matches anything)")
                    yield Input(
                        ", ".join(filters.ignores().label_patterns),
                        placeholder="e.g. wip, size/*",
                        id="ignored-labels",
                    )
                yield Static("Comments to ignore (i on a comment adds one too):", classes="note")
                yield Vertical(id="rules")
                with Horizontal(classes="row", id="add-rule"):
                    yield Input(placeholder="from (login)", id="rule-author")
                    yield Input(placeholder="containing these words", id="rule-text")
                    yield Button("Add", id="add-rule-button")

                if self._maintainers_here():
                    yield Static("Maintainer teams (VantaInc/obsidian only)", classes="heading")
                    yield Static(
                        "Built for the one repository where revv's author works, and on only "
                        "there. It may change without notice, and it's no use to anybody else.",
                        classes="note",
                    )
                    with Horizontal(classes="row"):
                        yield Label(self._teams_label(), id="teams-label")
                        yield Button("Forget and re-check", id="forget-teams")

                yield Static("People", classes="heading")
                with Horizontal(classes="row"):
                    yield Label(self._nickname_label(), id="nickname-label")
                    yield Button("Edit…", id="edit-nicknames")

                yield Static("AI-writing check", classes="heading")
                yield self._switch(
                    "Check pull request descriptions with panc", "panc", bool(setting("panc"))
                )
                program = panc.executable()
                note = (
                    f"panc found at {program}. It sends the description to Pangram, using your "
                    "PANGRAM_API_KEY; results are cached per pull request."
                    if program
                    else "panc isn't on your PATH: see https://github.com/adamtrain/panc"
                )
                yield Static(note, classes="note")

                yield Static("Appearance", classes="heading")
                with Horizontal(classes="row"):
                    yield Label("Theme (also in the command palette)")
                    themes = sorted(self.app.available_themes)
                    yield Select(
                        [(name, name) for name in themes],
                        value=self.app.theme if self.app.theme in themes else Select.BLANK,
                        id="theme",
                    )

                yield Static("Storage", classes="heading")
                with Horizontal(classes="row"):
                    yield Label("Cache: …", id="cache-label")
                    yield Button("Clear cache", id="clear-cache")
            yield Static(f"Saved in {_tilde(config_path())}", id="footer-note")

    def on_mount(self) -> None:
        self.measure_cache()
        self._show_rules()

    def _show_rules(self) -> None:
        container = self.query_one("#rules", Vertical)
        container.remove_children()
        rules = filters.ignores().rules
        if not rules:
            container.mount(Static("  none yet", classes="note"))
            return
        for index, rule in enumerate(rules):
            container.mount(
                Horizontal(
                    Label(f"  {rule.describe()}"),
                    Button("Remove", name=str(index), classes="remove-rule"),
                    classes="row",
                )
            )

    def _filters_changed(self) -> None:
        from revv.ui.inbox import InboxScreen
        from revv.ui.review import ReviewScreen

        for screen in self.app.screen_stack:
            if isinstance(screen, ReviewScreen):
                screen.refresh_filters()
            elif isinstance(screen, InboxScreen):
                screen._render_list()

    @on(Input.Changed, "#ignored-labels")
    def labels_changed(self, event: Input.Changed) -> None:
        filters.set_ignored_labels(event.value.split(","))
        self._filters_changed()

    @on(Button.Pressed, "#add-rule-button")
    def add_rule(self) -> None:
        author = self.query_one("#rule-author", Input).value.strip().lstrip("@")
        text = self.query_one("#rule-text", Input).value.strip()
        if not author and not text:
            self.notify("Enter a login, some words, or both", severity="warning", timeout=2)
            return
        filters.add_comment_rule(filters.CommentRule(author or None, text or None))
        self.query_one("#rule-author", Input).value = ""
        self.query_one("#rule-text", Input).value = ""
        self._show_rules()
        self._filters_changed()

    @on(Button.Pressed, ".remove-rule")
    def remove_rule(self, event: Button.Pressed) -> None:
        rules = filters.ignores().rules
        index = int(event.button.name or "-1")
        if 0 <= index < len(rules):
            filters.remove_comment_rule(rules[index])
            self._show_rules()
            self._filters_changed()

    @staticmethod
    def _switch(label: str, id: str, value: bool) -> Horizontal:
        return Horizontal(Label(label), Switch(value=value, id=id), classes="row")

    def _maintainers_here(self) -> bool:
        """Whether the maintainer-teams extra is on for anything open (it's on in only one
        repository, so everybody else never sees it)."""
        from revv.ui.inbox import InboxScreen
        from revv.ui.review import ReviewScreen

        for screen in self.app.screen_stack:
            if isinstance(screen, ReviewScreen) and applies_to(screen.session.ref.repo):
                return True
            if isinstance(screen, InboxScreen) and any(
                applies_to(item.ref.repo) for section in screen.sections for item in section.items
            ):
                return True
        return False

    def _teams_label(self) -> str:
        cache = getattr(self.app, "cache", None)
        teams = cache.cached_teams() if cache is not None else []
        extra = MaintainerSettings.load().my_teams
        known = sorted({*teams, *extra}, key=str.lower)
        if not known:
            return "Your teams: not known yet (checked when a pull request opens)"
        return "Your teams: " + ", ".join(known) + " (from GitHub, re-checked daily)"

    @on(Button.Pressed, "#forget-teams")
    def forget_teams(self) -> None:
        cache = getattr(self.app, "cache", None)
        if cache is not None:
            cache.clear_teams()
        from revv.ui.inbox import InboxScreen
        from revv.ui.review import ReviewScreen

        for screen in self.app.screen_stack:
            if isinstance(screen, (ReviewScreen, InboxScreen)):
                screen.recheck_teams()
        self.query_one("#teams-label", Label).update(self._teams_label())
        self.notify("Forgot your cached teams; checking GitHub again", timeout=2)

    @staticmethod
    def _nickname_label() -> str:
        count = len(setting("nicknames"))
        return f"Nicknames: {count}" + ("" if count else "  (@ on a person adds one)")

    # -- changes ---------------------------------------------------------------------

    @on(Switch.Changed)
    def switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id in ("hide-tests", "hide-generated"):
            kinds = []
            if self.query_one("#hide-tests", Switch).value:
                kinds.append("test")
            if self.query_one("#hide-generated", Switch).value:
                kinds.append("generated")
            save_config(hide_by_default=kinds)
            self.notify("Applies to pull requests you open from now on", timeout=2)
        elif event.switch.id == "panc":
            save_config(panc=event.value)
            if event.value and panc.executable() is None:
                self.notify("panc isn't installed, so nothing will run yet", severity="warning")

    @on(Select.Changed)
    def select_changed(self, event: Select.Changed) -> None:
        value = event.value
        if value is Select.BLANK:
            return
        if event.select.id == "open-tab":
            save_config(open_tab=value)
        elif event.select.id == "inbox-sort":
            save_config(inbox_sort=value)
            from revv.ui.inbox import InboxScreen

            for screen in self.app.screen_stack:
                if isinstance(screen, InboxScreen) and screen.sort != value:
                    screen.action_toggle_sort()
        elif event.select.id == "theme" and isinstance(value, str):
            self.app.theme = value  # saved by the app

    @on(Input.Changed, "#refresh")
    def refresh_changed(self, event: Input.Changed) -> None:
        try:
            seconds = max(0, int(event.value))
        except ValueError:
            return
        save_config(refresh_interval=seconds)

    @on(Input.Changed, "#sidebar")
    def sidebar_changed(self, event: Input.Changed) -> None:
        try:
            width = int(event.value)
        except ValueError:
            return
        if not 16 <= width <= 120:
            return
        save_config(sidebar_width=width)
        from revv.ui.review import ReviewScreen

        for screen in self.app.screen_stack:
            if isinstance(screen, ReviewScreen):
                screen._apply_sidebar_width(width)

    @on(Button.Pressed, "#clear-ignored")
    def clear_ignored(self) -> None:
        save_config(ignored_prs={})
        from revv.ui.inbox import InboxScreen

        for screen in self.app.screen_stack:
            if isinstance(screen, InboxScreen):
                screen.ignored.clear()
                screen._update_tabs()
                screen._render_list()
        self.query_one("#ignored-label", Label).update("Ignored pull requests: 0")
        self.notify("Every ignored pull request is back", timeout=2)

    @on(Button.Pressed, "#edit-nicknames")
    def edit_nicknames(self) -> None:
        self._edit_nicknames()

    @work
    async def _edit_nicknames(self) -> None:
        names = setting("nicknames")
        if not names:
            self.notify("No nicknames yet: press @ on a person to add one", timeout=3)
            return
        result = await self.app.push_screen_wait(NicknameDialog(list(names), names))
        if result is None:
            return
        set_nicknames({login: name for login, name in result.items() if name})
        self.query_one("#nickname-label", Label).update(self._nickname_label())
        from revv.ui.review import ReviewScreen

        for screen in self.app.screen_stack:
            if isinstance(screen, ReviewScreen):
                screen.refresh_names()

    @on(Button.Pressed, "#clear-cache")
    def clear_cache(self) -> None:
        cache = getattr(self.app, "cache", None)
        if cache is None:
            self.notify("The cache is off in this session", timeout=2)
            return
        self._clear_cache(cache)

    @work(thread=True)
    def _clear_cache(self, cache) -> None:
        import shutil

        shutil.rmtree(cache.root, ignore_errors=True)
        self.app.call_from_thread(self.notify, "Cache cleared", timeout=2)
        self.app.call_from_thread(self.measure_cache)

    @work(thread=True, exclusive=True, group="measure")
    def measure_cache(self) -> None:
        cache = getattr(self.app, "cache", None)
        root = cache.root if cache is not None else default_cache_dir()
        label = f"Cache: {_human(_size(root))} in {_tilde(root)}"
        if cache is None:
            label += " (off in this session)"
        self.app.call_from_thread(self.query_one("#cache-label", Label).update, label)

    def action_close(self) -> None:
        self.dismiss(None)
