"""The comment editor: a modal with context, a markdown text area, and submit keys."""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.console import RenderableType
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

EditorMode = Literal["thread", "reply", "edit", "issue", "review-body"]

# Unsent text survives closing the editor (keyed by what was being commented on).
DRAFTS: dict[str, str] = {}


@dataclass(slots=True)
class EditorResult:
    body: str
    publish_now: bool = False


class CommentEditor(ModalScreen[EditorResult | None]):
    """Write a comment. ctrl+s adds it to your review (or saves), ctrl+g posts right away."""

    DEFAULT_CSS = """
    CommentEditor {
        align: center middle;
        background: $background 55%;
    }
    CommentEditor > #editor {
        width: 92%;
        max-width: 116;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $primary;
        border-title-color: $text;
        border-title-style: bold;
        padding: 0 1;
    }
    CommentEditor #context {
        height: auto;
        max-height: 12;
        margin: 0 0 1 0;
    }
    CommentEditor TextArea {
        height: 12;
        min-height: 5;
        border: tall $panel;
    }
    CommentEditor TextArea:focus {
        border: tall $accent;
    }
    CommentEditor #hints {
        height: auto;
        color: $text-muted;
        padding: 0 0;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "submit(False)", "Save", priority=True),
        Binding("ctrl+g", "submit(True)", "Comment now", priority=True),
        Binding("ctrl+o", "external_editor", "$EDITOR", priority=True),
        Binding("ctrl+t", "insert_suggestion", "Suggest", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(
        self,
        title: str,
        *,
        mode: EditorMode,
        context: RenderableType | None = None,
        initial: str = "",
        draft_key: str | None = None,
        pending_review: bool = False,
        pending_count: int = 0,
        suggestion: str | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.mode = mode
        self.context = context
        self.draft_key = draft_key
        self.initial = DRAFTS.get(draft_key, initial) if draft_key else initial
        self.pending_review = pending_review
        self.pending_count = pending_count
        self.suggestion = suggestion

    @property
    def can_publish_now(self) -> bool:
        return self.mode in ("thread", "reply") and not self.pending_review

    def compose(self) -> ComposeResult:
        with Vertical(id="editor") as box:
            box.border_title = self.title_text
            if self.context is not None:
                yield Static(self.context, id="context")
            yield TextArea(
                self.initial,
                soft_wrap=True,
                tab_behavior="indent",
                show_line_numbers=False,
                highlight_cursor_line=False,
                placeholder=self._placeholder(),
                id="body",
            )
            yield Static(self._hints(), id="hints")

    def _placeholder(self) -> str:
        return {
            "thread": "Leave a comment (markdown supported)",
            "reply": "Write a reply",
            "edit": "Edit your comment",
            "issue": "Add a comment to the conversation",
            "review-body": "Summarize your review (optional)",
        }[self.mode]

    def _hints(self) -> Text:
        def key(k: str, label: str) -> Text:
            return Text.assemble((f" {k} ", "bold reverse"), f" {label}   ")

        parts: list[Text] = []
        if self.mode in ("thread", "reply"):
            if self.pending_review:
                label = f"add to review ({self.pending_count} pending)"
                parts.append(key("ctrl+s", label))
            else:
                parts.append(key("ctrl+s", "start a review"))
                parts.append(key("ctrl+g", "comment now"))
        elif self.mode == "edit":
            parts.append(key("ctrl+s", "save"))
        else:
            parts.append(key("ctrl+s", "comment" if self.mode == "issue" else "done"))
        if self.suggestion is not None:
            parts.append(key("ctrl+t", "suggest change"))
        parts.append(key("ctrl+o", "$EDITOR"))
        parts.append(key("esc", "cancel"))
        return Text.assemble(*parts)

    def on_mount(self) -> None:
        area = self.query_one(TextArea)
        area.focus()
        area.move_cursor(area.document.end)

    @property
    def text(self) -> str:
        return self.query_one(TextArea).text

    def action_submit(self, now: bool) -> None:
        body = self.text.strip()
        if not body:
            self.notify("Write something first", severity="warning", timeout=2)
            return
        if now and not self.can_publish_now:
            if self.mode in ("thread", "reply"):
                self.notify(
                    "You have a pending review, so this goes into it (ctrl+s)",
                    severity="warning",
                    timeout=3,
                )
            return
        if self.draft_key:
            DRAFTS.pop(self.draft_key, None)
        self.dismiss(EditorResult(body, publish_now=now))

    def action_cancel(self) -> None:
        text = self.text
        if self.draft_key:
            if text.strip() and text != self.initial:
                DRAFTS[self.draft_key] = text
                self.notify("Draft kept — it will be back next time", timeout=2)
            elif not text.strip():
                DRAFTS.pop(self.draft_key, None)
        self.dismiss(None)

    def action_insert_suggestion(self) -> None:
        if self.suggestion is None:
            self.notify("Suggestions only apply to lines of the new version", severity="warning")
            return
        area = self.query_one(TextArea)
        block = f"```suggestion\n{self.suggestion}\n```\n"
        if area.text and not area.text.endswith("\n"):
            block = "\n" + block
        area.insert(block)

    def action_external_editor(self) -> None:
        area = self.query_one(TextArea)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as handle:
            handle.write(area.text)
            path = handle.name
        try:
            with self.app.suspend():
                subprocess.run([*shlex.split(editor), path], check=False)
            with open(path, encoding="utf-8") as handle:
                area.load_text(handle.read().rstrip("\n"))
            area.move_cursor(area.document.end)
        except OSError as error:
            self.notify(f"Couldn't run {editor}: {error}", severity="error")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
