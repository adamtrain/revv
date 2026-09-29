"""Colours for the diff view, derived from whichever Textual theme is active."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cached_property

from rich.style import Style
from textual.app import App
from textual.color import Color

from revv.highlight import Highlighter, syntax_theme_for


def _color(variables: dict[str, str], name: str, fallback: str) -> Color:
    value = variables.get(name)
    try:
        color = Color.parse(value) if value else Color.parse(fallback)
    except Exception:
        return Color.parse(fallback)
    if color.ansi is not None or color.a == 0:
        return Color.parse(fallback)
    return color.with_alpha(1.0)


@dataclass(frozen=True, eq=False)
class Palette:
    name: str
    dark: bool
    bg: Color
    fg: Color
    surface: Color
    panel: Color
    primary: Color
    accent: Color
    success: Color
    error: Color
    warning: Color

    @classmethod
    def from_app(cls, app: App) -> Palette:
        theme = app.current_theme
        variables = app.get_css_variables()
        dark = theme.dark
        bg = _color(variables, "background", "#1e1e1e" if dark else "#f5f5f5")
        return cls(
            name=theme.name,
            dark=dark,
            bg=bg,
            fg=_color(variables, "foreground", "#e0e0e0" if dark else "#1e1e1e"),
            surface=_color(variables, "surface", bg.hex),
            panel=_color(variables, "panel", bg.hex),
            primary=_color(variables, "primary", "#0178D4"),
            accent=_color(variables, "accent", "#ffa62b"),
            success=_color(variables, "success", "#4EBF71"),
            error=_color(variables, "error", "#ba3c5b"),
            warning=_color(variables, "warning", "#ffa62b"),
        )

    # -- helpers -----------------------------------------------------------------

    def mix(self, color: Color, amount: float, base: Color | None = None) -> Color:
        return (base or self.bg).blend(color, amount)

    def fg_mix(self, amount: float) -> Color:
        """Foreground faded towards the background (1.0 = full foreground)."""
        return self.bg.blend(self.fg, amount)

    @cached_property
    def highlighter(self) -> Highlighter:
        return Highlighter(syntax_theme_for(self.name, self.dark))

    @property
    def syntax_theme(self) -> str:
        return self.highlighter.style_name

    # -- diff backgrounds ------------------------------------------------------------

    @cached_property
    def _k(self) -> float:
        return 1.0 if self.dark else 0.8

    @cached_property
    def add_bg(self) -> Color:
        return self.mix(self.success, 0.13 * self._k)

    @cached_property
    def add_emph_bg(self) -> Color:
        return self.mix(self.success, 0.36 * self._k)

    @cached_property
    def add_gutter_bg(self) -> Color:
        return self.mix(self.success, 0.22 * self._k)

    @cached_property
    def del_bg(self) -> Color:
        return self.mix(self.error, 0.13 * self._k)

    @cached_property
    def del_emph_bg(self) -> Color:
        return self.mix(self.error, 0.38 * self._k)

    @cached_property
    def del_gutter_bg(self) -> Color:
        return self.mix(self.error, 0.22 * self._k)

    @cached_property
    def context_gutter_bg(self) -> Color:
        return self.bg.blend(self.fg, 0.035)

    @cached_property
    def expanded_bg(self) -> Color:
        return self.bg.blend(self.fg, 0.02)

    @cached_property
    def hunk_bg(self) -> Color:
        return self.mix(self.primary, 0.10)

    @cached_property
    def header_bg(self) -> Color:
        return self.panel if self.panel != self.bg else self.bg.blend(self.fg, 0.08)

    @cached_property
    def thread_bg(self) -> Color:
        return self.bg.blend(self.fg, 0.045)

    @cached_property
    def cursor_gutter_bg(self) -> Color:
        return self.primary

    def cursor_bg(self, base: Color) -> Color:
        return base.blend(self.primary, 0.22)

    def select_bg(self, base: Color) -> Color:
        return base.blend(self.accent, 0.20)

    # -- styles ----------------------------------------------------------------------

    def style(
        self,
        fg: Color | None = None,
        bg: Color | None = None,
        *,
        bold: bool = False,
        dim: bool = False,
        italic: bool = False,
        underline: bool = False,
    ) -> Style:
        return Style(
            color=fg.rich_color if fg else None,
            bgcolor=bg.rich_color if bg else None,
            bold=bold or None,
            dim=dim or None,
            italic=italic or None,
            underline=underline or None,
        )

    @cached_property
    def text(self) -> Color:
        return self.fg_mix(0.92)

    @cached_property
    def muted(self) -> Color:
        return self.fg_mix(0.6)

    @cached_property
    def faint(self) -> Color:
        return self.fg_mix(0.38)

    @cached_property
    def add_fg(self) -> Color:
        return self.bg.blend(self.success, 0.9).blend(self.fg, 0.15)

    @cached_property
    def del_fg(self) -> Color:
        return self.bg.blend(self.error, 0.9).blend(self.fg, 0.15)

    @cached_property
    def warning_fg(self) -> Color:
        return self.bg.blend(self.warning, 0.9).blend(self.fg, 0.1)

    @cached_property
    def primary_fg(self) -> Color:
        return self.bg.blend(self.primary, 0.85).blend(self.fg, 0.2)

    @cached_property
    def accent_fg(self) -> Color:
        return self.bg.blend(self.accent, 0.9).blend(self.fg, 0.1)

    def author_color(self, login: str) -> Color:
        """A stable, readable colour per person."""
        digest = hashlib.md5(login.encode()).digest()
        hue = digest[0] / 255.0
        color = Color.from_hsl(hue, 0.55, 0.68 if self.dark else 0.38)
        return color
