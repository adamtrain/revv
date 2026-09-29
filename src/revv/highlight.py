"""Syntax highlighting of diff lines with Pygments, producing Rich `Text` per line.

Lines are highlighted as a whole side of a file (all old lines, or all new lines) so that
multi-line constructs such as docstrings and block comments come out right. Background
colours from the Pygments theme are dropped: the diff view paints its own backgrounds.
"""

from __future__ import annotations

import threading
from functools import lru_cache

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import _TokenType
from pygments.util import ClassNotFound
from rich.style import Style
from rich.syntax import PygmentsSyntaxTheme
from rich.text import Text

TAB_SIZE = 4
MAX_HIGHLIGHT_CHARS = 400_000
MAX_HIGHLIGHT_LINE = 4_000  # minified code: not worth colouring, and slow to lex

# Control characters would corrupt the terminal; show them as Unicode control pictures.
_CONTROL = {c: 0x2400 + c for c in range(0x20) if c != 0x09} | {0x7F: 0x2421}

_EXTRA_FILENAMES = {
    "dockerfile": "docker",
    "justfile": "make",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "podfile": "ruby",
    "vagrantfile": "ruby",
    "brewfile": "ruby",
    "pipfile": "toml",
    "cargo.lock": "toml",
    "uv.lock": "toml",
    "poetry.lock": "toml",
    "go.mod": "go",
    ".bashrc": "bash",
    ".zshrc": "bash",
    ".envrc": "bash",
}


def display_text(text: str) -> str:
    """What we actually put on screen for a line of code."""
    if "\t" in text:
        text = text.expandtabs(TAB_SIZE)
    return text.translate(_CONTROL)


@lru_cache(maxsize=512)
def lexer_for(path: str) -> Lexer | None:
    name = path.rsplit("/", 1)[-1]
    options = {"stripnl": False, "stripall": False, "ensurenl": False}
    try:
        return get_lexer_for_filename(name, **options)
    except ClassNotFound:
        pass
    alias = _EXTRA_FILENAMES.get(name.lower())
    if alias is not None:
        try:
            return get_lexer_by_name(alias, **options)
        except ClassNotFound:
            return None
    return None


def language_name(path: str) -> str:
    lexer = lexer_for(path)
    return str(lexer.name) if lexer is not None else "Text"


class Highlighter:
    """Highlights code with one Pygments style; safe to share between threads."""

    def __init__(self, style_name: str) -> None:
        self.style_name = style_name
        self._theme = PygmentsSyntaxTheme(style_name)
        self._styles: dict[_TokenType, Style] = {}
        self._lock = threading.Lock()

    def _style(self, token_type: _TokenType) -> Style:
        style = self._styles.get(token_type)
        if style is None:
            base = self._theme.get_style_for_token(token_type)
            style = Style(
                color=base.color,
                bold=base.bold,
                italic=base.italic,
                underline=base.underline,
            )
            with self._lock:
                self._styles[token_type] = style
        return style

    def highlight(self, lines: list[str], path: str) -> list[Text]:
        """Highlight display lines (already tab-expanded) as one contiguous document."""
        lexer = lexer_for(path)
        if (
            lexer is None
            or sum(len(line) for line in lines) > MAX_HIGHLIGHT_CHARS
            or any(len(line) > MAX_HIGHLIGHT_LINE for line in lines)
        ):
            return [Text(line) for line in lines]
        code = "\n".join(lines)
        result: list[Text] = [Text()]
        style_for = self._style
        try:
            for token_type, value in lexer.get_tokens(code):
                if not value:
                    continue
                style = style_for(token_type)
                parts = value.split("\n")
                for index, part in enumerate(parts):
                    if index:
                        result.append(Text())
                    if part:
                        result[-1].append(part, style)
        except Exception:  # a misbehaving lexer should never take the viewer down
            return [Text(line) for line in lines]
        # Lexers may add or swallow a trailing newline; normalize to the input length.
        if len(result) > len(lines):
            del result[len(lines) :]
        while len(result) < len(lines):
            result.append(Text(lines[len(result)]))
        return result


# Pygments style to pair with each built-in Textual theme.
SYNTAX_THEMES: dict[str, str] = {
    "textual-dark": "github-dark",
    "textual-light": "default",
    "nord": "nord",
    "gruvbox": "gruvbox-dark",
    "catppuccin-mocha": "one-dark",
    "catppuccin-latte": "friendly",
    "catppuccin-frappe": "one-dark",
    "catppuccin-macchiato": "one-dark",
    "dracula": "dracula",
    "tokyo-night": "one-dark",
    "monokai": "monokai",
    "flexoki": "gruvbox-dark",
    "solarized-light": "solarized-light",
    "solarized-dark": "solarized-dark",
    "rose-pine": "one-dark",
    "rose-pine-moon": "one-dark",
    "rose-pine-dawn": "friendly",
    "atom-one-dark": "one-dark",
    "atom-one-light": "default",
    "textual-ansi": "native",
}


def syntax_theme_for(theme_name: str, dark: bool) -> str:
    return SYNTAX_THEMES.get(theme_name, "github-dark" if dark else "default")
