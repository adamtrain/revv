"""Small modal dialogs: confirm, submit review, help."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RadioButton, RadioSet, Static, TextArea

from revv.models import PullRequest, ReviewEvent


class ConfirmDialog(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmDialog { align: center middle; background: $background 55%; }
    ConfirmDialog > Vertical {
        width: auto; min-width: 44; max-width: 80; height: auto;
        background: $surface; border: round $error; padding: 1 2;
    }
    ConfirmDialog #message { width: auto; max-width: 76; margin-bottom: 1; }
    ConfirmDialog Horizontal { height: auto; width: auto; align-horizontal: right; }
    ConfirmDialog Button { margin-left: 2; min-width: 10; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "Yes", show=False),
        Binding("n,escape,q", "answer(False)", "No", show=False),
    ]

    def __init__(self, message: str, confirm: str = "Delete", *, danger: bool = True) -> None:
        super().__init__()
        self.message = message
        self.confirm = confirm
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message, id="message")
            with Horizontal():
                yield Button("Cancel (n)", id="no")
                yield Button(
                    f"{self.confirm} (y)", id="yes", variant="error" if self.danger else "primary"
                )

    def on_mount(self) -> None:
        self.query_one("#no" if self.danger else "#yes", Button).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


@dataclass(slots=True)
class SubmitResult:
    action: Literal["submit", "discard"]
    event: ReviewEvent = ReviewEvent.COMMENT
    body: str = ""


class SubmitReviewDialog(ModalScreen[SubmitResult | None]):
    DEFAULT_CSS = """
    SubmitReviewDialog { align: center middle; background: $background 55%; }
    SubmitReviewDialog > Vertical {
        width: 92%; max-width: 96; height: auto; max-height: 90%;
        background: $surface; border: round $primary; padding: 0 1;
        border-title-style: bold;
    }
    SubmitReviewDialog #summary { margin: 1 0; height: auto; }
    SubmitReviewDialog RadioSet { width: 100%; layout: horizontal; margin-bottom: 1; }
    SubmitReviewDialog RadioButton { width: auto; margin-right: 2; }
    SubmitReviewDialog TextArea { height: 8; border: tall $panel; }
    SubmitReviewDialog TextArea:focus { border: tall $accent; }
    SubmitReviewDialog #buttons { height: auto; margin: 1 0 0 0; }
    SubmitReviewDialog #buttons Button { margin-right: 2; }
    SubmitReviewDialog #hints { color: $text-muted; height: auto; margin-bottom: 1; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "submit", "Submit", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    EVENTS = (
        (ReviewEvent.COMMENT, "Comment", "Submit general feedback without explicit approval."),
        (ReviewEvent.APPROVE, "Approve", "Submit feedback and approve merging these changes."),
        (
            ReviewEvent.REQUEST_CHANGES,
            "Request changes",
            "Submit feedback that must be addressed before merging.",
        ),
    )

    def __init__(self, pr: PullRequest, preset: ReviewEvent | None = None, body: str = "") -> None:
        super().__init__()
        self.pr = pr
        self.preset = preset or ReviewEvent.COMMENT
        self.body = body

    def compose(self) -> ComposeResult:
        pr = self.pr
        pending = pr.pending_comment_count
        with Vertical() as box:
            box.border_title = "Finish your review"
            summary = Text()
            if pending:
                files = len({t.path for t in pr.threads if t.has_pending})
                summary.append(f"{pending} pending comment{'s' if pending != 1 else ''}", "bold")
                summary.append(f" on {files} file{'s' if files != 1 else ''} will be published.")
            else:
                summary.append("No pending comments.", "dim")
            if pr.viewer_did_author:
                summary.append(
                    "\nThis is your pull request, so you can only comment.", "italic dim"
                )
            yield Static(summary, id="summary")
            with RadioSet(id="event"):
                for event, label, _ in self.EVENTS:
                    disabled = pr.viewer_did_author and event is not ReviewEvent.COMMENT
                    yield RadioButton(
                        label, value=event is self.preset, disabled=disabled, name=event.value
                    )
            yield Static(self._describe(self.preset), id="describe")
            yield TextArea(
                self.body,
                soft_wrap=True,
                tab_behavior="focus",
                placeholder="Leave a summary (markdown, optional for approvals)",
                id="body",
            )
            with Horizontal(id="buttons"):
                yield Button("Submit review", variant="primary", id="submit")
                if pr.pending_review is not None:
                    yield Button("Discard pending review", variant="error", id="discard")
                yield Button("Cancel", id="cancel")
            yield Static(
                Text.assemble(
                    (" ctrl+s ", "bold reverse"),
                    " submit   ",
                    (" tab ", "bold reverse"),
                    " next field   ",
                    (" ← → ", "bold reverse"),
                    " choose   ",
                    (" esc ", "bold reverse"),
                    " cancel",
                ),
                id="hints",
            )

    def _describe(self, event: ReviewEvent) -> Text:
        for value, _, description in self.EVENTS:
            if value is event:
                return Text(description, style="italic dim")
        return Text()

    def on_mount(self) -> None:
        if self.preset is ReviewEvent.COMMENT and not self.body:
            self.query_one(RadioSet).focus()
        else:
            self.query_one(TextArea).focus()

    @property
    def event(self) -> ReviewEvent:
        pressed = self.query_one(RadioSet).pressed_button
        if pressed is None or pressed.name is None:
            return ReviewEvent.COMMENT
        return ReviewEvent(pressed.name)

    @on(RadioSet.Changed)
    def changed(self) -> None:
        self.query_one("#describe", Static).update(self._describe(self.event))

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.action_submit()
        elif event.button.id == "discard":
            self.dismiss(SubmitResult("discard"))
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        event = self.event
        body = self.query_one(TextArea).text.strip()
        if event is ReviewEvent.REQUEST_CHANGES and not body and not self.pr.pending_comment_count:
            self.notify("Say what needs to change (or add inline comments)", severity="warning")
            return
        if event is ReviewEvent.COMMENT and not body and not self.pr.pending_comment_count:
            self.notify("Write a comment first", severity="warning")
            return
        self.dismiss(SubmitResult("submit", event, body))

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Moving around",
        [
            ("j k ↓ ↑", "line down / up"),
            ("space ctrl+f / ctrl+b", "page down / up"),
            ("ctrl+d / ctrl+u", "half page down / up"),
            ("g G", "top / bottom"),
            ("} {", "next / previous change"),
            ("] [", "next / previous file"),
            ("n N", "next / previous comment thread"),
            ("h l", "left / right side (side-by-side view)"),
            ("/", "go to file…"),
            ("tab", "switch between file tree and diff"),
        ],
    ),
    (
        "Commenting",
        [
            ("c or ↵", "comment on the line (or the file, on its header)"),
            ("V", "select a range of lines, then c"),
            ("s", "suggest a change to the selected lines"),
            ("r", "reply to the thread"),
            ("x", "resolve / unresolve the thread (or conversation comment)"),
            ("e", "edit your comment"),
            ("d", "delete your comment"),
            ("z", "fold / unfold thread (or file)"),
        ],
    ),
    (
        "Reviewing",
        [
            ("v", "mark file as viewed (and move on)"),
            ("T", "mark all test files viewed & hide them (again: show)"),
            ("X", "mark all generated files viewed & hide them"),
            ("S", "submit review…"),
            ("A", "approve…"),
            ("C", "comment on the pull request"),
            ("1 2", "files / conversation"),
            ("|", "toggle side-by-side view"),
            ("E", "expand the whole file"),
            ("t", "toggle the file tree"),
            ("o", "open in the browser"),
            ("y", "copy file path and line"),
            ("R", "refresh from GitHub"),
            ("@", "nicknames for people"),
            ("ctrl+p", "command palette (themes and more)"),
            ("q", "back to inbox / quit"),
        ],
    ),
    (
        "In the editor",
        [
            ("ctrl+s", "add to review (or save)"),
            ("ctrl+g", "comment immediately (when no review is pending)"),
            ("ctrl+t", "insert a suggested change"),
            ("ctrl+o", "write in $EDITOR"),
            ("esc", "cancel (your draft is kept)"),
        ],
    ),
]


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 60%; }
    HelpScreen > VerticalScroll {
        width: 96; max-width: 96%; height: auto; max-height: 92%;
        background: $surface; border: round $primary; padding: 0 2;
        border-title-style: bold;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q,question_mark", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll() as scroll:
            scroll.border_title = "revv keys"
            scroll.border_subtitle = "esc to close"
            yield Static(self._table())

    def _table(self) -> Table:
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column()
        grid.add_column()
        halves = [HELP_SECTIONS[:2], HELP_SECTIONS[2:]]
        cells = []
        for half in halves:
            table = Table.grid(padding=(0, 1))
            table.add_column(style="bold", no_wrap=True)
            table.add_column()
            for title, rows in half:
                table.add_row(Text(title, style="bold underline"), "")
                for keys, description in rows:
                    table.add_row(Text(keys, style="bold"), Text(description, style="dim"))
                table.add_row("", "")
            cells.append(table)
        grid.add_row(*cells)
        return grid

    def action_close(self) -> None:
        self.dismiss(None)


class NicknameDialog(ModalScreen[dict[str, str] | None]):
    """Give people a name you'd rather see than their GitHub login."""

    DEFAULT_CSS = """
    NicknameDialog { align: center middle; background: $background 55%; }
    NicknameDialog > Vertical {
        width: 72; max-width: 95%; height: auto; max-height: 90%;
        background: $surface; border: round $primary; padding: 0 1;
        border-title-style: bold;
    }
    NicknameDialog #intro { color: $text-muted; margin: 1 0; height: auto; }
    NicknameDialog VerticalScroll { height: auto; max-height: 24; }
    NicknameDialog .person { height: 3; }
    NicknameDialog .person Static { width: 24; padding: 1 1 0 0; text-align: right; }
    NicknameDialog .person Input { width: 1fr; }
    NicknameDialog #hints { color: $text-muted; height: auto; margin: 1 0; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("down", "app.focus_next", "Next", show=False),
        Binding("up", "app.focus_previous", "Previous", show=False),
    ]

    def __init__(self, logins: list[str], nicknames: dict[str, str]) -> None:
        super().__init__()
        known = {login.lower(): login for login in logins}
        for login in nicknames:
            known.setdefault(login.lower(), login)
        self.logins = sorted(known.values(), key=str.lower)
        self.nicknames = {login.lower(): name for login, name in nicknames.items()}

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = "Nicknames"
            yield Static(
                "Shown instead of GitHub logins, only to you. Leave a field empty to "
                "use the login.",
                id="intro",
            )
            with VerticalScroll():
                for login in self.logins:
                    with Horizontal(classes="person"):
                        yield Static(login)
                        yield Input(
                            self.nicknames.get(login.lower(), ""),
                            placeholder=login,
                            name=login,
                            select_on_focus=False,
                        )
            yield Static(
                Text.assemble(
                    (" ctrl+s ", "bold reverse"),
                    " save   ",
                    (" ↑ ↓ ", "bold reverse"),
                    " move   ",
                    (" esc ", "bold reverse"),
                    " cancel",
                ),
                id="hints",
            )

    def on_mount(self) -> None:
        inputs = list(self.query(Input))
        if inputs:
            inputs[0].focus()

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.action_save()

    def action_save(self) -> None:
        names = {str(field.name): field.value.strip() for field in self.query(Input)}
        self.dismiss({login: name for login, name in names.items() if name})

    def action_cancel(self) -> None:
        self.dismiss(None)
