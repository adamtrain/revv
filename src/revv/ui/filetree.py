"""The changed-files sidebar."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from revv.models import FileStatus
from revv.ui.diffmodel import FileSection
from revv.ui.palette import Palette


def tree_order(paths: list[str]) -> list[str]:
    """Order paths the way the tree shows them: folders first, then files, alphabetically."""
    root: dict = {}
    for path in paths:
        node = root
        *dirs, name = path.split("/")
        for part in dirs:
            node = node.setdefault(part + "/", {})
        node[name] = path

    ordered: list[str] = []

    def walk(node: dict) -> None:
        folders = sorted((k for k, v in node.items() if isinstance(v, dict)), key=str.lower)
        files = sorted((k for k, v in node.items() if not isinstance(v, dict)), key=str.lower)
        for key in folders:
            walk(node[key])
        for key in files:
            ordered.append(node[key])

    walk(root)
    return ordered


class FileTree(Tree[FileSection | None]):
    """Folders and changed files with status, comment and viewed markers."""

    DEFAULT_CSS = """
    FileTree {
        background: $surface;
        padding: 0;
        scrollbar-size-vertical: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "select_cursor", "Open", show=False),
        Binding("h", "collapse_or_parent", "Collapse", show=False),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("files", id=id)
        self.show_root = False
        self.guide_depth = 2
        self._path_nodes: dict[str, TreeNode[FileSection | None]] = {}
        self._palette: Palette | None = None

    @property
    def palette(self) -> Palette:
        if self._palette is None:
            self._palette = Palette.from_app(self.app)
        return self._palette

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, lambda _: self._retheme())

    def _retheme(self) -> None:
        self._palette = None
        for node in self._path_nodes.values():
            if node.data is not None:
                node.set_label(self.label_for(node.data))

    def build(self, sections: list[FileSection]) -> None:
        self.clear()
        self._path_nodes.clear()
        tree: dict = {}
        for section in sections:
            node = tree
            *dirs, _ = section.path.split("/")
            for part in dirs:
                node = node.setdefault(part + "/", {})
            node[section.path] = section

        def compress(name: str, node: dict) -> tuple[str, dict]:
            # Merge chains of folders that only contain one folder: src/ + revv/ -> src/revv/
            while len(node) == 1:
                ((key, value),) = node.items()
                if not isinstance(value, dict):
                    break
                name, node = name + key, value
            return name, node

        def add(parent: TreeNode[FileSection | None], node: dict) -> None:
            folders = sorted((k for k, v in node.items() if isinstance(v, dict)), key=str.lower)
            files = sorted(
                (v for v in node.values() if not isinstance(v, dict)), key=lambda s: s.path.lower()
            )
            for key in folders:
                name, child = compress(key, node[key])
                branch = parent.add(Text(name, style="bold"), data=None, expand=True)
                add(branch, child)
            for section in files:
                leaf = parent.add_leaf(self.label_for(section), data=section)
                self._path_nodes[section.path] = leaf

        add(self.root, tree)
        self.root.expand_all()

    def label_for(self, section: FileSection) -> Text:
        p = self.palette
        file = section.file
        status_color = {
            FileStatus.ADDED: p.add_fg,
            FileStatus.REMOVED: p.del_fg,
            FileStatus.RENAMED: p.primary_fg,
            FileStatus.COPIED: p.primary_fg,
        }.get(file.status, p.warning_fg)
        name = file.path.rsplit("/", 1)[-1]
        viewed = file.is_viewed
        text = Text()
        text.append(file.status.letter + " ", p.style(status_color, bold=True))
        text.append(name, p.style(p.faint) if viewed else p.style(p.text))
        if section.pending_count:
            text.append(f" ✎{section.pending_count}", p.style(p.warning_fg))
        if section.unresolved_count:
            text.append(f" ●{section.unresolved_count}", p.style(p.accent_fg))
        if viewed:
            text.append(" ✓", p.style(p.add_fg, bold=True))
        if "test" in section.kinds:
            text.append(" test", p.style(p.faint, italic=True))
        elif "generated" in section.kinds:
            text.append(" gen", p.style(p.faint, italic=True))
        return text

    def refresh_labels(self) -> None:
        for node in self._path_nodes.values():
            if node.data is not None:
                node.set_label(self.label_for(node.data))

    def reveal(self, section: FileSection) -> None:
        """Move the tree cursor to a file without stealing focus."""
        node = self._path_nodes.get(section.path)
        if node is not None and self.cursor_node is not node:
            self.move_cursor(node, animate=False)

    def action_collapse_or_parent(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.move_cursor(node.parent)
