"""Regenerate the "Keys" tables in README.md from the in-app help: uv run scripts/readme_keys.py"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from revv.ui.dialogs import help_markdown

README = Path(__file__).resolve().parent.parent / "README.md"
BLOCK = re.compile(r"(<!-- keys -->\n).*?(<!-- /keys -->)", re.DOTALL)


def main() -> int:
    text = README.read_text()
    if not BLOCK.search(text):
        print("README.md has no <!-- keys --> … <!-- /keys --> block", file=sys.stderr)
        return 1
    updated = BLOCK.sub(lambda m: m.group(1) + "\n" + help_markdown() + "\n" + m.group(2), text)
    if updated != text:
        README.write_text(updated)
        print("updated the keys in README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
