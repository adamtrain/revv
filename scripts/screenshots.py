"""Regenerate the README screenshots from the offline demo: uv run scripts/screenshots.py

They show made-up sample data (the same as `revv --demo`), with the clock frozen so the
images only change when the app does.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"


async def shoot(name: str, keys: list[str], size: tuple[int, int], inbox: bool = False) -> None:
    from revv.demo import DEMO_REF, NOW, DemoBackend
    from revv.ui import render
    from revv.ui.app import RevvApp

    render.clock = lambda: NOW + timedelta(hours=2)
    app = RevvApp(DemoBackend(latency=0), target=None if inbox else DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.5)
        for key in keys:
            if key.startswith("wait"):
                await pilot.pause(float(key[4:]))
            else:
                await pilot.press(key)
                await pilot.pause(0.05)
        await pilot.pause(0.3)
        svg = app.export_screenshot(title="revv")
    (DOCS / f"{name}.svg").write_text(svg)
    print(f"wrote docs/{name}.svg")


async def main() -> None:
    with tempfile.TemporaryDirectory() as home:
        # a clean config and no cache, whatever the machine has
        os.environ["XDG_CONFIG_HOME"] = str(Path(home) / "config")
        os.environ["XDG_CACHE_HOME"] = str(Path(home) / "cache")
        DOCS.mkdir(exist_ok=True)
        # the diff, with an inline thread under line 58 of client.py
        await shoot(
            "hero", ["1", "wait0.3", *["right_square_bracket"], *["n"] * 2, "wait0.2"], (118, 38)
        )
        await shoot("inbox", [], (104, 30), inbox=True)
        await shoot("conversation", ["wait0.3"], (110, 34))
        await shoot(
            "split",
            ["1", "wait0.2", "vertical_line", "right_curly_bracket", "right_curly_bracket"],
            (140, 30),
        )
        # suggesting a change on an added line
        await shoot(
            "comment",
            ["1", "wait0.2", "right_square_bracket", "right_curly_bracket", "j", "s", "wait0.2"],
            (110, 26),
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
