"""panc integration, with a fake `panc` executable on PATH (nothing is sent anywhere)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from revv import panc
from revv.cache import DiskCache
from revv.config import save_config
from revv.demo import DEMO_REF, DemoBackend
from revv.ui.app import RevvApp
from revv.ui.conversation import Card
from revv.ui.review import ReviewScreen

FAKE_PANC = """#!{python}
import json, os, sys
text = sys.stdin.read()
with open(os.environ["PANC_CALLS"], "a") as calls:
    calls.write("x")
print(json.dumps({{
    "prediction_short": "Mixed",
    "headline": "Mixed AI and human writing",
    "fraction_ai": 0.4,
    "fraction_ai_assisted": 0.2,
    "fraction_human": 0.4,
    "windows": [{{"text": text[:40], "label": "AI", "ai_assistance_score": 0.9,
                  "confidence": "High", "word_count": 7, "is_humanized": True}}],
}}))
"""


@pytest.fixture
def fake_panc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "panc"
    script.write_text(FAKE_PANC.format(python=sys.executable))
    script.chmod(0o755)
    calls = tmp_path / "panc-calls"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PANC_CALLS", str(calls))
    return calls


def calls(path: Path) -> int:
    return len(path.read_text()) if path.exists() else 0


async def test_check_parses_panc_output(fake_panc: Path) -> None:
    result = await panc.check("Some description text that is long enough to check.")
    assert result.error is None
    assert result.verdict == "Mixed" and result.fraction_ai == 0.4
    assert result.segments[0].ai_score == 0.9 and result.segments[0].humanized


async def test_missing_panc_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    result = await panc.check("text")
    assert result.error == "panc is not installed"


def test_changed_enough() -> None:
    text = "This pull request replaces the retry loop with a reusable policy. " * 5
    assert not panc.changed_enough(text, text)
    assert not panc.changed_enough(text, text.replace("reusable", "shared", 1))
    assert panc.changed_enough(text, text[: len(text) // 2])
    assert panc.changed_enough("", "something")


async def wait_for_verdict(pilot, screen: ReviewScreen) -> None:
    for _ in range(200):
        check = screen.ai_check
        if check is not None and not screen.conversation.ai_running:
            return
        await pilot.pause(0.02)
    raise AssertionError("panc never reported back")


async def test_runs_once_per_description_and_is_cached(fake_panc: Path, tmp_path: Path) -> None:
    save_config(panc=True)
    cache = DiskCache(tmp_path / "cache")
    backend = DemoBackend(latency=0)

    app = RevvApp(backend, target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, ReviewScreen)
        await wait_for_verdict(pilot, screen)
        assert screen.ai_check is not None and screen.ai_check.verdict == "Mixed"
        assert "MIXED" in str(screen.header.render())
        description = next(
            c for c in screen.conversation.query(Card) if c.item.kind == "description"
        )
        text = "\n".join(strip.text for strip in description.strips(description.size.width))
        assert "40% AI" in text and "humanized" in text
    assert calls(fake_panc) == 1

    # opening it again uses the cached verdict
    again = RevvApp(backend, target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
    async with again.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.2)
        screen = again.screen
        assert isinstance(screen, ReviewScreen)
        await wait_for_verdict(pilot, screen)
        for _ in range(20):
            if screen.session.fresh:
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
    assert calls(fake_panc) == 1

    # a substantially rewritten description is checked again
    backend._pr.body = "Completely different words now. " * 20
    third = RevvApp(backend, target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
    async with third.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.2)
        screen = third.screen
        assert isinstance(screen, ReviewScreen)
        for _ in range(200):
            if calls(fake_panc) == 2 and not screen.conversation.ai_running:
                break
            await pilot.pause(0.02)
    assert calls(fake_panc) == 2


async def test_off_by_default(fake_panc: Path) -> None:
    app = RevvApp(DemoBackend(latency=0), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.5)
        screen = app.screen
        assert isinstance(screen, ReviewScreen)
        assert screen.ai_check is None
    assert calls(fake_panc) == 0
