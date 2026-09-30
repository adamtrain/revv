"""The key guidance (in-app help and README) must cover every key binding."""

from pathlib import Path

from textual.binding import Binding

from revv.ui.app import RevvApp
from revv.ui.conversation import ConversationView
from revv.ui.dialogs import (
    HELP_SECTIONS,
    MAINTAINER_HELP,
    SubmitReviewDialog,
    display_key,
    help_markdown,
)
from revv.ui.diffview import DiffView
from revv.ui.editor import CommentEditor
from revv.ui.filetree import FileTree
from revv.ui.inbox import InboxScreen
from revv.ui.review import ReviewScreen

README = Path(__file__).resolve().parent.parent / "README.md"


def documented_keys() -> set[str]:
    sections = [*HELP_SECTIONS, MAINTAINER_HELP]  # the latter shows where that extra is on
    return {token for _, rows in sections for keys, _ in rows for token in keys.split()}


def test_every_binding_is_in_the_help() -> None:
    documented = documented_keys()
    missing = []
    for cls in (
        RevvApp,
        InboxScreen,
        ReviewScreen,
        DiffView,
        ConversationView,
        FileTree,
        CommentEditor,
        SubmitReviewDialog,
    ):
        for entry in cls.BINDINGS:
            binding = entry if isinstance(entry, Binding) else Binding(*entry)
            # "pagedown,ctrl+f,space" is one binding: any of its keys may document it
            keys = [display_key(key.strip()) for key in binding.key.split(",")]
            if not any(key in documented for key in keys):
                missing.append(f"{cls.__name__}: {binding.key} ({binding.action})")
    assert not missing, "undocumented keys:\n" + "\n".join(missing)


def test_readme_keys_match_the_help() -> None:
    text = README.read_text()
    start = text.index("<!-- keys -->\n") + len("<!-- keys -->\n")
    end = text.index("<!-- /keys -->")
    assert text[start:end].strip() == help_markdown().strip(), (
        "README keys are stale: run uv run scripts/readme_keys.py"
    )
