"""The Textual application."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from textual.app import App, SystemCommand
from textual.binding import Binding, BindingType
from textual.screen import Screen

from revv.backend import Backend
from revv.cache import DiskCache
from revv.config import load_config, save_config
from revv.models import PRRef, RepoRef
from revv.session import ReviewSession
from revv.ui.inbox import InboxScreen
from revv.ui.review import ReviewScreen


class RevvApp(App[str | None]):
    TITLE = "revv"
    CSS = """
    Screen { background: $background; }
    Toast { max-width: 70; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        backend: Backend,
        *,
        target: PRRef | None = None,
        repo: RepoRef | None = None,
        all_repos: bool = False,
        cache: DiskCache | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.cache = cache
        self.target = target
        self.repo = repo
        self.all_repos = all_repos
        config = load_config()
        theme = config.get("theme")
        if isinstance(theme, str) and theme in self.available_themes:
            self.theme = theme

    def on_mount(self) -> None:
        self.theme_changed_signal.subscribe(self, lambda theme: save_config(theme=theme.name))
        if self.target is not None:
            self.open_review(self.target, from_inbox=False)
        else:
            self.push_screen(
                InboxScreen(self.backend, self.repo, all_repos=self.all_repos, cache=self.cache)
            )
        if self.cache is not None:
            self.run_worker(self.cache.prune, thread=True, group="prune", exit_on_error=False)

    def on_inbox_screen_open_pull_request(self, message: InboxScreen.OpenPullRequest) -> None:
        self.open_review(message.ref, from_inbox=True)

    def open_review(self, ref: PRRef, *, from_inbox: bool) -> None:
        session = ReviewSession(self.backend, ref, cache=self.cache)
        self.push_screen(ReviewScreen(session, from_inbox=from_inbox))

    async def on_unmount(self) -> None:
        await self.backend.aclose()

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        if isinstance(screen, InboxScreen):
            yield SystemCommand(
                "Nicknames…", "Choose the names you see for people (@)", screen.action_nicknames
            )
        if isinstance(screen, ReviewScreen):
            commands = [
                ("Submit review…", "Approve, request changes or comment", "submit"),
                ("Approve…", "Approve these changes", "submit('APPROVE')"),
                ("Comment on the pull request", "Add to the conversation", "general_comment"),
                ("Go to file…", "Jump to a changed file", "find_file"),
                ("Toggle side-by-side view", "Split or unified diff", "diff_split"),
                ("Toggle file tree", "Show or hide the sidebar", "toggle_tree"),
                ("Hide test files", "Mark them viewed and hide them (T)", "hide_kind('test')"),
                (
                    "Hide generated files",
                    "Lockfiles, generated code… (X)",
                    "hide_kind('generated')",
                ),
                ("Refresh", "Reload the pull request from GitHub", "refresh"),
                ("Open in browser", "Open the current location on GitHub", "open_browser"),
                ("Show files", "Files tab", "switch_tab('files')"),
                ("Show conversation", "Conversation tab", "switch_tab('conversation')"),
                ("Nicknames…", "Choose the names you see for people (@)", "nicknames"),
                ("Keyboard shortcuts", "All the keys", "help"),
            ]
            for title, help_text, action in commands:
                if action == "diff_split":
                    yield SystemCommand(title, help_text, screen.diff.action_toggle_split)
                else:
                    yield SystemCommand(title, help_text, lambda a=action: screen.run_action(a))
