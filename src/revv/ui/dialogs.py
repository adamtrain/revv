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

from revv.filters import CommentRule, words
from revv.models import REACTION_EMOJI, PullRequest, ReviewEvent


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
        "Inbox",
        [
            ("j k ↓ ↑", "move"),
            ("↵", "review the pull request (on a stack: unfold / fold it)"),
            ("→ ← l h", "unfold / fold a stack"),
            ("tab shift+tab", "next / previous tab"),
            ("/", "filter the list"),
            ("0-9", "type a pull request number (or paste a URL) and press ↵"),
            ("esc", "clear the filter"),
            ("s", "oldest first / newest first"),
            ("i", "ignore the pull request (in the Ignored tab: bring it back)"),
            ("a", "this repository / all repositories"),
            ("o", "open in the browser"),
            ("r R", "refresh"),
            ("@", "nickname the author (and a requested team)"),
            ("q", "quit"),
        ],
    ),
    (
        "Moving around a pull request",
        [
            ("j k ↓ ↑", "line down / up"),
            ("space ctrl+f", "page down (also pagedown)"),
            ("ctrl+b", "page up (also pageup)"),
            ("ctrl+d ctrl+u", "half page down / up"),
            ("g G", "top / bottom (also home / end)"),
            ("} {", "next / previous change"),
            ("] [", "next / previous file"),
            ("n N", "next / previous comment thread"),
            ("u U", "next / previous unresolved thread"),
            ("h l", "left / right side (side by side)"),
            ("/", "search the changed lines…"),
            ("f ctrl+k", "go to a file…"),
            ("1 2", "files / conversation"),
            ("tab", "between the file tree and the diff"),
        ],
    ),
    (
        "File tree",
        [
            ("j k", "move (the diff follows)"),
            ("↵ l", "open the file"),
            ("h", "fold the folder / go to its parent"),
            ("space", "fold / unfold a folder"),
        ],
    ),
    (
        "Commenting",
        [
            ("c ↵", "comment on the line (on a file header: on the file)"),
            ("V", "select lines, then c or s (esc clears)"),
            ("s", "suggest a change"),
            ("r", "reply"),
            ("x", "resolve / unresolve"),
            ("e", "edit your comment"),
            ("d", "delete your comment"),
            ("+", "react with an emoji"),
            ("i", "ignore comments like this one…"),
            ("z ↵", "fold / unfold a thread (or a file, on its header)"),
        ],
    ),
    (
        "Reviewing",
        [
            ("v", "mark the file viewed and move on"),
            ("T X", "show test / generated files; again: hide them and mark viewed"),
            ("L", "only the changes since your last review"),
            ("E", "expand the whole file"),
            ("|", "side by side / unified"),
            ("S", "submit review…"),
            ("A", "approve…"),
            ("C", "comment on the pull request"),
            ("R", "refresh (applies changes noticed on GitHub)"),
            ("o", "open in the browser"),
            ("y", "copy path:line"),
            ("t", "hide / show the file tree"),
            ("< >", "narrower / wider file tree"),
        ],
    ),
    (
        "Conversation",
        [
            ("j k g G", "move between entries"),
            ("↵ z", "on a thread: jump to the code; otherwise fold"),
            ("r", "reply (quoting a comment)"),
            ("x", "resolve / unresolve"),
            ("e d + i", "edit · delete · react · ignore"),
            ("C", "comment on the pull request"),
            ("D", "a long description: Claude's summary / the original"),
        ],
    ),
    (
        "Writing a comment",
        [
            ("ctrl+s", "add to your review (or save)"),
            ("ctrl+g", "comment right away (when no review is pending)"),
            ("ctrl+t", "insert a suggested change"),
            ("ctrl+o", "write it in $EDITOR"),
            ("esc", "cancel (the draft is kept)"),
        ],
    ),
    (
        "Everywhere",
        [
            (",", "settings"),
            ("@", "nicknames for the people and teams in view"),
            ("ctrl+p", "command palette: every action, themes"),
            ("?", "this help"),
            ("q", "back to the inbox / quit"),
            ("ctrl+q", "quit"),
        ],
    ),
]

# Only shown where the maintainer-teams extra is on (one repository).
MAINTAINER_HELP: tuple[str, list[tuple[str, str]]] = (
    "Maintainer teams",
    [
        ("m", "group files by maintainer team (yours first) / by folder"),
        ("M", "only the files your teams maintain / everything"),
    ],
)

KEY_DISPLAY = {
    "right_curly_bracket": "}",
    "left_curly_bracket": "{",
    "right_square_bracket": "]",
    "left_square_bracket": "[",
    "vertical_line": "|",
    "question_mark": "?",
    "slash": "/",
    "at": "@",
    "comma": ",",
    "plus": "+",
    "less_than_sign": "<",
    "greater_than_sign": ">",
    "escape": "esc",
    "enter": "↵",
    "down": "↓",
    "up": "↑",
    "left": "←",
    "right": "→",
}


def display_key(key: str) -> str:
    return KEY_DISPLAY.get(key, key)


def help_markdown() -> str:
    """The key reference as markdown tables (the README's "Keys" section is generated
    from this, so the app and the README can't disagree)."""
    blocks = []
    for title, rows in HELP_SECTIONS:
        lines = [f"| {title} | |", "| --- | --- |"]
        for keys, description in rows:
            shown = " ".join(f"`{key}`" for key in keys.split()).replace("`|`", "`\\|`")
            lines.append(f"| {shown} | {description} |")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 60%; }
    HelpScreen > VerticalScroll {
        width: 110; max-width: 96%; height: auto; max-height: 94%;
        background: $surface; border: round $primary; padding: 0 2;
        border-title-style: bold;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q,question_mark", "close", "Close", show=False),
    ]

    def __init__(self, first: str | None = None, *, maintainers: bool = False) -> None:
        super().__init__()
        self.first = first  # the section to show first (e.g. "Inbox")
        self.maintainers = maintainers  # the maintainer-teams extra is on here

    def compose(self) -> ComposeResult:
        with VerticalScroll() as scroll:
            scroll.border_title = "revv keys"
            scroll.border_subtitle = "esc to close · , settings · ctrl+p every action"
            yield Static(self._table())

    def _sections(self) -> list[tuple[str, list[tuple[str, str]]]]:
        sections = list(HELP_SECTIONS)
        if self.maintainers:
            sections.insert(-1, MAINTAINER_HELP)
        if self.first:
            sections.sort(key=lambda section: section[0] != self.first)
        elif sections and sections[0][0] == "Inbox":
            sections.append(sections.pop(0))  # in a pull request, the inbox comes last
        return sections

    def _table(self) -> Table:
        grid = Table.grid(padding=(0, 3), expand=True)
        grid.add_column()
        grid.add_column()
        sections = self._sections()
        middle = (len(sections) + 1) // 2
        cells = []
        for half in (sections[:middle], sections[middle:]):
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
    """Give people (and teams) a name you'd rather see than their GitHub login."""

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
        """`logins` are people's logins and teams' "org/team" names."""
        super().__init__()
        cleaned = (login.strip().lstrip("@") for login in logins)
        unique = {login.lower(): login for login in cleaned if login}
        self.logins = list(unique.values())
        self.nicknames = {login.lower(): name for login, name in nicknames.items()}

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = "Nickname" if len(self.logins) == 1 else "Nicknames"
            yield Static(
                "Shown instead of the GitHub login or team name, only to you. "
                "Leave it empty to use the original.",
                id="intro",
            )
            with VerticalScroll():
                for login in self.logins:
                    with Horizontal(classes="person"):
                        if "/" in login:  # a team: "org/team"
                            label = Text.assemble(("team ", "dim"), login.split("/", 1)[1])
                            yield Static(label)
                        else:
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
        """Returns every shown login with its (possibly empty, meaning "none") nickname."""
        self.dismiss({str(field.name): field.value.strip() for field in self.query(Input)})

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReactionPicker(ModalScreen[str | None]):
    """Pick an emoji reaction: number keys, or click. Yours are highlighted."""

    DEFAULT_CSS = """
    ReactionPicker { align: center middle; background: $background 40%; }
    ReactionPicker > Vertical {
        width: auto; height: auto; background: $surface; border: round $primary;
        padding: 0 1; border-title-style: bold;
    }
    ReactionPicker Horizontal { width: auto; height: auto; margin: 1 0 0 0; }
    ReactionPicker Button { min-width: 8; width: 8; margin: 0 1 0 0; border: none; height: 3; }
    ReactionPicker Button.mine { background: $primary 45%; }
    ReactionPicker #reaction-hints { color: $text-muted; margin: 1 0; width: auto; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        *[Binding(str(i + 1), f"pick({i})", show=False) for i in range(8)],
        Binding("escape,q", "cancel", "Cancel", show=False),
    ]

    def __init__(self, mine: set[str], title: str = "React") -> None:
        super().__init__()
        self.mine = mine
        self.title_text = title
        self.contents = list(REACTION_EMOJI)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self.title_text
            with Horizontal():
                for index, content in enumerate(self.contents):
                    yield Button(
                        f"{REACTION_EMOJI[content]} {index + 1}",
                        id=f"r-{content}",
                        classes="mine" if content in self.mine else "",
                    )
            yield Static(
                "1–8 to toggle a reaction · highlighted ones are yours · esc to close",
                id="reaction-hints",
            )

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss((event.button.id or "").removeprefix("r-") or None)

    def action_pick(self, index: int) -> None:
        self.dismiss(self.contents[index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class IgnoreCommentDialog(ModalScreen[CommentRule | None]):
    """Choose how to ignore comments like the highlighted one."""

    DEFAULT_CSS = """
    IgnoreCommentDialog { align: center middle; background: $background 55%; }
    IgnoreCommentDialog > Vertical {
        width: 84; max-width: 95%; height: auto; max-height: 90%;
        background: $surface; border: round $primary; padding: 0 1;
        border-title-style: bold;
    }
    IgnoreCommentDialog #excerpt { margin: 1 0; height: auto; color: $text-muted; }
    IgnoreCommentDialog RadioSet { width: 100%; margin-bottom: 1; }
    IgnoreCommentDialog Input { margin-bottom: 0; }
    IgnoreCommentDialog #ignore-hint { color: $text-muted; height: auto; margin: 0 0 1 1; }
    IgnoreCommentDialog #ignore-buttons { height: auto; margin-bottom: 1; }
    IgnoreCommentDialog #ignore-buttons Button { margin-right: 2; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "confirm", "Ignore", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, author: str, body: str) -> None:
        super().__init__()
        self.author = author
        self.body = body

    def compose(self) -> ComposeResult:
        excerpt = " ".join(self.body.split())
        suggestion = " ".join(words(self.body)[:6])
        with Vertical() as box:
            box.border_title = "Ignore comments like this one"
            yield Static(
                Text.assemble(
                    (f"@{self.author}: ", "bold"),
                    (f"“{excerpt[:220]}{'…' if len(excerpt) > 220 else ''}”", "italic"),
                ),
                id="excerpt",
            )
            with RadioSet(id="kind"):
                yield RadioButton(f"Everything from @{self.author}", name="author")
                yield RadioButton("Anything containing the words below", name="text")
                yield RadioButton(
                    f"From @{self.author}, containing the words below", name="both", value=True
                )
            yield Input(suggestion, placeholder="words to look for", id="words")
            yield Static(
                "Case and punctuation don't matter; the words must appear in this order "
                "(words may be the start of longer ones). Change or remove rules in settings (,).",
                id="ignore-hint",
            )
            with Horizontal(id="ignore-buttons"):
                yield Button("Ignore (ctrl+s)", variant="primary", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(RadioSet).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.action_confirm()
        else:
            self.dismiss(None)

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.action_confirm()

    def action_confirm(self) -> None:
        pressed = self.query_one(RadioSet).pressed_button
        kind = pressed.name if pressed is not None else "both"
        text = self.query_one(Input).value.strip()
        if kind in ("text", "both") and not text:
            self.notify("Enter some words to look for", severity="warning")
            return
        if kind == "author":
            self.dismiss(CommentRule(author=self.author))
        elif kind == "text":
            self.dismiss(CommentRule(text=text))
        else:
            self.dismiss(CommentRule(author=self.author, text=text))

    def action_cancel(self) -> None:
        self.dismiss(None)
